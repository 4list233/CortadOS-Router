# PURPOSE: Sole label writer, durable fallback identities and crash-safe publication.
# CONTRACT: RT-TRAINING-TEACHER-007 sections 3-5; no provider transport.
import copy
import contextvars
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from integrations.mcp.schemas import WorkerError, ERROR_CODES, uuid4, validate
from integrations.mcp.job_store import TERMINAL
from . import learning_data
from .task_evidence import canonical

_ACTIVE = contextvars.ContextVar("teacher_label_invocation", default=None)
_HANDOFF = contextvars.ContextVar("teacher_label_handoff", default=None)

FIELDS = frozenset(('attempt_id', 'request_id', 'row_ids', 'payload', 'sample', 'system_prompt',
    'system_prompt_digest', 'provider', 'model', 'arguments', 'config_digest', 'state', 'job_id',
    'result_digest', 'terminal_state', 'publication_ts', 'publication_rows', 'last_refusal'))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: _bad()
        result[key] = value
    return result


def _bad():
    raise WorkerError('storage_error', 'Teacher attempt state is invalid')


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _integer(value):
    return type(value) is int and value >= 0


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


class LabelAttempts:
    def __init__(self, data_dir, *, validate_answers, questions):
        self.data = Path(data_dir).resolve(strict=True)
        self.identity = dict(self._identity(), volume=self._volume())
        self.private = self.data / '.teacher-attempts'
        self.path = self.private / 'manifest.json'
        self.validate_answers, self.questions = validate_answers, tuple(questions)
        self.mutex = threading.RLock()
        self.lock = None
        self.lock_identity = None
        self.active_token = None
        self._label_cache = {}
        self._slot_cache = None
        self.poisoned = False
        self.owner_pid, self.owner_thread = os.getpid(), threading.get_ident()
        self.handoff_nonce = None

    def _identity(self):
        info = self.data.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid(): _bad()
        return dict(path=str(self.data), device=info.st_dev, inode=info.st_ino)

    def _volume(self):
        """The data folder's APFS volume UUID: stable across reboots, unlike st_dev.

        APFS assigns device numbers at mount time, so a saved st_dev goes stale on reboot
        (2026-10-19: every later run refused its own manifest). Inodes are only unique within
        a volume, so path + inode alone would accept a replacement volume's manifest.
        """
        try:
            df = subprocess.run(['/bin/df', '-P', str(self.data)], capture_output=True, text=True,
                                timeout=10, check=True).stdout.splitlines()
            info = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', df[-1].split()[0]],
                                  capture_output=True, timeout=10, check=True).stdout
            volume = plistlib.loads(info)['VolumeUUID']
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as exc:
            raise WorkerError('storage_error', 'Teacher data volume is unidentifiable') from exc
        if not isinstance(volume, str) or not re.fullmatch('[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}', volume): _bad()
        return volume

    def _continuous(self, saved):
        """Whether a saved identity names this same folder on this same volume.

        A manifest without a volume UUID proves nothing about its volume (a replacement volume can
        receive the same device number), so it is refused until migrate_volume records one.
        """
        if (not isinstance(saved, dict) or set(saved) != {'path', 'device', 'inode', 'volume'}
                or not isinstance(saved['path'], str) or not _integer(saved['device'])
                or not _integer(saved['inode'])): _bad()
        return (saved['path'], saved['inode'], saved['volume']) == (
            self.identity['path'], self.identity['inode'], self.identity['volume'])

    @classmethod
    def migrate_volume(cls, data_dir, volume):
        """Operator migration for a manifest written before the volume field existed.

        The operator names the volume UUID they verified as the original; it must equal the data
        folder's current volume, and the saved path, device and inode must all still match. Takes
        the writer lock, exposes no attempts, and changes nothing but data_identity.
        """
        store = cls(data_dir, validate_answers=None, questions=())
        if volume != store.identity['volume']: _bad()
        try:
            fd = os.open(store.private / 'lock', os.O_RDWR | os.O_NOFOLLOW)
            with os.fdopen(fd, 'a+b'):
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise WorkerError('workspace_busy', 'Teacher labels already have a writer') from exc
                store._secure(store.path, 0o600)
                saved = json.loads(store.path.read_text(), object_pairs_hook=_unique)
                legacy = {k: store.identity[k] for k in ('path', 'device', 'inode')}
                if not isinstance(saved, dict) or saved.get('data_identity') != legacy: _bad()
                saved['data_identity'] = store.identity
                store._atomic(store.path, canonical(saved).encode(), 0o600)
        except (OSError, ValueError) as exc:
            raise WorkerError('storage_error', 'Teacher attempt state is invalid') from exc
        return store.identity

    def _check(self):
        if self.poisoned or self.lock is None or self.lock.closed or self.owner_pid != os.getpid(): _bad()
        try:
            held = os.fstat(self.lock.fileno())
            current = (self.private / 'lock').lstat()
            if ((held.st_dev, held.st_ino) != self.lock_identity
                    or (current.st_dev, current.st_ino) != self.lock_identity): _bad()
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerError('storage_error', 'Teacher writer lock is unavailable') from exc
        if (self._identity() != {k: self.identity[k] for k in ('path', 'device', 'inode')}
                or self.manifest['data_identity'] != self.identity): _bad()
        self._secure(self.private, 0o700, directory=True)
        self._secure(self.private / 'lock', 0o600)
        if self.path.exists() or self.path.is_symlink(): self._secure(self.path, 0o600)

    @staticmethod
    def _secure(path, mode, directory=False):
        info = path.lstat()
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not kind(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode: _bad()

    def __enter__(self):
        with self.mutex:
            if self.lock is not None: _bad()
            try:
                if not self.private.exists() and not self.private.is_symlink(): self.private.mkdir(mode=0o700)
                self._secure(self.private, 0o700, directory=True)
                fd = os.open(self.private / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                self.lock = os.fdopen(fd, 'a+b')
                held = os.fstat(fd)
                self.lock_identity = held.st_dev, held.st_ino
                self._secure(self.private / 'lock', 0o600)
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise WorkerError('workspace_busy', 'Teacher labels already have a writer') from exc
                # A new lock acquisition starts a fresh derived-cache generation.
                self._label_cache.clear()
                self._slot_cache = None
                if self.path.exists() or self.path.is_symlink():
                    self._secure(self.path, 0o600)
                    self.manifest = json.loads(self.path.read_text(), object_pairs_hook=_unique)
                else:
                    self.manifest = dict(schema='cortados.teacher-attempts.v1', data_identity=self.identity,
                                         revision=0, attempts=[])
                self._validate_manifest()
                # Same folder on the same volume: adopt this boot's device number and the volume UUID;
                # the next save persists both.
                self.manifest['data_identity'] = self.identity
                self._check()
                self._labels(self.data / 'labels.jsonl')
                self.active_token = _ACTIVE.set(self)
                return self
            except WorkerError:
                self.__exit__(None, None, None)
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.__exit__(None, None, None)
                raise WorkerError('storage_error', 'Teacher attempt state is invalid') from exc

    def __exit__(self, *args):
        with self.mutex:
            self._label_cache.clear()
            self._slot_cache = None
            if self.active_token is not None:
                _ACTIVE.reset(self.active_token)
                self.active_token = None
            if self.lock is not None:
                self.lock.close()
                self.lock = None

    @contextmanager
    def _handoff(self):
        """One-use adapter handoff; a copied Context cannot confer a second invocation."""
        with self.mutex:
            if (self.lock is None or self.lock.closed or self.owner_pid != os.getpid()
                    or self.owner_thread != threading.get_ident() or _ACTIVE.get() is not self
                    or self.handoff_nonce is not None): _bad()
            self._check()
            self.handoff_nonce = uuid.uuid4().hex
            token = _HANDOFF.set((self, self.owner_pid, self.owner_thread, self.handoff_nonce))
        try:
            yield
        finally:
            self.handoff_nonce = None
            _HANDOFF.reset(token)

    def _claim_handoff(self, data_dir, validator, questions):
        with self.mutex:
            expected = (self, os.getpid(), threading.get_ident(), self.handoff_nonce)
            if (self.handoff_nonce is None or _HANDOFF.get() != expected or _ACTIVE.get() is not self
                    or self.owner_pid != os.getpid() or self.owner_thread != threading.get_ident()
                    or self.lock is None or self.lock.closed or self.data != Path(data_dir).resolve()
                    or self.validate_answers is not validator or self.questions != tuple(questions)): _bad()
            self._check()
            self.handoff_nonce = None

    def _validate_manifest(self):
        m = self.manifest
        if (not isinstance(m, dict) or set(m) != {'schema', 'data_identity', 'revision', 'attempts'}
                or m['schema'] != 'cortados.teacher-attempts.v1' or not _integer(m['revision'])
                or not self._continuous(m['data_identity']) or not isinstance(m['attempts'], list)): _bad()
        ids, requests, slots = set(), set(), set()
        for a in m['attempts']:
            self._validate_attempt(a)
            if a['attempt_id'] in ids or a['request_id'] in requests: _bad()
            ids.add(a['attempt_id']); requests.add(a['request_id'])
            for row_id in a['row_ids']:
                slot = row_id, a['sample']
                if slot in slots: _bad()
                slots.add(slot)

    def _validate_attempt(self, a):
        if not isinstance(a, dict) or set(a) != FIELDS: _bad()
        for key in ('attempt_id', 'request_id'):
            try: uuid4(a[key], key)
            except WorkerError: _bad()
        if (not isinstance(a['row_ids'], list) or not a['row_ids']
                or any(not isinstance(i, str) or not i for i in a['row_ids'])
                or len(set(a['row_ids'])) != len(a['row_ids']) or not _integer(a['sample'])
                or not isinstance(a['payload'], list) or len(a['payload']) != len(a['row_ids'])
                or a['provider'] != 'codex-cli' or a['model'] != 'gpt-6.1-sol'
                or not isinstance(a['system_prompt'], str)
                or a['system_prompt_digest'] != _digest(a['system_prompt'].encode())
                or not _sha(a['config_digest']) or not isinstance(a['arguments'], dict)
                or a['arguments'].get('request_id') != a['request_id']
                or a['arguments'].get('task') != a['system_prompt']
                or json.loads(a['arguments'].get('context', 'null')) != a['payload']
                or a['last_refusal'] not in ERROR_CODES | {None}): _bad()
        for index, row in enumerate(a['payload']):
            if (not isinstance(row, dict) or type(row.get('i')) is not int or row['i'] != index
                    or not {'i', 'unit_kind', 'request'} <= set(row)
                    or set(row) - {'i', 'unit_kind', 'request', 'outcome_annotation'}
                    or not isinstance(row['unit_kind'], str) or not isinstance(row['request'], str)): _bad()
        try:
            normalized = validate('delegate', a['arguments'], {})
        except WorkerError: _bad()
        if (normalized != a['arguments'] or normalized['selection'] != {'mode': 'explicit', 'model': 'gpt-6.1-sol'}
                or normalized['constraints'] != {'allowed_models': ['gpt-6.1-sol']}
                or normalized['permissions'] != {'network': False, 'mcp_servers': []}
                or normalized['timeout_s'] != 600 or 'task_evidence' in normalized): _bad()
        state = a['state']
        if state not in ('reserved', 'submitted', 'terminal', 'published'): _bad()
        if a['job_id'] is not None:
            try: uuid4(a['job_id'], 'job_id')
            except WorkerError: _bad()
        if state == 'reserved' and a['job_id'] is not None: _bad()
        if state in ('reserved', 'submitted'):
            if any(a[k] is not None for k in ('result_digest', 'terminal_state', 'publication_ts', 'publication_rows')): _bad()
        elif a['job_id'] is None or not _sha(a['result_digest']) or a['terminal_state'] not in TERMINAL: _bad()
        if (a['publication_ts'] is None) != (a['publication_rows'] is None): _bad()
        if state == 'published' and a['publication_ts'] is None: _bad()
        if a['publication_ts'] is not None:
            if not _integer(a['publication_ts']) or not isinstance(a['publication_rows'], list): _bad()
            seen = set()
            for row in a['publication_rows']:
                if (not isinstance(row, dict) or set(row) != {'index', 'line_digest'}
                        or not _integer(row['index']) or row['index'] >= len(a['row_ids'])
                        or row['index'] in seen or not _sha(row['line_digest'])): _bad()
                seen.add(row['index'])

    def _atomic(self, path, content, mode):
        fd, temp = tempfile.mkstemp(prefix='.teacher-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
                os.replace(temp, path)
                parent = os.open(path.parent, os.O_RDONLY)
                try: os.fsync(parent)
                finally: os.close(parent)
                published = os.fstat(stream.fileno())
                if (written.st_size, written.st_mtime_ns) != (published.st_size, published.st_mtime_ns): _bad()
                return published
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def _save(self, candidate):
        self._check()
        candidate['revision'] = self.manifest['revision'] + 1
        try:
            self._atomic(self.path, canonical(candidate).encode(), 0o600)
        except BaseException:
            self.poisoned = True
            raise
        if self._slot_cache is not None:
            for attempt in candidate['attempts'][len(self.manifest['attempts']):]:
                self._slot_cache[2].update((rid, attempt['sample']) for rid in attempt['row_ids'])
        self.manifest = candidate

    def _update(self, attempt_id, **changes):
        candidate = copy.deepcopy(self.manifest)
        a = next((a for a in candidate['attempts'] if a['attempt_id'] == attempt_id), None)
        if a is None: _bad()
        a.update(changes)
        self._validate_attempt(a)
        self._save(candidate)
        return copy.deepcopy(a)

    def _get(self, attempt_id):
        self._check()
        a = next((a for a in self.manifest['attempts'] if a['attempt_id'] == attempt_id), None)
        if a is None: _bad()
        return a

    def _labels(self, path):
        if not path.exists() and not path.is_symlink(): return b'', [], 0o644
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid(): _bad()
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        cached = self._label_cache.get(path)
        if cached is not None and cached[0] == signature: return cached[1]
        raw = path.read_bytes()
        if raw and not raw.endswith(b'\n'): _bad()
        try:
            rows = [json.loads(line) for line in raw.splitlines()]
            if any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                   or not isinstance(row.get('teacher'), str) or not _integer(row.get('sample')) for row in rows): _bad()
        except (ValueError, UnicodeError): _bad()
        result = raw, rows, stat.S_IMODE(info.st_mode)
        self._label_cache[path] = signature, result
        if path == self.data / 'labels.jsonl': self._slot_cache = None
        return result

    def _append(self, path, prior, rows, lines):
        """Retain derived caches only for this owner's validated append generation."""
        raw, labels, mode = prior
        current = self._labels(path)
        if current[0] != raw or current[2] != mode: _bad()
        self._check()
        content = raw + b''.join(lines)
        try:
            published = self._atomic(path, content, mode)
        except BaseException:
            self.poisoned = True
            raise
        info = path.lstat()
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if signature != (published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns, published.st_ctime_ns): _bad()
        self._label_cache[path] = signature, (content, labels + rows, mode)
        if path == self.data / 'labels.jsonl' and self._slot_cache is not None:
            for row in rows:
                identity = row['id'], row['sample'], row.get('observation_digest')
                self._slot_cache[0].add(identity)
                self._slot_cache[1].add((*identity, row['teacher']))

    def occupied(self, row, sample, model, snapshot):
        self._check()
        labels = self._labels(self.data / 'labels.jsonl')[1]
        if self._slot_cache is None:
            full, teachers, reserved = set(), set(), set()
            for label in labels:
                if label.get('override'): continue
                digest = label.get('observation_digest')
                full.add((label['id'], label['sample'], digest))
                teachers.add((label['id'], label['sample'], digest, label['teacher']))
            for attempt in self.manifest['attempts']:
                reserved.update((rid, attempt['sample']) for rid in attempt['row_ids'])
            self._slot_cache = full, teachers, reserved
        full, teachers, reserved = self._slot_cache
        train = row.get('split') == 'train' if snapshot is None else snapshot['partitions'].get(row['id']) == 'train'
        identity = row['id'], sample, row.get('observation_digest')
        return ((identity in full if train else (*identity, model) in teachers)
                or (row['id'], sample) in reserved)

    def pending(self):
        with self.mutex:
            self._check()
            return tuple(copy.deepcopy(a) for a in self.manifest['attempts'] if a['state'] != 'published')

    def reserve(self, batch, *, sample, system_prompt, arguments, config_digest, snapshot):
        with self.mutex:
            if not _integer(sample) or snapshot is None or not batch: _bad()
            current = {r['id']: r for r in snapshot['rows']}
            if any(snapshot['partitions'].get(r['id']) != 'train' or current.get(r['id']) != r
                   or self.occupied(r, sample, 'gpt-6.1-sol', snapshot) for r in batch): _bad()
            a = dict(attempt_id=str(uuid.uuid4()), request_id=arguments['request_id'],
                row_ids=[r['id'] for r in batch], payload=learning_data.teacher_payload(batch), sample=sample,
                system_prompt=system_prompt, system_prompt_digest=_digest(system_prompt.encode()),
                provider='codex-cli', model='gpt-6.1-sol', arguments=copy.deepcopy(arguments),
                config_digest=config_digest, state='reserved', job_id=None, result_digest=None,
                terminal_state=None, publication_ts=None, publication_rows=None, last_refusal=None)
            self._validate_attempt(a)
            candidate = copy.deepcopy(self.manifest); candidate['attempts'].append(a)
            self._save(candidate)
            return copy.deepcopy(a)

    def submitted(self, attempt_id):
        with self.mutex:
            a = self._get(attempt_id)
            if a['state'] not in ('reserved', 'submitted'): _bad()
            return self._update(attempt_id, state='submitted', last_refusal=None)

    def bind_job(self, attempt_id, job_id):
        with self.mutex:
            a = self._get(attempt_id)
            if a['state'] != 'submitted' or a['job_id'] not in (None, job_id): _bad()
            return self._update(attempt_id, job_id=job_id)

    def defer(self, attempt_id, code):
        with self.mutex:
            a = self._get(attempt_id)
            if a['state'] != 'submitted' or a['job_id'] is not None or code not in ERROR_CODES: _bad()
            return self._update(attempt_id, state='reserved', last_refusal=code)

    def terminal(self, attempt_id, terminal_payload):
        with self.mutex:
            a = self._get(attempt_id)
            job = terminal_payload.get('job', {})
            digest = _digest(canonical(terminal_payload).encode())
            if (terminal_payload.get('ok') is not True or job.get('job_id') != a['job_id']
                    or job.get('state') not in TERMINAL or not isinstance(terminal_payload.get('result'), dict)): _bad()
            if a['state'] in ('terminal', 'published'):
                if a['result_digest'] != digest or a['terminal_state'] != job['state']: _bad()
                return copy.deepcopy(a)
            if a['state'] != 'submitted': _bad()
            return self._update(attempt_id, state='terminal', result_digest=digest, terminal_state=job['state'])

    def _answers(self, answers, count):
        if not isinstance(answers, list): return []
        seen = set()
        for answer in answers:
            if (not isinstance(answer, dict) or not _integer(answer.get('i'))
                    or answer['i'] >= count or answer['i'] in seen): return []
            seen.add(answer['i'])
        return [a for a in answers if self.validate_answers(a)]

    def publish(self, attempt_id, *, terminal, answers, snapshot):
        with self.mutex:
            a = self.terminal(attempt_id, terminal)
            current = {r['id']: r for r in snapshot['rows']} if snapshot is not None else {}
            if any(rid not in current or snapshot['partitions'].get(rid) != 'train' for rid in a['row_ids']): _bad()
            batch = [current[rid] for rid in a['row_ids']]
            if learning_data.teacher_payload(batch) != a['payload']: _bad()
            if a['state'] == 'published': return 0
            good = self._answers(answers, len(batch)) if a['terminal_state'] == 'succeeded' else []
            if good:
                result = terminal['result']; worker = result.get('worker', {})
                if (result.get('error') is not None or worker.get('provider') != 'codex-cli'
                        or worker.get('requested_model') != a['model'] or worker.get('account_ref') != 'native-default'
                        or worker.get('reported_model') is not None or not worker.get('cli_version')): _bad()
                try: actual = json.loads(result['summary'])
                except (ValueError, TypeError): _bad()
                if actual != answers: _bad()
            timestamp = a['publication_ts'] if a['publication_ts'] is not None else int(time.time())
            lines, newrows = [], []
            for answer in good:
                row = batch[answer['i']]
                execution = dict(schema='cortados.teacher-execution.v1', provider='codex-cli',
                    requested_model=a['model'], reported_model=None, job_id=a['job_id'], request_id=a['request_id'],
                    attempt_id=a['attempt_id'], cli_version=terminal['result']['worker']['cli_version'], result_digest=a['result_digest'])
                label = dict(id=row['id'], teacher=a['model'], sample=a['sample'], ts=timestamp,
                    **learning_data.label_provenance(row), answers={q: answer[q] for q in self.questions}, teacher_execution=execution)
                newrows.append(label)
                lines.append((answer['i'], (canonical(label) + '\n').encode()))
            intent = [dict(index=i, line_digest=_digest(line)) for i, line in lines]
            if a['publication_rows'] is not None and a['publication_rows'] != intent: _bad()
            if a['publication_rows'] is None:
                a = self._update(attempt_id, publication_ts=timestamp, publication_rows=intent)
            path = self.data / 'labels.jsonl'; raw, labels, mode = self._labels(path)
            existing = [line for line, row in zip(raw.splitlines(keepends=True), labels)
                        if (row.get('teacher_execution') or {}).get('attempt_id') == attempt_id]
            wanted = [line for _, line in lines]
            if existing:
                if existing != wanted: _bad()
                added = 0
            else:
                if any(row['id'] in a['row_ids'] and row['sample'] == a['sample'] for row in labels): _bad()
                self._check()
                if wanted: self._append(path, (raw, labels, mode), newrows, wanted)
                added = len(wanted)
            self._update(attempt_id, state='published')
            return added

    @staticmethod
    def _indexed(answers, count):
        return [a for a in answers if isinstance(a, dict) and isinstance(a.get('i'), int)
                and 0 <= a['i'] < count] if isinstance(answers, list) else []

    def publish_claude(self, batch, *, model, sample, answers, meta, snapshot):
        with self.mutex:
            self._check()
            path = self.data / 'labels.jsonl'; raw, labels, mode = self._labels(path)
            lines, newrows = [], []
            seen = set()
            for answer in self._indexed(answers, len(batch)):
                if not self.validate_answers(answer): continue
                row = batch[answer['i']]
                if (row['id'], sample) in seen or self.occupied(row, sample, model, snapshot): continue
                seen.add((row['id'], sample))
                label = dict(id=row['id'], teacher=model, sample=sample, ts=int(time.time()),
                    **(learning_data.label_provenance(row) if snapshot is not None else {}),
                    answers={q: answer[q] for q in self.questions})
                newrows.append(label)
                lines.append((canonical(label) + '\n').encode())
            if lines: self._append(path, (raw, labels, mode), newrows, lines)
            return len(lines)

    def publish_override(self, batch, *, model, sample, answers, only, defaults, snapshot):
        with self.mutex:
            self._check()
            if not only or any(q not in self.questions for q in only):
                raise WorkerError('invalid_request', 'Unknown override question')
            path = self.data / ('labels-override-' + '-'.join(only) + '.jsonl')
            raw, labels, mode = self._labels(path)
            done = {(r['id'], r['teacher'], r['sample']) for r in labels}
            lines, newrows = [], []
            for answer in self._indexed(answers, len(batch)):
                row = batch[answer['i']]; key = row['id'], model, sample
                selected = {q: answer[q] for q in only if q in answer}
                if key in done or len(selected) != len(only) or not self.validate_answers({**defaults, **selected}): continue
                done.add(key)
                label = dict(id=row['id'], teacher=model, sample=sample, ts=int(time.time()), override=True,
                    **(learning_data.label_provenance(row) if snapshot is not None else {}), answers=selected)
                newrows.append(label)
                lines.append((canonical(label) + '\n').encode())
            if lines: self._append(path, (raw, labels, mode), newrows, lines)
            return len(lines)
