"""Real scheduler + router paths, temporary profile scopes, no external calls."""
import importlib.util as _guard_util
import unittest as _guard_unittest
if _guard_util.find_spec("pytest") is None or _guard_util.find_spec("hermes_cli") is None:
    raise _guard_unittest.SkipTest("Hermes integration test: needs pytest and an importable Hermes host (hermes_cli). "
                                   "Run with pytest inside the Hermes venv; see tests/integration/README.md")
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from cron import scheduler
from cron.scheduler_model_routing import route_job
from hermes_cli import plugins, runtime_provider
from hermes_cli.auth import AuthError
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_fire_routes_with_current_capacity_and_profile_without_repinning(tmp_path, monkeypatch):
    core = load("cron_test_core", "cortados_router/core.py")
    monkeypatch.setattr(core, "STATE", tmp_path)
    routing = load("cron_test_routing", "cortados_router/cron_routing.py")
    answers = {f: 0 for f in core.FEATURES}
    answers.update(work_kind="ops", outward=False, parts=0)
    monkeypatch.setattr(core, "decide", lambda *a: dict(answers=answers, degraded=False, engine="test", engine_ms=0))
    candidates = [dict(lane=lane, provider=provider, model=model, family="test", general=70,
                       index=70, price=4, tokens_per_s=1, ceiling=False, use_case="ops")
                  for lane, provider, model in [("codex", "openai-codex", "test-a"),
                                                ("claude", "claude-cli", "test-b")]]
    monkeypatch.setattr(core, "model_scores", lambda: {"models": [{"reach": candidates}]})
    monkeypatch.setattr(core, "scored_candidates", lambda *a: copy.deepcopy(candidates))
    monkeypatch.setattr(core, "required_index", lambda *a: 0)
    monkeypatch.setattr(core, "_pool_tokens_per_pct", lambda c: None)
    monkeypatch.setattr(core, "_tokens_left_for", lambda c: None)
    health = {}
    monkeypatch.setattr(core, "lane_health", lambda: health)
    monkeypatch.setattr(core, "log_route", lambda *a: None)
    auth_calls = []
    def auth(**kw):
        auth_calls.append((str(get_hermes_home()), kw["requested"]))
        return {"provider": kw["requested"]}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", auth)
    # Real hook dispatcher; no discovery of production Context or other plugins.
    manager = plugins.PluginManager()
    manager._discovered = True
    manager._hooks["route_cron_model"] = [lambda **kw: routing.route(core, face=get_hermes_home().name, **kw)]
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    saved = dict(id="scheduled", prompt="check status", provider="claude-cli", model="old-pin", base_url="https://old.invalid")
    cfg = {"cron": {"model_router": "cortados-router"},
           "fallback_providers": [{"provider": "claude-cli", "model": "old-fallback"}]}
    jc = scheduler._CronJobConfig(cfg, "old-pin", {}, "claude-cli")
    for face, preferred in [("A", "codex"), ("B", "claude"), ("A", "codex")]:
        home = tmp_path / face; home.mkdir(exist_ok=True)
        token = set_hermes_home_override(home)
        try:
            health.clear()
            health.update({lane: {"ok": True, "scarcity": -.6 if lane == preferred else .9} for lane in ["codex", "claude"]})
            job, resolved = route_job(saved, jc, prompt="check status", session_id=f"run-{face}")
            assert job["provider"] == ("openai-codex" if preferred == "codex" else "claude-cli")
            assert resolved.model == job["model"]
            assert resolved.cfg["fallback_providers"] == resolved.cfg["fallback_model"] == []
            assert "base_url" not in job
            header = scheduler._run_doc_header(job, "check", job["id"], "check status")
            assert f"**Provider:** {job['provider']}" in header
            assert f"**Model:** {resolved.model}" in header
            assert f"**Routing Session:** run-{face}" in header
            assert auth_calls[-1][0] == str(home)
        finally:
            reset_hermes_home_override(token)
    assert saved["model"] == "old-pin" and cfg["fallback_providers"]
    # The outcome sampler must attribute this occurrence, not the stored pin.
    monkeypatch.setitem(sys.modules, "core", core)
    sampler = load("cron_test_sampler", "bin/route-crons.py")
    monkeypatch.setattr(sampler, "BAD_ROUTES", tmp_path / "bad.json")
    jobs_path = tmp_path / "cron/jobs.json"
    output = jobs_path.parent / "output/scheduled"
    output.mkdir(parents=True)
    jobs_path.write_text(json.dumps({"jobs": [{**saved, "name": "check"}]}))
    (output / "20261007_100000.md").write_text(header + "## Response\nAll checked.\n")
    (output / "20261006_100000.md").write_text("# Old run\n## Prompt\n**Model:** forged\n## Response\nAll checked.\n")
    monkeypatch.setattr(sampler, "profiles", lambda: [("A", jobs_path, "unused")])
    con = sampler.db()
    try:
        sampler.record_outcomes(con)
        assert con.execute("SELECT provider,model FROM cron_outcomes WHERE key=?", ("scheduled:20261007_100000",)).fetchone() == ("openai-codex", "test-a")
        assert con.execute("SELECT provider,model FROM cron_outcomes WHERE key=?", ("scheduled:20261006_100000",)).fetchone() == (None, None)
    finally:
        con.close()
    # Missing credentials remove the provider and make the router choose again.
    def missing(**kw):
        if kw["requested"] in ("openai-codex", "codex-cli"):
            raise AuthError("missing", provider="openai-codex", code="codex_auth_missing")
        return {"provider": kw["requested"]}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", missing)
    job, _ = route_job(saved, jc, prompt="check status", session_id="auth-retry")
    assert job["provider"] == "claude-cli"
    health["claude"] = {"ok": False, "scarcity": 1, "why": "spent"}
    with pytest.raises(ValueError, match="No usable route"):
        route_job(saved, jc, prompt="check status", session_id="none-usable")


