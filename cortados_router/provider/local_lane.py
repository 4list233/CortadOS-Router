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
    """The local-resources config is unreadable or its `ollama` block is malformed."""


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
                                   "evaluated and given routing-table candidates); set it to false")
        env_name = block.get("base_url_env")
        base = (os.environ.get(env_name) if env_name else None) or block.get("base_url") or ""
        base = base.strip().rstrip("/")
        u = urllib.parse.urlparse(base)
        if u.scheme not in ("http", "https") or not u.hostname:
            where = f"${env_name}" if env_name else "base_url"
            raise LocalConfigError(f"node {nid}: no usable Ollama base URL (set {where} to http://<host>:11434)")
        models = []
        for m in block.get("models") or []:
            mid = str(m.get("id") or "").strip()
            if not mid:
                raise LocalConfigError(f"node {nid}: every model needs an `id` (the Ollama tag)")
            entry = {"id": mid, "alias": (str(m["alias"]).strip() or None) if m.get("alias") else None,
                     "name": m.get("name") or mid, "context_length": int(m.get("context_length") or 32768)}
            for pin in {mid.casefold(), (entry["alias"] or mid).casefold()}:
                if pin in seen:
                    raise LocalConfigError(f"pin name {pin!r} is offered twice (nodes {seen[pin]} and {nid})")
                seen[pin] = nid
            models.append(entry)
        out.append({"node": nid, "lane": LANE_PREFIX + nid, "label": block.get("label") or nid.capitalize(),
                    "base_url": base, "models": models,
                    "probe_timeout_s": float(block.get("probe_timeout_s", PROBE_TIMEOUT_S)),
                    "connect_timeout_s": float(block.get("connect_timeout_s", CONNECT_TIMEOUT_S)),
                    "read_timeout_s": float(block.get("read_timeout_s", READ_TIMEOUT_S))})
    return out


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


def probe(node: dict[str, Any]) -> set[str]:
    """The tags the node lists (GET /api/tags) within its probe timeout. LocalUnavailable when it does not answer."""
    conn = _connection(node["base_url"], node["probe_timeout_s"])
    try:
        conn.request("GET", _prefix(node["base_url"]) + "/api/tags")
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise LocalUnavailable(f"node {node['node']} answered /api/tags with HTTP {resp.status}")
        data = json.loads(raw)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise LocalUnavailable(f"node {node['node']} did not answer /api/tags within {node['probe_timeout_s']:g}s "
                               f"(asleep, offline or busy?): {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()
    return {str(m.get("name") or m.get("model") or "") for m in (data.get("models") or []) if isinstance(m, dict)}


def health(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Per lane: {ok, models (listed tags), why}. No quota: a local lane is available or not."""
    out = {}
    for n in nodes(path):
        try:
            out[n["lane"]] = {"ok": True, "models": sorted(probe(n)), "why": None}
        except LocalUnavailable as exc:
            out[n["lane"]] = {"ok": False, "models": [], "why": str(exc)}
    return out


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


def serve(handler, body: dict[str, Any], node: dict[str, Any], model: dict[str, Any], *,
          echo_model: str) -> str:
    """Relay one turn to the node's /v1/messages and the answer back to the client. Returns the outcome: "completed"
    (a whole message or a parsed message_stop), "rejected" (the node answered an HTTP error, relayed as-is),
    "unreachable" (503 sent), or "incomplete" (the stream broke). The response names `echo_model` (what the client
    asked for), as every other lane does; content, tool_use blocks and stop reasons pass through untouched."""
    out = dict(body)
    out["model"] = model["id"]
    data = json.dumps(out).encode()
    conn = _connection(node["base_url"], node["connect_timeout_s"])
    try:
        try:
            conn.connect()
            conn.sock.settimeout(node["read_timeout_s"])        # connect bounded short, each read bounded long
            conn.request("POST", _prefix(node["base_url"]) + "/v1/messages", body=data,
                         headers={"content-type": "application/json", "anthropic-version": "2023-06-01"})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            _send_json(handler, 503, _error("overloaded_error",
                       f"cortados: local node {node['node']} unreachable for sub/{pin_id(model)} "
                       f"({type(exc).__name__}: {exc}); it may be asleep or busy. No other model was tried."))
            return "unreachable"
        if resp.status != 200:
            raw = resp.read()
            try:
                json.loads(raw)
            except ValueError:
                raw = json.dumps(_error("api_error", f"cortados: local node {node['node']} answered HTTP "
                                        f"{resp.status}: {raw[:300].decode('utf-8', 'replace')}")).encode()
            code = resp.status if 400 <= resp.status < 600 else 502
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
                _send_json(handler, 502, _error("api_error", f"cortados: local node {node['node']} sent an "
                                                f"unreadable response ({type(exc).__name__})"))
                return "incomplete"
            if isinstance(msg, dict) and msg.get("type") == "message":
                msg["model"] = echo_model
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
                    if b'"message_stop"' in line:
                        try:
                            done = done or json.loads(line[5:].strip()).get("type") == "message_stop"
                        except ValueError:
                            pass
                try:
                    handler.wfile.write(line)
                    if line in (b"\n", b"\r\n"):
                        handler.wfile.flush()
                except OSError:
                    return "incomplete"                          # the client left
        except (OSError, http.client.HTTPException):
            pass                                                 # the node stalled past read_timeout_s or dropped
        if not done:                                             # say so in the stream, not by a silent truncation
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
