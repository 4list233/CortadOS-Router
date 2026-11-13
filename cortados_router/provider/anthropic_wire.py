"""The Anthropic Messages wire: reading a client's request and writing the response, plain JSON or SSE.

Owner of one rule: the client's message shapes are translated here and nowhere else. The bridge only sees
`ClientTurn` (what is new in this request) and hands back content blocks in Anthropic form.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

MODEL_ID = "cortados"


# Anthropic server tools: the Messages API itself executes them (web search, web fetch, code execution) and answers with
# server_tool_use / *_tool_result blocks. This endpoint's backends are a headless CLI or Codex serving only the client's
# own tools, so nothing here can execute one. Before this list existed, a `web_search_20250305` tool (it has a `name`)
# became a fake bridge tool with an empty schema, and Claude Code's WebSearch got an empty success with no links
# (incident 2026-11-03). Matched by type prefix because the API versions them with a date suffix. Client-executed
# Anthropic-defined tools (bash_*, text_editor_*, computer_*) are not listed: the client runs those.
SERVER_TOOL_PREFIXES = ("web_search_", "web_fetch_", "code_execution_")
SERVER_TOOL_UNAVAILABLE = ("cortados: {names} is unavailable on this CortadOS lane: the routed backend cannot run "
                           "Anthropic's server-side tools, so no search was performed and there are no results. Use "
                           "WebFetch on a known URL, or report what you have without searching.")


def is_server_tool(tool: Any) -> bool:
    return isinstance(tool, dict) and str(tool.get("type") or "").startswith(SERVER_TOOL_PREFIXES)


def server_tool_names(body: dict[str, Any]) -> list[str]:
    """Names of the server tools the request declares (empty if none, or if `tools` is malformed: parse_request
    reports that)."""
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return []
    return [str(t.get("name") or t.get("type")) for t in tools if is_server_tool(t)]


def server_tool_kinds(body: dict[str, Any]) -> list[tuple[str, str]]:
    """(name, type) of each server tool the request declares; the search lane serves only web_search_* ones."""
    tools = body.get("tools") if isinstance(body, dict) else None
    return [(str(t.get("name") or ""), str(t.get("type") or "")) for t in (tools if isinstance(tools, list) else [])
            if is_server_tool(t)]


def _forced_server_tool_choice(tools: list[Any], choice: dict[str, Any]) -> bool:
    """True when a non-auto tool_choice forces only server tools, so the request can reach the server's unavailable
    reply. Claude Code's WebSearch (2.1.317) sends tools=[web_search_*] with tool_choice {"type": "tool", "name":
    "web_search"}; refusing that with the bridge's auto-only 400 meant the refusal text never reached the model.
    Allowed only when every tool is a server tool (no backend is started for such a request, so nothing has to honour
    the forcing) and the choice is `any` or names one of them. Forcing a client tool still fails: the bridge cannot."""
    if not tools or not all(is_server_tool(t) for t in tools):
        return False
    if choice.get("type") == "any":
        return True
    return choice.get("type") == "tool" and choice.get("name") in {t.get("name") for t in tools}


class WireError(ValueError):
    """The request is not a Messages request this endpoint can serve; the server answers 400 with the message."""


@dataclass
class ClientTurn:
    system: str
    tools: list[dict[str, Any]]
    content: list[dict[str, Any]]                 # new user content, tool_result blocks removed
    tool_results: dict[str, dict[str, Any]]       # tool_use_id -> MCP tool result
    history: list[str] = field(default_factory=list)   # reply_fingerprint of every earlier assistant message
    stream: bool = False
    model: str = MODEL_ID
    # Canonical digest of the conversation before its new turn (conversation_digest): what the user and the tools
    # said, plus the assistant fingerprints. It names the conversation itself, where `history` only proves the replies.
    prefix_digest: str = ""


def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
    raise WireError("system must be a string or a list of text blocks")


def without_cache_control(block: dict[str, Any]) -> dict[str, Any]:
    """Copy a client block without cache metadata, including nested content blocks.

    The backend CLI owns its cache breakpoints. Client markers collided with its own live on
    2026-11-02: API Error: 400 messages.103.content.0.cache_control.ttl: a ttl='1h'
    cache_control block must not come after a ttl='5m' cache_control block.
    Only block metadata is stripped: tool inputs and JSON schemas are application data.
    """
    out = {k: v for k, v in block.items() if k != "cache_control"}
    if isinstance(out.get("content"), list):
        out["content"] = [without_cache_control(b) if isinstance(b, dict) else b for b in out["content"]]
    return out


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [without_cache_control(b) for b in content if isinstance(b, dict)]
    raise WireError("message content must be a string or a list of blocks")


def mcp_content(blocks: Any) -> list[dict[str, Any]]:
    """A tool_result's content as MCP content. Text and base64 images map one to one; anything else is passed as its
    JSON text so the model still sees it."""
    out = []
    for b in _blocks(blocks if blocks is not None else ""):
        if b.get("type") == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
            out.append({"type": "image", "data": b["source"]["data"], "mimeType": b["source"].get("media_type", "image/png")})
        else:
            out.append({"type": "text", "text": json.dumps(b)})
    return out


def turn_fingerprint(turn: "ClientTurn") -> str:
    """Digest of a request's new user turn (its user content and tool results), so a retry of an answered turn can be
    told from a different message on the same history (server.Replay). Never logged."""
    return hashlib.sha256(json.dumps([turn.content, turn.tool_results], sort_keys=True, default=str).encode()).hexdigest()


def reply_fingerprint(blocks: Any) -> str:
    """One assistant message as the endpoint wrote it: its text (whitespace-trimmed, blocks joined) and its tool_use
    ids. Every assistant message in a conversation was written by this endpoint, so the sequence of fingerprints
    identifies the conversation and proves the client's history agrees with the CLI session's."""
    bs = _blocks(blocks)
    text = "".join(b.get("text", "") for b in bs if b.get("type") == "text").strip()
    ids = [b.get("id") for b in bs if b.get("type") == "tool_use"]
    return hashlib.sha256(json.dumps([text, ids]).encode()).hexdigest()


# Client-injected transient text inside user content. Claude Code wraps its reminders (todo state, file-change notes,
# hook output) in <system-reminder> and re-renders earlier ones from turn to turn, so they say nothing about which
# conversation a request belongs to. An unclosed tag is left alone (it is user text).
_OPEN, _CLOSE = "<system-reminder>", "</system-reminder>"


def canonical_text(text: Any) -> str:
    """User or tool text with client-injected reminder spans removed and whitespace collapsed, so re-rendered
    wrappers and spacing do not change a conversation's identity. Scanned with str.find, not a regex:
    `<system-reminder>.*?</system-reminder>` rescans to the end of the string for every opening tag without a closing
    one, which is quadratic in a client-sized tool result (a 368 KB body cost 4.7 s, paid again on every later turn).
    An unclosed opening ends the scan: no later opening can have a closing tag after it either."""
    s, out, i = str(text), [], 0
    while True:
        a = s.find(_OPEN, i)
        if a < 0:
            break
        b = s.find(_CLOSE, a)
        if b < 0:
            break
        out.append(s[i:a])
        i = b + len(_CLOSE)
    out.append(s[i:])
    return " ".join(" ".join(out).split())


# How deep canonical_parts follows tool_result content nested inside tool_result content. Real clients nest one level
# (a tool_result holding text and images); 64 is far past any real shape. Past it the block is hashed whole
# (history_digest) instead of recursed into, so the digest still covers every byte while its recursion stays bounded:
# each level costs several Python frames, and a body json.loads accepted could otherwise raise RecursionError, which
# is not a WireError and would escape the request handler.
DIGEST_MAX_DEPTH = 64


def _digest_blocks(content: Any) -> list[dict[str, Any]]:
    """_blocks for the conversation digest only. Earlier messages were already served (or are the client's own
    business), so content _blocks refuses (null, a number, an object) is hashed as one opaque `_raw` block instead of
    turning a conversation the endpoint used to serve into a 400. The new turn still goes through _blocks."""
    try:
        return _blocks(content)
    except WireError:
        return [{"type": "_raw", "raw": history_digest(content)}]


def canonical_parts(blocks: list[dict[str, Any]], depth: int = 0) -> list[list[Any]]:
    """Anthropic-shaped user blocks as conversation_digest tokens. Kept: text (canonical_text, empty dropped), images
    (by a hash of their source), tool_result ids with their canonical content and error flag, any other block as
    its JSON (cache metadata already removed by _blocks). Dropped: thinking / redacted_thinking (signatures and
    summaries the client may add or strip). A tool_result nested past DIGEST_MAX_DEPTH is hashed whole."""
    out: list[list[Any]] = []
    for b in blocks:
        kind = b.get("type")
        if kind == "text":
            t = canonical_text(b.get("text", ""))
            if t:
                out.append(["t", t])
        elif kind == "tool_result" and depth >= DIGEST_MAX_DEPTH:
            out.append(["o", history_digest(b)])
        elif kind == "tool_result":
            out.append(["r", str(b.get("tool_use_id")), bool(b.get("is_error")),
                        canonical_parts(_digest_blocks(b.get("content") if b.get("content") is not None else ""),
                                        depth + 1)])
        elif kind == "image":
            out.append(["i", history_digest(b.get("source"))])
        elif kind in ("thinking", "redacted_thinking"):
            continue
        else:
            out.append(["o", history_digest(b)])
    return out


def conversation_digest(messages: list[dict[str, Any]]) -> str:
    """The canonical identity of a conversation's history (the messages before the new turn), for the failed-session
    tombstone key (server.Registry.failure_key). It must survive what a client re-renders between a request and its
    retry, and still tell two conversations apart:
    - user content: in, as canonical_parts (real user text and tool results are the conversation). Message
      boundaries are not: a client may merge or split consecutive user messages.
    - assistant messages: in, as reply_fingerprint (text + tool_use ids, which already omits thinking blocks).
    - mid-conversation `system` messages and the top-level system prompt: out. Both are operator text the client
      regenerates (date, git status, environment), so a retry would miss its tombstone; separation between clients
      comes from the user content, the User-Agent and the run id (failure_key).
    - cache_control: out (_blocks strips it; it moves every turn).
    - content _blocks would refuse: hashed opaque (_digest_blocks), never a 400 for an earlier message."""
    tokens: list[list[Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            tokens.append(["a", reply_fingerprint(m.get("content"))])
        elif role == "user":
            tokens += canonical_parts(_digest_blocks(m.get("content")))
    return history_digest(tokens)


def history_digest(*parts: Any) -> str:
    """A stable digest of a request's conversation so far. Canonical JSON (sorted keys, ASCII escapes, so a lone
    surrogate hashes too); the same request re-sent gives the same digest, any other content another."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()


