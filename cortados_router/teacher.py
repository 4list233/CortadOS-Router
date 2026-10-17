# PURPOSE: Training-only provider selection through existing Claude and MCP owners.
# CONTRACT: RT-TRAINING-TEACHER-007; no native process supervision or partition arithmetic.
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from integrations.mcp import broker
from integrations.mcp.config import config_bytes, load_config
from integrations.mcp.schemas import WorkerError, validate, ERROR_CODES
from integrations.mcp.job_store import TERMINAL
from . import learning_data, quota_admission, scarcity
from .teacher_attempts import LabelAttempts, _HANDOFF


class PreStartRefusal(Exception):
    def __init__(self, reason):
        if reason not in ('quota_unknown', 'quota_spent', 'scarcity_stop'):
            raise ValueError('Invalid pre-start refusal')
        self._reason = reason
        super().__init__(reason)

    @property
    def reason(self):
        return self._reason


def call_claude(model, batch, *, system_prompt, environment, cwd, config, max_scarcity):
    executable = shutil.which('claude', path=environment.get('PATH'))
    if not executable: raise PreStartRefusal('quota_unknown')
    argv = [str(Path(executable).resolve()), '-p', '--output-format', 'json', '--no-session-persistence',
        '--max-turns', '1', '--tools', '', '--disable-slash-commands', '--strict-mcp-config',
        '--setting-sources', 'project', '--model', model, '--system-prompt', system_prompt]
    # Teacher fields govern only worker_admission; the unchanged quota owner accepts
    # the ordinary closed config shape. Preserve every existing quota/source value.
    quota_config = {key: value for key, value in config.items()
                    if key not in ('teacher_policy', 'teacher_max_scarcity')}
    try:
        actual = quota_admission.prepare_actual('claude-cli', environment=environment,
            executable=argv[0], argv=tuple(argv), cwd=cwd, config=quota_config)
    except PermissionError:
        raise
    except ValueError as exc:
        # prepare_actual documents ValueError as its unproven-source result.
        raise PreStartRefusal('quota_unknown') from exc
    payload = json.dumps(learning_data.teacher_payload(batch), ensure_ascii=False)
    quota = quota_admission.check_actual('claude-cli', model, actual=actual, config=quota_config)
    if quota.get('status') != 'available':
        raise PreStartRefusal('quota_spent' if quota.get('status') == 'spent' else 'quota_unknown')
    lane = (scarcity.current().get('lanes') or {}).get('claude') or {}
    pool = scarcity.pool_for('claude', model, list(lane.get('pools') or {}))
    value = ((lane.get('pools') or {}).get(pool) or lane).get('scarcity')
    if type(value) in (float, int) and math.isfinite(value) and value >= max_scarcity:
        raise PreStartRefusal('scarcity_stop')
    quota = quota_admission.check_actual('claude-cli', model, actual=actual, config=quota_config)
    if quota.get('status') != 'available':
        raise PreStartRefusal('quota_spent' if quota.get('status') == 'spent' else 'quota_unknown')
    # Seconds: inherited teacher timeout bounds a stalled call; smaller aborts useful batches,
    # larger extends retained worker occupancy. No exception after this boundary grants fallback.
    result = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=600,
                            cwd=cwd, env=environment)
    out = json.loads(result.stdout)
    if out.get('is_error'): raise RuntimeError(str(out.get('result'))[:300])
    match = re.search(r'\[[\s\S]*\]', out.get('result') or '')
    return json.loads(match.group(0)) if match else [], out



def record_claude_usage(model, meta):
    """Adopt the existing PTY usage writer; native fallback never enters this path."""
    try:
        import usage
        u = meta.get('usage') or {}
        usage.record(dict(job_id=f"teacher-{meta.get('session_id', time.time())}", runtime='claude',
            account='subscription', model=model, session_id=meta.get('session_id'),
            input_tokens=u.get('input_tokens'), output_tokens=u.get('output_tokens'),
            cache_read_tokens=u.get('cache_read_input_tokens'), cache_write_tokens=u.get('cache_creation_input_tokens'),
            source='router-teacher', label='router-teacher', dedup_key=f"teacher:{meta.get('session_id')}"))
    except Exception as exc:
        print(f"teacher usage unavailable: {type(exc).__name__}", flush=True)


