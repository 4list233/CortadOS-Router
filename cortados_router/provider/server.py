"""The CortadOS endpoint: an Anthropic Messages server whose one model, `cortados`, is served by bridged headless CLIs.

Owner of the conversation registry: which bridge session a request belongs to. A request with no assistant messages
starts a new session; any other goes to the one live session whose replies are exactly its assistant history, and
fails with 409 when there is none or more than one, or with a terminal 400 naming the failure's category when the
request belongs to a conversation closed because its backend failed (a bounded, time-limited tombstone keyed by the
conversation itself; both wires share the lookup).
A request without tools always gets its own throwaway CLI.

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
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):                           # run as a file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cortados_router.compat import getenv
from cortados_router.provider import anthropic_wire, local_lane, responses_wire, routing
from cortados_router.provider.bridge import BridgeError, BridgeSession, claude_env
from cortados_router.provider.codex_bridge import CodexSession

DEFAULT_PORT = 8432                                     # next to S1's 8431
# A turn covers the model's thinking until it finishes or asks for a tool; long agentic turns run minutes.
TURN_TIMEOUT_S = float(getenv("CORTADOS_PROVIDER_TURN_TIMEOUT_S") or 1800)
# A conversation the client has not touched for this long is closed; its CLI process holds memory and a login slot.
IDLE_S = float(getenv("CORTADOS_PROVIDER_IDLE_S") or 1800)
# A conversation dropped because its backend FAILED leaves a tombstone, so the client's retry of the failed request,
# or its next message on the same history, is told why instead of a misleading 409. A tombstone is keyed by the
# conversation itself (Registry.failure_key: the wire, the calling client, the conversation identity the client sends
# (CONVERSATION_ID_HEADERS) and a canonical digest of the history the request carried, free of client-regenerated text:
# anthropic_wire.conversation_digest), so another conversation that happens to contain the same reply never receives
# it. A request without a client conversation identity is never tombstoned and never matches one: the digest alone
# cannot tell two independent conversations with the same prefix apart (it keeps the ordinary 409).
# It holds only an allowlisted diagnostic (failure_diagnostic): a fixed category and message, never backend or tool text.
# TTL: a client's automatic retries finish within minutes; 15 minutes also covers a person re-sending by hand, and
# after that the conversation is as unknown as any other (409). Max entries: one per recently failed conversation,
# capped so a burst of failures (a vendor outage across many sessions) cannot grow memory without bound; the oldest
# goes first.
TOMBSTONE_TTL_S = 900.0
TOMBSTONE_MAX = 256
# Recently closed conversations kept for the 409 diagnostic (Registry.closed): enough to cover the live conversations
# of a busy endpoint, and an hour, past which a reap no longer explains a 409.
CLOSED_RECORD_MAX = 128
CLOSED_RECORD_TTL_S = 3600.0
# Largest last response a session keeps for a retry replay (_remember_reply): 1 MiB covers ordinary replies (text plus
# tool calls) while bounding what each idle session can hold. One reply per live session, so the aggregate is at most
# live sessions x this cap, and the idle reaper (IDLE_S) bounds how long any of it is held.
REPLAY_MAX_BYTES = 1 << 20
# The answer to a request on a failed conversation must be terminal: an api_error (5xx, or an SSE `api_error`) is
# what Claude Code and the Anthropic SDK retry, so a retryable shape would make the client re-send the same doomed
# request until its retry budget runs out. 400 invalid_request_error is not retried (the SDK retries 408, 409, 429
# and 5xx only), and `x-should-retry: false` says so explicitly to the SDK; the message says to start a new
# conversation. The Responses wire uses its own non-retried code (responses_wire.TERMINAL_CODE).
TERMINAL_ERROR = "invalid_request_error"
TERMINAL_STATUS = 400
# A request that waited TURN_TIMEOUT_S for a session another request still holds: the conversation is healthy, only
# this request is late, so the answer is retryable. 529 overloaded_error is Anthropic's own "busy, retry" shape
# (the SDK retries 5xx); Codex retries a 5xx too.
BUSY_STATUS = 529

# What a tombstone may say about a failure: (category, fixed message), chosen by matching the bridge's own BridgeError
# templates. Only these fixed strings are ever replayed; an error message can embed tool arguments (a malformed
# item/tool/call), backend output or paths, and none of that may reach another request. First match wins.
_FAILURE_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    # Any status but "completed" (codex_bridge raises `the Codex turn {status}: ...` for every one of them, including
    # statuses this endpoint has never seen); a completed turn with leftover tool calls is a tool_result_mismatch.
    (r"^the Codex turn (?!completed )", "codex_turn_failed", "the Codex backend failed the turn"),
    (r"^the headless CLI reported an error", "backend_reported_error", "the Claude backend reported an error"),
    (r"within \d+s|deadline passed|produced nothing for", "backend_timeout",
     "the backend did not answer in time"),
    (r"exited with code|is gone:|the session was closed", "backend_exited", "the backend process exited"),
    (r"^cannot start", "backend_start_failed", "the backend could not be started"),
    (r"isolation|non-bridged tool|rerouted the turn|started the thread (on|with)", "backend_policy_violation",
     "the backend broke the endpoint's isolation or routing policy"),
    (r"tool results? for|pending tool calls|without a result for|unanswered|without consuming",
     "tool_result_mismatch", "the tool results did not match the backend's pending tool calls"),
    (r"malformed|reused tool call id|refused|without a (thread|turn) id|cliVersion", "backend_protocol_error",
     "the backend broke its protocol"),
    # Turn-identity breaks from the Codex code-mode yield handling (events or calls naming a turn that is not the live
    # one, ended-turn bookkeeping that cannot continue): the app-server broke the turn protocol.
    (r"the id of an earlier turn|belongs? to no live turn|this session never started|is neither current"
     r"|not the current turn|no late results to continue from|outstanding at once",
     "backend_protocol_error", "the backend broke its protocol"),
)
# Codex's codexErrorInfo values (app-server protocol enum): the one detail of a failed Codex turn safe to replay,
# because it is a fixed identifier from this list, never free text.
_CODEX_ERROR_INFO = frozenset({
    "contextWindowExceeded", "usageLimitExceeded", "httpConnectionFailed", "responseStreamConnectionFailed",
    "internalServerError", "unauthorized", "badRequest", "sandboxError", "responseStreamDisconnected",
    "responseTooManyFailedAttempts", "threadRollbackFailed", "other"})


def failure_diagnostic(exc: object) -> str:
    """The allowlisted, replayable description of a backend failure: `<category>: <fixed message>`, plus an HTTP
    status (Claude) or codexErrorInfo value (Codex) only when it has exactly the known shape. Never raw error text."""
    text = str(exc)
    for pattern, kind, message in _FAILURE_CATEGORIES:
        if re.search(pattern, text):
            break
    else:
        kind, message = "backend_error", "the backend failed"
        # Not silent: an unmatched BridgeError template loses its category (and any safe detail). Only the category
        # is logged, never the reason text, which can carry tool arguments or backend output.
        logger.info("uncategorised backend failure replayed as backend_error; add a pattern to _FAILURE_CATEGORIES")
    detail = ""
    if kind == "backend_reported_error":
        m = re.match(r"^the headless CLI reported an error: API Error: ([1-5]\d\d)\b", text)
        detail = f" (API status {m.group(1)})" if m else ""
    elif kind == "codex_turn_failed":
        m = re.search(r"\(([A-Za-z]+)\)$", text)
        detail = f" ({m.group(1)})" if m and m.group(1) in _CODEX_ERROR_INFO else ""
    return f"{kind}: {message}{detail}"

_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
_dashboard_cache: dict = {}


# The page holds no data or secret, so it needs no token; these headers keep it from being cached, framed, sniffed as
# another type, or sending its Referer. Its Content-Security-Policy is _dashboard_csp(): it pins the one inline script.
DASHBOARD_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
# script-src carries only the hash of the page's own inline script (no 'unsafe-inline'): markup injected into the page
# could not run a script. base-uri/form-action/frame-ancestors 'none': no base rewrite, no form post, no framing.
_DASHBOARD_CSP = ("default-src 'none'; script-src '{h}'; style-src 'unsafe-inline'; connect-src 'self'; "
                  "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class _ScriptPage(HTMLParser):
    """The page's one inline script, as an HTML parser sees it: the parser is the only source of both the count and the
    hashed body, so a decoy in a comment, an attribute or a CDATA section can neither be counted nor be hashed in
    place of the script that runs. Tag names are lowercased by the parser, so any case counts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.n = 0
        self.inside = False
        self.closed = False
        self.parts: list[str] = []
        self.bad: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.n += 1
            self.inside = True
            if attrs:   # src= would load another script; any attribute is outside what the page needs
                self.bad = self.bad or "script tag with attributes"

    def handle_startendtag(self, tag, attrs):
        if tag == "script":
            self.n += 1
            self.bad = self.bad or "self-closing script tag"

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)   # script content arrives raw (CDATA mode), possibly in several chunks

    def handle_endtag(self, tag):
        if tag == "script" and self.inside:
            self.inside, self.closed = False, True


def _dashboard_script(page: bytes) -> str:
    """The body of the page's single inline <script>, or ValueError: any count but one, attributes, a self-closing or
    unclosed script, undecodable bytes, or markup the parser cannot handle."""
    p = _ScriptPage()
    try:
        p.feed(page.decode("utf-8"))   # UnicodeDecodeError is a ValueError
        p.close()
    except (NotImplementedError, AssertionError) as exc:
        # Python 3.9's HTMLParser raises these on malformed declarations (`<![bogus]>`); unhandled, the request thread
        # dies and the client sees a dropped connection instead of a 500
        raise ValueError(f"dashboard page markup not parseable: {type(exc).__name__}") from exc
    if p.n != 1:
        raise ValueError(f"dashboard page must hold exactly one inline <script>, found {p.n}")
    if p.bad:
        raise ValueError(f"dashboard page: {p.bad}")
    if p.inside or not p.closed:
        raise ValueError("dashboard page: unclosed script")
    return "".join(p.parts)


