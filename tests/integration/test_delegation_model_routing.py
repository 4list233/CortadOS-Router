"""Staged host integration; no live installation, plugin discovery or model calls.

Run with the Hermes venv and PYTHONPATH pointing at the host checkout. Patched
functions execute with real Hermes normalization/runtime helpers, while child
construction, credentials, dispatch and transcripts are replaced at the boundary.
"""
import importlib.util as _guard_util
import unittest as _guard_unittest
if _guard_util.find_spec("pytest") is None or _guard_util.find_spec("hermes_cli") is None:
    raise _guard_unittest.SkipTest("Hermes integration test: needs pytest and an importable Hermes host (hermes_cli). "
                                   "Run with pytest inside the Hermes venv; see tests/integration/README.md")
import ast
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
    installer = load("delegation_installer", ROOT / "integrations/hermes/patches/delegation-routing.py")
    root = tmp_path / "host"
    for rel in ("tools/delegate_tool.py", "hermes_cli/plugins.py", "agent/review_engine.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((HOST / rel).read_text())
    return installer, root


@pytest.fixture
def env(staged, tmp_path, monkeypatch):
    installer, root = staged
    installer.apply(root)
    # Scope any incidental host reads/writes to a private profile.
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")
    monkeypatch.setenv("CORTADOS_LIVE_WRITE", "0")
    monkeypatch.setenv("WANGCAI_LIVE_WRITE", "0")
    from hermes_cli import plugins, config
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    monkeypatch.setattr(config, "load_config_readonly", lambda: {})
    from tools import delegate_tool, delegation_live_log
    adapter = load("staged_delegate_model_routing", root / "tools/delegate_model_routing.py")
    monkeypatch.setitem(sys.modules, "tools.delegate_model_routing", adapter)
    source = ast.parse((root / "tools/delegate_tool.py").read_text())
    functions = [n for n in source.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("delegate_task", "_build_children")]
    ns = vars(delegate_tool).copy()
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(root / "tools/delegate_tool.py"), "exec"), ns)
    events, built, batches, hook_calls, credential_calls = [], [], [], [], []
    cfg = {"model_router": "cortados-router", "provider": "stale-provider",
           "model": "stale-model", "base_url": "https://stale.invalid", "api_key": "stale-key",
           "request_overrides": {"old": True}, "command": "missing-command",
           "fallback_providers": [{"provider": "escape", "model": "escape"}]}
    parent = NS(provider="parent-provider", model="parent-model", session_id="parent-session",
                _current_turn_id="parent-turn", _delegate_depth=0,
                base_url="https://parent.invalid", api_key="parent-key", request_overrides={"parent": True},
                acp_command="parent-acp", acp_args=["old"],
                _fallback_chain=[{"provider": "parent-fallback", "model": "fallback"}])

    def credentials(selected, parent_agent):
        events.append("credentials")
        credential_calls.append(copy.deepcopy(selected))
        if selected.get("provider") == "bad-auth":
            raise ValueError("selected provider auth unavailable")
        return {"model": selected.get("model"), "provider": selected.get("provider"),
                "base_url": "https://selected.invalid", "api_key": "private-selected-key",
                "api_mode": "chat_completions", "request_overrides": {}}

    def route(**kw):
        events.append("route")
        hook_calls.append(copy.deepcopy(kw))
        i = kw["task_context"]["task_index"]
        return {"router": "cortados-router", "provider": f"provider-{i}",
                "model": f"model-{i}", "route_id": f"receipt-{i}", "degraded": False}

    manager._hooks["route_agent_model"] = [route]

    def build(**kw):
        events.append("build")
        built.append(kw)
        return NS(_session_init_model_config={}, model=kw["model"])

    def dispatch(batch, background):
        events.append("dispatch")
        batches.append(batch)
        return json.dumps({"status": "dispatched", "background": background})

    ns.update(_load_config=lambda: cfg, _get_max_concurrent_children=lambda: 8,
              _get_max_spawn_depth=lambda: 3, is_spawn_paused=lambda: False,
              _resolve_delegation_credentials=credentials,
              _build_child_preserving_parent_tools=build,
              _announce_batch=lambda *a: None, _capture_origin=lambda: ("", "", None, None, False),
              _run_batch=dispatch)
    def transcripts(*a, **kw):
        events.append("transcripts")
        return "deleg-test", [], []
    monkeypatch.setattr(delegation_live_log, "create_live_transcripts", transcripts)
    try:
        yield NS(installer=installer, root=root, ns=ns, cfg=cfg, parent=parent, manager=manager,
                 events=events, built=built, batches=batches, hook_calls=hook_calls,
                 credential_calls=credential_calls, adapter=adapter, config=config)
    finally:
        reset_hermes_home_override(token)


TASKS = [{"goal": "Investigate the first independent issue and report the evidence", "context": "first context"},
         {"goal": "Investigate the second independent issue and report the evidence", "context": "second context"}]


def call(env, **kw):
    return json.loads(env.ns["delegate_task"](parent_agent=env.parent, **kw))


