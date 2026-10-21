"""The Anthropic Messages wire: reading a client's request and writing the response, plain JSON or SSE.

Owner of one rule: the client's message shapes are translated here and nowhere else. The bridge only sees
`ClientTurn` (what is new in this request) and hands back content blocks in Anthropic form.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

MODEL_ID = "cortados"


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


def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
    raise WireError("system must be a string or a list of text blocks")


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
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


def reply_fingerprint(blocks: Any) -> str:
    """One assistant message as the endpoint wrote it: its text (whitespace-trimmed, blocks joined) and its tool_use
    ids. Every assistant message in a conversation was written by this endpoint, so the sequence of fingerprints
    identifies the conversation and proves the client's history agrees with the CLI session's."""
    bs = _blocks(blocks)
    text = "".join(b.get("text", "") for b in bs if b.get("type") == "text").strip()
    ids = [b.get("id") for b in bs if b.get("type") == "tool_use"]
    return hashlib.sha256(json.dumps([text, ids]).encode()).hexdigest()


def parse_request(body: dict[str, Any]) -> ClientTurn:
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
        results[last_id]["content"] += mcp_content([b for b in content if b.get("type") == "text"])
        content = []
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise WireError("tools must be a list")
    # The CLI session's tool set is fixed when it starts and the CLI decides itself whether to call one, so only
    # `auto` can be honoured. Anything else is refused, never silently ignored. (max_tokens is not enforced either:
    # the CLI has no output cap flag; docs/PROVIDER-PLAN.md lists it as a known limit.)
    choice = (body.get("tool_choice") or {"type": "auto"}).get("type")
    if choice != "auto":
        raise WireError(f"tool_choice {choice!r} is not supported by the cortados model; only 'auto'")
    return ClientTurn(system=_system_text(body.get("system")), tools=[t for t in tools if t.get("name")],
                      content=content, tool_results=results,
                      history=[reply_fingerprint(m.get("content")) for m in messages if m.get("role") == "assistant"],
                      stream=bool(body.get("stream")), model=str(body.get("model") or MODEL_ID))


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