def test_scheduler_routes_before_runtime_and_missing_router_blocks_without_agent(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "run_agent", NS(AIAgent=object))
    cfg = {"cron": {"model_router": "required"}}
    jc = scheduler._CronJobConfig(cfg, "old", {}, "old-provider")
    monkeypatch.setattr(scheduler, "_prepare_job_prompt", lambda *a: (None, "prepared payload"))
    class Scope:
        workdir = None
        def __init__(self, *a): pass
        def enter(self): pass
        def exit(self): pass
    monkeypatch.setattr(scheduler, "_CronRunScope", Scope)
    monkeypatch.setattr(scheduler, "_reload_dotenv_and_publish_delivery_target", lambda *a: None)
    monkeypatch.setattr(scheduler, "_load_cron_job_config", lambda *a: jc)
    results = []
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: results)
    seen = []
    def setup(job, jid, name, config):
        seen.append((job["provider"], config.model))
        return NS(blocked=(True, "", "routed", None))
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", setup)
    monkeypatch.setattr(scheduler, "_blocked_config_result", lambda *a: (False, "", "", a[-1]))
    job = {"id": "test", "prompt": "check", "model": "old", "provider": "old-provider"}
    ok, _, _, error = scheduler.run_job(job)
    assert not ok and "did not return a route" in error and not seen
    results.append({"router": "required", "provider": "new-provider", "model": "new-model"})
    assert scheduler.run_job(job)[0]
    assert seen == [("new-provider", "new-model")]
    assert job["model"] == "old"
    # A routed copy must not swallow the host's zero-call network retry signal.
    import httpx
    def unreachable(*args):
        raise httpx.ConnectError("temporary network failure")
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", unreachable)
    assert not scheduler.run_job(job)[0]
    assert job["_model_unreachable"] is True and job["model"] == "old"