def _dashboard_html() -> bytes:
    """The served dashboard page (static HTML/JS; the page itself asks for the token and fetches /v1/cortados/usage).
    Read once with its CSP: the page must hold exactly one inline <script>, whose sha256 (of the body the parser
    found, UTF-8 encoded as the browser hashes it) the CSP pins."""
    if "b" not in _dashboard_cache:
        b = _DASHBOARD_PATH.read_bytes()
        body = _dashboard_script(b)
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        _dashboard_cache["csp"] = _DASHBOARD_CSP.format(h=f"sha256-{digest}")
        _dashboard_cache["b"] = b
    return _dashboard_cache["b"]


def _dashboard_csp() -> str:
    """The Content-Security-Policy for the page _dashboard_html() returned."""
    _dashboard_html()
    return _dashboard_cache["csp"]
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
    `stream_error(msg_id, model, message[, terminal=])` also receive the id `stream_start` announced and the requested model,
    because the Responses wire must echo both in its final event (Codex compares the model) while the Anthropic
    wire needs neither. `terminal=True` marks an error no retry can fix with each wire's non-retried kind."""

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
    stream_error=lambda msg_id, model, message, terminal=False: anthropic_wire.sse(
        "error", anthropic_wire.error_json(TERMINAL_ERROR if terminal else "api_error", message)),
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
    stream_error=lambda msg_id, model, message, terminal=False: responses_wire.sse_error(message, msg_id=msg_id, model=model,
                                                                                         terminal=terminal),
    message_json=responses_wire.message_json, error_json=responses_wire.error_json)
WIRES: dict[str, Wire] = {"/v1/messages": ANTHROPIC, "/v1/responses": RESPONSES}
wire = ANTHROPIC                                        # the endpoint's own errors (GET 401, 404) speak the Anthropic wire


class UnservedProvider(BridgeError):
    """The route names a provider this endpoint has no backend for; the server answers 503, never a guess."""


def _base_provider(p: str) -> str:
    """The backend a per-account provider maps to: `claude-cli#b` -> `claude-cli`."""
    return (p or "").split("#", 1)[0]


# Request header a routed subagent sends (the cortados-routed-subagents mod) to be told the real routed model and its
# lane's remaining window in the response's `model` field. Absent, every response names `cortados` as before.
REVEAL_HEADER = "x-cortados-reveal"
# Request header naming a model a new conversation must not route to: an independent review never runs on the model
# that built the work (the builder never approves its own work). Read once, when the route is chosen.
EXCLUDE_MODEL_HEADER = "x-cortados-exclude-model"
# What the header may carry: a model id (`claude-opus-5-5[1m]`, `sub/opus`, `api/claude-sonnet-5`), nothing else.
# Checked before it reaches the router, a log line or an error message.
_EXCLUDE_MODEL_RE = re.compile(r"[A-Za-z0-9._/\[\]:-]{1,128}")
_EXCLUDE_MODEL_BAD = "cortados: x-cortados-exclude-model is not a model id"
# A review may need to stay off several models (every builder of the work), so the header is a comma list; capped so
# a header cannot fan out into an unbounded exclusion set (or empty the pool by sheer length).
EXCLUDE_MODEL_MAX = 8


def _exclude_model_header(headers) -> tuple[str | None, bool]:
    """The x-cortados-exclude-model header: (its items, stripped and comma-joined, or None when absent/empty, valid).
    Empty items are ignored; each item must be a model id, and more than EXCLUDE_MODEL_MAX items is invalid."""
    items = [i.strip() for i in (headers.get(EXCLUDE_MODEL_HEADER) or "").split(",")]
    items = [i for i in items if i]
    if not items:
        return None, True
    valid = len(items) <= EXCLUDE_MODEL_MAX and all(_EXCLUDE_MODEL_RE.fullmatch(i) for i in items)
    return ",".join(items), valid


# Request header carrying the client's own quota floor: a percentage of weekly headroom below which the provider must not
# serve the request on an account (routing.min_left_block). It only tightens; absent, routing is exactly as before.
MIN_LEFT_HEADER = "x-cortados-min-left-pct"
_MIN_LEFT_BAD = "cortados: x-cortados-min-left-pct is not a number from 0 to 100"


def _min_left_header(headers) -> tuple[float | None, bool]:
    """(floor, valid) of the x-cortados-min-left-pct header; (None, True) when absent or empty."""
    raw = (headers.get(MIN_LEFT_HEADER) or "").strip()
    if not raw:
        return None, True
    try:
        v = float(raw)
    except ValueError:
        return None, False
    return (v, True) if 0.0 <= v <= 100.0 else (None, False)


# Request header naming the client's run (the routed-subagents mod runs each subagent as a headless `claude -p`, whose
# JSON output names only `cortados`): every routed request of the run is recorded under it, and
# GET /v1/cortados/runs/<id> tells the client which model really ran (to label it, and to keep a later review off it).
RUN_ID_HEADER = "x-cortados-run-id"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_RUN_ID_BAD = "cortados: x-cortados-run-id is not a run id"
RUNS_PATH = "/v1/cortados/runs/"

# What a refusal record (RunRecords.refuse) and the refusal log line hold: the status, the error type, an endpoint id
# from _ENDPOINT_IDS, and a fixed reason string of this endpoint's own (a call site's, the allowlisted
# failure_diagnostic of a backend failure, else the default for the status below). Never any backend text, request
# text or raw path: backend output can echo credentials (a local gate's token, a key in an upstream error body) and a
# path can carry a client's secret (/v1/cortados/runs/<anything>). The client's response still carries the full
# message, and backend failures keep their existing provider log lines.
_REFUSAL_REASONS = {
    400: "the request was refused as invalid",
    401: "bad or missing token",
    404: "not found",
    409: "the request does not match the conversation's current state",
    413: "the request is larger than the model's context",
    429: "the lane's rate or credit limit is reached",
    500: "the endpoint failed internally",
    502: "the backend failed",
    503: "no backend or lane is available for the request",
    BUSY_STATUS: "the conversation is busy with another request",
}


def endpoint_id(path: str | None) -> str:
    """The allowlisted name a refusal log line gives a request path: never the path itself."""
    p = (path or "").split("?")[0]
    if p == "/v1/messages":
        return "messages"
    if p == "/v1/messages/count_tokens":
        return "count_tokens"
    if p == "/v1/responses":
        return "responses"
    if p.startswith(RUNS_PATH):
        return "runs"
    if p.startswith("/v1/models"):
        return "models"
    if p in ("/health", "/resources", "/bridge/call"):
        return p[1:].replace("/", "_")
    if p in ("/", "/dashboard"):
        return "dashboard"
    return "other"


# Headers a client sends that name its conversation, for the failed-session tombstone (Registry.failure_key). Each was
# confirmed on the real client's own requests to a capture endpoint (2026-11-02):
#   x-cortados-run-id          this endpoint's own run header; the headless tools set it per run.
#   x-claude-code-session-id   Claude Code 2.1.316 sends X-Claude-Code-Session-Id on every /v1/messages request (the
#                              same id is the session_id inside metadata.user_id). The main loop and its subagents
#                              share it; their histories differ, so identity + digest still names one conversation.
#   session-id                 Codex 0.166.0 sends its thread id as `session-id` (and `thread-id`, x-client-request-id,
#                              prompt_cache_key) on /v1/responses; the 0.163.0 binary carries the same header names.
# Every one present is part of the identity; with none, the request has no identity and gets no tombstone.
# These headers are client-asserted, not authenticated: a client holding the endpoint token could send another's. They
# scope failure diagnostics to a conversation; they do not isolate one client from another.
CONVERSATION_ID_HEADERS = (RUN_ID_HEADER, "x-claude-code-session-id", "session-id")


class RunRecords:
    """What each client run was routed to, in memory: run id -> up to PER_RUN entries {provider, model, label, tools}.
    Bounded (LRU over MAX_RUNS ids) because the ids are client-chosen; a restart forgets them, which only costs the
    client its label. Locked: the server is threaded and a run's requests may arrive concurrently.
    Client-influenced, so advisory: any holder of the shared token can add entries under any run id it names. That
    is inside the token's existing trust boundary (the same holder can already route and spend); the record only
    labels runs and steers reviews, and never authorizes anything."""
    MAX_RUNS = 512
    PER_RUN = 16

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        # Refused requests per run id: a request refused before routing (413 too large, a bad pin) has no route entry,
        # and without this a headless client could only learn "no routed request recorded". Recorded only for an
        # authenticated request (Handler._note_refusal), so a caller without the token cannot add or evict entries.
        self._refusals: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()

    def refuse(self, run_id: str, *, status: int, kind: str, reason: str) -> None:
        """Record one refusal: the status, the error type and a fixed reason (Handler._note_refusal; never raw text)."""
        entry = {"status": int(status), "type": str(kind), "reason": str(reason), "at": round(time.time(), 3)}
        with self._lock:
            entries = self._refusals.pop(run_id, [])
            entries = (entries + [entry])[-self.PER_RUN:]      # the most recent PER_RUN refusals
            self._refusals[run_id] = entries
            while len(self._refusals) > self.MAX_RUNS:
                self._refusals.popitem(last=False)

    def record(self, run_id: str, *, provider: str, model: str, label: str, tools: bool) -> None:
        entry = {"provider": provider, "model": model, "label": label, "tools": bool(tools)}
        with self._lock:
            entries = self._runs.pop(run_id, [])
            if len(entries) < self.PER_RUN:
                entries.append(entry)
            elif entry["tools"]:
                # Full: a tool-bearing entry (one that can write code) is always kept, so tool-less side calls (titles,
                # summaries) can never crowd out the builder. It replaces the oldest tool-less entry, if there is one;
                # a run already full of tool-bearing entries keeps its first PER_RUN. A tool-less entry beyond the
                # cap is dropped.
                oldest_toolless = next((i for i, e in enumerate(entries) if not e["tools"]), None)
                if oldest_toolless is not None:
                    del entries[oldest_toolless]
                    entries.append(entry)
            self._runs[run_id] = entries
            while len(self._runs) > self.MAX_RUNS:
                self._runs.popitem(last=False)

    def all_refusals(self) -> list[dict[str, Any]]:
        """Every recorded refusal (for GET /v1/cortados/usage's 24-hour counts)."""
        with self._lock:
            return [dict(e) for es in self._refusals.values() for e in es]

    def model_counts(self) -> dict[str, int]:
        """Routed entries per provider/model since start (in memory, bounded; for GET /v1/cortados/usage)."""
        with self._lock:
            out: dict[str, int] = {}
            for es in self._runs.values():
                for e in es:
                    k = f"{e['provider']}/{e['model']}"
                    out[k] = out.get(k, 0) + 1
            return out

    def summary(self, run_id: str) -> dict[str, Any] | None:
        """The run's answer: the primary entry (the first that had tools, the agent's own loop, else the first), the
        distinct models in order (`models`, every entry, tool-less side calls included) and the distinct models of the
        entries that had tools (`builder_models`: the ones that could have written code; empty if none); None for an
        unknown (or evicted) id."""
        with self._lock:
            entries = self._runs.get(run_id)
            refusals = list(self._refusals.get(run_id) or [])
            if entries is None and not refusals:
                return None
            if entries is not None:
                self._runs.move_to_end(run_id)
                entries = list(entries)
        if not entries:                                 # only refusals: nothing was routed
            return {"run_id": run_id, "model": None, "label": None, "provider": None, "models": [],
                    "builder_models": [], "refusals": refusals}
        primary = next((e for e in entries if e["tools"]), entries[0])
        out = {"run_id": run_id, "model": primary["model"], "label": primary["label"],
               "provider": primary["provider"], "models": list(dict.fromkeys(e["model"] for e in entries)),
               "builder_models": list(dict.fromkeys(e["model"] for e in entries if e["tools"]))}
        if refusals:
            out["refusals"] = refusals
        return out
