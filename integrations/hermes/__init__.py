"""Cortados model routing, execution receipts, image conversion and observation: the Hermes plugin.

Host boundary. Hermes modules (hermes_cli, hermes_constants, gateway, agent, tools) and the CortadOS Context engine
(context.hermes_engine, context.hermes_input) are optional here: when one is missing the plugin records it
(cortados_router.compat.missing_modules) and takes the documented fallback instead of failing the turn.
"""
from __future__ import annotations

# The router package lives two levels up (repo/cortados_router); Hermes loads this file by path.
import sys as _compat_sys
from pathlib import Path as _CompatPath
_ROUTER_ROOT = _CompatPath(__file__).resolve().parents[2]
if str(_ROUTER_ROOT) not in _compat_sys.path:
    _compat_sys.path.insert(0, str(_ROUTER_ROOT))

from cortados_router.compat import getenv as _env_get, note_missing as _note_missing, optional_module as _optional_module

import json
import logging
import os
import re
import sys
import threading
import time
from contextvars import copy_context
from pathlib import Path

_HERE = _ROUTER_ROOT / "cortados_router"      # the router modules (core, reviews, images, transport are loaded by path below)
import importlib.util as _ilu  # load core under a unique module name: other plugins also ship a core-like module name
_spec = _ilu.spec_from_file_location("cortados_router_core", str(_HERE / "core.py"))
core = _ilu.module_from_spec(_spec); _spec.loader.exec_module(core)  # type: ignore[union-attr]
_review_spec = _ilu.spec_from_file_location("cortados_router_reviews", str(_HERE / "reviews.py"))
reviews = _ilu.module_from_spec(_review_spec); _review_spec.loader.exec_module(reviews)
_image_spec = _ilu.spec_from_file_location("cortados_router_images", str(_HERE / "images.py"))
image_conversion = _ilu.module_from_spec(_image_spec); _image_spec.loader.exec_module(image_conversion)
_transport_spec = _ilu.spec_from_file_location("cortados_router_transport", str(_HERE / "transport.py"))
transport = _ilu.module_from_spec(_transport_spec); _transport_spec.loader.exec_module(transport)
from cortados_router.task_context import unavailable as _task_unavailable


def _pty_plugin_dir() -> Path:
    """CortadOS's PTY plugin (agent bus transport for reviews, usage ledger): $CORTADOS_PTY_PLUGIN, else the CortadOS checkout."""
    return Path(_env_get("CORTADOS_PTY_PLUGIN") or core.REPO / "plugins/cortados-pty")


def _set_window_hint(session_id, *, model, context_length) -> None:
    """Tell the CortadOS Context engine the routed window, when that engine is installed."""
    engine = _optional_module("context.hermes_engine", "no context-window hint is sent; Context budgets for the profile's model")
    if engine is not None:
        engine.set_router_window_hint(session_id, model=model, context_length=context_length)

logger = logging.getLogger(__name__)
FOOTER_WAIT_S = float(_env_get("CORTADOS_ROUTER_FOOTER_WAIT_S", "3"))
FOOTER_PLATFORMS = {"discord", "telegram", "cli"}
_pending: dict[str, dict] = {}
_lock = threading.Lock()
_review_pending: dict[str, dict] = {}
_image_routes: dict[str, dict] = {}


def _route_agent_model(router='', prompt='', session_id='', platform='', turn_id='',
                       api_request_id='', task_context=None, constraints=None, **kwargs):
    if router != 'cortados-router':
        return None
    from cortados_router.execution_routing import select
    try:
        face = _profile_name()
        if turn_id:
            _slot(turn_id, session_id, platform, kwargs.get('current_model', ''))
        with _lock:
            slot = _pending.get(turn_id) or {}
            origin = dict(slot.get('routing_input') or {})
            review = slot.get('review')
            failed_routes = list(slot.get('failed_routes') or [])
        if review:
            choice = review['chosen']
            constraints = {'allowed_routes': [(choice['provider'], choice['model'])]}
        constraints = dict(constraints or {})
        constraints['excluded_routes'] = list(constraints.get('excluded_routes') or []) + failed_routes
        if task_context is None:
            task_context = _task_for_router(session_id, face, current_request=str(prompt),
                                           native_input=origin.get('native_input'))
        recall = _recall_for_router(session_id, str(prompt), face, [])
        result = select(core, prompt=prompt, session_id=session_id, face=face,
                        platform=platform, turn_id=turn_id, api_request_id=api_request_id,
                        task_context=task_context, constraints=constraints, recall=recall,
                        escalate=_esc_for(session_id),
                        **{k: v for k, v in kwargs.items() if k in
                           {'entrypoint', 'input_fit', 'current_provider', 'current_model'}})
        if not result.get('error') and turn_id:
            with _lock:
                slot = _pending.get(turn_id)
                if slot is not None:
                    slot['execution_route'] = result
                    slot['scored_context_digest'] = _context_digest(task_context)
        if not result.get('error') and session_id:
            _set_window_hint(session_id, model=result['model'], context_length=result['context_length'])
        return result
    except Exception as exc:
        logger.warning('required model routing failed: %s', exc)
        return {'router': router, 'error': f'Model routing unavailable: {type(exc).__name__}: {exc}'}


def _profile_name() -> str:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home()).resolve()
    except ImportError:
        home = Path(_env_get("HERMES_HOME") or Path.home() / ".cortados/hermes").resolve()
    roster = Path(os.environ.get("CORTADOS_FACES_JSON", str(core.REPO / "faces.json")))
    if not roster.exists():
        # No CortadOS fleet roster: every turn belongs to the single default profile.
        _note_missing(str(roster), "fleet roster absent; every turn is routed as profile 'default'")
        return "default"
    faces = json.loads(roster.read_text())["faces"]
    return next((name for name, face in faces.items() if str(home) in
                 [str(Path(p).resolve()) for p in [face["home"], *face.get("alias_homes", [])]]), "default")


def _router_review(args: dict, turn_id: str = "", session_id: str = "", **_) -> str:
    try:
        # Hermes dispatch supplies session_id, while the observer hooks also supply turn_id.
        turn_id = turn_id or _by_session.get(session_id, "")
        action = args.get("action")
        if action == "authors":
            return json.dumps({"authors": reviews.authors(core)})
        if action == "status":
            return json.dumps(reviews.status_for(core, args.get("request_id", "")))
        if action != "request":
            raise ValueError("action must be authors, request or status")
        if (_pending.get(turn_id) or {}).get("review"):
            raise ValueError("A reviewer must submit its independent review, not start another review pair")
        sender = _profile_name()
        faces = json.loads(Path(os.environ.get("CORTADOS_FACES_JSON", str(core.REPO / "faces.json"))).read_text())["faces"]
        if sender not in faces or not faces[sender].get("bot_user_id"):
            raise ValueError("Review dispatch needs a fleet face with a bot")
        # Load the existing audited bus transport without registering/starting the PTY plugin.
        bus_spec = _ilu.spec_from_file_location("cortados_review_bus", str(_pty_plugin_dir() / "bus.py"))
        pty_path = str(_pty_plugin_dir())
        if pty_path not in sys.path:
            sys.path.append(pty_path)
        bus = _ilu.module_from_spec(bus_spec); bus_spec.loader.exec_module(bus)
        return json.dumps(reviews.dispatch(core, args, sender, faces[sender]["bot_user_id"], bus.post, turn_id))
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _text(msg) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in msg)
    return str(msg or "")


