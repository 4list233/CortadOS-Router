# PURPOSE: Own remote SQL and atomic seed adoption in the existing JobStore.
# CONTRACT: RT-COWORK-MCP-008 v4 sections 2–4; JobStore is sole transaction owner.
# DEPENDS: remote_schemas records; existing jobs and job_feedback tables.
# CEILING: No connection, BEGIN/COMMIT, filesystem preparation or job-ID allocation.
import hashlib
import json
import os
import sqlite3
import time
import uuid

from . import remote_schemas as s

TABLES = {
    'remote_tasks': ('TaskRow', ('owner_id', 'task_id')),
    'remote_operations': ('OperationRow', ('owner_id', 'operation', 'request_id')),
    'remote_preparations': ('PreparationRow', ('preparation_id',)),
    'remote_job_bindings': ('RemoteBinding', ('job_id',)),
    'remote_artifacts': ('ArtifactRow', ('owner_id', 'task_id', 'artifact_id')),
    'remote_tickets': ('TicketRow', ('ticket_id',)),
    'remote_publications': ('PublicationRow', ('job_id',)),
    'remote_reservations': ('ReservationRow', ('reservation_id',)),
}
ENTITIES = {'remote_tasks': 'task', 'remote_artifacts': 'artifact', 'remote_tickets': 'ticket',
            'remote_publications': 'publication'}


def uid():
    return str(uuid.uuid4())


def now():
    return time.time_ns() // 1000000


def _fields(table):
    return s.PRIVATE[TABLES[table][0]]['properties']


def _nested(field):
    if '$ref' in field:
        return s.PRIVATE[field['$ref'].rsplit('/', 1)[-1]].get('type') in ('object', 'array')
    if 'anyOf' in field:
        return _nested(field['anyOf'][0])
    return field.get('type') in ('object', 'array')


def _column(name, field):
    return name + '_json' if _nested(field) else name


def _scalar_constraints(column, field):
    if '$ref' in field:
        field = s.PRIVATE[field['$ref'].rsplit('/', 1)[-1]]
    if 'anyOf' in field:
        return _scalar_constraints(column, field['anyOf'][0])
    tests = []
    def literal(value):
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return str(int(value))
    if 'enum' in field:
        tests.append(column + ' IN (' + ','.join(literal(v) for v in field['enum']) + ')')
    if 'const' in field and field['const'] is not None:
        tests.append(column + '=' + literal(field['const']))
    if field.get('type') in ('integer', 'boolean'):
        tests.append("typeof(" + column + ")='integer'")
        if field['type'] == 'boolean':
            tests.append(column + ' IN (0,1)')
        for key, operator in (('minimum', '>='), ('maximum', '<=')):
            if key in field:
                tests.append(column + operator + str(field[key]))
    return ''.join(' CHECK(' + column + ' IS NULL OR (' + test + '))' for test in tests)