# Route provider -> scarcity lane, for a route that does not carry its resolved candidate's lane. (The anthropic-api
# lane is not here: an api/-pinned request never has a route and reveals its % in _serve_api_lane instead.)
_PROVIDER_LANES = {"claude-cli": "claude", "codex-cli": "codex"}
# Lanes whose cached reading carries no login identity, so it cannot be confirmed to be the executing login's: the
# Codex /status sample runs on the sampler's CODEX_HOME, the backend on --codex-home / CODEX_HOME / ~/.codex. Their
# reveal names the model only, never another login's %.
_UNIDENTIFIED_LANES = {"codex"}


def _route_lane(route: dict) -> str | None:
    """The scarcity lane a route draws on: the resolved candidate's own lane (per-account: claude#b), else the
    provider's vendor lane with its account suffix kept (claude-cli#b -> claude#b)."""
    chosen = (route.get("route") or {}).get("chosen") or {}
    if chosen.get("lane"):
        return chosen["lane"]
    base, _, acct = (route.get("provider") or "").partition("#")
    lane = _PROVIDER_LANES.get(base)
    return f"{lane}#{acct}" if lane and acct else lane


def _ledger_turn(session: Any, usage: dict | None) -> None:
    """One usage-ledger row per completed backend turn (unitcost learns tokens per % from it). Only routed sessions carry a
    lane; a fixed-model session has no route, so its tokens cannot be charged to a pool and are not written."""
    route = getattr(session, "route", None)
    lane = _route_lane(route) if route else None
    if not lane:
        return
    from cortados_router import usage_ledger
    usage_ledger.record_async(lane, str(route.get("model") or ""), usage)


_ledger_hook_warned: set[str] = set()


def _log_ledger_hook_failure(exc: BaseException) -> None:
    """Type name only (the message could carry a path or usage payload), once per type, so a broken hook cannot flood the log."""
    kind = type(exc).__name__
    if kind not in _ledger_hook_warned:
        _ledger_hook_warned.add(kind)
        logger.warning("usage ledger hook failed (%s); the turn is unaffected; later %s failures are not logged", kind, kind)


def _route_pct_left(route: dict) -> float | None:
    """% left in the pool the route's model draws on, via core.headroom_pct_for (the min headroom across the lane's
    general windows and the model's own pool; None unless the reading is fresh, for the login the route EXECUTES on,
    and observes that pool). The login is what Registry.new runs the session on: the route's config_dir only when it
    names an account, else the default login, stated as the effective login of the env the bridge would start the CLI
    with (bridge.claude_env), so an inherited CLAUDE_CONFIG_DIR / token is never mistaken for the sampler's."""
    from cortados_router import core
    lane = _route_lane(route)
    if not lane or lane.split("#", 1)[0] in _UNIDENTIFIED_LANES:
        return None
    cfg = route.get("config_dir") if route.get("account") else None
    chosen = (route.get("route") or {}).get("chosen") or {}
    return core.headroom_pct_for({"lane": lane, "model": str(route["model"]), "config_dir": cfg,
                                  "login": core.claude_login_identity(claude_env(cfg)),
                                  "pool": chosen.get("pool"), "family": chosen.get("family")})


def _pct_label(model: str, pct_left: float | None) -> str:
    return model if pct_left is None else f"{model} · {round(pct_left)}% left"


def reveal_model(route: dict | None) -> str | None:
    """`<model> · <pct>% left` for a revealing client, `<model>` when the % is unknown, None (answer `cortados`) when
    there is no route or it is degraded. Never raises: a reveal is cosmetic and must not fail a turn."""
    try:
        if not route or route.get("degraded") or route.get("error") or not route.get("model"):
            return None
        model = str(route["model"])
        try:
            left = _route_pct_left(route)
        except Exception:
            logger.debug("reveal: lane %% unavailable for %s", model, exc_info=True)
            left = None
        return _pct_label(model, left)
    except Exception:
        logger.debug("reveal failed", exc_info=True)
        return None


def api_reveal_suffix() -> str | None:
    """` · <pct>% left` of the anthropic-api credit window, for an api/-pinned request that asked to reveal. The
    upstream response already names the real model; this only adds the %. None when the meter cannot be read."""
    try:
        from cortados_router import api_credits
        used = float(api_credits.window_numbers(time.time())[0])
        return f" · {round(100 - max(0.0, min(100.0, used)))}% left"
    except Exception:
        logger.debug("reveal: anthropic-api meter unavailable", exc_info=True)
        return None


def _claude_session(reg: "Registry", *, account: str | None = None, config_dir: str | None = None, **kw) -> BridgeSession:
    return BridgeSession(gateway_url=reg.gateway_url, token=reg.token, claude_bin=reg.claude_bin,
                         config_dir=config_dir, **kw)


def _codex_session(reg: "Registry", *, account: str | None = None, config_dir: str | None = None, **kw) -> CodexSession:
    # Code mode must not yield to the model before the request's turn budget is spent (codex_isolation.EXEC_YIELD_KEY).
    return CodexSession(codex_bin=reg.codex_bin, codex_home=reg.codex_home, exec_yield_ms=int(TURN_TIMEOUT_S * 1000),
                        **kw)   # Codex multi-account is Phase B


# Backend per route provider. Each factory takes the registry (for its binary and gateway settings, read at call time
# because tests rebind gateway_url after binding a port) and the session kwargs: system, tools, model.
SESSION_FACTORIES: dict[str, Callable[..., Any]] = {"claude-cli": _claude_session, "codex-cli": _codex_session}


class LocalSession:
    """A local-pinned conversation (local_lane.py). The turns are relayed statelessly, so there is no backend to hold;
    what is kept is the conversation's binding to its model and the fingerprints of the replies it sent, which is how
    find() continues it and _model_change keeps it on its model, exactly as for a Claude or Codex pin."""

    def __init__(self, reg: "Registry", *, model: str, **_):
        self.id, self.model = uuid.uuid4().hex, model
        self.history: list[str] = []
        self.lock = threading.Lock()
        self.last_used = time.time()

    def close(self) -> None:
        pass