_by_session: dict[str, str] = {}


def _slot(turn_id: str, session_id: str = "", platform: str = "", model: str = "") -> dict:
    """One record per turn: what the router would do AND what really ran (brain calls, context size, workers)."""
    with _lock:
        slot = _pending.get(turn_id)
        if slot is None:
            slot = _pending[turn_id] = {"done": threading.Event(), "t0": time.time(), "platform": (platform or "").lower(), "model": model, "calls": [], "workers": [], "session_id": session_id}
        if session_id:
            _by_session[session_id] = turn_id
    return slot


def _required_routing_enabled():
    try:
        from hermes_cli.config import load_config_readonly
        return (load_config_readonly().get('agent') or {}).get('model_router') == 'cortados-router'
    except ImportError:
        # Standalone policy readers do not have an installed Hermes host.
        return False


def _execution_admission(request, next_call, *, turn_id='', api_request_id='', provider='', model='', **_):
    if not _required_routing_enabled():
        return next_call(request)
    from agent.agent_model_routing import admitted, validate_request, ModelRoutingError
    with _lock:
        receipt = (_pending.get(turn_id) or {}).get('execution_route')
    if not receipt or receipt.get('api_request_id') != api_request_id or (
            receipt['provider'], receipt['model']) != (provider, model):
        from hermes_cli.local_execution import LocalExecutionFailure
        return LocalExecutionFailure(message='Required model route is missing or changed; no model call was made.',
                                     reason='model_route_unavailable', retryable=True, origin='cortados_router')
    from types import SimpleNamespace
    try:
        validate_request(SimpleNamespace(provider=provider, model=model), request, receipt)
    except ModelRoutingError as exc:
        from hermes_cli.local_execution import LocalExecutionFailure
        return LocalExecutionFailure(message=str(exc), reason='model_route_unavailable',
                                     retryable=True, origin='cortados_router')
    with admitted(receipt):
        return next_call(request)


NATIVE_RUNTIME = {"openai-codex": "codex", "anthropic": "claude", "google": "agy", "gemini": "agy"}   # Hermes-native providers → ledger runtime
PTY_PROVIDERS = {"claude-cli", "codex-cli", "agy-cli", "cursor-cli", "custom"}                       # already recorded by the PTY runner


def _api_request_error(turn_id='', api_request_id='', reason='', **_):
    if reason not in {'auth', 'auth_permanent', 'billing', 'rate_limit', 'upstream_rate_limit',
                      'overloaded', 'server_error', 'timeout', 'model_not_found'}:
        return
    with _lock:
        slot = _pending.get(turn_id) or {}
        route = slot.get('execution_route')
        if route and route.get('api_request_id') == api_request_id:
            pair = [route['provider'], route['model']]
            failures = slot.setdefault('failed_routes', [])
            if pair not in failures:
                failures.append(pair)


def _post_api_request(turn_id: str = "", session_id: str = "", platform: str = "", model: str = "", provider: str = "", usage=None, api_duration: float = 0.0,
                      message_count: int = 0, api_request_id: str = "", response_model: str = "", **_) -> None:
    if not turn_id:
        return
    if response_model == 'cortados-local-context-guard':
        # Hermes delivers a local execution short-circuit through response hooks.
        # It is neither a provider sample nor independent-review authorship.
        _slot(turn_id, session_id, platform, model)['context_blocked'] = True
        return
    u = usage or {}
    if provider and provider not in PTY_PROVIDERS and u:      # native-provider calls (Codex today) reach the ledger here; unit-cost learning needs them
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("cortados_usage", str(_pty_plugin_dir() / "usage.py")); um = _ilu.module_from_spec(spec); spec.loader.exec_module(um)
            um.record({"job_id": f"native-{turn_id[:12]}", "runtime": NATIVE_RUNTIME.get(provider, provider), "account": "subscription", "model": model, "session_id": session_id,
                       "input_tokens": u.get("prompt_tokens") or u.get("input_tokens"), "output_tokens": u.get("completion_tokens") or u.get("output_tokens"),
                       "cache_read_tokens": u.get("cache_read_tokens") or u.get("cached_tokens") or 0, "cache_write_tokens": u.get("cache_write_tokens") or 0,
                       "source": f"hermes:{provider}", "label": f"brain:{NATIVE_RUNTIME.get(provider, provider)}:{model}", "dedup_key": f"api:{api_request_id or turn_id + ':' + str(time.time())}"})
        except Exception as exc:
            logger.debug("native usage not recorded: %s", exc)
    slot = _slot(turn_id, session_id, platform, model)
    selected = slot.get('execution_route')
    if selected and selected.get('api_request_id') == api_request_id:
        core.log_execution(selected['route_id'], provider, model)
        slot['executed_route'] = selected if (selected['provider'], selected['model']) == (provider, model) else None
    try:
        reviews.record_author(core.STATE, turn_id, session_id, _profile_name(), model, provider)
    except Exception as exc:
        logger.warning("router author provenance not recorded: %s", exc)
    if slot.get("review"):
        expected = slot["review"]["chosen"]["model"]
        try:
            matches = reviews.model_row(core, model)["family"] == reviews.model_row(core, expected)["family"]
        except ValueError:
            matches = False
        if not matches:
            slot["review_error"] = f"Review route changed from {expected} to {model}; this is not a valid independent review. Request a retry."
    slot["calls"].append({"model": model, "provider": provider, "prompt_tokens": int(u.get("prompt_tokens") or 0),
                                                                   "s": round(api_duration or 0, 1), "messages": message_count})


LANE_RUNTIME = {"claude": "claude", "codex": "codex", "antigravity": "agy", "cursor": "cursor"}   # router lane → PTY worker runtime


