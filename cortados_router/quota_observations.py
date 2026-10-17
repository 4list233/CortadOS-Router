# PURPOSE: Collect existing read-only quota sources in bounded isolated children.
# CONTRACT: RT-FRESH-QUOTA-005; one in-flight probe per source, no persistent writes.
# DEPENDS: NativeMetadata and existing per-lane scarcity samplers.
"""Only the parent timestamps source start; failed refresh discards old evidence."""
import concurrent.futures
import contextvars
import hashlib
import hmac
import secrets
import copy
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import quota_admission as policy

# Immutable deployment dependency; private fixtures may patch this constant.
RELEASE_PYTHON = '/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/bin/python'
LANES = frozenset(('claude', 'codex', 'antigravity', 'cursor', 'local'))
# Bytes per pipe read: syscall tradeoff only; configured combined output cap bounds allocation.
READ_BYTES = 65536
_lock = threading.Lock()
_cache = {}
_inflight = {}
_codex_environment = contextvars.ContextVar("quota_codex_environment", default=None)
_key_pid, _key_secret = os.getpid(), secrets.token_bytes(32)


def _configuration(config):
    if not isinstance(config, dict):
        raise ValueError('Observation config must be normalized')
    try:
        from integrations.mcp.config import LIMITS
        from integrations.mcp.schemas import WorkerError, object_fields, positive_integer, string_list, text_value
        fields = ('schema', 'client_id', 'state_dir', 'codex_home', 'codex_bin', 'model_allowlist', 'allow_network', 'connector_registry', 'limits')
        object_fields(config, fields, fields, 'normalized config')
        if config['schema'] != 'cortados.mcp-worker.config.v1' or type(config['allow_network']) is not bool:
            raise ValueError('Invalid observation config schema')
        text_value(config['client_id'], 'client_id')
        string_list(config['model_allowlist'], 'model_allowlist', empty=False)
        if not isinstance(config['connector_registry'], dict):
            raise ValueError('Invalid connector registry')
        object_fields(config['limits'], LIMITS, LIMITS, 'limits')
        for key in LIMITS:
            positive_integer(config['limits'][key], key)
        for key in ('codex_bin', 'codex_home', 'state_dir'):
            policy._path(config[key])
        return json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (KeyError, TypeError, ValueError, WorkerError) as exc:
        raise ValueError('Observation config must be normalized') from exc


def _environment(lane, config):
    from . import scarcity
    from integrations.mcp.codex import native_environment
    env = dict(_codex_environment.get() or native_environment(config)) if lane == 'codex' else scarcity._cli_env()
    if lane == 'cursor':
        env['AGENT_CLI_CREDENTIAL_STORE'] = 'file'
    command = {'codex': config['codex_bin'], 'claude': 'claude', 'cursor': 'cursor-agent', 'antigravity': 'agy'}.get(lane)
    executable = shutil.which(command, path=env.get('PATH')) if command else None
    scope = policy._source_scope(lane, env, executable, config) if lane != 'local' else policy._unproven(lane)
    return env, executable, scope


def _failure(lane, scope, wall, mono, reason):
    return dict(lane=lane, source='quota-observation', auth_scope=scope, observed_at=wall,
                observed_monotonic=mono, record=None, error={'code': 'quota_unknown', 'reason': reason})


