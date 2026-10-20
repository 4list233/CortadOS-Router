"""The CortadOS endpoint: an Anthropic Messages server whose one model, `cortados`, is served by bridged headless CLIs.

Owner of the conversation registry: which bridge session a request belongs to. A request with no assistant messages
starts a new session; any other goes to the one live session whose replies are exactly its assistant history, and
fails with 409 when there is none or more than one. A request without tools always gets its own throwaway CLI.

Run:  python3 -B -m cortados_router.provider.server [--port 8432]
      then  ANTHROPIC_BASE_URL=http://127.0.0.1:8432 ANTHROPIC_AUTH_TOKEN=<token> claude --model cortados
P1: S1 routes each new conversation to a model this endpoint serves (routing.py; docs/PROVIDER-PLAN.md).
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
from cortados_router.provider import anthropic_wire as wire
from cortados_router.provider.bridge import BridgeError, BridgeSession

DEFAULT_PORT = 8432                                     # next to S1's 8431
# A turn covers the model's thinking until it finishes or asks for a tool; long agentic turns run minutes.
TURN_TIMEOUT_S = float(getenv("CORTADOS_PROVIDER_TURN_TIMEOUT_S") or 1800)
# A conversation the client has not touched for this long is closed; its CLI process holds memory and a login slot.
IDLE_S = float(getenv("CORTADOS_PROVIDER_IDLE_S") or 1800)
# SSE pings while a turn runs, so the client's read timeout does not fire during a long think.
PING_S = 10.0
logger = logging.getLogger("cortados.provider")


class Registry:
    """`router(body, client)` picks the route of a new conversation (routing.choose); without one every conversation
    runs on `model` (the P0 behaviour, kept for testing the bridge alone). `executed(route)` records that a route
    really ran (core.log_execution), once per conversation."""

    def __init__(self, *, gateway_url: str, token: str, claude_bin: str = "claude", model: str | None = None,
                 router: Callable[[dict, str], dict] | None = None, executed: Callable[[dict], None] | None = None,
                 session_factory=BridgeSession):
        self.gateway_url, self.token, self.claude_bin, self.model = gateway_url, token, claude_bin, model
        self.router, self.executed = router, executed
        self._factory = session_factory
        self._lock = threading.Lock()
        self.sessions: dict[str, BridgeSession] = {}

    def new(self, turn: wire.ClientTurn, *, keep: bool, route: dict | None = None) -> BridgeSession:
        s = self._factory(system=turn.system, tools=turn.tools, gateway_url=self.gateway_url, token=self.token,
                          claude_bin=self.claude_bin, model=route["model"] if route else self.model)
        s.route, s.route_logged = route, False
        if keep:
            with self._lock:
                self.sessions[s.id] = s
        return s

    def find(self, turn: wire.ClientTurn) -> BridgeSession | None:
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

    def get(self, session_id: str) -> BridgeSession | None:
        with self._lock:
            return self.sessions.get(session_id)

    def drop(self, s: BridgeSession) -> None:
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
        return json.loads(self.rfile.read(n) or b"{}")

    def _json(self, code: int, data: dict[str, Any], session: BridgeSession | None = None) -> None:
        raw = json.dumps(data).encode()
        self.send_response(code)
        self._route_header(session)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code: int, kind: str, message: str) -> None:
        self._json(code, wire.error_json(kind, message))

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "conversations": len(self.server.registry.sessions)})
        if self.path.startswith("/v1/models"):
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            return self._json(200, {"data": [{"type": "model", "id": wire.MODEL_ID, "display_name": "CortadOS"}],
                                    "has_more": False, "first_id": wire.MODEL_ID, "last_id": wire.MODEL_ID})
        self._error(404, "not_found_error", self.path)

    def do_POST(self):
        if not self._authorized():
            return self._error(401, "authentication_error", "bad or missing token")
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except ValueError:
            return self._error(400, "invalid_request_error", "body is not JSON")
        if path == "/v1/messages":
            return self._messages(body)
        if path == "/v1/messages/count_tokens":
            # Estimate only (chars / 4): the CLI behind cortados counts its own tokens and exposes no counter.
            return self._json(200, {"input_tokens": len(json.dumps(body)) // 4})
        if path == "/bridge/call":
            return self._bridge_call(body)
        self._error(404, "not_found_error", path)

    def _bridge_call(self, body: dict[str, Any]) -> None:
        s = self.server.registry.get(body.get("session", ""))
        if s is None:
            return self._json(200, {"result": {"content": [{"type": "text", "text": "cortados: session closed"}], "isError": True}})
        try:
            result = s.wait_result(body["tool_use_id"], timeout=3600)
        except BridgeError as exc:
            result = {"content": [{"type": "text", "text": f"cortados: {exc}"}], "isError": True}
        self._json(200, {"result": result})

    def _messages(self, body: dict[str, Any]) -> None:
        reg = self.server.registry
        dump = getenv("CORTADOS_PROVIDER_DUMP_DIR")     # debugging aid: every request body as received
        if dump:
            Path(dump).mkdir(parents=True, exist_ok=True)
            (Path(dump) / f"{time.time():.3f}.json").write_text(json.dumps(body, indent=1))
        try:
            turn = wire.parse_request(body)
            # A tool-less request (a client's title or summary call) never joins a live session: it would hold that
            # session's lock while the session waits for a tool result. It gets its own throwaway CLI instead.
            one_shot = not turn.tools
            session = None if one_shot else reg.find(turn)
        except wire.WireError as exc:
            return self._error(400, "invalid_request_error", str(exc))
        except LostSession as exc:
            return self._error(409, "invalid_request_error", str(exc))
        if one_shot:
            turn.content, turn.tool_results = wire.one_shot_content(body["messages"]), {}
        if session is None:
            route = None
            if reg.router:
                # The route is chosen once, for the whole conversation: switching models mid-conversation would lose
                # the CLI session and its prompt cache.
                try:
                    route = reg.router(body, self._client())
                except Exception as exc:                # scorer, tables or route log failed: a dependency, not the client
                    logger.exception("routing failed")
                    return self._error(503, "overloaded_error", f"cortados: routing failed: {type(exc).__name__}: {exc}")
                if route.get("error"):
                    return self._error(503, "overloaded_error", f"cortados: {route['error']}")
                logger.info("route %s: %s/%s band %s%s", route["route_id"], route["provider"], route["model"],
                            route["band"], " (S1 degraded)" if route.get("degraded") else "")
            session = reg.new(turn, keep=not one_shot, route=route)
        msg_id = wire.new_message_id()
        if turn.stream:
            self.send_response(200)
            self._route_header(session)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(wire.sse_start(msg_id, turn.model))
            self.wfile.flush()
        outcome: dict[str, Any] = {}

        def work():
            # One deadline covers waiting for the session, writing to the CLI and reading its reply.
            deadline = time.time() + TURN_TIMEOUT_S
            if not session.lock.acquire(timeout=TURN_TIMEOUT_S):
                outcome["error"] = BridgeError(f"conversation busy for {TURN_TIMEOUT_S:.0f}s")
                return
            try:
                if turn.tool_results:
                    session.deliver(turn.tool_results)
                else:
                    session.send_user(turn.content, timeout=deadline - time.time())
                outcome["turn"] = session.run_turn(deadline - time.time())
                session.history.append(wire.reply_fingerprint(outcome["turn"][0]))
                if session.route and not session.route_logged and reg.executed:
                    try:
                        reg.executed(session.route)
                        session.route_logged = True     # only after the write succeeded; a failure retries next turn
                    except Exception:
                        # The reply is good; only its training record failed. Answer the client and say so loudly.
                        logger.exception("execution log for route %s failed%s", session.route.get("route_id"),
                                         " (one-shot: no later turn to retry)" if one_shot else "; retrying next turn")
            except BridgeError as exc:
                outcome["error"] = exc
            finally:
                session.lock.release()

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(PING_S)
            if worker.is_alive() and turn.stream:
                self.wfile.write(wire.sse_ping())
                self.wfile.flush()
        if one_shot:
            session.close()
        if "error" in outcome:
            logger.warning("conversation %s failed: %s", session.id[:8], outcome["error"])
            if not one_shot:
                reg.drop(session)
            if turn.stream:
                self.wfile.write(wire.sse("error", wire.error_json("api_error", str(outcome["error"]))))
                return
            return self._error(502, "api_error", str(outcome["error"]))
        blocks, stop, usage = outcome["turn"]
        logger.info("conversation %s: %s, %d blocks", session.id[:8], stop, len(blocks))
        if turn.stream:
            for chunk in wire.sse_body(blocks, stop, usage):
                self.wfile.write(chunk)
            self.wfile.flush()
        else:
            self._json(200, wire.message_json(msg_id, turn.model, blocks, stop, usage), session=session)

    def _client(self) -> str:
        """The calling tool, from its User-Agent ("claude-cli/2.1.304 (external, cli)" → "claude-cli")."""
        return (self.headers.get("user-agent") or "unknown").split("/")[0].strip() or "unknown"

    def _route_header(self, session: BridgeSession | None) -> None:
        r = getattr(session, "route", None)
        if r:
            self.send_header("x-cortados-route", f"{r['provider']}/{r['model']} band={r['band']} id={r['route_id']}")


class ProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, registry: Registry):
        super().__init__(addr, Handler)
        self.registry = registry


def serve(host: str, port: int, token: str, *, claude_bin: str, fixed_model: str | None) -> None:
    router = executed = None
    if not fixed_model:
        from cortados_router import core                  # loads the tables and the live scarcity reader
        from cortados_router.provider import routing

        def router(body, client):
            return routing.choose(core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin=claude_bin)

        def executed(route):
            core.log_execution(route["route_id"], route["provider"], route["model"])

    registry = Registry(gateway_url=f"http://{host}:{port}", token=token, claude_bin=claude_bin, model=fixed_model,
                        router=router, executed=executed)
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
    ap.add_argument("--fixed-model", default=getenv("CORTADOS_PROVIDER_FIXED_MODEL"),
                    help="skip routing and run every conversation on this Claude CLI model (bridge testing)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = getenv("CORTADOS_PROVIDER_TOKEN")
    if not token:
        token = secrets.token_urlsafe(24)
        print(f"CORTADOS_PROVIDER_TOKEN not set; this run's token: {token}", file=sys.stderr, flush=True)
    serve(a.host, a.port, token, claude_bin=a.claude_bin, fixed_model=a.fixed_model)


if __name__ == "__main__":
    main()
