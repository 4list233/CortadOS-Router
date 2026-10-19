# PURPOSE: Own immutable artifact extents and ordered output snapshot publication.
# CONTRACT: RT-COWORK-MCP-008 v4 R053,055,058–063.
# DEPENDS: Descriptor-relative workspace traversal, task_store, runtime capacity.
# CEILING: No mutable-workspace downloads, content deduplication or native-log export.
import ctypes
import hashlib
import os
import stat
import sys

from . import remote_schemas as s
from . import task_store as store
from .task_workspace import open_dir


def _runtime():
    from .remote_runtime import current
    return current()


def _task_dir(root, task_id, create=False):
    return open_dir(_runtime().root_fds[root], task_id, create=create)


def _check(fd, *, blob=None):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise s.RemoteError('artifact_corrupt')
    if blob is not None and (info.st_dev, info.st_ino, info.st_size) != (blob.device, blob.inode, blob.bytes):
        raise s.RemoteError('artifact_corrupt')
    return info


def _digest(fd, chunk_bytes):
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, chunk_bytes, offset):
        offset += len(chunk)
        digest.update(chunk)
    return offset, digest.hexdigest()


def open_stage(intent):
    fd = _task_dir('staging_dir', intent.task_id, True)
    try:
        result = os.open(intent.artifact_id, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        os.fsync(fd)
        return result
    finally:
        os.close(fd)


def _rename_exclusive(source_dir, source, target_dir, target):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        # Darwin RENAME_EXCL refuses an existing destination atomically.
        result = libc.renameatx_np(source_dir, os.fsencode(source), target_dir, os.fsencode(target), 4)
    else:
        # Linux RENAME_NOREPLACE is the equivalent same-filesystem primitive.
        result = libc.renameat2(source_dir, os.fsencode(source), target_dir, os.fsencode(target), 1)
    if result != 0:
        raise OSError(ctypes.get_errno(), 'Exclusive artifact rename refused')


def seal(fd, intent):
    runtime = _runtime()
    info = _check(fd)
    count, digest = _digest(fd, runtime.config.limits.chunk_bytes)
    if count != intent.declared_bytes:
        raise s.RemoteError('length_mismatch')
    if digest != intent.declared_hash:
        raise s.RemoteError('hash_mismatch')
    os.fsync(fd)
    blob = s.BlobIdentity(device=info.st_dev, inode=info.st_ino,
        relative_name=intent.task_id + '/' + intent.artifact_id, bytes=count, sha256=digest)
    with runtime.broker.store.transaction() as tx:
        store.save(tx, 'remote_artifacts', store.changed(intent, blob=s.plain(blob)))
    source = _task_dir('staging_dir', intent.task_id)
    target = _task_dir('blobs_dir', intent.task_id, True)
    try:
        current = os.stat(intent.artifact_id, dir_fd=source, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise s.RemoteError('artifact_corrupt')
        _rename_exclusive(source, intent.artifact_id, target, intent.artifact_id)
        os.fsync(source)
        os.fsync(target)
    finally:
        os.close(source)
        os.close(target)
    return blob


def open_blob(row):
    runtime = _runtime()
    fd = None
    try:
        if row.state != 'sealed' or row.blob is None:
            raise s.RemoteError('artifact_not_found')
        directory = _task_dir('blobs_dir', row.task_id)
        try:
            fd = os.open(row.artifact_id, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        _check(fd, blob=row.blob)
        count, digest = _digest(fd, runtime.config.limits.chunk_bytes)
        if (count, digest) != (row.declared_bytes, row.declared_hash):
            raise s.RemoteError('artifact_corrupt')
        return fd
    except (OSError, s.RemoteError) as exc:
        if fd is not None:
            os.close(fd)
        with runtime.broker.store.transaction() as tx:
            store.save(tx, 'remote_artifacts', store.changed(row, state='quarantined'))
            task = store.get_owned(tx, row.owner_id, row.task_id)
            if task.state not in ('quarantined', 'deleted'):
                store.transition(tx, task.owner_id, task.task_id, task.version, 'quarantined')
        raise s.RemoteError('artifact_corrupt') from exc


def snapshot(binding):
    runtime = _runtime()
    seed = binding.seed
    task = store.get_owned(runtime.broker.store.db, seed.owner_id, seed.task_id)
    job = runtime.broker.store.get(binding.job_id)
    directory = open_dir(runtime.root_fds['tasks_dir'], binding.workspace_identity.relative_name + '/outputs')
    intents = []
    try:
        names = sorted(os.listdir(directory), key=lambda name: name.encode('utf-8'))
        total = 0
        metadata = []
        for name in names:
            s.filename(name)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise s.RemoteError('publication_failed')
            if info.st_size > task.policy.limits.file_bytes:
                raise s.RemoteError('output_limit')
            total += info.st_size
            metadata.append((name, info))
        existing = store.rows(runtime.broker.store.db, 'remote_artifacts', 'owner_id=? AND task_id=?', (task.owner_id, task.task_id))
        if len(existing) + len(names) > task.policy.limits.artifacts_per_task:
            raise s.RemoteError('capacity_exhausted')
        runtime.capacity.check(task.owner_id, task.task_id, total)
        with runtime.broker.store.transaction() as tx:
            reservation = runtime.capacity.reserve(tx, task.owner_id, task.task_id, 'delegate', seed.request_id, 'output_snapshot', total)
        for name, info in metadata:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            target = None
            try:
                opened = _check(source)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    raise s.RemoteError('publication_failed')
                count, digest = _digest(source, runtime.config.limits.chunk_bytes)
                row = s.ArtifactRow(owner_id=task.owner_id, task_id=task.task_id, artifact_id=store.uid(),
                    filename=name, media_type='application/octet-stream', declared_bytes=count, declared_hash=digest,
                    observed_bytes=None, observed_hash=None, origin='output', job_id=binding.job_id, state='reserved',
                    blob=None, partial=job['state'] != 'succeeded', created_at_ms=store.now())
                with runtime.broker.store.transaction() as tx:
                    store.save(tx, 'remote_artifacts', row, insert=True)
                target = open_stage(row)
                offset = 0
                while chunk := os.pread(source, runtime.config.limits.chunk_bytes, offset):
                    offset += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(target, view):]
                blob = seal(target, row)
                row = store.changed(row, observed_bytes=count, observed_hash=digest, blob=s.plain(blob), state='sealed')
                intents.append((row, s.OutputEntry(artifact=s.artifact_projection(row), blob=blob, phase='sealed')))
            finally:
                for fd in (source, target):
                    if fd is not None:
                        os.close(fd)
        with runtime.broker.store.transaction() as tx:
            for row, entry in intents:
                store.save(tx, 'remote_artifacts', row)
            store.save(tx, 'remote_reservations', store.changed(reservation, state='materialized', materialized_bytes=total))
        return tuple(entry for row, entry in intents)
    finally:
        os.close(directory)