def _reap(process, tracker, grace):
    if tracker is not None:
        tracker.signal_groups(signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if tracker is not None:
        tracker.signal_groups(signal.SIGKILL)
    # An unreaped Popen root still owns its PID, even if descendant inventory
    # failed. Stop that root without guessing any descendant process group.
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return False
    return tracker is not None and tracker.contained()


def _payload(output):
    value = json.loads(output.decode('utf-8'))
    if not output.endswith(b'\n') or not isinstance(value, dict) or set(value) != {'source', 'auth_scope', 'record', 'error'}:
        raise ValueError('Invalid child envelope')
    if not isinstance(value['source'], str) or not value['source'].strip():
        raise ValueError('Invalid child source')
    scope = value['auth_scope']
    if not isinstance(scope, dict) or set(scope) != {'kind', 'lane', 'scope_id'}:
        raise ValueError('Invalid child scope')
    if scope['kind'] == 'cli_store':
        if not policy._valid_scope(scope, scope['lane']):
            raise ValueError('Invalid child scope identity')
    elif scope != policy._unproven(scope['lane']):
        raise ValueError('Unsupported child scope identity')
    error = value['error']
    if error is None:
        if not isinstance(value['record'], dict):
            raise ValueError('Missing child record')
    elif (value['record'] is not None or not isinstance(error, dict) or set(error) != {'code', 'reason'} or
          error['code'] not in ('quota_unknown', 'quota_spent') or not isinstance(error['reason'], str) or not error['reason'].strip()):
        raise ValueError('Invalid child error')
    return value


def _exchange(process, config, deadline, tracker, *, request=None, parser=None):
    selector = selectors.DefaultSelector()
    pending = memoryview((json.dumps(config if request is None else request, ensure_ascii=False) + '\n').encode())
    size, output = 0, bytearray()
    try:
        for pipe, event in ((process.stdin, selectors.EVENT_WRITE),
                            (process.stdout, selectors.EVENT_READ), (process.stderr, selectors.EVENT_READ)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, event)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Quota observation deadline expired')
            tracker.sample()
            if tracker.lost_ownership:
                raise ValueError('Quota observation process ownership unavailable')
            for key, _ in selector.select(min(remaining, config['limits']['process_poll_ms'] / 1000)):
                if key.fileobj is process.stdin:
                    written = os.write(process.stdin.fileno(), pending)
                    pending = pending[written:]
                    if not pending:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                    continue
                data = os.read(key.fileobj.fileno(), READ_BYTES)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                size += len(data)
                if size > config['limits']['max_output_bytes']:
                    raise ValueError('Quota observation output limit exceeded')
                if key.fileobj is process.stdout:
                    output.extend(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise ValueError('Quota observation process failed')
        return (parser or _payload)(bytes(output))
    finally:
        selector.close()


def _run(argv, *, config, request, environment=None, cwd=None, parser=None):
    from integrations.mcp.native_process import ProcessTracker, process_identity
    process, tracker = None, None
    deadline = time.monotonic() + config['limits']['startup_timeout_s']
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, shell=False, env=environment, cwd=cwd)
        identity = process_identity(process.pid)
        if identity is None:
            raise ValueError('Quota child identity unavailable')
        tracker = ProcessTracker(identity)
        return _exchange(process, config, deadline, tracker, request=request, parser=parser)
    finally:
        if process is not None:
            contained = _reap(process, tracker, config['limits']['termination_grace_s'])
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
            if not contained:
                raise ValueError('Quota observation process containment could not be proven')


def _probe(lane, config, scope):
    wall, mono = time.time(), time.monotonic()
    try:
        value = _run([RELEASE_PYTHON, '-B', '-m', 'cortados_router.quota_observations', '--observe-lane', lane],
                     config=config, request=config, environment=_codex_environment.get() if lane == 'codex' else None)
        if value['auth_scope'].get('lane') != lane:
            raise ValueError('Quota observation source lane mismatch')
        return dict(value, lane=lane, observed_at=wall, observed_monotonic=mono)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return _failure(lane, scope, wall, mono, 'Quota observation source is unavailable')


def _usable(row, config, *, now=None, monotonic=None):
    now = time.time() if now is None else now
    monotonic = time.monotonic() if monotonic is None else monotonic
    age = policy._age(row, now, monotonic, policy._maximum(config))
    deadline = row.get('_valid_until_epoch')
    return (row.get('error') is None and age is not None and
            ('_valid_until_epoch' not in row or policy._number(deadline) and now + config['limits']['startup_timeout_s'] <= deadline))


def _cached(key, config, probe, failure):
    with _lock:
        row = _cache.get(key)
        if row is not None and _usable(row, config):
            return copy.deepcopy(row)
        _cache.pop(key, None)
        future = _inflight.get(key)
        leader = future is None
        if leader:
            future = concurrent.futures.Future()
            _inflight[key] = future
    if not leader:
        try:
            return copy.deepcopy(future.result(timeout=config['limits']['startup_timeout_s'] + 2 * config['limits']['termination_grace_s']))
        except concurrent.futures.TimeoutError:
            return failure()
    try:
        row = probe()
        with _lock:
            if _usable(row, config):
                _cache[key] = copy.deepcopy(row)
            future.set_result(row)
        return copy.deepcopy(row)
    except Exception:
        row = failure()
        future.set_result(row)
        return row
    finally:
        with _lock:
            _inflight.pop(key, None)


def _lane(lane, config, identity):
    if lane == 'cursor':
        try:
            actual = policy._configured_actual('cursor-cli', 'auto', config)
            entry = policy._same_actual(actual, config)
            row = collect_actual(entry, config=config)
            if policy._same_actual(actual, config) is None:
                raise ValueError('Source generation changed')
            return {k: v for k, v in dict(row, auth_scope=actual.public_scope).items() if not k.startswith('_')}
        except (ValueError, OSError):
            return _failure(lane, policy._unproven(lane), time.time(), time.monotonic(), 'Quota source metadata is unavailable')
    _, _, scope = _environment(lane, config)
    key = (lane, identity, json.dumps(scope, sort_keys=True))
    failure = lambda: _failure(lane, scope, time.time(), time.monotonic(), 'Quota observation source is unavailable')
    return _cached(key, config, lambda: _probe(lane, config, scope), failure)


def collect(lanes, *, config):
    if not isinstance(lanes, tuple) or any(not isinstance(lane, str) or lane not in LANES for lane in lanes):
        raise ValueError('Invalid observation lanes')
    identity = _configuration(config)
    requested = sorted(set(lanes))
    if not requested:
        return ()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(requested)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, _lane, lane, copy.deepcopy(config), identity) for lane in requested]
        return tuple(future.result() for future in futures)


