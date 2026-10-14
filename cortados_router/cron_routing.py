"""Select a cron's model at dispatch, using live capacity and usable credentials."""
import logging
import json
import time
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('cortados_cron_execution', Path(__file__).with_name('execution_routing.py'))
_execution = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_execution)

logger = logging.getLogger(__name__)
ROUTER = "cortados-router"


def route(core, *, router, job, prompt, session_id, face=None, **_telemetry):
    if router != ROUTER:
        return None
    try:
        if face is None:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            face = home.name if home.parent.name == "profiles" else "default"
        return _route(core, job, prompt, session_id, face)
    except Exception as exc:
        logger.warning("cron routing failed for %s: %s", job.get("id"), exc)
        return {"router": ROUTER, "error": f"Routing failed: {type(exc).__name__}: {exc}"}


def _route(core, job, prompt, session_id, face):
    select = _execution.select
    text = core.clean_request(prompt) or job.get("prompt") or job.get("name") or ""
    candidates = [r for m in (core.model_scores() or {}).get("models", []) for r in m.get("reach", [])]
    candidates += [r for rows in core.table()["candidates"].values() for r in rows]
    bad_file = core.STATE / "cron-bad-routes.json"
    bad = json.loads(bad_file.read_text()).get(job["id"], {}) if bad_file.exists() else {}
    excluded = [(r["provider"], r["model"]) for r in candidates
                if bad.get(f"{r['lane']}/{r['model']}", 0) > time.time()]
    excluded += [("codex-cli", m) for p, m in excluded if p == "openai-codex"]
    result = select(core, prompt=text, session_id=session_id, face=face, platform="cron",
                    entrypoint="cron-dispatch", constraints={"excluded_routes": excluded},
                    route_id=f"cron-dispatch:{face}:{session_id}", log_platform="cron-dispatch")
    if result.get("error"):
        result["error"] = "No usable route fits this cron. " + result["error"]
    result['constraints'] = {'excluded_routes': excluded}
    return result