def codex_arguments(batch, *, sample, system_prompt, request_id, cwd):
    # Seconds: inherit the captured teacher 600-second bound; too short interrupts labels,
    # too long retains quota and a native slot beyond the approved batch budget.
    return dict(task=system_prompt, context=json.dumps(learning_data.teacher_payload(batch), ensure_ascii=False),
        selection=dict(mode='explicit', model='gpt-6.1-sol'), constraints=dict(allowed_models=['gpt-6.1-sol']),
        permissions=dict(network=False, mcp_servers=[]), timeout_s=600, account_ref='native-default',
        request_id=request_id, cwd=str(cwd))


class CodexTeacher:
    def __init__(self, config_path):
        self.path = Path(config_path)
        self.raw = config_bytes(self.path)
        self.config_digest = hashlib.sha256(self.raw).hexdigest()
        self.config = load_config(self.path, self.raw)
        if self.config.get('teacher_policy') != 'training_labels_v1':
            raise WorkerError('invalid_request', 'Teacher policy is required')
        self.cwd = Path(self.config['state_dir']) / 'teacher-cwd'
        if not self.cwd.is_dir() or self.cwd.is_symlink():
            raise WorkerError('storage_error', 'Teacher cwd is unavailable')
        info = self.cwd.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or any(self.cwd.iterdir()):
            raise WorkerError('storage_error', 'Teacher cwd must be private and empty')
        self.admission_lock = threading.RLock()
        self.pending_job = False

    def _rpc(self, tool, arguments):
        normalized = validate(tool, arguments, self.config)
        broker.ensure_broker(self.config, self.path, self.config_digest)
        response = broker.rpc(self.config, self.config_digest, tool, normalized)
        if response.get('ok') is not True:
            error = response.get('error', {})
            raise WorkerError(error.get('code', 'storage_error'), error.get('message', 'Teacher broker failure'))
        return response

    def submit(self, arguments):
        return self._rpc('delegate', arguments)

    def status(self, job_id):
        return self._rpc('job_status', dict(job_id=job_id))

    def result(self, job_id):
        return self._rpc('job_result', dict(job_id=job_id))


def _options(options):
    fields = {'split', 'k', 'batch', 'workers', 'limit', 'max_scarcity', 'only'}
    if not isinstance(options, dict) or set(options) != fields: raise ValueError('Invalid teacher options')
    if options['split'] not in ('train', 'holdout', 'all') or options['only'] != '':
        raise ValueError('Invalid fallback selection')
    for name in ('k', 'batch', 'workers', 'limit'):
        value = options[name]
        if type(value) is not int or value < (0 if name == 'limit' else 1): raise ValueError('Invalid ' + name)
    value = options['max_scarcity']
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid max_scarcity')


def _diagnostic(summary, code, attempt_id=None):
    summary['diagnostics'].append(dict(code=code, attempt_id=attempt_id))


