"""Required per-request routing. No dependency on a particular Router plugin."""
import json
import logging
import math
from contextvars import ContextVar
from contextlib import contextmanager

_ADMISSION = ContextVar('model_routing_admission', default=None)


@contextmanager
def admitted(receipt):
    token = _ADMISSION.set(receipt)
    try:
        yield
    finally:
        _ADMISSION.reset(token)


def current_receipt():
    return _ADMISSION.get()

logger = logging.getLogger(__name__)


class ModelRoutingError(RuntimeError):
    pass


def _configured_router(agent):
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly()
    return (cfg.get('agent') or {}).get('model_router')


def _size(messages, tools):
    return len(json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False,
                          default=str, separators=(',', ':')).encode('utf-8'))


def estimate_payload_tokens(payload):
    """Separate image token cost from encoded transport bytes, like host preflight."""
    from agent.model_metadata import estimate_messages_tokens_rough
    remainder = dict(payload)
    messages = remainder.pop('messages', None)
    if messages is None:
        messages = remainder.pop('input', None)
    if isinstance(messages, list):
        content_tokens = estimate_messages_tokens_rough(messages)
    else:
        content_tokens = len(json.dumps(messages, ensure_ascii=False, default=str).encode('utf-8')) / 4
    other_tokens = len(json.dumps(remainder, ensure_ascii=False, default=str,
                                 separators=(',', ':')).encode('utf-8')) / 4
    return math.ceil((content_tokens + other_tokens) * 1.2)


def route_request(agent, *, prompt, messages, tools, turn_id, api_request_id, approx_tokens=0):
    agent._cortados_route_receipt = None
    router = _configured_router(agent)
    if not router:
        return None
    from hermes_cli.plugins import invoke_hook
    size = _size(messages, tools)
    fit = {'assembled': True, 'known': True, 'input_bytes': size,
           'input_tokens': max(estimate_payload_tokens({'messages': messages, 'tools': tools}), int(approx_tokens or 0)),
           'source': 'assembled_messages_and_tool_schemas'}
    constraints = dict(getattr(agent, '_cortados_route_constraints', None) or {})
    if any(isinstance(m, dict) and isinstance(m.get('content'), list) and
           any(isinstance(p, dict) and p.get('type') in ('image_url', 'image', 'input_image')
               for p in m['content']) for m in messages):
        constraints['required_capabilities'] = list(set(constraints.get('required_capabilities') or []) | {'vision'})
    results = [r for r in invoke_hook('route_agent_model', router=router,
                   entrypoint='model-request', prompt=prompt,
                   session_id=getattr(agent, 'session_id', '') or '',
                   platform='subagent' if getattr(agent, 'is_subagent', False) else getattr(agent, 'platform', '') or '', turn_id=turn_id,
                   api_request_id=api_request_id, input_fit=fit,
                   current_provider=agent.provider, current_model=agent.model,
                   constraints=constraints)
                   if isinstance(r, dict) and r.get('router') == router]
    if len(results) != 1:
        raise ModelRoutingError('Required model router must return exactly one decision')
    result = results[0]
    if not result or result.get('error'):
        raise ModelRoutingError((result or {}).get('error') or 'Required model router returned no decision')
    provider, model = result.get('provider'), result.get('model')
    if not all(isinstance(v, str) and v for v in (provider, model)):
        raise ModelRoutingError('Required model router returned an incomplete route')
    # Static fallback chains cannot silently override required selection, even
    # after a provider error. A retry goes through routing again.
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._fallback_index = 0
    if (agent.provider, agent.model) != (provider, model):
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
        agent.switch_model(model, provider, api_key=runtime.get('api_key', ''),
                           base_url=runtime.get('base_url', ''), api_mode=runtime.get('api_mode', ''),
                           capabilities=runtime.get('runtime_capabilities'))
    if (agent.provider, agent.model) != (provider, model):
        raise ModelRoutingError('Host did not apply the required route')
    agent._cortados_route_receipt = result
    logger.info('Required route: request=%s selected=%s/%s route=%s degraded=%s',
                api_request_id, provider, model, result.get('route_id'), result.get('degraded'))
    return result


def request_receipt(agent, api_request_id):
    if not _configured_router(agent):
        return None
    receipt = getattr(agent, '_cortados_route_receipt', None)
    if not receipt or receipt.get('api_request_id') != api_request_id:
        raise ModelRoutingError('This model request has not passed required routing')
    return receipt


def prepare_routed_request(agent, state):
    """Select per attempt; a new model needs a fresh Context preparation/permit."""
    before = (agent.provider, agent.model)
    receipt = route_request(agent, prompt=state.original_user_message,
        messages=state.api_messages, tools=state.tools_for_api, turn_id=state.turn_id,
        api_request_id=state.api_request_id, approx_tokens=state.approx_tokens)
    if receipt is None:
        return None
    if before != (agent.provider, agent.model):
        from agent.conversation_loop import _run_phase, assemble_api_request, run_preflight_gate
        _run_phase(assemble_api_request, agent, state)
        verdict = _run_phase(run_preflight_gate, agent, state)
        if verdict.action != 'fallthrough':
            return verdict
    validate_request(agent, {'model': agent.model, 'messages': state.api_messages,
                             'tools': state.tools_for_api}, receipt)
    return None


def validate_request(agent, api_kwargs, receipt):
    """The final serialized request must still use the admitted route."""
    if receipt is None:
        return
    if (agent.provider, agent.model) != (receipt['provider'], receipt['model']):
        raise ModelRoutingError('Model changed after required routing')
    if api_kwargs.get('model') not in (None, receipt['model']):
        raise ModelRoutingError('Request middleware changed the routed model')
    size = len(json.dumps(api_kwargs, ensure_ascii=False, default=str,
                          separators=(',', ':')).encode('utf-8'))
    cap = receipt.get('context_length')
    if type(cap) is not int or cap <= 0 or estimate_payload_tokens(api_kwargs) > cap:
        raise ModelRoutingError('Final request exceeds the routed model window')
    if receipt['provider'] == 'agy-cli' and size > 180000:
        raise ModelRoutingError('Final request exceeds the Antigravity transport byte limit')