class Registry:
    """`router(body, client[, exclude_model=])` picks the route of a new conversation (routing.choose); without one every conversation
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
        # floor_turn(route, min_left_pct) -> refusal or None: rechecks and reserves a continuation's executing account
        # under the client's x-cortados-min-left-pct (routing.floor_turn). None: a floor on a continuation fails closed.
        self.floor_turn: Callable[[dict | None, float], str | None] | None = None
        self._factories = SESSION_FACTORIES if session_factories is None else session_factories
        self._lock = threading.Lock()
        self.sessions: dict[str, Any] = {}
        # failure_key of a failed conversation -> (diagnostic, failed_at, short id); under self._lock, oldest first.
        self.tombstones: "OrderedDict[str, tuple[str, float, str]]" = OrderedDict()
        self.runs = RunRecords()                    # x-cortados-run-id -> what its routed requests ran on
        # Recently closed conversations, for the 409 diagnostic only (find -> _lost_diagnostic): (closed_at, reason,
        # first reply fingerprint, history length). A 409 whose first fingerprint is here was a conversation this
        # endpoint closed itself (reaped, dropped, failed), which tells a reap from a lost response or an edited
        # history. Fingerprints are digests of reply text, never logged; bounded by count and age so a long-running
        # endpoint keeps only what can still explain a 409 burst.
        self.closed: "deque[tuple[float, str, str, int]]" = deque(maxlen=CLOSED_RECORD_MAX)

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

    def new(self, turn: anthropic_wire.ClientTurn, *, keep: bool, route: dict | None = None,
            scope: str | None = None, wire_path: str | None = None):
        provider = route["provider"] if route else "claude-cli"
        factory = self._factories.get(provider) or self._factories.get(_base_provider(provider))
        if factory is None and route and route.get("local") and provider.startswith(local_lane.LANE_PREFIX):
            factory = LocalSession                  # a local pin: no backend process, only the conversation's binding
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
        s.scope = scope                             # conversation_scope of the request that opened it (failure())
        s.wire_path = wire_path                     # the wire it was opened on (/v1/messages, /v1/responses): _replays
        s.tool_names, s.tools_warned = {t["name"] for t in turn.tools}, False   # what the backend was started with
        if keep:
            with self._lock:
                self.sessions[s.id] = s
        return s

    @staticmethod
    def failure_key(turn: anthropic_wire.ClientTurn, *, wire_path: str, client: str,
                    identity: str | None) -> str | None:
        """The identity of the conversation a request belongs to, for its tombstone: the wire, the calling client (its
        User-Agent name), the conversation identity the client sent (Handler._conversation_identity), and the wire's
        canonical digest of the history before the new turn (user content, tool results and assistant fingerprints;
        system text, reminders, thinking and cache markers excluded: anthropic_wire.conversation_digest). A retry of a
        failed request, and the next message a person types after it, carry that same identity and history; an
        unrelated conversation does not. None for an opening request (no conversation to name) and for a request
        without an identity: two independent conversations can share a prefix, so the digest alone would hand one the
        other's failure. find never matches None, and fail publishes nothing under it."""
        if not turn.history or not identity:
            return None
        return anthropic_wire.history_digest(wire_path, client, identity, turn.prefix_digest)

    @staticmethod
    def conversation_scope(*, wire_path: str, client: str, identity: str | None) -> str | None:
        """Who a conversation belongs to without its history: the wire, the client's User-Agent name and the
        conversation identity it sent; None without an identity. Kept on each live session (Registry.new) so failure()
        can tell a sibling's live conversation (same identity, see CONVERSATION_ID_HEADERS) from a stranger's."""
        return anthropic_wire.history_digest(wire_path, client, identity) if identity else None

    def find(self, turn: anthropic_wire.ClientTurn, failure_key: str | None = None, scope: str | None = None,
             wire_path: str | None = None):
        """The live session whose replies are exactly this request's assistant history; None for a new conversation.
        No match, or more than one, is a LostSession: never continue a conversation on a guess. With no match, a
        tombstone under `failure_key` makes it a FailedSession instead; a retry of the session's last answered turn
        (see Replay) raises Replay with the stored response. More than one such retry candidate is not replayed (it
        falls through to the ordinary 409, whose diagnostic counts the candidates)."""
        if not turn.history:
            return None
        with self._lock:
            matches = [s for s in self.sessions.values() if s.history == turn.history]
            replays = [] if matches else self._replays(turn, scope, wire_path)
            if len(replays) == 1:
                # A client in a retry loop touches nothing else, so the replay itself must count as use: otherwise the
                # idle reaper closes the conversation the client is still trying to recover.
                replays[0][0].last_used = time.time()
        if len(matches) > 1:
            logger.warning("409 lost conversation: class=ambiguous candidates=%d request_len=%d",
                           len(matches), len(turn.history))
            raise LostSession(f"{len(matches)} live conversations have this exact history; refusing to pick one")
        if not matches:
            if len(replays) == 1:
                s, last = replays[0]
                logger.info("conversation %s: replaying its last response to a retry of the answered turn "
                            "(history %d replies); the backend is not called", s.id[:8], len(turn.history))
                raise Replay(s, last)
            dead = self.failure(failure_key, turn.history, scope)
            if dead:
                raise dead
            logger.warning("409 lost conversation: %s", self._lost_diagnostic(turn))
            raise LostSession("a conversation this endpoint no longer holds (restarted, expired, or history edited)")
        return matches[0]

    def _replays(self, turn: anthropic_wire.ClientTurn, scope: str | None = None,
                 wire_path: str | None = None) -> list:
        """(session, last_reply) for each live session this request is an exact retry of: its history is the session's
        minus the last reply, and its new user turn (turn_fingerprint) is the one that reply answered. Caller holds
        self._lock. last_reply[0] is the history length the reply belongs to, so a reply recorded for an older turn
        (or a session mid-way through appending) never matches. Only sessions in the request's `scope` (the same
        client and conversation identity, see conversation_scope) qualify, as in failure(): a stored reply is never
        handed to another client's conversation that happens to share the history. A request without a conversation
        identity (scope None, see CONVERSATION_ID_HEADERS) is never replayed: two identity-less sessions share the None
        scope, so None == None would hand one client's reply to a stranger (another User-Agent, even another wire); it
        gets the ordinary 409. The session must also have been opened on the request's wire (`wire_path`), so a
        /v1/messages reply is never replayed on /v1/responses."""
        if scope is None or wire_path is None:
            return []
        n, fp, out = len(turn.history), None, []
        for s in self.sessions.values():
            last = getattr(s, "last_reply", None)
            if (last and getattr(s, "scope", None) == scope and getattr(s, "wire_path", None) == wire_path
                    and last[0] == len(s.history) == n + 1
                    and s.history[:n] == turn.history):
                fp = fp or anthropic_wire.turn_fingerprint(turn)
                if last[1] == fp:
                    out.append((s, last))
        return out

    def _lost_diagnostic(self, turn: anthropic_wire.ClientTurn) -> str:
        """Why a continuation matched no live session, for the 409 log line: counts, lengths and a classification only
        (no fingerprints, no content). `retry_of_answered_turn`: the request is the best candidate's history minus its
        last reply (a lost response, retried, with a different user turn, no stored reply to replay, or more than one
        live session it could be a retry of); `client_behind=<k>`: the request's history is a strict prefix of the
        candidate's, k >= 2 replies short (a retry of an older turn: only the last one is ever replayed);
        `client_ahead`: the candidate's history is a strict prefix of the request's; `diverged_at=<i>`: the first
        index where they differ; `reaped_recently`: no live candidate, but this endpoint closed a conversation with
        the same first reply within CLOSED_RECORD_TTL_S; `no_candidate`: nothing shares the first reply."""
        h, now = turn.history, time.time()
        with self._lock:
            hists = [list(s.history) for s in self.sessions.values()]
            closed = [c for c in self.closed if now - c[0] <= CLOSED_RECORD_TTL_S and c[2] == h[0]]

        def lcp(a: list) -> int:
            i = 0
            while i < min(len(a), len(h)) and a[i] == h[i]:
                i += 1
            return i
        cands = [c for c in hists if c and c[0] == h[0]]
        if not cands:
            if closed:
                at, reason, _, length = closed[-1]
                return (f"class=reaped_recently candidates=0 request_len={len(h)} closed_len={length} "
                        f"closed_reason={reason} closed_ago_s={int(now - at)}")
            return f"class=no_candidate candidates=0 request_len={len(h)} live_sessions={len(hists)}"
        best = max(cands, key=lambda c: (lcp(c), -abs(len(c) - len(h))))
        common = lcp(best)
        behind = len(best) - len(h)
        if common == len(h) and behind == 1:
            cls = "retry_of_answered_turn"
        elif common == len(h) and behind >= 2:
            cls = f"client_behind={behind}"
        elif common == len(best) < len(h):
            cls = "client_ahead"
        else:
            cls = f"diverged_at={common}"
        return (f"class={cls} candidates={len(cands)} common_prefix={common} request_len={len(h)} "
                f"candidate_len={len(best)}")

    def note_closed(self, s, reason: str) -> None:
        """Remember a closed conversation for _lost_diagnostic (see self.closed)."""
        hist = getattr(s, "history", None)
        if hist:
            with self._lock:
                self.closed.append((time.time(), reason, hist[0], len(hist)))

    def failure(self, key: str | None, history: list | None = None,
                scope: str | None = None) -> "FailedSession | None":
        """The FailedSession for this conversation's live tombstone, or None; expired tombstones are pruned on the way.
        None too when a live session in the same `scope` has a history that strictly extends `history`: see below."""
        if not key:
            return None
        now = time.time()
        with self._lock:
            while self.tombstones:
                oldest = next(iter(self.tombstones))
                if now - self.tombstones[oldest][1] <= TOMBSTONE_TTL_S:
                    break
                del self.tombstones[oldest]
            dead = self.tombstones.get(key)
            # Claude Code's session id is shared by its main loop and every subagent, so two siblings can carry the
            # same identity and history: sibling A fails (tombstone), sibling B advances on its live session, loses
            # that response, and retries its previous history. That retry matches A's tombstone, but it is B's
            # lost-response retry of a healthy conversation, and A's terminal error would kill B. A live session in
            # the same scope whose history strictly extends the request's names that case: the ordinary 409 instead.
            # Checked under the same lock as the tombstone lookup, so no fail() or turn can slip in between.
            if dead and scope and history is not None:
                n = len(history)
                if any(getattr(s, "scope", None) == scope and len(s.history) > n and s.history[:n] == history
                       for s in self.sessions.values()):
                    dead = None
        if not dead:
            return None
        diagnostic, failed_at, short = dead
        out = FailedSession(f"cortados: conversation {short} failed at "
                            f"{time.strftime('%H:%M:%S', time.localtime(failed_at))} ({diagnostic}; details are in "
                            "the provider log) and was closed: start a new conversation")
        out.diagnostic = diagnostic                     # the allowlisted failure_diagnostic, for the refusal record
        return out

    def fail(self, s, exc: object, key: str | None, *, unkeyed_reason: str | None = None) -> None:
        """Close a conversation whose backend failed. The caller holds `s.lock` and passes the failing request's own
        failure_key, so no request can reach the backend in between and the key never depends on session state.
        Removing the session and installing the tombstone happen together under the registry lock: no request ever
        sees both the live session and its tombstone, or neither. The backend is closed after, outside that lock (a
        slow close must not stall every other request). The tombstone keeps only failure_diagnostic(exc).
        `unkeyed_reason`: why a continuation has no key (no client identity); logged once per process."""
        global _unkeyed_logged
        diagnostic = failure_diagnostic(exc)
        if not key and unkeyed_reason and not _unkeyed_logged:
            _unkeyed_logged = True
            logger.info("failed conversation left no tombstone (%s); its retries get the ordinary 409. "
                        "Logged once per process.", unkeyed_reason)
        self.note_closed(s, "failed")
        with self._lock:
            self.sessions.pop(s.id, None)
            if key:
                self.tombstones.pop(key, None)
                self.tombstones[key] = (diagnostic, time.time(), str(s.id)[:8])
                while len(self.tombstones) > TOMBSTONE_MAX:
                    self.tombstones.popitem(last=False)
        s.close()

    def get(self, session_id: str):
        with self._lock:
            return self.sessions.get(session_id)

    def drop(self, s, reason: str = "dropped") -> None:
        self.note_closed(s, reason)
        with self._lock:
            self.sessions.pop(s.id, None)
        s.close()

    def reap(self, idle_s: float) -> None:
        with self._lock:
            stale = [s for s in self.sessions.values() if time.time() - s.last_used > idle_s and not s.lock.locked()]
        for s in stale:
            logger.info("closing idle conversation %s", s.id[:8])
            self.drop(s, "reaped")