def check_tool_name(name: str, what: str = "tool name") -> str:
    """`name` if it is a valid Unicode scalar string. JSON can carry a lone surrogate ("\\ud800"), which Python decodes
    into a str that cannot be encoded as UTF-8 and so cannot be hashed, logged or sent to a backend: refused here, at
    the wire, as a client error instead of failing somewhere downstream."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        # !a escapes every non-ASCII code point, so the message itself can always be encoded (é and a surrogate alike).
        raise WireError(f"{what} {name!a} is not valid Unicode "
                        "(it contains a lone surrogate)") from None
    return name


def parse_request(body: dict[str, Any]) -> ClientTurn:
    """The request as a ClientTurn, or WireError. DIGEST_MAX_DEPTH bounds the digest's own recursion; the remaining
    recursive steps (without_cache_control, json.dumps of a block) follow the client's nesting, and a body nested
    close to json.loads' limit, arriving on a handler thread's deeper stack, can still exhaust it. A RecursionError
    is not a WireError and would escape the handler, so it is answered as the request-shape error it is."""
    try:
        return _parse_request(body)
    except RecursionError:
        raise WireError("message content is nested too deeply") from None


def _parse_request(body: dict[str, Any]) -> ClientTurn:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise WireError("messages must be a non-empty list")
    if messages[-1].get("role") == "assistant":
        raise WireError("the last message must not be an assistant message (assistant prefill is not supported)")
    # The new turn is everything after the last assistant message: user messages and mid-conversation system
    # messages (Claude Code sends one after the user turn). The CLI only accepts user input, so system text is passed
    # as user text, marked as coming from the operator.
    start = max((i + 1 for i, m in enumerate(messages) if m.get("role") == "assistant"), default=0)
    content, results = [], {}
    for m in messages[start:]:
        role = m.get("role")
        if role not in ("user", "system"):
            raise WireError(f"unsupported message role {role!r}")
        for b in _blocks(m.get("content")):
            if b.get("type") == "tool_result" and role == "user":
                results[b["tool_use_id"]] = {"content": mcp_content(b.get("content")), "isError": bool(b.get("is_error"))}
            elif role == "system":
                if b.get("type") == "text" and b.get("text"):
                    content.append({"type": "text", "text": f"<system-message>\n{b['text']}\n</system-message>"})
            else:
                content.append(b)
    if results and content:
        # The CLI can only receive tool results through the pending MCP calls, so text sent next to them (Claude
        # Code's reminders) rides on the last result rather than being dropped.
        last_id = list(results)[-1]
        results[last_id]["content"] += mcp_content(content)        # text and images alike; nothing the client sent is dropped
        content = []
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise WireError("tools must be a list")
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            check_tool_name(t["name"])
    # The CLI session's tool set is fixed when it starts and the CLI decides itself whether to call one, so only
    # `auto` can be honoured. Anything else is refused, never silently ignored. (max_tokens is not enforced either:
    # the CLI has no output cap flag; docs/PROVIDER-PLAN.md lists it as a known limit.)
    raw_choice = body.get("tool_choice") or {"type": "auto"}
    if not isinstance(raw_choice, dict):
        raise WireError("tool_choice must be an object")
    choice = raw_choice.get("type")
    # One exception, for server-tool-only requests: see _forced_server_tool_choice.
    if choice != "auto" and not _forced_server_tool_choice(tools, raw_choice):
        raise WireError(f"tool_choice {choice!r} is not supported by the cortados model; only 'auto'")
    return ClientTurn(system=_system_text(body.get("system")), tools=[without_cache_control(t) for t in tools
                                                                     if t.get("name") and not is_server_tool(t)],
                      content=content, tool_results=results,
                      history=[reply_fingerprint(m.get("content")) for m in messages if m.get("role") == "assistant"],
                      stream=bool(body.get("stream")), model=str(body.get("model") or MODEL_ID),
                      prefix_digest=conversation_digest(messages[:start]))


def transcript_text(messages: list[dict[str, Any]]) -> str:
    """Messages as plain text, for a tool-less request served by a fresh CLI with no session to replay into."""
    lines = []
    for m in messages:
        for b in _blocks(m.get("content")):
            if b.get("type") == "text":
                lines.append(f"{m.get('role')}: {b.get('text', '')}")
            elif b.get("type") in ("tool_use", "tool_result"):
                lines.append(f"{m.get('role')}: [{b['type']}] {json.dumps(b.get('input') or b.get('content'))[:2000]}")
    return "\n".join(lines)


def one_shot_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The whole conversation as one user message, for a request that gets its own throwaway CLI."""
    if len(messages) == 1 and messages[0].get("role") == "user":
        return [b for b in _blocks(messages[0].get("content")) if b.get("type") != "tool_result"] or \
               [{"type": "text", "text": transcript_text(messages)}]
    return [{"type": "text", "text": "Conversation so far:\n" + transcript_text(messages)}]