def _reconcile(store, client, attempt, snapshot, summary):
    attempt_id = attempt['attempt_id']
    if attempt['config_digest'] != client.config_digest:
        raise WorkerError('storage_error', 'Teacher configuration identity changed')
    with client.admission_lock:
        if client.pending_job:
            _diagnostic(summary, 'job_not_terminal', attempt_id)
            summary['deferred_batches'] += 1
            return
        if attempt['state'] in ('reserved', 'submitted') and attempt['job_id'] is None:
            store.submitted(attempt_id)
            try:
                response = client.submit(attempt['arguments'])
            except WorkerError as exc:
                # Explicit admission refusal proves no job was accepted. Other errors retain
                # submitted/unknown identity so the next invocation replays the same UUID.
                if exc.code in ('capacity_exhausted', 'workspace_busy', 'quota_unknown', 'quota_spent',
                                'permission_refused', 'model_unavailable', 'subscription_required',
                                'account_unavailable', 'invalid_request'):
                    store.defer(attempt_id, exc.code)
                else:
                    client.pending_job = True
                summary['deferred_batches'] += 1
                _diagnostic(summary, exc.code, attempt_id)
                return
            attempt = store.bind_job(attempt_id, response['job']['job_id'])
        # Seconds and cadence inherit broker timeout/grace/poll budgets. A smaller bound
        # abandons observable reconciliation early; a larger one occupies a label worker longer.
        deadline = time.monotonic() + attempt['arguments']['timeout_s'] + client.config['limits']['termination_grace_s']
        try:
            while True:
                status = client.status(attempt['job_id'])
                if status['job']['state'] in TERMINAL: break
                if time.monotonic() >= deadline:
                    client.pending_job = True
                    summary['deferred_batches'] += 1
                    _diagnostic(summary, 'job_not_terminal', attempt_id)
                    return
                time.sleep(client.config['limits']['process_poll_ms'] / 1000)
            terminal = client.result(attempt['job_id'])
            store.terminal(attempt_id, terminal)
            try: answers = json.loads(terminal['result']['summary']) if terminal['job']['state'] == 'succeeded' else []
            except (ValueError, TypeError): answers = None
            if terminal['job']['state'] != 'succeeded':
                error = terminal['result'].get('error') or {}
                code = error.get('code')
                _diagnostic(summary, code if code in ERROR_CODES else 'native_failed', attempt_id)
            elif len(store._answers(answers, len(attempt['row_ids']))) != len(attempt['row_ids']):
                _diagnostic(summary, 'invalid_request', attempt_id)
            summary['published_labels'] += store.publish(attempt_id, terminal=terminal, answers=answers, snapshot=snapshot)
            summary['reconciled_batches'] += 1
        except WorkerError as exc:
            client.pending_job = True
            summary['deferred_batches'] += 1
            _diagnostic(summary, exc.code, attempt_id)


def run_labels(*, data_dir, snapshot, options, system_prompt, claude_environment,
               claude_cwd, worker_config_path, validate_answers, questions):
    _options(options)
    if os.environ.get('CORTADOS_TRAINING_TEACHER_V1') != '1' or os.environ.get('CORTADOS_MCP_LEARNING_V1') != '1':
        raise ValueError('Training teacher requires learning eligibility')
    summary = dict(planned_batches=0, reconciled_batches=0, deferred_batches=0, published_labels=0, diagnostics=[])
    handoff = _HANDOFF.get()
    if handoff is not None:
        active = handoff[0]
        active._claim_handoff(data_dir, validate_answers, questions)
        return _run_locked(active, snapshot, options, system_prompt, claude_environment,
                           claude_cwd, worker_config_path, summary)
    with LabelAttempts(data_dir, validate_answers=validate_answers, questions=questions) as store:
        return _run_locked(store, snapshot, options, system_prompt, claude_environment,
                           claude_cwd, worker_config_path, summary)