def _pre_tool_call(tool_name: str = "", args=None, session_id: str = "", turn_id: str = "", **_):
    """Sub-agents follow the router (Forest 2026-09-19, asked again 2026-09-26): when a face launches a worker with agent_run, the SUB-TASK
    prompt is scored and routed like any request and the worker's runtime/model are set to the winner. The face still decides WHAT to delegate
    and WHEN; the router decides WHO runs it. `pin_model: true` in the call keeps the face's own choice. Degraded scorer → untouched."""
    slot = _pending.get(turn_id) or _pending.get(_by_session.get(session_id, "")) or {}
    if slot.get("review_error"):
        return {"action": "block", "message": slot["review_error"]}
    if slot.get("review") and tool_name == "router_review" and (args or {}).get("action") == "request":
        return {"action": "block", "message": "Submit your independent review; do not recursively request reviewers."}
    if slot.get("review") and tool_name in {"agent_run", "delegate_task"}:
        return {"action": "block", "message": "Complete this independent review on your assigned model; do not delegate it to a different author."}
    if tool_name == "bus_post" and isinstance(args, dict) and args.get("to") in {"rachel", "samantha"} and args.get("tag") in {"ask", "assign"} and re.search(r"\breview\b", args.get("text", ""), re.I):
        return {"action": "block", "message": "For an explicitly requested review use router_review once: it sends BOTH roles with verified original-author routing. Ordinary tasks need no review."}
    if _required_routing_enabled():
        # The runner is the enforceable spawn boundary, including Python callers.
        return None
    if tool_name != "agent_run" or not isinstance(args, dict) or args.get("pin_model"):
        return None
    try:
        prompt = str(args.get("prompt") or "")
        label = str(args.get("label") or "").lower()
        # a credential / liveness probe tests ONE specific lane: rerouting it defeats the test (2026-09-27: auth-freshness's Codex check landed on Antigravity)
        if len(prompt.strip()) < 8 or re.search(r"credential|freshness|liveness|health.?check|lane.?check", label) or re.match(r"\s*reply (with exactly|ok|with the single word)", prompt, re.I):
            return None
        decision = core.decide_s1(prompt, "background")
        if not decision or decision["degraded"]:
            return None
        route = core.resolve(decision, "background", history_chars=0, escalate=_esc_for(session_id))
        ch = route.get("chosen") or {}
        rt = LANE_RUNTIME.get(ch.get("lane"))
        if not rt or not ch.get("model"):
            return None
        sub_id = f"sub:{(turn_id or session_id or 'x')[:24]}:{int(time.time())}"
        core.log_route(sub_id, {"session_id": session_id, "platform": "subagent", "unit_kind": "background", "actual_model": f"{rt}/{ch['model']}",
                                "request": prompt[:8000], "history_msgs": 0}, decision, route)
        logger.info("router sub-agent: %s → %s/%s (%s, index %s ≥ %s) was %s/%s", sub_id, rt, ch["model"], route["band"], ch.get("index"), route.get("required_index"),
                    args.get("runtime"), args.get("model"))
        return {"action": "modify", "args": {"runtime": rt, "model": ch["model"]}}
    except Exception as exc:
        logger.warning("router sub-agent: left as called: %s", exc)
        return None


_FILE_KEYS = ("path", "file_path", "filename", "file", "target", "target_file")


def _esc_cfg() -> dict:
    try:
        return json.loads(core.BUDGETS_PATH.read_text()).get("escalation") or {}
    except Exception:
        return {}


def _esc_for(session_id: str) -> dict | None:
    """The session's live escalation (router_escalate or trajectory), or None once its TTL has passed."""
    if not session_id:
        return None
    with _lock:
        sc_ = _session_ctx.get(session_id, {})
        e = sc_.get("escalate")
    if e and time.time() - float(e.get("ts") or 0) < float(_esc_cfg().get("ttl_s", 7200)):
        return e
    return None


def _set_escalation(session_id: str, d_floor: int, reason: str, source: str) -> dict:
    e = {"D_floor": int(d_floor), "reason": reason[:200], "source": source, "ts": time.time()}
    with _lock:
        cur = (_session_ctx.setdefault(session_id, {})).get("escalate") or {}
        if not cur or time.time() - float(cur.get("ts") or 0) >= float(_esc_cfg().get("ttl_s", 7200)) or int(cur.get("D_floor") or 0) <= e["D_floor"]:
            _session_ctx[session_id]["escalate"] = e
        else:
            e = cur
    return e


def _post_tool_call(tool_name: str = "", args=None, session_id: str = "", turn_id: str = "", **_) -> None:
    try:
        tools = (json.loads(core.BUDGETS_PATH.read_text()).get("stickiness") or {}).get("browser_tools") or []
        if session_id and any(t in (tool_name or "") for t in tools):
            with _lock:
                _session_ctx.setdefault(session_id, {})["browser_ts"] = time.time()
    except Exception:
        pass
    # trajectory signal (Forest 2026-09-29): a turn that keeps calling tools / touching files is bigger than its request read. Past the
    # budgets.json thresholds the session is escalated (D floored) for its next calls and sub-agents — the scorer cannot see the codebase.
    try:
        if session_id:
            cfg = _esc_cfg()
            tk = turn_id or _by_session.get(session_id, "")
            with _lock:
                tr = _session_ctx.setdefault(session_id, {}).setdefault("traj", {"turn": tk, "calls": 0, "files": set()})
                if tr.get("turn") != tk:
                    tr.update({"turn": tk, "calls": 0, "files": set()})
                tr["calls"] += 1
                a = args if isinstance(args, dict) else {}
                for k in _FILE_KEYS:
                    if isinstance(a.get(k), str) and a[k].strip():
                        tr["files"].add(a[k].strip())
                calls, files = tr["calls"], len(tr["files"])
            if cfg and (calls == int(cfg.get("tool_calls", 25)) or files == int(cfg.get("files_touched", 8))) and not _esc_for(session_id):
                why = f"{calls} tool calls, {files} files touched this turn"
                _set_escalation(session_id, int(cfg.get("D_floor_trajectory", 9)), why, "trajectory")
                logger.info("router escalation (trajectory): %s: %s", session_id[:24], why)
    except Exception as exc:
        logger.debug("router trajectory: %s", exc)
    if tool_name != "agent_run":
        return
    slot = _pending.get(_by_session.get(session_id, ""))
    if slot is not None:
        a = args or {}
        slot["workers"].append(f"{a.get('runtime', '?')}/{a.get('model') or 'default'}")


def _actual_line(slot: dict) -> str:
    calls = slot.get("calls") or []
    models = sorted({c["model"] for c in calls}) or [slot.get("model") or "?"]
    bits = ["answered by " + " + ".join(models) + (f" ({calls[0]['provider']})" if calls else "")]
    if calls:
        bits.append(f"{len(calls)} model call{'s' if len(calls) != 1 else ''}")
        ctx = max(c["prompt_tokens"] for c in calls)
        if ctx:
            bits.append(f"context {ctx / 1000:.0f}k tokens")
    if slot.get("workers"):
        from collections import Counter
        bits.append("workers: " + ", ".join(f"{w} ×{n}" if n > 1 else w for w, n in Counter(slot["workers"]).items()))
    return "-# " + " · ".join(bits)


