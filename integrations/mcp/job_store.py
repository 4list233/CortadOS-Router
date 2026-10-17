# PURPOSE: Own durable job identity, workspace reservations, and state transitions.
# CONTRACT: RT-MCP-001 sections 2, 6 and 7; one broker is the only database writer.
# DEPENDS: SQLite transactions, strict MCP schemas, watchdog termination evidence.
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .schemas import WorkerError, canonical, emit_event
from .native_profile import captured_version, record_version

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'})
# Schema version is an identity, not a tunable budget: unknown layouts must refuse.
SCHEMA_VERSION = 1
PROGRESS = {'queued': 'Worker accepted', 'running': 'Codex started',
            'cancelling': 'Stopping worker', 'succeeded': 'Worker completed',
            'failed': 'Native worker failed', 'cancelled': 'Worker cancelled',
            'timed_out': 'Worker timed out', 'interrupted': 'Broker interrupted'}


def utc_ms():
    return time.time_ns() // 1_000_000


def failure_result(record, code, message):
    """A failed attempt has no accepted answer, even when partial artifacts exist."""
    state = Path(record['state_dir'])
    base = Path('jobs') / record['job_id']
    return {'summary': None,
            'worker': {'provider': 'codex-cli', 'requested_model': record['route']['chosen']['model'],
                       'reported_model': None, 'cli_version': record_version(record), 'account_ref': 'native-default'},
            'usage': {key: None for key in ('input_tokens', 'cached_input_tokens',
                      'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')},
            'artifacts': {key: str(base / name) if (state / base / name).is_file() else None
                          for key, name in (('final_message', 'final.txt'), ('events', 'events.jsonl'))},
            'changed_paths': None, 'verification': 'planner_review_required', 'warnings': [],
            'error': {'code': code, 'message': message}}


