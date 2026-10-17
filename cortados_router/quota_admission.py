# PURPOSE: Own source-bound fresh quota admission and immutable selection inputs.
# CONTRACT: RT-FRESH-QUOTA-005; no credential reads or new scarcity arithmetic.
# DEPENDS: quota_observations, existing scarcity and SelectionResources owners.
"""Quota observations are evidence, never durable execution permission."""

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from . import scarcity
from .selection_resources import SelectionResources

# Seconds: Forest's fixed ceiling. Lower values reject useful observations;
# larger values could admit a dispatch against quota older than the agreed bound.
MAX_AGE_S = 30
PROVIDERS = {'codex-cli': 'codex', 'claude-cli': 'claude',
             'agy-cli': 'antigravity', 'cursor-cli': 'cursor'}
PREFIXES = {'codex': ('OPENAI_', 'CODEX_'), 'claude': ('ANTHROPIC_', 'CLAUDE_'),
            'cursor': ('CURSOR_', 'AGENT_CLI_'), 'antigravity': ('GOOGLE_', 'GEMINI_', 'ANTIGRAVITY_')}
SELECTORS = {'codex': ('OPENAI_API_KEY', 'OPENAI_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY', 'CODEX_AUTH_TOKEN'),
             'claude': ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN',
                        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'),
             'cursor': ('CURSOR_API_KEY', 'CURSOR_AUTH_TOKEN'),
             'antigravity': ('GOOGLE_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS')}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _unproven(lane):
    return {'kind': 'unproven', 'lane': lane, 'scope_id': None}


def _path(value):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise ValueError('Source path is not absolute')
    return str(Path(value).resolve())


def _source_scope(lane, environment, executable, config):
    """Private descriptor shared by validated observations and actual CLI dispatch."""
    try:
        for name, value in environment.items():
            if value and (name in SELECTORS[lane] or
                          (name.startswith(PREFIXES[lane]) and name.endswith(('_TOKEN', '_KEY', '_BASE_URL', '_AUTH')))):
                return _unproven(lane)
        home = _path(environment.get('HOME'))
        config_home = _path(environment.get('XDG_CONFIG_HOME') or str(Path(home) / '.config'))
        binary = _path(executable)
        store = {'codex': environment.get('CODEX_HOME') or str(Path(home) / '.codex'),
                 'claude': environment.get('CLAUDE_CONFIG_DIR') or str(Path(home) / '.claude'),
                 'cursor': config_home, 'antigravity': config_home}[lane]
        store = _path(store)
        if lane == 'codex' and (store != _path(config['codex_home']) or binary != _path(config['codex_bin'])):
            return _unproven(lane)
        if lane == 'cursor' and environment.get('AGENT_CLI_CREDENTIAL_STORE') != 'file':
            return _unproven(lane)
        descriptor = dict(lane=lane, executable=binary, home=home, config_home=config_home,
                          store=store, store_mode='file' if lane == 'cursor' else 'default', uid=os.getuid())
        digest = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        return {'kind': 'cli_store', 'lane': lane, 'scope_id': digest}
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        return _unproven(lane)


def auth_scope(provider, *, environment, executable, config, argv=()):
    lane = PROVIDERS.get(provider)
    if lane is None:
        return _unproven('local' if provider in ('custom', 'ollama') else None)
    if not isinstance(argv, (tuple, list)) or not all(isinstance(arg, str) for arg in argv):
        return _unproven(lane)
    if lane == 'codex' and (len(argv) < 2 or argv[1] != 'exec' or
                            not {'--strict-config', '--ignore-user-config'}.issubset(argv)):
        return _unproven(lane)
    return _source_scope(lane, environment, executable, config)


def _valid_scope(scope, lane, *, native=False):
    return (isinstance(scope, dict) and set(scope) == {'kind', 'lane', 'scope_id'} and
            scope['kind'] in (('cli_store', 'native_subscription') if native else ('cli_store',)) and scope['lane'] == lane and
            isinstance(scope['scope_id'], str) and re.fullmatch('[0-9a-f]{64}', scope['scope_id']) is not None)