def _context_digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _pre_api_request(session_id: str = "", conversation_history=None, system_prompt=None,
                     request=None, turn_id: str = "", api_request_id: str = "",
                     model: str = "", provider: str = "", **_) -> None:
    """Observe the assembled envelope on every tool-loop call; never mutate it."""
    if not session_id:
        return
    payload = request if isinstance(request, dict) else {}
    envelope = transport.encoded_size(system_prompt or '') + transport.encoded_size(payload.get('tools') or [])
    with _lock:
        _session_ctx.setdefault(session_id, {}).update(
            history_bytes=transport.history_size(conversation_history), envelope_bytes=envelope)
        slot=_pending.get(turn_id)
        origin=dict(slot.get('routing_input') or {}) if slot and slot.get('session_id')==session_id else {}
    # Native identity was supplied separately at the turn boundary. Never
    # recover it from prompt text, another turn or the newest channel message.
    if not origin or not api_request_id or not origin.get('native_input'):
        return
    try:
        snapshot=_task_for_router(session_id,origin['face'],native_input=origin['native_input'])
        digest=_context_digest(snapshot)
        with _lock:
            if _pending.get(turn_id) is not slot or slot.get('routing_input')!=origin:
                return
            baseline=slot.get('scored_context_digest')
            status='score_pending' if baseline is None else ('unchanged' if baseline==digest else 'changed')
            seen=slot.setdefault('call_context_digests',set())
            if digest not in seen:
                if len(seen)<64:seen.add(digest)
                else:slot['routing_context_incomplete']=True
            if status=='changed':slot['routing_context_changed']=True
            slot['current_task_context']=snapshot
        core.log_call_context(turn_id,api_request_id,session_id=session_id,model=model,provider=provider,
            task_context=snapshot,scored_digest=baseline,current_digest=digest,status=status)
    except Exception as exc:
        with _lock:
            if _pending.get(turn_id) is slot:slot['routing_context_incomplete']=True
        logger.debug('router call context unavailable: %s',type(exc).__name__)


def _dispatch_fit(gateway, key, current, recall=''):
    from hermes_constants import get_hermes_home
    with _lock:
        cache = {sid: dict(value) for sid, value in _session_ctx.items()}
    return transport.measure(gateway, key, current, recall, cache,
                             Path(get_hermes_home()), core._context_cfg())


def _pre_llm_call(session_id: str = "", turn_id: str = "", user_message=None, conversation_history=None, model: str = "",
                  platform: str = "", incoming_message=None, **_) -> None:
    current_request = _text(user_message)
    raw = current_request.strip()
    if not turn_id or not raw or raw.startswith("[IMPORTANT: Background process"):
        return None
    text = core.clean_request(raw)      # the scorer and the training set see the same thing: the request, not the gateway's wrapper
    if not text:
        return None
    hist = conversation_history or []
    unit_kind = "interactive" if (platform or "").lower() in FOOTER_PLATFORMS else "background"
    if session_id:
        with _lock:
            _session_ctx.setdefault(session_id, {})["history_bytes"] = transport.history_size(hist)
    slot = _slot(turn_id, session_id, platform, model)
    marker = reviews.MARKER.search(text)
    if marker:
        with _lock:
            review = _review_pending.pop(marker.group(0), None)
        if review:
            slot["review"] = review
            slot["done"].set()
            return None  # Never rescore a constrained reviewer as an ordinary author task.
    slot["routing"] = True
    try:
        face=_profile_name()
        native_input=({'platform':platform,'message_id':incoming_message['platform_message_id']}
            if isinstance(incoming_message,dict) and incoming_message.get('role')=='user'
            and incoming_message.get('platform_message_id') and platform=='discord' else None)
        with _lock:slot['routing_input']={'face':face,'native_input':native_input}
    except Exception:
        with _lock:slot['routing_input']={}
    with _lock:
        for k in [k for k, v in _pending.items() if time.time() - v["t0"] > 3600]:
            _pending.pop(k, None)

    if _required_routing_enabled():
        # The authoritative pass runs after Context has assembled the request.
        # Do not run a second, conflicting shadow decision for that same turn.
        slot['done'].set()
        return None

    def work() -> None:
        try:
            face = _profile_name()
            ctx = {"history_messages": len(hist), "history_chars": sum(len(_text(m.get("content"))) for m in hist if isinstance(m, dict)),
                   "platform": platform, "unit_kind": unit_kind}
            # the live pass (pre_gateway_dispatch) already scored and routed this very request: reuse it, so the log, the footer and the
            # model that actually answered agree (2026-09-29: two scorer calls on one turn disagreed — "planning"/Astra live, "writing" logged)
            # the gateway's event text and the turn's user_message are wrapped differently (reply quotes, triggering-id notes), so match the
            # live record on a normalised head of the request rather than equality (2026-10-01: the 06:59 turn missed its live record)
            head = re.sub(r"\s+", " ", text)[:80]
            image_marker = image_conversion.ROUTE_MARKER.search(raw)
            hermes_input = _optional_module("context.hermes_input", "task lookup gets the full turn text, not the projected current utterance")
            recall_query = hermes_input.recall_query if hermes_input is not None else (lambda text: text)
            native_input = ({'platform': platform, 'message_id': incoming_message['platform_message_id']}
                            if isinstance(incoming_message, dict) and incoming_message.get('platform_message_id')
                            and incoming_message.get('role') == 'user'
                            and platform == 'discord' else None)
            current_task_context = _task_for_router(session_id, face, current_request=recall_query(current_request),
                                                    native_input=native_input)
            with _lock:
                # Images carry a unique turn identity through gateway wrappers. Never match
                # two captionless photos by their identical boilerplate or request prefix.
                live = _image_routes.get(image_marker.group(1)) if image_marker else next((v for v in _live_by_session.values() if v.get("face") == face and time.time() - float(v.get("ts") or 0) < 180 and v.get("decision")
                             and (text in (v.get("text"), v.get("text_raw")) or head and head in re.sub(r"\s+", " ", v.get("text_raw") or ""))), None)
            if live and live.get("face") != face:
                live = None
            if live and live.get('task_context') != current_task_context:
                # Identical continuation text can belong to a different task
                # or a newer checkpoint. Never borrow its earlier decision.
                live = None
            req_text, ctx_slice = text, ""
            task_context = None
            if live:
                decision, route = live["decision"], live["route"]
                req_text = live.get("text") or text               # the transcribed request is what gets logged and labelled
                ctx_slice = live.get("context") or ""
                task_context = live.get('task_context')
            else:
                task_context = current_task_context
                ctx['task_context'] = task_context
                decision = core.decide(text, ctx)
                route = core.resolve(decision, unit_kind, history_chars=transport.history_size(hist) + len(raw.encode('utf-8')),
                                     escalate=_esc_for(session_id))
            with _lock:
                baseline=_context_digest(task_context)
                slot['scored_context_digest']=baseline
                if any(digest!=baseline for digest in slot.get('call_context_digests',())):
                    slot['routing_context_changed']=True
            if not ctx_slice:                                     # every logged request carries its context slice (scorer-with-context labels)
                ctx_slice = _recall_for_router(session_id, text, face, _IMG_RE.findall(raw))
            core.log_route(turn_id, {"session_id": session_id, "platform": platform, "unit_kind": unit_kind, "actual_model": model,
                                     "request": req_text[:8000], "history_msgs": len(hist), "context": ctx_slice or None,
                                     "task_context": task_context}, decision, route)
            slot["decision"], slot["route"] = decision, route
        except Exception as exc:  # shadow mode must never hurt a turn
            logger.warning("cortados-router: decision failed: %s", exc)
        finally:
            slot["done"].set()

    # Hermes keeps the active face/home in ContextVars. Plain threads lose that
    # scope and can recall as the process's default face in a multiplexed gateway.
    scope = copy_context()
    threading.Thread(target=scope.run, args=(work,), name=f"router-{turn_id[:8]}", daemon=True).start()
    return None  # shadow mode injects nothing into the turn