def migrate(db):
    existed = db.execute("SELECT 1 FROM sqlite_master WHERE name='remote_schema'").fetchone()
    if existed and db.execute('SELECT singleton,version FROM remote_schema').fetchall() != [(1, 1)]:
        raise s.RemoteError('storage_error')
    try:
        if not existed:
            db.execute('CREATE TABLE remote_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version=1))')
            db.execute('INSERT INTO remote_schema VALUES(1,1)')
        for table, (record, keys) in TABLES.items():
            fields = _fields(table)
            columns = []
            for name, field in fields.items():
                column = _column(name, field)
                target = s.PRIVATE.get(field.get('$ref', '').rsplit('/', 1)[-1], field)
                if 'anyOf' in target:
                    scalar = target['anyOf'][0]
                    target = s.PRIVATE.get(scalar.get('$ref', '').rsplit('/', 1)[-1], scalar)
                numeric = target.get('type') in ('integer', 'boolean')
                nullable = 'anyOf' in field and any(f.get('type') == 'null' for f in field['anyOf'])
                columns.append(column + (' INTEGER' if numeric else ' TEXT') + ('' if nullable else ' NOT NULL')
                               + ('' if _nested(field) else _scalar_constraints(column, field)))
            extras = []
            if table == 'remote_job_bindings':
                columns += ['owner_id TEXT NOT NULL', 'task_id TEXT NOT NULL', 'active INTEGER NOT NULL CHECK(active IN (0,1))']
            if table == 'remote_operations':
                columns += ['task_id TEXT NOT NULL']
            extras.append('PRIMARY KEY(' + ','.join(keys) + ')')
            if table != 'remote_tasks':
                extras.append('FOREIGN KEY(owner_id,task_id) REFERENCES remote_tasks(owner_id,task_id)')
            if table in ('remote_job_bindings', 'remote_publications'):
                extras.append('FOREIGN KEY(job_id) REFERENCES jobs(job_id)')
            if table == 'remote_preparations':
                extras.append('FOREIGN KEY(job_id) REFERENCES jobs(job_id)')
            if table in ('remote_tickets', 'remote_reservations'):
                extras.append('FOREIGN KEY(owner_id,operation,request_id) REFERENCES remote_operations(owner_id,operation,request_id)')
            if table == 'remote_tickets':
                extras.append('FOREIGN KEY(owner_id,task_id,artifact_id) REFERENCES remote_artifacts(owner_id,task_id,artifact_id)')
            sql = 'CREATE TABLE ' + table + '(' + ','.join(columns + extras) + ')'
            actual = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
            if existed:
                if not actual or actual[0] != sql:
                    raise s.RemoteError('storage_error')
            else:
                db.execute(sql)
        indexes = [
            'CREATE INDEX remote_tasks_owner_state ON remote_tasks(owner_id,state)',
            'CREATE INDEX remote_operations_task ON remote_operations(owner_id,task_id)',
            'CREATE UNIQUE INDEX remote_preparations_active ON remote_preparations(owner_id,task_id) WHERE phase IN (\'intent\',\'prepared\',\'quarantined\')',
            'CREATE INDEX remote_preparations_request ON remote_preparations(owner_id,request_id)',
            'CREATE UNIQUE INDEX remote_bindings_active ON remote_job_bindings(owner_id,task_id) WHERE active=1',
            'CREATE INDEX remote_tickets_task ON remote_tickets(owner_id,task_id,state)',
            'CREATE INDEX remote_reservations_task ON remote_reservations(owner_id,task_id,state)',
            'CREATE TABLE remote_audit(event_id TEXT PRIMARY KEY,event_json TEXT NOT NULL)',
            "CREATE TRIGGER remote_audit_no_update BEFORE UPDATE ON remote_audit BEGIN SELECT RAISE(ABORT,'audit is append-only'); END",
            "CREATE TRIGGER remote_audit_no_delete BEFORE DELETE ON remote_audit BEGIN SELECT RAISE(ABORT,'audit is append-only'); END"]
        for sql in indexes:
            if not existed:
                db.execute(sql)
            else:
                words = sql.split()
                marker = next(kind for kind in ('INDEX', 'TABLE', 'TRIGGER') if kind in words)
                name = words[words.index(marker) + 1].split('(', 1)[0]
                actual = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                if not actual or actual[0] != sql:
                    raise s.RemoteError('storage_error')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise s.RemoteError('storage_error')
    except sqlite3.Error as exc:
        raise s.RemoteError('storage_error') from exc


def rows(tx, table, where='', parameters=(), order=''):
    fields = _fields(table)
    columns = [_column(name, field) for name, field in fields.items()]
    extras = ['owner_id', 'task_id', 'active'] if table == 'remote_job_bindings' else ['task_id'] if table == 'remote_operations' else []
    sql = 'SELECT ' + ','.join(columns + extras) + ' FROM ' + table
    if where:
        sql += ' WHERE ' + where
    if order:
        sql += ' ORDER BY ' + order
    result = []
    try:
        for raw in tx.execute(sql, parameters):
            value = {name: s.strict_json(item.encode('utf-8')) if _nested(field) and item is not None else item
                     for (name, field), item in zip(fields.items(), raw)}
            for name, field in fields.items():
                if field.get('type') == 'boolean':
                    if type(value[name]) is not int or value[name] not in (0, 1):
                        raise s.RemoteError('storage_error')
                    value[name] = bool(value[name])
            record = getattr(s, TABLES[table][0])(**value)
            value = s.plain(record)
            if table == 'remote_job_bindings':
                if type(raw[-1]) is not int or raw[-1] not in (0, 1):
                    raise s.RemoteError('storage_error')
                if tuple(raw[len(columns):2 + len(columns)]) != (value['seed']['owner_id'], value['seed']['task_id']):
                    raise s.RemoteError('policy_unavailable')
            if table == 'remote_operations' and raw[-1] != value['object_ids']['task_id']:
                raise s.RemoteError('policy_unavailable')
            if table == 'remote_preparations':
                relative = value['task_id'] + '/runs/' + value['request_id']
                identity = value['workspace_identity']
                if (value['relative_name'] != relative
                        or (identity is not None and identity['relative_name'] != relative)
                        or ((value['phase'] == 'adopted') != (value['job_id'] is not None))
                        or (value['phase'] in ('prepared', 'adopted') and identity is None)):
                    raise s.RemoteError('policy_unavailable')
            if table == 'remote_tasks' and value['owner_id'] != s.owner(s.Principal(**value['policy']['principal'])):
                raise s.RemoteError('policy_unavailable')
            result.append(record)
        return result
    except Exception as exc:
        from .remote_runtime import current
        current().fatal()
        if isinstance(exc, s.RemoteError) and exc.code == 'policy_unavailable':
            raise
        raise s.RemoteError('storage_error') from exc


