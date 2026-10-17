# PURPOSE: Read consistent SQLite snapshots into typed MCP learning observations.
# CONTRACT: RT-MCP-LEARNING-003; accepted inputs are authoritative, outcomes are evidence.
# DEPENDS: Read-only SQLite, task_evidence projections, native_profile version owner.
# CEILING: No job writes, artifact crawling, inferred correctness or teacher answers.
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from integrations.mcp.native_profile import record_version
from integrations.mcp.schemas import JOB_STATES, WorkerError, canonical as argument_bytes, validate, validate_feedback, uuid4
from .task_evidence import MAX_SCORER_CHARS, canonical, digest, learning_text, scoring_text, validate_evidence

TERMINAL = frozenset(('succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'))
USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens')


class SourceError(Exception):
    def __init__(self, code, source):
        self.code, self.source = code, str(source)
        super().__init__(code)


def _snapshot(path, queries):
    if not path.is_file():
        raise SourceError('mcp_source_unavailable', path)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('BEGIN')
            if path.name == 'jobs.db' and db.execute('PRAGMA user_version').fetchone() != (1,):
                raise ValueError('Unknown job store schema')
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('Source integrity')
            result = []
            for query in queries:
                if 'FROM job_feedback' in query and not db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_feedback'").fetchone():
                    result.append([])
                else:
                    result.append(db.execute(query).fetchall())
            return result
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise SourceError('mcp_source_invalid', path) from exc


