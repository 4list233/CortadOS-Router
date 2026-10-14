"""Full AIAgent routing checks with fake providers and a ContextEngine stub.

Run with pytest in a Hermes-compatible Python environment. HERMES_TEST_ROOT may
point to a host checkout; the default is ~/.cortados/hermes/hermes-agent. Only a
temporary overlay is patched. Cases run in separate processes to prevent host
imports, fake plugins and the live-Context import guard from leaking to other
tests. No provider requests, live Context imports or runtime writes are allowed.
"""
import importlib.util as _guard_util
import unittest as _guard_unittest
if _guard_util.find_spec("pytest") is None or _guard_util.find_spec("hermes_cli") is None:
    raise _guard_unittest.SkipTest("Hermes integration test: needs pytest and an importable Hermes host (hermes_cli). "
                                   "Run with pytest inside the Hermes venv; see tests/integration/README.md")
import ast
from contextlib import contextmanager
import copy
import importlib.abc
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST = Path(os.environ.get("HERMES_TEST_ROOT", Path.home() / ".cortados/hermes/hermes-agent")).resolve()
HOST_TARGETS = (
    "agent/turn_api_request.py", "agent/conversation_loop.py", "hermes_cli/plugins.py",
    "tools/delegate_tool.py", "agent/review_engine.py", "agent/auxiliary_client.py",
    "cron/scheduler.py",
)
CASES = (
    "tool_round_routes_every_send_and_reassembles_after_switch",
    "missing_route_never_dispatches_and_returns_local_failure",
    "missing_second_route_preserves_completed_tool_without_another_send",
    "provider_retry_routes_and_renews_context_without_replaying_tool",
    "missing_final_admission_prevents_dispatch_after_successful_route",
)


@pytest.fixture(scope="module")
def staged_host(tmp_path_factory):
    if not (HOST / "run_agent.py").is_file():
        pytest.skip("Set HERMES_TEST_ROOT to a Hermes checkout to run full-loop integration")
    stage = tmp_path_factory.mktemp("required-routing-host")
    for relative in HOST_TARGETS:
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((HOST / relative).read_text())
    # These adapters share the hook registry and must coexist. No live host file
    # is handed to an installer for mutation.
    for name in ("cron-routing", "required-routing", "delegation-routing", "auxiliary-routing"):
        installer = runpy.run_path(str(ROOT / "integrations" / "hermes" / "patches" / (name + ".py")))
        installer["apply"](stage)
    return stage