_SILENCE_FALLBACK = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY", "[NO_REPLY]", "[静默]", "静默", "[沉默]", "沉默"})
_ACK_RE = re.compile(r"^(ok(ay)?|noted|ack(nowledged)?|done|got it|understood|👍|✅)[.!]?$", re.I)


def _is_silence(text: str) -> bool:
    """Hermes's intentional-silence markers (exact, bracketed, or the guard's rewrite) and bare one-word acknowledgements: no footer."""
    s = (text or "").strip()
    if not s:
        return True
    try:
        from gateway.response_filters import LIVE_GATEWAY_SILENT_MARKERS, is_partial_silence_marker
        markers = set(LIVE_GATEWAY_SILENT_MARKERS) | _SILENCE_FALLBACK
        if is_partial_silence_marker(s):
            return True
    except Exception:
        markers = _SILENCE_FALLBACK
    core_txt = s.strip("[]() \t`*_").upper()
    return s in markers or core_txt in {m.strip("[]").upper() for m in markers} or (len(s) <= 12 and bool(_ACK_RE.match(s)))


def _transform_llm_output(response_text: str = "", turn_id: str = "", platform: str = "", **_):
    slot = _pending.get(turn_id)
    if slot and slot.get("review_error"):
        r = slot["review"]
        reviews.mark(core, r["request_id"], r["role"], "blocked", slot["review_error"])
        return "Review blocked: " + slot["review_error"]
    plat = (platform or (slot or {}).get("platform") or "").lower()
    if not slot or plat not in FOOTER_PLATFORMS or not response_text:
        logger.info("router footer: skipped turn %s (slot=%s platform=%r chars=%d)", turn_id[:8], bool(slot), plat, len(response_text or ""))
        return None
    if _env_get("CORTADOS_ROUTER_FOOTER", "1") != "1":
        logger.info("router footer: disabled by CORTADOS_ROUTER_FOOTER")
        return None
    if _is_silence(response_text):
        # a silence marker must reach the gateway untouched: with a footer glued on, Hermes no longer recognises it and POSTS it, and on
        # Discord that pings the peer face, which answers … (Hermes Agents 2026-09-29: four Harvey↔Jessica rounds of "NO_REPLY -# answered by")
        logger.info("router footer: skipped turn %s (silence marker)", turn_id[:8])
        return None
    lines = [_actual_line(slot)]                      # always: what really produced this reply
    if slot.get("review"):
        r = slot["review"]
        reviews.mark(core, r["request_id"], r["role"], "reviewed")
        return response_text.rstrip() + "\n\n" + lines[0] + f"\n-# requested review: {r['role']} · author {r['author']['model']} → {r['chosen']['model']} · {r['request_id']}"
    execution = slot.get('executed_route')
    if execution:
        state = 'degraded policy' if execution['degraded'] else 'trained scorer'
        return (response_text.rstrip() + '\n\n' + lines[0] +
                f"\n-# router LIVE: {execution['band']} → {execution['provider']}/{execution['model']} ({state}; assembled input checked)")
    live = None
    with _lock:
        for k, v in list(_live_by_session.items()):
            if time.time() - v["ts"] > 1800:
                _live_by_session.pop(k, None)
    sid = slot.get("session_id") or ""
    live = next((v for k, v in _live_by_session.items() if sid and (k in sid or sid in k)), None)
    if live is None and _live_recent and _live_recent["ts"] >= slot["t0"] - 10:
        live = _live_recent          # the gateway session key and the agent's session id never contain each other; this plugin instance is
                                     # one profile, so the override applied just before this turn started is this turn's (2026-09-29 fix)
    if live:
        lines.append(f"-# router LIVE: {live['band']} → {live['provider']}/{live['model']} (index {live['index']}, needs {live['required']} for {live['use_case']}, decided in {live['ms']} ms)")
    if slot.get("routing"):
        slot["done"].wait(FOOTER_WAIT_S)
        lines.append(core.footer(slot["route"], slot["decision"]) if "route" in slot
                     else "-# router (shadow): still scoring this one, the verdict lands in `/router`")
    logger.info("router footer: appended to turn %s (%s, %d chars, live=%s)", turn_id[:8], plat, len(response_text), bool(live))
    return response_text.rstrip() + "\n\n" + "\n".join(lines)


def _post_llm_call(turn_id: str = "", assistant_response=None, **_) -> None:
    slot = _pending.get(turn_id)
    if not slot:
        return

    def finish() -> None:
        slot["done"].wait(core.DEADLINE_S + 5)
        try:
            if not any(slot.get(key) for key in ('context_blocked','routing_context_changed','routing_context_incomplete')):
                core.log_outcome(turn_id, len(_text(assistant_response)), int((time.time() - slot["t0"]) * 1000))
        except Exception as exc:
            logger.warning("cortados-router: outcome log failed: %s", exc)
        with _lock:
            _pending.pop(turn_id, None)

    threading.Thread(target=finish, daemon=True).start()


SUBSCRIPTION_USAGE_SCHEMA = {
    "name": "subscription_usage",
    "description": ("Read how much of every AI subscription is used, in the vendors' own units: for each plan window (Claude 5-hour + week, Codex week, "
                    "Cursor month, Antigravity 5-hour + week) the % used, the % of the window's time already elapsed, the pace (projected % at the reset), "
                    "the headroom left and the reset time. Figures come from each CLI's own usage screen read through a PTY; no tokens are spent. "
                    "There are NO dollar budgets in this system: judge usage only as % used versus % of time elapsed until the reset. "
                    "refresh=true re-reads the screens now (about 30 s); otherwise the last reading (at most ~20 min old) is returned."),
    "parameters": {"type": "object", "properties": {"refresh": {"type": "boolean", "description": "re-read the vendor screens now"},
                                                    "format": {"type": "string", "enum": ["text", "json"], "description": "default text"}}},
}


def _subscription_usage(args=None, **_) -> str:
    import json as _json
    args = args or {}
    sc = core._scarcity_module()
    d = sc.sample(force=True) if args.get("refresh") else sc.current()
    if not d.get("lanes"):
        d = sc.sample()
    if args.get("format") == "json":
        return _json.dumps(d)
    age = int((time.time() - float(d.get("fetched_at") or 0)) / 60)
    return f"subscription usage (read {age} min ago; units are % of each plan window, never dollars)\n" + "\n".join(sc.summary_lines(d))


ROUTER_ESCALATE_SCHEMA = {
    "name": "router_escalate",
    "description": ("Tell the model router that the task in hand is BIGGER than the request read — e.g. 'change the auth button' turned out to touch "
                    "most of the codebase once you mapped it. The router then treats the rest of this session and every sub-agent you launch as "
                    "hard work (scope 'larger' = R3, 'much_larger' = R4) and routes them to a stronger model. Your own current call is unaffected; "
                    "delegate the heavy parts with agent_run after calling this, or finish the turn so the next one is upgraded. "
                    "Call it once, when you find out; never for tasks that are merely long."),
    "parameters": {"type": "object", "required": ["reason"],
                   "properties": {"reason": {"type": "string", "description": "what you found (one sentence)"},
                                  "scope": {"type": "string", "enum": ["larger", "much_larger"], "description": "default larger"}}},
}