def _age(row, now, monotonic, maximum):
    a, b = row.get('observed_at'), row.get('observed_monotonic')
    if not all(_number(v) for v in (a, b, now, monotonic)):
        return None
    deltas = (now - a, monotonic - b)
    return max(deltas) if all(0 <= delta <= maximum for delta in deltas) else None


def _result(lane, row=None, age=None, pool=None, status='unknown', reason='Quota observation unavailable'):
    return dict(status=status, kind='local_resource' if lane == 'local' else 'subscription' if lane else 'unknown',
                lane=lane, pool=pool, observed_at=row.get('observed_at') if row and _number(row.get('observed_at')) else None,
                age_s=age, source=row.get('source') if row else None, reason=reason)


def _evaluate(candidate, observations, *, now, monotonic, max_age_s, _native=False):
    lane = PROVIDERS.get(candidate.get('provider'))
    if lane is None:
        return _result('local' if candidate.get('provider') in ('ollama', 'custom') else None,
                       reason='Actual transport source is unproven')
    if candidate.get('lane', lane) != lane or not isinstance(candidate.get('model'), str) or not candidate['model'].strip():
        return _result(lane, reason='Candidate identity is invalid')
    matches = [r for r in observations if isinstance(r, dict) and r.get('lane') == lane]
    if len(matches) != 1:
        return _result(lane, reason='Missing or duplicate lane observation')
    row = matches[0]
    age = _age(row, now, monotonic, max_age_s)
    result = _result(lane, row, age)
    if age is None or not {'lane', 'source', 'auth_scope', 'observed_at', 'observed_monotonic', 'record', 'error'}.issubset(row):
        return result
    if not isinstance(row['source'], str) or not row['source'].strip():
        return result
    if not _valid_scope(candidate.get('auth_scope'), lane, native=_native) or candidate['auth_scope'] != row.get('auth_scope'):
        return {**result, 'reason': 'Actual authentication source is unproven or mismatched'}
    if row.get('error') is not None:
        error = row['error']
        return {**result, 'status': 'spent' if isinstance(error, dict) and error.get('code') == 'quota_spent' else 'unknown',
                'reason': 'Quota source reported unavailable usage'}
    record = row.get('record')
    if isinstance(record, dict) and lane == 'codex' and record.get('refusing') is True and record.get('windows') == []:
        return {**result, 'status': 'spent', 'reason': 'Included native usage is explicitly spent'}
    if not isinstance(record, dict) or not isinstance(record.get('windows'), list) or not record['windows']:
        return result
    windows = record['windows']
    for w in windows:
        if not isinstance(w, dict) or w.get('stale') is True or not all(isinstance(w.get(k), str) and w[k].strip() for k in ('window', 'pool')):
            return result
        used, duration, reset = (w.get(k) for k in ('used_pct', 'window_s', 'reset_epoch'))
        if not all(_number(v) for v in (used, duration, reset)) or not (0 <= used <= 100 and duration > 0 and now < reset <= row['observed_at'] + duration):
            return result
    pools = {w['pool'] for w in windows}
    selected = candidate.get('pool') or scarcity.pool_for(lane, candidate['model'], list(pools))
    required = {'all'} if lane in ('codex', 'claude') else {selected}
    if selected:
        required.add(selected)
    if not required.issubset(pools) or 'info' in required:
        return {**result, 'reason': 'Required quota pool is unavailable'}
    selected_windows = [w for w in windows if w['pool'] in required]
    if lane in ('cursor', 'antigravity'):
        spent = scarcity.finish(lane, copy.deepcopy(record))['pools'][selected]['spent']
    else:
        spent = record.get('refusing') is True or scarcity._roll(selected_windows)['spent']
    return {**result, 'pool': selected or 'all', 'status': 'spent' if spent else 'available',
            'reason': 'Included usage is spent' if spent else 'Known fresh configured-source quota'}