@pytest.mark.parametrize("case", CASES)
def test_required_routing_full_loop(case, staged_host, tmp_path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HERMES_HOME=str(tmp_path / "home"),
               HERMES_IGNORE_USER_CONFIG="1", HERMES_TELEMETRY_ENABLED="false",
               CORTADOS_LIVE_WRITE="0", WANGCAI_LIVE_WRITE="0")
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), case,
         str(HOST), str(staged_host), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class NoLiveContext(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'context' or fullname.startswith('context.'):
            raise AssertionError('Live Context import forbidden in independent routing check: ' + fullname)


@contextmanager
def _rig(tmp_path, monkeypatch, host, stage):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('CORTADOS_LIVE_WRITE', '0')
    monkeypatch.setenv('WANGCAI_LIVE_WRITE', '0')
    monkeypatch.setenv('HERMES_TELEMETRY_ENABLED', 'false')
    (tmp_path / 'home').mkdir()
    monkeypatch.chdir(tmp_path)
    def no_network(*a, **kw):
        raise AssertionError('Unexpected network request during full-loop check')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(sys, 'meta_path', [NoLiveContext(), *sys.meta_path])
    monkeypatch.syspath_prepend(str(host))
    for package in ('agent', 'hermes_cli', 'tools', 'cron'):
        module = __import__(package)
        module.__path__.insert(0, str(stage / package))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(tmp_path / 'home')
    from hermes_cli import config, plugins, runtime_provider
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, '_delivery_manager', lambda: manager)
    monkeypatch.setattr(plugins, '_ensure_plugins_discovered', lambda: manager)
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: manager)
    monkeypatch.setattr(plugins, 'discover_plugins', lambda *a, **kw: None)
    cfg = {'model': {'provider': 'openrouter', 'model': 'initial-model'},
           'context': {'engine': 'independent-stub', 'compression_enabled': False},
           'agent': {'model_router': 'cortados-router'}, 'display': {'streaming': False},
           'auxiliary': {'title_generation': {'enabled': False}}}
    monkeypatch.setattr(config, 'load_config', lambda *a, **kw: cfg)
    monkeypatch.setattr(config, 'load_config_readonly', lambda *a, **kw: cfg)
    from agent.context_engine import ContextEngine
    from agent import model_metadata
    monkeypatch.setattr(model_metadata, 'get_model_context_length', lambda *a, **kw: 100000)
    import model_tools
    tools = [{'type': 'function', 'function': {'name': 'echo_test', 'description': 'Isolated echo',
              'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}}]
    monkeypatch.setattr(model_tools, 'get_tool_definitions', lambda *a, **kw: copy.deepcopy(tools))
    monkeypatch.setattr(model_tools, 'check_toolset_requirements', lambda *a, **kw: {})
    evidence = {'routes': [], 'assemblies': [], 'updates': [], 'sends': [], 'tools': [], 'permits': set()}
    state = {'missing': False, 'fail_first': False, 'missing_after': None, 'missing_admission': False}
    pending = {}
    # Compile the actual two admission functions without importing the plugin
    # module, whose unrelated imports would load the live Context package.
    router_source = ROOT / 'integrations/hermes/__init__.py'
    router_tree = ast.parse(router_source.read_text())
    admission_tree = ast.Module(body=[node for node in router_tree.body
        if isinstance(node, ast.FunctionDef) and node.name in
        {'_required_routing_enabled', '_execution_admission'}], type_ignores=[])
    admission_globals = {'_pending': pending, '_lock': threading.Lock()}
    exec(compile(admission_tree, str(router_source), 'exec'), admission_globals)
    manager._middleware['llm_execution'] = [admission_globals['_execution_admission']]
    class StubContext(ContextEngine):
        requires_selection_per_attempt = True
        model = 'unset'
        serial = 0
        @property
        def name(self): return 'independent-stub'
        def should_compress(self, prompt_tokens=None): return False
        def compress(self, messages, current_tokens=None): return messages
        def update_from_response(self, usage): pass
        def update_model(self, model, context_length, **kw):
            super().update_model(model, context_length, **kw)
            self.model = model
            evidence['updates'].append(model)
        def select_context(self, messages, **kw):
            self.serial += 1
            marker = f'CONTEXT-PERMIT:{self.model}:{self.serial}'
            evidence['assemblies'].append((self.model, marker, copy.deepcopy(messages)))
            selected = copy.deepcopy(messages)
            selected.insert(0, {'role': 'system', 'content': marker})
            self.permit = marker
            return selected
    engine = StubContext()
    import plugins.context_engine
    monkeypatch.setattr(plugins.context_engine, 'load_context_engine', lambda *a, **kw: engine)
    def route(**kw):
        evidence['routes'].append(copy.deepcopy(kw))
        if state['missing'] or state['missing_after'] is not None and len(evidence['routes']) > state['missing_after']:
            return None
        model = 'routed-model-A' if len(evidence['routes']) == 1 else 'routed-model-B'
        receipt = {'router': 'cortados-router', 'provider': 'openrouter', 'model': model,
                'context_length': 100000, 'route_id': 'route-' + str(len(evidence['routes'])),
                'api_request_id': kw['api_request_id']}
        if not state['missing_admission']:
            pending[kw['turn_id']] = {'execution_route': receipt}
        return receipt
    manager._hooks['route_agent_model'] = [route]
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', lambda **kw:
        {'provider': kw['requested'], 'model': kw['target_model'], 'api_key': 'fake-key',
         'base_url': 'https://test.invalid/v1', 'api_mode': 'chat_completions'})
    from agent import credential_pool
    monkeypatch.setattr(credential_pool, 'load_pool', lambda *a, **kw: None)
    from openai.types.chat import ChatCompletion
    def create(**kw):
        from agent.agent_model_routing import current_receipt
        admitted_receipt = current_receipt()
        assert admitted_receipt is not None, 'Provider dispatch lacks admission scope'
        assert admitted_receipt['api_request_id'] == evidence['routes'][-1]['api_request_id']
        assert admitted_receipt['model'] == kw['model']
        evidence['sends'].append(copy.deepcopy(kw))
        assert engine.model == kw['model'], 'Context model differs from sent model'
        assert engine.permit in json.dumps(kw['messages']), 'Missing renewed Context preparation'
        assert engine.permit not in evidence['permits'], 'Context permit reused'
        evidence['permits'].add(engine.permit)
        assert len(evidence['routes']) == len(evidence['sends']), 'A provider attempt bypassed routing'
        if state['fail_first'] and len(evidence['sends']) == 1:
            from openai import APIConnectionError
            import httpx
            raise APIConnectionError(request=httpx.Request('POST', 'https://test.invalid'))
        if len(evidence['sends']) == 1:
            choice = {'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                      'tool_calls': [{'id': 'echo-1', 'type': 'function', 'function': {'name': 'echo_test', 'arguments': '{"value":"evidence"}'}}]}}
        else:
            choice = {'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'done'}}
        return ChatCompletion(id='fake', object='chat.completion', created=0, model=kw['model'], choices=[choice])
    class Client:
        base_url = 'https://test.invalid/v1'
        api_key = 'fake-key'
        def __init__(self, *a, **kw): self.chat = NS(completions=NS(create=create))
        def close(self): pass
        def with_options(self, **kw): return self
    import agent.process_bootstrap
    monkeypatch.setattr(agent.process_bootstrap, 'OpenAI', Client)
    from run_agent import AIAgent
    monkeypatch.setattr(AIAgent, '_create_openai_client', lambda self, *a, **kw: Client())
    target = AIAgent(model='initial-model', provider='openrouter', api_key='fake-key', base_url='https://test.invalid/v1',
                     quiet_mode=True, skip_context_files=True, skip_memory=True, max_iterations=5, platform='cli')
    target._disable_streaming = True
    target._use_prompt_caching = False
    target._cached_system_prompt = 'Independent full-loop test.'
    target.save_trajectories = False
    target.tool_delay = 0
    target.compression_enabled = False
    monkeypatch.setattr(target, '_persist_session', lambda *a, **kw: None)
    monkeypatch.setattr(target, '_save_trajectory', lambda *a, **kw: None)
    monkeypatch.setattr(target, '_cleanup_task_resources', lambda *a, **kw: None)
    def execute(name, args, *a, **kw):
        evidence['tools'].append((name, copy.deepcopy(args)))
        return json.dumps({'echo': args['value']})
    monkeypatch.setattr(model_tools, 'handle_function_call', execute)
    from agent import conversation_loop, turn_api_request
    assert Path(conversation_loop.__file__).is_relative_to(stage)
    assert Path(turn_api_request.__file__).is_relative_to(stage)
    try:
        yield NS(agent=target, engine=engine, evidence=evidence, state=state)
    finally:
        from agent.agent_model_routing import current_receipt
        assert current_receipt() is None, 'Admission scope leaked beyond provider dispatch'
        target.close()
        reset_hermes_home_override(token)
        assert not any(name == 'context' or name.startswith('context.') for name in sys.modules)


def _check_tool_round_routes_every_send_and_reassembles_after_switch(rig, monkeypatch):
    result = rig.agent.run_conversation('Use echo_test once, then report done.', conversation_history=[])
    e = rig.evidence
    assert result.get('final_response') == 'done', (result, e)
    assert [call['model'] for call in e['sends']] == ['routed-model-A', 'routed-model-B']
    assert len(e['routes']) == 2 and e['tools'] == [('echo_test', {'value': 'evidence'})]
    assert [row[0] for row in e['assemblies']] == ['initial-model', 'routed-model-A', 'routed-model-A', 'routed-model-B']
    assert len(e['permits']) == 2


def _check_missing_route_never_dispatches_and_returns_local_failure(rig, monkeypatch):
    rig.state['missing'] = True
    result = rig.agent.run_conversation('Report a result.', conversation_history=[])
    assert not rig.evidence['sends'] and not rig.evidence['tools'], result
    assert result.get('failed') is True, result
    assert result.get('failure_reason') == 'local_execution_blocked', result
    assert result['local_execution_failure'] == {'reason': 'model_route_unavailable', 'origin': 'required_model_router',
                                                'provider_called_for_attempt': False}, result


def _check_missing_second_route_preserves_completed_tool_without_another_send(rig, monkeypatch):
    rig.state['missing_after'] = 1
    result = rig.agent.run_conversation('Use echo_test then finish.', conversation_history=[])
    assert len(rig.evidence['sends']) == 1 and len(rig.evidence['routes']) == 2
    assert rig.evidence['tools'] == [('echo_test', {'value': 'evidence'})]
    assert result['local_execution_failure']['reason'] == 'model_route_unavailable'
    assert any(row.get('role') == 'tool' and 'evidence' in row.get('content', '') for row in result['messages'])


def _check_provider_retry_routes_and_renews_context_without_replaying_tool(rig, monkeypatch):
    rig.state['fail_first'] = True
    # Eliminate backoff only; retain the actual error classifier and retry loop.
    import time
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    result = rig.agent.run_conversation('Report done.', conversation_history=[])
    assert result.get('final_response') == 'done', (result, rig.evidence)
    assert len(rig.evidence['routes']) == len(rig.evidence['sends']) == 2
    assert len(rig.evidence['permits']) == 2 and not rig.evidence['tools']


def _check_missing_final_admission_prevents_dispatch_after_successful_route(rig, monkeypatch):
    rig.state['missing_admission'] = True
    result = rig.agent.run_conversation('Report a result.', conversation_history=[])
    assert len(rig.evidence['routes']) == 1
    assert not rig.evidence['sends'] and not rig.evidence['tools'], result
    assert result['local_execution_failure']['origin'] == 'cortados_router'
    assert result['local_execution_failure']['reason'] == 'model_route_unavailable'


if __name__ == "__main__":
    case, host, stage, scratch = sys.argv[1:]
    if case not in CASES:
        raise ValueError("Unknown full-loop scenario: " + case)
    with pytest.MonkeyPatch.context() as patch:
        with _rig(Path(scratch), patch, Path(host), Path(stage)) as scenario:
            globals()["_check_" + case](scenario, patch)
