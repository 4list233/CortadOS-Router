# PURPOSE: Measure logical payload extents and own reservation arithmetic/monitoring.
# CONTRACT: RT-COWORK-MCP-008 v5 R064–067; monitoring is not a hard filesystem quota.
# DEPENDS: Runtime root descriptors, task_store SQL and existing Broker.stop.
# CEILING: No retention cleanup, quota policy replacement or optimistic write credit.
import asyncio
import os
import stat
import time

from . import remote_schemas as s
from . import task_store as store


class CapacityMonitor:
    def __init__(self, runtime):
        self.runtime = runtime
        self.closed = False
        self.last = None
        self.invalid_tasks = set()
        self.unknown = False

    def measure(self):
        runtime = self.runtime
        tasks = store.rows(runtime.broker.store.db, 'remote_tasks')
        known = {task.task_id: task for task in tasks}
        measured = {task.task_id: 0 for task in tasks}
        self.invalid_tasks = set()
        self.unknown = False
        seen = set()
        total = 0

        def walk(fd, task_id):
            amount = 0
            for name in os.listdir(fd):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        if os.fstat(child).st_ino != info.st_ino:
                            raise s.RemoteError('capacity_exhausted')
                        amount += walk(child, task_id)
                    finally:
                        os.close(child)
                else:
                    identity = (info.st_dev, info.st_ino)
                    if identity not in seen:
                        amount += info.st_size
                        seen.add(identity)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or (task_id in known and info.st_size > known[task_id].policy.limits.file_bytes)):
                        self.invalid_tasks.add(task_id)
            return amount

        for key in ('tasks_dir', 'staging_dir', 'blobs_dir'):
            root = runtime.root_fds[key]
            for name in os.listdir(root):
                info = os.stat(name, dir_fd=root, follow_symlinks=False)
                if name not in known:
                    self.unknown = True
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                    try:
                        amount = walk(child, name)
                    finally:
                        os.close(child)
                else:
                    amount = info.st_size
                    self.unknown = True
                total += amount
                if name in measured:
                    measured[name] += amount
        reservations = store.rows(runtime.broker.store.db, 'remote_reservations', "state='held'")
        promised = {task.task_id: 0 for task in tasks}
        for reservation in reservations:
            promised[reservation.task_id] += max(0, reservation.bytes - reservation.materialized_bytes)
        available = os.fstatvfs(runtime.root_fds['tasks_dir'])
        usage = s.CapacityUsage(tasks=[dict(owner_id=task.owner_id, task_id=task.task_id,
            measured_bytes=measured[task.task_id], promised_bytes=promised[task.task_id],
            artifact_slots=len(store.rows(runtime.broker.store.db, 'remote_artifacts', 'owner_id=? AND task_id=?', (task.owner_id, task.task_id)))) for task in tasks],
            installation_measured_bytes=total, installation_promised_bytes=sum(promised.values()),
            free_bytes=available.f_bavail * available.f_frsize, sampled_at_ms=store.now())
        self.last = usage
        return usage

    def check(self, owner_id, task_id, amount, *, measured=None):
        usage = measured if measured is not None else self.measure()
        task = store.get_owned(self.runtime.broker.store.db, owner_id, task_id)
        item = next(item for item in usage.tasks if item.task_id == task_id and item.owner_id == owner_id)
        limits = self.runtime.config.limits
        if (self.unknown or task_id in self.invalid_tasks
                or item.measured_bytes + item.promised_bytes + amount > task.policy.limits.task_bytes
                or usage.installation_measured_bytes + usage.installation_promised_bytes + amount > limits.installation_bytes
                or usage.free_bytes < usage.installation_promised_bytes + amount + limits.disk_headroom_bytes):
            raise s.RemoteError('capacity_exhausted')

    def reserve(self, tx, owner_id, task_id, operation, request_id, kind, bytes):
        # The caller samples under admission/task locks immediately before BEGIN.
        # No filesystem operation is allowed inside a JobStore transaction.
        if self.last is None:
            raise s.RemoteError('capacity_exhausted')
        self.check(owner_id, task_id, bytes, measured=self.last)
        return store.save(tx, 'remote_reservations', s.ReservationRow(reservation_id=store.uid(),
            owner_id=owner_id, task_id=task_id, operation=operation, request_id=request_id, kind=kind,
            bytes=bytes, materialized_bytes=0, state='held'), insert=True)

    async def run(self):
        interval = self.runtime.config.limits.workspace_poll_ms / 1000
        previous = None
        while not self.closed:
            start = time.monotonic()
            try:
                if previous is not None and start - previous > interval:
                    raise s.RemoteError('capacity_exhausted')
                usage = self.measure()
                limits = self.runtime.config.limits
                installation_bad = (self.unknown or usage.installation_measured_bytes + usage.installation_promised_bytes > limits.installation_bytes
                                    or usage.free_bytes < usage.installation_promised_bytes + limits.disk_headroom_bytes)
                if installation_bad:
                    self.runtime.fail_capacity()
                for item in usage.tasks:
                    task = store.get_owned(self.runtime.broker.store.db, item.owner_id, item.task_id)
                    bad = (installation_bad or task.task_id in self.invalid_tasks
                           or item.measured_bytes + item.promised_bytes > task.policy.limits.task_bytes
                           or item.artifact_slots > task.policy.limits.artifacts_per_task)
                    if bad:
                        self.runtime.fail_capacity()
                    if bad and task.active_job_id:
                        await self.runtime.broker.stop(task.active_job_id, 'capacity_exhausted')
                    if task.task_id in self.invalid_tasks and task.state not in ('deleted', 'quarantined', 'busy'):
                        with self.runtime.broker.store.transaction() as tx:
                            store.transition(tx, task.owner_id, task.task_id, task.version, 'quarantined')
                if time.monotonic() - start > interval:
                    raise s.RemoteError('capacity_exhausted')
            except (OSError, s.RemoteError) as exc:
                if isinstance(exc, s.RemoteError) and exc.code != 'capacity_exhausted':
                    self.runtime.fatal()
                self.runtime.fail_capacity()
                for record in self.runtime.broker.store.active():
                    if record.get('execution_kind') == 'cowork_remote_v1':
                        await self.runtime.broker.stop(record['job_id'], 'capacity_exhausted')
            previous = start
            # Wake before the hard next-sample deadline; no lateness tolerance is added.
            await asyncio.sleep(max(0, (interval - (time.monotonic() - start)) / 2))

    async def close(self):
        self.closed = True