def _string(value, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ValueError('Invalid source string')
    value.encode('utf-8')


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('Invalid nonnegative source integer')


def _feedback(rows):
    result = {}
    for seq, client, job, feedback_id, argument_hash, created, payload in rows:
        _integer(seq)
        _integer(created)
        value = json.loads(payload)
        # Reuse transport validation with the actual payload length as the bound;
        # collection validates shape without imposing today's deployment budget.
        normalized = validate_feedback(value, {'limits': {'max_handoff_bytes': len(payload.encode('utf-8'))}})
        if (seq < 1 or normalized != value or value['job_id'] != job
                or value['feedback_id'] != feedback_id or digest(canonical(value)) != argument_hash):
            raise ValueError('Invalid feedback identity')
        result.setdefault(job, []).append((client, dict(value, seq=seq, created_at_ms=created)))
    return result


def _observation(row, route_context, feedback):
    job_id, client, request_id, argument_hash, encoded, raw = row
    record = json.loads(raw)
    if not isinstance(record, dict) or not isinstance(record.get('arguments'), dict):
        raise ValueError('Invalid job record schema')
    args = record['arguments']
    for identifier in (job_id, request_id, record['route_id']):
        uuid4(identifier, 'source identity')
    routing = {key: value for key, value in args.items() if key not in ('cwd', 'request_id', 'timeout_s')}
    if validate('route', routing, {}) != routing:
        raise ValueError('Invalid accepted argument schema')
    _string(args['cwd'])
    if not Path(args['cwd']).is_absolute() or type(args['timeout_s']) is not int or args['timeout_s'] < 1:
        raise ValueError('Invalid accepted execution schema')
    state = record['state']
    if (state not in JOB_STATES or record['job_id'] != job_id or args['request_id'] != request_id
            or record['argument_hash'] != argument_hash or hashlib.sha256(argument_bytes(args)).hexdigest() != argument_hash
            or argument_bytes(args) != bytes(encoded)):
        raise ValueError('Invalid job identity or schema')
    _string(client)
    _string(args['task'])
    _string(args.get('context', ''))
    _string(record['reason'], nullable=True)
    _integer(record['elapsed_ms'])
    mode = args['selection']['mode']
    if mode not in ('s1', 'explicit'):
        raise ValueError('Invalid selection')
    evidence = args.get('task_evidence')
    if evidence is not None:
        validate_evidence(evidence)
    reviews = []
    for feedback_client, value in feedback.get(job_id, []):
        if feedback_client != client:
            raise ValueError('Feedback ownership mismatch')
        reviews.append(value)
    result = record['result']
    if result is not None and not isinstance(result, dict):
        raise ValueError('Invalid native result')
    native_worker = result.get('worker') if result is not None else {}
    if not isinstance(native_worker, dict):
        raise ValueError('Invalid native worker schema')
    reported = native_worker.get('reported_model')
    _string(reported, nullable=True)
    requested = record['route']['chosen']['model']
    _string(requested)
    if result is not None and (native_worker.get('provider') != 'codex-cli'
            or native_worker.get('requested_model') != requested
            or record_version(native_worker) != record_version(record)):
        raise ValueError('Invalid native worker identity')
    worker = {'provider': 'codex-cli', 'requested_model': requested,
              'reported_model': reported, 'cli_version': record_version(record)}
    usage = result['usage'] if result is not None else None
    if usage is not None:
        if not isinstance(usage, dict) or set(usage) != set(USAGE_FIELDS):
            raise ValueError('Invalid native usage schema')
        for value in usage.values():
            if value is not None:
                _integer(value)
    scoring = scoring_text(args)
    stored_digest = digest(scoring) if scoring is not None else None
    reason = None
    if isinstance(route_context, dict) and 'projection_version' in route_context:
        if route_context['projection_version'] != 1:
            raise ValueError('Unknown projection version')
        if mode == 's1':
            scoring = route_context['scoring_text']
            stored_digest = route_context['scoring_digest']
            _string(scoring)
            _string(stored_digest)
            if digest(scoring) != stored_digest or scoring != scoring_text(args):
                reason = 'scoring_digest_mismatch'
    text = scoring if mode == 's1' else learning_text(args)
    if reason is None and len(text) > MAX_SCORER_CHARS:
        reason = 'scoring_context_too_large'
    outcome = 'completed' if state == 'succeeded' else (state if state in TERMINAL else 'pending')
    value = {'schema': 'cortados.mcp-learning.v1', 'job_id': job_id, 'route_id': record['route_id'],
        'request_id': request_id, 'client_id': client, 'state': state, 'reason': record['reason'],
        'task': args['task'], 'context': args.get('context', ''), 'task_evidence': evidence,
        'selection_mode': mode, 'scoring_text': scoring, 'scoring_digest': stored_digest,
        'learning_text': text, 'task_group_id': evidence['task_group_id'] if evidence else None,
        'worker': worker, 'usage': usage, 'elapsed_ms': record['elapsed_ms'] if state in TERMINAL else None,
        'feedback': reviews, 'native_outcome': outcome,
        'planner_verdict': reviews[-1]['verdict'] if reviews else None,
        'eligibility': reason is None, 'exclusion_reason': reason}
    material = {key: value[key] for key in ('job_id', 'learning_text', 'native_outcome', 'reason',
                                          'worker', 'usage', 'elapsed_ms', 'feedback')}
    value['observation_digest'] = digest(canonical(material))
    value['learning_id'] = 'mcp.' + job_id + '.' + value['observation_digest']
    return value


def read_observations(state_dir):
    state = Path(state_dir)
    jobs_path, routes_path = state / 'jobs.db', state / 'router/router.db'
    job_rows, feedback_rows = _snapshot(jobs_path, [
        'SELECT job_id,client_id,request_id,argument_hash,arguments,record FROM jobs ORDER BY job_id',
        'SELECT seq,client_id,job_id,feedback_id,argument_hash,created_at_ms,payload FROM job_feedback ORDER BY seq'])
    route_rows, = _snapshot(routes_path, ['SELECT turn_id,context FROM routes ORDER BY turn_id'])
    try:
        routes = {}
        for route_id, context in route_rows:
            _string(route_id)
            try:
                parsed = json.loads(context) if isinstance(context, str) else context
            except ValueError:
                parsed = context
            routes[route_id] = parsed
        feedback = _feedback(feedback_rows)
        if set(feedback) - {row[0] for row in job_rows}:
            raise ValueError('Orphan feedback')
        observations, joined = [], set()
        for row in job_rows:
            record = json.loads(row[-1])
            route_id = record['route_id']
            observations.append(_observation(row, routes.get(route_id), feedback))
            joined.add(route_id)
        for route_id in sorted(set(routes) - joined):
            observations.append({'schema': 'cortados.mcp-route-observation.v1', 'route_id': route_id,
                'context': routes[route_id], 'eligibility': False, 'exclusion_reason': 'no_accepted_job'})
        return observations
    except (ValueError, TypeError, KeyError, UnicodeError, WorkerError) as exc:
        raise SourceError('mcp_source_invalid', jobs_path) from exc