def test_routes_and_authenticates_whole_batch_before_construction(env):
    saved = copy.deepcopy(env.cfg)
    tasks = copy.deepcopy(TASKS)
    assert call(env, tasks=tasks, background=True)["status"] == "dispatched"
    assert env.events == ["route", "route", "credentials", "credentials", "transcripts", "build", "build", "dispatch"]
    assert tasks == TASKS and env.cfg == saved
    for i, kwargs in enumerate(env.built):
        assert (kwargs["override_provider"], kwargs["model"]) == (f"provider-{i}", f"model-{i}")
        assert kwargs["override_base_url"] == "https://selected.invalid"
        assert kwargs["override_api_key"] == "private-selected-key"
        assert kwargs["override_acp_command"] is None
        assert kwargs["override_request_overrides"] == {}
        assert env.credential_calls[i] == {"provider": f"provider-{i}", "model": f"model-{i}",
                                           "fallback_providers": [], "fallback_model": []}
        payload = env.hook_calls[i]
        assert payload["prompt"] == TASKS[i]["goal"] + "\n\n" + TASKS[i]["context"]
        assert payload["platform"] == "subagent" and payload["entrypoint"] == "native-delegation"
        assert payload["session_id"] == "parent-session" and payload["turn_id"] == ""
        assert payload["task_context"]["parent_turn_id"] == "parent-turn"
        child = env.batches[0].children[i][2]
        assert child._cortados_route_receipt["route_id"] == f"receipt-{i}"
        assert "key" not in json.dumps(child._session_init_model_config)
        # Real runtime helper: override clears parent's transport and fallback.
        rt = env.ns["_resolve_child_runtime"](
            env.parent, env.cfg, env.parent.api_key, model=kwargs["model"],
            **{k: v for k, v in kwargs.items() if k.startswith("override_") and k != "override_request_overrides"},
            routing_cfg=kwargs["routing_cfg"])
        assert rt["provider"] == f"provider-{i}" and rt["model"] == f"model-{i}"
        assert rt["fallback_model"] is None and rt["acp_command"] is None and rt["acp_args"] == []
    assert env.batches[0].creds["model"] is None and env.batches[0].creds["provider"] is None
    assert len({call["api_request_id"] for call in env.hook_calls}) == 2


def test_real_credential_resolution_uses_selected_pair_in_current_profile(env, monkeypatch):
    from tools.delegate_tool_config import _resolve_delegation_credentials
    from hermes_cli import runtime_provider
    from hermes_constants import get_hermes_home
    calls = []
    def resolve(**kwargs):
        calls.append((kwargs, get_hermes_home()))
        return {"provider": kwargs["requested"], "model": kwargs["target_model"],
                "api_key": "scoped-test-key", "base_url": "https://scoped.invalid",
                "api_mode": "chat_completions"}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    env.ns["_resolve_delegation_credentials"] = _resolve_delegation_credentials
    assert call(env, goal="Inspect scoped credentials")["status"] == "dispatched"
    assert calls == [({"requested": "provider-0", "target_model": "model-0"}, env.root.parent / "profile")]
    assert env.built[0]["override_api_key"] == "scoped-test-key"
    assert env.batches[0].creds["model"] == "model-0"


def test_model_registry_cannot_supply_private_constraints(env):
    tree = ast.parse((env.root / "tools/delegate_tool.py").read_text())
    registry_call = next(n.value for n in tree.body if isinstance(n, ast.Expr)
                         and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                         and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == "registry"
                         and n.value.func.attr == "register")
    handler = next(k.value for k in registry_call.keywords if k.arg == "handler")
    seen = []
    ns = {**env.ns, "delegate_task": lambda **kw: seen.append(kw)}
    invoke = eval(compile(ast.Expression(body=handler), "staged-registry", "eval"), ns)
    invoke({"goal": "Review this change", "credentials_cfg": {"model": "bypass"},
            "routing_constraints": {"kind": "review", "model": "bypass"}}, parent_agent=env.parent)
    assert "credentials_cfg" not in seen[0] and "routing_constraints" not in seen[0]


@pytest.mark.parametrize("result", [None, {"router": "other", "model": "wrong", "provider": "wrong"},
    {"router": "cortados-router", "error": "capacity exhausted"},
    {"router": "cortados-router", "provider": "p", "model": " "}])
def test_missing_failed_or_incomplete_router_never_builds_or_inherits(env, result):
    env.manager._hooks["route_agent_model"] = [lambda **kw: result]
    assert "error" in call(env, goal="Reply exactly HEALTH_OK")
    assert env.events == [] and not env.built and not env.credential_calls


def test_hook_exception_fails_closed(env):
    def broken(**kw):
        raise RuntimeError("router down")
    env.manager._hooks["route_agent_model"] = [broken]
    assert "error" in call(env, goal="Check a small task")
    assert not env.built and not env.credential_calls


