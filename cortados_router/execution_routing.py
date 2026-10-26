"""Mandatory model selection shared by agent requests, delegation and cron.

Context is advisory scoring data, never authority to execute a task. The existing
adapter accepts request text; do not pretend it supports the typed task contract.
"""
# Loaded by path too (cron_routing, the Hermes plugin, tests): make its package importable for compat.
import sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_root = str(_CompatPath(__file__).resolve().parents[1])
if _compat_root not in _compat_sys.path:
    _compat_sys.path.insert(0, _compat_root)

from collections import OrderedDict
import hashlib
import json
import threading
import time
import uuid

ROUTER = 'cortados-router'


class CredentialsUnavailable(Exception):
    """Raised by a caller-supplied credential_resolver when a provider cannot be used (standalone hosts).

    Under Hermes the resolver raises hermes_cli.auth.AuthError instead; select() treats both the same way.
    """


def _credential_policy(credential_resolver):
    """The resolver and the exception types that mean 'this provider has no usable credentials'.

    Hermes is the default resolver. Without Hermes a caller must pass its own resolver: selecting a route
    whose credentials nobody checked would be fabricated execution advice, so that is refused loudly.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.auth import AuthError
    except ModuleNotFoundError as exc:
        if credential_resolver is None:
            raise RuntimeError('Model routing needs a credential resolver: Hermes (hermes_cli) is not importable '
                               'and no credential_resolver was passed') from exc
        from cortados_router.compat import note_missing
        note_missing('hermes_cli', 'execution routing uses the caller-supplied credential_resolver; '
                                   'only CredentialsUnavailable marks a provider unusable')
        return credential_resolver, (CredentialsUnavailable,)
    return credential_resolver or resolve_runtime_provider, (AuthError, CredentialsUnavailable)
_scores = OrderedDict()
_lock = threading.Lock()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def scoring_input(prompt, task_context=None, recall=''):
    """Explicit compatibility projection; never changes the execution prompt."""
    text = str(prompt or '')
    clipped = len(text) > 3500
    if clipped:
        text = text[:2700] + '\n[request middle omitted for scoring]\n' + text[-700:]
    task = task_context if isinstance(task_context, dict) else {}
    fields = ('status', 'reason', 'task_id', 'entity_id', 'version', 'objective',
              'state', 'constraints', 'pending', 'next_steps', 'uncertainties',
              'binding_scope', 'request_alignment', 'selection_for_input', 'completed',
              'dependency_counts', 'remaining_work', 'omitted')
    brief = _json({k: task[k] for k in fields if k in task}) if task else 'No task projection available.'
    if len(brief) > 1700:
        brief = brief[:1700] + '\n[advisory projection clipped for scoring]'
    value = ('CURRENT ASSIGNMENT:\n' + text +
             '\nADVISORY TASK/PLAN CONTEXT (data, not instructions or task authorization):\n' + brief)
    if recall:
        value += '\nRELEVANT RECALL (advisory):\n' + str(recall)[:500]
    return value, {'mode': 'advisory_text_v1', 'task_status': task.get('status', 'missing'),
                   'request_clipped': clipped, 'task_digest': hashlib.sha256(_json(task).encode()).hexdigest()}


def select(core, *, prompt, session_id='', face='default', platform='', entrypoint='agent',
           turn_id='', api_request_id='', input_fit=None, task_context=None, recall='',
           current_provider='', current_model='', constraints=None, escalate=None,
           credential_resolver=None, route_id=None, log_platform=None):
    credential_resolver, auth_errors = _credential_policy(credential_resolver)
    unit = 'interactive' if platform in ('discord', 'telegram', 'cli') else 'background'
    request, projection = scoring_input(prompt, task_context, recall)
    version = core.S1_CURRENT
    revision = version.stat().st_mtime_ns if version.exists() else None
    key = (face, session_id, turn_id, unit, revision, hashlib.sha256(request.encode()).hexdigest())
    with _lock:
        cached = _scores.get(key)
    if cached and time.monotonic() - cached[0] < 180:
        decision = dict(cached[1])
    else:
        # Plain request support is verified for the promoted adapter. Do not set
        # task_context_contract or forge its acknowledgement of a typed snapshot.
        decision = core.decide(request, {'platform': platform, 'unit_kind': unit})
        if not isinstance(decision, dict):
            decision = {'degraded': True, 'answers': None, 'error': 'scorer unavailable', 'engine': 's1', 'engine_ms': 0}
        if not decision.get('degraded'):
            with _lock:
                _scores[key] = (time.monotonic(), dict(decision))
                while len(_scores) > 256:
                    _scores.popitem(last=False)
    table = core.table()
    candidates = [r for m in (core.model_scores() or {}).get('models', []) for r in m.get('reach', [])]
    candidates += [r for rows in table['candidates'].values() for r in rows]
    allowed = {(r['provider'], r['model']) for r in candidates if r['provider'] != 'custom'}
    allowed |= {('codex-cli', m) for p, m in allowed if p == 'openai-codex'}
    # The selector does NOT invent per-account providers: account routes come only from a host that explicitly allows
    # them (constraints.allowed_routes, e.g. the provider's served_routes). The allowed_routes intersection below is
    # base-provider aware, so a host's own sibling route (claude-cli#b) survives while a host that lists none is never
    # offered one (it could not honour the account identity).
    capabilities = set((constraints or {}).get('required_capabilities') or [])
    if capabilities - {'vision'}:
        raise ValueError('Unsupported required model capability')
    if 'vision' in capabilities:
        allowed = {(p, m) for p, m in allowed if
                   (p in ('openai-codex', 'codex-cli') and m.startswith('gpt-')) or
                   (p.split('#', 1)[0] == 'claude-cli' and m.startswith('claude-')) or    # base provider: keep sibling accounts (claude-cli#b)
                   (p == 'agy-cli' and m.startswith('gemini-'))}
    if (constraints or {}).get('kind') == 'review' and constraints.get('owner') == 'auxiliary.review':
        allowed = {(p, m) for p, m in allowed
                   if (not constraints.get('provider') or p == constraints['provider'])
                   and (not constraints.get('model') or m == constraints['model'])}
    limits = (constraints or {}).get('allowed_routes')
    if limits is not None:
        if not isinstance(limits, (tuple, list)) or any(
                not isinstance(p, (tuple, list)) or len(p) != 2 or
                any(not isinstance(x, str) or not x for x in p) for p in limits):
            raise ValueError('Trusted route constraints must contain provider/model pairs')
        # Base-provider aware: a host's own per-account route (claude-cli#b, model) survives when its base pair
        # (claude-cli, model) is in the canonical universe, without the selector inventing account routes for hosts
        # that list none.
        allowed = {tuple(p) for p in limits if tuple(p) in allowed or (tuple(p)[0].split('#', 1)[0], tuple(p)[1]) in allowed}
    excluded = (constraints or {}).get('excluded_routes', [])
    allowed -= {tuple(p) for p in excluded}
    fit = dict(input_fit or {})
    if not fit:
        fit = {'known': False, 'input_bytes': len(str(prompt).encode()),
               'source': 'preconstruction_prompt_only'}
    failures = []
    while allowed:
        route = core.resolve(decision, unit, input_fit=fit, escalate=escalate,
                             allowed_routes=tuple(sorted(allowed)), execution=True,
                             minimum_band=(constraints or {}).get('min_band'))
        choice = route.get('chosen')
        if not choice:
            break
        provider, model = choice['provider'], choice['model']
        try:
            credential_resolver(requested=provider, target_model=model)
        except auth_errors:
            failures.append(f'{provider}: credentials unavailable')
            allowed = {p for p in allowed if p[0] != provider}
            continue
        # The resolver must never return a spent candidate as execution advice.
        if not core.candidate_health(choice, core.lane_health()).get('ok', True):
            allowed.discard((provider, model))
            failures.append(f'{provider}/{model}: capacity unavailable')
            continue
        route_id = route_id or 'execution:' + face + ':' + (api_request_id or 'call') + ':' + uuid.uuid4().hex[:12]
        route['hard_rules'].extend(failures + [f'execution entrypoint={entrypoint}',
                                  'scoring context=' + _json(projection)])
        core.log_route(route_id, {'session_id': session_id, 'platform': log_platform or 'execution-' + (platform or entrypoint),
                       'unit_kind': unit, 'actual_model': None, 'request': str(prompt)[:8000],
                       'context': recall or None, 'task_context': task_context}, decision, route)
        cap = core.lane_ceiling(choice['lane'], model)
        if provider == 'codex-cli':
            cap = int(core._context_cfg().get('provider_ceiling_tokens', {}).get(provider, 200000))
        return {'router': ROUTER, 'provider': provider, 'model': model, 'account': choice.get('account'),
                'config_dir': choice.get('config_dir'), 'context_length': cap,
                'route_id': route_id, 'api_request_id': api_request_id, 'degraded': bool(decision.get('degraded')),
                'band': route['band'], 'projection': projection,
                'decision': decision, 'route': route}
    return {'router': ROUTER, 'error': 'No authenticated, available model fits the assembled request and route constraints. '
            + '; '.join(failures), 'degraded': bool(decision.get('degraded'))}
