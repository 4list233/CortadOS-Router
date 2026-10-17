# PURPOSE: Collect existing read-only quota sources in bounded isolated children.
# CONTRACT: RT-FRESH-QUOTA-005; one in-flight probe per source, no persistent writes.
# DEPENDS: NativeMetadata and existing per-lane scarcity samplers.
"""Only the parent timestamps source start; failed refresh discards old evidence."""
import concurrent.futures
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
RELEASE_PYTHON = '/Users/velox/.local/share/cortados-workers/releases/fresh-quota-v1-20261017/venv/bin/python'
LANES = frozenset(('claude', 'codex', 'antigravity', 'cursor', 'local'))
# Bytes per pipe read: syscall tradeoff only; configured combined output cap bounds allocation.
READ_BYTES = 65536
_lock = threading.Lock()
_cache = {}
_inflight = {}


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
    env = native_environment(config) if lane == 'codex' else scarcity._cli_env()
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


def _exchange(process, config, deadline, tracker):
    selector = selectors.DefaultSelector()
    pending = memoryview((json.dumps(config, ensure_ascii=False) + '\n').encode())
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
        return _payload(bytes(output))
    finally:
        selector.close()


def _probe(lane, config, scope):
    from integrations.mcp.native_process import ProcessTracker, process_identity
    wall, mono = time.time(), time.monotonic()
    limits = config['limits']
    process, tracker = None, None
    try:
        process = subprocess.Popen([RELEASE_PYTHON, '-B', '-m', 'cortados_router.quota_observations', '--observe-lane', lane],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, shell=False)
        identity = process_identity(process.pid)
        if identity is None:
            raise ValueError('Quota child identity unavailable')
        tracker = ProcessTracker(identity)
        value = _exchange(process, config, mono + limits['startup_timeout_s'], tracker)
        if value['auth_scope'].get('lane') != lane:
            raise ValueError('Quota observation source lane mismatch')
        result = dict(value, lane=lane, observed_at=wall, observed_monotonic=mono)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        result = _failure(lane, scope, wall, mono, 'Quota observation process failed, exceeded its deadline or returned invalid output')
    finally:
        if process is not None:
            if not _reap(process, tracker, limits['termination_grace_s']):
                result = _failure(lane, scope, wall, mono, 'Quota observation process containment could not be proven')
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
    return result


def _lane(lane, config, identity):
    _, _, scope = _environment(lane, config)
    key = (lane, identity, json.dumps(scope, sort_keys=True))
    now, mono = time.time(), time.monotonic()
    with _lock:
        row = _cache.get(key)
        if row is not None and policy._age(row, now, mono, min(config['limits']['metadata_max_age_s'], policy.MAX_AGE_S)) is not None:
            return copy.deepcopy(row)
        _cache.pop(key, None)
        future = _inflight.get(key)
        leader = future is None
        if leader:
            future = concurrent.futures.Future()
            _inflight[key] = future
    if not leader:
        try:
            return copy.deepcopy(future.result(timeout=config['limits']['startup_timeout_s'] + config['limits']['termination_grace_s']))
        except concurrent.futures.TimeoutError:
            return _failure(lane, scope, now, mono, 'Quota observation shared wait deadline expired')
    try:
        row = _probe(lane, config, scope)
        with _lock:
            if row['error'] is None and policy._age(row, time.time(), time.monotonic(), min(config['limits']['metadata_max_age_s'], policy.MAX_AGE_S)) is not None:
                _cache[key] = copy.deepcopy(row)
            future.set_result(row)
        return copy.deepcopy(row)
    finally:
        with _lock:
            _inflight.pop(key, None)


def collect(lanes, *, config):
    if not isinstance(lanes, tuple) or any(not isinstance(lane, str) or lane not in LANES for lane in lanes):
        raise ValueError('Invalid observation lanes')
    identity = _configuration(config)
    requested = sorted(set(lanes))
    if not requested:
        return ()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(requested)) as pool:
        futures = [pool.submit(_lane, lane, copy.deepcopy(config), identity) for lane in requested]
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


def main():
    if len(sys.argv) != 3 or sys.argv[1] != '--observe-lane' or sys.argv[2] not in LANES:
        raise ValueError('Invalid observation command')
    config = json.load(sys.stdin)
    _configuration(config)
    print(json.dumps(observe_lane(sys.argv[2], config), allow_nan=False, ensure_ascii=False))


if __name__ == '__main__':
    main()
