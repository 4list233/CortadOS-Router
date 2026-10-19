# PURPOSE: Own scoped versioned ticket capabilities and connection-bound leases.
# CONTRACT: RT-COWORK-MCP-008 v4 R048–051,054,056–057.
# DEPENDS: Runtime policy, task_store transactions and file_transfer descriptor owner.
# CEILING: No OAuth, caller-selected identity or independently opened database.
import base64
import hashlib
import hmac
import time

from . import remote_schemas as s
from . import task_store as store


def _runtime():
    from .remote_runtime import current
    return current()


def capability(ticket):
    config = _runtime().config
    if ticket.key_version != config.ticket_key_version:
        raise s.RemoteError('ticket_invalid')
    values = [ticket.key_version, ticket.ticket_id, ticket.owner_id, ticket.task_id, ticket.operation,
              ticket.request_id, ticket.artifact_id, ticket.direction, ticket.bytes, ticket.sha256, ticket.expires_at_ms]
    message = b'cortados.cowork-ticket.v1\x00' + s.json_bytes(values)
    return base64.urlsafe_b64encode(hmac.digest(config.ticket_key, message, 'sha256')).rstrip(b'=').decode('ascii')


def artifact(tx, owner_id, task_id, artifact_id):
    found = store.rows(tx, 'remote_artifacts', 'owner_id=? AND task_id=? AND artifact_id=?', (owner_id, task_id, artifact_id))
    if not found:
        raise s.RemoteError('artifact_not_found')
    return found[0]


def require_publication(tx, row):
    try:
        return _require_publication(tx, row)
    except s.RemoteError as exc:
        if exc.code != 'publication_failed':
            _runtime().fatal()
        raise


def _require_publication(tx, row):
    if row.origin == 'input':
        return
    found = store.rows(tx, 'remote_publications', 'job_id=? AND owner_id=? AND task_id=?',
                       (row.job_id, row.owner_id, row.task_id))
    if len(found) != 1:
        raise s.RemoteError('storage_error')
    if found[0].state != 'complete':
        raise s.RemoteError('publication_failed')
    manifest = found[0].manifest
    names = [entry.artifact.filename for entry in manifest]
    ids = [entry.artifact.artifact_id for entry in manifest]
    if names != sorted(set(names), key=lambda name: name.encode('utf-8')) or len(ids) != len(set(ids)):
        raise s.RemoteError('artifact_corrupt')
    if row.artifact_id not in ids:
        raise s.RemoteError('artifact_corrupt')
    for entry in manifest:
        member = artifact(tx, row.owner_id, row.task_id, entry.artifact.artifact_id)
        if (entry.phase != 'sealed' or member.state != 'sealed' or member.origin != 'output'
                or member.job_id != row.job_id or s.plain(entry.artifact) != s.artifact_projection(member)
                or entry.blob != member.blob):
            raise s.RemoteError('artifact_corrupt')