def test_later_auth_failure_leaves_no_children_or_transcripts(env):
    def route(**kw):
        return {"router": "cortados-router", "provider": "bad-auth" if kw["task_context"]["task_index"] else "ok", "model": "m"}
    env.manager._hooks["route_agent_model"] = [route]
    assert "auth unavailable" in call(env, tasks=TASKS)["error"]
    assert env.events == ["credentials", "credentials"] and not env.built


def test_trusted_constraints_persist_but_task_fields_and_pins_are_not_constraints(env):
    trusted = {"kind": "review", "owner": "auxiliary.review", "provider": "p", "model": "m"}
    assert call(env, goal="Review the requested change", credentials_cfg={"provider": "old", "model": "old"},
                routing_constraints=trusted)["status"] == "dispatched"
    assert env.hook_calls[0]["constraints"] == trusted
    child = env.batches[0].children[0][2]
    assert child._cortados_route_constraints == trusted
    trusted["model"] = "mutated"
    assert child._cortados_route_constraints["model"] == "m"
    task = {**TASKS[0], "model": "bypass", "provider": "bypass", "constraints": {"kind": "diagnostic"}}
    assert call(env, tasks=[task], credentials_cfg={"model": "old"})["status"] == "dispatched"
    assert env.hook_calls[-1]["constraints"] == {}


def test_agent_setting_fallback_and_disabled_legacy_behavior(env, monkeypatch):
    env.cfg.pop("model_router")
    monkeypatch.setattr(env.config, "load_config_readonly", lambda: {"agent": {"model_router": "cortados-router"}})
    assert call(env, goal="Investigate a child task")["status"] == "dispatched"
    assert len(env.hook_calls) == 1
    env.cfg["model_router"] = None
    assert call(env, goal="Legacy route")["status"] == "dispatched"
    assert len(env.hook_calls) == 1 and env.credential_calls[-1] == env.cfg


def test_controls_and_invalid_tasks_do_not_route(env):
    env.ns["_handle_control_action"] = lambda *a: json.dumps({"status": "controlled"})
    assert call(env, action="list")["status"] == "controlled"
    assert "error" in call(env, goal="")
    assert "error" in call(env, goal="Valid task", images=[None])
    assert not env.events


def test_output_contract_and_images_survive_routing(env):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    assert call(env, goal="Inspect screenshot and answer", context="Original context", output_schema=schema,
                images=["https://example.invalid/screenshot.png"])["status"] == "dispatched"
    child = env.batches[0].children[0][2]
    assert child._delegate_output_schema == schema
    assert child._delegate_images == ["https://example.invalid/screenshot.png"]
    assert "Original context" in env.built[0]["context"] and "answer" in env.built[0]["context"]


def test_review_entry_supplies_explicit_private_constraints(env):
    tree = ast.parse((env.root / "agent/review_engine.py").read_text())
    start = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_review")
    seen = []
    delegate_stub = NS(delegate_task=lambda **kw: seen.append(kw) or '{"status":"dispatched"}')
    original = sys.modules.get("tools.delegate_tool")
    sys.modules["tools.delegate_tool"] = delegate_stub
    ns = {"Optional": object, "List": list, "Dict": dict, "Any": object, "json": json,
          "snapshot_recent_messages": lambda x: x, "collect_parent_loaded_skills": lambda *a: [],
          "build_review_task": lambda *a: ("Review", "Evidence"),
          "_load_review_credentials_cfg": lambda: {"provider": "review-provider", "model": "review-model"}}
    try:
        exec(compile(ast.Module(body=[start], type_ignores=[]), "staged-review", "exec"), ns)
        ns["start_review"](env.parent, [{"role": "user", "text": "review"}])
    finally:
        if original is None:
            sys.modules.pop("tools.delegate_tool", None)
        else:
            sys.modules["tools.delegate_tool"] = original
    assert seen[0]["routing_constraints"] == {"kind": "review", "owner": "auxiliary.review",
                                               "provider": "review-provider", "model": "review-model"}


def test_installer_check_idempotence_and_existing_generic_hook(staged):
    installer, root = staged
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    assert len(installer.apply(root, check=True)) == 4
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before
    hooks = root / "hermes_cli/plugins.py"
    hooks.write_text(installer.transform_hooks(hooks.read_text()))
    assert len(installer.apply(root)) == 3
    assert installer.apply(root) == []
    assert hooks.read_text().count('"route_agent_model"') == 1


def test_installer_drift_aborts_all_writes(staged):
    installer, root = staged
    target = root / "agent/review_engine.py"
    target.write_text(target.read_text().replace("    raw = delegate_task(", "    raw = changed_delegate_task("))
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    with pytest.raises(RuntimeError, match="Inspect changed Hermes"):
        installer.apply(root)
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before


def test_installer_detects_drift_after_previous_install(staged):
    installer, root = staged
    installer.apply(root)
    target = root / "tools/delegate_tool.py"
    target.write_text(target.read_text().replace("        child_routes=child_routes,\n", ""))
    with pytest.raises(RuntimeError, match="Inspect changed patched Hermes"):
        installer.apply(root)