def observe_lane(lane, config):
    """Child source entrypoint; never calls collect or a persistent sampling owner."""
    from . import scarcity
    from integrations.mcp.schemas import WorkerError
    env, executable, scope = _environment(lane, config)
    try:
        if lane == 'codex':
            from integrations.mcp.native_metadata import NativeMetadata
            from .worker_admission import validate_snapshot
            reader = NativeMetadata(config)
            reader.environment = env
            snapshot = reader.observe()
            record = validate_snapshot(snapshot, admission=False)
        elif lane == 'local':
            record = scarcity._local()
        else:
            if scope['kind'] != 'cli_store' or executable is None:
                raise WorkerError('quota_unknown', 'Quota CLI source is unproven')
            record = scarcity.SAMPLERS[lane](environment=env)
            record = scarcity.finish(lane, record)
        return dict(source=record.get('source') or 'quota-observation', auth_scope=scope, record=record, error=None)
    except (WorkerError, OSError, RuntimeError, ValueError) as exc:
        code = exc.code if isinstance(exc, WorkerError) and exc.code in ('quota_unknown', 'quota_spent') else 'quota_unknown'
        return dict(source='quota-observation', auth_scope=policy._unproven(lane), record=None,
                    error=dict(code=code, reason='Quota source metadata is unavailable'))


def _reset_after_fork():
    global _key_pid, _key_secret, _cache, _inflight, _lock
    _key_pid, _key_secret = os.getpid(), secrets.token_bytes(32)
    _cache, _inflight, _lock = {}, {}, threading.Lock()


os.register_at_fork(after_in_child=_reset_after_fork)


def invalidate_actual(entry, config):
    with _lock:
        _cache.pop(_actual_key(entry, config), None)


def _actual_key(entry, config):
    global _key_pid, _key_secret
    if _key_pid != os.getpid():
        _reset_after_fork()
    return ('actual', hmac.new(_key_secret, entry[-1].material, hashlib.sha256).digest(), _configuration(config))


def _actual_payload(output, nonce, profile, observed_at):
    from . import quota_sources, scarcity
    value = json.loads(output.decode('utf-8'))
    if (not output.endswith(b'\n') or set(value) != {'schema', 'nonce', 'profile', 'source', 'record', 'error', 'proof'} or
            value['schema'] != 'cortados.quota-source.response.v1' or value['nonce'] != nonce or value['profile'] != profile or
            value['source'] != 'cursor native getCurrentPeriodUsage' or value['error'] is not None or not isinstance(value['record'], dict)):
        raise ValueError('Invalid source response')
    proof = value['proof']
    if not isinstance(proof, dict) or set(proof) != {'valid_until_epoch'} or not policy._number(proof['valid_until_epoch']):
        raise ValueError('Invalid source proof')
    record = value['record']
    windows = []
    for w in record['windows']:
        used, reset, duration = (w[k] for k in ('used_pct', 'reset_epoch', 'window_s'))
        if not all(policy._number(v) for v in (used, reset, duration)) or not 0 <= used <= 100 or duration <= 0 or not observed_at < reset <= observed_at + duration:
            raise ValueError('Invalid source geometry')
        projected = scarcity.window_view(w['window'], used, reset, duration, now=observed_at)
        windows.append({**projected, 'used_pct': used, 'headroom_pct': 100 - used, 'pool': w['pool']})
    if not windows:
        raise ValueError('Missing source geometry')
    return {'source': value['source'], 'record': scarcity.finish('cursor', dict(record, windows=windows)),
            'error': None, '_valid_until_epoch': proof['valid_until_epoch']}