class JobStore:
    def __init__(self, config, boot_id):
        self.config, self.boot_id = config, boot_id
        path = Path(config['state_dir']) / 'jobs.db'
        existed = path.exists()
        self.db = None
        try:
            self.db = sqlite3.connect(path, isolation_level=None)
            if self.db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise WorkerError('storage_error', 'Job store integrity check failed')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if existed and version != SCHEMA_VERSION:
                raise WorkerError('storage_error', 'Incompatible job store schema')
            if not existed:
                self.db.executescript('''BEGIN IMMEDIATE;
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                        request_id TEXT NOT NULL, argument_hash TEXT NOT NULL,
                        arguments BLOB NOT NULL, cwd TEXT NOT NULL,
                        active INTEGER NOT NULL, record TEXT NOT NULL,
                        UNIQUE(client_id, request_id));
                    CREATE INDEX jobs_active ON jobs(active);
                    PRAGMA user_version=1;
                    COMMIT;''')
            self.db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS job_feedback (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
                    job_id TEXT NOT NULL, feedback_id TEXT NOT NULL,
                    argument_hash TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(client_id,feedback_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id));
                CREATE TRIGGER IF NOT EXISTS job_feedback_no_update BEFORE UPDATE ON job_feedback
                    BEGIN SELECT RAISE(ABORT,'feedback is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS job_feedback_no_delete BEFORE DELETE ON job_feedback
                    BEGIN SELECT RAISE(ABORT,'feedback is append-only'); END;
                COMMIT;""")
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
        except (sqlite3.Error, OSError, WorkerError) as exc:
            if self.db:
                self.db.close()
            raise WorkerError('storage_error', 'Cannot open intact job store') from exc

    def _read(self, sql, params):
        try:
            row = self.db.execute(sql, params).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise WorkerError('storage_error', 'Cannot read job store') from exc

    def get(self, job_id):
        record = self._read('SELECT record FROM jobs WHERE job_id=?', (job_id,))
        if record is None:
            raise WorkerError('job_not_found', 'Job was not found')
        return record

    def replay(self, arguments):
        record = self._read('SELECT record FROM jobs WHERE client_id=? AND request_id=?',
                            (self.config['client_id'], arguments['request_id']))
        if record is not None and record['argument_hash'] != hashlib.sha256(canonical(arguments)).hexdigest():
            raise WorkerError('idempotency_conflict', 'Request identity has different arguments')
        return record

    def active(self):
        try:
            return [json.loads(row[0]) for row in self.db.execute('SELECT record FROM jobs WHERE active=1')]
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise WorkerError('storage_error', 'Cannot read active reservations') from exc

    def accept(self, arguments, route, *, cli_version):
        encoded = canonical(arguments)
        record = {'job_id': str(uuid.uuid4()), 'route_id': route['route_id'], 'state': 'queued',
                  'cwd': arguments['cwd'], 'created_at_ms': utc_ms(), 'started_at_ms': None,
                  'finished_at_ms': None, 'elapsed_ms': 0, 'progress': PROGRESS['queued'],
                  'reason': None, 'arguments': arguments, 'route': route,
                  'cli_version': captured_version(cli_version),
                  'argument_hash': hashlib.sha256(encoded).hexdigest(),
                  'launch_nonce': str(uuid.uuid4()), 'host_boot_id': self.boot_id,
                  'watchdog': None, 'native': None, 'accepted_monotonic': time.monotonic(),
                  'stop_intent': None, 'result': None, 'state_dir': self.config['state_dir']}
        try:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.replay(arguments)
            if previous:
                self.db.execute('ROLLBACK')
                return previous, False
            active = self.active()
            for other in active:
                common = os.path.commonpath((record['cwd'], other['cwd']))
                if common in (record['cwd'], other['cwd']):
                    raise WorkerError('workspace_busy', 'Workspace overlaps an active job')
            if len(active) >= self.config['limits']['max_concurrent_jobs']:
                raise WorkerError('capacity_exhausted', 'All worker slots are reserved')
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                (record['job_id'], self.config['client_id'], arguments['request_id'],
                 record['argument_hash'], encoded, record['cwd'], 1, json.dumps(record)))
            self.db.execute('COMMIT')
        except Exception as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if isinstance(exc, sqlite3.Error):
                raise WorkerError('storage_error', 'Job reservation did not commit') from exc
            raise
        self._event(record, None)
        return record, True

    def feedback(self, arguments):
        from .schemas import validate_feedback
        arguments = validate_feedback(arguments, self.config)
        encoded = canonical(arguments)
        argument_hash = hashlib.sha256(encoded).hexdigest()
        client = self.config['client_id']
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT client_id,record FROM jobs WHERE job_id=?',
                                  (arguments['job_id'],)).fetchone()
            if row is None or row[0] != client:
                raise WorkerError('job_not_found', 'Job was not found')
            if json.loads(row[1])['state'] not in TERMINAL:
                raise WorkerError('job_not_terminal', 'Planner feedback requires a terminal job')
            previous = self.db.execute(
                'SELECT argument_hash,seq,created_at_ms,payload FROM job_feedback WHERE client_id=? AND feedback_id=?',
                (client, arguments['feedback_id'])).fetchone()
            if previous:
                if previous[0] != argument_hash:
                    raise WorkerError('idempotency_conflict', 'Feedback identity has different arguments')
                seq, created = previous[1:3]
            else:
                created = max(0, utc_ms())
                cursor = self.db.execute(
                    'INSERT INTO job_feedback(client_id,job_id,feedback_id,argument_hash,created_at_ms,payload) VALUES (?,?,?,?,?,?)',
                    (client, arguments['job_id'], arguments['feedback_id'], argument_hash, created, encoded.decode('utf-8')))
                seq = cursor.lastrowid
            self.db.execute('COMMIT')
        except (sqlite3.Error, ValueError, KeyError, TypeError, WorkerError) as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if isinstance(exc, WorkerError):
                raise
            raise WorkerError('storage_error', 'Planner feedback did not commit') from exc
        return {'ok': True, 'feedback': {'job_id': arguments['job_id'], 'feedback_id': arguments['feedback_id'],
            'seq': seq, 'created_at_ms': created, 'verdict': arguments['verdict'], 'verification_status': 'planner_asserted'}}

    def _save(self, record):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('UPDATE jobs SET active=?,record=? WHERE job_id=?',
                            (int(record['state'] not in TERMINAL), json.dumps(record), record['job_id']))
            self.db.execute('COMMIT')
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise WorkerError('storage_error', 'Job transition did not commit') from exc

    def _event(self, record, old_state):
        emit_event(self.config, 'state_transition', request_id=record['arguments']['request_id'],
                   route_id=record['route_id'], job_id=record['job_id'], from_state=old_state,
                   state=record['state'], reason=record['reason'])

    def identify(self, job_id, **identities):
        if 'cli_version' in identities:
            raise WorkerError('storage_error', 'Accepted native profile is immutable')
        record = self.get(job_id)
        if record['state'] not in TERMINAL:
            record.update(identities)
            self._save(record)
        return record

    def transition(self, job_id, state, *, reason=None, result=None):
        record = self.get(job_id)
        old = record['state']
        if old in TERMINAL or old == state:
            return record
        allowed = {'queued': {'running', 'failed', 'cancelling', 'interrupted'},
                   'running': {'succeeded', 'failed', 'cancelling', 'interrupted'},
                   'cancelling': {'cancelled', 'timed_out', 'interrupted'}}
        if state not in allowed[old]:
            raise WorkerError('storage_error', 'Invalid job state transition')
        elapsed = self.snapshot(record)['elapsed_ms']
        record.update(state=state, reason=reason, progress=PROGRESS[state], elapsed_ms=elapsed)
        if state == 'running':
            record['started_at_ms'] = utc_ms()
        if state == 'cancelling':
            record['stop_intent'] = reason
        if state in TERMINAL:
            record['finished_at_ms'] = utc_ms()
            record['result'] = result
        self._save(record)
        self._event(record, old)
        return record

    def containment_failed(self, job_id):
        record = self.get(job_id)
        if record['state'] not in TERMINAL and record['reason'] != 'containment_failed':
            record['reason'] = 'containment_failed'
            record['progress'] = 'Worker termination could not be proven'
            self._save(record)
            emit_event(self.config, 'refusal', request_id=record['arguments']['request_id'],
                       route_id=record['route_id'], job_id=record['job_id'],
                       from_state=record['state'], state=record['state'], reason='containment_failed')
        return record

    @staticmethod
    def snapshot(record):
        keys = ('job_id', 'route_id', 'state', 'cwd', 'created_at_ms', 'started_at_ms',
                'finished_at_ms', 'elapsed_ms', 'progress', 'reason')
        snapshot = {key: record[key] for key in keys}
        if record['state'] not in TERMINAL:
            snapshot['elapsed_ms'] = max(record['elapsed_ms'],
                int((time.monotonic() - record['accepted_monotonic']) * 1000))
        return snapshot

    def response(self, record, include_result=False):
        value = {'ok': True, 'job': self.snapshot(record)}
        if include_result:
            value['result'] = record['result']
        return value

    def close(self):
        self.db.close()
