# PURPOSE: Own descriptor-relative task directories and immutable selected-input copies.
# CONTRACT: RT-COWORK-MCP-008 v4 R027,058,069,081–084.
# DEPENDS: Runtime-owned roots, artifact_store and preparation journal.
# CEILING: No job acceptance, native invocation, unowned removal or symlink traversal.
import os
import stat
import hashlib

from . import remote_schemas as s


def open_dir(root_fd, relative, *, create=False):
    parts = relative.split('/')
    if any(not part or part in ('.', '..') for part in parts):
        raise s.RemoteError('storage_error')
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if os.fstat(child).st_dev != os.fstat(root_fd).st_dev:
                os.close(child)
                raise s.RemoteError('storage_error')
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _root():
    from .remote_runtime import current
    return current().root_fds['tasks_dir']


def identity(fd, relative):
    info = os.fstat(fd)
    return s.WorkspaceIdentity(device=info.st_dev, inode=info.st_ino, relative_name=relative)


def create_task(task):
    root = _root()
    os.mkdir(task.task_id, mode=0o700, dir_fd=root)
    fd = open_dir(root, task.task_id)
    try:
        os.mkdir('runs', mode=0o700, dir_fd=fd)
        os.fsync(fd)
        os.fsync(root)
        return identity(fd, task.task_id)
    finally:
        os.close(fd)


def prepare_empty(task, intent):
    root = _root()
    parent = open_dir(root, task.task_id + '/runs')
    fd = None
    try:
        task_fd = open_dir(root, task.task_id)
        try:
            if identity(task_fd, task.task_id) != task.root_identity:
                raise s.RemoteError('task_quarantined')
        finally:
            os.close(task_fd)
        os.mkdir(intent.request_id, mode=0o700, dir_fd=parent)
        fd = open_dir(parent, intent.request_id)
        os.mkdir('outputs', mode=0o700, dir_fd=fd)
        os.fsync(fd)
        os.fsync(parent)
        return identity(fd, intent.relative_name)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def remove_preparation(task, intent):
    root = _root()
    try:
        fd = open_dir(root, intent.relative_name)
    except FileNotFoundError:
        return
    try:
        if intent.workspace_identity is None or identity(fd, intent.relative_name) != intent.workspace_identity:
            raise s.RemoteError('task_quarantined')
        if os.listdir(fd) != ['outputs']:
            raise s.RemoteError('task_quarantined')
        output = open_dir(fd, 'outputs')
        try:
            if os.listdir(output):
                raise s.RemoteError('task_quarantined')
        finally:
            os.close(output)
        os.rmdir('outputs', dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = open_dir(root, task.task_id + '/runs')
    try:
        os.rmdir(intent.request_id, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def prepare(task, binding):
    from .artifact_store import open_blob
    fd = open_dir(_root(), binding.workspace_identity.relative_name)
    try:
        if identity(fd, binding.workspace_identity.relative_name) != binding.workspace_identity:
            raise s.RemoteError('policy_unavailable')
        if binding.seed.input_manifest:
            os.mkdir('inputs', mode=0o700, dir_fd=fd)
        for entry in binding.seed.input_manifest:
            directory = open_dir(fd, 'inputs/' + entry.artifact.artifact_id, create=True)
            source = None
            target = None
            try:
                from .remote_runtime import current
                from .task_store import rows
                runtime = current()
                row = rows(runtime.broker.store.db, 'remote_artifacts', 'owner_id=? AND task_id=? AND artifact_id=?',
                           (task.owner_id, task.task_id, entry.artifact.artifact_id))[0]
                source = open_blob(row)
                target = os.open(entry.artifact.filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                digest = hashlib.sha256()
                count = 0
                while chunk := os.read(source, task.policy.limits.chunk_bytes):
                    digest.update(chunk)
                    count += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(target, view):]
                if count != entry.artifact.bytes or digest.hexdigest() != entry.artifact.sha256:
                    raise s.RemoteError('artifact_corrupt')
                os.fsync(target)
                os.fsync(directory)
            finally:
                for handle in (source, target, directory):
                    if handle is not None:
                        os.close(handle)
        os.fsync(fd)
        return binding.workspace_identity
    finally:
        os.close(fd)


def _remove_tree(fd):
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = open_dir(fd, name)
            try:
                _remove_tree(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            os.unlink(name, dir_fd=fd)
        else:
            raise s.RemoteError('task_quarantined')
    os.fsync(fd)


def remove(task):
    from .remote_runtime import current
    runtime = current()
    try:
        for key in ('tasks_dir', 'blobs_dir', 'staging_dir'):
            root = runtime.root_fds[key]
            try:
                fd = open_dir(root, task.task_id)
            except FileNotFoundError:
                continue
            try:
                if key == 'tasks_dir' and identity(fd, task.task_id) != task.root_identity:
                    raise s.RemoteError('task_quarantined')
                _remove_tree(fd)
            finally:
                os.close(fd)
            os.rmdir(task.task_id, dir_fd=root)
            os.fsync(root)
        return s.DeleteProgress(state='deleted', cleanup_error=None)
    except (OSError, s.RemoteError):
        return s.DeleteProgress(state='deleting', cleanup_error=s.RemoteError('storage_error').payload()['error'])


def reconcile(intent):
    from .remote_runtime import current
    from .task_store import get_owned
    task = get_owned(current().broker.store.db, intent.owner_id, intent.object_ids.task_id)
    if task.state == 'deleting':
        progress = remove(task)
        return s.RecoveryResult(state=progress.state, error=progress.cleanup_error)
    return s.RecoveryResult(state='quarantined', error=s.RemoteError('storage_error').payload()['error'])
