"""The CortadOS endpoint: an Anthropic Messages server whose one model, `cortados`, is served by bridged headless CLIs.

Owner of the conversation registry: which bridge session a request belongs to. A request with no assistant messages
starts a new session; any other goes to the one live session whose replies are exactly its assistant history. A
request whose client compacted or edited that history is served on a REBUILT backend (Registry.rebuild, issue #43:
a fresh session on the same route, seeded with the client's current history as a transcript; identical concurrent
rebuilds share one). When no rebuild is possible it fails with a terminal 400 (x-should-retry: false), because the
retry carries the same history: no conversation identity, no live conversation of the same client wrote its last
reply (nor one the idle reaper closed within the tool-wait limit: ReapedConversation, #132 item 6), more than one did,
the conversation reached REBUILD_MAX, its replayed history does not fit its model, it asks for another model, or it
was closed because its backend failed (a bounded, time-limited tombstone keyed by the conversation itself; both wires
share the lookup). A state a moment can change (a rebuild in flight, a turn running)
is a retryable 529; docs/PROVIDER-PLAN.md has the table. An exact match is looked up only among the request's own
scope (wire, client, conversation identity): two clients never share one, and a request without an identity is
continued only on proof that it is the session's caller (a tool_use id of that session in its history).
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
import html as html_escape
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict, deque
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):                           # run as a file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cortados_router import http_body
from cortados_router.compat import getenv
from cortados_router.safe_text import install_log_sanitizer
from cortados_router.compaction import prune as compaction_prune, store as compaction_store
from cortados_router.provider import (anthropic_wire, clients, dashboard_auth, effect_boundary, live, local_lane,
                                      local_switch, office, responses_wire, routing, search_lane, sensitive_notice,
                                      work_split)
from cortados_router.provider.bridge import BridgeError, BridgeSession, ToolWaitExpired, claude_env
from cortados_router.provider.codex_bridge import CodexSession
from cortados_router.provider.timeouts import IDLE_S, TOOL_WAIT_S, TURN_TIMEOUT_S, effective_tool_wait

DEFAULT_PORT = 8432                                     # next to S1's 8431
# A turn covers the model's thinking until it finishes or asks for a tool; long agentic turns run minutes.
# Resolved centrally through compat.getenv; the shim gets the same budgets explicitly.
# Tool-wait sessions use at least TOOL_WAIT_S (default 3 h), not the ordinary idle TTL.
# Bound held CLI processes and replay memory even if many clients abandon long-running tools. Refuse new
# conversations at capacity rather than evicting a client still inside its promised tool-result window.
MAX_SESSIONS = int(getenv("CORTADOS_PROVIDER_MAX_SESSIONS") or 128)
if MAX_SESSIONS < 1:
    raise ValueError("CORTADOS_PROVIDER_MAX_SESSIONS must be positive")
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
# Rebuilds per conversation (Registry.rebuild): each one replays the client's whole history, uncached, into a fresh
# backend, so a client whose history diverges on every request (a client bug, two clients fighting over one identity)
# must not pay that on every turn. 5 covers several client compactions in one long run (the 409 bursts of #43 were in
# 70-80 minute builder runs) and is counted along the conversation's lineage: every session rebuilt from it, by
# compaction or by a branch, shares ONE budget (RebuildBudget), so branching cannot reset it. Reaching it is terminal
# (TERMINAL_STATUS, never the retried 409): a retry carries the same rewritten history and would be refused again.
# 0 switches rebuilding off (the pre-#43 409 for every rewritten history).
REBUILD_MAX = 5
# How long a rebuild waits for another rebuild of the same conversation point (same scope, wire and anchor reply) to
# finish starting its backend before it answers a retryable busy (Registry.rebuild, review P1-a). A backend start is a
# few seconds; a minute covers a slow Codex app-server start without holding a handler thread for a whole turn.
REBUILD_WAIT_S = 60.0
# Recently closed conversations kept for the 409 diagnostic (Registry.closed): enough to cover the live conversations
# of a busy endpoint, and an hour, past which a reap no longer explains a 409.
CLOSED_RECORD_MAX = 128
CLOSED_RECORD_TTL_S = 3600.0
# Conversations the idle reaper closed, kept so their client's return can be rebuilt (Registry.recoverable, #132 item
# 6): a client that left a conversation idle past IDLE_S (a child waiting on a long background job) and comes back gets
# it rebuilt through the compaction-rebuild path instead of a terminal no_writer. Only idle reaping leaves one (a
# failed, exited, dropped or rebuilt conversation never does); each holds metadata only (ReapedConversation: scope
# digest, wire, client name, route, the LAST reply's fingerprint, the lineage's rebuild budget), never request text.
# Kept for the tool-wait limit (recover_ttl_s(): effective_tool_wait(IDLE_S, TOOL_WAIT_S), 3 h by default), the
# longest a client is ever promised to be waited for; bounded by count, the oldest goes first.
RECOVERABLE_MAX = 128
# Cap pressure (#132 item 6, review round 1): when opening one more conversation would exceed MAX_SESSIONS, the
# registry retires the longest-idle conversations that could be recovered (ReapedConversation.eligible, with a rebuild
# left in their lineage's budget: Registry._recovery_left_locked, review round 2), have no turn
# running and do not wait on a tool result, into the same recovery records the idle reaper writes, until there is
# room (Registry._retire_for_room_locked). One idle less than this is never retired: a client between two turns of an
# active conversation is not made to pay a rebuild.
PRESSURE_MIN_IDLE_S = 120.0
# The longest a conversation's x-cortados-run-id earns it the tool-wait idle limit, counted from its first request
# carrying that id: a child task's own maximum run time (child_tasks.CHILD_MAX_S_DEFAULT, CORTADOS_CHILD_MAX_S). A run
# this host's log never records ending (a task on another host, or any syntactically valid header) cannot hold a
# conversation past it; after it the conversation is reaped at the normal IDLE_S.
try:
    CHILD_RUN_MAX_S = float(int(getenv("CORTADOS_CHILD_MAX_S") or 3 * 3600))
    if CHILD_RUN_MAX_S <= 0:
        raise ValueError
except ValueError:
    CHILD_RUN_MAX_S = 3 * 3600.0


def recover_ttl_s() -> float:
    """How long a reaped conversation stays recoverable: read at call time (tests rebind IDLE_S / TOOL_WAIT_S)."""
    return effective_tool_wait(IDLE_S, TOOL_WAIT_S)
# Ended run conversations kept for the live view's tree (Registry.ended): a few hundred child sub-sessions is hours of
# the owner's fan-out; the view shows each for live.SUBSESSION_KEEP_S after it closed.
ENDED_CONVERSATIONS_MAX = 256
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


# The token page holds no data or secret, so it needs no token; in session mode the page carries the session's CSRF
# token. These headers keep it from being stored by the browser or any cache (private, no-store; Vary names the
# credentials it depends on), framed, sniffed as another type, or sending its Referer. Its Content-Security-Policy is
# _dashboard_csp(): it pins the one inline script.
DASHBOARD_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie, Authorization",
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


# The page's auth mode and CSRF token travel in two <meta> tags (outside the script, so the CSP's script hash is the same
# for every mode). "token": no password is set, the page asks for the provider token as before. "session": a dashboard
# session (provider/dashboard_auth.py) is open, the page fetches with its cookie and sends the CSRF token on a POST.
_AUTH_META = b'<meta id="cortados-auth" name="cortados-auth" content="token">'
_CSRF_META = b'<meta id="cortados-csrf" name="cortados-csrf" content="">'


def _dashboard_page(session: "dashboard_auth.Session | None") -> bytes:
    """The dashboard page for this request: token mode, or session mode carrying that session's CSRF token."""
    page = _dashboard_html()
    if session is None:
        return page
    if page.count(_AUTH_META) != 1 or page.count(_CSRF_META) != 1:
        raise ValueError("dashboard page: the auth meta tags are missing")
    csrf = html_escape.escape(session.csrf, quote=True).encode()   # urlsafe base64 already; escaped all the same
    return (page.replace(_AUTH_META, _AUTH_META.replace(b'content="token"', b'content="session"'))
            .replace(_CSRF_META, _CSRF_META.replace(b'content=""', b'content="' + csrf + b'"')))