def collect_actual(entry, *, config):
    pid, provider, environment, executable, argv, cwd, capture = entry
    scope = policy._unproven(capture.lane)
    def failure():
        return _failure(capture.lane, scope, time.time(), time.monotonic(), 'Quota source metadata is unavailable')
    def probe():
        wall, mono, nonce = time.time(), time.monotonic(), secrets.token_hex(32)
        request = dict(schema='cortados.quota-source.request.v1', nonce=nonce, profile=capture.profile,
                       config=config, executable=executable, source_argv=capture.source_argv, cwd=cwd)
        value = _run([RELEASE_PYTHON, '-I', '-B', '-m', 'cortados_router.quota_observations', '--observe-actual'],
                     config=config, request=request, environment=dict(environment), cwd=cwd,
                     parser=lambda raw: _actual_payload(raw, nonce, capture.profile, wall))
        row = dict(value, lane=capture.lane, auth_scope=scope, observed_at=wall, observed_monotonic=mono)
        return row if _usable(row, config) else failure()
    return _cached(_actual_key(entry, config), config, probe, failure)


def status_record(lane, *, config, environment=None):
    from integrations.mcp.schemas import WorkerError
    try:
        if lane != 'cursor':
            raise ValueError()
        actual = policy._configured_actual('cursor-cli', 'auto', config, environment)
        entry = policy._same_actual(actual, config)
        row = collect_actual(entry, config=config)
        if policy._same_actual(actual, config) is None or not _usable(row, config):
            raise ValueError()
        outcome = policy._evaluate(dict(provider='cursor-cli', model='auto', auth_scope=actual.public_scope),
                   ({**row, 'auth_scope': actual.public_scope},), now=time.time(), monotonic=time.monotonic(),
                   max_age_s=policy._maximum(config), _native=True)
        if outcome['status'] not in ('available', 'spent'):
            raise ValueError()
        return copy.deepcopy(row['record'])
    except (ValueError, OSError, TypeError, KeyError):
        raise WorkerError('quota_unknown', 'Quota source metadata is unavailable') from None


def _actual_child(request):
    from . import quota_sources
    import re
    fields = {'schema', 'nonce', 'profile', 'config', 'executable', 'source_argv', 'cwd'}
    if set(request) != fields or request['schema'] != 'cortados.quota-source.request.v1' or not re.fullmatch('[0-9a-f]{64}', request['nonce']) or request['profile'] != quota_sources.PROFILE:
        raise ValueError('Invalid source request')
    _configuration(request['config'])
    value = dict(schema='cortados.quota-source.response.v1', nonce=request['nonce'], profile=request['profile'],
                 source='cursor native getCurrentPeriodUsage', record=None, error=None, proof=None)
    try:
        read = quota_sources.read_source(request['profile'], environment=dict(os.environ), executable=request['executable'],
                   source_argv=tuple(request['source_argv']), cwd=request['cwd'], config=request['config'])
        value.update(record=read.record, proof={'valid_until_epoch': read.valid_until_epoch})
    except Exception:
        value['error'] = {'code': 'quota_unknown', 'reason': 'Quota source metadata is unavailable'}
    return value


def main():
    request = json.load(sys.stdin)
    if sys.argv[1:] == ['--observe-actual']:
        result = _actual_child(request)
    elif len(sys.argv) == 3 and sys.argv[1] == '--observe-lane' and sys.argv[2] in LANES:
        _configuration(request)
        result = observe_lane(sys.argv[2], request)
    else:
        raise ValueError('Invalid observation command')
    print(json.dumps(result, allow_nan=False, ensure_ascii=False))


if __name__ == '__main__':
    main()