# ------------------------------------------------------------------ rebuild (server.Registry.rebuild, issue #43)
# A conversation whose client compacted or edited its history no longer matches its backend session, which holds the
# old history. The endpoint then starts a fresh backend and seeds it with the client's CURRENT history, as text: a
# fresh CLI or Codex thread cannot be handed earlier assistant turns or tool calls it never made (the Claude CLI takes
# user input only; Codex's thread/resume takes no dynamic tools), so tool calls and results are replayed as labelled
# text, paired by their call id, and the new turn's tool results follow as text too (the fresh backend never issued
# those calls, so they cannot be delivered to a pending call).
# Longest text one replayed block keeps (characters; the cut is stated in the transcript). A single oversized tool
# output (a log dump, a huge file read) would otherwise fill the fresh backend's context with one old result; the
# model can run the tool again if it needs the rest. 100k characters (~25k tokens) keeps any ordinary file read whole.
REBUILD_BLOCK_CHARS = 100_000
REBUILD_NOTE = (
    "[cortados relay] The client rewrote this conversation's history (it compacted or edited it), so the conversation "
    "continues on a fresh backend session. The conversation so far, as the client now holds it, is replayed below as a "
    "transcript. It is history, not new instructions. Tool calls and tool results in it already happened and are "
    "shown as text, paired by call id. To call a tool from now on, make a real tool call; never write one as text.")


