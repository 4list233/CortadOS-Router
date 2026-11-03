"""Per-occurrence model routing, before credential resolution or agent construction."""
from dataclasses import replace
import logging

logger = logging.getLogger(__name__)


def route_job(job, jc, *, prompt, session_id):
    """Use the configured plugin's decision without rewriting the saved job."""
    router = (jc.cfg.get("cron") or {}).get("model_router",
                 (jc.cfg.get("agent") or {}).get("model_router"))
    if not router:
        return job, jc
    from hermes_cli.plugins import invoke_hook
    results = invoke_hook("route_cron_model", router=router, job=dict(job),
                          prompt=prompt, session_id=session_id)
    result = next((r for r in results if isinstance(r, dict) and r.get("router") == router), None)
    if not result or result.get("error"):
        raise ValueError((result or {}).get("error") or f"Required cron router {router!r} did not return a route")
    provider, model = result.get("provider"), result.get("model")
    if not all(isinstance(v, str) and v.strip() for v in (provider, model)):
        raise ValueError("Cron router returned an incomplete provider/model pair")
    routed = {**job, "provider": provider, "model": model, "_routing_session": session_id,
              '_routing_constraints': result.get('constraints') or {}}
    # A saved endpoint belongs to the old pin, never to a newly selected provider.
    routed.pop("base_url", None)
    # The router already checked credentials and quota. A later failure must not
    # escape that decision through a static default (including mid-turn failover).
    cfg = {**jc.cfg, "fallback_providers": [], "fallback_model": []}
    logger.info("Cron router %s: job=%s selected=%s/%s", router, job["id"], provider, model)
    return routed, replace(jc, cfg=cfg, model=model, cron_default_provider=provider)


def routing_header(job):
    """Persist the selected route alongside this run, never infer it from a later pin."""
    if not job.get("_routing_session"):
        return ""
    return (f"**Routing Session:** {job['_routing_session']}\n"
            f"**Provider:** {job['provider']}\n**Model:** {job['model']}\n")
