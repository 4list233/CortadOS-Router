#!/usr/bin/env python3
"""Install mandatory per-model-request routing at audited Hermes call sites."""
import argparse
from pathlib import Path

START = '    agent._reset_stream_delivery_tracking()\n'
ROUTED = '''    from agent.agent_model_routing import request_receipt, validate_request
    _required_route = request_receipt(agent, api_request_id)
''' + START
FINAL = '    _fire_pre_api_request_hook(\n'
CHECKED = '    validate_request(agent, api_kwargs, _required_route)\n\n' + FINAL
BUILD = '            _run_phase(build_api_request, agent, s)\n'
PREPARED = '''            from agent.agent_model_routing import prepare_routed_request
            routed = prepare_routed_request(agent, s)
            if routed is not None:
                if routed.action == "return":
                    return routed.result
                if routed.action == "break":
                    return None
                if routed.action == "continue":
                    s._retry.restart_with_rebuilt_messages = True
                    return None
''' + BUILD
ERROR = '        except Exception as api_error:\n'
ROUTE_ERROR = ERROR + '''            from agent.agent_model_routing import ModelRoutingError
            if isinstance(api_error, ModelRoutingError):
                from hermes_cli.local_execution import LocalExecutionFailure
                s.local_execution_failure = LocalExecutionFailure(
                    message=str(api_error), reason="model_route_unavailable",
                    retryable=True, origin="required_model_router")
                return None
'''


def apply(root, *, check=False):
    changes = {}
    for rel, old, new in [('agent/turn_api_request.py', START, ROUTED),
                          ('agent/turn_api_request.py', FINAL, CHECKED),
                          ('agent/conversation_loop.py', BUILD, PREPARED),
                          ('agent/conversation_loop.py', ERROR, ROUTE_ERROR)]:
        p = root / rel
        text = changes.get(p, p.read_text())
        if new in text:
            continue
        if text.count(old) != 1:
            raise RuntimeError(f'Inspect changed Hermes call site: {rel}')
        changes[p] = text.replace(old, new)
    p = root / 'hermes_cli/plugins.py'
    text = p.read_text()
    if '"route_agent_model"' not in text:
        old = '    "pre_gateway_dispatch",'
        if text.count(old) != 1:
            raise RuntimeError('Inspect changed Hermes hook registry')
        changes[p] = text.replace(old, old + ' "route_agent_model",')
    source = Path(__file__).with_name('required-routing') / 'agent_model_routing.py'
    target = root / 'agent/agent_model_routing.py'
    if not target.exists() or target.read_text() != source.read_text():
        changes[target] = source.read_text()
    for p, text in changes.items():
        compile(text, str(p), 'exec')
    if not check:
        for p, text in changes.items():
            p.write_text(text)
    return bool(changes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-root', type=Path, default=Path.home()/'.cortados/hermes/hermes-agent')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    changed = apply(args.hermes_root, check=args.check)
    print(('would patch' if args.check else 'patched') if changed else 'already patched')
