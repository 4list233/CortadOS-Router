"""The OpenAI Responses wire, for the Codex CLI as a client: reading its request and writing the response, JSON or SSE.

Owner of one rule: Responses item shapes are translated here and nowhere else. The module keeps anthropic_wire's
contract (a `ClientTurn` in, Anthropic content blocks out, the same function names) so server.py can serve
POST /v1/responses with the session logic of /v1/messages unchanged. Shapes were verified against Codex CLI 0.163.0:
the request in tests/fixtures/codex-responses-request-0.163.0.json is a real capture with its prose replaced; the
history item shapes (assistant `message`, `function_call`, `function_call_output`) and the SSE event names the Codex
parser handles come from the binary's serde names (docs/PROVIDER-PLAN.md, P2).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Iterator

from cortados_router.provider import anthropic_wire
from cortados_router.provider.anthropic_wire import ClientTurn, WireError, mcp_content, reply_fingerprint, sse

MODEL_ID = anthropic_wire.MODEL_ID
# Item types Codex re-sends that this endpoint never produced and that carry no conversation content: they are
# skipped before fingerprinting, else Registry.find could never match a history that contains them.
#   reasoning: encrypted reasoning the client echoes back (we emit none, but `include` asks for it);
#   item_reference: a server-side reference, only meaningful with store=true (Codex sends store=false);
#   compaction: Codex's own context-compaction marker.
SKIPPED_ITEM_TYPES = ("reasoning", "item_reference", "compaction")
# Codex puts its own context in front of the prompt as user text blocks (verified on the real capture): the user's
# AGENTS.md as "# AGENTS.md instructions\n\n<INSTRUCTIONS>...</INSTRUCTIONS>" and an "<environment_context>...
# </environment_context>" block. They are the client's, not the request, so scoring drops them (the Responses twin of
# routing._INJECTED); without that S1 would band every new Codex conversation on the user's agent instructions. The
# model still receives them in the turn, as Codex intends.
CODEX_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>")
# Tool types that cannot be bridged to a headless CLI and are dropped with one warning per request: the model it
# routes to has no web search of its own on this endpoint, and the other built-in Responses tools (file_search,
# code_interpreter, computer, image_generation, mcp, ...) are hosted tools, not functions the client would run.
# The `namespace` type is refused, not dropped (see _parse_tools).
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ request side
def _image_block(item: dict[str, Any]) -> dict[str, Any]:
    """An input_image item as an Anthropic image block. A data URL is carried as base64 (what mcp_content and the
    headless CLI accept); any other URL is kept as a url source and reaches the model as its JSON text."""
    url = item.get("image_url")
    if isinstance(url, dict):                           # the Chat-Completions-style nesting, accepted for safety
        url = url.get("url")
    if not isinstance(url, str) or not url:
        raise WireError("input_image must carry image_url")
    if url.startswith("data:"):
        head, sep, data = url.partition(",")
        if not sep or ";base64" not in head:
            raise WireError("input_image data URL must be base64 (data:<mime>;base64,<data>)")
        return {"type": "image", "source": {"type": "base64", "media_type": head[5:].split(";")[0] or "image/png",
                                            "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _blocks_of(content: Any) -> list[dict[str, Any]]:
    """Message or output content as Anthropic blocks. Responses content is a string or a list of typed items;
    text items (input_text, output_text) become text, input_image becomes an image, anything else is refused by
    name rather than guessed at (input_audio has no headless-CLI equivalent)."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise WireError("content must be a string or a list of content items")
    out = []
    for item in content:
        if not isinstance(item, dict):
            raise WireError("content items must be objects")
        kind = item.get("type")
        if kind in ("input_text", "output_text"):
            out.append({"type": "text", "text": str(item.get("text", ""))})
        elif kind == "input_image":
            out.append(_image_block(item))
        else:
            raise WireError(f"unsupported content item type {kind!r}")
    return out


def _tool_use_block(item: dict[str, Any]) -> dict[str, Any]:
    """A function_call history item as the tool_use block this endpoint emitted for it: the call_id is the tool_use
    id (bridge.emitted / deliver work unchanged) and the arguments string is parsed back to the input object."""
    if item.get("namespace"):
        # The function_call item carries `namespace` (verified: ResponseItem::FunctionCall serde field) for tools
        # that live in a `namespace` tool (ChatGPT apps). Those are refused at the tool list; a namespaced call in
        # the history means the conversation was started elsewhere.
        raise WireError("function_call with a namespace cannot belong to a cortados conversation; "
                        "run Codex with --disable apps")
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise WireError("function_call must carry call_id")
    args = item.get("arguments", "{}")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError as exc:
            raise WireError(f"function_call {call_id} arguments are not JSON: {exc}") from None
    if not isinstance(args, dict):
        raise WireError(f"function_call {call_id} arguments must be a JSON object")
    return {"type": "tool_use", "id": call_id, "name": str(item.get("name") or ""), "input": args}


