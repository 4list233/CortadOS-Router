"""Shared record shapes for compaction (stdlib only).

One normalized form for a session, whatever produced it (a Claude Code transcript, a Codex rollout, the provider's
own conversation store): ordered messages, and the tool calls they issued paired with their results. Parsers emit
these; the handoff builder, the pruning rules, the label harvester and S1 read them. Keep this file small: a field is
added here only when two parts need it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ToolEvent:
    """One tool call and its result."""
    session: str                 # session / conversation id the call belongs to
    index: int                   # order of this call within the session, from 0
    message_index: int           # Message.index of the assistant message that issued it
    name: str                    # tool name as the client saw it
    input: dict[str, Any]        # tool arguments
    result_text: str             # full result text; "" when no result was recorded
    result_chars: int            # len of the original result (result_text may be stored truncated)
    is_error: bool               # the result was flagged as an error
    interrupted: bool            # the call has no result: the session ended or was cut mid-tool
    ts: Optional[str] = None     # ISO-8601 time of the call when the source records one


@dataclass(frozen=True)
class Message:
    """A user or assistant message, text only; its tool calls are ToolEvents pointing back by message_index."""
    session: str
    index: int                   # order within the session, from 0
    role: str                    # "user" or "assistant"
    text: str                    # visible text, verbatim
    tool_event_indexes: tuple[int, ...] = field(default_factory=tuple)
    ts: Optional[str] = None


@dataclass(frozen=True)
class Session:
    session: str
    source: str                  # "claude-code", "codex", "provider", ...
    messages: tuple[Message, ...]
    events: tuple[ToolEvent, ...]


@dataclass(frozen=True)
class KeepDecision:
    """Whether a past tool call stays in the context, and on whose say-so. Logged for every decision so labels can
    later be joined to it."""
    session: str
    event_index: int
    keep_call: bool              # keep the fact that the call happened (with its input)
    keep_result: bool            # keep the full result verbatim (False: drop or keep a short head only)
    decided_by: str              # "rule:<name>" or "s1:<adapter id>"
    reason: str
    p_keep_call: Optional[float] = None    # model probability when decided_by is a model
    p_keep_result: Optional[float] = None
