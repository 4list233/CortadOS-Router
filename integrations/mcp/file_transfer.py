# PURPOSE: Own open broker descriptors and exact bounded lease byte operations.
# CONTRACT: RT-COWORK-MCP-008 v5 R045,047,051–057.
# DEPENDS: artifact_store, transfer_tickets and JobStore transactions.
# CEILING: No framing, listener, text conversion or descriptor transfer to frontend.
import os
import time
import asyncio

from . import remote_schemas as s
from . import task_store as store
from . import artifact_store as artifacts
from . import transfer_tickets as tickets


def _runtime():
    from .remote_runtime import current
    return current()


def _ticket(lease):
    return store.rows(_runtime().broker.store.db, 'remote_tickets', 'ticket_id=?', (lease.ticket_id,))[0]


def _reservations(tx, lease):
    candidates = store.rows(tx, 'remote_reservations', 'owner_id=? AND task_id=? AND kind=? AND state=?',
                            (lease.owner_id, lease.task_id, 'upload', 'held'))
    result = []
    for reservation in candidates:
        operation = store.rows(tx, 'remote_operations', 'owner_id=? AND operation=? AND request_id=?',
                               (lease.owner_id, reservation.operation, reservation.request_id))[0]
        if lease.artifact_id in operation.object_ids.artifact_ids:
            result.append(reservation)
    return result


def open_lease(lease):
    runtime = _runtime()
    row = tickets.artifact(runtime.broker.store.db, lease.owner_id, lease.task_id, lease.artifact_id)
    fd = artifacts.open_stage(row) if lease.direction == 'put' else artifacts.open_blob(row)
    if lease.direction == 'put':
        info = os.fstat(fd)
        identity = s.BlobIdentity(device=info.st_dev, inode=info.st_ino,
            relative_name=row.task_id + '/' + row.artifact_id, bytes=row.declared_bytes, sha256=row.declared_hash)
        try:
            with runtime.broker.store.transaction() as tx:
                store.save(tx, 'remote_artifacts', store.changed(row, state='uploading', blob=s.plain(identity)))
                store.event(tx, 'lease', lease.lease_id, state='open')
        except BaseException:
            os.close(fd)
            raise
    runtime.descriptors[lease.lease_id] = fd
    runtime.leases[lease.lease_id] = lease
    if lease.direction == 'get':
        with runtime.broker.store.transaction() as tx:
            store.event(tx, 'lease', lease.lease_id, state='open')
    runtime.eof_leases.discard(lease.lease_id)
    runtime.lease_timers[lease.lease_id] = asyncio.create_task(_expire(lease.lease_id))
    return row


async def _expire(lease_id):
    runtime = _runtime()
    while lease_id in runtime.leases:
        lease = runtime.leases[lease_id]
        delay = (min(lease.deadline_ms, lease.idle_deadline_ms) - int(time.monotonic() * 1000)) / 1000
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            close(lease, 'timeout')
            return


def _check(lease, offset):
    runtime = _runtime()
    if lease.lease_id not in runtime.leases:
        raise s.RemoteError('ticket_invalid')
    try:
        runtime.authorize(runtime.config.principal, 'put_chunk' if lease.direction == 'put' else 'read_chunk')
    except s.RemoteError:
        close(lease, 'abort')
        raise
    lease = runtime.leases[lease.lease_id]
    now = int(time.monotonic() * 1000)
    if now >= min(lease.deadline_ms, lease.idle_deadline_ms):
        close(lease, 'timeout')
        raise s.RemoteError('timeout')
    if offset != lease.offset or lease.lease_id in runtime.eof_leases:
        close(lease, 'abort')
        raise s.RemoteError('length_mismatch')
    return lease


def _advance(lease, offset):
    runtime = _runtime()
    task = store.get_owned(runtime.broker.store.db, lease.owner_id, lease.task_id)
    runtime.leases[lease.lease_id] = store.changed(lease, offset=offset,
        idle_deadline_ms=int(time.monotonic() * 1000) + task.policy.limits.transfer_idle_ms)


