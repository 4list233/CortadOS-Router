"""Model choice for a provider conversation: S1 scores the request once, the router's execution policy picks the route.

Owner of two rules:
  SERVED_PROVIDERS  which routes this endpoint can serve. Choosing among them stays with execution_routing.select,
                    the router's one execution policy (S1 → band → cheapest capable route with live capacity), so the
                    endpoint is a host of the router, not a second router.
  no Claude when degraded  core.resolve's hard rule that a degraded scorer never defaults to Claude. Execution mode
                    skips the table's non-Claude fallback routes, so this host enforces the rule itself: while S1 is
                    down a Claude route is not offered, and with no other lane served the request is refused.
"""
from __future__ import annotations

import json
import re
import shutil
from typing import Any, Callable

from cortados_router import execution_routing
from cortados_router.provider import anthropic_wire as wire

# P1 serves the Claude CLI only. codex-cli joins with its bridge (P2); local models need the 2026-09-28 "no local
# routes" ruling lifted and local entries in the generated model scores.
SERVED_PROVIDERS = ("claude-cli",)
CLAUDE_PROVIDERS = ("claude-cli",)
# Coding clients are interactive sessions: the router's face floor (selection.interactive_min_general_index) applies.
PLATFORM = "cli"
# Client-injected context, not the request: Claude Code wraps its reminders and environment in these.
_INJECTED = re.compile(r"<(system-reminder|system-message)>.*?</\1>", re.S)


def served_routes(core) -> tuple[tuple[str, str], ...]:
    rows = [r for m in (core.model_scores() or {}).get("models", []) for r in m.get("reach", [])]
    rows += [r for band in core.table()["candidates"].values() for r in band]
    return tuple(sorted({(r["provider"], r["model"]) for r in rows if r["provider"] in SERVED_PROVIDERS}))


def request_text(messages: list[dict[str, Any]], *, standalone: bool = False) -> str:
    """What S1 scores, without the client's injected context: the conversation's first user message (the task the
    session is for), or for a standalone tool-less call (a title or summary) its own last user message."""
    users = [m for m in messages if m.get("role") == "user"]
    pick = (users[-1] if standalone else users[0]) if users else {}
    text = "\n".join(b.get("text", "") for b in wire._blocks(pick.get("content")) if b.get("type") == "text")
    return _INJECTED.sub("", text).strip()


class _NoClaudeWhenDegraded:
    """`core` for execution_routing.select, except that a degraded decision cannot resolve to a Claude route."""

    def __init__(self, core):
        self._core = core

    def __getattr__(self, name):
        return getattr(self._core, name)

    def resolve(self, decision, *args, allowed_routes=None, **kw):
        if decision.get("degraded") or not decision.get("answers"):
            allowed_routes = tuple(p for p in (allowed_routes or ()) if p[0] not in CLAUDE_PROVIDERS)
        return self._core.resolve(decision, *args, allowed_routes=allowed_routes, **kw)


def _credentials(claude_bin: str) -> Callable[..., None]:
    def check(*, requested: str, target_model: str) -> None:
        if requested not in SERVED_PROVIDERS:
            raise execution_routing.CredentialsUnavailable(f"{requested} is not served by this endpoint")
        if requested == "claude-cli" and not shutil.which(claude_bin):
            raise execution_routing.CredentialsUnavailable(f"{claude_bin} is not on PATH")
    return check


def choose(core, body: dict[str, Any], *, client: str, conversation_id: str, claude_bin: str = "claude") -> dict[str, Any]:
    """Route a new conversation. Returns execution_routing.select's result: provider, model, route_id, band, … or error."""
    raw = json.dumps(body)
    fit = {"known": True, "assembled": True, "input_tokens": len(raw) // 4, "input_bytes": len(raw.encode()),
           "source": "provider_request_estimate"}         # chars / 4; the CLI's own tokenizer is not reachable here
    r = execution_routing.select(
        _NoClaudeWhenDegraded(core), prompt=request_text(body.get("messages") or [], standalone=not body.get("tools")),
        session_id=conversation_id, platform=PLATFORM, face="provider", entrypoint="provider", input_fit=fit,
        constraints={"allowed_routes": list(served_routes(core))},
        credential_resolver=_credentials(claude_bin), log_platform=f"provider:{client}")
    if r.get("error") and r.get("degraded"):
        r["error"] = ("S1 is unavailable, and a degraded scorer never defaults to Claude; no other lane is served yet. "
                      + r["error"])
    return r