def save(tx, table, record, *, insert=False):
    if not tx.in_transaction:
        raise s.RemoteError('storage_error')
    fields = _fields(table)
    value = s.plain(record)
    getattr(s, TABLES[table][0])(**value)
    encoded = {_column(name, field): json.dumps(value[name], ensure_ascii=False, separators=(',', ':'), sort_keys=True)
               if _nested(field) and value[name] is not None else value[name] for name, field in fields.items()}
    if table == 'remote_operations':
        encoded['task_id'] = value['object_ids']['task_id']
    if table == 'remote_job_bindings':
        encoded.update(owner_id=value['seed']['owner_id'], task_id=value['seed']['task_id'], active=1)
    keys = TABLES[table][1]
    previous = [] if insert else rows(tx, table, ' AND '.join(k + '=?' for k in keys), tuple(value[k] for k in keys))
    if insert:
        tx.execute('INSERT INTO ' + table + '(' + ','.join(encoded) + ') VALUES(' + ','.join('?' for _ in encoded) + ')', tuple(encoded.values()))
    else:
        tx.execute('UPDATE ' + table + ' SET ' + ','.join(k + '=?' for k in encoded) + ' WHERE ' + ' AND '.join(k + '=?' for k in keys), (*encoded.values(), *(value[k] for k in keys)))
    if table in ENTITIES:
        old = previous[0].state if previous else None
        if old != record.state:
            entity = ENTITIES[table]
            object_id = value.get(entity + '_id', value.get('job_id'))
            event(tx, entity, object_id, old, record.state)
    return record


def event(tx, entity, object_id, old=None, state=None, reason=None, request_id=None):
    try:
        s.emit_remote_event(tx, s.RemoteEvent(schema='cortados.cowork-event.v1', event_id=uid(), at_ms=now(),
            event='transition' if state is not None else 'refusal', entity=entity, object_id=object_id,
            request_id=request_id, from_state=old, state=state, reason=reason))
    except Exception:
        if tx is not None:
            from .remote_runtime import current
            current().fatal()
        raise


def changed(record, **changes):
    return type(record)(**{**s.plain(record), **changes})


def get_owned(tx, owner_id, task_id):
    found = rows(tx, 'remote_tasks', 'owner_id=? AND task_id=?', (owner_id, task_id))
    if not found:
        raise s.RemoteError('task_not_found')
    return found[0]


def operation(tx, owner_id, operation, request_id, argument_hash):
    found = rows(tx, 'remote_operations', 'owner_id=? AND operation=? AND request_id=?', (owner_id, operation, request_id))
    if found and found[0].argument_hash != argument_hash:
        raise s.RemoteError('idempotency_conflict')
    return found[0] if found else None


def record_operation(tx, owner_id, operation, request_id, argument_hash, task_id, *, job_id=None, artifact_ids=(), ticket_id=None):
    return save(tx, 'remote_operations', s.OperationRow(owner_id=owner_id, operation=operation,
        request_id=request_id, argument_hash=argument_hash, phase='committed', error=None, created_at_ms=now(),
        object_ids=dict(task_id=task_id, job_id=job_id, artifact_ids=artifact_ids, ticket_id=ticket_id)), insert=True)


def create_intent(tx, principal, args, snapshot):
    task = s.TaskRow(owner_id=s.owner(principal), task_id=uid(), group_id=args['task_group_id'],
        title=args['title'], state='creating', policy=snapshot, created_at_ms=now(), updated_at_ms=now(),
        version=0, active_job_id=None, root_identity=None, cleanup_error=None)
    save(tx, 'remote_tasks', task, insert=True)
    from .schemas import canonical
    record_operation(tx, task.owner_id, 'task_create', args['request_id'], hashlib.sha256(canonical(args)).hexdigest(), task.task_id)
    return task