def put_chunk(lease, offset, body):
    runtime = _runtime()
    lease = _check(lease, offset)
    ticket = _ticket(lease)
    if lease.direction != 'put' or not body or len(body) > runtime.config.limits.chunk_bytes:
        close(lease, 'abort')
        raise s.RemoteError('invalid_request')
    if offset + len(body) > ticket.bytes:
        close(lease, 'abort')
        raise s.RemoteError('length_mismatch')
    fd = runtime.descriptors[lease.lease_id]
    row = tickets.artifact(runtime.broker.store.db, lease.owner_id, lease.task_id, lease.artifact_id)
    info = os.fstat(fd)
    from .task_workspace import open_dir
    directory = open_dir(runtime.root_fds['staging_dir'], lease.task_id)
    try:
        named = os.stat(lease.artifact_id, dir_fd=directory, follow_symlinks=False)
        if (row.blob is None or (info.st_dev, info.st_ino) != (row.blob.device, row.blob.inode)
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)):
            raise s.RemoteError('artifact_corrupt')
    finally:
        os.close(directory)
    view = memoryview(body)
    while view:
        view = view[os.write(fd, view):]
    os.fsync(fd)
    _advance(lease, offset + len(body))
    with runtime.broker.store.transaction() as tx:
        for row in _reservations(tx, lease):
            store.save(tx, 'remote_reservations', store.changed(row, materialized_bytes=offset + len(body)))
        row = tickets.artifact(tx, lease.owner_id, lease.task_id, lease.artifact_id)
        if row.state != 'uploading':
            store.save(tx, 'remote_artifacts', store.changed(row, state='uploading'))
    return offset + len(body)


def finish_put(lease):
    runtime = _runtime()
    lease = _check(lease, runtime.leases[lease.lease_id].offset)
    ticket = _ticket(lease)
    row = tickets.artifact(runtime.broker.store.db, lease.owner_id, lease.task_id, lease.artifact_id)
    try:
        if lease.offset != ticket.bytes:
            raise s.RemoteError('length_mismatch')
        blob = artifacts.seal(runtime.descriptors[lease.lease_id], row)
        row = store.changed(row, state='sealed', blob=s.plain(blob), observed_bytes=blob.bytes, observed_hash=blob.sha256)
        with runtime.broker.store.transaction() as tx:
            store.save(tx, 'remote_artifacts', row)
            store.save(tx, 'remote_tickets', store.changed(ticket, state='consumed', lease_id=None, connection_id=None))
            for reservation in _reservations(tx, lease):
                store.save(tx, 'remote_reservations', store.changed(reservation, state='materialized', materialized_bytes=reservation.bytes))
        close(lease, 'complete')
        return s.project_remote_result(dict(kind='receipt', task_id=lease.task_id, artifact=row))
    except (s.RemoteError, OSError):
        close(lease, 'abort')
        raise


def read_chunk(lease, offset, max_bytes):
    runtime = _runtime()
    lease = _check(lease, offset)
    if lease.direction != 'get' or type(max_bytes) is not int or not 0 < max_bytes <= runtime.config.limits.chunk_bytes:
        close(lease, 'abort')
        raise s.RemoteError('invalid_request')
    ticket = _ticket(lease)
    body = os.pread(runtime.descriptors[lease.lease_id], min(max_bytes, ticket.bytes - offset), offset)
    next_offset = offset + len(body)
    if not body and next_offset != ticket.bytes:
        close(lease, 'abort')
        raise s.RemoteError('artifact_corrupt')
    if body:
        _advance(lease, next_offset)
    if next_offset == ticket.bytes:
        runtime.eof_leases.add(lease.lease_id)
    return s.ReadResult(body=body, offset=next_offset, eof=next_offset == ticket.bytes)


def close(lease, reason):
    runtime = _runtime()
    timer = runtime.lease_timers.pop(lease.lease_id, None)
    if timer is not None and timer is not asyncio.current_task():
        timer.cancel()
    if lease.lease_id not in runtime.leases:
        return
    fd = runtime.descriptors.pop(lease.lease_id, None)
    try:
        try:
            identity = os.fstat(fd) if fd is not None else None
        finally:
            if fd is not None:
                os.close(fd)
        ticket = _ticket(lease)
        if lease.direction == 'put' and ticket.state != 'consumed' and identity is not None:
            from .task_workspace import open_dir
            directory = open_dir(runtime.root_fds['staging_dir'], lease.task_id)
            try:
                observed = os.stat(lease.artifact_id, dir_fd=directory, follow_symlinks=False)
                if (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino):
                    raise s.RemoteError('task_quarantined')
                os.unlink(lease.artifact_id, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
            with runtime.broker.store.transaction() as tx:
                for reservation in _reservations(tx, lease):
                    store.save(tx, 'remote_reservations', store.changed(reservation, materialized_bytes=0))
        if ticket.state == 'claimed':
            next_state = 'issued' if lease.direction == 'get' and reason == 'complete' else 'revoked'
            with runtime.broker.store.transaction() as tx:
                store.save(tx, 'remote_tickets', store.changed(ticket, state=next_state, lease_id=None, connection_id=None))
        runtime.leases.pop(lease.lease_id, None)
        runtime.eof_leases.discard(lease.lease_id)
        with runtime.broker.store.transaction() as tx:
            store.event(tx, 'lease', lease.lease_id, 'open', 'closed')
    except Exception:
        runtime.fatal()
        raise
    finally:
        runtime.leases.pop(lease.lease_id, None)
        runtime.eof_leases.discard(lease.lease_id)
