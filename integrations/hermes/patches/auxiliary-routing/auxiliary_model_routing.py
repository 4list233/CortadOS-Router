"""Required routing for auxiliary calls, without task-specific result parsing.

The public host entry points own a fresh scope per call. A selected route admits
one provider dispatch; callers must start a new routed call after any failure.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import inspect
import json
import math
from threading import Lock
from uuid import uuid4

_CALL = ContextVar("required_auxiliary_call", default=None)
_TRUSTED = ContextVar("trusted_auxiliary_constraints", default=None)


class AuxiliaryRoutingError(RuntimeError):
    pass


def auxiliary_scope(function):
    """Keep admission and retry state local across nested calls, threads and tasks."""
    @contextmanager
    def scope():
        token = _CALL.set({"required": False, "receipt": None, "attempts": 0, "lock": Lock()})
        try:
            yield _CALL.get()
        finally:
            _CALL.reset(token)
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with scope():
                return await function(*args, **kwargs)
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        with scope() as state:
            result = function(*args, **kwargs)
            if state["required"] and hasattr(result, "__next__"):
                return _ScopedStream(result, state)
            return result
    return synchronous


class _ScopedStream:
    """Relay streams may defer create() until iteration, after call_llm returns."""
    def __init__(self, stream, state):
        self.stream, self.state = stream, state

    def __iter__(self):
        return self

    def __next__(self):
        token = _CALL.set(self.state)
        try:
            return next(self.stream)
        finally:
            _CALL.reset(token)

    def close(self):
        token = _CALL.set(self.state)
        try:
            close = getattr(self.stream, "close", None)
            if close:
                close()
        finally:
            _CALL.reset(token)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextmanager
def trusted_routing(purpose, constraints):
    """Internal callers only: explicit review, benchmark or escalation policy.

    This is not accepted through tool arguments, messages or model/provider pins.
    Configured auxiliary task routing_constraints are the other trusted source.
    """
    if not isinstance(purpose, str) or not purpose.strip() or not isinstance(constraints, dict):
        raise ValueError("A trusted purpose and constraint dictionary are required")
    token = _TRUSTED.set((purpose, deepcopy(constraints)))
    try:
        yield
    finally:
        _TRUSTED.reset(token)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _media(messages):
    return any(isinstance(part, dict) and part.get("type") in
               {"image", "image_url", "input_image", "video_url", "input_video"}
               for message in messages if isinstance(message, dict)
               for part in (message.get("content") if isinstance(message.get("content"), list) else []))


def _scoring_prompt(task, messages, tools):
    # Image bytes are measured for fit, not sent to a text-only scorer.
    projected = []
    for message in messages:
        item = deepcopy(message)
        if isinstance(item.get("content"), list):
            item["content"] = [part if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
                               else {"type": "media", "serialized_bytes": len(_json(part).encode())}
                               for part in item["content"]]
        projected.append(item)
    return "Auxiliary task: " + str(task or "unspecified") + "\n" + _json({"messages": projected, "tools": tools})


def _constraints(task, task_cfg, messages):
    value = task_cfg.get("routing_constraints") or {}
    if not isinstance(value, dict):
        raise AuxiliaryRoutingError("auxiliary task routing_constraints must be a dictionary")
    result = deepcopy(value)
    purpose = str(task or "auxiliary")
    trusted = _TRUSTED.get()
    if trusted:
        purpose, extra = trusted
        # Never let an inner scope widen a configured set of admitted routes.
        if "allowed_routes" in result and "allowed_routes" in extra:
            configured = {tuple(pair) for pair in result["allowed_routes"]}
            extra = {**extra, "allowed_routes": [pair for pair in extra["allowed_routes"] if tuple(pair) in configured]}
        extra = deepcopy(extra)
        for key in ("required_capabilities", "excluded_routes"):
            if key in result:
                extra[key] = list(result[key]) + list(extra.get(key) or [])
        if result.get("min_band") and extra.get("min_band"):
            extra["min_band"] = max(result["min_band"], extra["min_band"])
        result.update(extra)
    if task == "review":
        result.update(kind="review", owner="auxiliary.review")
        for key in ("provider", "model"):
            configured = str(task_cfg.get(key) or "").strip()
            if configured and configured.lower() != "auto":
                result[key] = configured
    capabilities = set(result.get("required_capabilities") or [])
    if task == "vision" or _media(messages):
        capabilities.add("vision")
    if capabilities:
        result["required_capabilities"] = sorted(capabilities)
    return purpose, result


def prepare_required_request(host, task, *, provider, model, main_runtime, messages,
                             temperature, max_tokens, tools, timeout, extra_body,
                             reasoning_config, extra_headers, route_info, async_mode):
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    auxiliary = config.get("auxiliary") or {}
    router = auxiliary.get("model_router", (config.get("agent") or {}).get("model_router"))
    if not router:
        return None
    if not isinstance(router, str) or not router.strip():
        raise AuxiliaryRoutingError("auxiliary.model_router must be a plugin name")
    state = _CALL.get()
    if state is None:
        raise AuxiliaryRoutingError("Required auxiliary routing needs the call_llm entry scope")
    state["required"] = True
    purpose, constraints = _constraints(task, auxiliary.get(task) or {}, messages)
    size = len(_json({"messages": messages, "tools": tools}).encode())
    from agent.agent_model_routing import estimate_payload_tokens
    fit = {"assembled": True, "known": True, "input_bytes": size,
           "input_tokens": estimate_payload_tokens({"messages": messages, "tools": tools}),
           "source": "auxiliary_messages_and_tools"}
    from hermes_cli.plugins import invoke_hook
    runtime_main = main_runtime or {}
    from agent.portal_tags import get_affinity_scope
    session_id = runtime_main.get("session_id") or get_affinity_scope() or ""
    results = invoke_hook("route_agent_model", router=router, entrypoint="auxiliary",
                          prompt=_scoring_prompt(task, messages, tools), session_id=session_id,
                          platform="auxiliary", turn_id="", api_request_id="auxiliary-" + uuid4().hex,
                          input_fit=fit, current_provider=runtime_main.get("provider") or provider,
                          current_model=runtime_main.get("model") or model,
                          task_context={"auxiliary_task": task, "purpose": purpose}, constraints=constraints)
    matches = [r for r in results if isinstance(r, dict) and r.get("router") == router]
    if len(matches) != 1 or matches[0].get("error"):
        raise AuxiliaryRoutingError(str(matches[0].get("error")) if len(matches) == 1 and matches[0].get("error")
                                    else "Required auxiliary router must return exactly one decision")
    selected = matches[0]
    chosen_provider, chosen_model = selected.get("provider"), selected.get("model")
    if not all(isinstance(v, str) and v.strip() for v in (chosen_provider, chosen_model)):
        raise AuxiliaryRoutingError("Required auxiliary router returned an incomplete route")
    if chosen_provider in {"auto", "custom", "moa"}:
        raise AuxiliaryRoutingError("Required auxiliary router must select a concrete provider")
    limits = constraints.get("allowed_routes")
    if limits is not None and [chosen_provider, chosen_model] not in [list(pair) for pair in limits]:
        raise AuxiliaryRoutingError("Auxiliary route violates trusted allowed_routes")
    if constraints.get("kind") == "review" and constraints.get("owner") == "auxiliary.review":
        if any(constraints.get(key) and constraints[key] != selected[key] for key in ("provider", "model")):
            raise AuxiliaryRoutingError("Auxiliary route violates the review constraint")
    # Keep receipt host-owned and credential-free, including for CLI admission.
    receipt = {key: deepcopy(selected[key]) for key in
               ("router", "provider", "model", "context_length", "route_id", "degraded", "band") if key in selected}
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested=chosen_provider, target_model=chosen_model)
    # No task endpoint, key, old API mode or main-model fallback enters this path.
    selected_main = {"provider": chosen_provider, "model": chosen_model,
                     "base_url": runtime.get("base_url"), "api_key": runtime.get("api_key"),
                     "api_mode": runtime.get("api_mode"), "session_id": session_id}
    client, final_model = host["_get_cached_client"](
        chosen_provider, chosen_model, async_mode=async_mode, base_url=runtime.get("base_url"),
        api_key=runtime.get("api_key"), api_mode=runtime.get("api_mode"), main_runtime=selected_main,
        is_vision="vision" in constraints.get("required_capabilities", []), task=task)
    effective = host["_effective_provider_for_client"](client, chosen_provider) if client is not None else None
    if client is None or final_model != chosen_model or effective != chosen_provider:
        raise AuxiliaryRoutingError("Required auxiliary client did not preserve the selected provider/model")
    effective_extra = host["_get_task_extra_body"](task)
    effective_extra = {**effective_extra, **(extra_body or {})}
    if not async_mode:
        compression_cfg = (auxiliary.get("compression") or {}) if task == "compression" else {}
        _, effective_extra = host["_compression_fast_lane_controls"](
            task, actual_provider=chosen_provider, actual_model=chosen_model,
            requested_provider=chosen_provider, requested_model=chosen_model,
            route_config=compression_cfg, leak_guard_config=compression_cfg,
            max_tokens=max_tokens, extra_body=effective_extra)
    effective_timeout = host["_effective_aux_timeout"](task, timeout)
    base_info = str(getattr(client, "base_url", None) or runtime.get("base_url") or "")
    kwargs = host["_build_call_kwargs"](
        chosen_provider, chosen_model, messages, temperature=temperature, max_tokens=max_tokens,
        tools=tools, timeout=effective_timeout, extra_body=effective_extra,
        reasoning_config=reasoning_config, base_url=base_info, task=task)
    if extra_headers:
        kwargs["extra_headers"] = dict(extra_headers)
    if host["_is_anthropic_compat_endpoint"](chosen_provider, base_info):
        kwargs["messages"] = host["_convert_openai_images_to_anthropic"](kwargs["messages"])
    validate_payload(kwargs, receipt)
    host["_set_relay_auxiliary_route"](chosen_provider, chosen_model, runtime.get("api_mode"))
    host["_record_route_info"](route_info, chosen_provider, chosen_model)
    if route_info is not None:
        route_info["route_id"] = receipt.get("route_id", "")
    state["receipt"] = receipt
    return host["_PreparedAuxRequest"](
        client, chosen_model, kwargs, chosen_provider, chosen_provider, chosen_model,
        runtime.get("base_url"), runtime.get("api_key"), runtime.get("api_mode"), effective_timeout,
        effective_extra, base_info, receipt)


def validate_payload(kwargs, receipt):
    if kwargs.get("model") != receipt["model"]:
        raise AuxiliaryRoutingError("Auxiliary payload changed the selected model")
    body = kwargs.get("extra_body") or {}
    if not isinstance(body, dict) or set(body) & {"model", "models", "route", "provider", "messages", "tools", "base_url", "api_key"}:
        raise AuxiliaryRoutingError("Auxiliary extra_body cannot override the admitted route or input")
    headers = kwargs.get("extra_headers") or {}
    if any(str(key).lower() in {"authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie"} for key in headers):
        raise AuxiliaryRoutingError("Auxiliary per-call headers cannot override routed credentials")
    size = len(_json(kwargs).encode())
    cap = receipt.get("context_length")
    from agent.agent_model_routing import estimate_payload_tokens
    if type(cap) is not int or cap <= 0 or estimate_payload_tokens(kwargs) > cap:
        raise AuxiliaryRoutingError("Auxiliary payload exceeds the admitted model window")
    if receipt["provider"] == "agy-cli" and size > 180000:
        raise AuxiliaryRoutingError("Auxiliary payload exceeds the Antigravity transport limit")


@contextmanager
def _dispatch(kwargs):
    state = _CALL.get()
    if state is None or not state["required"]:
        yield
        return
    receipt = state["receipt"]
    if receipt is None:
        raise AuxiliaryRoutingError("Auxiliary dispatch has no required route")
    validate_payload(kwargs, receipt)
    with state["lock"]:
        if state["attempts"]:
            raise AuxiliaryRoutingError("Auxiliary provider retry requires a new routed call")
        state["attempts"] += 1
    from agent.agent_model_routing import admitted
    with admitted(deepcopy(receipt)):
        yield


def required_send(client, kwargs):
    with _dispatch(kwargs):
        return client.chat.completions.create(**kwargs)


async def required_asend(client, kwargs):
    with _dispatch(kwargs):
        return await client.chat.completions.create(**kwargs)