def resources(candidates, observations, cost_estimates=None, *, now, monotonic, max_age_s):
    if not _number(max_age_s) or not 0 < max_age_s <= MAX_AGE_S:
        raise ValueError('Invalid maximum observation age')
    rows, allowed, health = {}, [], {}
    for candidate in candidates:
        if any(not isinstance(candidate.get(k), str) or not candidate[k].strip() for k in ('lane', 'provider', 'model')):
            raise ValueError('Invalid quota resource candidate')
        lane = candidate['lane']
        health.setdefault(lane, {'ok': False, 'scarcity': None, 'pools': {}})
        outcome = _evaluate(candidate, observations, now=now, monotonic=monotonic, max_age_s=max_age_s, _native=True)
        if outcome['status'] == 'available':
            row = next(r for r in observations if r['lane'] == lane)
            record = scarcity.finish(lane, copy.deepcopy(row['record']))
            rows[lane] = record
            roll = scarcity._roll([w for w in record['windows'] if w['pool'] == 'all'])
            health[lane] = {**core_health(record['scarcity'], roll, record['refusing']), 'pools': {
                p: core_health(v['scarcity'], v, v['spent']) for p, v in record.get('pools', {}).items()}}
            pair = (candidate['provider'], candidate['model'])
            if pair not in allowed:
                allowed.append(pair)
    return SelectionResources.from_snapshot(rows, health, cost_estimates or {}, tuple(allowed))


def core_health(value, roll, refusing):
    from . import core
    return core._health_row(value, roll.get('tightest'), roll.get('expiring_pct', 0), refusing)


def _maximum(config):
    return min(config['limits']['metadata_max_age_s'], MAX_AGE_S)


def current_resources(candidates, *, config):
    from . import quota_observations
    lanes = tuple(sorted({PROVIDERS[c['provider']] for c in candidates if c.get('provider') in PROVIDERS}))
    observations = quota_observations.collect(lanes, config=config)
    projected = []
    for c in candidates:
        row = next((r for r in observations if r['lane'] == PROVIDERS.get(c.get('provider'))), None)
        projected.append({**c, 'auth_scope': row['auth_scope'] if row else _unproven(c.get('lane'))})
    costs = scarcity.unit_costs()
    now, monotonic = time.time(), time.monotonic()
    return resources(projected, observations, costs, now=now, monotonic=monotonic, max_age_s=_maximum(config))


def check(provider, model, *, auth_scope, config, observations=None):
    from . import quota_observations
    lane = PROVIDERS.get(provider)
    candidate = dict(provider=provider, model=model, auth_scope=auth_scope)
    now, monotonic = time.time(), time.monotonic()
    if isinstance(auth_scope, dict) and auth_scope.get('kind') == 'native_subscription':
        return _result(lane, reason='Actual source context is required')
    rows = tuple(observations or ())
    matching = [r for r in rows if isinstance(r, dict) and r.get('lane') == lane]
    if lane and (not matching or (len(matching) == 1 and _age(matching[0], now, monotonic, _maximum(config)) is None)):
        rows = quota_observations.collect((lane,), config=config)
        now, monotonic = time.time(), time.monotonic()
    return _evaluate(candidate, rows, now=now, monotonic=monotonic, max_age_s=_maximum(config))


def source_preflight(provider, model, *, config):
    from . import quota_observations
    lane = PROVIDERS.get(provider)
    if provider == 'cursor-cli':
        try:
            actual = _configured_actual(provider, model, config)
            return {'quota_stage': 'source_preflight', 'auth_scope': actual.public_scope,
                    'quota': check_actual(provider, model, actual=actual, config=config)}
        except (ValueError, OSError):
            return {'quota_stage': 'source_preflight', 'auth_scope': _unproven(lane), 'quota': _result(lane)}
    rows = quota_observations.collect((lane,), config=config) if lane else ()
    scope = rows[0]['auth_scope'] if len(rows) == 1 else _unproven(lane)
    return {'quota_stage': 'source_preflight', 'auth_scope': scope,
            'quota': check(provider, model, auth_scope=scope, config=config, observations=rows)}

# Context membership proves owner creation; it stores no quota authorization.
from dataclasses import dataclass
import secrets
import weakref

_contexts = weakref.WeakKeyDictionary()
os.register_at_fork(after_in_child=_contexts.clear)


@dataclass(frozen=True, eq=False, repr=False)
class ActualSource:
    _scope: tuple

    @property
    def public_scope(self):
        return dict(zip(('kind', 'lane', 'scope_id'), self._scope))

    def __repr__(self):
        return '<ActualSource redacted>'

    def __reduce_ex__(self, protocol):
        raise TypeError('Private source cannot be serialized')