# The login page (a password is set and the request has no valid session): a plain form and no script at all, so its
# policy allows no script; form-action 'self' is what lets it post to /dashboard/login.
LOGIN_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
# The login page's referrer policy is same-origin, not the dashboard's no-referrer: under no-referrer a browser sends
# `Origin: null` on a form POST, and the login's same-origin check (dashboard_auth.same_origin) would refuse the owner.
# same-origin still sends nothing to another site. (The page's own logout fetch sets referrerPolicy per request.)
LOGIN_HEADERS = {"Referrer-Policy": "same-origin"}
LOGIN_PATH, LOGOUT_PATH = "/dashboard/login", "/dashboard/logout"
# A login body is one short form field; anything bigger is refused before it is read.
LOGIN_MAX_BYTES = 4096
# The whole login body must arrive within this many seconds of the headers (http_body.read_within). The socket timeout
# (SOCKET_TIMEOUT_S) bounds inactivity only, so without it an unauthenticated client trickling one byte per 100 s holds
# a handler thread for days. 4 KiB in 5 s is ~6.5 kbit/s: a browser posts it in one write.
LOGIN_BODY_DEADLINE_S = 5.0
# Login requests handled at once, from headers to the reply; one more is refused with 503 before anything is read. The
# route is unauthenticated, so this caps the threads (and scrypt/PBKDF2 queue) strangers can occupy. Logins are
# serialised in dashboard_auth anyway, so 4 is plenty for the one owner.
LOGIN_MAX_CONCURRENT = 4
# Every authenticated view (owner token, scoped token or dashboard session): never stored by the browser or any cache,
# and a response depends on the credential it was asked with.
VIEW_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"}
_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>CortadOS · log in</title>
<style>body{{font:15px system-ui,sans-serif;max-width:22rem;margin:12vh auto;padding:0 1rem}}
input,button{{font:inherit;width:100%;box-sizing:border-box;padding:.5rem;margin:.4rem 0}}.msg{{color:#c0392b;min-height:1.2em}}</style>
</head><body><h1>CortadOS</h1>
<form method="post" action="/dashboard/login">
<label for="password">Dashboard password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Log in</button></form>
<p class="msg">{message}</p>
</body></html>
"""


def _login_page(message: str = "") -> bytes:
    """The login page; `message` is one of this module's fixed strings, escaped all the same."""
    return _LOGIN_PAGE.format(message=html_escape.escape(message, quote=True)).encode()
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
    wire needs neither. `terminal=True` marks an error no retry can fix with each wire's non-retried kind.
    `history_messages(body)` is the request's history before its new turn, Anthropic-shaped, for a rebuilt backend's
    seed (anthropic_wire.rebuild_transcript)."""

    def __init__(self, *, model_id: str, error_type: type, parse_request: Callable, one_shot_content: Callable,
                 routing_body: Callable, new_message_id: Callable, stream_start: Callable, stream_ping: Callable,
                 stream_body: Callable, stream_error: Callable, message_json: Callable, error_json: Callable,
                 history_messages: Callable):
        self.MODEL_ID, self.WireError = model_id, error_type
        self.history_messages = history_messages        # body -> Anthropic-shaped messages before the new turn (rebuild)
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
    message_json=anthropic_wire.message_json, error_json=anthropic_wire.error_json,
    history_messages=lambda body: anthropic_wire.history_messages(body["messages"]))
# The Responses wire scores what its user and assistant messages say, in Anthropic shape, with the rest of the body
# kept (routing.choose sizes the request from the whole body and reads `tools` to tell a one-shot from a session).
# Only the function tools are handed over: parse_request drops the hosted ones (web_search), so a request whose
# tools are all hosted is served as a one-shot, and routing's standalone test must agree with that.
RESPONSES = Wire(
    model_id=responses_wire.MODEL_ID, error_type=responses_wire.WireError, parse_request=responses_wire.parse_request,
    one_shot_content=responses_wire.one_shot_content,
    routing_body=lambda body: dict(body, messages=responses_wire.score_messages(body),
                                   tools=responses_wire.function_tools(body),
                                   **{routing.NO_LOCAL_KEY: True}),   # the local relay speaks Messages only
    new_message_id=responses_wire.new_message_id, stream_start=responses_wire.sse_start, stream_ping=responses_wire.sse_ping,
    stream_body=lambda msg_id, model, blocks, stop, usage: responses_wire.sse_body(blocks, stop, usage, msg_id=msg_id, model=model),
    stream_error=lambda msg_id, model, message, terminal=False: responses_wire.sse_error(message, msg_id=msg_id, model=model,
                                                                                         terminal=terminal),
    message_json=responses_wire.message_json, error_json=responses_wire.error_json,
    history_messages=responses_wire.history_messages)
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


_LOCAL_BAD = "cortados: x-cortados-local must be `sensitive` or `prefer`"


def _local_header(headers) -> tuple[str | None, bool]:
    """(mode, valid) of the x-cortados-local header (routing.LOCAL_MODES, case-insensitive); (None, True) when absent
    or empty. Read for a new conversation only: the route is chosen once per conversation."""
    raw = (headers.get(routing.LOCAL_HEADER) or "").strip().lower()
    if not raw:
        return None, True
    return (raw, True) if raw in routing.LOCAL_MODES else (None, False)


# Request header naming the client's run (the routed-subagents mod runs each subagent as a headless `claude -p`, whose
# JSON output names only `cortados`): every routed request of the run is recorded under it, and
# GET /v1/cortados/runs/<id> tells the client which model really ran (to label it, and to keep a later review off it).
RUN_ID_HEADER = "x-cortados-run-id"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_RUN_ID_BAD = "cortados: x-cortados-run-id is not a run id"
RUNS_PATH = "/v1/cortados/runs/"
# The Office view (provider/office.py): GET is open to the owner token and to a client token with the office:read scope
# (provider/clients.py); the heartbeat POST, which writes, needs the owner token (the mod already holds it to route).
OFFICE_HEARTBEAT_PATH = "/v1/cortados/office/heartbeat"
# The client-side boundary hook's question (docs/BOUNDARY-HOOK.md, effect_boundary.check_answer): owner token only.
BOUNDARY_CHECK_PATH = "/v1/cortados/boundary/check"
_TOOL_USE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SCOPE_DENIED = "cortados: this token's scopes do not cover this endpoint"

# What a refusal record (RunRecords.refuse) and the refusal log line hold: the status, the error type, an endpoint id
# from _ENDPOINT_IDS, and a fixed reason string of this endpoint's own (a call site's, the allowlisted
# failure_diagnostic of a backend failure, else the default for the status below). Never any backend text, request
# text or raw path: backend output can echo credentials (a local gate's token, a key in an upstream error body) and a
# path can carry a client's secret (/v1/cortados/runs/<anything>). The client's response still carries the full
# message, and backend failures keep their existing provider log lines.
_REFUSAL_REASONS = {
    400: "the request was refused as invalid",
    401: "bad or missing token",
    403: "the client token's scopes do not cover this endpoint",
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
    if p == clients.OFFICE_PATH:
        return "office"
    if p == OFFICE_HEARTBEAT_PATH:
        return "office_heartbeat"
    if p == live.LIVE_PATH:
        return "live"
    if p == live.HEARTBEAT_PATH:
        return "live_heartbeat"
    if p == local_switch.VIEW_PATH:
        return "local"
    if p == local_switch.SWITCH_PATH:
        return "local_switch"
    if p == BOUNDARY_CHECK_PATH:
        return "boundary_check"
    if p.startswith("/v1/models"):
        return "models"
    if p in ("/health", "/resources", "/bridge/call"):
        return p[1:].replace("/", "_")
    if p in ("/", "/dashboard"):
        return "dashboard"
    if p == LOGIN_PATH:
        return "dashboard_login"
    if p == LOGOUT_PATH:
        return "dashboard_logout"
    return "other"


# Headers a client sends that name its conversation, for the failed-session tombstone (Registry.failure_key). Each was
# confirmed on the real client's own requests to a capture endpoint (2026-11-02):
#   x-cortados-run-id          this endpoint's own run header; the headless tools set it per run.
#   x-claude-code-session-id   Claude Code 2.1.316 sends X-Claude-Code-Session-Id on every /v1/messages request (the
#                              same id is the session_id inside metadata.user_id). The main loop and its subagents
#                              share it; their histories differ, so identity + digest still names one conversation.
#   session-id                 Codex 0.166.0 sends its thread id as `session-id` (and `thread-id`, x-client-request-id,
#                              prompt_cache_key) on /v1/responses; the 0.163.0 binary carries the same header names.
# Every one present is part of the identity; with none, the request has no identity and gets no tombstone. The identity
# also scopes every lookup (Registry.find: exact match, replay, rebuild), and a continuation without one is continued
# only on a tool_use-id proof of caller, never rebuilt or replayed (#43 second review, P1-b). Every real client sends
# one: Claude Code its session id, CortadOS child tasks and routed subagents also x-cortados-run-id, Codex session-id.
# x-cortados-session (the Office's salted main-session tag) is not one: it only ever comes beside the run id or Claude
# Code's own id, and every subagent of a session shares it.
# These headers are client-asserted, not authenticated: a client holding the endpoint token could send another's. They
# keep honest clients' conversations apart; they are no defence against a token holder.
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
        # Backend tokens per run id (work_split.usage_tokens summed over every turn of the run's conversations), for the
        # live view's orchestrator tree: a child task's total survives its conversations being reaped. In memory only.
        self._tokens: "OrderedDict[str, dict[str, int]]" = OrderedDict()
        # Refused requests per run id: a request refused before routing (413 too large, a bad pin) has no route entry,
        # and without this a headless client could only learn "no routed request recorded". Recorded only for an
        # authenticated request (Handler._note_refusal), so a caller without the token cannot add or evict entries.
        self._refusals: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        # The x-cortados-origin the provider saw on each run's token-bearing requests (office.note_origin: the first,
        # or office.ORIGIN_CONFLICT once two differ). The live view joins a heartbeat-reported task's run tokens only
        # when this equals the task's bound origin (review round 4). Bounded with the tokens.
        self._origins: "OrderedDict[str, str]" = OrderedDict()

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

    def add_tokens(self, run_id: str, tokens: dict[str, int], origin: str | None = None) -> None:
        """Add one backend turn's tokens (work_split.usage_tokens) to the run's total; bounded like the run records.
        `origin`: the request's x-cortados-origin (OfficeBoard.read_origin), noted for the run (run_origins)."""
        with self._lock:
            t = self._tokens.pop(run_id, None) or {}
            work_split.add_tokens(t, tokens)
            self._tokens[run_id] = t
            o = office.note_origin(self._origins.pop(run_id, None), origin)
            if o is not None:
                self._origins[run_id] = o
            while len(self._tokens) > self.MAX_RUNS:
                gone, _ = self._tokens.popitem(last=False)
                self._origins.pop(gone, None)

    def run_origins(self) -> dict[str, str]:
        """run id -> the origin the provider observed on its requests (copies), for the live view's joins."""
        with self._lock:
            return dict(self._origins)

    def tokens_by_run(self) -> dict[str, dict[str, int]]:
        """run id -> its token total since this endpoint started (copies), for the live view."""
        with self._lock:
            return {k: dict(v) for k, v in self._tokens.items()}

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


def _floor_search(core, cand: dict, min_left_pct: float, rid: str | None = None) -> str | None:
    """One search under the client's floor: a whole run, charged once (no conversation id), on its executing account.
    `rid` stamps the reservation so _floor_release can take it back if the CLI never starts (#79)."""
    from cortados_router.provider import floor
    return floor.reserve(core, cand, search_lane.SEARCH_MODEL, min_left_pct, rid=rid)


def _floor_release(core, rid: str) -> bool:
    """Refund the search reservation `rid` (floor.release): only for a search whose CLI failed to start."""
    from cortados_router.provider import floor
    return floor.release(core, rid)


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


def _routed_cloud(route: Any) -> bool:
    """Whether `route` is an S1 route to a cloud model (#124): a dict naming its provider and model, neither a pin nor a
    local lane. Only such a conversation has a detector and a routes row, so only it can carry a sensitive-cloud notice;
    an unrouted session (a fixed-model endpoint: route None) never does."""
    return (isinstance(route, dict) and bool(route.get("provider")) and bool(route.get("model"))
            and not route.get("local") and not route.get("pinned"))


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
    """` · <pct>% left` of the anthropic-api credit (100 % = the stop line), for an API-lane request that asked to
    reveal. The upstream response already names the real model; this only adds the %. None when it cannot be read."""
    try:
        from cortados_router import api_credits
        used = float(api_credits.window_numbers(time.time())[0])
        return f" · {round(100 - max(0.0, min(100.0, used)))}% left"
    except Exception:
        logger.debug("reveal: anthropic-api meter unavailable", exc_info=True)
        return None


def _claude_session(reg: "Registry", *, account: str | None = None, config_dir: str | None = None, **kw) -> BridgeSession:
    # No owner token: the session mints its own bridge-only credential for its MCP shim (BridgeSession.bridge_token).
    return BridgeSession(gateway_url=reg.gateway_url, claude_bin=reg.claude_bin,
                         config_dir=config_dir, **kw)


def _codex_session(reg: "Registry", *, account: str | None = None, config_dir: str | None = None,
                   codex_home: str | None = None, **kw) -> CodexSession:
    # Code mode must not yield to the model before the request's turn budget is spent (codex_isolation.EXEC_YIELD_KEY).
    # `codex_home`: the canonical home a floor-checked route was admitted on (routing._bind_codex); else the provider's.
    return CodexSession(codex_bin=reg.codex_bin, codex_home=codex_home or reg.codex_home,
                        exec_yield_ms=int(TURN_TIMEOUT_S * 1000), **kw)   # Codex multi-account is Phase B


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


class ApiSession(LocalSession):
    """A conversation routing moved to the anthropic-api credit lane (routing.prefer_api_credit): relayed statelessly
    by Handler._relay_api, kept only as its binding to the model and its reply fingerprints, as a LocalSession is."""


def _configured_live_hosts() -> set[str]:
    """The hosts the owner configured (CORTADOS_LIVE_HOSTS or the defaults, and exec-hosts.json's names): the heartbeat
    board never evicts them. Asked only when the board is full; an unreadable exec-hosts file adds nothing."""
    from cortados_router import exec_hosts
    names = set(live.known_hosts())
    try:
        if os.path.exists(exec_hosts.config_path()):
            names |= set(exec_hosts.load().hosts)
    except Exception as exc:
        logger.warning("live: exec-hosts unreadable for the heartbeat board: %s", type(exc).__name__)
    return names


def _ended_runs_from_log() -> set:
    """The run ids this host's child-task log (office.CHILD_EVENT_LOG) records as finished or cancelled within the
    office's window. A run not in it counts as running: a task on another host, or one whose end was not logged, keeps
    the tool-wait idle limit, which a child's own maximum run time (3 h by default) bounds anyway."""
    from cortados_router import paths
    now = time.time()
    events = office.read_child_events(paths.state_dir() / office.CHILD_EVENT_LOG, now)
    return {tid for tid, c in office._children(events, now).items() if c.get("final")}


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
        self.search_bin = claude_bin                # the search lane's CLI (search_lane.py); tests swap in a fake
        self.router, self.executed, self.log_outcome = router, executed, log_outcome
        # floor_turn(route, min_left_pct) -> refusal or None: rechecks and reserves a continuation's executing account
        # under the client's x-cortados-min-left-pct (routing.floor_turn). None: a floor on a continuation fails closed.
        self.floor_turn: Callable[[dict | None, float], str | None] | None = None
        # floor_search(candidate, min_left_pct) -> refusal or None: checks and reserves the search lane's executing
        # account under the floor before a search launches (floor.reserve). None: a floor on a search fails closed.
        # The reservation is stamped with the rid passed in, so floor_release(rid) can refund it (#79).
        self.floor_search: Callable[[dict, float, str], str | None] | None = None
        # floor_release(rid) -> bool: refunds a floor_search reservation whose search CLI failed to start (a reservation
        # only counts once the run started). None: nothing to refund with; the row ages out as before, logged.
        self.floor_release: Callable[[str], bool] | None = None
        # refusal_route(body, route, run, why_out=None) -> moved route or None: the refusal fallback's re-route of a
        # refused opening turn to a node's sensitive model (routing.refusal_route; why_out receives the reason
        # categories when it is not taken, #124). None: no fallback (a fixed-model endpoint).
        self.refusal_route: Callable[..., dict | None] | None = None
        # sensitive_cloud(route_id, record) -> bool: the route row's sensitive-cloud record (core.log_sensitive_cloud,
        # #124: detector, reason, notice shown or opted out; categories only). None: logged, not recorded.
        self.sensitive_cloud: Callable[[str, dict], bool] | None = None
        # local_recheck(body, route) -> (admitted, metadata reason): S1 rescores a continuation of a conversation an
        # AUTOMATIC rule bound to a local lane (routing.continuation_admission, which applies routing.local_admission).
        # None: such a continuation cannot be checked and is refused (fails closed). Explicit local pins never use it.
        self.local_recheck: Callable[[dict, dict], tuple[bool, str]] | None = None
        # log_tokens(route_id, usage, office session tag or None, *, office_origin, office_run, office_via): one backend
        # turn's tokens onto its route's row (core.log_tokens). None: tokens are not recorded (fixed-model endpoint, tests).
        self.log_tokens: Callable[..., Any] | None = None
        self._factories = SESSION_FACTORIES if session_factories is None else session_factories
        self._lock = threading.Lock()
        # Orders a conversation's backend-exit Office transition against its requests' turn-end writes, so the exit's
        # `error` is terminal: a finalizer running after it cannot move the dead worker back to `working`/`idle`.
        self._office_exit_lock = threading.Lock()
        self.sessions: dict[str, Any] = {}
        self._opening = 0                              # admission reservations: concurrent starts count toward cap
        # Rebuilds in flight, (scope, wire_path, anchor reply) -> [gate lock, holders] (Registry.rebuild, review P1-a):
        # identical concurrent rebuild requests take turns on the gate, and the later ones join the session the first
        # published instead of starting their own. Read and changed under self._lock; an entry lives while held.
        self._rebuilding: dict[tuple, list] = {}
        # failure_key of a failed conversation -> (diagnostic, failed_at, short id); under self._lock, oldest first.
        self.tombstones: "OrderedDict[str, tuple[str, float, str]]" = OrderedDict()
        self.runs = RunRecords()                    # x-cortados-run-id -> what its routed requests ran on
        self.office = office.OfficeBoard()          # floors / workers / heartbeats behind GET /v1/cortados/office
        # host job heartbeats behind GET /v1/cortados/live (provider/live.py); the LRU never evicts a configured host
        # Its task-id bindings persist in $CORTADOS_HOME (live.BINDINGS_FILE, review round 4): a restart must not reopen
        # a bound id to the first host that reports it afterwards.
        from cortados_router import paths
        self.live = live.HeartbeatBoard(protected=_configured_live_hosts,
                                        path=paths.state_dir() / live.BINDINGS_FILE)
        # Scoped client tokens (clients.json). Consulted only when the owner token does not match, so the owner's
        # behaviour is unchanged; a client token opens only its scopes' endpoints and gets 403 everywhere else.
        self.clients = clients.ClientTokens(owner_token=token)
        # Dashboard password and sessions ($CORTADOS_HOME/dashboard-*.json, provider/dashboard_auth.py). A session opens
        # only the dashboard's views (and local:switch), through Handler._owner_or_session; the owner token is unchanged.
        self.dashboard = dashboard_auth.DashboardAuth()
        # Recently closed conversations, for the 409 diagnostic only (find -> _lost_diagnostic): (closed_at, reason,
        # first reply fingerprint, history length). A 409 whose first fingerprint is here was a conversation this
        # endpoint closed itself (reaped, dropped, failed), which tells a reap from a lost response or an edited
        # history. Fingerprints are digests of reply text, never logged; bounded by count and age so a long-running
        # endpoint keeps only what can still explain a 409 burst.
        self.closed: "deque[tuple[float, str, str, int]]" = deque(maxlen=CLOSED_RECORD_MAX)
        # Recently closed conversations of a run (office key "r:<run id>": a child task's main loop and subagents), as
        # the same metadata live_conversations publishes plus when and why they closed: the live view's orchestrator
        # tree shows a child's finished sub-sessions for a while instead of dropping them. Bounded by count; the view
        # applies its own age limit.
        self.ended: "deque[dict[str, Any]]" = deque(maxlen=ENDED_CONVERSATIONS_MAX)
        # Conversations the idle reaper closed, by closed session id, oldest first (ReapedConversation, #132 item 6):
        # the rebuild source of a client that comes back to one within recover_ttl_s(). Under self._lock; bounded by
        # RECOVERABLE_MAX and pruned by age on every lookup and reap.
        self.recoverable: "OrderedDict[str, ReapedConversation]" = OrderedDict()
        # ended_runs() -> the run ids (x-cortados-run-id) whose child task this host's child-task log says ended. A
        # conversation of any other run keeps the tool-wait idle limit (reap, #132 item 6 (b)). Tests swap in a fake.
        self.ended_runs: Callable[[], set] = _ended_runs_from_log

    def claude_config_dir(self, account: str | None) -> str | None:
        """The CLAUDE_CONFIG_DIR for a routed Claude account id, from tables/accounts.json; None (the default login)
        when no account is named or none is configured."""
        if not account:
            return None
        try:
            from cortados_router import core
            row = core.account_row(core._vendor_accounts("claude", "claude", "claude-cli"), account)
            return row.get("config_dir") if row else None
        except Exception:
            pass
        return None

    def new(self, turn: anthropic_wire.ClientTurn, *, keep: bool, route: dict | None = None,
            scope: str | None = None, wire_path: str | None = None, admitted: bool = False,
            client: str | None = None):
        if keep and not admitted:
            self.reserve_opening()
        try:
            s = self._new(turn, keep=keep, route=route, scope=scope, wire_path=wire_path, client=client)
        except BaseException:
            if keep:
                self.release_opening()
            raise
        if keep:
            self._publish(s)
        return s

    def _publish(self, s) -> None:
        """Make a fully initialised session findable: convert its admission reservation into a live session in one
        critical section. Everything find() and a turn read (history, rebuild_seed, the rebuild budget) must be set
        before this call: from here on any request can find it and take its turn lock."""
        with self._lock:
            self.sessions[s.id] = s
            self._opening -= 1
        if hasattr(s, "set_exit_handler"):
            s.set_exit_handler(self._backend_exited)

    def _backend_exited(self, s) -> None:
        """The backend process died on its own (e.g. the shim killed its CLI on a tool deadline): remove the
        conversation terminally and end the backend, without relying on the shim's abort ever arriving."""
        with self._lock:
            if self.sessions.get(s.id) is not s:
                return
            del self.sessions[s.id]
        logger.warning("conversation %s: backend exited; removed", str(s.id)[:8])
        self.note_closed(s, "exited")
        # Its Office worker: no request remains to end the turn, so the dead worker would show `working` until expiry.
        # Only that worker's record changes (status error); orchestrator heartbeats are untouched.
        worker = getattr(s, "office_worker", None)
        if worker:
            try:
                with self._office_exit_lock:
                    s.office_ended = True              # terminal: office_turn_end skips this conversation from now on
                    self.office.turn_end(worker[0], worker[1], status="error")
            except Exception as exc:
                logger.warning("office: backend-exit bookkeeping failed: %s", type(exc).__name__)
        s.close()

    def office_turn_end(self, session, key: str, tags: dict[str, str], **kw) -> None:
        """A request's turn end on the board, unless the conversation's backend already exited (_backend_exited
        ended its worker as `error`; that transition is terminal)."""
        with self._office_exit_lock:
            if getattr(session, "office_ended", False):
                return
            self.office.turn_end(key, tags, **kw)

    def reserve_opening(self) -> None:
        """Reserve conversation capacity before routing can reserve any quota. At the cap, the longest-idle recoverable
        conversations are retired to make room (_retire_for_room_locked); only when none can be is it a 503."""
        retired: list = []
        try:
            with self._lock:
                if len(self.sessions) + self._opening >= MAX_SESSIONS:
                    retired = self._retire_for_room_locked(time.time())
                if len(self.sessions) + self._opening >= MAX_SESSIONS:
                    raise UnservedProvider("held conversation capacity reached; retry after idle conversations expire")
                self._opening += 1
        finally:
            self._finish_retired(retired)

    def _retire_for_room_locked(self, now: float) -> list:
        """Under self._lock, at the cap: retire conversations, longest idle first, until one more fits. A candidate is
        recoverable (ReapedConversation.eligible: its client can come back and get it rebuilt, and _recovery_left_locked:
        its lineage still has a rebuild to recover with, or its return would be a terminal RebuildLimit), idle at least
        PRESSURE_MIN_IDLE_S, not waiting on a tool result, and its turn lock is free (taken here without blocking, so
        no turn is in flight and none can start: a continuation that loses the race finds the recovery record).
        Returns the retired sessions with their turn locks still held, for _finish_retired outside the lock."""
        cands = sorted((s for s in self.sessions.values()
                        if ReapedConversation.eligible(s) and self._recovery_left_locked(s)
                        and not getattr(s, "awaiting_tool_result", False)
                        and now - getattr(s, "last_used", now) >= PRESSURE_MIN_IDLE_S),
                       key=lambda s: getattr(s, "last_used", now))
        retired = []
        for s in cands:
            if len(self.sessions) + self._opening < MAX_SESSIONS:
                break
            lock = getattr(s, "lock", None)
            if lock is None or not lock.acquire(blocking=False):
                continue                                # a turn is running on it
            if getattr(s, "awaiting_tool_result", False) or not self._retire_locked(s, now):
                lock.release()                          # changed while its lock was free; leave it
                continue
            retired.append(s)
        return retired

    def _recovery_left_locked(self, s) -> bool:
        """Under self._lock: whether recovering s would get a rebuild (review round 2): rebuilding is on (REBUILD_MAX >
        0) and its lineage's committed plus pending rebuilds are below REBUILD_MAX. A conversation without one is never
        retired under pressure: its next continuation would be the terminal 400 "rebuild limit reached" instead."""
        if REBUILD_MAX <= 0:
            return False
        b = getattr(s, "rebuild_budget", None)
        return b is None or b.used + b.pending < REBUILD_MAX

    def _finish_retired(self, retired: list) -> None:
        """Outside self._lock: tear down conversations _retire_locked removed (their turn locks held), then release."""
        for s in retired:
            try:
                compaction_prune.shadow_decide_async(str(s.id))   # background thread; no-op unless the store is on
                logger.warning("closing idle conversation %s under capacity pressure (cap %d; idle %ds; recoverable)",
                               str(s.id)[:8], MAX_SESSIONS, int(time.time() - getattr(s, "last_used", time.time())))
                s.close()                               # teardown must not hold the registry lock
            except Exception as exc:
                logger.warning("closing a retired conversation failed: %s", type(exc).__name__)
            finally:
                s.lock.release()

    def release_opening(self) -> None:
        with self._lock:
            self._opening -= 1

    def rebind_local(self, old, turn: anthropic_wire.ClientTurn, route: dict):
        """Bind a conversation whose opening turn a cloud model refused to the local route the refusal fallback chose:
        a LocalSession (continued by the same reply fingerprints, so the client's next turn finds it) takes the old
        session's place, and the old backend is closed. Its exit is not an unexpected exit: the session is already out
        of the registry when it ends, so _backend_exited ignores it."""
        # client: find's scope without an identity (#43, merged from main): the moved conversation keeps its opener's
        # client, or an identity-less continuation would not find it.
        new = self._new(turn, keep=True, route=route, scope=getattr(old, "scope", None),
                        wire_path=getattr(old, "wire_path", None), client=getattr(old, "client", None))
        new.history = list(old.history)
        new.rebuild_budget = getattr(old, "rebuild_budget", new.rebuild_budget)   # the same lineage
        if getattr(old, "office_worker", None):
            new.office_worker = old.office_worker
        with self._lock:
            if self.sessions.get(old.id) is old:
                del self.sessions[old.id]
            self.sessions[new.id] = new
        try:
            old.close()
        except Exception as exc:                        # the conversation has moved on; only the old backend leaked
            logger.warning("refusal fallback: closing the refused backend failed: %s", type(exc).__name__)
        return new

    def route_new(self, body: dict, client: str, **kwargs) -> dict:
        """Reserve a held-session slot before routing can reserve a quota-floor turn."""
        self.reserve_opening()
        try:
            route = self.router(body, client, **kwargs)
        except BaseException:
            self.release_opening()
            raise
        if route.get("error"):                           # no Registry.new will consume this admission
            self.release_opening()
        return route

    def _new(self, turn: anthropic_wire.ClientTurn, *, keep: bool, route: dict | None,
             scope: str | None, wire_path: str | None, client: str | None = None):
        provider = route["provider"] if route else "claude-cli"
        factory = self._factories.get(provider) or self._factories.get(_base_provider(provider))
        if factory is None and route and route.get("local") and provider.startswith(local_lane.LANE_PREFIX):
            factory = LocalSession                  # a local pin: no backend process, only the conversation's binding
        if factory is None and route and provider == routing.API_PROVIDER and not route.get("local"):
            factory = ApiSession                    # the API credit lane: relayed per turn, no backend process
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
        codex_login = None
        if factory is self._factories.get("codex-cli"):
            # Every Codex session, floor or not, is started on ONE canonical home frozen here (the floor-checked home
            # when the route carries it, routing._bind_codex; else the provider's), and remembers it: a CODEX_HOME
            # symlink moved later cannot change which login a running backend uses, and a floor sent on a later
            # continuation is checked against this identity, never a re-resolved one (Codex review fu2).
            home = (route or {}).get("codex_home") or self.codex_home or getenv("CODEX_HOME") or "~/.codex"
            codex_login = os.path.realpath(os.path.abspath(os.path.expanduser(home)))
            kw["codex_home"] = codex_login
        s = factory(self, **kw)
        s.codex_login = codex_login
        s.route, s.route_logged, s.system = route, False, turn.system
        s.scope = scope                             # conversation_scope of the request that opened it (failure())
        s.wire_path = wire_path                     # the wire it was opened on (/v1/messages, /v1/responses): _replays
        s.client = client                           # the opener's User-Agent name: find's scope without an identity
        s.rebuild_budget = RebuildBudget()          # its lineage's rebuilds (REBUILD_MAX), shared on rebuild
        s.tool_names, s.tools_warned = {t["name"] for t in turn.tools}, False   # what the backend was started with
        s.tools_note = None                         # TOOLS_ADDED_NOTE, sent with the next turn once
        s.awaiting_tool_result = False
        s.opened_at = time.time()                   # the live view's elapsed (live_conversations)
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
             wire_path: str | None = None, client: str | None = None):
        """The live session whose replies are exactly this request's assistant history; None for a new conversation.
        Never continue a conversation on a guess. With no match, a tombstone under `failure_key` makes it a
        FailedSession; a retry of the session's last answered turn (see Replay) raises Replay with the stored
        response; a continuation whose client rewrote the history raises Rebuild (_rebuild_or_refuse, which also
        raises the refusal when no rebuild is possible). More than one such retry candidate is not replayed (it falls
        through to that check). More than one exact match is a Refused (terminal) when it is stable, ConversationBusy
        while a rebuild in the same scope is in flight (the duplicate is that rebuild's own, about to be resolved).
        A request without a conversation identity (scope None, see CONVERSATION_ID_HEADERS; review P1-b) is matched
        only on proof that it is the caller of that session: an exact match whose history carries a tool_use id
        (ClientTurn.history_tool_ids: the backend's own random ids, which only a client that received that
        session's output holds). The User-Agent name is no such proof (every Claude Code sends `claude-cli`), so
        without it the request is refused, terminally (no_identity); it is never rebuilt and never replayed."""
        if not turn.history:
            return None
        with self._lock:
            # Only sessions opened in the request's own scope (wire, client, conversation identity) and on its wire:
            # two clients, or two wires, whose reply histories happen to match must never continue each other's
            # backend (the same rule _replays and _rebuild_or_refuse apply). Identity-less sessions additionally
            # match by the opener's client name (`client`, kept by _new), then need the tool-id proof below.
            matches = self._exact(turn.history, scope, wire_path, client)
            replays = [] if matches or scope is None else self._replays(turn, scope, wire_path)
            if len(replays) == 1:
                # A client in a retry loop touches nothing else, so the replay itself must count as use: otherwise the
                # idle reaper closes the conversation the client is still trying to recover.
                replays[0][0].last_used = time.time()
            inflight = self._rebuild_in_flight(scope, wire_path)
        if scope is None:
            if len(matches) == 1 and turn.history_tool_ids:
                return matches[0]
            # The class: `ambiguous` (several identity-less sessions with this exact history: P1-b's shape),
            # `unproven` (one, but no tool_use id in the history to prove the caller), else _lost_diagnostic's.
            diag = (f"class=ambiguous candidates={len(matches)} request_len={len(turn.history)}" if len(matches) > 1
                    else f"class=unproven candidates=1 request_len={len(turn.history)}" if matches
                    else self._lost_diagnostic(turn))
            logger.warning("refused lost conversation: %s rebuild=refused:no_identity; terminal", diag)
            raise Refused(_NO_IDENTITY_MESSAGE, code="no_identity")
        if len(matches) > 1:
            if inflight:
                raise ConversationBusy("cortados: conversation busy: a rebuild of this conversation is starting; retry")
            logger.warning("refused lost conversation: class=ambiguous candidates=%d request_len=%d; terminal",
                           len(matches), len(turn.history))
            raise Refused(f"cortados: {len(matches)} live conversations of this client have this exact history; "
                          "refusing to pick one, and retrying cannot change that: start a new conversation",
                          code="ambiguous")
        if not matches:
            if len(replays) == 1:
                s, last = replays[0]
                logger.info("conversation %s: replaying its last response to a retry of the answered turn "
                            "(history %d replies); the backend is not called", s.id[:8], len(turn.history))
                raise Replay(s, last)
            dead = self.failure(failure_key, turn.history, scope)
            if dead:
                raise dead
            self._rebuild_or_refuse(turn, scope, wire_path)    # raises Rebuild, or the refusal saying why not
        return matches[0]

    def _exact(self, history: list, scope: str | None, wire_path: str | None, client: str | None = None) -> list:
        """Live sessions of the request's scope and wire whose replies are exactly `history` (under self._lock).
        Identity-less (scope None): only identity-less sessions opened by the same client name."""
        return [s for s in self.sessions.values() if s.history == history
                and getattr(s, "scope", None) == scope and getattr(s, "wire_path", None) == wire_path
                and (scope is not None or getattr(s, "client", None) == client)]

    def _rebuild_in_flight(self, scope: str | None, wire_path: str | None) -> bool:
        """Whether a rebuild in this scope and wire is starting right now (under self._lock): while it is, an old and
        a new session can both hold the same replies for a moment, which is no lasting ambiguity."""
        return scope is not None and any(k[0] == scope and k[1] == wire_path for k in self._rebuilding)

    def _rebuild_or_refuse(self, turn: anthropic_wire.ClientTurn, scope: str | None, wire_path: str | None):
        """A continuation no live session matches exactly: raise Rebuild for the one live conversation it continues
        with a rewritten history, else a refusal naming why no rebuild is possible (issue #43).
        The conversation it continues is the live session that wrote the request's LAST reply (its fingerprint is in
        that session's history), in the request's scope (same wire, client and conversation identity: a stranger's
        session is never rebuilt from, as in _replays) and opened on the request's wire. Two shapes:
        - compacted: that reply is the session's last one too. The client rewrote what came before it (a partial
          compaction, an edited or removed earlier message) and continues from where the session is, so the old
          session is superseded and closed once the rebuilt one is running.
        - branched: that reply is an earlier one of the session (a rewind, an edited message resent, a retry of an
          older turn). Such a request cannot be told from a fork the client sends beside its live loop (Claude Code's
          side requests share the loop's history), so the old session is KEPT: closing it could kill the loop that is
          still running on it. It ends as any idle conversation does (reap).
        The history before the anchor is not compared: rewriting it is what the client did.
        Refusals (the table in docs/PROVIDER-PLAN.md): no identity, no writer, and more than one writer are terminal
        (Refused: the retry carries the same history and meets the same live sessions), except more than one writer
        while a rebuild in the scope is in flight (ConversationBusy: its old and new sessions overlap for a moment);
        REBUILD_MAX 0 keeps the pre-#43 409.
        Recovery (#132 item 6): with no live writer, a conversation the idle reaper closed within recover_ttl_s(), in
        the same scope and wire, whose LAST reply is the request's last reply (_recoverable_writers) is the writer: the
        client came back to a conversation this endpoint closed only for idleness. It is rebuilt as a compaction
        (superseded: nothing live to keep) on its kept route and lineage budget. A conversation closed for any other
        reason (failed, exited, dropped, rebuilt, a boundary refusal) left no record and stays no_writer."""
        if not turn.history:
            return
        anchor = turn.history[-1]
        reaped = False
        with self._lock:
            cands = [] if scope is None or wire_path is None else [
                s for s in self.sessions.values()
                if getattr(s, "scope", None) == scope and getattr(s, "wire_path", None) == wire_path
                and anchor in s.history]
            if not cands and scope is not None and wire_path is not None:
                cands = self._recoverable_writers(scope, wire_path, anchor)
                reaped = bool(cands)
            inflight = self._rebuild_in_flight(scope, wire_path)
        if REBUILD_MAX <= 0:
            # Rebuilds switched off: the pre-#43 409 (tests use it to pin what find and the replay guard decide alone).
            logger.warning("409 lost conversation: %s rebuild=refused:disabled", self._lost_diagnostic(turn))
            raise LostSession("cortados: a conversation this endpoint no longer holds as sent (its history does not "
                              "match a live session's), and rebuilding a conversation from a rewritten history is "
                              "switched off (REBUILD_MAX 0)")
        code = why = None
        if scope is None:
            code, why = "no_identity", _NO_IDENTITY_MESSAGE
        elif not cands:
            code, why = "no_writer", (
                "cortados: a conversation this endpoint no longer holds as sent (its history does not match a live "
                "session's), and no live conversation of this client wrote its last reply (the endpoint restarted, "
                "the conversation expired, or the reply itself was edited), so there is no route or backend to "
                "rebuild from; retrying cannot help: start a new conversation")
        elif len(cands) > 1:
            if inflight:
                raise ConversationBusy("cortados: conversation busy: a rebuild of this conversation is starting; retry")
            code, why = "ambiguous_writer", (
                f"cortados: a conversation this endpoint no longer holds as sent, and {len(cands)} live conversations "
                "of this client wrote its last reply; refusing to pick one to rebuild from, and retrying cannot change "
                "that: start a new conversation")
        if code is None:
            s = cands[0]
            if self._budget(s).used >= REBUILD_MAX:
                # A cheap early refusal on COMMITTED rebuilds only; rebuild() reserves atomically and is the one that
                # counts (a pending reservation there is a retryable busy, never this terminal).
                logger.warning("rebuild refused: conversation %s reached REBUILD_MAX (%d); terminal",
                               str(s.id)[:8], REBUILD_MAX)
                raise RebuildLimit(rebuild_limit_message())
            if reaped:
                logger.info("conversation %s: the client returned %ds after the idle reaper closed it; recovering "
                            "it through a rebuild", s.id[:8], int(time.time() - s.closed_at))
                raise Rebuild(s, superseded=True)
            raise Rebuild(s, superseded=s.history[-1] == anchor)
        logger.warning("refused lost conversation: %s rebuild=refused:%s; terminal", self._lost_diagnostic(turn), code)
        raise Refused(why, code=code)

    def _budget(self, s) -> "RebuildBudget":
        """The rebuild budget of `s`'s lineage, created on first use for a session _new did not give one (a test's
        fake); under the registry lock so two concurrent rebuilds of one parent can never get two budgets."""
        with self._lock:
            b = getattr(s, "rebuild_budget", None)
            if b is None:
                b = s.rebuild_budget = RebuildBudget()
            return b

    def rebuild(self, old, turn: anthropic_wire.ClientTurn, *, superseded: bool, transcript: list,
                scope: str | None, wire_path: str | None, client: str | None = None):
        """A fresh backend for `old`'s conversation, seeded with the client's current history (issue #43): the same
        route (provider, model, account, the frozen Codex home), so the routing decision and its quota reservation
        stand; the request's own system prompt and tools, so the rebuilt backend serves what the client sends now.
        Its history is the request's, so this request and its successors match it exactly (find), and the seed
        (`rebuild_seed`, the history transcript as content blocks) is sent with the first turn it runs (Handler
        work(), which re-checks that seed + turn fit the route under the turn lock), together with that turn's tool
        results as text: the fresh backend never issued those calls. A rebuilt backend is created by the same factory
        as any other, so every isolation rule (no built-in tools, Codex's policy checks) holds.
        In flight (review P1-a): one rebuild at a time per (scope, wire, anchor reply), on a gate taken before the
        budget or the old session is touched. After taking it the exact lookup runs again: a session an identical
        request published meanwhile is JOINED (returned as is: both requests then meet on its turn lock, and the
        loser is a stale continuation whose retry is replayed), never duplicated, so identical concurrent requests
        cannot leave two sessions with one history. A session an identical request rebuilt (same gate key and new
        turn, `rebuilt_for`) that has already answered that turn (review round 3, P1) is not rebuilt again either:
        its stored reply is replayed (Replay), or, if none was kept, the request is a stale continuation
        (LostSession, the 409 work() gives one). A gate held past REBUILD_WAIT_S is ConversationBusy (retryable).
        Budget (review P2-4, P2-d): one RebuildBudget per lineage, shared by every session rebuilt from it (compacted
        or branched), with committed rebuilds (`used`) apart from starts still pending (`pending`). Committed usage
        at REBUILD_MAX is RebuildLimit (terminal); a limit reached only with pending starts is ConversationBusy
        (retryable: a pending start that fails gives its slot back). A slot moves from pending to used in the same
        step that publishes the new session, so neither branching nor concurrent rebuilds of one parent can exceed
        REBUILD_MAX.
        `superseded` (see _rebuild_or_refuse): the old session is closed once the new one runs, under the old one's
        turn lock, so no turn is cut off; a turn running on it raises ConversationBusy (retryable), and so does an old
        session that vanished meanwhile (another request rebuilt it first: the retry finds the new one). Under that
        lock the decision is re-checked (review P1-3): find classified the request before the lock was taken, and a
        normal turn may have advanced the old session since. Its last reply is then no longer the request's anchor,
        so the request now continues from an EARLIER reply of that session, which is exactly the branch shape: it is
        served as a branch and the advanced session is kept.
        A branch keeps the old session and counts toward MAX_SESSIONS like any new conversation.
        Publication (review P1-2): the session is registered only after its history, seed, budget and route state are
        all set (_publish), so a concurrent find() can never return a half-built session. Raises UnservedProvider /
        BridgeError when the new backend cannot start; the old session is then untouched and the slot returned."""
        anchor = turn.history[-1] if turn.history else None
        key = (scope, wire_path, anchor)
        with self._lock:
            gate = self._rebuilding.setdefault(key, [threading.Lock(), 0])
            gate[1] += 1
        try:
            if not gate[0].acquire(timeout=REBUILD_WAIT_S):
                raise ConversationBusy("cortados: conversation busy: another request is rebuilding this conversation; "
                                       "retry once it has finished")
            try:
                fp = anthropic_wire.turn_fingerprint(turn)
                n = len(turn.history)
                with self._lock:
                    joined = self._exact(turn.history, scope, wire_path, client)
                    # Review round 3, P1: an identical request may have rebuilt AND finished its first turn while this
                    # one waited (before the gate, or on it). That session no longer matches exactly (it holds the
                    # reply), but it is this very request's rebuild: by the gate key and the new turn's fingerprint.
                    advanced = [] if joined else [
                        s for s in self.sessions.values()
                        if getattr(s, "rebuilt_for", None) == (key, fp)
                        and len(s.history) == n + 1 and s.history[:n] == turn.history]
                    last = getattr(advanced[0], "last_reply", None) if len(advanced) == 1 else None
                    if last and last[0] == n + 1 and last[1] == fp:
                        advanced[0].last_used = time.time()     # a retry loop's use, as in find()
                if len(joined) == 1:
                    logger.info("conversation %s: an identical request rebuilt it first; joined, not rebuilt again",
                                str(joined[0].id)[:8])
                    return joined[0]
                if advanced:
                    if last and last[0] == n + 1 and last[1] == fp:
                        logger.info("conversation %s: an identical request rebuilt it and was answered first; "
                                    "replaying that answer, not rebuilding again", str(advanced[0].id)[:8])
                        raise Replay(advanced[0], last)
                    # Its reply was not kept for a replay (REPLAY_MAX_BYTES): this request is the stale twin of a
                    # served one, answered as work() answers a stale continuation; a second backend would leave two
                    # sessions with one history.
                    logger.info("conversation %s: an identical request rebuilt it and was answered first, with no "
                                "reply kept to replay; stale continuation (409)", str(advanced[0].id)[:8])
                    raise LostSession("the conversation moved on while this request waited for it: another request "
                                      "with the same history was served first; send the current history")
                return self._rebuild_gated(old, turn, superseded=superseded, transcript=transcript, scope=scope,
                                           wire_path=wire_path, client=client, anchor=anchor, rebuilt_for=(key, fp))
            finally:
                gate[0].release()
        finally:
            with self._lock:
                gate[1] -= 1
                if gate[1] == 0 and self._rebuilding.get(key) is gate:
                    del self._rebuilding[key]

    def _rebuild_gated(self, old, turn, *, superseded: bool, transcript: list, scope: str | None,
                       wire_path: str | None, client: str | None, anchor, rebuilt_for=None):
        """rebuild()'s work once its in-flight gate is held: reserve, start, publish, retire the old session.
        `old` may be a ReapedConversation (#132 item 6): there is no live session to take over or close, so the new
        backend counts as a new conversation against MAX_SESSIONS, and the record is consumed once it is published."""
        recovered = isinstance(old, ReapedConversation)
        budget = self._budget(old)
        with self._lock:
            if recovered and (self.recoverable.get(old.id) is not old
                              or time.time() - old.closed_at > recover_ttl_s()):
                # Recovered by another request meanwhile (its retry finds the rebuilt session) or just expired (its
                # retry gets the terminal no_writer): either way this request cannot use the record now.
                raise ConversationBusy("cortados: conversation busy: this conversation was recovered or expired while "
                                       "this request was looked up; retry")
            if budget.used >= REBUILD_MAX:
                raise RebuildLimit(rebuild_limit_message())
            if budget.used + budget.pending >= REBUILD_MAX:
                raise ConversationBusy("cortados: conversation busy: another rebuild of this conversation is starting "
                                       "and holds its last rebuild; retry once it has finished")
            budget.pending += 1                         # pending; committed below with publication, else given back
        committed = held = False
        try:
            if superseded and not recovered:
                held = old.lock.acquire(blocking=False)
                if not held:
                    raise ConversationBusy("cortados: conversation busy: a turn is running on the session this "
                                           "request would replace; retry once it has finished")
                if self.get(old.id) is not old:
                    raise ConversationBusy("cortados: the conversation was rebuilt or closed while this request was "
                                           "looked up; retry")
                if not old.history or old.history[-1] != anchor:
                    logger.info("conversation %s advanced after the rebuild lookup; served as a branch, kept",
                                str(old.id)[:8])
                    old.lock.release()
                    superseded = held = False
            route = getattr(old, "route", None)
            if route and getattr(old, "codex_login", None):
                route = {**route, "codex_home": old.codex_login}   # the login the conversation already runs on
            if superseded and not recovered:
                with self._lock:
                    self._opening += 1          # one-for-one replacement: the cap is not re-checked for it
            else:
                self.reserve_opening()          # a branch or a recovery is one more conversation: UnservedProvider
            try:
                new = self._new(turn, keep=True, route=route, scope=scope, wire_path=wire_path, client=client)
            except BaseException:
                self.release_opening()
                raise
            new.history = list(turn.history)
            new.rebuild_seed = None if isinstance(new, LocalSession) else transcript   # local: the relay is stateless
            new.rebuild_budget = budget
            new.rebuilt_for = rebuilt_for       # (gate key, new turn's fingerprint): rebuild()'s twin check
            new.route_logged = getattr(old, "route_logged", False)    # the route already ran and was recorded
            if getattr(old, "min_left_pct", None) is not None:
                new.min_left_pct = old.min_left_pct                   # the tightest client floor carries over
            if getattr(old, "run_id", None):
                # the same run's clock (CHILD_RUN_MAX_S): a rebuild or a recovery never restarts it
                new.run_id, new.run_since = old.run_id, getattr(old, "run_since", None)
            with self._lock:
                budget.pending -= 1
                budget.used += 1
                new.rebuilds = number = budget.used
                committed = True
            self._publish(new)                  # only now findable: every field above is set
            if recovered:
                with self._lock:
                    if self.recoverable.get(old.id) is old:
                        del self.recoverable[old.id]    # consumed: the conversation is live again
                logger.warning("conversation %s recovered as %s after its idle reap %ds ago (rebuild %d of %d; "
                               "request_len=%d candidate_len=%d)", old.id[:8], str(new.id)[:8],
                               int(time.time() - old.closed_at), number, REBUILD_MAX, len(turn.history), old.length)
                return new
            logger.warning("conversation %s rebuilt as %s from the client's rewritten history (%s; rebuild %d of %d; "
                           "request_len=%d candidate_len=%d)", str(old.id)[:8], str(new.id)[:8],
                           "compacted: old session closed" if superseded else "branched: old session kept",
                           number, REBUILD_MAX, len(turn.history), len(old.history))
            if superseded:
                with self._lock:
                    if self.sessions.get(old.id) is old:
                        del self.sessions[old.id]
                self.note_closed(old, "rebuilt")
                if hasattr(old, "expire_transport"):
                    # Its CLI may be blocked in a bridged tool call the client will now never answer (the request
                    # carried that result to the rebuilt backend instead): end the wait at once, so close() need not
                    # sit out its kill timeout.
                    old.expire_transport()
                old.close()
            return new
        finally:
            if held:
                old.lock.release()
            if not committed:
                with self._lock:
                    budget.pending -= 1         # the backend never ran: the slot is the lineage's again

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
        """Remember a closed conversation for _lost_diagnostic (see self.closed) and, for a run's, the live tree."""
        compaction_prune.shadow_decide_async(str(s.id))     # background thread; no-op unless the store is on
        hist = getattr(s, "history", None)
        with self._lock:
            if hist:
                self.closed.append((time.time(), reason, hist[0], len(hist)))
            self._note_ended_locked(s, reason)

    def _note_ended_locked(self, s, reason: str) -> None:
        """Under self._lock: keep a closed run conversation's metadata for the live view (self.ended). Never raises."""
        try:
            worker = getattr(s, "office_worker", None)
            key = worker[0] if isinstance(worker, tuple) and worker else None
            if isinstance(key, str) and key.startswith("r:"):      # only a run's conversations: nothing else is read
                meta = self._conversation_meta(s)
                self.ended.append({**meta, "busy": False, "awaiting_tool": False, "closed_at": time.time(),
                                   "closed": reason})
        except Exception:
            logger.debug("ended conversation not recorded", exc_info=True)

    def ended_conversations(self) -> list[dict[str, Any]]:
        """The recently closed run conversations (self.ended), oldest first, for the live view's orchestrator tree."""
        with self._lock:
            return [dict(e) for e in self.ended]

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

    def bridge_session(self, credential: str):
        """The live conversation whose bridge-only credential (BridgeSession.bridge_token, held by its MCP shim) is
        `credential`, or None. Only sessions in the registry match, so a dropped, retired, exited or closed session's
        credential is refused from the moment it leaves. Every candidate is compared in constant time."""
        if not credential:
            return None
        given, found = credential.encode(), None
        with self._lock:
            candidates = list(self.sessions.values())
        for s in candidates:
            t = getattr(s, "bridge_token", None)
            if isinstance(t, str) and t and hmac.compare_digest(given, t.encode()):
                found = s
        return found

    def live_conversations(self) -> list[dict[str, Any]]:
        """Every conversation this endpoint holds open, as metadata for the live view (provider/live.py): its Office key
        (the board's join key), a digest of its id (the id itself is never published), the route's provider, lane and
        model, when it opened and was last used, whether a turn is running on it (its lock is held) and whether it waits
        for the client's tool results. Never history, system text, tools or paths."""
        with self._lock:
            sessions = list(self.sessions.values())
        return [self._conversation_meta(s) for s in sessions]

    @staticmethod
    def _conversation_meta(s) -> dict[str, Any]:
        """One conversation's live-view metadata (live_conversations). Its tokens are the in-memory sum of its backend
        turns (Handler._log_turn_tokens), None before the first recorded turn."""
        route = getattr(s, "route", None) or {}
        rp = str(route.get("provider") or "claude-cli")
        lock = getattr(s, "lock", None)
        worker = getattr(s, "office_worker", None)
        tok = getattr(s, "live_tokens", None)
        # The request's own Office tags (OfficeBoard.read_tags, already validated; live.py re-checks them): which
        # session owns this conversation, without joining through the board's capped worker lists.
        tags = worker[1] if isinstance(worker, tuple) and len(worker) > 1 and isinstance(worker[1], dict) else {}
        return {
            "key": worker[0] if isinstance(worker, tuple) and worker and isinstance(worker[0], str) else None,
            "session": tags.get("session"), "project": tags.get("project"), "device": tags.get("device"),
            "title": tags.get("title"),
            # the x-cortados-origin this conversation's requests carried (office.note_origin): compared by the live
            # view's heartbeat joins, never published
            "origin": getattr(s, "office_origin", None),
            "ref": "c-" + hashlib.sha256(str(s.id).encode()).hexdigest()[:12],
            "provider": office.provider_of(rp, local_lane.LANE_PREFIX),
            "lane": rp if rp.startswith(local_lane.LANE_PREFIX) else (_route_lane(route) if route else None),
            "model": route.get("model") or getattr(s, "model", None),
            "opened_at": getattr(s, "opened_at", None), "last_used": getattr(s, "last_used", None),
            "busy": bool(lock is not None and hasattr(lock, "locked") and lock.locked()),
            "awaiting_tool": bool(getattr(s, "awaiting_tool_result", False)),
            "tokens": dict(tok) if isinstance(tok, dict) else None}

    def drop(self, s, reason: str = "dropped") -> None:
        self.note_closed(s, reason)
        with self._lock:
            self.sessions.pop(s.id, None)
        s.close()

    def reap(self, idle_s: float) -> None:
        """Close conversations idle past their limit: idle_s, or the tool-wait limit (effective_tool_wait) for one
        that waits on the client's tool results or belongs to a child task still running (#132 item 6 (b): its child may
        sit out a long background job between turns). A reaped conversation with an identity is kept as a
        ReapedConversation for recover_ttl_s(), so its client's return is rebuilt rather than refused (item 6 (a))."""
        with self._lock:
            candidates = list(self.sessions.values())
            self._prune_recoverable_locked(time.time())
        ended = None                                    # the child-task log, read at most once per pass
        for s in candidates:
            # Own the turn lock through the decision and close: a continuation must not race a stale snapshot.
            if not s.lock.acquire(blocking=False):
                continue
            try:
                awaiting = getattr(s, "awaiting_tool_result", False)
                run_id = getattr(s, "run_id", None)
                child = False
                if run_id and not awaiting and time.time() - s.last_used > idle_s:
                    if ended is None:
                        try:
                            ended = set(self.ended_runs())
                        except Exception as exc:        # unreadable log: the run counts as running (bounded at 3 h)
                            logger.warning("reap: child-task log unreadable (%s); runs count as running",
                                           type(exc).__name__)
                            ended = set()
                    # A run the log does not show ended keeps the long limit only within a child's own maximum run
                    # time since this conversation's first request of it (review round 1: a finished remote task, or
                    # any valid header, must not hold a conversation for hours).
                    since = getattr(s, "run_since", None)
                    child = (run_id not in ended and since is not None
                             and time.time() - since < CHILD_RUN_MAX_S)
                ttl = effective_tool_wait(idle_s, TOOL_WAIT_S) if (awaiting or child) else idle_s
                # Replay refresh and the final stale check/removal share the registry lock.
                with self._lock:
                    now = time.time()
                    expired = (self.sessions.get(s.id) is s and now - s.last_used > ttl
                               and self._retire_locked(s, now))
                if expired:
                    compaction_prune.shadow_decide_async(str(s.id))   # background thread; no-op unless the store is on
                if expired:
                    logger.info("closing idle conversation %s (awaiting_tool_result=%s, child_run=%s, ttl=%ss)",
                                s.id[:8], awaiting, child, ttl)
                    s.close()  # teardown must not hold the registry lock
            finally:
                s.lock.release()

    def _retire_locked(self, s, now: float) -> bool:
        """Under self._lock, with s's turn lock held: take an idle conversation out of the registry and keep its
        recovery record (ReapedConversation, bounded by RECOVERABLE_MAX, oldest first). The caller closes the backend
        outside the lock. False when s is no longer the registered session of its id (nothing done)."""
        if self.sessions.get(s.id) is not s:
            return False
        del self.sessions[s.id]
        hist = getattr(s, "history", None)
        if hist:
            self.closed.append((now, "reaped", hist[0], len(hist)))
        self._note_ended_locked(s, "reaped")
        if ReapedConversation.eligible(s):
            self.recoverable.pop(str(s.id), None)
            self.recoverable[str(s.id)] = ReapedConversation(s, now)
            while len(self.recoverable) > RECOVERABLE_MAX:
                self.recoverable.popitem(last=False)
        return True

    def _prune_recoverable_locked(self, now: float) -> None:
        """Under self._lock: drop reaped-conversation records older than recover_ttl_s() (oldest first)."""
        ttl = recover_ttl_s()
        while self.recoverable:
            oldest = next(iter(self.recoverable.values()))
            if now - oldest.closed_at <= ttl:
                break
            self.recoverable.popitem(last=False)

    def _recoverable_writers(self, scope: str, wire_path: str, anchor) -> list:
        """Under self._lock: the reaped conversations (ReapedConversation) this request returns to: in its scope (the
        same wire, client name and conversation identity, as for a live writer), opened on its wire, whose LAST reply is
        the request's last reply, closed within recover_ttl_s(). A reply that is only an earlier one of the reaped
        conversation (a branch) does not qualify: only the client coming back to where it left is recovered."""
        self._prune_recoverable_locked(time.time())
        return [r for r in self.recoverable.values()
                if r.scope == scope and r.wire_path == wire_path and r.anchor == anchor]


def _remember_reply(session, turn, msg_id: str, model: str, result) -> None:
    """Keep the session's last response for one replay (Replay), replacing the previous one: only the last answered
    turn can be retried by a client that lost its response, so one per session is all that is needed. Not kept when
    its serialized size passes REPLAY_MAX_BYTES (a replay is then a 409, as before), so idle sessions cannot pin
    large replies in memory. Called before the reply's fingerprint is appended, under the session lock."""
    blocks, stop, usage = result
    # Start the client execution window when the reply is ready, not before a potentially long backend think.
    # Independent of replay storage: even oversized tool replies must retain their waiting conversation.
    session.last_used = time.time()
    session.awaiting_tool_result = stop == "tool_use" or any(b.get("type") == "tool_use" for b in blocks)
    if session.awaiting_tool_result and hasattr(session, "arm_tool_result_deadline"):
        session.arm_tool_result_deadline(session.last_used + effective_tool_wait(IDLE_S, TOOL_WAIT_S))
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


class ReapedConversation:
    """What Registry.reap keeps of a conversation it closed for idleness (#132 item 6), so the client's return is
    rebuilt (Registry.rebuild) instead of refused: the rebuild source for _rebuild_or_refuse when no live conversation
    wrote the request's last reply. Metadata only: the conversation's scope (a digest of wire, client name and
    conversation identity, Registry.conversation_scope), its wire and client name, its route (what rebuild reuses: the
    same provider, model, account and frozen Codex login), the fingerprint (a sha256 digest) of its LAST reply and its
    length, the lineage's RebuildBudget (shared, so recovering counts against REBUILD_MAX), the client's quota floor and
    the run id. Never the system prompt, tools, messages, tool arguments or replies; __slots__ keeps it that way.
    `id` is the closed session's id, so log lines can name it."""

    __slots__ = ("id", "scope", "wire_path", "client", "route", "codex_login", "anchor", "length", "rebuild_budget",
                 "route_logged", "min_left_pct", "was_local", "run_id", "run_since", "closed_at")

    def __init__(self, s, closed_at: float):
        self.id = str(s.id)
        self.scope, self.wire_path = getattr(s, "scope", None), getattr(s, "wire_path", None)
        self.client = getattr(s, "client", None)
        self.route, self.codex_login = getattr(s, "route", None), getattr(s, "codex_login", None)
        self.anchor, self.length = s.history[-1], len(s.history)
        b = getattr(s, "rebuild_budget", None)
        self.rebuild_budget = b if b is not None else RebuildBudget()
        self.route_logged = getattr(s, "route_logged", False)
        self.min_left_pct = getattr(s, "min_left_pct", None)
        self.was_local = isinstance(s, LocalSession)
        self.run_id = getattr(s, "run_id", None)
        self.run_since = getattr(s, "run_since", None)
        self.closed_at = closed_at

    @staticmethod
    def eligible(s) -> bool:
        """Only a conversation a rebuild could ever be matched to: one with a conversation identity (scope), a wire and
        at least one reply. An identity-less one is never rebuilt (no_identity), so nothing is kept for it."""
        return bool(getattr(s, "scope", None) and getattr(s, "wire_path", None) and getattr(s, "history", None))


class Replay(Exception):
    """A request that retries a live session's last answered turn (Registry._replays): the client never received that
    response (a lost or timed-out reply), so the session's history is one reply ahead of the request's and every
    later request would 409. The stored response is sent again; the backend is not called and the session is
    unchanged. `last` is the session's last_reply: (history length, turn fingerprint, msg_id, model, blocks, stop,
    usage)."""

    def __init__(self, session, last):
        super().__init__("replay")
        self.session, self.last = session, last


class Rebuild(Exception):
    """The request continues a live conversation whose history the client rewrote (Registry._rebuild_or_refuse):
    `session` is that conversation's backend, `superseded` whether the request continues from its last reply (the old
    session is then replaced) or branches from an earlier one (kept)."""

    def __init__(self, session, *, superseded: bool):
        super().__init__("rebuild")
        self.session, self.superseded = session, superseded


class RebuildBudget:
    """How many rebuilds one conversation lineage has used (REBUILD_MAX). One object is shared by a session and every
    session rebuilt from it, compacted or branched. `used` counts committed rebuilds (a backend that started and was
    published), `pending` the starts still in progress (given back if they fail); both are read and changed only under
    Registry._lock. Only `used` makes the limit terminal (review P2-d)."""

    def __init__(self):
        self.used = 0
        self.pending = 0


def rebuild_limit_message() -> str:
    """RebuildLimit's text, read at call time (REBUILD_MAX is a module setting tests rebind)."""
    return (f"cortados: this conversation reached the rebuild limit ({REBUILD_MAX} rebuilds) after its client rewrote "
            "the history again and again; retrying cannot help: start a new conversation")


class Refused(LostSession):
    """A continuation no retry can serve (Registry.find / _rebuild_or_refuse): answered TERMINAL_STATUS with
    x-should-retry: false, never the 409 Claude Code retries, because the retry carries the same history and meets the
    same live sessions. `code` names the category for the log and the request record (no_identity, no_writer,
    ambiguous, ambiguous_writer, limit); the message is this endpoint's own text."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class RebuildLimit(Refused):
    """The conversation's lineage committed all REBUILD_MAX rebuilds: terminal, because its retry carries the same
    rewritten history."""

    def __init__(self, message: str):
        super().__init__(message, code="limit")


# Refused(no_identity): the request continues a conversation but names none (review P1-b).
_NO_IDENTITY_MESSAGE = (
    "cortados: this request continues a conversation but carries no conversation identity (one of "
    + ", ".join(CONVERSATION_ID_HEADERS) + ") and no tool call id of that conversation, so it cannot be told from "
    "another client's conversation with the same replies; it is not continued, replayed or rebuilt. Retrying cannot "
    "help: send a conversation identity header (Claude Code and Codex send one by themselves), or start a new "
    "conversation")


class ConversationBusy(Exception):
    """A rebuild could not take over its old session right now (a turn runs on it, or it was just replaced): the
    answer is the retryable BUSY_STATUS, and the retry finds the conversation's current state."""


class FailedSession(LostSession):
    """The request belongs to a conversation that was closed because its backend failed (a tombstone): answered with
    a terminal error (TERMINAL_STATUS / TERMINAL_ERROR) naming the failure's category, not the 409 of a conversation
    this endpoint never held or has forgotten."""
    diagnostic: str | None = None


REBUILD_LIMIT_REASON = "the conversation reached its rebuild limit (terminal)"
REBUILD_TOO_LARGE_REASON = "the rebuilt history does not fit the conversation's model (terminal)"
MODEL_CHANGE_REASON = "the continuation asks for another model than its conversation runs (terminal)"
# Refused.code -> the fixed record reason (_note_refusal); never request-derived text.
REFUSED_REASONS = {
    "no_identity": "the continuation carries no conversation identity (terminal)",
    "no_writer": "no live conversation of this client wrote the request's last reply (terminal)",
    "ambiguous": "more than one live conversation has the request's exact history (terminal)",
    "ambiguous_writer": "more than one live conversation wrote the request's last reply (terminal)",
    "limit": REBUILD_LIMIT_REASON,
}


def replay_overflow(session, transcript: list, turn) -> tuple[int, int] | None:
    """(need, cap) in tokens when a rebuilt backend's first turn (the seed transcript, the new turn, the system prompt
    and the tools) does not fit `session`'s frozen route, else None (review P2-5). Measured with the estimator the
    opening route was fitted with (routing.request_fit, chars / 4 of the JSON body). A local pin is never checked: its
    seed is never sent (the relay re-sends the whole body under its own per-turn fit gate); neither is a route without
    context_length (a fixed-model endpoint: the backend reports an overflow itself)."""
    cap = (getattr(session, "route", None) or {}).get("context_length")
    if not cap or isinstance(session, LocalSession) or getattr(session, "was_local", False):
        return None
    seed_body = {"system": turn.system, "tools": turn.tools,
                 "messages": [{"role": "user", "content": anthropic_wire.rebuild_content(transcript, turn)}]}
    need = int(routing.request_fit(seed_body)["input_tokens"])
    return (need, int(cap)) if need > int(cap) else None


def replay_overflow_message(need: int, cap: int) -> str:
    return (f"cortados: the client rewrote this conversation's history, and replaying it on a fresh backend "
            f"(≈{need // 1000}k tokens) does not fit the conversation's model context ({cap // 1000}k tokens); "
            "retrying cannot help: start a new conversation or compact it further")


def _failed_reason(exc: object) -> str:
    """The refusal reason for a request on a failed conversation: fixed text plus the tombstone's allowlisted
    failure_diagnostic (never the backend's own text)."""
    diag = getattr(exc, "diagnostic", None)
    return "the conversation already failed (terminal)" + (f": {diag}" if diag else "")


# What the backend model is told, once, when the client added tools after the backend started (#117, #121): it must not
# call them, and should say so if the task needs them. Only the provider can say it: the client lists the tools, and
# the backend's own error ("No such tool available") does not say why. A FIXED text (#117 review round 2): a client's
# tool name is not text the provider may put in its own voice (anthropic_wire.shown_name), and the model needs no list:
# whatever it was shown but cannot call fails at once. The provider log names the tools (as shown_name renders them).
TOOLS_ADDED_NOTE = ("[cortados provider] Tools were added to this conversation after it started. They cannot be called "
                    "in it: only the tools available when the conversation started can be called. Do not call the "
                    "added tools; if the task needs one, say so in your answer (it works in a new conversation that "
                    "has it from its first request).")


# The tool-schema budget (#117 review round 2). With tool search off (child tasks, --strict-mcp-config sessions) every
# MCP tool schema is in every request, and a host's own MCP servers load too, outside any launch-time selection. A tool
# COUNT bounds nothing (66 tools with 8,000-character descriptions came to ~133k tokens), and the launcher cannot list
# a connector's tools (OAuth in the exec host's Keychain). So the provider enforces the real serialized size of each
# request's `tools`, the one place every client's catalog arrives in full, before anything is routed or started.
#   - BYTES_PER_TOKEN: the measured 290k tokens / ~1.1 MB of the full connector catalog (README "Connectors") is
#     request_fit's own chars / 4 estimate (~3.8 bytes a token), not a tokenizer count, so it cannot set the factor.
#     JSON schemas (quotes, braces, short identifiers, camelCase) tokenize denser than prose; 3 bytes a token is
#     taken as the conservative factor (api_credits' cost bound uses 2 for a ceiling on a bill, not a budget).
#   - CLIENT_BUILTIN_TOOLS_BYTES: Claude Code's own tools are in `tools` too and are not the catalog: 2.1.325 with tool
#     search off and no MCP server sent 23 built-in tools = 70,034 bytes (local sink, 2026-11-14); 80 KB leaves room for
#     a release's growth.
#   - TOOL_CATALOG_BUDGET_TOKENS: 40k tokens of MCP schemas leaves a child on the 128k local lane room for its system
#     prompt, the built-ins and the work itself.
# Applied to EVERY request, not only eager ones: the provider cannot tell tool search on from off except by a client
# detail (a ToolSearch tool), a deferred request (~70 KB of built-ins + ToolSearch) is far below the budget, and one
# rule bounds every client alike (Codex declares its tools up front, always). A deferred session would trip it only
# after loading >120 KB of MCP schemas, which then cost the same context on every turn anyway.
# Refused with a 400, not a 413: retrying unchanged cannot succeed, and Claude Code shows a 413 as "Request too large
# (max 32MB) ... images" (README "Connectors"). routing.request_fit still gates the whole request against each route.
BYTES_PER_TOKEN = 3
CLIENT_BUILTIN_TOOLS_BYTES = 80_000
TOOL_CATALOG_BUDGET_TOKENS = 40_000
TOOL_SCHEMA_BUDGET_BYTES = CLIENT_BUILTIN_TOOLS_BYTES + TOOL_CATALOG_BUDGET_TOKENS * BYTES_PER_TOKEN    # 200,000
TOOL_BUDGET_NAMESPACES_SHOWN = 6
TOOL_BUDGET_REASON = "the request's tool schemas are over the tool-schema budget"
UNPREFIXED_TOOLS = "(tools without a server prefix)"


def tool_namespace(name: Any) -> str:
    """The namespace a tool's size is reported under: `mcp__<server>` for an MCP tool (Claude Code and Codex both name
    them mcp__<server>__<tool>), UNPREFIXED_TOOLS for any other valid name (built-ins, Codex functions), and
    anthropic_wire.INVALID_NAME for a name shown_name would not show."""
    shown = anthropic_wire.shown_name(name)
    if shown == anthropic_wire.INVALID_NAME:
        return shown
    if shown.startswith("mcp__"):
        server = shown[len("mcp__"):].split("__", 1)[0]
        return f"mcp__{server}" if server else UNPREFIXED_TOOLS
    return UNPREFIXED_TOOLS


def tool_schema_overrun(body: Any) -> str | None:
    """None when the request's `tools` serialize within TOOL_SCHEMA_BUDGET_BYTES (or are absent or malformed:
    parse_request reports that), else the client's error message: the size, the budget, the largest namespaces (only
    as tool_namespace renders them: never a raw client string) and how to shrink the set. Measured as request_fit
    measures the body (json.dumps, UTF-8 bytes)."""
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list) or not tools:
        return None
    try:
        total = len(json.dumps(tools).encode())
        if total <= TOOL_SCHEMA_BUDGET_BYTES:
            return None
        sizes: dict[str, list[int]] = {}
        for t in tools:
            entry = sizes.setdefault(tool_namespace(t.get("name") if isinstance(t, dict) else None), [0, 0])
            entry[0] += 1
            entry[1] += len(json.dumps(t).encode())
    except RecursionError:                  # nested past json's limit: parse_request answers it as a shape error
        return None
    ranked = sorted(sizes.items(), key=lambda kv: (-kv[1][1], kv[0]))
    shown = "; ".join(f"{ns} ({n} tool{'s' if n != 1 else ''}, {b // 1000} KB)"
                      for ns, (n, b) in ranked[:TOOL_BUDGET_NAMESPACES_SHOWN])
    more = len(ranked) - TOOL_BUDGET_NAMESPACES_SHOWN
    return (f"cortados: the request's tool definitions are {total // 1000} KB (≈{total // BYTES_PER_TOKEN // 1000}k "
            f"tokens), over this endpoint's tool-schema budget of {TOOL_SCHEMA_BUDGET_BYTES // 1000} KB "
            f"(≈{TOOL_SCHEMA_BUDGET_BYTES // BYTES_PER_TOKEN // 1000}k tokens: the client's built-in tools plus "
            f"≈{TOOL_CATALOG_BUDGET_TOKENS // 1000}k tokens of MCP tools). Every tool schema is sent with every request "
            f"(tool search is off), so the conversation would have no room. Largest tool namespaces: {shown}"
            + (f"; and {more} more" if more > 0 else "") +
            ". Shrink the tool set: fewer connectors (start_task `connectors`), or start Claude Code with "
            "--strict-mcp-config and an --mcp-config naming only the servers this work needs (the host's own MCP "
            "servers count too). Retrying unchanged cannot succeed.")


