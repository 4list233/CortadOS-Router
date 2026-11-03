"""Local model lanes: an Ollama node on the owner's machines, served PIN-ONLY as `sub/<local model id or alias>`.

Owner of three rules:
  config        the nodes and the models they offer live in the local-resources config (CORTADOS_LOCAL_RESOURCES_CONFIG,
                the same file local_resources.py samples; schema in docs/LOCAL-RESOURCES.md), under each node's `ollama`
                block. Addresses are config or env only, never tables or code: the real file is git-ignored.
  pin-only      a local lane is never offered to S1. `auto_route` is the switch for that and defaults off; turning it
                on is refused (LocalConfigError) because auto-routing a local lane changes routing decisions and needs
                an evaluation and routing-table candidates first. A switch that silently did nothing would mislead.
  availability  a lane is available when its node answers GET /api/tags within a short timeout and lists the model.
                The node is a laptop that may be asleep or busy: then the pin fails with a clear 503, and nothing falls
                back to another model (the person chose this one).

Ollama implements the Anthropic Messages API at POST /v1/messages, so a turn is relayed as-is (stateless: the client
sends the whole conversation every turn) with only `model` rewritten to the node's tag. Stdlib only.
"""
from __future__ import annotations

import http.client
import json
import os
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
        if block.get("auto_route", False) is not False:
            raise LocalConfigError(f"node {nid}: auto_route is not built yet (local lanes are pin-only until they are "
                                   "evaluated and given routing-table candidates); set it to false", nid)
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
                     "name": m.get("name") or mid, "context_length": _context_length(m, nid, mid)}
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
        out.append({"node": nid, "lane": LANE_PREFIX + nid, "label": block.get("label") or nid.capitalize(),
                    "base_url": base, "models": models, "token": token,
                    **timeouts})
    return out


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


GATE_REFUSED = ("local gate on node {node} refused the provider token (401): the provider's token file does not "
                "match the gate's")


def _auth(node: dict[str, Any]) -> dict[str, str]:
    """The gate's credential (local_gate.py), when the node is behind one. Never logged."""
    return {"authorization": f"Bearer {node['token']}"} if node.get("token") else {}


def probe(node: dict[str, Any]) -> set[str]:
    """The tags the node lists (GET /api/tags) within its probe timeout. LocalUnavailable when it does not answer."""
    conn = _connection(node["base_url"], node["probe_timeout_s"])
    try:
        conn.request("GET", _prefix(node["base_url"]) + "/api/tags", headers=_auth(node))
        resp = conn.getresponse()
        raw = resp.read()
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


def _bounded_read(resp) -> bytes | None:
    """At most ERROR_BODY_MAX bytes of an error response, or None when it is truncated (IncompleteRead), stalls past
    the read timeout, or the connection drops. read(amt) returns a short read at EOF rather than raising, so the
    length the response declared is checked: fewer bytes than min(declared, cap) is a truncated body."""
    declared = resp.length                      # None for a chunked or close-delimited body
    want = ERROR_BODY_MAX if declared is None else min(declared, ERROR_BODY_MAX)
    try:
        data = resp.read(want)
    except (OSError, http.client.HTTPException):
        return None
    if declared is not None and len(data) < want:
        return None
    return data


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


def _refused(handler, code: int, kind: str, reason: str, detail: str | None = None) -> None:
    """Log and record a refusal this relay answers itself, through the server's one refusal helper
    (Handler._note_refusal: fixed reason, redacted and bounded detail, recorded only for an authenticated run). A
    handler without it (a unit test's stand-in) records nothing; the answer to the client is the same either way."""
    note = getattr(handler, "_note_refusal", None)
    if note is not None:
        note(code, kind, reason=reason, detail=detail)


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


def serve(handler, body: dict[str, Any], node: dict[str, Any], model: dict[str, Any], *,
          echo_model: str, reply: list | None = None) -> str:
    """Relay one turn to the node's /v1/messages and the answer back to the client. Returns the outcome: "completed"
    (a whole message or a parsed message_stop), "rejected" (the node answered an HTTP error, relayed as-is),
    "unreachable" (503 sent), or "incomplete" (the stream broke). The response names `echo_model` (what the client
    asked for), as every other lane does; content, tool_use blocks and stop reasons pass through untouched. `reply`,
    when given, receives the answer's content blocks (text and tool_use, rebuilt from the stream) so the server can
    fingerprint the reply and continue the conversation by its history, as for every other pin."""
    reply = [] if reply is None else reply
    out = dict(body)
    out["model"] = model["id"]
    data = json.dumps(out).encode()
    conn = _connection(node["base_url"], node["connect_timeout_s"])
    try:
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            _refused(handler, 503, "overloaded_error", "the local node is unreachable",
                     f"node {node['node']}: {type(exc).__name__}: {exc}")
            _send_json(handler, 503, _error("overloaded_error",
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
            _refused(handler, 503, "overloaded_error", "the local node refused or dropped the request",
                     f"node {node['node']}: {type(exc).__name__}: {exc}")
            _send_json(handler, 503, _error("overloaded_error",
                       f"cortados: local node {node['node']} accepted the connection for sub/{pin_id(model)}, then "
                       f"refused or dropped the request ({type(exc).__name__}: {exc}). No other model was tried."))
            return "unreachable"
        if resp.status == 401:                                   # the gate's refusal is the provider's problem,
            _bounded_read(resp)                                  # not the client's: a 503 that says so
            _refused(handler, 503, "overloaded_error", "the local gate refused the provider's token",
                     f"node {node['node']}")
            _send_json(handler, 503, _error("overloaded_error", "cortados: " + GATE_REFUSED.format(node=node["node"])
                                            + "; no other model was tried"))
            return "unreachable"
        if resp.status != 200:
            raw = _bounded_read(resp)
            if raw is None:                                      # truncated or stalled error body: say so, bounded
                _refused(handler, 502, "api_error", "the local node's error body was truncated or stalled",
                         f"node {node['node']}: HTTP {resp.status}")
                _send_json(handler, 502, _error("api_error", f"cortados: local node {node['node']} answered HTTP "
                                                f"{resp.status} and its error body was truncated or stalled"))
                return "incomplete"
            try:
                json.loads(raw)
            except ValueError:
                raw = json.dumps(_error("api_error", f"cortados: local node {node['node']} answered HTTP "
                                        f"{resp.status}: {raw[:300].decode('utf-8', 'replace')}")).encode()
            code = resp.status if 400 <= resp.status < 600 else 502
            _refused(handler, code, "api_error", "the local node answered an HTTP error",
                     f"node {node['node']}: HTTP {resp.status}: {raw.decode('utf-8', 'replace')}")
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
                _refused(handler, 502, "api_error", "the local node's answer was unreadable or timed out",
                         f"node {node['node']}: {type(exc).__name__}")
                _send_json(handler, 502, _error("api_error", f"cortados: local node {node['node']} sent an "
                                                f"unreadable response ({type(exc).__name__})"))
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
        done = False
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
                        _fold(reply, ev)
                        done = done or ev.get("type") == "message_stop"
                try:
                    handler.wfile.write(line)
                    if line in (b"\n", b"\r\n"):
                        handler.wfile.flush()
                except OSError:
                    return "incomplete"                          # the client left
        except (OSError, http.client.HTTPException):
            pass                                                 # the node stalled past read_timeout_s or dropped
        if not done:                                             # say so in the stream, not by a silent truncation
            _refused(handler, 502, "api_error", "the local node stopped mid-answer (stalled or dropped)",
                     f"node {node['node']}: read timeout {node['read_timeout_s']:g}s")
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