def _router_escalate(args=None, **kw) -> str:
    args = args or {}
    sid = str(kw.get("session_id") or kw.get("task_id") or "")
    if not sid:   # the registry did not hand us the session: take the turn most recently routed (one gateway process, turns are short)
        with _lock:
            live = sorted(((v.get("t0") or 0), v.get("session_id") or "") for v in _pending.values())
        sid = live[-1][1] if live else ""
    if not sid:
        return "router: no active session to escalate"
    cfg = _esc_cfg()
    floor = int(cfg.get("D_floor_much_larger", 11) if args.get("scope") == "much_larger" else cfg.get("D_floor_larger", 9))
    e = _set_escalation(sid, floor, str(args.get("reason") or "face reported a larger task"), "face")
    logger.info("router escalation (face): %s → D ≥ %s: %s", sid[:24], e["D_floor"], e["reason"])
    return (f"router: escalated — this session and its sub-agents are routed as {'R4' if e['D_floor'] >= 11 else 'R3'} work for the next "
            f"{int(float(cfg.get('ttl_s', 7200)) / 60)} min (D ≥ {e['D_floor']}). Delegate the heavy parts with agent_run now.")


def _task_for_router(session_id, face, gateway=None, *, current_request=None, native_input=None):
    """Keep gateway conversation keys distinct from persistent session IDs."""
    unavailable = _task_unavailable
    try:
        if gateway is not None:
            entry = gateway.session_store.lookup_by_session_key(session_id)
            session_id = getattr(entry, 'session_id', None) or session_id
        engine = _optional_module("context.hermes_engine", "no durable task context; the scorer sees the request alone")
        if engine is None:
            return unavailable()
        task_context_for_router = engine.task_context_for_router
        options = {'current_request': current_request} if current_request is not None else {}
        if native_input is not None:
            options['native_input'] = native_input
        return task_context_for_router(session_id, face=face or 'default', **options)
    except Exception:
        return unavailable()


def _recall_for_router(session_id: str, text: str, face: str | None, images: list[str]) -> str:
    """The engine's model-free recall slice for the scorer (Forest 2026-09-30: judge difficulty WITH what memory knows). FTS + graph seeds,
    ≤ ~600 tokens, cached by the engine so the in-turn prefetch reuses it. Empty until context.hermes_engine.recall_for_router exists (P14)."""
    try:
        engine = _optional_module("context.hermes_engine", "no recall slice; the scorer sees the request alone")
        if engine is None:
            return ""
        recall_for_router = engine.recall_for_router
    except Exception:
        return ""
    try:
        return str(recall_for_router(session_id, text, face=face, budget_tokens=600, image_paths=images or None) or "")[:4000]
    except Exception as exc:
        logger.debug("router: recall_for_router failed: %s", exc)
        return ""


LIVE_PLATFORMS = {"discord", "telegram", "cli"}
_ATTACH_RE = re.compile(r"\[(?:Image|File|Attachment|Document|Audio|Video) attached|sent a message with no text content", re.I)
_IMG_RE = re.compile(r"\[Image attached at: ([^\]]+)\]")
_TRANSCRIPTS_DIR = Path(os.path.expanduser("~/.cortados/router/transcriptions"))


def _vision_config() -> dict:
    from hermes_cli.config import load_config
    return dict((load_config().get("auxiliary") or {}).get("vision") or {})


def _live_enabled() -> bool:
    if _env_get("CORTADOS_ROUTER_LIVE", "1") != "1":
        return False
    try:
        return bool((core.table().get("tiering") or {}).get("live"))
    except Exception:
        return False


