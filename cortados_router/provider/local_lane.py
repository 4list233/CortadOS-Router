"""Local model lanes: an Ollama node on the owner's machines (the Pro, #75), served as `sub/<local model id or alias>`
pins and, when its config opts in, as an auto-route target after S1 has chosen a band.

Owner of three rules:
  config        the nodes and the models they offer live in the local-resources config (CORTADOS_LOCAL_RESOURCES_CONFIG,
                the same file local_resources.py samples; schema in docs/LOCAL-RESOURCES.md), under each node's `ollama`
                block. Addresses are config or env only, never tables or code: the real file is git-ignored.
  auto_route    off (absent or false) by default: the node is pin-only. On, it must be an object naming ONE of the node's
                models and the S1 bands it may take (`{"model": "<alias or tag>", "bands": ["R0"]}`); there is no default
                band, because which work runs on a local model is the owner's call. Only AUTO_ROUTE_BANDS may be named:
                R3 is where core.resolve's hard rules put outward and blast-radius-3 work (Opus-class floor) and R4 is
                escalation only, so a local model never takes either. `true` and anything malformed are refused
                (LocalConfigError): a switch that silently did nothing, or did more than it says, would mislead.
                routing.auto_local applies it; S1 and core.resolve never see a local lane. Superseded by index_route for any
                model with a published proxy index; kept working for configs that set it (it is checked first).
  index_route   on by default (`"index_route": false` turns it off per node): a node model with a proxy index in the
                generated scores (tables/local-models.json → index_proxies, built into model-scores.generated.json →
                local_models) is offered wherever its index clears S1's required index (routing.auto_local). A variant
                with thinking on is sent thinking enabled with a budget that fits max_tokens (think_budget); one with
                thinking off is sent thinking disabled. `max_inflight` (default 1) is how many relayed turns make the
                node busy: a busy node is not offered by index_route.
  availability  a lane is available when its node answers GET /api/tags within a short timeout and lists the model.
                On the Pro the gate (local_gate.py) answers that probe with a 503 while on battery or with the manual
                switch off, so one probe covers AC power, the switch and gate health. A pin to an unavailable node fails
                with a clear 503 and nothing falls back to another model (the person chose this one); an auto-route to
                one is simply not taken, and the conversation keeps the route S1 chose.

Ollama implements the Anthropic Messages API at POST /v1/messages, so a turn is relayed as-is (stateless: the client
sends the whole conversation every turn) with `model` rewritten to the node's tag and, unless the model entry says
`"thinking": "passthrough"`, thinking turned off when the client did not ask for it (#108, THINKING_MODES). Stdlib only.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
import urllib.parse
from typing import Any

from cortados_router.compat import getenv

CONFIG_ENV = "CORTADOS_LOCAL_RESOURCES_CONFIG"
LANE_PREFIX = "local-"
# Bounded waits. Probe: /api/tags answers in milliseconds on an awake node, so a short wait tells "asleep" quickly.
# Connect: the same. Read: the wait for each response chunk; a 16 GB model's first load can take a minute before the
# first byte, so it is generous, but bounded so a hung node never holds a client forever.
PROBE_TIMEOUT_S = 2.0
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 300.0


class LocalConfigError(ValueError):
    """The local-resources config is unreadable or its `ollama` block is malformed. `node` names the node when known:
    the full message (which may hold a file path) is for the provider log; a client is told only the node."""

    def __init__(self, message: str, node: str | None = None):
        super().__init__(message)
        self.node = node


class LocalUnavailable(RuntimeError):
    """The node did not answer, or does not list the model."""


def _config_path(path: str | None) -> str | None:
    return path if path is not None else (getenv(CONFIG_ENV) or None)


def nodes(path: str | None = None) -> list[dict[str, Any]]:
    """Every node with an `ollama` block, validated: [{node, label, base_url, models: [{id, alias, name,
    context_length}], timeouts…}]. No config file means no local lanes (an empty list), not an error."""
    p = _config_path(path)
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        raise LocalConfigError(f"local-resources config {CONFIG_ENV} unreadable: {exc}") from exc
    out, seen = [], {}
    for node in config.get("nodes") or []:
        block = node.get("ollama")
        if block is None:
            continue
        nid = str(node.get("id") or "").strip()
        if not nid or not isinstance(block, dict):
            raise LocalConfigError("an `ollama` block needs a node `id` and must be an object")
        env_name = block.get("base_url_env")
        base = (os.environ.get(env_name) if env_name else None) or block.get("base_url") or ""
        base = base.strip().rstrip("/")
        u = urllib.parse.urlparse(base)
        if u.scheme not in ("http", "https") or not u.hostname:
            where = f"${env_name}" if env_name else "base_url"
            raise LocalConfigError(f"node {nid}: no usable Ollama base URL (set {where} to http://<host>:11434)", nid)
        token = None
        tf_env = block.get("token_file_env")
        token_file = (os.environ.get(tf_env) if tf_env else None) or block.get("token_file")
        if tf_env and not token_file:
            raise LocalConfigError(f"node {nid}: ${tf_env} is not set (the path of the local gate's token file)", nid)
        if token_file:
            from cortados_router.local_gate import GateConfigError, read_token   # same file rules as the gate
            try:
                token = read_token(token_file)
            except GateConfigError as exc:
                raise LocalConfigError(f"node {nid}: {exc}", nid) from exc
        models = []
        for m in block.get("models") or []:
            mid = str(m.get("id") or "").strip()
            if not mid:
                raise LocalConfigError(f"node {nid}: every model needs an `id` (the Ollama tag)", nid)
            entry = {"id": mid, "alias": (str(m["alias"]).strip() or None) if m.get("alias") else None,
                     "name": m.get("name") or mid, "context_length": _context_length(m, nid, mid),
                     "thinking": _thinking_mode(m, nid, mid)}
            for pin in {mid.casefold(), (entry["alias"] or mid).casefold()}:
                if pin in seen:
                    raise LocalConfigError(f"pin name {pin!r} is offered twice (nodes {seen[pin]} and {nid})", nid)
                seen[pin] = nid
            models.append(entry)
        timeouts = {}
        for key, default in (("probe_timeout_s", PROBE_TIMEOUT_S), ("connect_timeout_s", CONNECT_TIMEOUT_S),
                             ("read_timeout_s", READ_TIMEOUT_S)):
            try:
                timeouts[key] = float(block.get(key, default))
            except (TypeError, ValueError):
                timeouts[key] = -1.0
            if not timeouts[key] > 0:            # 0 would make the socket non-blocking; negative raises mid-turn
                raise LocalConfigError(f"node {nid}: {key} must be a positive number of seconds", nid)
        index_route = block.get("index_route", True)
        if not isinstance(index_route, bool):
            raise LocalConfigError(f"node {nid}: index_route must be true or false, got {index_route!r}", nid)
        max_inflight = block.get("max_inflight", MAX_INFLIGHT_DEFAULT)
        if isinstance(max_inflight, bool) or not isinstance(max_inflight, int) or max_inflight < 1:
            raise LocalConfigError(f"node {nid}: max_inflight must be a positive integer, got {max_inflight!r}", nid)
        out.append({"node": nid, "lane": LANE_PREFIX + nid, "label": block.get("label") or nid.capitalize(),
                    "base_url": base, "models": models, "token": token,
                    "auto_route": _auto_route(block.get("auto_route", False), models, nid),
                    "index_route": index_route, "max_inflight": max_inflight,
                    **timeouts})
    return out


# The S1 bands a local model may be auto-routed in. Not R3 (core.resolve floors outward and blast-radius-3 work there,
# Opus-class) and not R4 (escalation only): those hard rules must hold whatever a node's config says.
AUTO_ROUTE_BANDS = ("R0", "R1", "R2")


def _auto_route(value: Any, models: list[dict[str, Any]], nid: str) -> dict[str, Any] | None:
    """A node's validated auto_route: None (off) or {"model": <one of its model entries>, "bands": [...]}."""
    if value is False or value is None:
        return None
    if not isinstance(value, dict):
        raise LocalConfigError(f"node {nid}: auto_route must be false or an object "
                               '{"model": "<alias or tag>", "bands": [...]} naming the model and the S1 bands it may '
                               f"take (got {value!r}; there is no default band)", nid)
    unknown = set(value) - {"model", "bands", "_comment"}
    if unknown:
        raise LocalConfigError(f"node {nid}: auto_route has unknown keys {sorted(unknown)}", nid)
    name = str(value.get("model") or "").strip().casefold()
    entry = next((m for m in models if name and name in (m["id"].casefold(), (m["alias"] or "").casefold())), None)
    if entry is None:
        raise LocalConfigError(f"node {nid}: auto_route.model must name one of this node's models (alias or tag)", nid)
    bands = value.get("bands")
    if not isinstance(bands, list) or not bands or not all(isinstance(b, str) for b in bands):
        raise LocalConfigError(f"node {nid}: auto_route.bands must be a non-empty list of S1 bands from "
                               f"{', '.join(AUTO_ROUTE_BANDS)} (there is no default)", nid)
    bad = [b for b in bands if b not in AUTO_ROUTE_BANDS]
    if bad:
        raise LocalConfigError(f"node {nid}: auto_route.bands {bad} not allowed: a local model may take only "
                               f"{', '.join(AUTO_ROUTE_BANDS)} (R3 carries outward and blast-radius-3 work, R4 is "
                               "escalation only)", nid)
    return {"model": entry, "bands": list(dict.fromkeys(bands))}


