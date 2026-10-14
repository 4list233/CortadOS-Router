"""Preflight native children through the configured router before constructing any agent.

This module has no model policy or credential store. The plugin selects routes;
the ordinary Hermes resolver supplies credentials for each selected provider.
"""
from copy import deepcopy
from uuid import uuid4


def prepare_children(task_list, *, cfg, credentials_cfg, parent_agent,
                     constraints, resolve_credentials):
    owner = credentials_cfg if credentials_cfg is not None else cfg
    if "model_router" in cfg:
        router = cfg["model_router"]
    else:
        from hermes_cli.config import load_config_readonly
        router = (load_config_readonly().get("agent") or {}).get("model_router")
    if not router:
        return resolve_credentials(owner, parent_agent), None
    if not isinstance(router, str) or not router.strip():
        raise ValueError("delegation.model_router must be a nonempty plugin name")
    if constraints is not None and not isinstance(constraints, dict):
        raise ValueError("Internal routing constraints must be a dictionary")

    from hermes_cli.plugins import invoke_hook
    plans = []
    for index, task in enumerate(task_list):
        trusted = deepcopy(constraints or {})
        prompt = task["goal"]
        if task.get("context"):
            prompt += "\n\n" + task["context"]
        results = invoke_hook(
            "route_agent_model", router=router, entrypoint="native-delegation",
            prompt=prompt, session_id=getattr(parent_agent, "session_id", "") or "",
            # No child turn exists yet. Reusing the parent's turn would replace
            # its route receipt and can accidentally borrow reviewer state.
            platform="subagent", turn_id="",
            api_request_id="delegation-preflight-" + uuid4().hex,
            current_provider=getattr(parent_agent, "provider", None),
            current_model=getattr(parent_agent, "model", None),
            task_context={"task_index": index, "task_count": len(task_list),
                          "parent_turn_id": getattr(parent_agent, "_current_turn_id", "") or "",
                          "parent_subagent_id": getattr(parent_agent, "_subagent_id", None)},
            constraints=deepcopy(trusted),
        )
        matches = [r for r in results if isinstance(r, dict) and r.get("router") == router]
        if len(matches) != 1:
            raise ValueError(f"Required delegation router {router!r} must return exactly one route")
        result = matches[0]
        if result.get("error"):
            raise ValueError(str(result["error"]))
        provider, model = result.get("provider"), result.get("model")
        if not all(isinstance(v, str) and v.strip() for v in (provider, model)):
            raise ValueError("Delegation router returned an incomplete provider/model pair")
        # Construct a fresh config: old endpoints, keys, request personalities,
        # ACP commands and fallback chains belong to the superseded pin.
        selected = {"provider": provider.strip(), "model": model.strip(),
                    "fallback_providers": [], "fallback_model": []}
        receipt = {key: deepcopy(result[key]) for key in
                   ("router", "provider", "model", "context_length", "route_id", "degraded")
                   if key in result}
        plans.append({"routing_cfg": selected, "constraints": trusted,
                      "receipt": receipt})

    # Resolve every credential bundle before any child, transcript or dispatch.
    for plan in plans:
        plan["creds"] = resolve_credentials(plan["routing_cfg"], parent_agent)
    # Call-wide metadata must not attribute mixed children to the first model.
    pairs = {(p["creds"].get("provider"), p["creds"].get("model")) for p in plans}
    shared = dict(plans[0]["creds"])
    if len(pairs) != 1:
        shared.update(provider=None, model=None)
    return shared, plans


def attach_route(child, plan, writer=None):
    """Carry trusted policy to the request gate and retain a credential-free receipt."""
    if plan is None:
        return
    child._cortados_route_constraints = deepcopy(plan["constraints"])
    child._cortados_route_receipt = deepcopy(plan["receipt"])
    config = getattr(child, "_session_init_model_config", None)
    if isinstance(config, dict):
        config["_cortados_route"] = deepcopy(plan["receipt"])
    if writer is not None:
        receipt = plan["receipt"]
        writer.event("routing", f"{receipt['provider']}/{receipt['model']} route_id={receipt.get('route_id', '')}")
