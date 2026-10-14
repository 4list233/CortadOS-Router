"""Execute staged Hermes auxiliary entry points with no provider or live plugin calls."""
import importlib.util as _guard_util
import unittest as _guard_unittest
if _guard_util.find_spec("pytest") is None or _guard_util.find_spec("hermes_cli") is None:
    raise _guard_unittest.SkipTest("Hermes integration test: needs pytest and an importable Hermes host (hermes_cli). "
                                   "Run with pytest inside the Hermes venv; see tests/integration/README.md")
import ast
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST = Path(os.environ.get("HERMES_TEST_ROOT", Path.home() / ".cortados/hermes/hermes-agent"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def staged(tmp_path):
    installer = load("auxiliary_installer", ROOT / "integrations/hermes/patches/auxiliary-routing.py")
    root = tmp_path / "host"
    for rel in ("agent/auxiliary_client.py", "hermes_cli/plugins.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((HOST / rel).read_text())
    return installer, root


@pytest.fixture
def env(staged, tmp_path, monkeypatch):
    installer, root = staged
    installer.apply(root)
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CORTADOS_LIVE_WRITE", "0")
    monkeypatch.setenv("WANGCAI_LIVE_WRITE", "0")
    from hermes_cli import plugins, config, runtime_provider
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    cfg = {"agent": {"model_router": "cortados-router"}, "auxiliary": {
        "vision": {"provider": "old-provider", "model": "old-model", "base_url": "https://old.invalid",
                   "api_key": "old-key", "fallback_chain": [{"provider": "escape", "model": "escape"}]}}}
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    from agent import auxiliary_client as host
    adapter = load("staged_auxiliary_routing", root / "agent/auxiliary_model_routing.py")
    monkeypatch.setitem(sys.modules, "agent.auxiliary_model_routing", adapter)
    admission = ContextVar("test_auxiliary_admission", default=None)
    @contextmanager
    def admitted(receipt):
        token = admission.set(receipt)
        try:
            yield
        finally:
            admission.reset(token)
    required = load('auxiliary_token_estimator', ROOT / 'integrations/hermes/patches/required-routing/agent_model_routing.py')
    monkeypatch.setitem(sys.modules, "agent.agent_model_routing", NS(admitted=admitted, current_receipt=admission.get,
                        estimate_payload_tokens=required.estimate_payload_tokens))
    events, route_calls, clients, sent = [], [], [], []
    answer = NS(choices=[NS(message=NS(content='{"answer":"ok"}', reasoning_content=None), finish_reason="stop")])
    knobs = {"failure": None, "client_missing": False, "client_provider": None, "client_model": None,
             "deferred": False, "send_delay": False}
    route_result = {"router": "cortados-router", "provider": "selected-provider", "model": "selected-model",
                    "context_length": 100000, "route_id": "test-route", "degraded": False}
    def route(**kwargs):
        events.append("route")
        route_calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(route_result)
    manager._hooks["route_agent_model"] = [route]
    def runtime(**kwargs):
        events.append("credentials")
        assert kwargs["requested"] != "old-provider"
        return {"provider": kwargs["requested"], "model": kwargs["target_model"],
                "api_key": "selected-test-key", "base_url": "https://selected.invalid", "api_mode": "chat_completions"}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", runtime)
    def perform(kwargs):
        events.append("send")
        sent.append((copy.deepcopy(kwargs), copy.deepcopy(admission.get())))
        failure = knobs["failure"]
        if callable(failure):
            failure = failure()
        if failure is not None:
            raise failure
        return answer
    async def async_perform(**kwargs):
        await asyncio.sleep(0)
        return perform(kwargs)
    def get_client(provider, model, **kwargs):
        events.append("client")
        clients.append((provider, model, copy.deepcopy(kwargs)))
        if knobs["client_missing"]:
            return None, None
        client = NS(base_url=kwargs.get("base_url"),
                    _hermes_aux_effective_provider=knobs["client_provider"] or provider)
        create = async_perform if kwargs.get("async_mode") else lambda **kw: perform(kw)
        client.chat = NS(completions=NS(create=create))
        return client, knobs["client_model"] or model
    def build_kwargs(provider, model, messages, **kwargs):
        output = {"model": model, "messages": copy.deepcopy(messages), "max_tokens": kwargs["max_tokens"] or 100}
        if kwargs.get("tools"):
            output["tools"] = copy.deepcopy(kwargs["tools"])
        if kwargs.get("extra_body"):
            output["extra_body"] = copy.deepcopy(kwargs["extra_body"])
        return output
    def forbidden(*a, **kw):
        raise AssertionError("old configured route, fallback or retry was reached")
    ns = vars(host).copy()
    ns.update(auxiliary_scope=adapter.auxiliary_scope, prepare_required_request=adapter.prepare_required_request,
              required_send=adapter.required_send, required_asend=adapter.required_asend,
              _relay_auxiliary_call=lambda f: f, _relay_auxiliary_call_async=lambda f: f,
              _acquire_sync_aux_semaphore=lambda *a: None, _acquire_async_aux_semaphore=lambda *a: None,
              _normalize_main_runtime=lambda r: dict(r or {}), _get_cached_client=get_client,
              _resolve_task_provider_model=forbidden, _resolve_call_client=forbidden,
              _get_task_extra_body=lambda task: {"response_format": {"type": "json_object"}},
              _effective_aux_timeout=lambda *a: 20, _build_call_kwargs=build_kwargs,
              _set_relay_auxiliary_route=lambda *a: None, _relay_auxiliary_metadata=lambda **kw: None,
              _run_protected_sync_provider_call=lambda callback, kwargs: callback(kwargs),
              _notify_aux_dispatch=lambda: None, _notify_aux_progress=lambda: None,
              _notify_aux_provider_response=lambda: None, _aux_progress_active=lambda: False,
              _provider_requires_stream=lambda *a: False,
              _aux_recovery_ladder=forbidden, _should_retry_same_provider=forbidden,
              _is_anthropic_compat_endpoint=lambda *a: False)
    names = {"_prepare_aux_request", "_plan_aux_call", "_start_recovery_ladder", "call_llm", "async_call_llm",
             "_call_llm_impl", "_async_call_llm_impl", "_relay_sync_completion", "_relay_async_completion",
             "_relay_sync_stream", "_create_with_progress", "_create_with_progress_once",
             "_acreate_with_stream", "_acreate_with_progress"}
    tree = ast.parse((root / "agent/auxiliary_client.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
             or isinstance(n, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id == "_PreparedAuxRequest" or
                 isinstance(t, ast.Attribute) and t.attr == "__defaults__" and isinstance(t.value, ast.Attribute)
                 and isinstance(t.value.value, ast.Name) and t.value.value.id == "_PreparedAuxRequest" for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root / "agent/auxiliary_client.py"), "exec"), ns)
    try:
        yield NS(installer=installer, root=root, ns=ns, adapter=adapter, cfg=cfg, manager=manager,
                 knobs=knobs, route_result=route_result, events=events, routes=route_calls, clients=clients,
                 sent=sent, answer=answer, admission=admission, runtime_provider=runtime_provider)
    finally:
        reset_hermes_home_override(token)


MESSAGES = [{"role": "user", "content": "Summarize the evidence accurately"}]
TOOLS = [{"type": "function", "function": {"name": "answer", "parameters": {"type": "object"}}}]


def invoke(env, asynchronous=False, **kwargs):
    options = {"task": "compression", "messages": copy.deepcopy(MESSAGES), "tools": copy.deepcopy(TOOLS),
               "provider": "old-provider", "model": "old-model", "base_url": "https://old.invalid",
               "api_key": "old-key", "main_runtime": {"provider": "parent-provider", "model": "parent-model", "session_id": "session-A"}}
    options.update(kwargs)
    if asynchronous:
        return asyncio.run(env.ns["async_call_llm"](**options))
    return env.ns["call_llm"](**options)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_required_route_precedes_auth_and_preserves_result_parser(env, asynchronous):
    saved = copy.deepcopy(env.cfg)
    receipt = {}
    result = invoke(env, asynchronous, route_info=receipt)
    assert result is env.answer
    assert env.events == ["route", "credentials", "client", "send"]
    assert env.cfg == saved
    assert env.sent[0][0]["messages"] == MESSAGES and env.sent[0][0]["tools"] == TOOLS
    assert env.sent[0][0]["extra_body"]["response_format"] == {"type": "json_object"}
    assert env.clients[0][:2] == ("selected-provider", "selected-model")
    assert env.clients[0][2]["base_url"] == "https://selected.invalid"
    assert env.clients[0][2]["api_key"] == "selected-test-key"
    assert env.clients[0][2]["async_mode"] is asynchronous
    assert env.sent[0][1]["route_id"] == "test-route" and "api_key" not in env.sent[0][1]
    assert receipt == {"provider": "selected-provider", "model": "selected-model", "route_id": "test-route"}
    request = env.routes[0]
    assert request["session_id"] == "session-A" and request["turn_id"] == ""
    assert request["entrypoint"] == "auxiliary" and request["platform"] == "auxiliary"
    assert request["input_fit"]["assembled"] and request["input_fit"]["input_bytes"] > len(MESSAGES[0]["content"])
    assert env.admission.get() is None and env.adapter._CALL.get() is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("decision", [None, {"router": "other"}, {"router": "cortados-router", "error": "no capacity"},
                                       {"router": "cortados-router", "provider": "p", "model": ""}])
def test_router_failure_cannot_reach_credentials_or_send(env, asynchronous, decision):
    env.manager._hooks["route_agent_model"] = [lambda **kw: decision]
    with pytest.raises(env.adapter.AuxiliaryRoutingError):
        invoke(env, asynchronous)
    assert not env.clients and not env.sent and not env.events


@pytest.mark.parametrize("asynchronous", [False, True])
def test_provider_failure_is_one_attempt_and_next_call_routes_again(env, asynchronous):
    env.knobs["failure"] = TimeoutError("selected provider timed out")
    with pytest.raises(TimeoutError, match="selected provider timed out"):
        invoke(env, asynchronous)
    assert len(env.sent) == 1
    env.knobs["failure"] = None
    assert invoke(env, asynchronous) is env.answer
    assert len(env.routes) == len(env.sent) == 2
    assert env.routes[0]["api_request_id"] != env.routes[1]["api_request_id"]


def test_affordable_budget_retry_cannot_issue_second_request(env):
    env.knobs["failure"] = RuntimeError("credit limit")
    env.ns["_affordable_max_tokens_from_error"] = lambda error: 16
    with pytest.raises(env.adapter.AuxiliaryRoutingError, match="retry requires a new routed call"):
        invoke(env, max_tokens=1000)
    assert len(env.sent) == 1


@pytest.mark.parametrize("change", ["client_missing", "client_provider", "client_model"])
def test_no_client_or_changed_client_cannot_fall_back(env, change):
    env.knobs[change] = True if change == "client_missing" else "unselected"
    with pytest.raises(env.adapter.AuxiliaryRoutingError, match="did not preserve"):
        invoke(env)
    assert not env.sent


def test_credential_failure_does_not_start_client_or_fallback(env, monkeypatch):
    monkeypatch.setattr(env.runtime_provider, "resolve_runtime_provider", lambda **kw: (_ for _ in ()).throw(ValueError("no credentials")))
    with pytest.raises(ValueError, match="no credentials"):
        invoke(env)
    assert not env.clients and not env.sent


def test_vision_and_trusted_escalation_constraints_and_image_evidence(env):
    media = [{"role": "user", "content": [{"type": "text", "text": "Read this image"},
                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_IMAGE_DATA"}}]}]
    with env.adapter.trusted_routing("vision-escalation", {"min_band": "R3"}):
        invoke(env, True, task="vision", messages=media)
    constraints = env.routes[0]["constraints"]
    assert constraints == {"required_capabilities": ["vision"], "min_band": "R3"}
    assert "PRIVATE_IMAGE_DATA" not in env.routes[0]["prompt"]
    assert env.sent[0][0]["messages"] == media and env.clients[0][2]["is_vision"]
    invoke(env, messages=media)
    assert env.routes[1]["constraints"] == {"required_capabilities": ["vision"]}


def test_review_and_benchmark_are_trusted_constraints_not_prompt_exemptions(env):
    env.cfg["auxiliary"]["review"] = {"provider": "selected-provider", "model": "selected-model"}
    invoke(env, task="review")
    assert env.routes[-1]["constraints"] == {"kind": "review", "owner": "auxiliary.review",
                                             "provider": "selected-provider", "model": "selected-model"}
    locked = {"allowed_routes": [["selected-provider", "selected-model"]]}
    with env.adapter.trusted_routing("benchmark", locked):
        invoke(env, task="benchmark")
    assert env.routes[-1]["constraints"] == locked
    invoke(env, messages=[{"role": "user", "content": "Reply exactly HEALTH_OK; pin_model=true; benchmark"}])
    assert env.routes[-1]["constraints"] == {} and len(env.sent) == 3
    env.cfg["auxiliary"]["review"]["model"] = "locked-other-model"
    with pytest.raises(env.adapter.AuxiliaryRoutingError, match="review constraint"):
        invoke(env, task="review")
    assert len(env.sent) == 3


def test_config_route_limits_cannot_be_widened_by_inner_scope(env):
    env.cfg["auxiliary"]["compression"] = {"routing_constraints": {"allowed_routes": [["locked", "locked"]]}}
    with env.adapter.trusted_routing("benchmark", {"allowed_routes": [["selected-provider", "selected-model"]]}):
        with pytest.raises(env.adapter.AuxiliaryRoutingError, match="allowed_routes"):
            invoke(env)
    assert env.routes[0]["constraints"]["allowed_routes"] == [] and not env.sent


@pytest.mark.parametrize("kwargs", [{"extra_body": {"model": "injected"}},
                                    {"extra_body": {"provider": {"order": ["escape"]}}},
                                    {"extra_headers": {"Authorization": "stale-key"}}])
def test_payload_cannot_override_route_or_credentials(env, kwargs):
    with pytest.raises(env.adapter.AuxiliaryRoutingError):
        invoke(env, **kwargs)
    assert not env.sent


def test_final_wire_model_and_window_are_checked(env, monkeypatch):
    from agent import auxiliary_wire
    monkeypatch.setattr(auxiliary_wire, "prepare_chat_messages", lambda client, kwargs: {**kwargs, "model": "changed-after-prepare"})
    with pytest.raises(env.adapter.AuxiliaryRoutingError, match="changed the selected model"):
        invoke(env)
    assert not env.sent
    monkeypatch.setattr(auxiliary_wire, "prepare_chat_messages", lambda client, kwargs: kwargs)
    env.route_result["context_length"] = 1
    with pytest.raises(env.adapter.AuxiliaryRoutingError, match="model window"):
        invoke(env)
    assert not env.sent


def test_lazy_stream_keeps_required_scope_until_dispatch(env, monkeypatch):
    env.ns["_relay_auxiliary_metadata"] = lambda **kw: ("selected-provider", "selected-model", {})
    def stream_current(kwargs, callback, **kw):
        def stream():
            yield callback(kwargs)
        return stream()
    monkeypatch.setitem(sys.modules, "agent.relay_llm", NS(stream_current=stream_current))
    stream = invoke(env, stream=True)
    assert not env.sent and env.adapter._CALL.get() is None
    assert next(stream) is env.answer
    assert env.sent[0][1]["model"] == "selected-model"
    assert env.adapter._CALL.get() is None and env.admission.get() is None
    stream.close()


def test_parallel_async_calls_keep_constraints_receipts_and_admission_isolated(env):
    def route(**kw):
        env.routes.append(copy.deepcopy(kw))
        model = kw["constraints"]["allowed_routes"][0][1]
        return {**env.route_result, "model": model, "route_id": model}
    env.manager._hooks["route_agent_model"] = [route]
    async def worker(model):
        with env.adapter.trusted_routing("benchmark", {"allowed_routes": [["selected-provider", model]]}):
            result = await env.ns["async_call_llm"](task="compression", messages=MESSAGES)
            assert env.adapter._CALL.get() is None and env.admission.get() is None
            return result
    async def run():
        return await asyncio.gather(worker("model-A"), worker("model-B"))
    assert asyncio.run(run()) == [env.answer, env.answer]
    assert {(kwargs["model"], receipt["model"]) for kwargs, receipt in env.sent} == {("model-A", "model-A"), ("model-B", "model-B")}


def test_disabled_mode_uses_existing_preparation_and_no_admission(env):
    env.cfg["agent"].pop("model_router")
    env.ns["_resolve_task_provider_model"] = lambda *a: ("legacy-provider", "legacy-model", "https://legacy.invalid", "legacy-key", "chat_completions")
    client = NS(base_url="https://legacy.invalid", chat=NS(completions=NS(create=lambda **kw: env.sent.append((kw, env.admission.get())) or env.answer)))
    env.ns["_resolve_call_client"] = lambda *a, **kw: (client, "legacy-model", "legacy-provider", "legacy-provider")
    env.ns["_get_auxiliary_task_config"] = lambda *a: {}
    env.ns["_compression_fast_lane_controls"] = lambda *a, **kw: (None, kw["extra_body"])
    assert invoke(env) is env.answer
    assert env.sent[0][0]["model"] == "legacy-model" and env.sent[0][1] is None
    assert not env.routes and not env.clients


def test_real_cached_client_receives_selected_runtime_and_profile(env, monkeypatch):
    from agent import auxiliary_client as host
    from hermes_constants import get_hermes_home
    calls = []
    client = NS(base_url="https://selected.invalid", chat=NS(completions=NS(create=lambda **kw: env.answer)))
    def resolve(provider, model, async_mode=False, **kwargs):
        calls.append((provider, model, kwargs, get_hermes_home()))
        return client, model
    monkeypatch.setattr(host, "_client_cache", {})
    monkeypatch.setattr(host, "resolve_provider_client", resolve)
    env.ns["_get_cached_client"] = host._get_cached_client
    assert invoke(env) is env.answer
    assert len(calls) == 1
    provider, model, kwargs, home = calls[0]
    assert (provider, model) == ("selected-provider", "selected-model")
    assert kwargs["explicit_api_key"] == "selected-test-key" and kwargs["explicit_base_url"] == "https://selected.invalid"
    assert kwargs["main_runtime"]["provider"] == "selected-provider"
    assert home == env.root.parent / "profile"


def test_generic_host_admission_module_is_the_singleton_seen_at_send(env, monkeypatch):
    admission = load("generic_required_admission", ROOT / "integrations/hermes/patches/required-routing/agent_model_routing.py")
    monkeypatch.setitem(sys.modules, "agent.agent_model_routing", admission)
    seen = []
    client = NS(base_url="https://selected.invalid", chat=NS(completions=NS(
        create=lambda **kw: seen.append(copy.deepcopy(admission.current_receipt())) or env.answer)))
    env.ns["_get_cached_client"] = lambda *a, **kw: (client, "selected-model")
    assert invoke(env) is env.answer
    assert seen[0]["route_id"] == "test-route" and admission.current_receipt() is None


def test_cancelled_async_call_releases_both_scopes(env):
    env.knobs["failure"] = asyncio.CancelledError()
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await env.ns["async_call_llm"](task="compression", messages=MESSAGES)
        assert env.adapter._CALL.get() is None and env.admission.get() is None
    asyncio.run(run())
    assert len(env.sent) == 1


def test_installer_dry_run_idempotence_and_drift(staged):
    installer, root = staged
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    assert len(installer.apply(root, check=True)) == 3
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before
    installer.apply(root)
    assert installer.apply(root) == []
    target = root / "agent/auxiliary_client.py"
    # Alter an audited send anchor that exists on both fresh and patched hosts.
    target.write_text(target.read_text().replace("await required_asend(client, stream_kwargs)", "await changed(client, stream_kwargs)", 1))
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    with pytest.raises(RuntimeError, match="Inspect changed Hermes"):
        installer.apply(root)
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before