def with_tools_note(results: dict, note: str | None) -> dict:
    """The client's tool results with the note appended as a text item to the first one (a copy; the turn and its
    fingerprint are unchanged)."""
    if not note or not results:
        return results
    out = dict(results)
    first = next(iter(out))
    out[first] = {**out[first], "content": list(out[first].get("content") or []) + [{"type": "text", "text": note}]}
    return out


class Handler(BaseHTTPRequestHandler):
    server: "ProviderServer"
    _scoped: "clients.Client | None" = None             # the scoped client of the current request (_scope_gate)
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S

    def log_message(self, fmt, *args):                  # route http.server's stderr lines into logging
        # fmt is http.server's own constant; its args (the request line, a path) are client text: each rendered by
        # log_text, and the record escaped again by the installed SafeFormatter (serve()).
        logger.debug("%s " + fmt, self.address_string(), *(anthropic_wire.log_text(a, 300) if isinstance(a, str) else a for a in args))

    # ------------------------------------------------------------------ plumbing
    def _credential(self) -> str:
        given = self.headers.get("x-cortados-token") or self.headers.get("x-api-key") or ""
        auth = self.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            given = given or auth[7:]
        return given

    def _authorized(self) -> bool:
        ok = hmac.compare_digest(self._credential().encode(), self.server.registry.token.encode())
        self._authed = ok                               # per request: do_GET/do_POST reset it first (keep-alive reuse)
        return ok

    def _peer(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _dash_session(self) -> "dashboard_auth.Session | None":
        """The dashboard session this request's cookie names, or None. Only a request that carries no token at all is
        looked up: a client that sends a token is judged on that token alone, never on a cookie beside it."""
        if self._credential():
            return None
        try:
            return self.server.registry.dashboard.session(self.headers.get("cookie"))
        except Exception as exc:                        # fail closed for the cookie; the owner token is unaffected
            logger.error("dashboard session check failed: %s", type(exc).__name__)
            return None

    def _owner_or_session(self, method: str) -> bool:
        """THE auth check of the dashboard's views (live, usage, tasks, office) and of the local switch
        (/v1/cortados/local and /local/switch): True for a scoped client token _scope_gate already admitted (no
        clients.json scope covers the local switch, so none reaches it), for the owner token, or, when the request
        carries no token, for a dashboard session whose scopes cover (method, path) (dashboard_auth.SESSION_SCOPES). A
        state-changing method on a session must also pass the same-origin and CSRF-token check (the page's fetch must
        set referrerPolicy "same-origin", else the browser sends `Origin: null` under the page's no-referrer policy).
        Everything else (/v1/messages, /v1/responses, heartbeats, runs, models, resources) calls _authorized(), which
        never reads the cookie. On False, _refuse_auth() answers."""
        self._auth_refusal = None
        if self._scoped is not None or self._authorized():
            return True
        s = self._dash_session()
        if s is None or not s.allows(method, self.path.split("?")[0]):
            return False
        if method not in dashboard_auth.SAFE_METHODS and not self.server.registry.dashboard.csrf_ok(
                s, self.headers, self._peer()):
            self._auth_refusal = (403, "permission_error", "cortados: cross-site or CSRF-less dashboard request refused")
            return False
        return True

    def _refuse_auth(self):
        code, kind, msg = getattr(self, "_auth_refusal", None) or (401, "authentication_error", "bad or missing token")
        return self._error(code, kind, msg)

    def _html_page(self, code: int, page: bytes, csp: str, headers: dict[str, str] | None = None) -> None:
        """An HTML page with the dashboard's headers; `headers` replaces a header of the same name, never doubles it."""
        self.send_response(code)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(page)))
        for k, v in {**DASHBOARD_HEADERS, "Content-Security-Policy": csp, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(page)

    def _dashboard_get(self) -> None:
        """GET /dashboard: no password set, the token-paste page (as before); a password set, the dashboard in session
        mode for a valid session cookie, else the login page. An unusable auth file is a 500, never the token page."""
        auth = self.server.registry.dashboard
        try:
            rec = auth.record()
        except dashboard_auth.AuthFileError:
            return self._html_page(500, _login_page("dashboard login unavailable: dashboard-auth.json is unusable "
                                                    "(see the provider log)"), LOGIN_CSP, LOGIN_HEADERS)
        session = None
        if rec is not None:
            session = self._dash_session()
            if session is None:
                return self._html_page(200, _login_page(), LOGIN_CSP, LOGIN_HEADERS)
        try:
            page = _dashboard_page(session)
            csp = _dashboard_csp()
        except (OSError, ValueError) as exc:
            logger.warning("dashboard page unavailable: %s", type(exc).__name__)
            return self._error(500, "api_error", "dashboard page unavailable")
        return self._html_page(200, page, csp)

    def _dashboard_login(self) -> None:
        """POST /dashboard/login (form field `password`). Unauthenticated, so everything that can be refused is refused
        before the body is read: a free login slot (503), size, type, origin, and the lockout (429). The body is then
        read under LOGIN_BODY_DEADLINE_S. Every refusal closes the connection. The password is never logged."""
        self.close_connection = True                    # one login per connection: nothing unread is parsed later
        slots = self.server.login_slots
        if not slots.acquire(blocking=False):
            logger.warning("dashboard login refused: %d logins already in progress", LOGIN_MAX_CONCURRENT)
            return self._html_page(503, _login_page("too many logins in progress; try again shortly"), LOGIN_CSP,
                                   dict(LOGIN_HEADERS, **{"Retry-After": "5"}))
        try:
            return self._dashboard_login_slot()
        finally:
            slots.release()

    def _dashboard_login_slot(self) -> None:
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > LOGIN_MAX_BYTES:
            return self._html_page(413, _login_page("the login request is too large"), LOGIN_CSP, LOGIN_HEADERS)
        ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._html_page(415, _login_page("the login form must be posted as a form"), LOGIN_CSP, LOGIN_HEADERS)
        if not dashboard_auth.same_origin(self.headers, self._peer()):
            logger.warning("dashboard login refused: not same-origin (origin %s, host %s, forwarded proto %s)",
                           anthropic_wire.log_safe(self.headers.get("origin") or "", 100),
                           anthropic_wire.log_safe(self.headers.get("host") or "", 100),
                           anthropic_wire.log_safe(self.headers.get("x-forwarded-proto") or "", 20))
            return self._html_page(403, _login_page("cross-site login refused"), LOGIN_CSP, LOGIN_HEADERS)
        addr = dashboard_auth.client_address(self._peer(), self.headers.get("x-forwarded-for") or "")
        auth = self.server.registry.dashboard
        r = auth.locked_out(addr)                       # before the read: a locked-out client gets no thread on its body
        if r is None:
            raw = http_body.read_within(self.connection, self.rfile, n, LOGIN_BODY_DEADLINE_S, self.timeout)
            if raw is None:                             # too slow or gone: no reply worth writing, the socket is closed
                logger.warning("dashboard login from %s dropped: body not received within %.1f s", addr,
                               LOGIN_BODY_DEADLINE_S)
                return
            from urllib.parse import parse_qs
            try:
                form = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
            except ValueError:                          # undecodable bytes or too many fields
                return self._html_page(400, _login_page("the login form is malformed"), LOGIN_CSP, LOGIN_HEADERS)
            password = (form.get("password") or [""])[0]
            r = auth.login(password, addr)
            del password, form, raw
        if r["ok"]:
            self.send_response(303)
            self.send_header("Location", "/dashboard")
            self.send_header("Set-Cookie", dashboard_auth.session_cookie(r["cookie"]))
            self.send_header("Cache-Control", "no-store")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        headers = dict(LOGIN_HEADERS, **({"Retry-After": str(r["retry_after"])} if r.get("retry_after") else {}))
        msg = r["why"] + (f"; try again in {max(1, round(r['retry_after'] / 60))} min" if r.get("retry_after") else "")
        return self._html_page(r["status"], _login_page(msg), LOGIN_CSP, headers)

    def _dashboard_logout(self) -> None:
        """POST /dashboard/logout: ends the cookie's session. Needs the session, same origin and its CSRF token."""
        self.close_connection = True                    # the body (if any) is never read
        s = self._dash_session()
        if s is None:
            return self._error(401, "authentication_error", "no dashboard session")
        auth = self.server.registry.dashboard
        if not auth.csrf_ok(s, self.headers, self._peer()):
            return self._error(403, "permission_error", "cortados: cross-site or CSRF-less dashboard request refused")
        try:
            auth.logout(s)
        except OSError as exc:
            logger.error("dashboard logout: cannot update the sessions file: %s", type(exc).__name__)
            return self._error(500, "api_error", "cortados: logout failed (see provider log)")
        return self._json(200, {"ok": True}, headers={"Set-Cookie": dashboard_auth.clear_cookie(),
                                                      "Cache-Control": "no-store"})

    def _scoped_client(self) -> "clients.Client | None":
        """The scoped client whose token this request carries, or None (no token, the owner token, or an unknown one).
        The owner token is checked first and never reaches the client table."""
        given = self._credential()
        if not given or hmac.compare_digest(given.encode(), self.server.registry.token.encode()):
            return None
        return self.server.registry.clients.match(given)

    def _scope_gate(self, method: str) -> bool:
        """For a request carrying a scoped client token: True when it was refused (403: its scopes do not cover this
        method and path, the public pages included, so a reader's token is only ever good for its view). Sets
        self._scoped to the client when its scopes do cover the request. False for every other request, untouched."""
        self._scoped = None
        try:
            c = self._scoped_client()
        except Exception as exc:                        # the table failed: fail closed for that token, owner unaffected
            logger.error("client token check failed: %s", type(exc).__name__)
            c = None
        if c is None:
            return False
        if c.allows(method, self.path.split("?")[0]):
            self._scoped = c
            return False
        self._scoped = c
        self._error(403, "permission_error", _SCOPE_DENIED)
        return True

    def _control_body(self, max_bytes: int, deadline_s: float) -> dict[str, Any] | None:
        """The JSON object body of a small control POST (POST /v1/cortados/local/switch), or None after an error was
        sent. Unlike _body (the wires, count_tokens, the office heartbeat and /bridge/call), it does not trust
        content-length: missing is a 411, negative or not a number a 400, over `max_bytes` a 413, all refused before a
        byte is read; and the read has an absolute deadline (http_body.read_within, the reader the login and the
        local gate share: each wait given only the time left, so a trickle cannot stretch it) past which it is a 408. Every refusal before the body is fully read closes the connection: the
        unread rest must not be parsed as the next request."""
        cl = self.headers.get("content-length")
        if cl is None:
            self.close_connection = True
            return self._error(411, "invalid_request_error", "cortados: content-length is required",
                               reason="the request was refused as invalid")
        try:
            n = int(cl.strip()) if cl.strip().isdigit() else -1
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return self._error(400, "invalid_request_error", "cortados: content-length is not a byte count",
                               reason="the request was refused as invalid")
        if n > max_bytes:
            self.close_connection = True
            return self._error(413, "invalid_request_error", f"cortados: this body is at most {max_bytes} bytes",
                               reason="the control request body is too large")
        raw = http_body.read_within(self.connection, self.rfile, n, deadline_s, self.timeout)
        if raw is None:                                 # past the deadline, stalled or gone
            self.close_connection = True
            return self._error(408, "invalid_request_error",
                               f"cortados: the body did not arrive within {deadline_s:g}s",
                               reason="the control request body did not arrive in time")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:                              # JSONDecodeError and undecodable bytes alike
            return self._error(400, "invalid_request_error", "body is not JSON", reason="body is not JSON")
        if not isinstance(body, dict):
            return self._error(400, "invalid_request_error", "body must be a JSON object")
        return body

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
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _error(self, code: int, kind: str, message: str, w: Wire = wire, *, reason: str | None = None,
               headers: dict[str, str] | None = None) -> None:
        """Answer an error in the wire's shape. `message` goes to the client only; the log line and the run record get
        the fixed `reason` of _note_refusal. `headers` are added to the answer (e.g. x-should-retry: false)."""
        self._note_refusal(code, kind, reason=reason)
        if headers:
            self._json(code, w.error_json(kind, message), headers=headers)
        else:
            self._json(code, w.error_json(kind, message))

    def _note_refusal(self, code: int, kind: str, *, reason: str | None = None) -> None:
        """The ONE place a refusal is logged and recorded, for every failure path (JSON errors, SSE errors, the local
        relay). Logged with an endpoint id, its status, type and fixed reason (a 413 or 400 used to leave no line at
        all). An authenticated POST that names a valid run id also records it there, so GET /v1/cortados/runs/<id> can
        tell a headless client why it was refused. `reason` must be one of this endpoint's own fixed strings (see
        _REFUSAL_REASONS). Bookkeeping only: it never raises into the response."""
        try:
            where = endpoint_id(self.path)
            bridged = getattr(self, "_bridge", None)
            if bridged is not None:
                # A session's MCP shim (bridge-only credential): logged by its conversation, never recorded.
                logger.warning("bridge %s: %s %s refused with %s %s", str(bridged.id)[:8], self.command, where, code, kind)
                return
            client = getattr(self, "_scoped", None)
            if client is not None:
                # A scoped client: named in the log, never recorded (its token cannot write run records).
                logger.warning("client %s: %s %s refused with %s %s", client.name, self.command, where, code, kind)
                return
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
                    if self.path.split("?")[0] in WIRES:   # the office shows the refused run as an error
                        tags = getattr(self, "_office_tags", None) or {}
                        board = self.server.registry.office
                        key = board.worker_key(tags, run_id, None)
                        board.turn_start(key, tags, run_id=run_id, provider=None, lane=None, model=None)
                        board.turn_end(key, tags, status="error")
        except Exception as exc:
            logger.warning("refusal bookkeeping failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ routes
    def _unsupported_method(self):
        self._authed, self._bridge = False, None
        if not self._scope_gate(self.command):
            # Preserve BaseHTTPRequestHandler's unsupported-method response for owner/unknown credentials.
            self.send_error(501, "Unsupported method (%r)" % self.command)

    def __getattr__(self, name):
        # BaseHTTPRequestHandler dispatches via do_<method>. Cover EVERY unsupported method, including extension
        # methods, rather than an allowlist that silently leaves gaps in the scoped-token 403 contract.
        if name.startswith("do_"):
            return self._unsupported_method
        raise AttributeError(name)

    def do_GET(self):
        self._authed, self._bridge = False, None
        if self._scope_gate("GET"):
            return
        if self.path.split("?")[0] == clients.OFFICE_PATH:
            # The owner token, a scoped token with office:read, or a dashboard session. Read-only, metadata only.
            if not self._owner_or_session("GET"):
                return self._refuse_auth()
            try:
                view = self.server.office_view(self.server.registry)
                body = json.dumps(view, allow_nan=False).encode()
            except Exception as exc:
                logger.warning("office: view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: office view unavailable (see provider log)")
            return self._json(200, body, headers=VIEW_HEADERS)
        if self.path.split("?")[0] == live.LIVE_PATH:
            # Owner token, a scoped token with live:read, or a dashboard session. Metadata only (provider/live.py).
            if not self._owner_or_session("GET"):
                return self._refuse_auth()
            # ?session=<tag>: one orchestrating session's tasks and conversations (live.for_session); a bad tag is a 400
            sess = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("session")
            if sess is not None and (len(sess) != 1 or live.valid_session(sess[0]) is None):
                return self._error(400, "invalid_request_error",
                                   "cortados: session must be " + live.SESSION_FILTER_RULE)
            try:
                body = self.server.live_body(self.server.registry, sess[0] if sess else None)
            except Exception as exc:
                logger.warning("live: view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: live view unavailable (see provider log)")
            return self._json(200, body, headers=VIEW_HEADERS)
        if self.path.split("?")[0] == live.SESSION_PATH:
            # One orchestrating session's own tree (live.session_view): the pulse pane. Owner token, live:read or the
            # narrower live:session scope. ?session=<tag> is required: this endpoint never lists sessions, so a
            # live:session token reads only a subtree whose tag it already holds (docs/PULSE-PER-SESSION.md).
            if not self._owner_or_session("GET"):
                return self._refuse_auth()
            sess = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("session")
            if not sess or len(sess) != 1 or live.valid_session(sess[0]) is None:
                return self._error(400, "invalid_request_error",
                                   "cortados: session is required and must be " + live.SESSION_FILTER_RULE)
            try:
                body = self.server.live_body(self.server.registry, sess[0], session_view=True)
            except Exception as exc:
                logger.warning("live: session view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: live view unavailable (see provider log)")
            return self._json(200, body, headers=VIEW_HEADERS)
        if self.path == "/health":
            return self._json(200, {"ok": True, "conversations": len(self.server.registry.sessions)})
        if self.path in ("/", "/dashboard"):
            return self._dashboard_get()
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
        if self.path.split("?")[0] == local_switch.VIEW_PATH:
            # The owner token or a dashboard session (scope local:switch). A scoped client token was already refused
            # by _scope_gate: no clients.json scope names this path, by design.
            if not self._owner_or_session("GET"):
                return self._refuse_auth()
            try:
                view = self.server.local_view()
            except local_lane.LocalConfigError as exc:   # as /v1/models: the node is named, the file path is not
                logger.warning("local: %s", exc)
                return self._error(500, "api_error", "cortados: local node "
                                   f"{getattr(exc, 'node', None) or '?'} is misconfigured on the provider (see provider log)")
            except Exception as exc:
                logger.warning("local: view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: local view unavailable (see provider log)")
            return self._json(200, view, headers=VIEW_HEADERS)
        if self.path.split("?")[0] == "/v1/cortados/tasks":
            if not self._owner_or_session("GET"):        # the owner token or a dashboard session
                return self._refuse_auth()
            try:
                tv = self.server.tasks_view(self.server.registry)
                if isinstance(tv, dict) and isinstance(tv.get("token_totals"), dict):
                    # the work split's internal pending child runs are counted unattributed here (no task log join)
                    tv = {**tv, "token_totals": work_split.resolve_pending(tv["token_totals"], None)}
                body = json.dumps(tv, allow_nan=False).encode()
            except Exception as exc:
                logger.warning("tasks: view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: tasks view unavailable (see provider log)")
            return self._json(200, body, headers=VIEW_HEADERS)
        if self.path.split("?")[0] == "/v1/cortados/usage":
            if not self._owner_or_session("GET"):        # the owner token or a dashboard session
                return self._refuse_auth()
            try:   # a bug in a source must not drop the connection: a clean 500, detail (type only) in the log
                view = self.server.usage_view(self.server.registry)
                # the live view arrives encoded (usage.collect memoises the bytes); a dict is encoded strictly here,
                # before any byte is sent, so a NaN is a clean 500 and never invalid JSON
                body = view if isinstance(view, bytes) else json.dumps(view, allow_nan=False).encode()
            except Exception as exc:
                logger.warning("usage: view failed: %s", type(exc).__name__)
                return self._error(500, "api_error", "cortados: usage view unavailable (see provider log)")
            return self._json(200, body, headers=VIEW_HEADERS)
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
        self._authed, self._bridge = False, None
        self._office_tags = {}
        self._office_origin = None
        path = self.path.split("?")[0]
        if self._scope_gate("POST"):
            return
        if path == live.HEARTBEAT_PATH:
            return self._live_heartbeat()
        if path == LOGIN_PATH:
            return self._dashboard_login()
        if path == LOGOUT_PATH:
            return self._dashboard_logout()
        if path == "/bridge/call":
            return self._bridge_post()
        # Every error on a wire's path is in that wire's shape, the 401 and the unreadable body included: Codex prints
        # the Responses error body verbatim and would show an Anthropic envelope as noise.
        if path == local_switch.SWITCH_PATH:
            # The owner token, or a dashboard session (scope local:switch) that also passes the same-origin + CSRF-token
            # check (_owner_or_session -> DashboardAuth.csrf_ok): a browser attaches the cookie to a cross-site POST,
            # never the x-cortados-csrf header. A scoped client token never reaches here: no clients.json scope covers
            # this path, so _scope_gate answered 403 above. All refused from the headers alone, before a byte is read.
            if not self._owner_or_session("POST"):
                self.close_connection = True            # its body stays unread: never parse it as the next request
                return self._refuse_auth()
            body = self._control_body(local_switch.SWITCH_MAX_BODY, local_switch.SWITCH_BODY_DEADLINE_S)
            return None if body is None else self._local_switch(body)
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
            try:   # cosmetic: an invalid tag is dropped and counted inside, and a bug here never refuses the request
                self._office_tags = self.server.registry.office.read_tags(self.headers)
                self._office_origin = self.server.registry.office.read_origin(self.headers)
            except Exception as exc:
                logger.warning("office: tag read failed: %s", type(exc).__name__)
            return self._messages(body, w)
        if path == OFFICE_HEARTBEAT_PATH:
            hb, bad = office.validate_heartbeat(body)
            if hb is None:
                return self._error(400, "invalid_request_error", f"cortados: heartbeat field {bad} is invalid",
                                   reason="the heartbeat is malformed")
            return self._json(200, self.server.registry.office.heartbeat(hb))
        if path == BOUNDARY_CHECK_PATH:
            return self._boundary_check(body)
        if path == "/v1/messages/count_tokens":
            # Estimate only (chars / 4): the CLI behind cortados counts its own tokens and exposes no counter.
            return self._json(200, {"input_tokens": len(json.dumps(body)) // 4})
        self._error(404, "not_found_error", path)

    def _boundary_hook(self) -> bool:
        """Whether this request's client runs the boundary hook (effect_boundary.HOOK_HEADER: exactly "1")."""
        return (self.headers.get(effect_boundary.HOOK_HEADER) or "").strip() == "1"

    def _boundary_check(self, body: dict[str, Any]) -> None:
        """POST /v1/cortados/boundary/check (docs/BOUNDARY-HOOK.md): the client's PreToolUse hook asks about one
        tool_use id; the answer is the provider's record of it (effect_boundary.check_answer) or {"known": false}.
        Owner token only (checked by do_POST before the body is read). Metadata only: the id, never a tool input."""
        tid = body.get("tool_use_id")
        if not isinstance(tid, str) or not _TOOL_USE_ID_RE.fullmatch(tid):
            return self._error(400, "invalid_request_error", "cortados: tool_use_id must be 1-128 of [A-Za-z0-9_-]",
                               reason="the boundary check is malformed")
        try:
            answer = effect_boundary.check_answer(tid)
        except Exception as exc:                        # the hook fails closed on a 500 for a marked id
            logger.error("boundary check failed: %s", type(exc).__name__)
            return self._error(500, "api_error", "cortados: boundary check failed (see provider log)")
        return self._json(200, answer, headers={"cache-control": "no-store"})

    def _live_heartbeat(self) -> None:
        """POST /v1/cortados/live/heartbeat (provider/live.py): a host's job states. The owner token or a live:heartbeat
        client token. The size is checked from content-length BEFORE any byte is read, so a huge body costs nothing.
        Every refusal before the read closes the connection: the unread body must not be parsed as the next request."""
        if self._scoped is None and not self._authorized():
            self.close_connection = True
            return self._error(401, "authentication_error", "bad or missing token")
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return self._error(400, "invalid_request_error", "cortados: content-length is not a byte count",
                               reason="the heartbeat is malformed")
        if n > live.HEARTBEAT_MAX_BYTES:
            self.close_connection = True
            return self._error(413, "invalid_request_error",
                               f"cortados: a heartbeat is at most {live.HEARTBEAT_MAX_BYTES} bytes",
                               reason="the heartbeat is too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:                              # JSONDecodeError and undecodable bytes alike
            return self._error(400, "invalid_request_error", "body is not JSON", reason="body is not JSON")
        now = time.time()
        hb, bad = live.validate_heartbeat(body, now)
        if hb is None:
            return self._error(400, "invalid_request_error", f"cortados: heartbeat field {bad} is invalid",
                               reason="the heartbeat is malformed")
        c = self._scoped
        if c is not None and hb["host"] != c.host:
            # A live:heartbeat client speaks for its bound host only (clients.py); the owner token is never bound.
            # The posted name is not echoed; the bound one is the client's own configuration.
            return self._error(403, "permission_error",
                               f"cortados: this token posts heartbeats for host {c.host or '(none)'} only",
                               reason="the heartbeat names another host")
        return self._json(200, self.server.registry.live.post(hb, now))

    def _local_switch(self, body: dict[str, Any]) -> None:
        """POST /v1/cortados/local/switch {"node", "on"} (owner token or CSRF-checked dashboard session, checked by
        do_POST): flip a local node's manual
        switch through its gate (provider/local_switch.py). The answer is the node's new status row."""
        try:
            row = self.server.local_switch(body.get("node"), body.get("on"))
        except local_switch.SwitchError as exc:
            kind = {400: "invalid_request_error", 404: "not_found_error", 409: "invalid_request_error"}.get(
                exc.status, "api_error")
            return self._error(exc.status, kind, f"cortados: {exc}",
                               reason={400: "the switch request is malformed", 404: "unknown local node",
                                       409: "the local node has no gate configured",
                                       503: "the local node is unreachable"}.get(exc.status, "the local gate failed"))
        except local_lane.LocalConfigError as exc:
            logger.warning("local switch: %s", exc)
            return self._error(500, "api_error", "cortados: local node "
                               f"{getattr(exc, 'node', None) or '?'} is misconfigured on the provider (see provider log)")
        except Exception as exc:
            logger.warning("local switch failed: %s", type(exc).__name__)
            return self._error(500, "api_error", "cortados: local switch failed (see provider log)")
        return self._json(200, row, headers=VIEW_HEADERS)

    def _bridge_post(self) -> None:
        """POST /bridge/call: the owner token, or a session's bridge-only credential (BridgeSession.bridge_token, the
        one its MCP shim holds), which opens this endpoint for that session only. That credential is refused on every
        other endpoint (_authorized checks the owner token alone) and once its session has left the registry."""
        bound = None
        if not self._authorized():
            bound = self.server.registry.bridge_session(self._credential())
            if bound is None:
                return self._error(401, "authentication_error", "bad or missing token")
            self._bridge = bound
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._error(400, "invalid_request_error", "body is not JSON", reason="body is not JSON")
        except ValueError as exc:                       # JSON, but not an object
            return self._error(400, "invalid_request_error", str(exc))
        if bound is not None and body.get("session") != bound.id:
            # Another session's credential: the same answer as a bad token, so it reveals nothing about that session.
            return self._error(401, "authentication_error", "bad or missing token")
        return self._bridge_call(body)

    def _bridge_call(self, body: dict[str, Any]) -> None:
        s = self.server.registry.get(body.get("session", ""))
        if s is None or not hasattr(s, "wait_result"):   # only the Claude bridge's MCP shim calls back over HTTP
            return self._json(200, {"result": {"content": [{"type": "text", "text": "cortados: session closed"}], "isError": True}})
        if body.get("abort") is True:
            # The shim's outer transport expired. Close without taking the turn lock: a continuation may own it,
            # but BridgeSession's _cv terminal transition makes a concurrent delivery lose atomically.
            s.expire_transport()
            if self.server.registry.get(s.id) is s:
                self.server.registry.drop(s, "bridge_transport_expired")
            return self._json(200, {"closed": True})
        try:
            result = s.wait_result(body["tool_use_id"], timeout=effective_tool_wait(IDLE_S, TOOL_WAIT_S))
        except ToolWaitExpired as exc:
            # End the CLI before answering the shim. Never convert expiry into an error tool_result that lets the
            # unattended model take another paid turn; a late client continuation now sees a closed conversation.
            if self.server.registry.get(s.id) is s:
                self.server.registry.drop(s, "tool_result_expired")
            return self._error(410, "invalid_request_error", f"cortados: {exc}")
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
        """An `api/<model>` pin: one stateless turn on the anthropic-api credit lane (_api_turn). No session, no S1."""
        # A revealing client (the routed-subagents mod) also sees the credit's % left after the real model the upstream
        # response names; read BEFORE this turn reserves. Without the header nothing about the relay changes.
        reveal = (api_reveal_suffix() if self._wants_reveal() else None)
        # Recorded once admitted (it is about to run). The label is the reveal the response carries when one was asked
        # for; otherwise the model (the meter is not read just for the record).
        self._api_turn(body, model, wire, reveal=reveal,
                       admitted=lambda: self._record_run(provider="anthropic-api", model=model,
                                                         label=model + reveal if reveal else model,
                                                         tools=bool(body.get("tools"))))

    def _api_turn(self, body: dict[str, Any], model: str, wire: Wire, *, reveal: str | None = None,
                  echo_model: str | None = None, reply: list | None = None,
                  commit: Callable[[list], None] | None = None,
                  admitted: Callable[[], None] | None = None) -> str:
        """One turn on the anthropic-api credit lane (#114), under the lane's money rules (api_credits): refuse unless
        the lane is on, reserve the request's UPPER-bound cost under the ledger lock before dispatch (never past the
        95% stop line, whatever runs concurrently), proxy to api.anthropic.com, then settle the real cost from the
        response's usage (the full reservation when it is unknown). Fails closed: an unreadable config or ledger
        refuses. Returns "refused" (answered before anything was sent) or anthropic_proxy.serve's outcome."""
        from cortados_router import api_credits
        from cortados_router.provider import anthropic_proxy
        why = api_credits.key_problem()
        if why:
            self._error(503, "overloaded_error", f"cortados: the anthropic-api credit lane is off: {why}", wire)
            return "refused"
        body = api_credits.enforce_server_tool_caps(body)   # cap metered tools to what we reserve
        try:
            max_cost = api_credits.estimate_max_cost(body, model)
        except api_credits.Unbounded as exc:            # a cost no reservation can cover: never dispatched
            # Non-retryable: the same request can never be bounded (a routed request never gets here with it:
            # routing.prefer_api_credit runs the same estimate and leaves such a request where it was).
            self._error(400, "invalid_request_error", f"cortados: the anthropic-api credit lane cannot bound the cost "
                        f"of this request ({exc}); not permitted on this lane", wire, headers={"x-should-retry": "false"})
            return "refused"
        now = time.time()
        cap = anthropic_proxy.turn_cap_s()
        # The turn's hard wall-clock deadline, measured from BEFORE the reservation (so never later than cap after the
        # send): the reservation admits only a turn that ends by then well before its grant expires.
        deadline = time.monotonic() + cap
        try:
            res_id = api_credits.reserve(max_cost, now, duration_s=cap)
        except api_credits.LedgerError as exc:
            logger.error("anthropic-api accounting unavailable, lane off, refusing to spend blind: %s", exc)
            self._error(503, "overloaded_error",
                        f"cortados: the anthropic-api credit lane is off: its accounting is unavailable ({exc})", wire)
            return "refused"
        if res_id is None:
            st = api_credits.state(now)
            if not st.get("on"):
                why = st.get("reason")
            elif max_cost > float(st.get("headroom_usd") or 0):
                why = (f"this request's upper-bound cost (${max_cost:.2f}) does not fit under the stop line "
                       f"(${float(st.get('headroom_usd') or 0):.2f} left)")
            else:
                why = (f"a turn started now could still run (its {cap / 60:.0f}-minute time cap) when the credit grant "
                       "stops counting")
            self._error(429, "rate_limit_error", f"cortados: the anthropic-api credit lane refused the request: {why}",
                        wire)
            return "refused"
        key = api_credits.api_key()
        if not key:                                     # the key went away between the check and the reservation
            api_credits.settle(res_id, 0.0, now)
            self._error(503, "overloaded_error", "cortados: the anthropic-api credit lane has no key", wire)
            return "refused"
        logger.info("anthropic-api lane: reserved $%.4f for %s", max_cost, routing.shown_pin(model))
        if admitted is not None:
            try:
                admitted()
            except Exception:
                logger.exception("anthropic-api lane: admission record failed")
        usage: dict[str, Any] = {}
        outcome = "unknown"                       # if serve raises, we do not know what was billed -> conservative
        try:
            usage, outcome = anthropic_proxy.serve(self, body, model, now, key=key,
                                                   **({"model_suffix": reveal} if reveal else {}),
                                                   **({"echo_model": echo_model} if echo_model else {}),
                                                   **({"reply": reply} if reply is not None else {}),
                                                   **({"commit": commit} if commit is not None else {}),
                                                   deadline=deadline)
        except OSError as exc:
            logger.info("anthropic-api proxy: client left (%s)", type(exc).__name__)
        except Exception as exc:
            # Any other failure out of serve() (e.g. a response-parse error after the POST was dispatched) leaves the
            # spend genuinely unknown -> keep outcome "unknown" so we settle conservatively rather than crash/undercharge.
            logger.error("anthropic-api proxy failed after dispatch (settling conservatively): %s", type(exc).__name__)
        finally:
            geo = bool(body.get("inference_geo"))
            if outcome == "rejected":
                charge = 0.0                       # the API returned an error; nothing was billed
            elif outcome == "completed":
                charge = api_credits.cost_from_usage(model, usage, geo=geo)   # a terminal response: the actual cost
            else:
                # unknown (timeout / transport error / mid-stream disconnect) or aborted at the time cap (a closed
                # connection is no proof billing stopped): the full reservation, but never below the cost we already
                # observed on the wire, so a partial-but-expensive turn cannot under-settle.
                charge = max(max_cost, api_credits.cost_from_usage(model, usage, geo=geo))
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
                # The reservation stays in the ledger at its full (upper-bound) amount, so the budget still counts it.
                logger.error("anthropic-api settle gave up for %s: reservation $%.4f retained (charge was $%.4f)",
                             res_id, max_cost, charge)
        return outcome

    def _relay_api(self, session: "ApiSession", turn, body: dict[str, Any], *, created: bool, one_shot: bool,
                   wire: Wire) -> str:
        """One turn of a conversation routing moved to the API credit lane (routing.prefer_api_credit): relayed
        statelessly by _api_turn (the client sends the whole history), under the conversation's lock and the same
        history rules as a local relay (_relay_local): the reply's fingerprint is recorded just before its terminal
        bytes reach the client, and undone if the turn did not complete. An opening turn that fails drops the
        conversation. A later turn the lane can no longer take (stop line, expiry, ledger) is refused with the reason;
        moving the conversation to another lane mid-way is #43's re-route seam, not built here."""
        reg = self.server.registry
        route = session.route or {}
        if not session.lock.acquire(timeout=TURN_TIMEOUT_S):
            self._error(BUSY_STATUS, "overloaded_error", "cortados: conversation busy; retry once the other request on "
                        "it has finished", wire)
            return "refused"
        try:
            if not created and reg.get(session.id) is not session:
                self._error(BUSY_STATUS, "overloaded_error", "cortados: conversation busy: it was rebuilt or closed "
                            "while this request waited for it; retry", wire)
                return "refused"
            if not created and session.history != turn.history:
                self._error(409, "invalid_request_error", "the conversation moved on while this request waited for "
                            "it: another request with the same history was served first; send the current history",
                            wire)
                return "refused"
            undo: list = []

            def commit(blocks: list) -> None:
                undo.append((len(session.history), session.awaiting_tool_result))
                session.awaiting_tool_result = any(b.get("type") == "tool_use" for b in blocks)
                session.history.append(anthropic_wire.reply_fingerprint(blocks))

            reveal = (api_reveal_suffix() or "") if self._wants_reveal() else None
            echo = f"{session.model}{reveal}" if reveal is not None else turn.model
            reply: list = []
            t0 = time.time()
            office_key = self._office_turn_start(session, one_shot=one_shot)
            outcome = "incomplete"

            def admitted() -> None:
                if created:
                    self._record_run(provider=str(route.get("provider") or "anthropic-api"), model=session.model,
                                     label=session.model, tools=not one_shot)
            try:
                outcome = self._api_turn(body, session.model, wire, echo_model=echo, reply=reply, commit=commit,
                                         admitted=admitted)
            except Exception as exc:
                logger.exception("anthropic-api relay failed")
                outcome = "incomplete"
                try:
                    self._error(502, "api_error", f"cortados: API relay failed: {type(exc).__name__}", wire)
                except Exception:
                    pass
            if outcome != "completed" and undo:
                n, awaiting = undo[0]
                del session.history[n:]
                session.awaiting_tool_result = awaiting
            elif outcome == "completed" and not undo:
                commit(reply)                       # defensive: a completed turn always committed already
            session.last_used = time.time()
            self._office_turn_end(session, office_key, {"turn": (reply, "anthropic-api", None)}
                                  if outcome == "completed" else {"error": outcome})
            if outcome == "completed":
                if route.get("route_id") and not session.route_logged and reg.executed:
                    try:
                        reg.executed(route)
                        session.route_logged = True
                    except Exception as exc:
                        logger.warning("anthropic-api lane: route record failed: %s", exc)
                    else:
                        if reg.log_outcome:
                            chars = sum(len(b.get("text", "")) for b in reply
                                        if isinstance(b, dict) and b.get("type") == "text")
                            ms = int((time.time() - t0) * 1000)

                            def _log_outcome(route=route, chars=chars, ms=ms):
                                try:
                                    reg.log_outcome(route, chars, ms)
                                except Exception:
                                    logger.exception("outcome log for route %s failed", route.get("route_id"))
                            threading.Thread(target=_log_outcome, name="cortados-outcome-log", daemon=True).start()
            elif created:
                reg.drop(session)
        finally:
            session.lock.release()
        return outcome

    def _open_conversation(self, turn, body: dict[str, Any], wire: Wire, *, one_shot: bool, floor: float | None,
                           scope: str | None, also_exclude: str | None = None):
        """Route and open a new conversation (or a one-shot) for `turn`: the session, or None when the request was
        refused (the answer is sent). `also_exclude`: a model to keep the route off on top of the client's
        x-cortados-exclude-model (a local model whose node cannot hold the request: the caller's re-route)."""
        reg = self.server.registry
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
            if also_exclude:                            # a re-route: the model whose node could not hold the request
                exclude = ",".join(x for x in (exclude, also_exclude) if x)
            local, valid = _local_header(self.headers)
            if not valid:
                return self._error(400, "invalid_request_error", _LOCAL_BAD, wire)
            try:
                route_fn = reg.router if one_shot else reg.route_new
                route = route_fn(wire.routing_body(body), self._client(),
                                 **({"exclude_model": exclude} if exclude else {}),
                                 **({"local": local} if local else {}),
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
            return reg.new(turn, keep=not one_shot, route=route, scope=scope,
                           wire_path=self.path.split("?")[0], admitted=bool(reg.router and not one_shot),
                           client=self._client())
        except UnservedProvider as exc:
            return self._error(503, "overloaded_error", f"cortados: {exc}", wire)
        except BridgeError as exc:                  # the backend refused to start (version pin, isolation, login)
            logger.warning("backend start failed: %s", exc)
            return self._error(502, "api_error", str(exc), wire, reason=failure_diagnostic(exc))

    def _relay_local(self, session: "LocalSession", turn, body: dict[str, Any], *, created: bool, one_shot: bool,
                     wire: Wire, reroute: bool | None = None) -> str | None:
        """One turn of a local-pinned conversation: relayed statelessly to its Ollama node (local_lane.serve), under
        the conversation's lock and the same stale-history rule as a CLI turn. The reply's fingerprint is appended to
        the session's history, so the next turn finds this conversation and keeps its model (_model_change). A node
        that fails a turn leaves the conversation as it was: the laptop may wake, and the same history retried
        continues it. A conversation whose opening turn failed is dropped (no reply exists to continue from).
        Returns the lane's outcome (None when refused before the relay). `reroute` (default: an opening turn a rule
        moved here, not a pin) lets the lane hand back local_lane.TOO_LARGE, UNSUPPORTED or NODE_ERROR
        (local_lane.REROUTE), unanswered, when the node's gate can never take the turn or the node answered a 5xx on a
        COST-profile bind of a request every sensitivity detector cleared (local_lane.CLOUD_REROUTE_OK); the caller then
        routes the conversation elsewhere."""
        reg = self.server.registry
        route = session.route or {}
        if not session.lock.acquire(timeout=TURN_TIMEOUT_S):
            return self._error(BUSY_STATUS, "overloaded_error", "cortados: conversation busy; retry once the other "
                               "request on it has finished", wire)
        try:
            if not created and reg.get(session.id) is not session:
                # Review round 3, P2: the lookup ran before the lock, and the conversation's session was replaced
                # (a compaction rebuilt it) or closed meanwhile. Relaying on the retired session would answer from a
                # conversation no later request can find; the retry is looked up afresh. Nothing reaches the node.
                logger.info("local conversation %s was replaced or closed while this request waited; 529",
                            str(session.id)[:8])
                return self._error(BUSY_STATUS, "overloaded_error", "cortados: conversation busy: it was rebuilt or "
                                   "closed while this request waited for it; retry", wire)
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
            need = local_lane.input_tokens(body, local["model"])   # request_fit's estimate; images as read (#125)
            cap = int(local["model"]["context_length"])
            if need > cap:
                return self._error(413, "request_too_large",
                                   f"cortados: sub/{local_lane.pin_id(local['model'])}: the conversation (≈{need // 1000}k "
                                   f"tokens) now exceeds {local['model']['id']}'s configured context ({cap // 1000}k); "
                                   "compact it or start a new conversation", wire,
                                   reason="the conversation exceeds the local model's configured context")
            t0 = time.time()
            office_key = self._office_turn_start(session, one_shot=one_shot)
            outcome = "incomplete"
            if reroute is None:              # an opening turn a rule moved here may be routed elsewhere (REROUTE)
                reroute = created and not route.get("pinned")
            # #117 review round 5: the reply's fingerprint is recorded BEFORE the client can see the answer complete
            # (local_lane.serve calls this just before the final body / message_stop event is written). Recorded after
            # delivery, a client continuing at once was looked up (Registry.find, before this lock) against the old
            # history and refused terminally (no_writer). Undone below if the delivery then failed.
            undo: list = []

            def commit(blocks: list) -> None:
                undo.append((len(session.history), session.awaiting_tool_result))
                session.awaiting_tool_result = any(b.get("type") == "tool_use" for b in blocks)
                session.history.append(anthropic_wire.reply_fingerprint(blocks))

            try:
                outcome = local_lane.serve(self, body, local["node"], local["model"], echo_model=turn.model,
                                           reply=reply, auto_routed=not route.get("pinned"),
                                           bind_reason=route.get("bind_reason"), hook=self._boundary_hook(),
                                           conversation=str(session.id), reroute=reroute,
                                           commit=commit,
                                           # cleared by every sensitivity detector when routed (#122 round 2)
                                           cloud_ok=route.get(local_lane.CLOUD_REROUTE_OK) is True)
            except Exception as exc:             # serve handles its own I/O; anything else must still clean up
                logger.exception("local lane: relay failed")
                outcome = "incomplete"
                try:
                    self._error(502, "api_error", f"cortados: local relay failed: {type(exc).__name__}", wire,
                                reason="the local relay failed")
                except Exception:
                    pass                         # headers may already be out; the client sees a broken response
            if outcome == "completed" and not undo:
                commit(reply)                    # a serve that completed without calling it (defensive)
            elif outcome != "completed" and undo:
                # The client never received the whole answer (its connection broke on the final write): the
                # conversation stays where it was, as for any failed turn. Still under the lock, so no turn ran since.
                n, awaiting = undo[0]
                del session.history[n:]
                session.awaiting_tool_result = awaiting
            turn_ms = int((time.time() - t0) * 1000)
            session.last_used = time.time()
            self._office_turn_end(session, office_key, {"turn": (reply, "local", None)} if outcome == "completed"
                                           else {"error": outcome})
            if outcome == "completed":
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
        return outcome

    def _local_continuation_refused(self, session: "LocalSession", body: dict[str, Any], wire: Wire) -> bool:
        """The continuation re-check (docs/LOCAL-RESOURCES.md "Continuations of a local-bound conversation"): a
        conversation an AUTOMATIC rule bound to a local lane (auto_route, the published-index route, the sensitive flag /
        prefer / keyword, the refusal fallback; route not `pinned`) is rescored by S1 on every later turn (Registry.local_recheck →
        routing.continuation_admission → routing.local_admission) before anything reaches the node. An explicit local
        pin (`sub/<local model or alias>`) is the client's own choice and is not rescored.

        Not admitted (an outward effect, blast_radius 2 or more, a degraded scorer, missing or malformed answers, no
        checker): the turn is refused with a terminal 400 (`x-should-retry: false`) telling the client the conversation
        is local-bound and this turn needs a cloud model. Nothing was sent, so the conversation is untouched: its
        history is what it was, and a later turn with that same history that passes the gate continues it. Logs the
        route id and the gate's metadata reason only, never the request text. Returns True when it answered (refused).

        SEAM(#43): moving the conversation to a cloud lane mid-way (re-route when the Pro is unplugged) would replace
        this refusal with a rebind; not built here."""
        route = getattr(session, "route", None) or {}
        if route.get("pinned"):
            return False
        rid = route.get("route_id")
        reg = self.server.registry
        if reg.local_recheck is None:
            ok, why = False, "this endpoint has no continuation re-check (fixed model)"
        else:
            try:
                ok, why = reg.local_recheck(wire.routing_body(body), route)
            except Exception as exc:                    # a checker bug fails closed, as a degraded scorer does
                logger.exception("local continuation re-check failed for %s", rid)
                ok, why = False, f"the re-check failed ({type(exc).__name__})"
        if ok is True:
            logger.info("local continuation admitted for %s: %s", rid, why)
            return False
        logger.warning("local continuation refused for %s: %s", rid, why)
        self._note_refusal(400, "invalid_request_error",
                           reason="a turn of a local-bound conversation failed the local admission gate")
        self._json(400, wire.error_json(
            "invalid_request_error",
            "cortados: this conversation is bound to a local model on this machine, and this turn may have an effect "
            "outside it (or could not be checked), so it needs a cloud model; it was not sent. Start a new "
            "conversation (it is routed again) or pin a cloud model with sub/<model>."),
            headers={"x-should-retry": "false"})
        return True

    def _api_continuation_refused(self, session: "ApiSession", body: dict[str, Any], wire: Wire) -> bool:
        """The continuation gate of a conversation the API credit expiry preference moved to the API (#114 review round
        1), run on EVERY later turn before anything is forwarded. The opening turn was moved only because it was
        local-eligible COST work that every sensitivity detector cleared, under an expiring grant
        (routing.prefer_api_credit); a later turn is forwarded only while all of that still holds
        (routing.api_continuation_admission): no x-cortados-local header (sensitive, or the client's own prefer for
        local), no sensitivity detector marking the turn, S1 admitting it under the local gate (no outward effect, blast
        radius 0-1, not degraded: Registry.local_recheck, the local continuation's own check), and the lane still on
        with a grant inside its prefer window. Not admitted, the turn is refused with a terminal 400 before anything was
        sent (the conversation is untouched; a new conversation is routed again). Moving it mid-way is #43's seam.
        Logs the route id and a metadata reason only. Returns True when it answered (refused)."""
        route = getattr(session, "route", None) or {}
        rid = route.get("route_id")
        local, valid = _local_header(self.headers)
        if not valid:
            ok, why = False, "the x-cortados-local header is malformed"
        else:
            try:
                ok, why = routing.api_continuation_admission(wire.routing_body(body), route, local=local,
                                                             recheck=self.server.registry.local_recheck)
            except Exception as exc:                    # a checker bug fails closed
                logger.exception("api credit continuation gate failed for %s", rid)
                ok, why = False, f"the gate failed ({type(exc).__name__})"
        if ok is True:
            logger.info("api credit continuation admitted for %s: %s", rid, why)
            return False
        logger.warning("api credit continuation refused for %s: %s", rid, why)
        self._note_refusal(400, "invalid_request_error",
                           reason="a turn of an API-credit conversation failed its continuation gate")
        self._json(400, wire.error_json(
            "invalid_request_error",
            "cortados: this conversation was moved to the API credit lane because its opening turn was local-eligible "
            "work no detector marked sensitive, under credit about to expire; this turn no longer qualifies "
            f"({why}), so it was not sent. Start a new conversation (it is routed again) or pin a model with "
            "sub/<model>."), headers={"x-should-retry": "false"})
        return True

    def _refusal_fallback(self, session, turn, body: dict[str, Any], result, *, created: bool, one_shot: bool,
                          wire: Wire, why_out: list[str] | None = None):
        """The refusal fallback (docs/LOCAL-RESOURCES.md "Sensitive requests"): when the cloud backend refused this
        turn (stop "refusal"; only the Claude bridge reports one, the Codex app-server has no refusal signal), re-run it
        on a node's sensitive model and bind the conversation there. Returns (session, local result) or None (the
        refusal stands and goes to the client as it is). Only the OPENING turn of a conversation on /v1/messages, and
        only when no client tool has run: a request carrying an assistant message or a tool result, or a refused reply
        holding a tool_use, is never re-run, so a tool's effect can never repeat. Eligibility of the request itself (no
        outward effect, scorer not degraded, exclusions) was settled when it was routed (routing.sensitive_local);
        routing.refusal_route rechecks the node and rewrites the routes row only once the local answer exists. Logs the
        rule and metadata reasons only, never the request or the reply. `why_out` receives why a refusal of an
        S1-routed cloud conversation was not moved, as sensitive_notice reason categories (#124); nothing for a
        pinned or local conversation or a fixed-model endpoint (no detector applies there)."""
        why_out = [] if why_out is None else why_out
        blocks, stop, _usage = result
        if stop != "refusal":
            return None
        reg = self.server.registry
        route = getattr(session, "route", None)
        rid = (route or {}).get("route_id")
        why, category = None, None
        if not _routed_cloud(route):
            # Checked before any notice category (#124 round 3): an unrouted conversation (a fixed-model endpoint), a
            # pin or a local lane has no detector and no route row, so no category is named and nothing is re-run.
            why = "the conversation is not S1-routed to a cloud model"
        elif not created:
            # HOOK(rebuild-on-409): a refusal later in a conversation could move it too, by seeding the local lane from
            # the client's transcript as branch rebuild-on-409 does for a lost backend. Not built: the conversation
            # stays on its cloud model and the client sees the refusal, with the notice saying so (#124 round 2).
            # Checked before the wire: on either wire a continuation is never moved; its notice is the continuation one.
            why = "a refusal after the opening turn does not move the conversation (follow-up: rebuild-on-409)"
            category = sensitive_notice.REFUSED_CONTINUATION
        elif wire is not ANTHROPIC:
            why = "the conversation is on /v1/responses (the local relay speaks Messages only)"
            category = "responses_wire"
        elif (turn.tool_results or any(isinstance(m, dict) and m.get("role") == "assistant"
                                       for m in body.get("messages") or [])
              or any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)):
            why = "client tools ran or were requested in this conversation; a turn with tool effects is never re-run"
            category = "tools_ran"
        elif reg.refusal_route is None:
            why = "this endpoint has no refusal fallback"
        if why:
            logger.info("sensitive: refusal not taken for %s: %s", rid, why)
            if category:
                why_out.append(category)
            return None
        got: dict[str, Any] = {}

        def run(moved: dict) -> bool:
            try:
                # boundary: an automatic move, so the effect boundary applies (effect_boundary.py); a blocked call is
                # LocalUnavailable here and the cloud refusal stands
                got["turn"] = local_lane.complete(body, moved["local"]["node"], moved["local"]["model"],
                                                  boundary=True, bind_reason=moved.get("bind_reason"),
                                                  hook=self._boundary_hook(), conversation=str(session.id))
            except local_lane.LocalUnavailable as exc:
                logger.info("sensitive: refusal: local answer failed for %s: %s", rid, exc)
                return False
            return True

        try:
            moved = reg.refusal_route(body, route, run, why_out=why_out)
        except Exception as exc:                        # a fallback bug must never cost the client the cloud answer
            logger.exception("sensitive: refusal fallback failed for %s (%s); the refusal stands", rid,
                             type(exc).__name__)
            why_out.append("no_answer")
            return None
        if not moved or "turn" not in got:
            if not why_out:
                why_out.append("no_answer")
            return None
        if one_shot:
            session.route = moved                       # a throwaway backend: nothing to rebind; the record is local
            return session, got["turn"]
        try:
            return reg.rebind_local(session, turn, moved), got["turn"]
        except Exception as exc:
            logger.exception("sensitive: refusal: binding %s to the local lane failed (%s); the refusal stands", rid,
                             type(exc).__name__)
            why_out.append("no_answer")
            return None

    def _sensitive_cloud_notice(self, session, *, created: bool, stop: Any, refusal_why: list[str]) -> dict | None:
        """The sensitive-cloud notice of this turn (#124, sensitive_notice.py), or None. The turn that opened the
        conversation carries it (the turn whose routing kept the request off the Pro): a route routing.choose marked
        (sensitive_notice.ROUTE_KEY), or a refused opening turn whose refusal fallback was not taken (detector
        "refusal", the reason refusal_why names). A continuation stays where its conversation is, so it carries a
        notice only when the cloud model refused it (round 2: the refusal detector fired on this turn; reason
        refused_continuation, worded as "refused, not moved, the refusal stands"); its earlier mark is not repeated.
        Every such turn is logged and recorded on its route's row (detector, reason, notice shown or opted out:
        categories only, never text), except that a continuation never overwrites a record the row already holds (the
        first detection stays; the log line still names this one); the client's OPT_OUT_HEADER suppresses only the
        injected block. Bookkeeping never fails the turn."""
        route = getattr(session, "route", None)
        if not _routed_cloud(route):
            return None                                 # no S1 route (fixed model, pin, local lane): no detector, no row
        mark = route.get(sensitive_notice.ROUTE_KEY)
        write = True
        if not created:
            if stop != "refusal" or not refusal_why:
                return None
            write = not mark                            # the row keeps its first record
            if not mark:
                session.route = {**route, sensitive_notice.ROUTE_KEY: sensitive_notice.mark("refusal",
                                                                                            refusal_why[0])}
            mark = sensitive_notice.mark("refusal", refusal_why[0])
        elif not mark and stop == "refusal" and refusal_why:
            mark = sensitive_notice.mark("refusal", refusal_why[0])
            session.route = {**route, sensitive_notice.ROUTE_KEY: mark}
        if not mark:
            return None
        out = sensitive_notice.opted_out(self.headers)
        rec = sensitive_notice.record(mark, sensitive_notice.OPTED_OUT if out else sensitive_notice.SHOWN)
        rid = route.get("route_id")
        if rec["reason"] == sensitive_notice.REFUSED_CONTINUATION:
            logger.warning("sensitive-cloud: route %s: %s/%s refused a turn after the opening one; not moved to the Pro, "
                           "the refusal stands; notice %s%s", rid, route.get("provider"), route.get("model"),
                           rec["notice"], "" if write else " (route row keeps its earlier record)")
        else:
            logger.warning("sensitive-cloud: route %s marked by %s answered in the cloud by %s/%s: the Pro did not "
                           "take it (%s); notice %s", rid, rec["detector"], route.get("provider"), route.get("model"),
                           rec["reason"], rec["notice"])
        hook = self.server.registry.sensitive_cloud
        if write and hook is not None and rid:
            try:
                if hook(str(rid), rec) is False:
                    logger.warning("sensitive-cloud: route %s: the route row's record was not written", rid)
            except Exception as exc:                    # the reply is good; only its record failed
                logger.warning("sensitive-cloud: route %s: recording failed: %s", rid, type(exc).__name__)
        return None if out else sensitive_notice.block(rec["detector"], rec["reason"], route.get("model"))

    def _exclusion_refusal(self, model: str, label: str, advice: str) -> str | None:
        """Why x-cortados-exclude-model refuses a request that can only run on `model` (a lane that never reaches the
        router, which applies the header otherwise: the api/ pin, the search lane), or None. The same rules as choose:
        a malformed header, an item naming no model, or `model` itself excluded."""
        raw, valid = _exclude_model_header(self.headers)
        if not valid:
            return _EXCLUDE_MODEL_BAD
        pinned_key = routing.model_key(routing.normalize_model(model))
        for item in (raw.split(",") if raw else []):   # each listed model, the same rules as choose
            excluded = routing.normalize_model(item)
            if excluded == "":                    # `api`, `sub/`, `api/`: a prefix naming no model, as in choose
                return (f"cortados: x-cortados-exclude-model: {item} names no model this endpoint serves; the review "
                        "could not be kept off it")
            if excluded and routing.model_key(excluded) == pinned_key:
                return f"cortados: {label} is the excluded model (x-cortados-exclude-model); {advice}"
        return None

    def _web_search(self, body: dict[str, Any], turn, names: list[str], wire: Wire, floor: float | None = None) -> None:
        """A web-search-only request on the search lane (search_lane.py: a headless Claude CLI with only WebSearch,
        the one exception to bridge.py's no-tool rule). Its result text is the reply; any failure falls back to the
        explicit unavailable reply, naming the failure class, never to an empty success."""
        reg = self.server.registry
        # The request's policy headers bind this lane as they bind routed calls (the router never sees this request):
        # both checks run before launch, and nothing here spends quota until they pass.
        refused = self._exclusion_refusal(search_lane.SEARCH_MODEL, f"the search lane's model {search_lane.SEARCH_MODEL}",
                                          "this request cannot be searched under that exclusion")
        if refused:
            return self._error(400, "invalid_request_error", refused, wire)
        reserved: str | None = None                     # the floor reservation's id while it may still need a refund
        try:
            # Query checks and a search slot come first, then the quota floor's reservation: a refused search (no
            # query, too long, all slots busy) must never leave a reservation. The slot is held through the launch.
            query = search_lane.extract_query(body)
            search_lane.check_query(query)
            allowed, blocked = search_lane.domain_filters(body)   # parsed before reserving: nothing between it and launch
            with search_lane.capacity():
                who = search_lane.login()               # the login and its ledger lane, resolved together before launch
                if floor is not None:
                    # The client's quota floor on the account the search will execute on, checked and reserved through
                    # floor.reserve like a routed conversation's. No enforcer (a fixed-model endpoint) or one that fails:
                    # closed, as the routed paths do.
                    if reg.floor_search is None:
                        return self._error(429, "rate_limit_error", "cortados: this endpoint cannot check "
                                           "x-cortados-min-left-pct for the search lane", wire,
                                           reason="the client's quota floor cannot be checked here")
                    rid = uuid.uuid4().hex
                    try:
                        why = reg.floor_search(who.floor_candidate(), floor, rid)
                    except Exception as exc:
                        logger.exception("search lane: quota floor check failed")
                        return self._error(429, "rate_limit_error", "cortados: the search lane's quota floor check failed "
                                           f"({type(exc).__name__}); x-cortados-min-left-pct cannot be honoured", wire,
                                           reason="the client's quota floor cannot be checked here")
                    if why:
                        return self._error(429, "rate_limit_error", f"cortados: {why}", wire,
                                           reason="the search account is below the client's quota floor")
                    reserved = rid
                result = search_lane.run(getattr(reg, "search_bin", None) or reg.claude_bin,
                                         query, who.config_dir, allowed=allowed, blocked=blocked, slot_held=True)
        except search_lane.SearchFailed as exc:
            logger.warning("web search lane failed (%s): %s", exc.kind, anthropic_wire.log_text(exc.detail, 200))
            if exc.kind == "start_failed" and reserved:
                # The CLI never started, so nothing was spent: refund the reservation, once (#79). Every other failure
                # ran the CLI (it may have spent quota) and keeps its reservation. Only an OSError from launching the
                # CLI becomes start_failed (search_lane._run_cli); any other exception here is a lane bug, logged by
                # the generic handler below, and its reservation is not refunded: it ages out as before.
                self._refund_search_floor(reserved)
            reason = ("search busy" if exc.kind == "busy" else "no search was performed" if exc.kind == "no_search"
                      else f"the search lane failed: {exc.kind}")
            return self._server_tools_unavailable(turn, names, wire, reason=reason)
        except Exception:
            # Anything else is a bug in the lane: the client still gets a complete reply (never a dropped
            # connection), and the traceback goes to the log.
            logger.exception("web search lane failed unexpectedly")
            return self._server_tools_unavailable(turn, names, wire, reason="the search lane failed: internal")
        logger.info("web search answered on the search lane (%s, %d chars)", search_lane.SEARCH_MODEL,
                    len(result.text))
        # Accounting, not a quota gate: scarcity/quota decide whether a lane may spend; the ledger only teaches
        # unitcost tokens per %. So, like every other lane, the row is written after the fact (record_async) with no
        # pre-launch reservation; a lost row weakens one learning interval and is logged at error level with the lane
        # and run id. Only a completed search reaches here: every failure (incomplete_output included) returned above.
        try:
            from cortados_router import usage_ledger
            run_id, valid = self._run_id()
            usage_ledger.record_async(who.ledger_lane, search_lane.SEARCH_MODEL, result.usage,
                                      context=f"search lane, run {run_id if run_id and valid else '-'}")
        except Exception as exc:
            _log_ledger_hook_failure(exc)               # the ledger never breaks a reply (as _ledger_turn's caller)
        return self._reply_text(turn, result.text, result.usage, wire)

    def _refund_search_floor(self, rid: str) -> None:
        """Release one search reservation through reg.floor_release. Never raises: a refund that cannot be made leaves
        the row to age out (the behaviour before #79), and that is logged, not hidden."""
        release = self.server.registry.floor_release
        if release is None:
            logger.warning("search lane: start failed but no floor_release is wired; reservation %s ages out", rid)
            return
        try:
            if not release(rid):
                logger.warning("search lane: floor reservation %s was not found to refund", rid)
        except Exception:
            logger.exception("search lane: refunding floor reservation %s failed; it ages out", rid)

    def _server_tools_unavailable(self, turn, names: list[str], wire: Wire, reason: str = "") -> None:
        """A complete, ordinary assistant reply whose text tells the model the server tools did not run. A reply,
        not a 4xx: the client hands the text to the model as the tool's output, so the model reads why and stops
        retrying, where an HTTP error may be retried or surface only as a generic API error."""
        shown = sorted({anthropic_wire.shown_name(n) for n in names})     # never a raw client name (#117)
        text = anthropic_wire.SERVER_TOOL_UNAVAILABLE.format(names=", ".join(shown))
        if reason:
            text += f" (reason: {reason})"
        logger.warning("request with server tool(s) %s answered as unavailable%s", shown,
                       f" ({reason})" if reason else "")
        return self._reply_text(turn, text, {}, wire)

    def _reply_text(self, turn, text: str, usage: dict[str, int], wire: Wire) -> None:
        """One complete end_turn text reply, as JSON or SSE."""
        msg_id, blocks = wire.new_message_id(), [{"type": "text", "text": text}]
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
            logger.info("client gone while a server-tool reply was written (%s)", gone)

    def _messages(self, body: dict[str, Any], wire: Wire) -> None:
        """One client request on `wire`: find or start its session, run one turn, answer in the wire's shape."""
        reg = self.server.registry
        dump = getenv("CORTADOS_PROVIDER_DUMP_DIR")     # debugging aid: every request body as received
        if dump:
            Path(dump).mkdir(parents=True, exist_ok=True)
            (Path(dump) / f"{time.time():.3f}.json").write_text(json.dumps(body, indent=1))
        # The tool-schema budget (TOOL_SCHEMA_BUDGET_BYTES), first: before any lane, pin, lookup or backend, on both wires.
        over = tool_schema_overrun(body)
        if over:
            return self._error(400, "invalid_request_error", over, wire, reason=TOOL_BUDGET_REASON)
        try:
            # The api/ lane relays the body verbatim to the Anthropic Messages API, so only a Messages-wire body can be
            # pinned to it; on /v1/responses an api/ (or sub/) name falls through to parse_request, which refuses any
            # model but `cortados` with a 400 (Codex compares the echoed model to the one it asked for).
            api_model = self._api_pin(body) if wire is ANTHROPIC else None
            if api_model:
                # api/ requests are stateless, so each is a new conversation: honour x-cortados-exclude-model here
                # (the router, which applies it otherwise, is never called for this lane).
                refused = self._exclusion_refusal(api_model, f"api/{routing.shown_pin(api_model)}",
                                                "pin another model or use cortados")
                if refused:
                    return self._error(400, "invalid_request_error", refused, wire)
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
                if all(t.startswith("web_search_") for _, t in anthropic_wire.server_tool_kinds(body)):
                    return self._web_search(body, turn, server_tools, wire, floor)
                return self._server_tools_unavailable(turn, server_tools, wire)
            if server_tools:
                # Mixed with client tools: parse_request dropped the server tools, the client tools are served.
                logger.warning("request declares server tool(s) %s that this endpoint cannot run; served without them",
                               sorted({anthropic_wire.shown_name(n) for n in server_tools}))
            # A tool-less request (a client's title or summary call) never joins a live session: it would hold that
            # session's lock while the session waits for a tool result. It gets its own throwaway CLI instead.
            one_shot = not turn.tools
            identity = self._conversation_identity()
            fail_key = None if one_shot else reg.failure_key(turn, wire_path=self.path.split("?")[0],
                                                              client=self._client(), identity=identity)
            unkeyed = (None if identity or not turn.history else
                       "the client sent no conversation identity: none of " + ", ".join(CONVERSATION_ID_HEADERS))
            scope = reg.conversation_scope(wire_path=self.path.split("?")[0], client=self._client(), identity=identity)
            rebuild = None
            try:
                session = None if one_shot else reg.find(turn, fail_key, scope, self.path.split("?")[0],
                                                         client=self._client())
            except Rebuild as rb:                       # served below, once the request is known to be well-formed
                session, rebuild = None, rb
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
                added, dropped = sorted(names - session.tool_names), sorted(session.tool_names - names)
                # The usual cause of an ADDED tool is a Claude Code client with tool search on (#117): it declares an
                # MCP tool only once ToolSearch has loaded it. The backend cannot call it ("No such tool available"),
                # so this says so plainly, and the model is told once (TOOLS_ADDED_NOTE) so it can tell the user
                # instead of retrying. Growing the backend's tool set is #121; launch bounded clients with tool search
                # off (session.TOOL_SEARCH_OFF).
                logger.warning("conversation %s: the tool set changed mid-conversation (added %s, dropped %s); the "
                               "running backend keeps the tools it started with, so an added tool CANNOT be called in "
                               "this conversation (calls fail as \"No such tool available\"; the model is told). "
                               "Usual cause: a Claude Code client with tool search on; start it with "
                               "ENABLE_TOOL_SEARCH=false or a new conversation (#117, #121)", session.id[:8],
                               [anthropic_wire.shown_name(n) for n in added],
                               [anthropic_wire.shown_name(n) for n in dropped])
                if added:
                    session.tools_note = TOOLS_ADDED_NOTE
            if one_shot:
                # The transcript re-reads the whole history, so a shape error in an older user item surfaces here,
                # not in parse_request: it is a request-shape error all the same and answers 400.
                turn.content, turn.tool_results = wire.one_shot_content(body), {}
        except wire.WireError as exc:                   # request-derived text: the client sees it, the record does not
            return self._error(400, "invalid_request_error", str(exc), wire,
                               reason="the request does not parse on this wire")
        except FailedSession as exc:
            logger.info("request on a failed conversation refused: %s", exc)
            return self._terminal(turn, wire, str(exc), reason=_failed_reason(exc))
        except Refused as exc:                          # terminal: the retry would carry the same history
            return self._terminal(turn, wire, str(exc), reason=REFUSED_REASONS.get(exc.code, "refused (terminal)"))
        except ConversationBusy as exc:                 # a rebuild in this scope is starting: the retry sees it done
            return self._error(BUSY_STATUS, "overloaded_error", str(exc), wire)
        except Replay as rp:
            return self._serve_replay(rp, turn, body, wire)
        except LostSession as exc:
            return self._error(409, "invalid_request_error", str(exc), wire)
        if rebuild is not None:
            session = self._rebuild(rebuild, turn, body, wire, scope)
            if session is None:
                return                                  # refused; the answer is sent
        changed = self._model_change(session, body) if session is not None else None
        if changed:
            return self._terminal(turn, wire, changed, reason=MODEL_CHANGE_REASON)
        created = session is None                       # this request opens the conversation (or is a one-shot)
        if session is None:
            session = self._open_conversation(turn, body, wire, one_shot=one_shot, floor=floor, scope=scope)
            if session is None:
                return                                  # refused; the answer is sent
        if isinstance(session, ApiSession):             # the API credit lane: relayed to the API, never a CLI turn
            if not created and self._api_continuation_refused(session, body, wire):
                return
            self._relay_api(session, turn, body, created=created, one_shot=one_shot, wire=wire)
            return
        if isinstance(session, LocalSession):           # a local lane: relayed to its node, never a CLI turn
            if not created and self._local_continuation_refused(session, body, wire):
                return
            outcome = self._relay_local(session, turn, body, created=created, one_shot=one_shot, wire=wire)
            if outcome not in local_lane.REROUTE:
                return
            # The node's gate can never take this opening turn (TOO_LARGE: more memory than its whole budget;
            # UNSUPPORTED: an image or document its text-only model cannot read), or the node answered a 5xx on a
            # COST-profile bind of a request no sensitivity detector marked (NODE_ERROR, #122; a sensitive request or
            # bind, or any doubt, never gets here: local_lane.node_error_reroutes, local_lane.CLOUD_REROUTE_OK):
            # nothing was sent and the conversation was dropped, so it is routed again with that local model excluded
            # (S1's route, or another node), once. A second refusal is answered by the lane itself (reroute off).
            moved = (session.route or {}).get("local") or {}
            logger.log(logging.WARNING if outcome == local_lane.NODE_ERROR else logging.INFO,
                       "route %s: local node %s %s; routing it elsewhere",
                       (session.route or {}).get("route_id"), (moved.get("node") or {}).get("node"),
                       "cannot hold the opening turn" if outcome == local_lane.TOO_LARGE else
                       "cannot read the opening turn's content (text-only model)" if outcome == local_lane.UNSUPPORTED
                       else "answered the opening turn with an HTTP 5xx before any byte reached the client")
            bound = session.route or {}
            session = self._open_conversation(turn, body, wire, one_shot=one_shot, floor=floor, scope=scope,
                                              also_exclude=str((moved.get("model") or {}).get("id") or ""))
            if session is None:
                return
            if isinstance(session, ApiSession):
                self._relay_api(session, turn, body, created=True, one_shot=one_shot, wire=wire)
                return
            if not isinstance(session, LocalSession):
                # #124: a request a detector marked now leaves the machine because the node could not take it. The
                # reason is the gate's outcome (the re-route's own skip only says the model was excluded); the
                # detector is the new route's, else the one the local bind recorded.
                det = ((session.route or {}).get(sensitive_notice.ROUTE_KEY) or {}).get("detector") or \
                    bound.get(sensitive_notice.DETECTOR_KEY)
                if det:
                    session.route = {**(session.route or {}),
                                     sensitive_notice.ROUTE_KEY: sensitive_notice.mark(det, outcome)}
            if isinstance(session, LocalSession):
                self._relay_local(session, turn, body, created=True, one_shot=one_shot, wire=wire, reroute=False)
                return
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
                route_now = getattr(session, "route", None)
                if route_now and getattr(session, "codex_login", None):
                    route_now = {**route_now, "codex_home": session.codex_login}   # the running backend's frozen home
                why = (reg.floor_turn(route_now, eff) if reg.floor_turn is not None else
                       "this endpoint cannot check x-cortados-min-left-pct on a continuation")
                if why:
                    return self._error(429, "rate_limit_error", f"cortados: {why}", wire,
                                       reason="the conversation's account is below the client's quota floor")
        try:
            # The run (x-cortados-run-id, validated in do_POST) this conversation serves: a running child task's
            # conversation gets the tool-wait idle limit (Registry.reap, #132 item 6 (b)). Bookkeeping only.
            run_id, valid = self._run_id()
            if run_id and valid and not one_shot and getattr(session, "run_id", None) != run_id:
                session.run_id, session.run_since = run_id, time.time()   # bounded by CHILD_RUN_MAX_S from here
        except Exception:
            logger.debug("run id not kept on the conversation", exc_info=True)
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
        office_key = self._office_turn_start(session, one_shot=one_shot)

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
                seed = getattr(session, "rebuild_seed", None)
                over = replay_overflow(session, seed, turn) if seed is not None else None
                if over:
                    # Review P2-c: the request that passed the rebuild's fit check is not necessarily the one that
                    # takes this first turn (any request with this exact history can), so the seed + THIS turn is
                    # checked again here, under the turn lock. Refused terminally; the seed stays for a turn that fits.
                    outcome["terminal"] = replay_overflow_message(*over)
                    logger.warning("conversation %s: first turn after a rebuild ≈%dk tokens exceeds its route's "
                                   "context %dk; terminal", session.id[:8], over[0] // 1000, over[1] // 1000)
                    return
                if seed is not None:
                    # A rebuilt backend's first turn (Registry.rebuild): the client's history as a transcript, then
                    # this turn, tool results as text. Cleared once sent: the backend holds the history from now on.
                    session.send_user(anthropic_wire.rebuild_content(seed, turn), timeout=deadline - time.time())
                    session.rebuild_seed = None
                elif turn.tool_results:
                    session.deliver(with_tools_note(turn.tool_results, getattr(session, "tools_note", None)),
                                    timeout=deadline - time.time())
                else:
                    note = getattr(session, "tools_note", None)
                    session.send_user(turn.content + ([{"type": "text", "text": note}] if note else []),
                                      timeout=deadline - time.time())
                session.tools_note = None               # said once; a rebuilt backend starts with the current tools
                _turn_t0 = time.time()
                result = session.run_turn(deadline - time.time())
                cur = session                           # the session this turn's answer belongs to
                refusal_why: list[str] = []
                moved = self._refusal_fallback(session, turn, body, result, created=created, one_shot=one_shot,
                                               wire=wire, why_out=refusal_why)
                if not moved:
                    # #124: a request a detector marked, answered in the cloud: the notice leads the reply.
                    outcome["notice"] = self._sensitive_cloud_notice(session, created=created, stop=result[1],
                                                                     refusal_why=refusal_why)
                if moved:
                    try:
                        _ledger_turn(session, result[2])   # the refused cloud turn was spent all the same
                    except Exception as exc:
                        _log_ledger_hook_failure(exc)
                    self._log_turn_tokens(session, result[2])   # and its tokens are on the cloud route's row
                    cur, result = moved
                    outcome["moved"] = cur
                outcome["turn"] = result
                _turn_ms = int((time.time() - _turn_t0) * 1000)
                shown_blocks = sensitive_notice.prepend(outcome["turn"][0], outcome.get("notice"))
                _remember_reply(cur, turn, msg_id, model, (shown_blocks, *outcome["turn"][1:]))
                # Compaction content store (shadow, off unless CORTADOS_COMPACTION_STORE=1): the one point where the
                # turn's new user content / tool results and the backend's reply are both known. It only reads them
                # and enqueues for the store's background writer; it never raises or blocks (failures are counted).
                self._compaction_record(cur, turn, outcome["turn"][0], wire)
                if not moved:                           # a local answer spends no subscription: nothing to learn
                    self._log_turn_tokens(session, outcome["turn"][2])
                    try:
                        _ledger_turn(session, outcome["turn"][2])
                    except Exception as exc:
                        # The ledger only feeds unit-cost learning: it must never break a turn. Without this guard the
                        # reply would go out while history fell behind the client, killing the conversation on its next
                        # continuation.
                        _log_ledger_hook_failure(exc)
                # #124: history holds the reply exactly as delivered (notice included); a client that echoes it matches
                # exactly; any other form of it is a rewritten history like any other (rebuild, 409 or terminal).
                cur.history.append(anthropic_wire.reply_fingerprint(shown_blocks))
                if cur.route and not cur.route_logged and reg.executed:
                    try:
                        reg.executed(cur.route)
                        cur.route_logged = True         # only after the write succeeded; a failure retries next turn
                    except Exception:
                        # The reply is good; only its training record failed. Answer the client and say so loudly.
                        logger.exception("execution log for route %s failed%s", cur.route.get("route_id"),
                                         " (one-shot: no later turn to retry)" if one_shot else "; retrying next turn")
                    else:
                        if reg.log_outcome:             # the turn's size and duration, for training; once per conversation, same row as the route
                            blocks = outcome["turn"][0]
                            chars = sum(len(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                            # off the critical path: a slow SQLite write must not hold the session lock or delay the reply.
                            # Best effort — a failed outcome log is logged, not retried (the request label is already saved).
                            route = cur.route
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
                if not worker.is_alive() and outcome.get("moved") is not None:
                    # The refusal fallback bound the conversation to a local session: the reply, the route header and
                    # the bookkeeping below belong to it now.
                    session = outcome["moved"]
                    routed = session.route
                    if revealed:
                        # A revealing client (route_task's trailer) must be told the model that answered. A streamed
                        # reply named the refused model in its message_start already; the JSON reply is corrected.
                        model = self._reveal(session) or model
                    self._record_run(provider=str(routed.get("provider") or ""), model=str(routed.get("model") or ""),
                                     label=str(routed.get("model") or ""), tools=not one_shot)
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
            if "terminal" in outcome:
                self._note_refusal(TERMINAL_STATUS, TERMINAL_ERROR, reason=REBUILD_TOO_LARGE_REASON)
                if client_gone:
                    return
                if turn.stream:                         # the 200 is sent: the error event carries the terminal kind
                    self.wfile.write(wire.stream_error(msg_id, model, outcome["terminal"], terminal=True))
                    return
                return self._json(TERMINAL_STATUS, wire.error_json(TERMINAL_ERROR, outcome["terminal"]),
                                  headers={"x-should-retry": "false"})
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
            blocks = sensitive_notice.prepend(blocks, outcome.get("notice"))   # its own leading block (#124)
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
            self._office_turn_end(session, office_key, outcome)
            if one_shot and not outcome.get("closed"):
                session.close()                         # the worker never got to it (it did not run): close it here

    def _terminal(self, turn, wire: Wire, message: str, *, reason: str) -> None:
        """A refusal no retry can fix, in each wire's non-retried shape (see TERMINAL_ERROR): HTTP 400
        invalid_request_error with `x-should-retry: false`, or for a streaming request an SSE error marked terminal
        (the Responses wire's TERMINAL_CODE). `message` is this endpoint's own text; `reason` the fixed record."""
        self._note_refusal(TERMINAL_STATUS, TERMINAL_ERROR, reason=reason)
        if turn.stream:                                 # an SSE error, as a streaming client gets any turn's failure
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
                self.wfile.write(wire.stream_error(msg_id, turn.model, message, terminal=True))
                self.wfile.flush()
            except OSError as gone:
                logger.info("client gone before the terminal error was sent (%s)", gone)
            return
        self._json(TERMINAL_STATUS, wire.error_json(TERMINAL_ERROR, message), headers={"x-should-retry": "false"})

    def _serve_replay(self, rp: "Replay", turn, body: dict[str, Any], wire: Wire) -> None:
        """Answer a retry of an answered turn with the stored reply (Registry.find, or rebuild()'s identical twin).
        A retry that also changes the sub/ pin gets the same refusal as any continuation that does: the stored
        answer came from the conversation's own model, which the request no longer asks for."""
        changed = self._model_change(rp.session, body)
        if changed:
            return self._terminal(turn, wire, changed, reason=MODEL_CHANGE_REASON)
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

    def _rebuild(self, rb: "Rebuild", turn, body: dict[str, Any], wire: Wire, scope: str | None):
        """Serve a continuation whose client rewrote the history on a rebuilt backend (Registry.rebuild); the new
        session, or None once a refusal has been answered. The conversation's model is fixed (a changed sub/ pin is
        refused exactly as on any continuation); a refusal to rebuild names its reason."""
        reg = self.server.registry
        changed = self._model_change(rb.session, body)
        if changed:
            self._terminal(turn, wire, changed, reason=MODEL_CHANGE_REASON)
            return None
        try:
            transcript = anthropic_wire.rebuild_transcript(wire.history_messages(body))
        except (wire.WireError, RecursionError) as exc:
            self._error(400, "invalid_request_error", f"cortados: the history cannot be replayed: {exc}", wire,
                        reason="the request does not parse on this wire")
            return None
        # Review P2-5: the per-block cut does not bound the whole replay. The rebuilt backend's first turn must fit the
        # conversation's frozen route (replay_overflow), refused BEFORE the old session is closed or replaced.
        # Terminal: a retry replays the same history. Checked again under the new session's turn lock (work()), for
        # whichever request actually sends the seed (review P2-c).
        over = replay_overflow(rb.session, transcript, turn)
        if over:
            logger.warning("rebuild refused: conversation %s replay ≈%dk tokens exceeds its route's context %dk; "
                           "terminal", str(rb.session.id)[:8], over[0] // 1000, over[1] // 1000)
            self._terminal(turn, wire, replay_overflow_message(*over), reason=REBUILD_TOO_LARGE_REASON)
            return None
        try:
            return reg.rebuild(rb.session, turn, superseded=rb.superseded, transcript=transcript, scope=scope,
                               wire_path=self.path.split("?")[0], client=self._client())
        except Refused as exc:                          # the limit, committed by a concurrent rebuild
            self._terminal(turn, wire, str(exc), reason=REFUSED_REASONS.get(exc.code, REBUILD_LIMIT_REASON))
        except Replay as rp:                            # an identical request rebuilt it and was answered first
            self._serve_replay(rp, turn, body, wire)
        except LostSession as exc:                      # ...and its answer was not kept: a stale continuation
            self._error(409, "invalid_request_error", str(exc), wire)
        except ConversationBusy as exc:
            self._error(BUSY_STATUS, "overloaded_error", str(exc), wire)
        except UnservedProvider as exc:
            self._error(503, "overloaded_error", f"cortados: {exc}", wire)
        except BridgeError as exc:                      # the rebuilt backend refused to start; the old one is untouched
            logger.warning("rebuilt backend start failed: %s", exc)
            self._error(502, "api_error", str(exc), wire, reason=failure_diagnostic(exc))
        return None

    # ------------------------------------------------------------------ office bookkeeping (provider/office.py)
    def _office_turn_start(self, session, *, one_shot: bool) -> str | None:
        """Mark this request's worker `working` on the Office board; returns its key (or None: nothing to show). A
        tool-less side call (a title or summary request) of a worker or main loop already on the board changes
        nothing, so it cannot flip a working agent to idle. Bookkeeping only: never raises into a turn."""
        try:
            board = self.server.registry.office
            tags = getattr(self, "_office_tags", None) or {}
            origin = getattr(self, "_office_origin", None)
            # the conversation's provider-observed origin, before any early return (a side call carries it too)
            session.office_origin = office.note_origin(getattr(session, "office_origin", None), origin)
            run_id, valid = self._run_id()
            run_id = run_id if valid else None
            # untagged, no run id: named by a digest of the conversation's id (the id itself is never published)
            conv = (None if (run_id or tags.get("session"))
                    else "c-" + hashlib.sha256(str(session.id).encode()).hexdigest()[:12])
            key = board.worker_key(tags, run_id, conv)
            if key is None or (one_shot and (key.startswith("main:") or board.known(key))):
                return None
            route = getattr(session, "route", None) or {}
            rp = str(route.get("provider") or "claude-cli")       # no route: a fixed-model endpoint, the Claude CLI
            lane = rp if rp.startswith(local_lane.LANE_PREFIX) else (_route_lane(route) if route else None)
            backend = (local_lane.node_backend(rp[len(local_lane.LANE_PREFIX):])
                       if rp.startswith(local_lane.LANE_PREFIX) else None)
            board.turn_start(key, tags, run_id=run_id or conv, provider=office.provider_of(rp, local_lane.LANE_PREFIX),
                             lane=lane, model=route.get("model") or getattr(session, "model", None), origin=origin,
                             backend=backend)
            # Kept on the conversation so an unexpected backend exit (Registry._backend_exited), with no request left
            # to end the turn, can still end this worker on the board. A failure here is reported by the warning below.
            session.office_worker = (key, dict(tags))
            return key
        except Exception as exc:
            logger.warning("office: turn start bookkeeping failed: %s", type(exc).__name__)
            return None

    def _office_turn_end(self, session, key: str | None, outcome: dict[str, Any]) -> None:
        """The turn's end on the board: `working` with the tool it handed the client, `idle` after an answer, `error`
        after a backend failure. Only tool NAMES are read from the reply, never their input or any text. Skipped once
        the conversation's backend has exited (Registry.office_turn_end)."""
        if not key:
            return
        try:
            tags = getattr(self, "_office_tags", None) or {}
            reg = self.server.registry
            if "turn" in outcome:
                blocks = outcome["turn"][0] or []
                tools = [b.get("name") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
                if tools:
                    reg.office_turn_end(session, key, tags, status="working", tool=tools[-1])
                else:
                    reg.office_turn_end(session, key, tags, status="idle")
            elif "error" in outcome:
                reg.office_turn_end(session, key, tags, status="error")
            # busy / stale: another request on the conversation holds it and is still working; nothing changes
        except Exception as exc:
            logger.warning("office: turn end bookkeeping failed: %s", type(exc).__name__)

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

    def _compaction_record(self, session, turn, blocks, w: Wire) -> None:
        """Hand the turn to the compaction store (shadow). Only enqueues for its background writer; every failure is
        contained here, so turn finalization (history, ledger, route log) always runs after it."""
        try:
            if not compaction_store.enabled():
                return
            rid, rid_ok = self._run_id()
            meta = {"run_id": rid if rid_ok else None,
                    "session": (self.headers.get("x-cortados-session") or "").strip()[:200] or None,
                    "client": self._client()}
            compaction_store.STORE.record_turn(
                str(session.id), source="codex" if w is RESPONSES else "claude-code",
                user_content=turn.content, tool_results=turn.tool_results, assistant_blocks=blocks, meta=meta)
        except Exception as exc:   # never interrupts the turn; counted like any store scheduling failure
            compaction_store.STORE._count("schedule_failures",
                                          f"turn not handed to the store: {type(exc).__name__}: {exc}")

    def _log_turn_tokens(self, session, usage) -> None:
        """Add this backend turn's tokens to its route's router.db row with the orchestrator's session tag (work split:
        provider/work_split.py). Off the critical path like the outcome log: a slow SQLite write must not hold the
        session lock or delay the reply. Never raises; core.log_tokens counts a lost write in router_db."""
        reg = self.server.registry
        if isinstance(usage, dict):
            # the live view's in-memory totals (orchestrator tree): per conversation and per run id, also when router.db
            # does not record tokens (fixed-model endpoint, local lane). Bookkeeping: never fails a turn.
            try:
                t = work_split.usage_tokens(usage)
                acc = getattr(session, "live_tokens", None)
                if not isinstance(acc, dict):
                    acc = {}
                    session.live_tokens = acc
                work_split.add_tokens(acc, t)
                run_id, valid = self._run_id()
                if run_id and valid:
                    reg.runs.add_tokens(run_id, t, origin=getattr(self, "_office_origin", None))
            except Exception:
                logger.debug("live token total not recorded", exc_info=True)
        route = getattr(session, "route", None)
        if not reg.log_tokens or not isinstance(route, dict) or not route.get("route_id") or not isinstance(usage, dict):
            return
        tag = office.clean(self.headers.get(office.SESSION_HEADER), office._HEADER_RULES[office.SESSION_HEADER])
        # The per-session work split's provider-observed keys (#127): a child's orchestrator (x-cortados-origin, as
        # read and validated for this request), its run id (valid only) and the front door (route_task).
        keys: dict[str, str | None] = {"office_origin": getattr(self, "_office_origin", None) or None,
                                       "office_run": None, "office_via": office.read_via(self.headers)}
        try:
            run_id, valid = self._run_id()
            keys["office_run"] = run_id if run_id and valid else None
        except Exception:
            pass

        def _write(route_id=route["route_id"], usage=dict(usage), tag=tag, keys=keys):
            try:
                reg.log_tokens(route_id, usage, tag, **keys)
            except Exception:
                logger.exception("token log for route %s failed", route_id)
        threading.Thread(target=_write, name="cortados-token-log", daemon=True).start()

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
        # As typed first; a friendly spelling (`sub/gpt-6.1`) is resolved only when that misses (_resolved_pin).
        if local:                       # a local pin answers to its alias or its tag, case-insensitively
            names = {local["model"]["id"].casefold(), local_lane.pin_id(local["model"]).casefold()}
            if r.get("pinned") and (pinned.strip().casefold() in names
                                    or Handler._resolved_pin(pinned).casefold() in names):
                return None
            shown = local_lane.pin_id(local["model"])
        else:
            if r.get("pinned") and (r.get("model") == pinned or r.get("model") == Handler._resolved_pin(pinned)):
                return None
            shown = r.get("model")
        now = f"pinned to sub/{shown}" if r.get("pinned") else f"routed by S1 to {r.get('model') or 'its model'}"
        return (f"cortados: the model is fixed per conversation: this one is {now}, and this request asks for "
                f"sub/{routing.shown_pin(pinned)}; start a new conversation to change models")

    @staticmethod
    def _resolved_pin(pinned: str) -> str:
        """The pin name a continuation's `sub/<name>` means, through the same resolver the opener went through
        (routing.resolve_pin), so a friendly pin (`sub/gpt-6.1`) continues on the model it opened on. Only reached when
        the name did not match as typed. An ambiguous or unknown name resolves to itself (and so is refused)."""
        from cortados_router import core
        try:
            return routing.resolve_pin(core, pinned) or pinned
        except routing.AmbiguousPin:
            return pinned

    def _reveal(self, session) -> str | None:   # api/-pinned requests reveal in _serve_api_lane (api_reveal_suffix)
        try:
            return reveal_model(getattr(session, "route", None))
        except Exception:                               # reveal_model already never raises; belt and braces
            return None

    def _route_header(self, session) -> None:
        """The x-cortados-route header of a routed session. Cosmetic metadata: a partial route (one missing provider
        or model, #124 round 3) sends no header, and the header can never fail the reply it rides on."""
        r = getattr(session, "route", None)
        if not isinstance(r, dict) or not r.get("provider") or not r.get("model"):
            return
        try:
            self.send_header("x-cortados-route", f"{r['provider']}/{r['model']} band={r.get('band', '?')} "
                                                 f"id={r.get('route_id', '?')}")
        except Exception:                               # a header value send_header refuses: the reply goes without it
            logger.debug("x-cortados-route header not sent", exc_info=True)


class ProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, registry: Registry):
        super().__init__(addr, Handler)
        try:
            self.registry = registry
            self.login_slots = threading.BoundedSemaphore(LOGIN_MAX_CONCURRENT)   # POST /dashboard/login (_dashboard_login)
            self._live_memo = live.Memo()                # GET /v1/cortados/live (live_body)
            # The usage ledger's writer starts with the endpoint, never from a request (record_async only queues).
            from cortados_router import usage_ledger
            if not usage_ledger.start():
                raise RuntimeError("usage ledger: the ledger writer is shut down; provider startup aborted")
        except BaseException:
            # The superclass already bound/listened. Release the port even if a caller retains our traceback.
            self.server_close()
            raise

    def office_view(self, registry: Registry) -> dict[str, Any]:
        """GET /v1/cortados/office (provider/office.py): the board plus the child-task log. Tests replace it."""
        from cortados_router import paths
        now = time.time()
        errors: list[dict[str, str]] = []
        try:
            events = office.read_child_events(paths.state_dir() / office.CHILD_EVENT_LOG, now)
        except OSError as exc:                          # said in the view, never hidden as "no child tasks"
            logger.warning("office: child-task log unreadable: %s", type(exc).__name__)
            errors.append({"source": "child_tasks", "error": type(exc).__name__})
            events = []
        return registry.office.view(events, now=now, errors=errors)

    def local_view(self) -> dict[str, Any]:
        """GET /v1/cortados/local (provider/local_switch.py): each local node's switch, asked through its gate. Tests
        replace it, or point CORTADOS_LOCAL_RESOURCES_CONFIG at a fake gate."""
        return local_switch.view()

    def local_switch(self, node: Any, on: Any) -> dict[str, Any]:
        """POST /v1/cortados/local/switch: set a node's switch through its gate (local_switch.switch). Tests replace it."""
        return local_switch.switch(node, on)

    def live_body(self, registry: Registry, session: str | None = None, session_view: bool = False) -> bytes:
        """The encoded live view, memoised live.MEMO_S with single flight (live.Memo): every dashboard tab and pulse
        pane polling at once costs one collect per view (the fleet, each ?session=). A heartbeat post (the board's
        generation) invalidates it. `session` must already be valid (live.valid_session)."""
        from cortados_router import paths
        key = (str(paths.state_dir()), id(registry), registry.live.generation, session, session_view)
        if session_view:                                # GET /v1/cortados/live/session: that session's subtree only
            return self._live_memo.get(key, lambda: json.dumps(
                live.session_view(self.live_view(registry, session, session_view=True), session),
                allow_nan=False).encode())
        return self._live_memo.get(key, lambda: json.dumps(self.live_view(registry, session), allow_nan=False).encode())

    def live_view(self, registry: Registry, session: str | None = None, session_view: bool = False) -> dict[str, Any]:
        """GET /v1/cortados/live (provider/live.py): composes the office board, the tasks and the live slice of the usage
        view, the child-task log (read once, 24 h) and the host heartbeats. Cache-only: no vendor CLI, no ssh. Tests
        replace the inputs it calls (office_view's board, tasks_view, live_usage_view) or this method. `session_view`
        (GET /v1/cortados/live/session): no other session's task directory is peeked, and the API credit section is
        left out (it is the fleet's, not the session's)."""
        from cortados_router import api_credits, child_tasks, exec_hosts, paths
        now = time.time()
        state = paths.state_dir()

        def office_of(events):
            return registry.office.view(events, now=now, errors=[], uncapped=True)

        def hosts():
            if not os.path.exists(exec_hosts.config_path()):
                return {}, None                         # no exec-hosts.json on this machine: no aliases, not an error
            h = exec_hosts.load()                       # a broken file is the hosts section's error (HostError)
            return {n: list(x.hostnames) for n, x in h.hosts.items()}, h.default

        return live.collect(
            now=now, child_events=lambda: office.read_child_events(state / office.CHILD_EVENT_LOG, now,
                                                                   window_s=live.CHILD_WINDOW_S),
            office_view=office_of, tasks_view=lambda: self.tasks_view(registry),
            usage_view=lambda: self.live_usage_view(registry, now),
            heartbeats=lambda: registry.live.snapshot(now), backend_sessions=len(registry.sessions),
            peek=child_tasks.peek_local, exec_hosts=hosts, self_host=exec_hosts.short_hostname(),
            # the session endpoint never lists the task dirs: its stubs come from the origin index (remote_stubs_for)
            remote_stubs=None if session_view else child_tasks.remote_stubs,
            session_stubs=child_tasks.remote_stubs_for, lane_enabled=live.lane_enabled,
            conversations=registry.live_conversations, ended_conversations=registry.ended_conversations,
            run_tokens=registry.runs.tokens_by_run, run_origins=registry.runs.run_origins,
            stubs_by_id=child_tasks.remote_stubs_by_id, binding_errors=registry.live.errors,
            api_credit=None if session_view else (lambda: api_credits.state(now)),
            session=session, peek_foreign=not session_view)

    def live_usage_view(self, registry: Registry, now: float) -> dict[str, Any]:
        """Lanes, S1 and hardware for the live view (usage.collect_live): the same sources usage_view reads."""
        from cortados_router import paths, scarcity
        from cortados_router.provider import usage
        try:
            from cortados_router.s1_endpoint import resolve_url
            s1_url = resolve_url()
        except Exception as exc:
            logger.warning("live: cannot resolve the S1 URL: %s", exc)
            s1_url = None
        state = paths.state_dir()
        return usage.collect_live(state_dir=state, scarcity_reader=lambda: usage.read_scarcity_cache(state / "scarcity.json"),
                                  s1_url=s1_url, freshness=scarcity.reading_freshness,
                                  claude_lanes=scarcity.claude_lanes(), now=now)

    def tasks_view(self, registry: Registry) -> dict[str, Any]:
        """GET /v1/cortados/tasks: read-only recorded decisions; no registry/live routing lock."""
        from cortados_router import paths
        from cortados_router.provider import tasks
        return tasks.collect(paths.state_dir())

    def usage_view(self, registry: Registry) -> dict[str, Any] | bytes:
        """GET /v1/cortados/usage (provider/usage.py): cache-only reads plus one short S1 health call; hardware from the
        live heartbeat board (registry.live), the same store the live view's hosts section reads. Tests replace it."""
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
                             heartbeats=registry.live.snapshot, encoded=True)


def serve(host: str, port: int, token: str, *, claude_bin: str, codex_bin: str, codex_home: str | None,
          fixed_model: str | None) -> None:
    # #117 round 5: every handler formats through safe_text.SafeFormatter, so no log record (http.server's request
    # lines via log_message included) can carry a raw newline or control character, whatever a call site passes.
    install_log_sanitizer()
    router = executed = log_turn_outcome = None
    if not fixed_model:
        from cortados_router import core                  # loads the tables and the live scarcity reader
        from cortados_router.provider import routing

        def router(body, client, exclude_model=None, min_left_pct=None, local=None):
            return routing.choose(core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin=claude_bin,
                                  codex_bin=codex_bin, exclude_model=exclude_model, min_left_pct=min_left_pct,
                                  codex_home=codex_home, local=local)

        def executed(route):
            core.log_execution(route["route_id"], route["provider"], route["model"])

        def log_turn_outcome(route, response_chars, turn_ms):
            core.log_outcome(route["route_id"], response_chars, turn_ms)

    registry = Registry(gateway_url=f"http://{host}:{port}", token=token, claude_bin=claude_bin, codex_bin=codex_bin,
                        codex_home=codex_home, model=fixed_model, router=router, executed=executed,
                        log_outcome=log_turn_outcome)
    if not fixed_model:
        registry.floor_turn = lambda route, floor: routing.floor_turn(core, route, floor, codex_home)
        registry.floor_search = lambda cand, floor, rid: _floor_search(core, cand, floor, rid)
        registry.floor_release = lambda rid: _floor_release(core, rid)
        registry.refusal_route = lambda body, route, run, why_out=None: routing.refusal_route(core, body, route, run,
                                                                                               why_out)
        registry.local_recheck = lambda body, route: routing.continuation_admission(core, body, route)
        registry.log_tokens = core.log_tokens
        registry.sensitive_cloud = core.log_sensitive_cloud
    httpd = ProviderServer((host, port), registry)

    def reaper():
        while True:
            time.sleep(60)
            registry.reap(IDLE_S)

    threading.Thread(target=reaper, daemon=True, name="cortados-provider-reaper").start()
    compaction_store.STORE.maybe_prune_async()          # retention at start; record_turn re-runs it at most hourly
    logger.info("cortados provider on http://%s:%d (model %s, %s)", host, port, wire.MODEL_ID,
                f"fixed to claude CLI {fixed_model}, no routing" if fixed_model else "routed by S1")
    try:
        httpd.serve_forever()
    finally:
        for s in list(registry.sessions.values()):
            registry.drop(s)                                # each drop enqueues that conversation's shadow decisions
        compaction_store.STORE.shutdown()                   # then drain them (bounded) and count what did not run


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