def prepare_actual(provider, *, environment, executable, argv, cwd, config):
    from . import quota_sources
    try:
        env = dict(environment)
        args = tuple(argv)
        binary, folder = _path(executable), _path(cwd)
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()) or any(not isinstance(v, str) for v in args):
            raise ValueError()
        if provider == 'codex-cli':
            scope = auth_scope(provider, environment=env, executable=binary, argv=args, config=config)
            if not _valid_scope(scope, 'codex'):
                raise ValueError()
            capture = quota_sources.SourceCapture(profile=quota_sources.CODEX_PROFILE, lane='codex',
                       source_argv=(), files=(), material=json.dumps([scope, env], sort_keys=True).encode())
        else:
            capture = quota_sources.capture_source(provider, environment=env, executable=binary, argv=args, cwd=folder, config=config)
            scope = {'kind': 'native_subscription', 'lane': capture.lane, 'scope_id': secrets.token_hex(32)}
        actual = ActualSource(tuple(scope[k] for k in ('kind', 'lane', 'scope_id')))
        _contexts[actual] = (os.getpid(), provider, env, binary, args, folder, capture)
        return actual
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        raise ValueError('Actual source is unavailable') from None


def _same_actual(actual, config):
    from . import quota_sources
    entry = _contexts.get(actual)
    if not entry:
        return None
    if entry[0] != os.getpid():
        _contexts.pop(actual, None)
        return None
    pid, provider, env, executable, argv, cwd, capture = entry
    try:
        if capture.profile == quota_sources.CODEX_PROFILE:
            valid = auth_scope(provider, environment=env, executable=executable, argv=argv, config=config) == actual.public_scope
        else:
            latest = quota_sources.capture_source(provider, environment=dict(env), executable=executable, argv=argv, cwd=cwd, config=config)
            valid = latest == capture
    except (ValueError, OSError):
        valid = False
    if valid:
        return entry
    from . import quota_observations
    _contexts.pop(actual, None)
    quota_observations.invalidate_actual(entry, config)
    return None


def check_actual(provider, model, *, actual, config):
    from . import quota_observations, quota_sources
    lane = PROVIDERS.get(provider) if isinstance(provider, str) else None
    try:
        entry = _same_actual(actual, config) if isinstance(actual, ActualSource) else None
        if not entry or entry[1] != provider:
            return _result(lane, reason='Actual source context is unavailable')
        if entry[-1].profile == quota_sources.CODEX_PROFILE:
            token = quota_observations._codex_environment.set(entry[2])
            try:
                return check(provider, model, auth_scope=actual.public_scope, config=config)
            finally:
                quota_observations._codex_environment.reset(token)
        source_argv = entry[-1].source_argv
        if len(source_argv) != 2 or source_argv != ('--model', model):
            return _result(lane, reason='Actual model identity is unavailable')
        row = quota_observations.collect_actual(entry, config=config)
        if _same_actual(actual, config) is None:
            return _result(lane, reason='Actual source generation changed')
        row = {**row, 'auth_scope': actual.public_scope}
        now, monotonic = time.time(), time.monotonic()
        if not quota_observations._usable(row, config, now=now, monotonic=monotonic):
            return _result(lane, reason='Actual source proof is unavailable')
        return _evaluate(dict(provider=provider, model=model, auth_scope=actual.public_scope), (row,),
                         now=now, monotonic=monotonic, max_age_s=_maximum(config), _native=True)
    except (TypeError, ValueError, KeyError, OSError):
        return _result(lane, reason='Actual source context is unavailable')


def _configured_actual(provider, model, config, environment=None):
    from . import quota_observations
    lane = PROVIDERS[provider]
    if environment is None:
        env, executable, _ = quota_observations._environment(lane, config)
    else:
        import shutil
        env = dict(environment)
        executable = shutil.which('cursor-agent', path=env.get('PATH'))
    argv = (executable, '-p', '--output-format', 'stream-json', '--trust', '--model', model)
    return prepare_actual(provider, environment=env, executable=executable, argv=argv, cwd=os.getcwd(), config=config)
