"""The local-models switch, seen and flipped from the provider: GET /v1/cortados/local and POST
/v1/cortados/local/switch (owner token, server.py). Each configured local node (local_lane.nodes) is asked through its
own local gate (local_gate.py, GET / POST local_gate.SWITCH_PATH) with the node's gate token, so the provider never
writes a node's switch file itself: the gate on the node does, through local_resources.set_local_switch.

What reaches a client is metadata the gate's answer is checked against, field by field (STATUS_FIELDS): never the
gate's raw text, the node's address, its token or a file path. A node that does not answer is reported as unreachable
(asleep, offline or the gate is down), with the exception type only. Stdlib only.
"""
from __future__ import annotations

import http.client
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from cortados_router.provider import local_lane

logger = logging.getLogger("cortados.provider.local_switch")

VIEW_PATH = "/v1/cortados/local"
SWITCH_PATH = "/v1/cortados/local/switch"
# One bound for a whole gate call, end to end (connect, headers and body share one monotonic deadline:
# local_gate.deadline_call), so a gate trickling bytes cannot stretch it. The gate's status reads pmset (its own 2 s
# cap) and Ollama's /api/ps (2 s end to end), so a healthy node answers in well under 6 s; an asleep node fails at the
# connect timeout (the node's connect_timeout_s, never more than this bound).
GATE_TIMEOUT_S = 6.0
GATE_MAX_BYTES = 65536
# POST SWITCH_PATH's body is {"node": "<id>", "on": true|false}: 1 KiB is ample (a larger one is refused unread), and
# it must arrive whole within SWITCH_BODY_DEADLINE_S, end to end (server._control_body).
SWITCH_MAX_BODY = 1024
SWITCH_BODY_DEADLINE_S = 5.0
POWER = {"ac", "battery", "unknown"}
OLLAMA = {"up", "unreachable", "unreadable"}
REASON_MAX = 200
MODEL_MAX = 200
MODELS_MAX = 32


class SwitchError(Exception):
    """A switch call that did not happen: `status` is the provider's HTTP answer, the message is for the client."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _text(v: Any, cap: int) -> str | None:
    return v if isinstance(v, str) and 0 < len(v) <= cap and v.isprintable() else None


def _clean(data: Any) -> dict[str, Any]:
    """The gate's status answer, field by field: only known fields with the expected types and bounded printable text.
    Anything else is dropped (shown as unknown), never passed on as raw text."""
    d = data if isinstance(data, dict) else {}
    models = d.get("models_loaded")
    if isinstance(models, list):
        models = [m for m in (_text(x, MODEL_MAX) for x in models[:MODELS_MAX]) if m]
    else:
        models = None
    return {"switch": d.get("switch") if d.get("switch") in ("on", "off") else "unknown",
            "allowed": d.get("allowed") if isinstance(d.get("allowed"), bool) else None,
            "reason": _text(d.get("reason"), REASON_MAX),
            "power": d.get("power") if d.get("power") in POWER else "unknown",
            "training": d.get("training") if isinstance(d.get("training"), bool) else None,
            "ollama": d.get("ollama") if d.get("ollama") in OLLAMA else "unknown",
            "models_loaded": models}


def _gate_call(node: dict[str, Any], method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One request to the node's gate switch endpoint; the cleaned status, or SwitchError with a client-safe message."""
    nid = node["node"]
    if not node.get("token"):
        # Without a gate there is no switch endpoint; an unauthenticated request to a bare Ollama would be a 404 at best.
        raise SwitchError(409, f"local node {nid} has no local gate configured on the provider (token_file / "
                               "token_file_env in its ollama block); its switch can only be flipped on the node")
    conn = local_lane._connection(node["base_url"], node["connect_timeout_s"])
    raw = json.dumps(body).encode() if body is not None else None
    # The gate's own constant (local_lane imports it too) and its end-to-end deadline reader.
    from cortados_router.local_gate import SWITCH_PATH as gate_path, deadline_call

    def unreachable(exc: BaseException) -> None:
        logger.info("local switch: node %s unreachable: %s", nid, type(exc).__name__)
        raise SwitchError(503, f"local node {nid} unreachable ({type(exc).__name__}): it may be asleep or "
                               "offline, or its local gate is not running") from None

    try:
        try:
            status, data = deadline_call(conn, GATE_TIMEOUT_S, method, local_lane._prefix(node["base_url"]) + gate_path,
                                         body=raw, headers={"content-type": "application/json", **local_lane._auth(node)},
                                         max_bytes=GATE_MAX_BYTES, connect_s=node["connect_timeout_s"],
                                         on_connect_error=unreachable)
        except (OSError, http.client.HTTPException) as exc:
            logger.info("local switch: node %s gate did not answer: %s", nid, type(exc).__name__)
            raise SwitchError(503, f"the local gate on node {nid} accepted the connection but did not answer within "
                                   f"{GATE_TIMEOUT_S:g}s ({type(exc).__name__})") from None
    finally:
        conn.close()
    if status == 401:
        raise SwitchError(502, f"the local gate on node {nid} refused the provider's token (401): the provider's "
                               "token file does not match the gate's")
    if status == 404:
        raise SwitchError(502, f"the local gate on node {nid} has no switch endpoint (404): it runs an older "
                               "local_gate.py; restart the gate's LaunchAgent on the node with the new code")
    if status != 200:
        logger.warning("local switch: node %s gate answered HTTP %s", nid, status)
        raise SwitchError(502, f"the local gate on node {nid} answered HTTP {status}")
    try:
        parsed = json.loads(data)
    except ValueError:
        raise SwitchError(502, f"the local gate on node {nid} sent an unreadable answer") from None
    return _clean(parsed)