def issue(tx, principal, args, direction):
    runtime = _runtime()
    owner_id = s.owner(principal)
    task = store.get_owned(tx, owner_id, args['task_id'])
    if task.state == 'deleted':
        raise s.RemoteError('task_deleted')
    if task.state == 'quarantined':
        raise s.RemoteError('task_quarantined')
    if direction == 'put' and task.state != 'ready':
        raise s.RemoteError('workspace_busy')
    operation = 'file_upload_ticket' if direction == 'put' else 'file_download_ticket'
    from .schemas import canonical
    argument_hash = hashlib.sha256(canonical(args)).hexdigest()
    if direction == 'put' and args['artifact_id'] is None:
        artifacts = store.rows(tx, 'remote_artifacts', 'owner_id=? AND task_id=?', (owner_id, task.task_id))
        if args['bytes'] > task.policy.limits.file_bytes:
            raise s.RemoteError('output_limit')
        if len(artifacts) >= task.policy.limits.artifacts_per_task:
            raise s.RemoteError('capacity_exhausted')
        row = s.ArtifactRow(owner_id=owner_id, task_id=task.task_id, artifact_id=store.uid(), filename=args['filename'],
            media_type=args['media_type'], declared_bytes=args['bytes'], declared_hash=args['sha256'], observed_bytes=None,
            observed_hash=None, origin='input', job_id=None, state='reserved', blob=None, partial=False, created_at_ms=store.now())
        store.save(tx, 'remote_artifacts', row, insert=True)
    else:
        row = artifact(tx, owner_id, task.task_id, args['artifact_id'])
        if direction == 'get' and row.state != 'sealed':
            raise s.RemoteError('artifact_corrupt' if row.state == 'quarantined' else 'upload_incomplete')
        if direction == 'get':
            require_publication(tx, row)
        if direction == 'put':
            if row.state not in ('reserved', 'uploading'):
                raise s.RemoteError('ticket_consumed')
            if (row.filename, row.media_type, row.declared_bytes, row.declared_hash) != (args['filename'], args['media_type'], args['bytes'], args['sha256']):
                raise s.RemoteError('idempotency_conflict')
            if any(lease.artifact_id == row.artifact_id for lease in runtime.leases.values()):
                raise s.RemoteError('transfer_busy')
            for old in store.rows(tx, 'remote_tickets', 'owner_id=? AND task_id=? AND artifact_id=?', (owner_id, task.task_id, row.artifact_id)):
                if old.state in ('issued', 'claimed'):
                    store.save(tx, 'remote_tickets', store.changed(old, state='revoked', lease_id=None, connection_id=None))
    ticket = s.TicketRow(ticket_id=store.uid(), owner_id=owner_id, task_id=task.task_id, artifact_id=row.artifact_id,
        operation=operation, request_id=args['request_id'], capability_hash='0' * 64, key_version=runtime.config.ticket_key_version,
        direction=direction, bytes=row.declared_bytes, sha256=row.declared_hash, expires_at_ms=store.now() + task.policy.limits.ticket_ttl_s * 1000,
        state='issued', lease_id=None, connection_id=None)
    token = capability(ticket)
    ticket = store.changed(ticket, capability_hash=hashlib.sha256(token.encode()).hexdigest())
    store.record_operation(tx, owner_id, operation, args['request_id'], argument_hash, task.task_id,
                           artifact_ids=(row.artifact_id,), ticket_id=ticket.ticket_id)
    store.save(tx, 'remote_tickets', ticket, insert=True)
    if direction == 'put' and args['artifact_id'] is None:
        runtime.capacity.reserve(tx, owner_id, task.task_id, operation, args['request_id'], 'upload', row.declared_bytes)
    return ticket


def claim(tx, ticket_id, capability, direction, connection_id):
    runtime = _runtime()
    found = store.rows(tx, 'remote_tickets', 'ticket_id=?', (ticket_id,))
    if not found:
        raise s.RemoteError('ticket_invalid')
    ticket = found[0]
    expected = globals()['capability'](ticket)
    if not isinstance(capability, str) or not hmac.compare_digest(capability, expected) or ticket.direction != direction:
        raise s.RemoteError('ticket_invalid')
    if not runtime.enabled:
        raise s.RemoteError('permission_refused')
    if ticket.state == 'consumed':
        raise s.RemoteError('ticket_consumed')
    if ticket.state == 'revoked':
        raise s.RemoteError('ticket_invalid')
    if ticket.expires_at_ms <= store.now():
        raise s.RemoteError('ticket_expired')
    if ticket.state == 'claimed':
        raise s.RemoteError('transfer_busy')
    task = store.get_owned(tx, ticket.owner_id, ticket.task_id)
    if task.state in ('deleted', 'deleting', 'quarantined'):
        raise s.RemoteError('task_deleted' if task.state != 'quarantined' else 'task_quarantined')
    if direction == 'get':
        require_publication(tx, artifact(tx, ticket.owner_id, ticket.task_id, ticket.artifact_id))
    leases = tuple(runtime.leases.values())
    limit = task.policy.limits.uploads_per_task if direction == 'put' else task.policy.limits.downloads_per_task
    if (len(leases) >= runtime.config.limits.streams_per_installation
            or sum(lease.task_id == task.task_id and lease.direction == direction for lease in leases) >= limit):
        raise s.RemoteError('transfer_busy')
    now = int(time.monotonic() * 1000)
    lease = s.Lease(lease_id=store.uid(), connection_id=connection_id, owner_id=ticket.owner_id, task_id=ticket.task_id,
        artifact_id=ticket.artifact_id, ticket_id=ticket.ticket_id, direction=direction, offset=0,
        deadline_ms=now + task.policy.limits.transfer_total_ms, idle_deadline_ms=now + task.policy.limits.transfer_idle_ms, state='open')
    store.save(tx, 'remote_tickets', store.changed(ticket, state='claimed', lease_id=lease.lease_id, connection_id=connection_id))
    return lease


def revoke(tx, owner_id, task_id):
    where = 'owner_id=?' + (' AND task_id=?' if task_id is not None else '')
    parameters = (owner_id, task_id) if task_id is not None else (owner_id,)
    tickets = store.rows(tx, 'remote_tickets', where, parameters)
    for ticket in tickets:
        if ticket.state in ('issued', 'claimed'):
            store.save(tx, 'remote_tickets', store.changed(ticket, state='revoked'))
    return tuple(ticket.ticket_id for ticket in tickets)