def auto_routes(path: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    """(node, model, bands) for every node whose auto_route is on, in config order."""
    return [(n, n["auto_route"]["model"], n["auto_route"]["bands"]) for n in nodes(path) if n.get("auto_route")]


DEFAULT_CONTEXT = 32768


def _context_length(m: dict[str, Any], nid: str, mid: str) -> int:
    """A model's configured context in tokens: the input-fit gate. Absent means DEFAULT_CONTEXT; anything else must be
    a positive integer ("64k", 0, true are refused, not guessed at: a wrong value lets oversized turns through)."""
    if "context_length" not in m or m["context_length"] is None:
        return DEFAULT_CONTEXT
    v = m["context_length"]
    try:
        if isinstance(v, bool) or isinstance(v, float) and not v.is_integer():
            raise ValueError
        n = int(v)
    except (TypeError, ValueError):
        raise LocalConfigError(f"node {nid}: model {mid}: context_length must be a positive integer of tokens, "
                               f"got {v!r}", nid) from None
    if n <= 0:
        raise LocalConfigError(f"node {nid}: model {mid}: context_length must be positive, got {v!r}", nid)
    return n


# A model entry's `thinking` (#108). Ollama's /v1/messages lets a thinking model (Qwen3.x) think whenever the request
# has no `thinking` field, and the thinking counts against max_tokens: a max_tokens=20 reflex request came back with
# only a thinking block and stop_reason max_tokens. Anthropic's default is the opposite (no field, no thinking), so
#   off-unless-asked  (default) a request without `thinking` is sent with {"type": "disabled"}, which Ollama honours
#                     (observed on 0.40.16: text only, end_turn); a client's own `thinking` is passed through.
#   passthrough       the request is relayed as the client sent it: for a model or node that rejects the field.
THINKING_MODES = ("off-unless-asked", "passthrough")
THINKING_DEFAULT = "off-unless-asked"


def _thinking_mode(m: dict[str, Any], nid: str, mid: str) -> str:
    """A model's validated `thinking` mode; absent means THINKING_DEFAULT, anything else unknown is refused."""
    v = m.get("thinking")
    if v is None:
        return THINKING_DEFAULT
    if v not in THINKING_MODES:
        raise LocalConfigError(f"node {nid}: model {mid}: thinking must be one of {', '.join(THINKING_MODES)}, "
                               f"got {v!r}", nid)
    return v


# Thinking variants (index_route, tables/local-models.json → index_proxies). Reason for the threshold (#108): Qwen3.8 27B
# with thinking on spent all of max_tokens=20 thinking and wrote no answer, and the thinking counts against max_tokens.
# Anthropic's extended thinking needs budget_tokens >= 1024 and < max_tokens, and the answer needs room after it; the
# think variant's index comes from published reasoning runs with large budgets, which a 1k budget would not reproduce.
# So the think variant is offered only from 4096 max_tokens, with half for thinking (>= 2048) and half for the answer
# (>= 2048). Below it the request goes to the non-thinking variant or the cloud route, never to the think variant.
THINK_MIN_MAX_TOKENS = 4096
VARIANT_THINKING = ("off", "on")


def think_budget(max_tokens: Any) -> int | None:
    """The thinking budget a think-variant turn is sent with: half of max_tokens, or None when max_tokens (absent, not
    an integer, or below THINK_MIN_MAX_TOKENS) leaves no room for thinking and an answer."""
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < THINK_MIN_MAX_TOKENS:
        return None
    return max_tokens // 2


# Busy (index_route). Reason: Ollama runs one request per loaded model at a time unless OLLAMA_NUM_PARALLEL says
# otherwise (a 27B at 64k context on the 48 GB Pro runs one), so a second turn queues behind the first for minutes
# while a cloud lane would answer now. Counted per node in this process: every relayed turn, pinned or routed.
MAX_INFLIGHT_DEFAULT = 1
_inflight: dict[str, int] = {}
_inflight_lock = threading.Lock()


def inflight(node: dict[str, Any]) -> int:
    """How many turns this process is relaying to the node right now."""
    with _inflight_lock:
        return _inflight.get(node["node"], 0)


def busy(node: dict[str, Any]) -> bool:
    """The node already relays its max_inflight turns: index_route does not add one more."""
    return inflight(node) >= int(node.get("max_inflight") or MAX_INFLIGHT_DEFAULT)


def client_asked_thinking(body: dict[str, Any]) -> bool:
    """The client asked for extended thinking: a `thinking` object whose type is anything but "disabled"."""
    t = body.get("thinking")
    return isinstance(t, dict) and t.get("type") != "disabled"


def _node_body(body: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """The request as the node gets it: `model` rewritten to the node's tag and, under off-unless-asked, thinking
    turned off when the client did not ask for it (a missing field, or one of another shape). An index-routed think
    variant (`variant_thinking` "on") the client did not ask to think is sent thinking enabled with think_budget, or
    disabled when this turn's max_tokens leaves no room for it; a client's own `thinking` always passes through."""
    out = dict(body)
    out["model"] = model["id"]
    if client_asked_thinking(body):
        return out
    if model.get("variant_thinking") == "on":
        budget = think_budget(body.get("max_tokens"))
        out["thinking"] = {"type": "enabled", "budget_tokens": budget} if budget else {"type": "disabled"}
    elif model.get("variant_thinking") == "off" or model.get("thinking", THINKING_DEFAULT) == "off-unless-asked":
        out["thinking"] = {"type": "disabled"}
    return out


def _answered(blocks: list) -> bool:
    """The reply holds an answer: a non-blank text block or a tool_use (a thinking block alone is not one)."""
    return any(isinstance(b, dict) and (b.get("type") == "tool_use" or
                                        b.get("type") == "text" and str(b.get("text") or "").strip())
               for b in blocks)


def thinking_starved(node: dict[str, Any], model: dict[str, Any], max_tokens: Any) -> str:
    """The client's message when an auto-routed turn spent its whole max_tokens without writing an answer (#108)."""
    return (f"cortados: this request was auto-routed by S1 to {model['id']} on {node['node']}, which used the whole "
            f"max_tokens ({max_tokens}) without writing an answer (thinking only); retry with a larger max_tokens, or "
            f"send x-cortados-exclude-model: {pin_id(model)} to keep S1's route")


def pin_id(model: dict[str, Any]) -> str:
    """The name a client pins: the alias when there is one, else the Ollama tag."""
    return model["alias"] or model["id"]


def pins(path: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(node, model) for every local pin, in config order."""
    return [(n, m) for n in nodes(path) for m in n["models"]]


def find(name: str | None, path: str | None = None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The (node, model) a `sub/` name pins, by alias or tag (case-insensitive), or None when it is not local."""
    key = (name or "").strip().casefold()
    if not key:
        return None
    for n, m in pins(path):
        if key in (m["id"].casefold(), (m["alias"] or "").casefold()):
            return n, m
    return None


def _connection(base_url: str, timeout: float) -> http.client.HTTPConnection:
    u = urllib.parse.urlparse(base_url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout)


def _prefix(base_url: str) -> str:
    return urllib.parse.urlparse(base_url).path.rstrip("/")


PRO_LOCAL_REFUSALS = {"local gate: local switch off", "local gate: local disabled: on battery",
                      "local gate: local disabled: unknown power source",
                      "local gate: local disabled: power check failed"}

GATE_REFUSED = ("local gate on node {node} refused the provider token (401): the provider's token file does not "
                "match the gate's")


def _policy_refusal(raw: bytes | None) -> str | None:
    """The gate's fixed policy reason (PRO_LOCAL_REFUSALS) in a 503 body, or None. Only those fixed strings are ever
    repeated to a client, never arbitrary upstream text (the same rule as probe)."""
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) and message in PRO_LOCAL_REFUSALS else None


def _auth(node: dict[str, Any]) -> dict[str, str]:
    """The gate's credential (local_gate.py), when the node is behind one. Never logged."""
    return {"authorization": f"Bearer {node['token']}"} if node.get("token") else {}


def probe(node: dict[str, Any]) -> set[str]:
    """The tags the node lists (GET /api/tags) within its probe timeout. LocalUnavailable when it does not answer."""
    conn = _connection(node["base_url"], node["probe_timeout_s"])
    try:
        conn.request("GET", _prefix(node["base_url"]) + "/api/tags", headers=_auth(node))
        resp = conn.getresponse()
        # Error bodies are deliberately capped at 64 KiB, but a valid Ollama
        # catalog can contain many models and gets its own larger documented cap.
        raw = _catalog_read(resp) if resp.status == 200 else _bounded_read(resp)
        if raw is None:
            raise ValueError("truncated or stalled probe response")
        if resp.status == 503:
            # Preserve only our fixed policy reasons, not arbitrary upstream text.
            data = json.loads(raw)
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if isinstance(message, str) and message in PRO_LOCAL_REFUSALS:
                raise LocalUnavailable(f"node {node['node']}: {message}")
        if resp.status == 401:
            raise LocalUnavailable(GATE_REFUSED.format(node=node["node"]))
        if resp.status != 200:
            raise LocalUnavailable(f"node {node['node']} answered /api/tags with HTTP {resp.status}")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("/api/tags answered JSON that is not an object")
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise LocalUnavailable(f"node {node['node']} did not answer /api/tags within {node['probe_timeout_s']:g}s "
                               f"(asleep, offline or busy?): {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()
    return {str(m.get("name") or m.get("model") or "") for m in (data.get("models") or []) if isinstance(m, dict)}


ERROR_BODY_MAX = 65536      # an error body is a short JSON message; more is not read, so a stall cannot pin a thread
CATALOG_MAX = 4 * 1024 * 1024  # successful /api/tags JSON; bounded so a peer cannot pin memory with an unbounded catalog


def _read_capped(resp, cap: int) -> bytes | None:
    """At most cap bytes of a response, or None when it is truncated (IncompleteRead), stalls past
    the read timeout, or the connection drops. read(amt) returns a short read at EOF rather than raising, so the
    length the response declared is checked: fewer bytes than min(declared, cap) is a truncated body."""
    declared = resp.length                      # None for a chunked or close-delimited body
    want = cap if declared is None else min(declared, cap)
    try:
        data = resp.read(want)
    except (OSError, http.client.HTTPException):
        return None
    if declared is not None and len(data) < want:
        return None
    return data


def _bounded_read(resp) -> bytes | None:
    """Read an error response with the 64 KiB cap."""
    return _read_capped(resp, ERROR_BODY_MAX)


def _catalog_read(resp) -> bytes | None:
    """Read a successful /api/tags catalog with its separate 4 MiB cap."""
    return _read_capped(resp, CATALOG_MAX)


def _send_json(handler, code: int, data: dict[str, Any]) -> None:
    raw = json.dumps(data).encode()
    try:
        handler.send_response(code)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
    except OSError:
        pass


def _refused(handler, code: int, kind: str, reason: str) -> None:
    """Log and record a refusal this relay answers itself, through the server's one refusal helper
    (Handler._note_refusal: a fixed reason only, never the node's text; recorded only for an authenticated run). A
    handler without it (a unit test's stand-in) records nothing; the answer to the client is the same either way."""
    note = getattr(handler, "_note_refusal", None)
    if note is not None:
        note(code, kind, reason=reason)


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _rename(event_line: bytes, name: str) -> bytes:
    """A `data:` line whose message_start names `name` as its model; any other line is returned byte-for-byte."""
    if b"message_start" not in event_line:
        return event_line
    try:
        ev = json.loads(event_line[5:].strip())
        if ev.get("type") == "message_start" and isinstance(ev.get("message"), dict):
            ev["message"]["model"] = name
            return b"data: " + json.dumps(ev).encode() + (b"\n" if event_line.endswith(b"\n") else b"")
    except ValueError:
        pass
    return event_line


def _fold(reply: list, ev: dict[str, Any]) -> None:
    """Rebuild the streamed answer's content blocks (text and tool_use ids are all a reply fingerprint reads)."""
    t = ev.get("type")
    if t == "content_block_start" and isinstance(ev.get("content_block"), dict):
        reply.append(dict(ev["content_block"]))
    elif t == "content_block_delta" and reply and isinstance(ev.get("delta"), dict):
        d = ev["delta"]
        if d.get("type") == "text_delta" and reply[-1].get("type") == "text":
            reply[-1]["text"] = reply[-1].get("text", "") + str(d.get("text", ""))


def auto_routed_unavailable(node: dict[str, Any], model: dict[str, Any], reason: str) -> str:
    """The client's message when the node of an S1 auto-routed (not pinned) conversation is unavailable mid-conversation.
    Reason: the person never pinned this model, so a "sub/<alias>" message would blame a choice they did not make. The
    conversation stays bound to the node (re-routing it mid-conversation is an owner decision not taken); a new
    conversation is routed again, and excluding the model keeps S1's lane even while the node is up."""
    return (f"cortados: this conversation was auto-routed by S1 to {model['id']} on {node['node']}; the node is "
            f"{reason}; start a new conversation (it is routed again) or send x-cortados-exclude-model: {pin_id(model)}")


def serve(handler, body: dict[str, Any], node: dict[str, Any], model: dict[str, Any], *,
          echo_model: str, reply: list | None = None, auto_routed: bool = False) -> str:
    """_serve, counted in the node's in-flight turns (busy) while it runs."""
    with _inflight_lock:
        _inflight[node["node"]] = _inflight.get(node["node"], 0) + 1
    try:
        return _serve(handler, body, node, model, echo_model=echo_model, reply=reply, auto_routed=auto_routed)
    finally:
        with _inflight_lock:
            left = _inflight.get(node["node"], 1) - 1
            if left > 0:
                _inflight[node["node"]] = left
            else:
                _inflight.pop(node["node"], None)


def _serve(handler, body: dict[str, Any], node: dict[str, Any], model: dict[str, Any], *,
           echo_model: str, reply: list | None = None, auto_routed: bool = False) -> str:
    """Relay one turn to the node's /v1/messages and the answer back to the client. Returns the outcome: "completed"
    (a whole message or a parsed message_stop), "rejected" (the node answered an HTTP error, relayed as-is),
    "unreachable" (503 sent), or "incomplete" (the stream broke). The response names `echo_model` (what the client
    asked for), as every other lane does; content, tool_use blocks and stop reasons pass through untouched. `reply`,
    when given, receives the answer's content blocks (text and tool_use, rebuilt from the stream) so the server can
    fingerprint the reply and continue the conversation by its history, as for every other pin. `auto_routed`: the
    conversation was moved here by S1's auto-route (routing.auto_local), not pinned; an unavailable node (unreachable,
    dropping requests, refusing the token, or the gate's fixed policy refusal: battery, switch off) is then worded by
    auto_routed_unavailable instead of naming a `sub/` pin the person never made."""
    reply = [] if reply is None else reply
    data = json.dumps(_node_body(body, model)).encode()
    # Safety net (#108): an auto-routed turn the client did not ask to think on, which still ended on max_tokens with
    # no text or tool_use, is answered with an error naming the cause instead of a silent empty answer. Not retried:
    # thinking was already asked off (or the model is set to passthrough), so the same request would fail the same
    # way. A pinned model (the person's choice) and a client that asked for thinking get the reply as the node sent it.
    guard = auto_routed and not client_asked_thinking(body)
    conn = _connection(node["base_url"], node["connect_timeout_s"])
    try:
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            _refused(handler, 503, "overloaded_error", "the local node is unreachable")
            _send_json(handler, 503, _error("overloaded_error", auto_routed_unavailable(
                       node, model, f"unreachable ({type(exc).__name__}: {exc}); it may be asleep or busy")
                       if auto_routed else
                       f"cortados: local node {node['node']} unreachable for sub/{pin_id(model)} "
                       f"({type(exc).__name__}: {exc}); it may be asleep or busy. No other model was tried."))
            return "unreachable"
        try:
            conn.sock.settimeout(node["read_timeout_s"])        # connect bounded short, each read bounded long
            conn.request("POST", _prefix(node["base_url"]) + "/v1/messages", body=data,
                         headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                                  **_auth(node)})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            # Connected, so the node is awake: it (or its gate) refused or dropped the request.
            _refused(handler, 503, "overloaded_error", "the local node refused or dropped the request")
            _send_json(handler, 503, _error("overloaded_error", auto_routed_unavailable(
                       node, model, f"refusing or dropping requests ({type(exc).__name__}: {exc})")
                       if auto_routed else
                       f"cortados: local node {node['node']} accepted the connection for sub/{pin_id(model)}, then "
                       f"refused or dropped the request ({type(exc).__name__}: {exc}). No other model was tried."))
            return "unreachable"
        if resp.status == 401:                                   # the gate's refusal is the provider's problem,
            _bounded_read(resp)                                  # not the client's: a 503 that says so
            _refused(handler, 503, "overloaded_error", "the local gate refused the provider's token")
            _send_json(handler, 503, _error("overloaded_error", auto_routed_unavailable(
                       node, model, "refusing the provider's token (401: the provider's token file does not match the "
                       "gate's)") if auto_routed else
                       "cortados: " + GATE_REFUSED.format(node=node["node"]) + "; no other model was tried"))
            return "unreachable"
        if resp.status != 200:
            raw = _bounded_read(resp)
            policy = _policy_refusal(raw) if auto_routed and resp.status == 503 else None
            if policy:                                           # the gate's fixed reason: battery, switch off, ...
                _refused(handler, 503, "overloaded_error", "the local gate refused local models")
                _send_json(handler, 503, _error("overloaded_error", auto_routed_unavailable(
                           node, model, f"refusing local models ({policy})")))
                return "rejected"
            if raw is None:                                      # truncated or stalled error body: say so, bounded
                _refused(handler, 502, "api_error", "the local node's error body was truncated or stalled")
                _send_json(handler, 502, _error("api_error", f"cortados: local node {node['node']} answered HTTP "
                                                f"{resp.status} and its error body was truncated or stalled"))
                return "incomplete"
            try:
                json.loads(raw)
            except ValueError:
                raw = json.dumps(_error("api_error", f"cortados: local node {node['node']} answered HTTP "
                                        f"{resp.status}: {raw[:300].decode('utf-8', 'replace')}")).encode()
            code = resp.status if 400 <= resp.status < 600 else 502
            _refused(handler, code, "api_error", "the local node answered an HTTP error")
            try:
                handler.send_response(code)
                handler.send_header("content-type", "application/json")
                handler.send_header("content-length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)
            except OSError:
                pass
            return "rejected"
        if not body.get("stream"):
            try:
                raw = resp.read()
                msg = json.loads(raw)
            except (OSError, http.client.HTTPException, ValueError) as exc:
                # A read timeout lands here too (the node stalled past read_timeout_s on a non-streaming answer).
                _refused(handler, 502, "api_error", "the local node's answer was unreadable or timed out")
                _send_json(handler, 502, _error("api_error", f"cortados: local node {node['node']} sent an "
                                                f"unreadable response ({type(exc).__name__})"))
                return "incomplete"
            if (guard and isinstance(msg, dict) and msg.get("type") == "message"
                    and msg.get("stop_reason") == "max_tokens" and not _answered(msg.get("content") or [])):
                _refused(handler, 502, "api_error", "the local model used the whole max_tokens without an answer")
                _send_json(handler, 502, _error("api_error", thinking_starved(node, model, body.get("max_tokens"))))
                return "incomplete"
            if isinstance(msg, dict) and msg.get("type") == "message":
                msg["model"] = echo_model
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                reply.extend(b for b in msg["content"] if isinstance(b, dict))
            _send_json(handler, 200, msg)
            return "completed" if isinstance(msg, dict) and msg.get("type") == "message" else "incomplete"
        try:
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("cache-control", "no-cache")
            handler.send_header("connection", "close")
            handler.end_headers()
        except OSError:
            return "incomplete"
        handler.close_connection = True
        done = starved = False
        pending: list = []                               # one SSE event's lines, written when its blank line arrives
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                if line.startswith(b"data:"):
                    line = _rename(line, echo_model)
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        ev = None
                    if isinstance(ev, dict):
                        if (guard and ev.get("type") == "message_delta" and isinstance(ev.get("delta"), dict)
                                and ev["delta"].get("stop_reason") == "max_tokens" and not _answered(reply)):
                            starved = True                   # this event and the rest are replaced by an error
                            break
                        _fold(reply, ev)
                        done = done or ev.get("type") == "message_stop"
                pending.append(line)
                if line in (b"\n", b"\r\n"):
                    try:
                        handler.wfile.write(b"".join(pending))
                        handler.wfile.flush()
                    except OSError:
                        return "incomplete"                      # the client left
                    pending = []
            if pending and not starved:
                try:
                    handler.wfile.write(b"".join(pending))
                except OSError:
                    return "incomplete"
        except (OSError, http.client.HTTPException):
            pass                                                 # the node stalled past read_timeout_s or dropped
        if starved:
            _refused(handler, 502, "api_error", "the local model used the whole max_tokens without an answer")
            ev = _error("api_error", thinking_starved(node, model, body.get("max_tokens")))
            try:
                handler.wfile.write(b"event: error\ndata: " + json.dumps(ev).encode() + b"\n\n")
                handler.wfile.flush()
            except OSError:
                pass
            return "incomplete"
        if not done:                                             # say so in the stream, not by a silent truncation
            _refused(handler, 502, "api_error", "the local node stopped mid-answer (stalled or dropped)")
            ev = _error("api_error", f"cortados: local node {node['node']} stopped mid-answer (stalled past "
                        f"{node['read_timeout_s']:g}s or dropped the connection)")
            try:
                handler.wfile.write(b"event: error\ndata: " + json.dumps(ev).encode() + b"\n\n")
            except OSError:
                pass
        try:
            handler.wfile.flush()
        except OSError:
            pass
        return "completed" if done else "incomplete"
    finally:
        conn.close()