def _pre_gateway_dispatch(event=None, gateway=None, **_):
    """LIVE routing (Forest 2026-09-22: faces follow the router per turn). Before the turn is dispatched: score the request with the trained
    scorer (≈1 s; the slow 14B is never used here), resolve with live scarcity, and apply the winner as a ONE-TURN model override, the same
    mechanism `/model --once` and `/moa` use. Anything missing or slow → no override, the face runs on its configured brain."""
    if event is None or gateway is None:
        return None
    if not _live_enabled():
        if reviews.MARKER.search(str(getattr(event, "text", "") or "")):
            return {"action": "skip", "reason": "Review routing is disabled; do not use a fixed default"}
        return None
    review_marker = False
    ticket = None
    try:
        src = getattr(event, "source", None)
        platform = str(getattr(getattr(src, "platform", None), "value", "") or "").lower()
        if platform not in LIVE_PLATFORMS:
            return None
        raw = str(getattr(event, "text", "") or "")
        current_request = raw  # Full human input before image conversion/scorer cleanup.
        images = image_conversion.attached_images(event)
        if raw.lstrip().startswith("/") or raw.startswith("[IMPORTANT: Background process"):
            return None
        # The gateway hook precedes authorization: do not run vision for unauthorized media.
        authorize = getattr(gateway, "_is_user_authorized_for_source", None)
        if images and callable(authorize) and not authorize(src):
            return None
        image_token, converted = None, []
        if images:
            import uuid
            image_token = uuid.uuid4().hex
            converted = image_conversion.convert(images, _vision_config(), _TRANSCRIPTS_DIR, request_text=raw)
            raw = image_conversion.apply_conversion(event, converted, image_token)
            logger.info("router live: converted %d/%d image(s) before scoring; vision models=%s; outcomes=%s",
                        sum(r["complete"] for r in converted), len(images),
                        [r.get("attempts") for r in converted], [r.get("outcomes") for r in converted])
        review_marker = bool(reviews.MARKER.search(raw))
        ticket = reviews.ticket_for(core, raw, _profile_name(), getattr(src, "user_id", ""))
        if ticket:
            key = gateway._session_key_for_source(src)
            fit = _dispatch_fit(gateway, key, raw)
            # Reviewer selection has the same exact identity and current-input
            # accounting. The final runner byte guard still covers every call.
            history = max(fit['history_bytes'] + fit['current_and_recall_bytes'],
                          int(fit['input_tokens'] / float(core._context_cfg().get('headroom', 1.2)) * 4)
                          - int(core._context_cfg().get('harness_tokens', 31000)) * 4)
            choice = reviews.plan(core, ticket["author"], ticket["use_case"], history_chars=history,
                                  roles=(ticket["role"],), locked_routes=ticket["reviewers"])["reviewers"][ticket["role"]]
            if (fit['input_tokens'] > core.lane_ceiling(choice['lane'], choice['model']) or
                    choice['lane'] == 'antigravity' and (not fit['known'] or fit['input_bytes'] > 180000)):
                raise ValueError('Selected reviewer transport cannot carry the intact request')
            override = _override_for(choice["provider"], choice["model"])
            if not override:
                raise ValueError("Selected reviewer model has no runtime override; refusing default-model review")
            try:
                reviews.claim(core, ticket["request_id"], ticket["role"], getattr(src, "message_id", ""))
            except ValueError as exc:
                return {"action": "skip", "reason": str(exc)}
            gateway._claim_one_turn_restore(key)
            gateway._session_state(key).conversation.model_override = override
            gateway._evict_cached_agent(key)
            marker = f"[cortados-review:{ticket['request_id']}:{ticket['role']}]"
            with _lock:
                _review_pending[marker] = dict(ticket, chosen=choice)
            logger.info("router review: %s %s author=%s reviewer=%s", ticket["request_id"], ticket["role"], ticket["author"]["model"], choice["model"])
            return None
        if not raw.strip() or raw.lstrip().startswith("/") or raw.startswith("[IMPORTANT: Background process"):
            return None
        text = core.clean_request(raw)
        if not text:
            return None
        # Conversion is complete before this separate task-routing pass, irrespective of
        # caption length, image count or the answerer's own multimodal capabilities.
        key = gateway._session_key_for_source(src)
        text_raw, transcribed = text, bool(converted) and all(r["complete"] for r in converted)
        # Durable state has its own versioned scorer input. General recall stays
        # separately logged; it is not confused with the bound task's briefing.
        # `agent:main` is a gateway namespace, not a memory face. Resolve the
        # trusted turn-local home against the roster; unmapped homes stay denied.
        # Keep the key itself intact for Context's session/cache lookup.
        face = _profile_name()
        native_input = ({'platform': platform, 'message_id': event.message_id}
                        if platform == 'discord' and getattr(event, 'message_id', None) else None)
        orientation=getattr(gateway,'cortados_task_orientation',None)
        oriented=None
        if orientation is not None:
            # A configured phase must finish before scoring or model override.
            # Missing/failed interpretation never silently borrows the old task.
            try:
                oriented=orientation(event=event,gateway=gateway,face=face,conversation_key=key)
                if not isinstance(oriented,dict) or oriented.get('status')!='selected':
                    return {'action':'skip','reason':'Current task selection is incomplete; the retained input requires preparation retry.'}
            except Exception:
                return {'action':'skip','reason':'Current task selection is unavailable; the retained input requires preparation retry.'}
        task_context = _task_for_router(key, face, gateway, current_request=current_request, native_input=native_input)
        if oriented is not None:
            selected=oriented.get('task_context') or {}
            current=task_context or {}
            if not isinstance(selected,dict) or not isinstance(current,dict):
                return {'action':'skip','reason':'Current task selection is invalid; the retained input requires preparation retry.'}
            chosen=selected.get('selection_for_input') or {};checked=current.get('selection_for_input') or {}
            if (not isinstance(chosen,dict) or not isinstance(checked,dict)
                    or current.get('status')!='bound' or checked.get('status')!='source_cited'
                    or not chosen.get('selection_id') or chosen.get('selection_id')!=checked.get('selection_id')
                    or selected.get('task_id')!=current.get('task_id') or selected.get('version')!=current.get('version')
                    or not native_input):
                return {'action':'skip','reason':'Current task selection changed before scoring; the retained input requires preparation retry.'}
        ctx_slice = _recall_for_router(key, text_raw, face, images)
        if _required_routing_enabled():
            # Select usable initialization credentials; exact fit is decided
            # again after Context assembly, before the first provider request.
            from cortados_router.execution_routing import select
            import uuid
            selected = select(core, prompt=text, session_id=key, face=face, platform=platform,
                              entrypoint='gateway-preconstruction', task_context=task_context,
                              recall=ctx_slice, api_request_id='gateway-'+uuid.uuid4().hex,
                              constraints={'required_capabilities':['vision'], 'min_band':'R3'} if images and not transcribed else None)
            if selected.get('error'):
                return {'action':'skip','reason':selected['error']}
            override = _override_for(selected['provider'], selected['model'])
            if not override:
                return {'action':'skip','reason':'Required model route could not initialize its provider.'}
            gateway._claim_one_turn_restore(key)
            gateway._session_state(key).conversation.model_override = override
            gateway._evict_cached_agent(key)
            _set_window_hint(key, model=selected['model'], context_length=selected['context_length'])
            return None
        fit = _dispatch_fit(gateway, key, raw, ctx_slice)
        options = {'task_context': task_context} if task_context is not None else {}
        decision = core.decide_s1(text, "interactive", **options)
        if not decision or decision["degraded"]:
            # scorer down: route by SCARCITY alone in the fallback band (Forest 2026-09-29: a down scorer must not default to Claude)
            logger.info("router live: scorer degraded (%s); routing by scarcity in the fallback band", (decision or {}).get("error"))
            decision = {**(decision or {}), "answers": None, "degraded": True, "engine": "s1", "engine_ms": (decision or {}).get('engine_ms', 0)}
        with _lock:
            sc_ = dict(_session_ctx.get(fit['session_id'], {}))
            prev = _live_by_session.get(key) or {}
        pinned = time.time() - float(sc_.get("browser_ts") or 0) < float((json.loads(core.BUDGETS_PATH.read_text()).get("stickiness") or {}).get("pin_ttl_s", 7200))
        sticky = {"lane": prev.get("lane"), "model": prev.get("model"), "band": prev.get("band"), "pinned": pinned} if prev else None
        esc = _esc_for(fit['session_id'])
        # image-only message: the scorer sees "(The user sent a message with no text content) [Image attached …]" and calls it R0 chit-chat —
        # RC28 answered a 20-question economics quiz on gpt-5.6-luna that way (2026-09-29). Unknown content is not trivial content: floor it.
        if (images and not transcribed) or (_ATTACH_RE.search(raw) and len(text_raw) < 200 and not transcribed):
            floor = int(_esc_cfg().get("D_floor_attachment", 7))
            if not esc or int(esc.get("D_floor") or 0) < floor:
                esc = {"D_floor": floor, "reason": "attachment conversion incomplete; missing content explicitly marked", "source": "attachment", "ts": time.time()}
        route = core.resolve(decision, "interactive", history_chars=fit['history_bytes'],
                             input_fit=fit, sticky=sticky, escalate=esc)
        logger.info("router fit: session=%s known=%s history_bytes=%s input_bytes=%s input_tokens=%s reason=%s",
                    fit['session_id'], fit['known'], fit['history_bytes'], fit['input_bytes'], fit['input_tokens'], fit['reason'])
        if route.get('fit_blocked'):
            # This pre-admission estimate can include history the engine will
            # leave out. Do not drop an unpersisted user request or clip it here.
            # Keep Hermes's configured brain and normal assembly/preflight;
            # the runner must validate the final bytes on every AGY call.
            logger.warning('router fit: no candidate fits conservative estimate; retaining host assembly/preflight')
            return None
        ch = route.get("chosen") or {}
        provider, model = ch.get("provider"), ch.get("model")
        if not provider or not model or ch.get("lane") == "local" and route["band"] != "R0":
            return None
        override = _override_for(provider, model)
        if not override:
            logger.info("router live: no override for %s/%s", provider, model)
            return None
        gateway._claim_one_turn_restore(key)
        gateway._session_state(key).conversation.model_override = override
        gateway._evict_cached_agent(key)
        # tell the context engine how big this turn's window is (Hermes Agents Context, 2026-09-30): it budgets recall to the routed lane's
        # ceiling (claude 200k / codex 100k / agy+cursor 200k) minus its own reserve, instead of the profile's model.context_length
        try:
            entry = gateway.session_store.lookup_by_session_key(key)
            sid = getattr(entry, "session_id", None)
            if sid:
                _set_window_hint(sid, model=override["model"], context_length=int(core.lane_ceiling(ch.get("lane"), model)))
        except Exception as exc:
            logger.debug("router live: window hint not sent: %s", exc)
        with _lock:
            _live_by_session[key] = {"provider": override["provider"], "model": override["model"], "lane": ch.get("lane"), "band": route["band"], "index": ch.get("index"),
                                     "required": route.get("required_index"), "use_case": ch.get("use_case"), "ms": decision["engine_ms"], "ts": time.time(),
                                     "text": text, "text_raw": text_raw, "decision": decision, "route": route, "context": ctx_slice,
                                     "task_context": task_context, "face": face}   # the shadow pass logs THIS decision instead of scoring again
            global _live_recent
            _live_recent = _live_by_session[key]
            if image_token:
                for old in [k for k, v in _image_routes.items() if time.time() - v["ts"] > 3600]:
                    _image_routes.pop(old, None)
                _image_routes[image_token] = _live_recent
        logger.info("router live: %s → %s/%s (%s, index %s ≥ %s, %s ms)", key, override["provider"], override["model"], route["band"], ch.get("index"), route.get("required_index"), decision["engine_ms"])
    except Exception as exc:
        logger.warning("router live: no override this turn: %s", exc)
        if review_marker:
            if ticket:
                reviews.mark(core, ticket["request_id"], ticket["role"], "blocked", str(exc))
            return {"action": "skip", "reason": f"Constrained review blocked: {exc}"}
    return None