def _remember_reply(session, turn, msg_id: str, model: str, result) -> None:
    """Keep the session's last response for one replay (Replay), replacing the previous one: only the last answered
    turn can be retried by a client that lost its response, so one per session is all that is needed. Not kept when
    its serialized size passes REPLAY_MAX_BYTES (a replay is then a 409, as before), so idle sessions cannot pin
    large replies in memory. Called before the reply's fingerprint is appended, under the session lock."""
    blocks, stop, usage = result
    try:
        size, why = len(json.dumps([blocks, usage], default=str)), None
        if size > REPLAY_MAX_BYTES:
            why = f"{size} bytes, over REPLAY_MAX_BYTES ({REPLAY_MAX_BYTES})"
    except (TypeError, ValueError, RecursionError) as exc:
        why = f"it does not serialize ({type(exc).__name__})"
    if why:
        # Its retry, if one comes, is then the ordinary 409: said here so that 409 can be told from a lost session.
        logger.info("conversation %s: last response not kept for a retry replay: %s", str(session.id)[:8], why)
        session.last_reply = None
        return
    session.last_reply = (len(session.history) + 1, anthropic_wire.turn_fingerprint(turn), msg_id, model, blocks,
                          stop, usage)


_unkeyed_logged = False                    # Registry.fail has logged a continuation it could not tombstone


class LostSession(LookupError):
    pass


class Replay(Exception):
    """A request that retries a live session's last answered turn (Registry._replays): the client never received that
    response (a lost or timed-out reply), so the session's history is one reply ahead of the request's and every
    later request would 409. The stored response is sent again; the backend is not called and the session is
    unchanged. `last` is the session's last_reply: (history length, turn fingerprint, msg_id, model, blocks, stop,
    usage)."""

    def __init__(self, session, last):
        super().__init__("replay")
        self.session, self.last = session, last


class FailedSession(LostSession):
    """The request belongs to a conversation that was closed because its backend failed (a tombstone): answered with
    a terminal error (TERMINAL_STATUS / TERMINAL_ERROR) naming the failure's category, not the 409 of a conversation
    this endpoint never held or has forgotten."""
    diagnostic: str | None = None


