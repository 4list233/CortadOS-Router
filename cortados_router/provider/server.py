"""The CortadOS endpoint: an Anthropic Messages server whose one model, `cortados`, is served by bridged headless CLIs.

Owner of the conversation registry: which bridge session a request belongs to. A request with no assistant messages
starts a new session; any other goes to the one live session whose replies are exactly its assistant history, and
fails with 409 when there is none or more than one. A request without tools always gets its own throwaway CLI.

Run:  python3 -B -m cortados_router.provider.server [--port 8432]
      then  ANTHROPIC_BASE_URL=http://127.0.0.1:8432 ANTHROPIC_AUTH_TOKEN=<token> claude --model cortados
      or, from Codex, POST /v1/responses (the OpenAI Responses wire, responses_wire.py; the client config is in
      docs/PROVIDER-PLAN.md, P2 status).
P1: S1 routes each new conversation to a model this endpoint serves (routing.py; docs/PROVIDER-PLAN.md).
P2: the route's provider picks the backend (SESSION_FACTORIES): `claude-cli` runs a headless Claude CLI (bridge.py),
    `codex-cli` a `codex app-server` thread (codex_bridge.py). `--codex-bin` / CORTADOS_PROVIDER_CODEX_BIN names the
    Codex binary (default `codex` on PATH; not scarcity's CORTADOS_CODEX_BIN, which points at the ChatGPT.app copy).
    `--codex-home` / CORTADOS_PROVIDER_CODEX_HOME is a dedicated, already logged-in CODEX_HOME for the backend; unset,
    the backend runs on the user's real Codex home (its AGENTS.md and skills leak into the thread: PROVIDER-PLAN.md).
    Each client wire (WIRES, keyed by path) owns its request and response shapes; the session logic is shared:
    /v1/messages speaks Anthropic Messages (Claude Code), /v1/responses speaks OpenAI Responses (Codex).
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):                           # run as a file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cortados_router.compat import getenv
from cortados_router.provider import anthropic_wire, responses_wire
from cortados_router.provider.bridge import BridgeError, BridgeSession
from cortados_router.provider.codex_bridge import CodexSession

DEFAULT_PORT = 8432                                     # next to S1's 8431
# A turn covers the model's thinking until it finishes or asks for a tool; long agentic turns run minutes.
TURN_TIMEOUT_S = float(getenv("CORTADOS_PROVIDER_TURN_TIMEOUT_S") or 1800)
# A conversation the client has not touched for this long is closed; its CLI process holds memory and a login slot.
IDLE_S = float(getenv("CORTADOS_PROVIDER_IDLE_S") or 1800)

_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
_dashboard_cache: dict = {}


def _dashboard_html() -> bytes:
    """The served dashboard page (static HTML/JS; the page itself asks for the token and fetches /resources)."""
    if "b" not in _dashboard_cache:
        _dashboard_cache["b"] = _DASHBOARD_PATH.read_bytes()
    return _dashboard_cache["b"]
# SSE pings while a turn runs, so the client's read timeout does not fire during a long think.
PING_S = 10.0
# Seconds a socket read or write may block (http.server applies it to the connection). A client that stops reading
# would otherwise block an SSE write forever and with it the handler thread. Too short: a client on a slow link, or
# one that pauses while it renders, is treated as gone and loses the reply. Too long: a dead client holds a handler
# thread that long. Backend cleanup does not depend on this: the worker that ran the turn owns it.
SOCKET_TIMEOUT_S = 120.0
logger = logging.getLogger("cortados.provider")


class Wire:
    """One client wire: the handful of shape-specific calls `_messages` makes, so a sibling wire (Codex's Responses
    API) reuses the session logic by supplying its own. `parse_request`, `one_shot_content(body)`, `message_json`
    and `error_json` take and return what the Anthropic ones do; `routing_body` turns the request into the
    Anthropic-shaped body `routing.choose` scores; `stream_body(msg_id, model, blocks, stop, usage)` and
    `stream_error(msg_id, model, message)` also receive the id `stream_start` announced and the requested model,
    because the Responses wire must echo both in its final event (Codex compares the model) while the Anthropic
    wire needs neither."""

    def __init__(self, *, model_id: str, error_type: type, parse_request: Callable, one_shot_content: Callable,
                 routing_body: Callable, new_message_id: Callable, stream_start: Callable, stream_ping: Callable,
                 stream_body: Callable, stream_error: Callable, message_json: Callable, error_json: Callable):
        self.MODEL_ID, self.WireError = model_id, error_type
        self.parse_request, self.one_shot_content, self.routing_body = parse_request, one_shot_content, routing_body
        self.new_message_id, self.stream_start, self.stream_ping = new_message_id, stream_start, stream_ping
        self.stream_body, self.stream_error, self.message_json, self.error_json = stream_body, stream_error, message_json, error_json


ANTHROPIC = Wire(
    model_id=anthropic_wire.MODEL_ID, error_type=anthropic_wire.WireError, parse_request=anthropic_wire.parse_request,
    one_shot_content=lambda body: anthropic_wire.one_shot_content(body["messages"]), routing_body=lambda body: body,
    new_message_id=anthropic_wire.new_message_id, stream_start=anthropic_wire.sse_start, stream_ping=anthropic_wire.sse_ping,
    stream_body=lambda msg_id, model, blocks, stop, usage: anthropic_wire.sse_body(blocks, stop, usage),
    stream_error=lambda msg_id, model, message: anthropic_wire.sse("error", anthropic_wire.error_json("api_error", message)),
    message_json=anthropic_wire.message_json, error_json=anthropic_wire.error_json)
# The Responses wire scores what its user and assistant messages say, in Anthropic shape, with the rest of the body
# kept (routing.choose sizes the request from the whole body and reads `tools` to tell a one-shot from a session).
# Only the function tools are handed over: parse_request drops the hosted ones (web_search), so a request whose
# tools are all hosted is served as a one-shot, and routing's standalone test must agree with that.
RESPONSES = Wire(
    model_id=responses_wire.MODEL_ID, error_type=responses_wire.WireError, parse_request=responses_wire.parse_request,
    one_shot_content=responses_wire.one_shot_content,
    routing_body=lambda body: dict(body, messages=responses_wire.score_messages(body),
                                   tools=responses_wire.function_tools(body)),
    new_message_id=responses_wire.new_message_id, stream_start=responses_wire.sse_start, stream_ping=responses_wire.sse_ping,
    stream_body=lambda msg_id, model, blocks, stop, usage: responses_wire.sse_body(blocks, stop, usage, msg_id=msg_id, model=model),
    stream_error=lambda msg_id, model, message: responses_wire.sse_error(message, msg_id=msg_id, model=model),
    message_json=responses_wire.message_json, error_json=responses_wire.error_json)
WIRES: dict[str, Wire] = {"/v1/messages": ANTHROPIC, "/v1/responses": RESPONSES}
wire = ANTHROPIC                                        # the endpoint's own errors (GET 401, 404) speak the Anthropic wire


class UnservedProvider(BridgeError):
    """The route names a provider this endpoint has no backend for; the server answers 503, never a guess."""


def _base_provider(p: str) -> str:
    """The backend a per-account provider maps to: `claude-cli#b` -> `claude-cli`."""
    return (p or "").split("#", 1)[0]


def _claude_session(reg: "Registry", *, account: str | None = None, config_dir: str | None = None, **kw) -> BridgeSession:
    return BridgeSession(gateway_url=reg.gateway_url, token=reg.token, claude_bin=reg.claude_bin,
                         config_dir=config_dir, **kw)


def _codex_session(reg: "Registry", *, account: str | None = None, config_dir: str | None = None, **kw) -> CodexSession:
    return CodexSession(codex_bin=reg.codex_bin, codex_home=reg.codex_home, **kw)   # Codex multi-account is Phase B


# Backend per route provider. Each factory takes the registry (for its binary and gateway settings, read at call time
# because tests rebind gateway_url after binding a port) and the session kwargs: system, tools, model.
SESSION_FACTORIES: dict[str, Callable[..., Any]] = {"claude-cli": _claude_session, "codex-cli": _codex_session}


class Registry:
    """`router(body, client)` picks the route of a new conversation (routing.choose); without one every conversation
    runs on `model` with the Claude CLI (the P0 behaviour, kept for testing the bridge alone). `executed(route)`
    records that a route really ran (core.log_execution), once per conversation."""

    def __init__(self, *, gateway_url: str, token: str, claude_bin: str | list[str] = "claude",
                 codex_bin: str | list[str] = "codex", codex_home: str | None = None, model: str | None = None,
                 router: Callable[[dict, str], dict] | None = None, executed: Callable[[dict], None] | None = None,
                 log_outcome: Callable[[dict, int, int], None] | None = None,
                 session_factories: dict[str, Callable[..., Any]] | None = None):
        self.gateway_url, self.token, self.claude_bin, self.model = gateway_url, token, claude_bin, model
        self.codex_bin, self.codex_home = codex_bin, codex_home
        self.router, self.executed, self.log_outcome = router, executed, log_outcome
        self._factories = SESSION_FACTORIES if session_factories is None else session_factories
        self._lock = threading.Lock()
        self.sessions: dict[str, Any] = {}

    def claude_config_dir(self, account: str | None) -> str | None:
        """The CLAUDE_CONFIG_DIR for a routed Claude account id, from tables/accounts.json; None (the default login)
        when no account is named or none is configured."""
        if not account:
            return None
        try:
            from cortados_router import core
            for a in core._vendor_accounts("claude", "claude", "claude-cli"):
                if a.get("account") == account:
                    return a.get("config_dir")
        except Exception:
            pass
        return None

    def new(self, turn: anthropic_wire.ClientTurn, *, keep: bool, route: dict | None = None):
        provider = route["provider"] if route else "claude-cli"
        factory = self._factories.get(provider) or self._factories.get(_base_provider(provider))
        if factory is None:
            raise UnservedProvider(f"routed to provider {provider!r}, which this endpoint has no backend for "
                                   f"(served: {', '.join(sorted(self._factories))})")
        kw = {"system": turn.system, "tools": turn.tools, "model": route["model"] if route else self.model}
        if route and route.get("account"):          # a multi-account route carries its account + the config_dir resolved AT selection
            cfg = route.get("config_dir")
            if not cfg:                             # a named account that cannot be resolved must refuse, never fall back to the default login
                raise UnservedProvider(f"routed to account {route['account']!r} on {route['provider']!r}, "
                                       f"which has no resolvable login (config_dir missing)")
            kw["account"], kw["config_dir"] = route["account"], cfg
        s = factory(self, **kw)
        s.route, s.route_logged, s.system = route, False, turn.system
        if keep:
            with self._lock:
                self.sessions[s.id] = s
        return s

    def find(self, turn: anthropic_wire.ClientTurn):
        """The live session whose replies are exactly this request's assistant history; None for a new conversation.
        No match, or more than one, is a LostSession: never continue a conversation on a guess."""
        if not turn.history:
            return None
        with self._lock:
            matches = [s for s in self.sessions.values() if s.history == turn.history]
        if len(matches) > 1:
            raise LostSession(f"{len(matches)} live conversations have this exact history; refusing to pick one")
        if not matches:
            raise LostSession("a conversation this endpoint no longer holds (restarted, expired, or history edited)")
        return matches[0]

    def get(self, session_id: str):
        with self._lock:
            return self.sessions.get(session_id)

    def drop(self, s) -> None:
        with self._lock:
            self.sessions.pop(s.id, None)
        s.close()

    def reap(self, idle_s: float) -> None:
        with self._lock:
            stale = [s for s in self.sessions.values() if time.time() - s.last_used > idle_s and not s.lock.locked()]
        for s in stale:
            logger.info("closing idle conversation %s", s.id[:8])
            self.drop(s)


class LostSession(LookupError):
    pass


class Handler(BaseHTTPRequestHandler):
    server: "ProviderServer"
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S

    def log_message(self, fmt, *args):                  # route http.server's stderr lines into logging
        logger.debug("%s " + fmt, self.address_string(), *args)

    # ------------------------------------------------------------------ plumbing
    def _authorized(self) -> bool:
        given = self.headers.get("x-cortados-token") or self.headers.get("x-api-key") or ""
        auth = self.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            given = given or auth[7:]
        return hmac.compare_digest(given.encode(), self.server.registry.token.encode())

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            # Both wires address the body by key; a list or scalar would crash the parser after the 401/400 checks.
            raise ValueError("body must be a JSON object")
        return body

    def _json(self, code: int, data: dict[str, Any], session=None) -> None:
        raw = json.dumps(data).encode()
        self.send_response(code)
        self._route_header(session)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code: int, kind: str, message: str, w: Wire = wire) -> None:
        self._json(code, w.error_json(kind, message))

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "conversations": len(self.server.registry.sessions)})
        if self.path in ("/", "/dashboard"):
            try:
                html = _dashboard_html()                # static page; the data it shows (/resources) still needs the token
            except OSError:
                return self._error(500, "api_error", "dashboard page unavailable")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path.startswith("/v1/models"):
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            return self._json(200, {"data": [{"type": "model", "id": wire.MODEL_ID, "display_name": "CortadOS"}],
                                    "has_more": False, "first_id": wire.MODEL_ID, "last_id": wire.MODEL_ID})
        if self.path == "/resources":
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            from cortados_router import core, resources     # the resource-pool snapshot: cached usage + scarcity + local devices + model-fit
            snap = resources.snapshot(core, local_config=getenv("CORTADOS_LOCAL_RESOURCES_CONFIG"))
            return self._json(200, snap)
        self._error(404, "not_found_error", self.path)

    def do_POST(self):
        path = self.path.split("?")[0]
        # Every error on a wire's path is in that wire's shape, the 401 and the unreadable body included: Codex prints
        # the Responses error body verbatim and would show an Anthropic envelope as noise.
        w = WIRES.get(path, wire)
        if not self._authorized():
            return self._error(401, "authentication_error", "bad or missing token", w)
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._error(400, "invalid_request_error", "body is not JSON", w)
        except ValueError as exc:                       # JSON, but not an object
            return self._error(400, "invalid_request_error", str(exc), w)
        if path in WIRES:
            return self._messages(body, w)
        if path == "/v1/messages/count_tokens":
            # Estimate only (chars / 4): the CLI behind cortados counts its own tokens and exposes no counter.
            return self._json(200, {"input_tokens": len(json.dumps(body)) // 4})
        if path == "/bridge/call":
            return self._bridge_call(body)
        self._error(404, "not_found_error", path)

    def _bridge_call(self, body: dict[str, Any]) -> None:
        s = self.server.registry.get(body.get("session", ""))
        if s is None or not hasattr(s, "wait_result"):   # only the Claude bridge's MCP shim calls back over HTTP
            return self._json(200, {"result": {"content": [{"type": "text", "text": "cortados: session closed"}], "isError": True}})
        try:
            result = s.wait_result(body["tool_use_id"], timeout=3600)
        except BridgeError as exc:
            result = {"content": [{"type": "text", "text": f"cortados: {exc}"}], "isError": True}
        self._json(200, {"result": result})

    @staticmethod
    def _api_pin(body: dict[str, Any]) -> str | None:
        """A request pinned to the anthropic-api credit lane: model `api/<claude-model>` (or bare `api` for the
        default). Returns the Claude model to run on the API lane, or None for a normal (S1-routed) request."""
        m = (body.get("model") or "").strip()
        if m == "api":
            return "claude-opus-5-5"
        if m.startswith("api/"):
            return m[4:].strip() or "claude-opus-5-5"
        return None

    def _serve_api_lane(self, body: dict[str, Any], model: str, wire: Wire) -> None:
        """Execute a pinned request on the anthropic-api credit lane under admission control: reserve a conservative
        max cost BEFORE dispatch, proxy to api.anthropic.com, then settle the actual cost (the full reservation if
        the actual is unknown). Refuse when the credit is spent, and fail closed if accounting is unavailable.
        Stateless — no session, no S1 (the pin is explicit)."""
        from cortados_router import api_credits
        from cortados_router.provider import anthropic_proxy
        if not anthropic_proxy.configured():
            return self._error(503, "overloaded_error",
                               "cortados: the anthropic-api lane has no key (set CORTADOS_ANTHROPIC_API_KEY)", wire)
        now = time.time()
        max_cost = api_credits.estimate_max_cost(body, model)
        try:
            res_id = api_credits.reserve(max_cost, now)
        except api_credits.LedgerError as exc:
            logger.error("anthropic-api accounting unavailable, refusing to spend blind: %s", exc)
            return self._error(503, "overloaded_error",
                               "cortados: the anthropic-api lane's accounting is unavailable; refusing", wire)
        if res_id is None:
            return self._error(429, "rate_limit_error",
                               f"cortados: the anthropic-api credit (${api_credits.budget_usd():.0f}) is spent or "
                               "fully reserved this period; it resets on the billing day", wire)
        logger.info("anthropic-api lane: reserved $%.2f for %s (committed $%.2f of $%.0f)",
                    max_cost, model, api_credits.committed(now), api_credits.budget_usd())
        usage: dict[str, Any] | None = None
        try:
            usage = anthropic_proxy.serve(self, body, model, now)
        except OSError as exc:
            logger.info("anthropic-api proxy: client left (%s)", exc)
        finally:
            if usage is None:
                charge = max_cost                 # serve raised; charge the full reservation (conservative)
            elif not usage:
                charge = 0.0                       # upstream rejected the request; nothing was billed
            elif usage.get("output_tokens") is not None:
                charge = api_credits.cost_from_usage(model, usage)   # a complete turn
            else:
                charge = max_cost                 # incomplete (e.g. a mid-stream disconnect); conservative
            try:
                api_credits.settle(res_id, charge, now)
            except api_credits.LedgerError as exc:
                logger.error("anthropic-api settle failed for %s (reservation retained, pacing stays safe): %s", res_id, exc)

    def _messages(self, body: dict[str, Any], wire: Wire) -> None:
        """One client request on `wire`: find or start its session, run one turn, answer in the wire's shape."""
        reg = self.server.registry
        dump = getenv("CORTADOS_PROVIDER_DUMP_DIR")     # debugging aid: every request body as received
        if dump:
            Path(dump).mkdir(parents=True, exist_ok=True)
            (Path(dump) / f"{time.time():.3f}.json").write_text(json.dumps(body, indent=1))
        try:
            api_model = self._api_pin(body)
            if api_model:
                # Pinned to the anthropic-api credit lane: proxy it directly, bypassing S1 and the session machinery.
                return self._serve_api_lane(body, api_model, wire)
            turn = wire.parse_request(body)
            # A tool-less request (a client's title or summary call) never joins a live session: it would hold that
            # session's lock while the session waits for a tool result. It gets its own throwaway CLI instead.
            one_shot = not turn.tools
            session = None if one_shot else reg.find(turn)
            if session is not None and getattr(session, "system", turn.system) != turn.system:
                # A running CLI keeps the system prompt it started with; a continuation that sends a different one
                # (changed `instructions`, a rewritten developer item) is served under the original, and says so here
                # rather than nowhere (P2 known limit: the conversation would otherwise have to be restarted).
                logger.warning("conversation %s: the system prompt changed mid-conversation; the running CLI keeps the "
                               "one it started with", session.id[:8])
            if one_shot:
                # The transcript re-reads the whole history, so a shape error in an older user item surfaces here,
                # not in parse_request: it is a request-shape error all the same and answers 400.
                turn.content, turn.tool_results = wire.one_shot_content(body), {}
        except wire.WireError as exc:
            return self._error(400, "invalid_request_error", str(exc), wire)
        except LostSession as exc:
            return self._error(409, "invalid_request_error", str(exc), wire)
        created = session is None                       # this request opens the conversation (or is a one-shot)
        if session is None:
            route = None
            if reg.router:
                # The route is chosen once, for the whole conversation: switching models mid-conversation would lose
                # the CLI session and its prompt cache.
                try:
                    route = reg.router(wire.routing_body(body), self._client())
                except Exception as exc:                # scorer, tables or route log failed: a dependency, not the client
                    logger.exception("routing failed")
                    return self._error(503, "overloaded_error", f"cortados: routing failed: {type(exc).__name__}: {exc}", wire)
                if route.get("error"):
                    return self._error(503, "overloaded_error", f"cortados: {route['error']}", wire)
                logger.info("route %s: %s/%s band %s%s", route["route_id"], route["provider"], route["model"],
                            route["band"], " (S1 degraded)" if route.get("degraded") else "")
            try:
                session = reg.new(turn, keep=not one_shot, route=route)
            except UnservedProvider as exc:
                return self._error(503, "overloaded_error", f"cortados: {exc}", wire)
            except BridgeError as exc:                  # the backend refused to start (version pin, isolation, login)
                logger.warning("backend start failed: %s", exc)
                return self._error(502, "api_error", str(exc), wire)
        msg_id = wire.new_message_id()
        if turn.stream:
            try:
                self.send_response(200)
                self._route_header(session)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(wire.stream_start(msg_id, turn.model))
                self.wfile.flush()
            except OSError as exc:
                # The client left before the first byte. A one-shot's backend lives nowhere but here, and a session
                # this request just opened has no history a retry could find again (find() never matches an opening
                # request), so both are closed now instead of outliving the request until the idle reaper. An
                # existing conversation is kept: its client may well come back.
                logger.info("conversation %s: client gone before the stream started (%s)", session.id[:8], exc)
                if one_shot:
                    session.close()
                elif created:
                    reg.drop(session)
                return
        outcome: dict[str, Any] = {}

        def work():
            # One deadline covers waiting for the session, writing to the CLI and reading its reply.
            deadline = time.time() + TURN_TIMEOUT_S
            if not session.lock.acquire(timeout=TURN_TIMEOUT_S):
                outcome["error"] = BridgeError(f"conversation busy for {TURN_TIMEOUT_S:.0f}s")
                return
            try:
                if not created and session.history != turn.history:
                    # Registry.find ran before the lock: a second continuation with the same history (a client
                    # retry, two overlapping requests) found the session too, and the one that lost the lock now
                    # sees a conversation that has moved on. Delivering its results would fail inside the backend
                    # and drop a healthy conversation; it is a stale request, answered 409, and the session is kept.
                    outcome["stale"] = LostSession("the conversation moved on while this request waited for it: another "
                                                   "request with the same history was served first; send the current history")
                    return
                if turn.tool_results:
                    session.deliver(turn.tool_results, timeout=deadline - time.time())
                else:
                    session.send_user(turn.content, timeout=deadline - time.time())
                _turn_t0 = time.time()
                outcome["turn"] = session.run_turn(deadline - time.time())
                _turn_ms = int((time.time() - _turn_t0) * 1000)
                session.history.append(anthropic_wire.reply_fingerprint(outcome["turn"][0]))
                if session.route and not session.route_logged and reg.executed:
                    try:
                        reg.executed(session.route)
                        session.route_logged = True     # only after the write succeeded; a failure retries next turn
                    except Exception:
                        # The reply is good; only its training record failed. Answer the client and say so loudly.
                        logger.exception("execution log for route %s failed%s", session.route.get("route_id"),
                                         " (one-shot: no later turn to retry)" if one_shot else "; retrying next turn")
                    else:
                        if reg.log_outcome:             # the turn's size and duration, for training; once per conversation, same row as the route
                            blocks = outcome["turn"][0]
                            chars = sum(len(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                            # off the critical path: a slow SQLite write must not hold the session lock or delay the reply.
                            # Best effort — a failed outcome log is logged, not retried (the request label is already saved).
                            route = session.route
                            def _log_outcome(route=route, chars=chars, ms=_turn_ms):
                                try:
                                    reg.log_outcome(route, chars, ms)
                                except Exception:
                                    logger.exception("outcome log for route %s failed", route.get("route_id"))
                            threading.Thread(target=_log_outcome, name="cortados-outcome-log", daemon=True).start()
            except BridgeError as exc:
                outcome["error"] = exc
            finally:
                session.lock.release()
                if one_shot:
                    # The throwaway CLI's work is done; the worker closes it here because this thread never touches
                    # the socket: a client that stopped reading can block the handler on a keepalive or on the reply
                    # write for as long as it likes, and a one-shot is in no registry for the reaper to find.
                    session.close()
                    outcome["closed"] = True

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        client_gone = False
        try:
            while worker.is_alive():
                worker.join(PING_S)
                if worker.is_alive() and turn.stream and not client_gone:
                    try:
                        self.wfile.write(wire.stream_ping())
                        self.wfile.flush()
                    except OSError as exc:
                        # The client hung up mid-turn (Ctrl-C). The worker keeps running until the CLI answers or
                        # the deadline passes, so it is still joined and its CLI reaped; only the writes stop.
                        client_gone = True
                        logger.info("conversation %s: client gone mid-turn (%s); waiting for the turn to end",
                                    session.id[:8], exc)
            if "stale" in outcome:
                logger.warning("conversation %s: stale continuation refused: %s", session.id[:8], outcome["stale"])
                if client_gone:
                    return
                if turn.stream:
                    self.wfile.write(wire.stream_error(msg_id, turn.model, str(outcome["stale"])))
                    return
                return self._error(409, "invalid_request_error", str(outcome["stale"]), wire)
            if "error" in outcome:
                logger.warning("conversation %s failed: %s", session.id[:8], outcome["error"])
                if not one_shot:
                    reg.drop(session)
                if client_gone:
                    return
                if turn.stream:
                    self.wfile.write(wire.stream_error(msg_id, turn.model, str(outcome["error"])))
                    return
                return self._error(502, "api_error", str(outcome["error"]), wire)
            blocks, stop, usage = outcome["turn"]
            logger.info("conversation %s: %s, %d blocks%s", session.id[:8], stop, len(blocks),
                        " (client gone; reply not sent)" if client_gone else "")
            if not client_gone:
                try:
                    if turn.stream:
                        for chunk in wire.stream_body(msg_id, turn.model, blocks, stop, usage):
                            self.wfile.write(chunk)
                        self.wfile.flush()
                    else:
                        self._json(200, wire.message_json(msg_id, turn.model, blocks, stop, usage), session=session)
                except OSError as exc:
                    client_gone = True
                    logger.info("conversation %s: client gone while the reply was written (%s)", session.id[:8], exc)
            if client_gone and created and not one_shot:
                # The reply never reached the client, and this request opened the conversation: its history on the
                # client side is still empty, so no retry can ever find this session (find() never matches an
                # opening request). Drop it now rather than leaving a backend to the idle reaper. An existing
                # conversation is kept: its client has the earlier replies and can continue.
                reg.drop(session)
        finally:
            if one_shot and not outcome.get("closed"):
                session.close()                         # the worker never got to it (it did not run): close it here

    def _client(self) -> str:
        """The calling tool, from its User-Agent ("claude-cli/2.1.304 (external, cli)" → "claude-cli",
        "codex_exec/0.163.0 (Mac OS 27.0.1; arm64)" → "codex_exec"); it names the route log's platform."""
        return (self.headers.get("user-agent") or "unknown").split("/")[0].strip() or "unknown"

    def _route_header(self, session) -> None:
        r = getattr(session, "route", None)
        if r:
            self.send_header("x-cortados-route", f"{r['provider']}/{r['model']} band={r['band']} id={r['route_id']}")


class ProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, registry: Registry):
        super().__init__(addr, Handler)
        self.registry = registry


def serve(host: str, port: int, token: str, *, claude_bin: str, codex_bin: str, codex_home: str | None,
          fixed_model: str | None) -> None:
    router = executed = log_turn_outcome = None
    if not fixed_model:
        from cortados_router import core                  # loads the tables and the live scarcity reader
        from cortados_router.provider import routing

        def router(body, client):
            return routing.choose(core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin=claude_bin,
                                  codex_bin=codex_bin)

        def executed(route):
            core.log_execution(route["route_id"], route["provider"], route["model"])

        def log_turn_outcome(route, response_chars, turn_ms):
            core.log_outcome(route["route_id"], response_chars, turn_ms)

    registry = Registry(gateway_url=f"http://{host}:{port}", token=token, claude_bin=claude_bin, codex_bin=codex_bin,
                        codex_home=codex_home, model=fixed_model, router=router, executed=executed,
                        log_outcome=log_turn_outcome)
    httpd = ProviderServer((host, port), registry)

    def reaper():
        while True:
            time.sleep(60)
            registry.reap(IDLE_S)

    threading.Thread(target=reaper, daemon=True, name="cortados-provider-reaper").start()
    logger.info("cortados provider on http://%s:%d (model %s, %s)", host, port, wire.MODEL_ID,
                f"fixed to claude CLI {fixed_model}, no routing" if fixed_model else "routed by S1")
    try:
        httpd.serve_forever()
    finally:
        for s in list(registry.sessions.values()):
            registry.drop(s)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(getenv("CORTADOS_PROVIDER_PORT") or DEFAULT_PORT))
    ap.add_argument("--claude-bin", default=getenv("CORTADOS_PROVIDER_CLAUDE_BIN") or "claude")
    ap.add_argument("--codex-bin", default=getenv("CORTADOS_PROVIDER_CODEX_BIN") or "codex",
                    help="the Codex CLI that serves codex-cli routes as `codex app-server` (env CORTADOS_PROVIDER_CODEX_BIN)")
    ap.add_argument("--codex-home", default=getenv("CORTADOS_PROVIDER_CODEX_HOME"),
                    help="a dedicated, logged-in CODEX_HOME for the Codex backend (env CORTADOS_PROVIDER_CODEX_HOME); "
                         "unset, the backend runs on the user's real Codex home")
    ap.add_argument("--fixed-model", default=getenv("CORTADOS_PROVIDER_FIXED_MODEL"),
                    help="skip routing and run every conversation on this Claude CLI model (bridge testing)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = getenv("CORTADOS_PROVIDER_TOKEN")
    if not token:
        token = secrets.token_urlsafe(24)
        print(f"CORTADOS_PROVIDER_TOKEN not set; this run's token: {token}", file=sys.stderr, flush=True)
    serve(a.host, a.port, token, claude_bin=a.claude_bin, codex_bin=a.codex_bin, codex_home=a.codex_home,
          fixed_model=a.fixed_model)


if __name__ == "__main__":
    main()