def transition(tx, owner_id, task_id, expected_version, next_state):
    task = get_owned(tx, owner_id, task_id)
    if task.version != expected_version:
        raise s.RemoteError('workspace_busy')
    allowed = {'creating': ('ready', 'deleting', 'quarantined'), 'ready': ('busy', 'deleting', 'quarantined'),
               'busy': ('ready', 'quarantined'), 'deleting': ('deleted', 'quarantined')}
    if next_state not in allowed.get(task.state, ()):
        raise s.RemoteError('task_quarantined' if task.state == 'quarantined' else 'workspace_busy')
    return save(tx, 'remote_tasks', changed(task, state=next_state, version=task.version + 1, updated_at_ms=now()))


def preparation_intent(tx, task, request_id, argument_hash):
    row = s.PreparationRow(preparation_id=uid(), owner_id=task.owner_id, task_id=task.task_id,
        request_id=request_id, argument_hash=argument_hash, expected_task_version=task.version,
        relative_name=task.task_id + '/runs/' + request_id, workspace_identity=None, phase='intent', job_id=None)
    return save(tx, 'remote_preparations', row, insert=True)


def preparation_transition(tx, preparation_id, expected_phase, next_phase, *, workspace_identity=None, job_id=None):
    found = rows(tx, 'remote_preparations', 'preparation_id=?', (preparation_id,))
    if not found or found[0].phase != expected_phase:
        raise s.RemoteError('storage_error')
    row = found[0]
    allowed = {'intent': ('prepared', 'retired', 'quarantined'), 'prepared': ('adopted', 'retired', 'quarantined')}
    if next_phase not in allowed.get(expected_phase, ()):
        raise s.RemoteError('storage_error')
    identity = workspace_identity or row.workspace_identity
    if (next_phase in ('prepared', 'adopted') and identity is None) or ((job_id is not None) != (next_phase == 'adopted')):
        raise s.RemoteError('storage_error')
    return save(tx, 'remote_preparations', changed(row, phase=next_phase, workspace_identity=s.plain(identity), job_id=job_id))


def accept_binding(tx, job_id, seed):
    task = get_owned(tx, seed.owner_id, seed.task_id)
    if task.state != 'ready' or task.version != seed.expected_task_version or task.group_id != seed.task_group_id:
        raise s.RemoteError('workspace_busy')
    prepared = rows(tx, 'remote_preparations', 'preparation_id=?', (seed.preparation_id,))
    if not prepared:
        raise s.RemoteError('policy_unavailable')
    prep = prepared[0]
    if (prep.phase != 'prepared' or prep.owner_id != seed.owner_id or prep.task_id != seed.task_id
            or prep.request_id != seed.request_id or prep.argument_hash != seed.argument_hash
            or prep.expected_task_version != seed.expected_task_version
            or prep.relative_name != seed.task_id + '/runs/' + seed.request_id
            or seed.policy_snapshot != task.policy
            or prep.workspace_identity is None
            or prep.workspace_identity.relative_name != prep.relative_name):
        raise s.RemoteError('policy_unavailable')
    path = os.path.join(task.policy.roots.tasks_dir, prep.relative_name)
    info = os.stat(path, follow_symlinks=False)
    if (info.st_dev, info.st_ino) != (prep.workspace_identity.device, prep.workspace_identity.inode):
        raise s.RemoteError('policy_unavailable')
    binding = s.RemoteBinding(job_id=job_id, execution_kind='cowork_remote_v1', seed=seed, workspace_identity=prep.workspace_identity)
    save(tx, 'remote_job_bindings', binding, insert=True)
    record_operation(tx, seed.owner_id, 'delegate', seed.request_id, seed.argument_hash, seed.task_id, job_id=job_id)
    from .remote_runtime import current
    current().capacity.reserve(tx, seed.owner_id, seed.task_id, 'delegate', seed.request_id,
                               'workspace_copy', sum(entry.artifact.bytes for entry in seed.input_manifest))
    save(tx, 'remote_publications', s.PublicationRow(job_id=job_id, owner_id=seed.owner_id, task_id=seed.task_id,
        manifest=(), state='pending', error=None), insert=True)
    task = transition(tx, task.owner_id, task.task_id, task.version, 'busy')
    save(tx, 'remote_tasks', changed(task, active_job_id=job_id))
    preparation_transition(tx, prep.preparation_id, 'prepared', 'adopted', job_id=job_id)
    return binding