def _run_locked(store, snapshot, options, system_prompt, environment, cwd, config_path, summary):
    client = None
    def codex():
        nonlocal client
        if client is None:
            client = CodexTeacher(config_path)
            if client.config['teacher_max_scarcity'] != options['max_scarcity']:
                raise WorkerError('invalid_request', 'Teacher scarcity policy differs from requested ceiling')
        return client
    for diagnostic in snapshot.get('diagnostics', []) + snapshot.get('excluded', []):
        code = diagnostic.get('code', diagnostic.get('reason'))
        if code in ('split_conflict', 'superseded', 'scoring_digest_mismatch'):
            _diagnostic(summary, code)
    labels = store._labels(store.data / 'labels.jsonl')[1]
    slots = set()
    for row in labels:
        key = row['id'], row['sample']
        if key in slots: _diagnostic(summary, 'historical_duplicate_slot')
        slots.add(key)
    # Previously accepted work is reconciled first without consuming a new-call budget.
    pending = store.pending()
    for attempt in pending:
        if attempt['state'] != 'reserved': _reconcile(store, codex(), attempt, snapshot, summary)
    for attempt in pending:
        if attempt['state'] != 'reserved': continue
        if options['limit'] and summary['planned_batches'] >= options['limit']: break
        summary['planned_batches'] += 1
        _reconcile(store, codex(), attempt, snapshot, summary)
    jobs = []
    for partition in ('train', 'valid', 'holdout'):
        if options['split'] != 'all' and options['split'] != partition: continue
        model = 'claude-opus-5' if partition == 'holdout' else 'claude-sonnet-5'
        for sample in range(options['k']):
            todo = sorted((r for r in snapshot['rows'] if snapshot['partitions'].get(r['id']) == partition
                           and not store.occupied(r, sample, model, snapshot)), key=lambda r: r['id'])
            random.Random(f'{partition}-{sample}').shuffle(todo)
            jobs.extend((partition, model, sample, todo[i:i + options['batch']]) for i in range(0, len(todo), options['batch']))
    random.Random(7).shuffle(jobs)
    remaining = max(0, options['limit'] - summary['planned_batches']) if options['limit'] else len(jobs)
    jobs = jobs[:remaining]
    if jobs:
        teacher = codex()
        stop = threading.Event()
        def work(job):
            return _work_batch(store, teacher, job, snapshot, options, system_prompt, environment, cwd, stop)
        with ThreadPoolExecutor(max_workers=options['workers']) as executor:
            for result in executor.map(work, jobs):
                for key in ('planned_batches', 'reconciled_batches', 'deferred_batches', 'published_labels'):
                    summary[key] += result[key]
                summary['diagnostics'].extend(result['diagnostics'])
    return summary


def _work_batch(store, teacher, job, snapshot, options, system_prompt, environment, cwd, stop):
    partition, model, sample, batch = job
    summary = dict(planned_batches=0, reconciled_batches=0, deferred_batches=0, published_labels=0, diagnostics=[])
    if stop.is_set(): return summary
    if teacher.pending_job:
        summary['deferred_batches'] += 1
        _diagnostic(summary, 'job_not_terminal')
        return summary
    summary['planned_batches'] = 1
    try:
        answers, meta = call_claude(model, batch, system_prompt=system_prompt, environment=environment,
                                   cwd=cwd, config=teacher.config, max_scarcity=options['max_scarcity'])
    except PreStartRefusal as exc:
        if partition != 'train':
            summary['deferred_batches'] += 1
            _diagnostic(summary, 'permission_refused' if exc.reason == 'scarcity_stop' else exc.reason)
            return summary
        # One native admission lane covers reservation through terminal reconciliation.
        # Waiting label workers retain no second native slot, and an unknown predecessor
        # prevents them from manufacturing another durable request identity.
        with teacher.admission_lock:
            if teacher.pending_job:
                summary['deferred_batches'] += 1
                _diagnostic(summary, 'job_not_terminal')
                return summary
            arguments = codex_arguments(batch, sample=sample, system_prompt=system_prompt,
                                       request_id=str(uuid.uuid4()), cwd=teacher.cwd)
            arguments = validate('delegate', arguments, teacher.config)
            attempt = store.reserve(batch, sample=sample, system_prompt=system_prompt, arguments=arguments,
                                    config_digest=teacher.config_digest, snapshot=snapshot)
            _reconcile(store, teacher, attempt, snapshot, summary)
    except Exception as exc:
        message = str(exc)
        print(f"  ! {model} s{sample}: {type(exc).__name__}: {message[:160]}", flush=True)
        if re.search(r'limit|429|quota|overloaded', message, re.I): stop.set()
    else:
        record_claude_usage(model, meta)
        summary['published_labels'] += store.publish_claude(batch, model=model, sample=sample,
                                                          answers=answers, meta=meta, snapshot=snapshot)
    return summary