def _auto(node: dict[str, Any]) -> dict[str, Any] | None:
    a = node.get("auto_route")
    return {"model": local_lane.pin_id(a["model"]), "bands": list(a["bands"])} if a else None


def node_status(node: dict[str, Any], call: Callable = _gate_call) -> dict[str, Any]:
    """One node's row of the view: label, reachable, the gate's status fields and the node's auto_route setting."""
    row = {"node": node["node"], "label": node["label"], "auto_route": _auto(node),
           "models": [local_lane.pin_id(m) for m in node["models"]]}
    try:
        st = call(node, "GET")
    except SwitchError as exc:
        return {**row, "reachable": False, "error": str(exc), **_clean(None)}
    return {**row, "reachable": True, "error": None, **st}


def view(config_path: str | None = None, call: Callable = _gate_call) -> dict[str, Any]:
    """GET /v1/cortados/local: every configured local node, asked in parallel (each bounded by GATE_TIMEOUT_S).
    local_lane.LocalConfigError propagates: the server answers it like /v1/models does, without file paths."""
    nodes = local_lane.nodes(config_path)
    if not nodes:
        return {"nodes": [], "note": "no local node is configured on the provider (CORTADOS_LOCAL_RESOURCES_CONFIG)"}
    with ThreadPoolExecutor(max_workers=min(4, len(nodes))) as ex:
        rows = list(ex.map(lambda n: node_status(n, call), nodes))
    return {"nodes": rows}


def switch(node_id: Any, on: Any, config_path: str | None = None, call: Callable = _gate_call) -> dict[str, Any]:
    """POST /v1/cortados/local/switch {"node", "on"}: set the node's switch through its gate and return its new row.
    SwitchError(400) for a malformed body, (404) for a node that is not configured, else the gate call's errors."""
    if not isinstance(node_id, str) or not node_id.strip():
        raise SwitchError(400, 'body must be {"node": "<node id>", "on": true|false} (node missing)')
    if not isinstance(on, bool):
        raise SwitchError(400, 'body must be {"node": "<node id>", "on": true|false} ("on" must be true or false)')
    nodes = local_lane.nodes(config_path)
    node = next((n for n in nodes if n["node"].casefold() == node_id.strip().casefold()), None)
    if node is None:
        known = ", ".join(n["node"] for n in nodes) or "none"
        raise SwitchError(404, f"unknown local node {node_id.strip()[:40]!r} (configured: {known})")
    st = call(node, "POST", {"on": on})
    logger.info("local switch: node %s set %s", node["node"], "on" if on else "off")
    return {"node": node["node"], "label": node["label"], "auto_route": _auto(node),
            "models": [local_lane.pin_id(m) for m in node["models"]], "reachable": True, "error": None, **st}
