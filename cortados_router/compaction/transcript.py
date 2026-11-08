"""Parse client transcripts into the shared `Session` shape (stdlib only, no model calls).

- `read_claude_code(path)`: a Claude Code transcript, ~/.claude/projects/<project>/<session-id>.jsonl. Lines of type
  "user"/"assistant" carry `message.content` (a string or blocks: text, tool_use, tool_result, thinking, image).
  Sidechain lines (subagent traffic) and meta lines (injected context, compaction summaries) are not part of the
  main conversation and are counted, not parsed. Consecutive assistant lines form one assistant Message (the client
  writes one line per content block).
- `read_codex(path)`: a Codex CLI rollout, ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. `session_meta` gives the id;
  `response_item` payloads of type message / function_call / function_call_output / custom_tool_call /
  custom_tool_call_output carry the conversation; `compacted` marks a Codex-side compaction.

Tool calls are paired with results by id; a call with no result is `interrupted=True`. Every line or block that is not
parsed is counted in `stats["skipped"]` by kind (no silent catch-all): `parse_*` returns `(Session, stats)` so callers
can report what was left out; `read_*` returns the Session alone.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .events import Message, Session, ToolEvent

Stats = Dict[str, Any]


class _Builder:
    """Mutable accumulator; frozen into a Session at the end."""

    def __init__(self, source: str):
        self.source = source
        self.session = ""
        self.messages: List[dict] = []
        self.events: List[dict] = []
        self.by_call_id: Dict[str, int] = {}
        self.skipped: Counter = Counter()
        self.counts: Counter = Counter()

    def message(self, role: str, text: str, ts: Optional[str], merge: bool = False) -> int:
        if merge and self.messages and self.messages[-1]["role"] == role:
            m = self.messages[-1]
            if text:
                m["text"] = (m["text"] + "\n\n" + text) if m["text"] else text
            return m["index"]
        self.messages.append({"index": len(self.messages), "role": role, "text": text, "events": [], "ts": ts})
        return len(self.messages) - 1

    def call(self, call_id: str, name: str, inp: dict, ts: Optional[str]) -> None:
        if not self.messages or self.messages[-1]["role"] != "assistant":
            self.message("assistant", "", ts)
        mi = self.messages[-1]["index"]
        ev = {"index": len(self.events), "message_index": mi, "name": name, "input": inp, "result": None,
              "is_error": False, "ts": ts}
        self.events.append(ev)
        self.messages[mi]["events"].append(ev["index"])
        if call_id:
            self.by_call_id[call_id] = ev["index"]

    def result(self, call_id: str, text: str, is_error: bool) -> None:
        idx = self.by_call_id.get(call_id)
        if idx is None:
            self.skipped["orphan_result"] += 1
            return
        ev = self.events[idx]
        ev["result"] = text
        ev["is_error"] = bool(is_error)

    def build(self) -> Tuple[Session, Stats]:
        sid = self.session or "unknown"
        events = tuple(ToolEvent(session=sid, index=e["index"], message_index=e["message_index"], name=e["name"],
                                 input=e["input"], result_text=e["result"] or "",
                                 result_chars=len(e["result"] or ""), is_error=e["is_error"],
                                 interrupted=e["result"] is None, ts=e["ts"]) for e in self.events)
        messages = tuple(Message(session=sid, index=m["index"], role=m["role"], text=m["text"],
                                 tool_event_indexes=tuple(m["events"]), ts=m["ts"]) for m in self.messages)
        stats = {"source": self.source, "messages": len(messages), "events": len(events),
                 "interrupted": sum(1 for e in events if e.interrupted),
                 "counts": dict(sorted(self.counts.items())), "skipped": dict(sorted(self.skipped.items()))}
        return Session(session=sid, source=self.source, messages=messages, events=events), stats


def _lines(path: str, b: _Builder):
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                b.skipped["bad_json"] += 1
                continue
            if not isinstance(obj, dict):
                b.skipped["not_object"] += 1
                continue
            yield obj


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
            elif isinstance(blk, dict):
                parts.append("[%s]" % blk.get("type", "block"))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, sort_keys=True)


def parse_claude_code(path: str) -> Tuple[Session, Stats]:
    b = _Builder("claude-code")
    for obj in _lines(path, b):
        t = obj.get("type")
        if not b.session and obj.get("sessionId"):
            b.session = str(obj["sessionId"])
        if t not in ("user", "assistant"):
            b.skipped["type:%s" % t] += 1
            continue
        if obj.get("isSidechain"):
            b.skipped["sidechain"] += 1
            continue
        if obj.get("isMeta") or obj.get("isCompactSummary"):
            b.skipped["meta"] += 1
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            b.skipped["no_message"] += 1
            continue
        ts = obj.get("timestamp")
        content = msg.get("content")
        b.counts[t] += 1
        if isinstance(content, str):
            b.message(t, content, ts, merge=(t == "assistant"))
            continue
        if not isinstance(content, list):
            b.skipped["content:%s" % type(content).__name__] += 1
            continue
        texts = []
        for blk in content:
            bt = blk.get("type") if isinstance(blk, dict) else None
            if bt == "text":
                texts.append(str(blk.get("text", "")))
            elif bt == "tool_use" and t == "assistant":
                if texts:
                    b.message("assistant", "\n\n".join(texts), ts, merge=True)
                    texts = []
                inp = blk.get("input") if isinstance(blk.get("input"), dict) else {"input": blk.get("input")}
                b.call(str(blk.get("id", "")), str(blk.get("name", "")), inp, ts)
            elif bt == "tool_result" and t == "user":
                b.result(str(blk.get("tool_use_id", "")), _result_text(blk.get("content")), bool(blk.get("is_error")))
            else:
                b.skipped["block:%s" % bt] += 1
        if texts:
            b.message(t, "\n\n".join(texts), ts, merge=(t == "assistant"))
    return b.build()


# Codex injects these as user-role messages; they are context, not prompts the user typed.
_CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions",
                            "<permissions instructions>", "<turn_aborted>")


def _codex_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for blk in content or []:
        if isinstance(blk, dict) and blk.get("type") in ("input_text", "output_text", "text"):
            parts.append(str(blk.get("text", "")))
    return "\n\n".join(parts)


def _codex_output(out: Any) -> Tuple[str, bool]:
    """function_call_output.output is a string (sometimes JSON with output + metadata.exit_code) or an object."""
    data = out
    if isinstance(out, str):
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return out, False
    if isinstance(data, dict):
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        code = meta.get("exit_code")
        text = data.get("output", data.get("content", ""))
        text = _result_text(text) if not isinstance(text, str) else text
        return text, isinstance(code, int) and code != 0
    return (out if isinstance(out, str) else json.dumps(out, sort_keys=True)), False


def parse_codex(path: str) -> Tuple[Session, Stats]:
    b = _Builder("codex")
    for obj in _lines(path, b):
        t = obj.get("type")
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ts = obj.get("timestamp")
        if t == "session_meta":
            b.session = str(p.get("id") or p.get("session_id") or b.session)
            b.counts["session_meta"] += 1
            continue
        if t == "compacted":
            b.counts["compacted"] += 1
            continue
        if t != "response_item":
            b.skipped["type:%s" % t] += 1
            continue
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            text = _codex_text(p.get("content"))
            if role not in ("user", "assistant"):
                b.skipped["role:%s" % role] += 1
                continue
            if role == "user" and text.lstrip().startswith(_CODEX_INJECTED_PREFIXES):
                b.skipped["meta"] += 1
                continue
            b.counts[role] += 1
            b.message(role, text, ts, merge=(role == "assistant"))
        elif pt == "function_call":
            args = p.get("arguments")
            try:
                inp = json.loads(args) if isinstance(args, str) else (args or {})
            except json.JSONDecodeError:
                inp = {"arguments": args}
            if not isinstance(inp, dict):
                inp = {"arguments": inp}
            b.counts["function_call"] += 1
            b.call(str(p.get("call_id", "")), str(p.get("name", "")), inp, ts)
        elif pt == "custom_tool_call":
            b.counts["custom_tool_call"] += 1
            b.call(str(p.get("call_id", "")), str(p.get("name", "")), {"input": p.get("input", "")}, ts)
        elif pt in ("function_call_output", "custom_tool_call_output"):
            text, err = _codex_output(p.get("output"))
            b.result(str(p.get("call_id", "")), text, err)
        else:
            b.skipped["item:%s" % pt] += 1
    return b.build()


def read_claude_code(path: str) -> Session:
    return parse_claude_code(path)[0]


def read_codex(path: str) -> Session:
    return parse_codex(path)[0]


def detect_format(path: str) -> str:
    """'codex' if the first parseable line looks like a rollout (session_meta / payload), else 'claude-code'."""
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return "codex" if ("payload" in obj or obj.get("type") == "session_meta") else "claude-code"
    raise ValueError("cannot detect transcript format: no JSON object lines in %s" % path)


def parse(path: str, fmt: str = "auto") -> Tuple[Session, Stats]:
    if fmt == "auto":
        fmt = detect_format(path)
    if fmt == "claude-code":
        return parse_claude_code(path)
    if fmt == "codex":
        return parse_codex(path)
    raise ValueError("unknown transcript format: %s" % fmt)