CLI_RUNTIME = {"claude-cli": "claude", "codex-cli": "codex", "agy-cli": "agy", "cursor-cli": "cursor"}


def _override_for(provider: str, model: str) -> dict | None:
    """The one-turn override dict the gateway applies as runtime kwargs. Our CLI providers are plugin providers that hermes_cli.model_switch
    does not know (it refused every claude-cli / agy-cli / cursor-cli switch since 2026-09-24: only Codex ever moved); build theirs directly
    from the provider plugin's contract (acp:// base_url, api_mode chat_completions). Native providers go through switch_model."""
    if provider in CLI_RUNTIME:
        rt = CLI_RUNTIME[provider]
        return {"model": model, "provider": provider, "api_key": f"{rt}-cli", "base_url": f"acp://cortados-{rt}", "api_mode": "chat_completions", "request_overrides": {}, "capabilities": {}}
    if provider == "custom":
        return None                                       # local ollama: the profile's custom provider config, not routed live for now
    try:
        from hermes_cli.config import load_config
        from hermes_cli.model_switch import switch_model
        cfg = load_config()
        res = switch_model(raw_input=model, current_provider=provider, current_model=model, explicit_provider=provider,
                           user_providers=cfg.get("providers") or {}, custom_providers=cfg.get("custom_providers") or [])
        if not res.success:
            logger.info("router live: switch to %s/%s refused: %s", provider, model, res.error_message)
            return None
        return {"model": res.new_model, "provider": res.target_provider, "api_key": res.api_key, "base_url": res.base_url, "api_mode": res.api_mode,
                "request_overrides": dict(res.request_overrides or {}), "capabilities": dict(res.runtime_capabilities or {})}
    except Exception as exc:
        logger.info("router live: switch_model failed for %s/%s: %s", provider, model, exc)
        return None


_live_by_session: dict[str, dict] = {}
_live_recent: dict | None = None
_session_ctx: dict[str, dict] = {}       # exact Hermes session_id → history/envelope bytes, browser state and escalation


def _slash_router(arg: str = "", **_) -> str:
    if arg.strip().split(" ")[0] in ("usage", "scarcity"):
        return _subscription_usage({"refresh": "refresh" in arg})
    rows = core.recent(int(arg) if arg.strip().isdigit() else 8)
    if not rows:
        return "router (shadow): no decisions logged yet"
    out = ["router (shadow) — last decisions:"]
    for x in rows:
        out.append(f"  {time.strftime('%H:%M', time.localtime(x['ts']))} {x['band']} → {x['lane']}/{x['model']} · sub={x['subagents']} · D={x['D']} · "
                   f"{x['engine_ms']} ms{' · DEGRADED' if x['degraded'] else ''} · ran on {x['actual_model']} | {x['request']}")
    return "\n".join(out)


def register(ctx) -> None:
    core.table()  # fail loudly at load if the table is unusable
    from cortados_router.cron_routing import route as route_cron
    ctx.register_hook("route_cron_model", lambda **kw: route_cron(core, **kw))
    try:
        ctx.register_hook("route_agent_model", _route_agent_model)
    except ValueError:
        # Existing processes/hosts can load the plugin before the durable host
        # patch is installed. Required hosts reject a missing hook at execution.
        logger.warning('Host needs required-routing patch before enabling agent.model_router')
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_middleware('llm_execution', _execution_admission)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("post_llm_call", _post_llm_call)
    try:
        ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    except Exception as exc:
        logger.warning("cortados-router: pre_gateway_dispatch not registered (live routing off): %s", exc)
    for name, fn in (("pre_api_request", _pre_api_request), ("post_api_request", _post_api_request), ("api_request_error", _api_request_error), ("post_tool_call", _post_tool_call), ("pre_tool_call", _pre_tool_call)):
        try:
            ctx.register_hook(name, fn)
        except Exception as exc:
            logger.warning("cortados-router: hook %s not registered: %s", name, exc)
    try:
        ctx.register_tool(name="router_review", toolset="cortados", schema=reviews.SCHEMA, handler=_router_review,
                          description=reviews.SCHEMA["description"], emoji="🔎")
    except Exception as exc:
        logger.warning("cortados-router: review tool not registered: %s", exc)
    try:
        ctx.register_tool(name="subscription_usage", toolset="cortados", schema=SUBSCRIPTION_USAGE_SCHEMA, handler=_subscription_usage,
                          description=SUBSCRIPTION_USAGE_SCHEMA["description"], emoji="📊")
    except Exception as exc:
        logger.warning("cortados-router: subscription_usage tool not registered: %s", exc)
    try:
        ctx.register_tool(name="router_escalate", toolset="cortados", schema=ROUTER_ESCALATE_SCHEMA, handler=_router_escalate,
                          description=ROUTER_ESCALATE_SCHEMA["description"], emoji="⬆️")
    except Exception as exc:
        logger.warning("cortados-router: router_escalate tool not registered: %s", exc)
    threading.Thread(target=lambda: core._scarcity_module().current(), daemon=True, name="cortados-scarcity-boot").start()
    try:
        ctx.register_command("router", _slash_router, description="shadow router: recent decisions · `/router usage` = subscription usage", args_hint="[n | usage [refresh]]")
    except Exception as exc:
        logger.warning("cortados-router: slash command not registered: %s", exc)
    logger.info("cortados-router: loaded (live=%s, table %s)", _live_enabled(), core.TABLE_PATH)