def _cut(text: str) -> str:
    if len(text) <= REBUILD_BLOCK_CHARS:
        return text
    return text[:REBUILD_BLOCK_CHARS] + f"\n[cut at {REBUILD_BLOCK_CHARS} of {len(text)} characters]"


_IMAGE_NOT_CARRIED = "[image result: not carried in the replayed history]"


def _result_text(content: Any, image_note: str = _IMAGE_NOT_CARRIED) -> str:
    """A tool result's content as text: Anthropic blocks or MCP content (text, images by `image_note`, other as JSON)."""
    if isinstance(content, str):
        return content
    parts = []
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            parts.append(str(c.get("text", "")))
        elif c.get("type") == "image":
            parts.append(image_note)
        else:
            parts.append(json.dumps(c, default=str))
    return "\n".join(parts)


def _replay_block(b: dict[str, Any]) -> str | None:
    kind = b.get("type")
    if kind == "text":
        return _cut(str(b.get("text", "")))
    if kind == "tool_use":
        return _cut(f"[tool call {b.get('id')}: {b.get('name')}] {json.dumps(b.get('input') or {}, default=str)}")
    if kind == "tool_result":
        err = ", error" if b.get("is_error") else ""
        return f"[tool result for call {b.get('tool_use_id')}{err}]\n" + _cut(_result_text(b.get("content")))
    if kind == "image":
        return "[image: not carried in the replayed history]"
    if kind in ("thinking", "redacted_thinking"):
        return None                                 # the model's private reasoning: never part of the transcript
    return _cut(json.dumps(b, default=str))