def _output_result(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A function_call_output item as (call_id, MCP tool result). `output` is a string or a list of input_text /
    input_image items. Responses has no error flag on an output: Codex reports a failed tool as text in `output`,
    so isError is always False here (the model reads the failure in the text, as it would on the Codex side)."""
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise WireError("function_call_output must carry call_id")
    output = item.get("output")
    if not isinstance(output, (str, list)):
        raise WireError(f"function_call_output {call_id} output must be a string or a list of content items")
    return call_id, {"content": mcp_content(_blocks_of(output)), "isError": False}


def _classify(item: Any) -> str:
    """One input item as its side: 'system' (developer/system message), 'user' (user message or a tool output),
    'assistant' (assistant message or function_call), 'skip' (SKIPPED_ITEM_TYPES); anything else is refused."""
    if not isinstance(item, dict):
        raise WireError("input items must be objects")
    kind = item.get("type", "message")                  # a bare {"role","content"} item is a message (Responses API)
    if kind == "message":
        role = item.get("role")
        if role in ("developer", "system"):
            return "system"
        if role in ("user", "assistant"):
            return role
        raise WireError(f"unsupported message role {role!r}")
    if kind == "function_call":
        return "assistant"
    if kind == "function_call_output":
        return "user"
    if kind in SKIPPED_ITEM_TYPES:
        return "skip"
    raise WireError(f"unsupported input item type {kind!r} (this endpoint never produced it)")


def _parse_tools(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise WireError("tools must be a list")
    out, dropped = [], []
    for t in tools:
        if not isinstance(t, dict):
            raise WireError("tools must be objects")
        kind = t.get("type")
        if kind == "function":
            name = t.get("name")
            if not isinstance(name, str) or not name:
                raise WireError("function tools must carry a name")
            # `strict` has no Anthropic equivalent and is dropped; a function without `parameters` takes no
            # arguments, which is the empty object schema.
            out.append({"name": name, "description": str(t.get("description") or ""),
                        "input_schema": t.get("parameters") or {"type": "object", "properties": {}}})
        elif kind == "namespace":
            # A `namespace` tool (ChatGPT apps: name "mcp__codex_apps__<app>", inner tools "_<tool>") would have to
            # be flattened to one function per inner tool and split back into (namespace, name) on every
            # function_call this endpoint emits. The emitter only sees `{"type":"tool_use","name":...}` blocks
            # and no tool list, and "<namespace>__<tool>" is not invertible when the namespace itself contains
            # "__" and the tool starts with "_" (every observed one does), so a flattened call would fail inside
            # Codex mid-turn instead of here. Refused up front, with the flag that removes these tools.
            raise WireError(f"namespace tool {t.get('name')!r} (ChatGPT apps) cannot be bridged; "
                            "run Codex with --disable apps")
        else:
            dropped.append(str(kind))
    if dropped:
        logger.warning("responses wire: dropped %d non-function tool(s) the cortados model cannot serve: %s",
                       len(dropped), ", ".join(sorted(set(dropped))))
    return out


def parse_request(body: dict[str, Any]) -> ClientTurn:
    if body.get("model") != MODEL_ID:
        # Codex checks the model the response echoes against the one it asked for; serving any other name would
        # fail on its side after the turn ran. Refuse first.
        raise WireError(f"model must be {MODEL_ID!r}, got {body.get('model')!r}")
    if body.get("previous_response_id"):
        # The endpoint keeps no response store: a conversation is identified by the full input (Registry.find), and
        # continuing one from an id alone would mean guessing which session it meant.
        raise WireError("previous_response_id is not supported; send the full input (store=false)")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise WireError("stream must be true or false")
    choice = body.get("tool_choice", "auto")
    if choice != "auto":
        # The CLI session's tool set is fixed when it starts and the CLI decides itself whether to call one, so only
        # `auto` can be honoured; anything else is refused, never silently ignored (as on the Messages wire).
        raise WireError(f"tool_choice {choice!r} is not supported by the cortados model; only 'auto'")
    items = body.get("input")
    if not isinstance(items, list) or not items:
        raise WireError("input must be a non-empty list of items")
    sides = [_classify(it) for it in items]
    kept = [(s, it) for s, it in zip(sides, items) if s != "skip"]
    if not kept or kept[-1][0] == "assistant":
        raise WireError("the input must end with user content or tool outputs, not an assistant item "
                        "(assistant prefill is not supported)")
    # System text: every developer/system item, in order (Codex sends its base prompt and the skills block that way;
    # the Responses `instructions` field is accepted ahead of them for other clients). The new turn is everything
    # after the last run of assistant items; a developer/system item inside it cannot change a running CLI's system
    # prompt, so it is passed as user text marked as the operator's, as the Messages wire does.
    start = max((i + 1 for i, (s, _) in enumerate(kept) if s == "assistant"), default=0)
    system_parts = [str(body["instructions"])] if body.get("instructions") else []
    history, run = [], []
    for s, it in kept[:start]:
        if s == "system":
            system_parts += [b["text"] for b in _blocks_of(it.get("content")) if b["type"] == "text" and b["text"]]
            if run:
                # A developer item after a reply was served as its own turn (operator text, below), so the reply it
                # follows ended there: without this boundary the two replies around it would merge into one
                # fingerprint and a conversation this endpoint served would come back as a 409.
                history.append(reply_fingerprint(run))
                run = []
        elif s == "assistant":
            run += _blocks_of(it.get("content")) if it.get("type", "message") == "message" else [_tool_use_block(it)]
        else:
            if run:
                history.append(reply_fingerprint(run))
                run = []
    if run:
        history.append(reply_fingerprint(run))
    content, results = [], {}
    for s, it in kept[start:]:
        if s == "system":
            texts = [b["text"] for b in _blocks_of(it.get("content")) if b["type"] == "text" and b["text"]]
            system_parts += texts
            if start:
                content += [{"type": "text", "text": f"<system-message>\n{t}\n</system-message>"} for t in texts]
        elif it.get("type") == "function_call_output":
            call_id, result = _output_result(it)
            if call_id in results:
                raise WireError(f"two function_call_output items for call_id {call_id}")
            results[call_id] = result
        else:
            content += _blocks_of(it.get("content"))
    if results and content:
        # The CLI can only receive tool results through the pending calls, so text sent next to them rides on the
        # last result rather than being dropped (same rule as the Messages wire).
        last_id = list(results)[-1]
        results[last_id]["content"] += mcp_content([b for b in content if b.get("type") == "text"])
        content = []
    if not content and not results:
        raise WireError("the new turn carries no user content and no tool outputs")
    return ClientTurn(system="\n\n".join(system_parts), tools=_parse_tools(body.get("tools")), content=content,
                      tool_results=results, history=history, stream=stream, model=MODEL_ID)


def _messages_of(body: dict[str, Any], *, with_tools: bool) -> list[dict[str, Any]]:
    """The input as Anthropic-shaped messages, developer/system items excluded: text and image blocks, plus, with
    with_tools, the tool_use / tool_result blocks for the transcript of a one-shot request. Images stay so a tool-less
    single-message request reaches its throwaway CLI with its evidence, as on the Anthropic wire (whose
    one_shot_content keeps a single user message's blocks; its transcript of a longer conversation is text only)."""
    messages: list[dict[str, Any]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if blocks:
            messages.append({"role": role, "content": blocks})

    for it in body.get("input") or []:
        if not isinstance(it, dict):
            continue
        kind = it.get("type", "message")
        if kind == "message" and it.get("role") in ("user", "assistant"):
            add(it["role"], [b for b in _blocks_of(it.get("content")) if b["type"] in ("text", "image")])
        elif with_tools and kind == "function_call":
            add("assistant", [_tool_use_block(it)])
        elif with_tools and kind == "function_call_output":
            call_id, result = _output_result(it)
            add("user", [{"type": "tool_result", "tool_use_id": call_id, "content": result["content"]}])
    return messages


def _injected(block: dict[str, Any]) -> bool:
    return block["type"] == "text" and block["text"].lstrip().startswith(CODEX_INJECTED_PREFIXES)


def score_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    """What routing.request_text scores: the user and assistant messages as role + text blocks. Developer/system
    items (Codex's base prompt, skills block) and the injected user blocks (CODEX_INJECTED_PREFIXES) are the
    client's, not the request; a user message left with nothing else is dropped, so routing's "first user message"
    is the prompt."""
    out = []
    for m in _messages_of(body, with_tools=False):
        blocks = [b for b in m["content"] if not (m["role"] == "user" and _injected(b))]
        if blocks:
            out.append({"role": m["role"], "content": blocks})
    return out


def function_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The request's function tools, as sent: what routing sees as `tools`, so its standalone test (no tools) agrees
    with the server's one-shot test, which counts only the function tools parse_request keeps."""
    tools = body.get("tools")
    return [t for t in tools if isinstance(t, dict) and t.get("type") == "function"] if isinstance(tools, list) else []


def one_shot_content(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The whole conversation as one user message, for a request that gets its own throwaway CLI."""
    return anthropic_wire.one_shot_content(_messages_of(body, with_tools=True))


# ------------------------------------------------------------------ response side
def new_message_id() -> str:
    return "resp_cortados_" + uuid.uuid4().hex[:24]


def _usage_json(usage: dict[str, int]) -> dict[str, Any]:
    """The session's usage in Responses form. Sessions report usage in the Messages shape (both bridges: input_tokens
    excludes cache reads and writes, which come as cache_read_input_tokens / cache_creation_input_tokens), while the
    Responses input_tokens includes everything the model read and cached_tokens is the part served from cache; the
    two are added up here so a client's token accounting matches what the backend billed."""
    out = int(usage.get("output_tokens", 0))
    cached, written = int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0)
    inp = int(usage.get("input_tokens", 0)) + cached + written
    reasoning = int(usage.get("reasoning_output_tokens") or 0)
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "input_tokens_details": {"cached_tokens": cached}, "output_tokens_details": {"reasoning_tokens": reasoning}}


def _output_items(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content blocks as Responses output items. The function_call's call_id is the tool_use id, so the id the
    client sends back in function_call_output is the one the session is waiting on; its item id is minted here."""
    items = []
    for b in blocks:
        if b["type"] == "text":
            items.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:24], "role": "assistant",
                          "status": "completed", "content": [{"type": "output_text", "text": b["text"], "annotations": []}]})
        elif b["type"] == "tool_use":
            items.append({"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:24], "call_id": b["id"],
                          "name": b["name"], "arguments": json.dumps(b["input"]), "status": "completed"})
        else:
            raise ValueError(f"unsupported block type {b['type']!r}")
    return items


def _response(msg_id: str, model: str, status: str, items: list[dict[str, Any]], usage: dict[str, Any] | None,
              error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": msg_id, "object": "response", "created_at": int(time.time()), "status": status, "model": model,
            "output": items, "error": error, "incomplete_details": None, "usage": usage}


def message_json(msg_id: str, model: str, blocks: list[dict[str, Any]], stop_reason: str, usage: dict[str, int]) -> dict:
    """The non-streaming response object. Responses has no stop reason: a function_call item is the tool_use stop."""
    return _response(msg_id, model, "completed", _output_items(blocks), _usage_json(usage))


def _event(kind: str, seq: int, **fields: Any) -> bytes:
    return sse(kind, {"type": kind, "sequence_number": seq, **fields})


def sse_start(msg_id: str, model: str) -> bytes:
    """response.created, sequence 0; the turn's events (sse_body) continue from 1."""
    return _event("response.created", 0, response=_response(msg_id, model, "in_progress", [], None))


def sse_ping() -> bytes:
    """An SSE comment line: Responses has no ping event, and a comment is what the SSE spec says a parser ignores."""
    return b": ping\n\n"


def sse_body(blocks: list[dict[str, Any]], stop_reason: str, usage: dict[str, int], *, msg_id: str | None = None,
             model: str = MODEL_ID, first_sequence: int = 1) -> Iterator[bytes]:
    """Everything after response.created, for a turn whose blocks are already complete: per item output_item.added
    and output_item.done (Codex builds its history from the done items), then response.completed with the whole
    response. `msg_id` should be the id sse_start announced; the model must be the requested one (Codex compares)."""
    items = _output_items(blocks)
    seq = first_sequence
    for i, item in enumerate(items):
        pending = dict(item, status="in_progress", content=[]) if item["type"] == "message" else dict(item, status="in_progress")
        yield _event("response.output_item.added", seq, output_index=i, item=pending)
        yield _event("response.output_item.done", seq + 1, output_index=i, item=item)
        seq += 2
    yield _event("response.completed", seq,
                 response=_response(msg_id or new_message_id(), model, "completed", items, _usage_json(usage)))


def sse_error(message: str, *, msg_id: str | None = None, model: str = MODEL_ID, sequence: int = 1) -> bytes:
    """response.failed; Codex shows response.error.message."""
    return _event("response.failed", sequence,
                  response=_response(msg_id or new_message_id(), model, "failed", [], None,
                                     error={"code": "server_error", "message": message}))


def error_json(kind: str, message: str) -> dict[str, Any]:
    """The HTTP error body; Codex prints it verbatim."""
    return {"error": {"type": kind, "message": message}}
