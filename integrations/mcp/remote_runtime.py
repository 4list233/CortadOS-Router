# PURPOSE: Own the complete local-core lock, recovery, sessions and worker lifetime.
# CONTRACT: RT-COWORK-MCP-008 v6 R010–018,068–070,074,093–102.
# DEPENDS: Existing Broker/JobStore, remote owners and descriptor-relative roots.
# CEILING: No listener, installation, credential discovery, OAuth or automatic relaunch.
import asyncio
import contextvars
import fcntl
import hashlib
import os
import socket
import stat
from types import SimpleNamespace

from . import remote_schemas as s
from . import task_store as store

CURRENT = contextvars.ContextVar('cowork_core_runtime')


def current():
    return CURRENT.get()


class CoreRuntime:
    def __init__(self, *, base_raw, core_raw, manifest_raw):
        self.base_raw, self.core_raw, self.manifest_raw = base_raw, core_raw, manifest_raw
        self.lock_fd = None
        self.root_fds = {}
        self.broker = None
        self.monitor_task = None
        self.monitor_stop_owned = False
        self.containment_task = None
        self.sessions = {}
        self.leases = {}
        self.descriptors = {}
        self.eof_leases = set()
        self.lease_timers = {}
        self.ready = False
        self.enabled = False
        self.admission_failed = False
        self.capacity_failed = False
        self.fatal_failed = False
        self.fatal_signalled = False
        self.fatal_signal_failed = False
        self.token = None
        self.task_locks = {}

    def authorize(self, principal, operation=None):
        self.observe_monitor(self.monitor_task)
        if principal != self.config.principal or not self.enabled:
            raise s.RemoteError('permission_refused')
        if not self.ready or self.fatal_failed or self.broker.store.admission_closed:
            raise s.RemoteError('storage_error')
        if self.capacity_failed and operation not in ('attach', 'bind', 'task_get', 'job_status', 'job_result', 'job_cancel', 'close'):
            raise s.RemoteError('capacity_exhausted')

    def observe_monitor(self, task):
        if task is None or not task.done():
            return
        if not task.cancelled():
            task.exception()  # Consume even after another detector has latched.
        if self.monitor_stop_owned:
            return
        if self.containment_task is None:
            self.containment_task = asyncio.create_task(
                self.broker.contain_remote('capacity_exhausted'))
        try:
            self.fatal()
        except Exception:
            # Sink failure remains latched for safe teardown reporting.
            self.fatal_signal_failed = True

    def fail_capacity(self):
        if self.capacity_failed:
            return
        # Latch before auditing: failure is fatal, never a second transition attempt.
        self.capacity_failed = True
        self.admission_failed = True
        if self.fatal_failed or self.broker.store.admission_closed:
            self.fatal()
            return
        try:
            with self.broker.store.transaction() as tx:
                store.event(tx, 'runtime', None, 'ready' if self.enabled else 'disabled',
                            'failed', reason='capacity_exhausted')
        except Exception:
            self.fatal()

    def fatal(self):
        self.admission_failed = True
        self.fatal_failed = True
        if self.broker:
            self.broker.store.admission_closed = True
        for timer in self.lease_timers.values():
            timer.cancel()
        self.lease_timers.clear()
        for fd in tuple(self.descriptors.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self.descriptors.clear()
        self.leases.clear()
        self.eof_leases.clear()
        if not self.fatal_signalled:
            self.fatal_signalled = True
            try:
                s.emit_remote_event(None, s.RemoteEvent(schema='cortados.cowork-event.v1',
                    event_id=store.uid(), at_ms=store.now(), event='refusal', entity='runtime',
                    object_id=None, request_id=None, from_state=None, state=None, reason='storage_error'))
            except Exception:
                self.fatal_signal_failed = True
                raise s.RemoteError('storage_error') from None
        if self.fatal_signal_failed:
            raise s.RemoteError('storage_error')

    async def __aenter__(self):
        from .config import load_config
        from .remote_config import load_core_config, policy_snapshot
        from .remote_native import load_manifest
        try:
            self.base = load_config(None, raw=self.base_raw)
            self.config = load_core_config(self.core_raw, self.base)
            self.snapshot = policy_snapshot(self.config)
            self.manifest = load_manifest(self.manifest_raw, self.base, self.config.execution_policy)
        except Exception as exc:
            raise s.RemoteError('policy_unavailable', 'Local core configuration is unavailable') from exc
        try:
            for name, path in s.plain(self.config.roots).items():
                self.root_fds[name] = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.lock_fd = os.open('broker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.root_fds['state_dir'])
            info = os.fstat(self.lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise s.RemoteError('storage_error')
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.token = CURRENT.set(self)
            from .broker import Broker
            self.broker = Broker(self.base, hashlib.sha256(self.base_raw).hexdigest(), core_runtime=self)
            with self.broker.store.transaction() as tx:
                store.migrate(tx)
                for table in store.TABLES:
                    store.rows(tx, table)
                for encoded, in tx.execute('SELECT event_json FROM remote_audit'):
                    s.RemoteEvent(**s.strict_json(encoded.encode('utf-8')))
                for ticket in store.rows(tx, 'remote_tickets'):
                    if ticket.state in ('issued', 'claimed'):
                        store.save(tx, 'remote_tickets', store.changed(ticket, state='revoked', lease_id=None, connection_id=None))
            self.broker.recover()
            await self.recover()
            from .remote_capacity import CapacityMonitor
            from .remote_dispatch import CoreBroker
            self.capacity = CapacityMonitor(self)
            self.core = CoreBroker(self)
            self.enabled = self.config.principal.enabled
            with self.broker.store.transaction() as tx:
                store.event(tx, 'runtime', None, state='ready')
            self.ready = True
            self.monitor_task = asyncio.create_task(self.capacity.run())
            self.monitor_task.add_done_callback(self.observe_monitor)
            return self
        except Exception as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            if isinstance(exc, s.RemoteError):
                raise
            raise s.RemoteError('storage_error') from exc

    async def __aexit__(self, exc_type, exc, tb):
        failures = []
        def record_fatal():
            try:
                self.fatal()
            except Exception as error:
                failures.append(error)
        self.observe_monitor(self.monitor_task)
        self.ready = False
        self.monitor_stop_owned = True
        for session in tuple(self.sessions.values()):
            session.socket.close()
            session.task.cancel()
        if self.sessions:
            results = await asyncio.gather(*(session.task for session in self.sessions.values()), return_exceptions=True)
            if any(isinstance(result, Exception) for result in results):
                record_fatal()
        from . import file_transfer
        for lease in tuple(self.leases.values()):
            try:
                file_transfer.close(lease, 'abort')
            except Exception:
                record_fatal()
        if self.monitor_task:
            await self.capacity.close()
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        if self.containment_task:
            await asyncio.gather(self.containment_task, return_exceptions=True)
        if self.broker:
            try:
                await self.broker.shutdown()
            except Exception:
                record_fatal()
                if self.broker.workers or any(not task.done() for task in self.broker.tasks):
                    # Unknown process containment retains lifetime ownership.
                    raise s.RemoteError('containment_failed')
            try:
                if not self.broker.store.admission_closed:
                    with self.broker.store.transaction() as tx:
                        store.event(tx, 'runtime', None, state='closed')
            except Exception:
                record_fatal()
            self.broker.store.close()
        for fd in self.root_fds.values():
            os.close(fd)
        self.root_fds.clear()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.token is not None:
            CURRENT.reset(self.token)
            self.token = None
        if failures or self.fatal_signal_failed:
            raise s.RemoteError('storage_error') from None

    async def attach(self, server_socket, principal):
        if not self.ready or principal != self.config.principal or not self.enabled:
            raise s.RemoteError('permission_refused')
        self.authorize(principal, 'attach')
        if server_socket.family != socket.AF_UNIX or server_socket.type != socket.SOCK_STREAM:
            raise s.RemoteError('permission_refused')
        from .remote_ipc import serve_endpoint
        connection_id = store.uid()
        session = SimpleNamespace(connection_id=connection_id, socket=server_socket,
                                  closed=asyncio.get_running_loop().create_future())
        session.task = asyncio.create_task(serve_endpoint(server_socket, self.core, principal))
        self.sessions[connection_id] = session
        def done(task):
            if not session.closed.done():
                session.closed.set_result(None)
        session.task.add_done_callback(done)
        return session

    async def disable_owner(self, owner_id):
        from . import transfer_tickets, file_transfer
        if owner_id != s.owner(self.config.principal):
            raise s.RemoteError('permission_refused')
        async with self.broker.admission_lock:
            if not self.enabled:
                return
            self.enabled = False
            with self.broker.store.transaction() as tx:
                store.event(tx, 'runtime', None, 'ready', 'disabled')
            with self.broker.store.transaction() as tx:
                transfer_tickets.revoke(tx, owner_id, None)
            for lease in tuple(self.leases.values()):
                try:
                    file_transfer.close(lease, 'abort')
                except Exception:
                    self.fatal()

    def task(self, task_id):
        return store.get_owned(self.broker.store.db, s.owner(self.config.principal), task_id)

    def binding(self, record):
        if not self.classify(record):
            raise s.RemoteError('policy_unavailable')
        return store.rows(self.broker.store.db, 'remote_job_bindings', 'job_id=?', (record['job_id'],))[0]

    def classify(self, record):
        # Persisted discriminator/binding disagreement is integrity loss, unlike
        # a preacceptance load_manifest refusal for a changed binary generation.
        try:
            return self._classify(record)
        except Exception:
            self.fatal()
            raise

    def _classify(self, record):
        found = store.rows(self.broker.store.db, 'remote_job_bindings', 'job_id=?', (record['job_id'],))
        if 'execution_kind' not in record and not found:
            return False
        if record.get('execution_kind') != 'cowork_remote_v1' or len(found) != 1:
            raise s.RemoteError('policy_unavailable')
        binding = found[0]
        seed = binding.seed
        task = self.task(seed.task_id)
        from .remote_native import worker_binding, verify_binding
        verify_binding(worker_binding(binding), seed.native_manifest)
        operations = store.rows(self.broker.store.db, 'remote_operations',
            'owner_id=? AND operation=? AND request_id=?', (seed.owner_id, 'delegate', seed.request_id))
        publications = store.rows(self.broker.store.db, 'remote_publications', 'job_id=?', (binding.job_id,))
        if (len(operations) != 1 or operations[0].object_ids.job_id != binding.job_id
                or operations[0].object_ids.task_id != seed.task_id
                or operations[0].argument_hash != seed.argument_hash
                or len(publications) != 1 or publications[0].owner_id != seed.owner_id
                or publications[0].task_id != seed.task_id
                or seed.execution_policy != task.policy.execution_policy):
            raise s.RemoteError('policy_unavailable')
        prepared = store.rows(self.broker.store.db, 'remote_preparations', 'preparation_id=?', (seed.preparation_id,))
        if (seed.owner_id != task.owner_id or seed.task_group_id != task.group_id
                or seed.policy_snapshot != task.policy or not prepared):
            raise s.RemoteError('policy_unavailable')
        prep = prepared[0]
        active = self.broker.store.db.execute('SELECT active FROM remote_job_bindings WHERE job_id=?',
                                              (binding.job_id,)).fetchone()
        if active != (int(task.active_job_id == binding.job_id),):
            raise s.RemoteError('policy_unavailable')
        if (prep.phase != 'adopted' or prep.job_id != binding.job_id
                or prep.owner_id != seed.owner_id or prep.task_id != seed.task_id
                or prep.request_id != seed.request_id or prep.argument_hash != seed.argument_hash
                or prep.expected_task_version != seed.expected_task_version
                or prep.workspace_identity != binding.workspace_identity
                or binding.workspace_identity is None
                or binding.workspace_identity.relative_name != prep.relative_name
                or prep.relative_name != seed.task_id + '/runs/' + seed.request_id
                or record['arguments']['request_id'] != seed.request_id
                or record['cwd'] != os.path.join(task.policy.roots.tasks_dir, prep.relative_name)):
            raise s.RemoteError('policy_unavailable')
        return True

    record_dict = staticmethod(s.plain)

    def materialize_inputs(self, binding):
        with self.broker.store.transaction() as tx:
            reservations = store.rows(tx, 'remote_reservations', 'owner_id=? AND task_id=? AND request_id=? AND kind=?',
                (binding.seed.owner_id, binding.seed.task_id, binding.seed.request_id, 'workspace_copy'))
            for reservation in reservations:
                store.save(tx, 'remote_reservations', store.changed(reservation,
                    materialized_bytes=reservation.bytes, state='materialized'))

    async def publish(self, job_id):
        from .artifact_store import snapshot
        from .job_store import TERMINAL
        record = self.broker.store.get(job_id)
        binding = self.binding(record)
        publication = store.rows(self.broker.store.db, 'remote_publications', 'job_id=?', (job_id,))[0]
        if publication.state != 'pending':
            return
        if record['state'] not in TERMINAL or record['reason'] == 'containment_failed':
            state, manifest, error = 'blocked', (), s.RemoteError('containment_failed').payload()['error']
        else:
            try:
                manifest = snapshot(binding)
                state, error = 'complete', None
            except Exception:
                state, manifest, error = 'failed', (), s.RemoteError('publication_failed').payload()['error']
        with self.broker.store.transaction() as tx:
            store.save(tx, 'remote_publications', store.changed(publication, state=state, manifest=s.plain(manifest), error=error))
            task = self.task(binding.seed.task_id)
            if task.state == 'busy':
                task = store.transition(tx, task.owner_id, task.task_id, task.version, 'quarantined' if state == 'blocked' else 'ready')
                store.save(tx, 'remote_tasks', store.changed(task, active_job_id=None if state != 'blocked' else job_id))
            if state != 'blocked':
                tx.execute('UPDATE remote_job_bindings SET active=0 WHERE job_id=?', (job_id,))

    async def recover(self):
        from .task_workspace import remove_preparation, open_dir, reconcile
        preparations = store.rows(self.broker.store.db, 'remote_preparations', "phase IN ('intent','prepared')")
        for intent in preparations:
            task = self.task(intent.task_id)
            try:
                if intent.workspace_identity is None:
                    try:
                        fd = open_dir(self.root_fds['tasks_dir'], intent.relative_name)
                    except FileNotFoundError:
                        pass
                    else:
                        os.close(fd)
                        raise s.RemoteError('task_quarantined')
                else:
                    remove_preparation(task, intent)
                phase = 'retired'
            except (OSError, s.RemoteError):
                phase = 'quarantined'
            with self.broker.store.transaction() as tx:
                store.preparation_transition(tx, intent.preparation_id, intent.phase, phase)
                if phase == 'quarantined' and task.state != 'quarantined':
                    store.transition(tx, task.owner_id, task.task_id, task.version, 'quarantined')
        for task in store.rows(self.broker.store.db, 'remote_tasks', "state='creating'"):
            with self.broker.store.transaction() as tx:
                store.transition(tx, task.owner_id, task.task_id, task.version, 'quarantined')
        for artifact in store.rows(self.broker.store.db, 'remote_artifacts', "state IN ('reserved','uploading')"):
            # A crashed descriptor has no durable stage-inode claim until seal.
            # Preserve its rows/promises and quarantine rather than infer ownership.
            stage = self.root_fds['staging_dir']
            try:
                directory = open_dir(stage, artifact.task_id)
            except FileNotFoundError:
                continue
            try:
                try:
                    os.stat(artifact.artifact_id, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                with self.broker.store.transaction() as tx:
                    store.save(tx, 'remote_artifacts', store.changed(artifact, state='quarantined'))
                    task = self.task(artifact.task_id)
                    if task.state not in ('quarantined', 'deleted'):
                        store.transition(tx, task.owner_id, task.task_id, task.version, 'quarantined')
            finally:
                os.close(directory)
        for binding in store.rows(self.broker.store.db, 'remote_job_bindings'):
            record = self.broker.store.get(binding.job_id)
            self.classify(record)
            task = self.task(binding.seed.task_id)
            publication = store.rows(self.broker.store.db, 'remote_publications', 'job_id=?', (binding.job_id,))
            if len(publication) != 1 or publication[0].task_id != task.task_id:
                raise s.RemoteError('storage_error')
            if publication[0].state == 'pending':
                contained = record['state'] in ('succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted')
                # Recovery never rediscovers mutable outputs or relaunches a job.
                with self.broker.store.transaction() as tx:
                    store.save(tx, 'remote_publications', store.changed(publication[0],
                        state='failed' if contained else 'blocked', error=s.RemoteError('publication_failed').payload()['error']))
                    if task.state == 'busy':
                        updated = store.transition(tx, task.owner_id, task.task_id, task.version, 'ready' if contained else 'quarantined')
                        if contained:
                            store.save(tx, 'remote_tasks', store.changed(updated, active_job_id=None))
                            tx.execute('UPDATE remote_job_bindings SET active=0 WHERE job_id=?', (binding.job_id,))
        for task in store.rows(self.broker.store.db, 'remote_tasks', "state='deleting'"):
            operation = store.rows(self.broker.store.db, 'remote_operations', 'owner_id=? AND task_id=? AND operation=?',
                                   (task.owner_id, task.task_id, 'task_delete'))
            if not operation:
                raise s.RemoteError('storage_error')
            result = reconcile(operation[0])
            with self.broker.store.transaction() as tx:
                if result.state == 'deleted':
                    store.transition(tx, task.owner_id, task.task_id, task.version, 'deleted')