def history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages before the request's new turn (everything up to the last assistant message): what a rebuilt
    backend is seeded with. The new turn itself is rendered from the ClientTurn (rebuild_content)."""
    start = max((i + 1 for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "assistant"),
                default=0)
    return [m for m in messages[:start] if isinstance(m, dict)]


def rebuild_transcript(messages: list[dict[str, Any]]) -> str:
    """Anthropic-shaped history messages as one labelled transcript (see REBUILD_NOTE). Content that _blocks refuses
    in an earlier message is shown as its JSON, never a 400: the endpoint already accepted it once."""
    out = []
    for m in messages:
        role = {"assistant": "assistant", "user": "user", "system": "operator"}.get(m.get("role"), str(m.get("role")))
        try:
            blocks = _blocks(m.get("content"))
        except WireError:
            blocks = [{"type": "_raw", "content": m.get("content")}]
        parts = [p for p in (_replay_block(b) for b in blocks) if p]
        if parts:
            out.append(f"<{role}>\n" + "\n".join(parts) + f"\n</{role}>")
    return "\n\n".join(out)


def rebuild_content(transcript: str, turn: ClientTurn) -> list[dict[str, Any]]:
    """The first user message of a rebuilt backend: REBUILD_NOTE, the history transcript, then the request's new turn
    (its tool results as text by call id, images kept as image blocks, then its user content)."""
    out: list[dict[str, Any]] = [{"type": "text", "text": f"{REBUILD_NOTE}\n\n<conversation-history>\n{transcript}\n"
                                                          "</conversation-history>\n\nThe client's new turn follows."}]
    for call_id, result in turn.tool_results.items():
        err = ", error" if result.get("isError") else ""
        out.append({"type": "text", "text": f"[tool result for call {call_id}{err}]\n"
                                            + _cut(_result_text(result.get("content"),
                                                                "[image result: attached after this text]"))})
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "image" and c.get("data"):
                out.append({"type": "image", "source": {"type": "base64", "media_type": c.get("mimeType") or "image/png",
                                                        "data": c["data"]}})
    return out + list(turn.content)


def new_message_id() -> str:
    return "msg_cortados_" + uuid.uuid4().hex[:24]


def message_json(msg_id: str, model: str, blocks: list[dict[str, Any]], stop_reason: str, usage: dict[str, int]) -> dict:
    return {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": blocks,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": usage_json(usage)}


def usage_json(usage: dict[str, int]) -> dict[str, int]:
    """The session's usage in Messages form: input_tokens excludes the cache reads and writes, which are reported
    beside it as the API does (both bridges report this shape; dropping the cache counts would hide most of a cached
    turn's input from the client's accounting)."""
    return {"input_tokens": int(usage.get("input_tokens", 0)), "output_tokens": int(usage.get("output_tokens", 0)),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0)}


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def sse_start(msg_id: str, model: str) -> bytes:
    return sse("message_start", {"type": "message_start", "message": message_json(msg_id, model, [], None, {})})


def sse_ping() -> bytes:
    return sse("ping", {"type": "ping"})


def sse_body(blocks: list[dict[str, Any]], stop_reason: str, usage: dict[str, int]) -> Iterator[bytes]:
    """Everything after message_start, for a turn whose blocks are already complete."""
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            yield sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}})
        elif b["type"] == "tool_use":
            yield sse("content_block_start", {"type": "content_block_start", "index": i,
                                              "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                              "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
        else:
            raise ValueError(f"unsupported block type {b['type']!r}")
        yield sse("content_block_stop", {"type": "content_block_stop", "index": i})
    # message_start carried zeros (nothing was known then); the whole turn's usage, cache counts included, rides here,
    # where the API reports cumulative usage and where clients read it.
    yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                "usage": usage_json(usage)})
    yield sse("message_stop", {"type": "message_stop"})


def error_json(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}