def _failed_reason(exc: object) -> str:
    """The refusal reason for a request on a failed conversation: fixed text plus the tombstone's allowlisted
    failure_diagnostic (never the backend's own text)."""
    diag = getattr(exc, "diagnostic", None)
    return "the conversation already failed (terminal)" + (f": {diag}" if diag else "")


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
        ok = hmac.compare_digest(given.encode(), self.server.registry.token.encode())
        self._authed = ok                               # per request: do_GET/do_POST reset it first (keep-alive reuse)
        return ok

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            # Both wires address the body by key; a list or scalar would crash the parser after the 401/400 checks.
            raise ValueError("body must be a JSON object")
        return body

    def _json(self, code: int, data: dict[str, Any] | bytes, session=None, headers: dict[str, str] | None = None) -> None:
        # bytes: a body the caller already encoded (and validated), sent as is, so a large view is serialised once.
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self._route_header(session)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code: int, kind: str, message: str, w: Wire = wire, *, reason: str | None = None) -> None:
        """Answer an error in the wire's shape. `message` goes to the client only; the log line and the run record get
        the fixed `reason` of _note_refusal."""
        self._note_refusal(code, kind, reason=reason)
        self._json(code, w.error_json(kind, message))

    def _note_refusal(self, code: int, kind: str, *, reason: str | None = None) -> None:
        """The ONE place a refusal is logged and recorded, for every failure path (JSON errors, SSE errors, the local
        relay). Logged with an endpoint id, its status, type and fixed reason (a 413 or 400 used to leave no line at
        all). An authenticated POST that names a valid run id also records it there, so GET /v1/cortados/runs/<id> can
        tell a headless client why it was refused. `reason` must be one of this endpoint's own fixed strings (see
        _REFUSAL_REASONS). Bookkeeping only: it never raises into the response."""
        try:
            where = endpoint_id(self.path)
            if not getattr(self, "_authed", False):
                # Logged apart and never recorded: an unauthenticated caller must not create or evict run records.
                logger.warning("unauthenticated %s %s refused with %s %s", self.command, where, code, kind)
                return
            reason = reason or _REFUSAL_REASONS.get(int(code), "refused")
            logger.warning("refused %s %s with %s %s: %s", self.command, where, code, kind, reason)
            if self.command == "POST":
                run_id, valid = self._run_id()
                if run_id and valid:
                    self.server.registry.runs.refuse(run_id, status=code, kind=kind, reason=reason)
        except Exception as exc:
            logger.warning("refusal bookkeeping failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        self._authed = False
        if self.path == "/health":
            return self._json(200, {"ok": True, "conversations": len(self.server.registry.sessions)})
        if self.path in ("/", "/dashboard"):
            try:
                html = _dashboard_html()                # static page; the data it shows (/v1/cortados/usage) still needs the token
                csp = _dashboard_csp()
            except (OSError, ValueError) as exc:
                logger.warning("dashboard page unavailable: %s", type(exc).__name__)
                return self._error(500, "api_error", "dashboard page unavailable")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(html)))
            for k, v in DASHBOARD_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path.startswith("/v1/models"):
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            data = [{"type": "model", "id": wire.MODEL_ID, "display_name": "CortadOS"}]
            # The sub/ pins are offered only when this endpoint routes: a --fixed-model endpoint runs every conversation
            # on its one model, so listing pins it would not honour would mislead the client's model picker.
            if self.server.registry.router is not None:
                from cortados_router import core
                try:
                    pins = routing.pinnable_models(core)
                except Exception as exc:        # a broken table: say so, rather than offer a picker that hides models
                    logger.warning("/v1/models: cannot list the pinnable models: %s", exc)
                    return self._error(500, "api_error", f"cortados: cannot list the pinnable models: {exc}")
                data += [{"type": "model", "id": f"{routing.SUB_PREFIX}{m}",
                          "display_name": f"{routing.readable_model_name(m)} · CortadOS"} for _, m in pins]
                try:
                    local = routing.local_pins(core)
                except local_lane.LocalConfigError as exc:   # same stance as a broken table, without the file paths
                    logger.warning("/v1/models: cannot list the local pins: %s", exc)
                    return self._error(500, "api_error", "cortados: cannot list the local pins: local node "
                                       f"{getattr(exc, 'node', None) or '?'} is misconfigured on the provider "
                                       "(see provider log)")
                data += [{"type": "model", "id": f"{routing.SUB_PREFIX}{local_lane.pin_id(m)}",
                          "display_name": f"{m['name']} · CortadOS ({n['label']})"} for n, m in local]
            return self._json(200, {"data": data, "has_more": False, "first_id": data[0]["id"],
                                    "last_id": data[-1]["id"]})
        if self.path == "/resources":
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            from cortados_router import core, resources     # the resource-pool snapshot: cached usage + scarcity + local devices + model-fit
            snap = resources.snapshot(core, local_config=getenv("CORTADOS_LOCAL_RESOURCES_CONFIG"))
            return self._json(200, snap)
        if self.path.split("?")[0] == "/v1/cortados/usage":
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            try:   # a bug in a source must not drop the connection: a clean 500, detail (type only) in the log
                view = self.server.usage_view(self.server.registry)
                # the live view arrives encoded (usage.collect memoises the bytes); a dict is encoded strictly here,
                # before any byte is sent, so a NaN is a clean 500 and never invalid JSON
                body = view if isinstance(view, bytes) else json.dumps(view, allow_nan=False).encode()
            except Exception as exc:
                logger.warning("usage: view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: usage view unavailable (see provider log)")
            return self._json(200, body)
        if self.path.split("?")[0].startswith(RUNS_PATH):
            if not self._authorized():
                return self._error(401, "authentication_error", "bad or missing token")
            run_id = self.path.split("?")[0][len(RUNS_PATH):]
            if not _RUN_ID_RE.fullmatch(run_id):
                return self._error(400, "invalid_request_error", _RUN_ID_BAD)
            found = self.server.registry.runs.summary(run_id)
            if found is None:
                return self._error(404, "not_found_error", f"cortados: no routed request recorded for run {run_id}")
            return self._json(200, found)
        self._error(404, "not_found_error", self.path)

    def do_POST(self):
        self._authed = False
        path = self.path.split("?")[0]
        # Every error on a wire's path is in that wire's shape, the 401 and the unreadable body included: Codex prints
        # the Responses error body verbatim and would show an Anthropic envelope as noise.
        w = WIRES.get(path, wire)
        if not self._authorized():
            return self._error(401, "authentication_error", "bad or missing token", w)
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._error(400, "invalid_request_error", "body is not JSON", w, reason="body is not JSON")
        except ValueError as exc:                       # JSON, but not an object
            return self._error(400, "invalid_request_error", str(exc), w)
        if path in WIRES:
            if not self._run_id()[1]:                   # checked before any routing or spend, like the exclude header
                return self._error(400, "invalid_request_error", _RUN_ID_BAD, w, reason=_RUN_ID_BAD)
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
        if not api_credits.configured_orgs():
            return self._error(503, "overloaded_error",
                               "cortados: the anthropic-api lane has no key (set CORTADOS_ANTHROPIC_API_KEY)", wire)
        bad = api_credits.unbounded_server_tools(body)
        if bad:                                   # a server tool whose cost we cannot bound/reconcile -> never dispatch
            return self._error(400, "invalid_request_error",
                               f"cortados: the anthropic-api credit lane cannot bound the cost of server tool(s) "
                               f"{', '.join(sorted(set(bad)))} (billed by server-side time); not permitted on this lane", wire)
        body = api_credits.enforce_server_tool_caps(body)   # cap metered tools to what we reserve, so admission is a real bound
        # A revealing client (the routed-subagents mod) also sees the credit window's % left after the real model the
        # upstream response names; read BEFORE this turn reserves. Without the header nothing about the relay changes.
        reveal = (api_reveal_suffix() if self._wants_reveal() else None)
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
                               f"cortados: the anthropic-api credit (${api_credits.total_budget_usd():.0f}) is spent or "
                               "fully reserved this period; it resets on the billing day", wire)
        key = api_credits.key_for_reservation(res_id)
        if not key:
            # The reservation's org was deconfigured between reserve and dispatch (rare). Release it and refuse
            # rather than fall back to another org's key, which would misattribute the spend.
            api_credits.settle(res_id, 0.0, now)
            return self._error(503, "overloaded_error",
                               "cortados: the anthropic-api lane's reserved org is no longer configured", wire)
        logger.info("anthropic-api lane: reserved $%.2f for %s (committed $%.2f of $%.0f)",
                    max_cost, model, api_credits.committed(now), api_credits.total_budget_usd())
        # Recorded once admitted (it is about to run). The label is the reveal the response carries when one was asked
        # for; otherwise the model (the meter is not read just for the record).
        self._record_run(provider="anthropic-api", model=model, label=model + reveal if reveal else model,
                         tools=bool(body.get("tools")))
        usage: dict[str, Any] = {}
        outcome = "unknown"                       # if serve raises, we do not know what was billed -> conservative
        try:
            usage, outcome = anthropic_proxy.serve(self, body, model, now, key=key,
                                                   **({"model_suffix": reveal} if reveal else {}))
        except OSError as exc:
            logger.info("anthropic-api proxy: client left (%s)", exc)
        except Exception as exc:
            # Any other failure out of serve() (e.g. a response-parse error after the POST was dispatched) leaves the
            # spend genuinely unknown -> keep outcome "unknown" so we settle conservatively rather than crash/undercharge.
            logger.error("anthropic-api proxy failed after dispatch (settling conservatively): %s", exc)
        finally:
            if outcome == "rejected":
                charge = 0.0                       # the API returned an error; nothing was billed
            elif outcome == "completed":
                charge = api_credits.cost_from_usage(model, usage)   # a terminal response: settle the actual cost
            else:
                # unknown (timeout / transport error / mid-stream disconnect): the full reservation, but never below
                # the cost we already observed on the wire, so a partial-but-expensive turn cannot under-settle.
                charge = max(max_cost, api_credits.cost_from_usage(model, usage))
            # Settle, retrying on a transient ledger failure (settle is idempotent, so a retry cannot double-charge).
            settled = False
            for attempt in range(3):
                try:
                    api_credits.settle(res_id, charge, now)
                    settled = True
                    break
                except api_credits.LedgerError as exc:
                    logger.error("anthropic-api settle attempt %d/3 failed for %s: %s", attempt + 1, res_id, exc)
            if not settled:
                # The reservation ($%.4f) is retained, so pacing stays safe UNLESS the observed charge exceeded it
                # (estimate_max_cost is a conservative upper bound, so that is rare); the API's overage-off credit
                # cap is the authoritative hard backstop in every case.
                logger.error("anthropic-api settle gave up for %s: reservation $%.4f retained, charge was $%.4f; "
                             "the API overage-off cap remains the hard backstop", res_id, max_cost, charge)

    def _relay_local(self, session: "LocalSession", turn, body: dict[str, Any], *, created: bool, one_shot: bool,
                     wire: Wire) -> None:
        """One turn of a local-pinned conversation: relayed statelessly to its Ollama node (local_lane.serve), under
        the conversation's lock and the same stale-history rule as a CLI turn. The reply's fingerprint is appended to
        the session's history, so the next turn finds this conversation and keeps its model (_model_change). A node
        that fails a turn leaves the conversation as it was: the laptop may wake, and the same history retried
        continues it. A conversation whose opening turn failed is dropped (no reply exists to continue from)."""
        reg = self.server.registry
        route = session.route or {}
        if not session.lock.acquire(timeout=TURN_TIMEOUT_S):
            return self._error(BUSY_STATUS, "overloaded_error", "cortados: conversation busy; retry once the other "
                               "request on it has finished", wire)
        try:
            if not created and session.history != turn.history:
                return self._error(409, "invalid_request_error", "the conversation moved on while this request waited "
                                   "for it: another request with the same history was served first; send the current "
                                   "history", wire)
            if created and route.get("model"):
                self._record_run(provider=str(route.get("provider") or ""), model=str(route["model"]),
                                 label=str(route["model"]), tools=not one_shot)
            reply: list = []
            local = route["local"]
            # The same input-fit gate as the opener (routing._choose_local), on every turn: the conversation grows,
            # and Ollama would silently truncate an over-long one. A refusal keeps the history (nothing was sent).
            need = int(routing.request_fit(body)["input_tokens"])
            cap = int(local["model"]["context_length"])
            if need > cap:
                return self._error(413, "request_too_large",
                                   f"cortados: sub/{local_lane.pin_id(local['model'])}: the conversation (≈{need // 1000}k "
                                   f"tokens) now exceeds {local['model']['id']}'s configured context ({cap // 1000}k); "
                                   "compact it or start a new conversation", wire,
                                   reason="the conversation exceeds the local model's configured context")
            t0 = time.time()
            try:
                outcome = local_lane.serve(self, body, local["node"], local["model"], echo_model=turn.model,
                                           reply=reply)
            except Exception as exc:             # serve handles its own I/O; anything else must still clean up
                logger.exception("local lane: relay failed")
                outcome = "incomplete"
                try:
                    self._error(502, "api_error", f"cortados: local relay failed: {type(exc).__name__}", wire,
                                reason="the local relay failed")
                except Exception:
                    pass                         # headers may already be out; the client sees a broken response
            turn_ms = int((time.time() - t0) * 1000)
            session.last_used = time.time()
            if outcome == "completed":
                session.history.append(anthropic_wire.reply_fingerprint(reply))
                if route.get("route_id") and not session.route_logged and reg.executed:
                    try:
                        reg.executed(route)
                        session.route_logged = True
                    except Exception as exc:     # the answer was delivered; only its record failed
                        logger.warning("local lane: route record failed: %s", exc)
                    else:
                        if reg.log_outcome:      # size and duration, once per conversation, as on the CLI path
                            chars = sum(len(b.get("text", "")) for b in reply
                                        if isinstance(b, dict) and b.get("type") == "text")

                            def _log_outcome(route=route, chars=chars, ms=turn_ms):
                                try:
                                    reg.log_outcome(route, chars, ms)
                                except Exception:
                                    logger.exception("outcome log for route %s failed", route.get("route_id"))
                            threading.Thread(target=_log_outcome, name="cortados-outcome-log", daemon=True).start()
            elif created:
                reg.drop(session)
        finally:
            session.lock.release()

    def _server_tools_unavailable(self, turn, names: list[str], wire: Wire) -> None:
        """A complete, ordinary assistant reply whose text tells the model the server tools did not run. A reply,
        not a 4xx: the client hands the text to the model as the tool's output, so the model reads why and stops
        retrying, where an HTTP error may be retried or surface only as a generic API error."""
        text = anthropic_wire.SERVER_TOOL_UNAVAILABLE.format(names=", ".join(sorted(set(names))))
        logger.warning("request with server tool(s) %s answered as unavailable", sorted(set(names)))
        msg_id, blocks, usage = wire.new_message_id(), [{"type": "text", "text": text}], {}
        if not turn.stream:
            return self._json(200, wire.message_json(msg_id, turn.model, blocks, "end_turn", usage))
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(wire.stream_start(msg_id, turn.model))
            for chunk in wire.stream_body(msg_id, turn.model, blocks, "end_turn", usage):
                self.wfile.write(chunk)
            self.wfile.flush()
        except OSError as gone:
            logger.info("client gone while the server-tool refusal was written (%s)", gone)

    def _messages(self, body: dict[str, Any], wire: Wire) -> None:
        """One client request on `wire`: find or start its session, run one turn, answer in the wire's shape."""
        reg = self.server.registry
        dump = getenv("CORTADOS_PROVIDER_DUMP_DIR")     # debugging aid: every request body as received
        if dump:
            Path(dump).mkdir(parents=True, exist_ok=True)
            (Path(dump) / f"{time.time():.3f}.json").write_text(json.dumps(body, indent=1))
        try:
            # The api/ lane relays the body verbatim to the Anthropic Messages API, so only a Messages-wire body can be
            # pinned to it; on /v1/responses an api/ (or sub/) name falls through to parse_request, which refuses any
            # model but `cortados` with a 400 (Codex compares the echoed model to the one it asked for).
            api_model = self._api_pin(body) if wire is ANTHROPIC else None
            if api_model:
                # api/ requests are stateless, so each is a new conversation: honour x-cortados-exclude-model here
                # (the router, which applies it otherwise, is never called for this lane).
                raw, valid = _exclude_model_header(self.headers)
                if not valid:
                    return self._error(400, "invalid_request_error", _EXCLUDE_MODEL_BAD, wire)
                pinned_key = routing.model_key(routing.normalize_model(api_model))
                for item in (raw.split(",") if raw else []):   # each listed model, the same rules as choose
                    excluded = routing.normalize_model(item)
                    if excluded == "":                # `api`, `sub/`, `api/`: a prefix naming no model, as in choose
                        return self._error(400, "invalid_request_error",
                                           f"cortados: x-cortados-exclude-model: {item} names no model this endpoint "
                                           "serves; the review could not be kept off it", wire)
                    if excluded and routing.model_key(excluded) == pinned_key:
                        return self._error(400, "invalid_request_error",
                                           f"cortados: api/{api_model} is the excluded model (x-cortados-exclude-model); "
                                           "pin another model or use cortados", wire)
                # Pinned to the anthropic-api credit lane: proxy it directly, bypassing S1 and the session machinery.
                return self._serve_api_lane(body, api_model, wire)
            # The client's quota floor is read on EVERY request, new or continuation: a bad value is refused before any
            # routing or paid turn, and a continuation is rechecked under it below.
            floor, floor_valid = _min_left_header(self.headers)
            if not floor_valid:
                return self._error(400, "invalid_request_error", _MIN_LEFT_BAD, wire)
            turn = wire.parse_request(body)
            server_tools = anthropic_wire.server_tool_names(body) if wire is ANTHROPIC else []
            if server_tools and not turn.tools:
                # A request whose only tools are server tools (Claude Code's WebSearch makes exactly such a call) asks
                # for work no backend here can do. Answer it at once with text saying so, never spend a backend on it:
                # a routed model would answer without searching, and the client would show that as an empty success.
                return self._server_tools_unavailable(turn, server_tools, wire)
            if server_tools:
                # Mixed with client tools: parse_request dropped the server tools, the client tools are served.
                logger.warning("request declares server tool(s) %s that this endpoint cannot run; served without them",
                               sorted(set(server_tools)))
            # A tool-less request (a client's title or summary call) never joins a live session: it would hold that
            # session's lock while the session waits for a tool result. It gets its own throwaway CLI instead.
            one_shot = not turn.tools
            identity = self._conversation_identity()
            fail_key = None if one_shot else reg.failure_key(turn, wire_path=self.path.split("?")[0],
                                                              client=self._client(), identity=identity)
            unkeyed = (None if identity or not turn.history else
                       "the client sent no conversation identity: none of " + ", ".join(CONVERSATION_ID_HEADERS))
            scope = reg.conversation_scope(wire_path=self.path.split("?")[0], client=self._client(), identity=identity)
            session = None if one_shot else reg.find(turn, fail_key, scope, self.path.split("?")[0])
            if session is not None and getattr(session, "system", turn.system) != turn.system:
                # A running CLI keeps the system prompt it started with; a continuation that sends a different one
                # (changed `instructions`, a rewritten developer item) is served under the original, and says so here
                # rather than nowhere (P2 known limit: the conversation would otherwise have to be restarted).
                logger.warning("conversation %s: the system prompt changed mid-conversation; the running CLI keeps the "
                               "one it started with", session.id[:8])
            names = {t["name"] for t in turn.tools}
            if session is not None and not getattr(session, "tools_warned", True) and names != session.tool_names:
                # Same limit for the tool set: find() continues by history and the backend was started with the
                # opening request's tools (Codex declares them once, on thread/start), so a tool added or dropped
                # mid-conversation is never re-declared. Said once per conversation, names only; routing is unchanged.
                session.tools_warned = True
                logger.warning("conversation %s: the tool set changed mid-conversation (added %s, dropped %s); the "
                               "running backend keeps the tools it started with", session.id[:8],
                               sorted(names - session.tool_names), sorted(session.tool_names - names))
            if one_shot:
                # The transcript re-reads the whole history, so a shape error in an older user item surfaces here,
                # not in parse_request: it is a request-shape error all the same and answers 400.
                turn.content, turn.tool_results = wire.one_shot_content(body), {}
        except wire.WireError as exc:                   # request-derived text: the client sees it, the record does not
            return self._error(400, "invalid_request_error", str(exc), wire,
                               reason="the request does not parse on this wire")
        except FailedSession as exc:
            logger.info("request on a failed conversation refused: %s", exc)
            self._note_refusal(TERMINAL_STATUS, TERMINAL_ERROR, reason=_failed_reason(exc))
            if turn.stream:                             # an SSE error, as a streaming client gets any turn's failure
                try:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("cache-control", "no-cache")
                    self.send_header("connection", "close")
                    self.send_header("x-should-retry", "false")
                    self.end_headers()
                    self.close_connection = True
                    msg_id = wire.new_message_id()
                    self.wfile.write(wire.stream_start(msg_id, turn.model))
                    self.wfile.write(wire.stream_error(msg_id, turn.model, str(exc), terminal=True))
                    self.wfile.flush()
                except OSError as gone:
                    logger.info("client gone before the failed-conversation error was sent (%s)", gone)
                return
            # Terminal, not retryable: see TERMINAL_ERROR.
            return self._json(TERMINAL_STATUS, wire.error_json(TERMINAL_ERROR, str(exc)),
                              headers={"x-should-retry": "false"})
        except Replay as rp:
            # A retry that also changes the sub/ pin gets the same refusal as any continuation that does: the stored
            # answer came from the conversation's own model, which the request no longer asks for.
            changed = self._model_change(rp.session, body)
            if changed:
                return self._error(409, "invalid_request_error", changed, wire)
            _, _, msg_id, model, blocks, stop, usage = rp.last
            if turn.stream:
                try:
                    self.send_response(200)
                    self._route_header(rp.session)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("cache-control", "no-cache")
                    self.send_header("connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(wire.stream_start(msg_id, model))
                    for chunk in wire.stream_body(msg_id, model, blocks, stop, usage):
                        self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError as gone:
                    logger.info("client gone while the replayed reply was written (%s)", gone)
                return
            return self._json(200, wire.message_json(msg_id, model, blocks, stop, usage), session=rp.session)
        except LostSession as exc:
            return self._error(409, "invalid_request_error", str(exc), wire)
        changed = self._model_change(session, body) if session is not None else None
        if changed:
            return self._error(409, "invalid_request_error", changed, wire)
        created = session is None                       # this request opens the conversation (or is a one-shot)
        if session is None:
            route = None
            if floor is not None and not reg.router:
                # A fixed-model endpoint has no router to choose and reserve an account: a floor cannot be honoured, so
                # the opening turn fails closed, as its continuations do.
                return self._error(429, "rate_limit_error",
                                   "cortados: this endpoint runs a fixed model and cannot check x-cortados-min-left-pct",
                                   wire, reason="the client's quota floor cannot be checked here")
            if reg.router:
                # The route is chosen once, for the whole conversation: switching models mid-conversation would lose
                # the CLI session and its prompt cache.
                exclude, valid = _exclude_model_header(self.headers)   # read for a new conversation only
                if not valid:
                    return self._error(400, "invalid_request_error", _EXCLUDE_MODEL_BAD, wire)
                try:
                    route = reg.router(wire.routing_body(body), self._client(),
                                       **({"exclude_model": exclude} if exclude else {}),
                                       **({"min_left_pct": floor} if floor is not None else {}))
                except Exception as exc:                # scorer, tables or route log failed: a dependency, not the client
                    logger.exception("routing failed")
                    return self._error(503, "overloaded_error", f"cortados: routing failed: {type(exc).__name__}: {exc}", wire,
                                       reason="routing failed")
                if route.get("error"):
                    if route.get("client_error"):       # a bad pin or exclusion (unserved model, emptied pool): the client's to fix
                        # It names the client's own pin or exclusion, request-derived: the record keeps the fixed reason.
                        return self._error(400, "invalid_request_error", f"cortados: {route['error']}", wire,
                                           reason="the pin or exclusion names no model this endpoint can serve")
                    if route.get("quota_blocked"):      # the client's x-cortados-min-left-pct floor excluded every account
                        return self._error(429, "rate_limit_error", f"cortados: {route['error']}", wire,
                                           reason="no account is above the client's quota floor")
                    if route.get("fit_blocked"):        # too large for every account: retrying unchanged cannot succeed
                        return self._error(413, "request_too_large", f"cortados: {route['error']}", wire,
                                           reason="the request is larger than every eligible model's context")
                    return self._error(503, "overloaded_error", f"cortados: {route['error']}", wire,
                                       reason="no route is available")
                logger.info("route %s: %s/%s band %s%s", route["route_id"], route["provider"], route["model"],
                            route["band"], " (S1 degraded)" if route.get("degraded") else "")
            try:
                session = reg.new(turn, keep=not one_shot, route=route, scope=scope,
                                  wire_path=self.path.split("?")[0])
            except UnservedProvider as exc:
                return self._error(503, "overloaded_error", f"cortados: {exc}", wire)
            except BridgeError as exc:                  # the backend refused to start (version pin, isolation, login)
                logger.warning("backend start failed: %s", exc)
                return self._error(502, "api_error", str(exc), wire, reason=failure_diagnostic(exc))
        if isinstance(session, LocalSession):           # a local pin: relayed to its node, never a CLI turn
            return self._relay_local(session, turn, body, created=created, one_shot=one_shot, wire=wire)
        if not created:
            # A continuation's paid turn under the client's floor: the session keeps the tightest floor it was ever sent,
            # and its executing account is rechecked and reserved before this turn (an opening turn was reserved when
            # the route was chosen).
            kept = getattr(session, "min_left_pct", None)
            if kept is None:
                kept = ((getattr(session, "route", None) or {}).get("min_left_pct"))
            eff = max(v for v in (kept, floor) if v is not None) if (kept is not None or floor is not None) else None
            if eff is not None:
                session.min_left_pct = eff
                why = (reg.floor_turn(getattr(session, "route", None), eff) if reg.floor_turn is not None else
                       "this endpoint cannot check x-cortados-min-left-pct on a continuation")
                if why:
                    return self._error(429, "rate_limit_error", f"cortados: {why}", wire,
                                       reason="the conversation's account is below the client's quota floor")
        msg_id = wire.new_message_id()
        # The model this response names: what the client asked for, unless it asked to see the real route (the
        # routed-subagents mod does; the main session does not and keeps seeing `cortados`).
        revealed = self._reveal(session) if self._wants_reveal() else None
        model = revealed or turn.model
        routed = getattr(session, "route", None) if created else None
        if routed and routed.get("model"):
            # Routed by this request (S1 or a sub/ pin; a continuation was routed when it opened, and is already on
            # record). The label is the reveal this response carries, or the model when none was asked for: the lane %
            # is not read just for the record.
            self._record_run(provider=str(routed.get("provider") or ""), model=str(routed["model"]),
                             label=revealed or str(routed["model"]), tools=not one_shot)
        if turn.stream:
            try:
                self.send_response(200)
                self._route_header(session)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(wire.stream_start(msg_id, model))
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
                # Request-local: another request owns the session and is still running its turn. Closing, dropping
                # or tombstoning here would kill that live backend (or overwrite its real failure diagnostic), so
                # this request only answers a retryable busy error (BUSY_STATUS) and the conversation is untouched.
                outcome["busy"] = (f"cortados: conversation busy: another request on this conversation has held it "
                                   f"for over {TURN_TIMEOUT_S:.0f}s; retry once that request has finished")
                return
            try:
                if not created and reg.get(session.id) is not session:
                    # Registry.find ran before the lock, and the conversation was closed while this request waited:
                    # its backend failed under the request that held the lock (Registry.fail), or it was reaped. The
                    # backend is gone, so nothing is sent to it; the request is told what a fresh lookup would say.
                    outcome["stale"] = reg.failure(fail_key, turn.history, scope) or LostSession(
                        "the conversation was closed while this request waited for it; start a new conversation")
                    outcome["stale_class"] = "closed_while_waiting"
                    return
                if not created and session.history != turn.history:
                    # Registry.find ran before the lock: a second continuation with the same history (a client
                    # retry, two overlapping requests) found the session too, and the one that lost the lock now
                    # sees a conversation that has moved on. Delivering its results would fail inside the backend
                    # and drop a healthy conversation; it is a stale request, answered 409, and the session is kept.
                    outcome["stale"] = LostSession("the conversation moved on while this request waited for it: another "
                                                   "request with the same history was served first; send the current history")
                    outcome["stale_class"] = "moved_on_while_waiting"
                    outcome["stale_lens"] = (len(turn.history), len(session.history))
                    return
                if turn.tool_results:
                    session.deliver(turn.tool_results, timeout=deadline - time.time())
                else:
                    session.send_user(turn.content, timeout=deadline - time.time())
                _turn_t0 = time.time()
                outcome["turn"] = session.run_turn(deadline - time.time())
                _turn_ms = int((time.time() - _turn_t0) * 1000)
                _remember_reply(session, turn, msg_id, model, outcome["turn"])
                try:
                    _ledger_turn(session, outcome["turn"][2])
                except Exception as exc:
                    # The ledger only feeds unit-cost learning: it must never break a turn. Without this guard the reply
                    # would go out while history fell behind the client, killing the conversation on its next continuation.
                    _log_ledger_hook_failure(exc)
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
                if not one_shot:
                    # Still under the session lock: a request waiting for it finds the conversation gone (above), never
                    # a backend that just failed. The key is this request's own, computed before the turn ran.
                    reg.fail(session, exc, fail_key, unkeyed_reason=unkeyed)
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
                stale = outcome["stale"]
                logger.warning("conversation %s: stale continuation refused: %s", session.id[:8], stale)
                failed = isinstance(stale, FailedSession)     # terminal, as when find meets the tombstone
                # Both kinds are this endpoint's own text (a fixed LostSession message, or the tombstone's allowlisted
                # diagnostic), recorded once here for the SSE and the JSON answer alike.
                if failed:
                    self._note_refusal(TERMINAL_STATUS, TERMINAL_ERROR, reason=_failed_reason(stale))
                else:
                    # The same classified line as find's 409s (lengths only), so every lost-conversation 409 has one.
                    req_len, sess_len = outcome.get("stale_lens") or (len(turn.history), None)
                    logger.warning("409 lost conversation: class=%s conversation=%s request_len=%d%s",
                                   outcome.get("stale_class", "stale"), session.id[:8], req_len,
                                   "" if sess_len is None else f" candidate_len={sess_len}")
                    self._note_refusal(409, "invalid_request_error", reason="stale continuation")
                if client_gone:
                    return
                if turn.stream:
                    self.wfile.write(wire.stream_error(msg_id, model, str(stale), terminal=failed))
                    return
                if failed:
                    return self._json(TERMINAL_STATUS, wire.error_json(TERMINAL_ERROR, str(stale)),
                                      headers={"x-should-retry": "false"})
                return self._json(409, wire.error_json("invalid_request_error", str(stale)))
            if "busy" in outcome:
                logger.warning("conversation %s: %s", session.id[:8], outcome["busy"])
                self._note_refusal(BUSY_STATUS, "overloaded_error")
                if client_gone:
                    return
                if turn.stream:                         # api_error / server_error: both wires retry it
                    self.wfile.write(wire.stream_error(msg_id, model, outcome["busy"]))
                    return
                return self._json(BUSY_STATUS, wire.error_json("overloaded_error", outcome["busy"]))
            if "error" in outcome:
                # A failed session turn was already closed and tombstoned by the worker, under the session lock.
                logger.warning("conversation %s failed: %s", session.id[:8], outcome["error"])
                # Recorded for the SSE answer as well as the JSON one (a streaming client gets HTTP 200 + an error
                # event, and used to leave no record): the allowlisted category only.
                self._note_refusal(502, "api_error", reason=failure_diagnostic(outcome["error"]))
                if client_gone:
                    return
                if turn.stream:
                    self.wfile.write(wire.stream_error(msg_id, model, str(outcome["error"])))
                    return
                return self._json(502, wire.error_json("api_error", str(outcome["error"])))
            blocks, stop, usage = outcome["turn"]
            logger.info("conversation %s: %s, %d blocks%s", session.id[:8], stop, len(blocks),
                        " (client gone; reply not sent)" if client_gone else "")
            if not client_gone:
                try:
                    if turn.stream:
                        for chunk in wire.stream_body(msg_id, model, blocks, stop, usage):
                            self.wfile.write(chunk)
                        self.wfile.flush()
                    else:
                        self._json(200, wire.message_json(msg_id, model, blocks, stop, usage), session=session)
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

    def _conversation_identity(self) -> str | None:
        """The conversation identity the client sent (CONVERSATION_ID_HEADERS, every one present, as name=value
        lines), or None. Only ever hashed into Registry.failure_key, never logged."""
        parts = []
        for name in CONVERSATION_ID_HEADERS:
            v = (self.headers.get(name) or "").strip()
            if v:
                parts.append(f"{name}={v}")
        return "\n".join(parts) or None

    def _run_id(self) -> tuple[str | None, bool]:
        """The x-cortados-run-id header: (id or None when absent/empty, valid)."""
        v = (self.headers.get(RUN_ID_HEADER) or "").strip()
        return (v or None), (not v or bool(_RUN_ID_RE.fullmatch(v)))

    def _record_run(self, *, provider: str, model: str, label: str | None, tools: bool) -> None:
        """Record a routed request under its run id, if it carries one. Bookkeeping only: never fails a turn."""
        try:
            run_id, valid = self._run_id()
            if run_id and valid:
                self.server.registry.runs.record(run_id, provider=provider, model=model, label=label or model,
                                                 tools=tools)
        except Exception:
            logger.debug("run record failed", exc_info=True)

    def _wants_reveal(self) -> bool:
        try:                                            # cosmetic: never raises into a turn (or the api lane's money path)
            return (self.headers.get(REVEAL_HEADER) or "").strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    @staticmethod
    def _model_change(session, body: dict[str, Any]) -> str | None:
        """Why a continuation must be refused for naming a different model than its conversation runs, or None.
        The route is fixed when the conversation opens (the running CLI cannot change model without losing its session
        and prompt cache), so a `sub/` pin on a continuation must name the model the conversation was pinned to; a
        different one, or a pin on an S1-routed conversation, is refused rather than silently served on the old route
        while the response echoes the new name. A continuation naming `cortados` (or any non-pin name) continues on
        whatever the conversation runs: on a pinned conversation the pinned model keeps running."""
        pinned = routing.sub_pin(body)
        if not pinned:
            return None
        r = getattr(session, "route", None) or {}
        local = r.get("local")
        if local:                       # a local pin answers to its alias or its tag, case-insensitively
            names = {local["model"]["id"].casefold(), local_lane.pin_id(local["model"]).casefold()}
            if r.get("pinned") and pinned.strip().casefold() in names:
                return None
            shown = local_lane.pin_id(local["model"])
        else:
            if r.get("pinned") and r.get("model") == pinned:
                return None
            shown = r.get("model")
        now = f"pinned to sub/{shown}" if r.get("pinned") else f"routed by S1 to {r.get('model') or 'its model'}"
        return (f"cortados: the model is fixed per conversation: this one is {now}, and this request asks for "
                f"sub/{pinned}; start a new conversation to change models")

    def _reveal(self, session) -> str | None:   # api/-pinned requests reveal in _serve_api_lane (api_reveal_suffix)
        try:
            return reveal_model(getattr(session, "route", None))
        except Exception:                               # reveal_model already never raises; belt and braces
            return None

    def _route_header(self, session) -> None:
        r = getattr(session, "route", None)
        if r:
            self.send_header("x-cortados-route", f"{r['provider']}/{r['model']} band={r['band']} id={r['route_id']}")


class ProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, registry: Registry):
        super().__init__(addr, Handler)
        self.registry = registry

    def usage_view(self, registry: Registry) -> dict[str, Any] | bytes:
        """GET /v1/cortados/usage (provider/usage.py): cache-only reads plus one short S1 health call. Tests replace it."""
        from cortados_router import paths, scarcity
        from cortados_router.provider import usage
        try:
            from cortados_router.s1_endpoint import resolve_url
            s1_url = resolve_url()
        except Exception as exc:                        # reported by the s1 section as unavailable, with the reason
            logger.warning("usage: cannot resolve the S1 URL: %s", exc)
            s1_url = None
        state = paths.state_dir()
        # strict cache-only read, not scarcity.current(refresh=False): that hides a missing/corrupt cache as no lanes
        return usage.collect(state_dir=state, scarcity_reader=lambda: usage.read_scarcity_cache(state / "scarcity.json"),
                             refusals=registry.runs.all_refusals(), runs_since_start=registry.runs.model_counts(),
                             s1_url=s1_url, freshness=scarcity.reading_freshness, claude_lanes=scarcity.claude_lanes(),
                             encoded=True)


def serve(host: str, port: int, token: str, *, claude_bin: str, codex_bin: str, codex_home: str | None,
          fixed_model: str | None) -> None:
    router = executed = log_turn_outcome = None
    if not fixed_model:
        from cortados_router import core                  # loads the tables and the live scarcity reader
        from cortados_router.provider import routing

        def router(body, client, exclude_model=None, min_left_pct=None):
            return routing.choose(core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin=claude_bin,
                                  codex_bin=codex_bin, exclude_model=exclude_model, min_left_pct=min_left_pct)

        def executed(route):
            core.log_execution(route["route_id"], route["provider"], route["model"])

        def log_turn_outcome(route, response_chars, turn_ms):
            core.log_outcome(route["route_id"], response_chars, turn_ms)

    registry = Registry(gateway_url=f"http://{host}:{port}", token=token, claude_bin=claude_bin, codex_bin=codex_bin,
                        codex_home=codex_home, model=fixed_model, router=router, executed=executed,
                        log_outcome=log_turn_outcome)
    if not fixed_model:
        registry.floor_turn = lambda route, floor: routing.floor_turn(core, route, floor)
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
