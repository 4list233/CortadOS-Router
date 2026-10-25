"""Model choice for a provider conversation: S1 scores the request once, the router's execution policy picks the route.

Owner of two rules:
  SERVED_PROVIDERS  which routes this endpoint can serve. Choosing among them stays with execution_routing.select,
                    the router's one execution policy (S1 → band → cheapest capable route with live capacity), so the
                    endpoint is a host of the router, not a second router.
  no Claude when degraded  core.resolve's hard rule that a degraded scorer never defaults to Claude. Execution mode
                    skips the table's non-Claude fallback routes, so this host enforces the rule itself: while S1 is
                    down a Claude route is not offered; the conversation runs on a Codex lane, and when no Codex
                    lane survives (binary missing, capacity spent) the request is refused.
"""
from __future__ import annotations

import json
import re
import shutil
from typing import Any, Callable

from cortados_router import execution_routing
from cortados_router.provider import anthropic_wire as wire

# The Claude CLI (P1, bridge.py) and the Codex CLI (P2, codex_bridge.py). Local models need the 2026-09-28 "no local
# routes" ruling lifted and local entries in the generated model scores. `openai-codex` is deliberately absent: it is
# the Hermes native-API provider this endpoint cannot run; its rows are served as their `codex-cli` twin instead.
SERVED_PROVIDERS = ("claude-cli", "codex-cli")
CLAUDE_PROVIDERS = ("claude-cli",)
# The tables carry no codex-cli rows: core.resolve clones each openai-codex candidate into a codex-cli one under
# execution, but only after the allowed_routes filter, so the allowlist itself must name the clone.
CLONED_PROVIDERS = {"openai-codex": "codex-cli"}
# Coding clients are interactive sessions: the router's face floor (selection.interactive_min_general_index) applies.
PLATFORM = "cli"
# Client-injected context, not the request: Claude Code wraps its reminders and environment in these.
_INJECTED = re.compile(r"<(system-reminder|system-message)>.*?</\1>", re.S)


def _base_provider(p: str) -> str:
    """The backend a per-account provider maps to: `claude-cli#b` -> `claude-cli`."""
    return (p or "").split("#", 1)[0]


def served_routes(core) -> tuple[tuple[str, str], ...]:
    rows = [r for m in (core.model_scores() or {}).get("models", []) for r in m.get("reach", [])]
    rows += [r for band in core.table()["candidates"].values() for r in band]
    pairs = {(CLONED_PROVIDERS.get(r["provider"], r["provider"]), r["model"]) for r in rows}
    served = {p for p in pairs if p[0] in SERVED_PROVIDERS}
    # Expand each Claude pair across configured accounts (claude-cli, claude-cli#b, …) so a second-account candidate
    # survives the allowlist filter in execution_routing.select; one account leaves this unchanged.
    accts = core._vendor_accounts("claude", "claude", "claude-cli")
    if len(accts) > 1:
        served |= {(a["provider"], model) for (prov, model) in set(served) if prov == "claude-cli" for a in accts}
    return tuple(sorted(served))


def strip_injected(text: str) -> str:
    """A user message without the client's injected reminders: what S1 scores (also used by the S1 outcome miner)."""
    return _INJECTED.sub("", text).strip()


def request_text(messages: list[dict[str, Any]], *, standalone: bool = False) -> str:
    """What S1 scores, without the client's injected context: the conversation's first user message (the task the
    session is for), or for a standalone tool-less call (a title or summary) its own last user message."""
    users = [m for m in messages if m.get("role") == "user"]
    pick = (users[-1] if standalone else users[0]) if users else {}
    text = "\n".join(b.get("text", "") for b in wire._blocks(pick.get("content")) if b.get("type") == "text")
    return strip_injected(text)


class _NoClaudeWhenDegraded:
    """`core` for execution_routing.select, except that a degraded decision cannot resolve to a Claude route."""

    def __init__(self, core):
        self._core = core

    def __getattr__(self, name):
        return getattr(self._core, name)

    def resolve(self, decision, *args, allowed_routes=None, **kw):
        if decision.get("degraded") or not decision.get("answers"):
            allowed_routes = tuple(p for p in (allowed_routes or ()) if _base_provider(p[0]) not in CLAUDE_PROVIDERS)
        return self._core.resolve(decision, *args, allowed_routes=allowed_routes, **kw)


def _credentials(claude_bin: str, codex_bin: str) -> Callable[..., None]:
    # A PATH check only: a real login check would open the vendor CLI (and for Codex spend a login refresh), which
    # the scarcity sampler alone may do. A missing login surfaces as a 502 on the first turn instead.
    binaries = {"claude-cli": claude_bin, "codex-cli": codex_bin}

    def check(*, requested: str, target_model: str) -> None:
        base = _base_provider(requested)        # a per-account provider (claude-cli#b) shares the base binary/login check
        if base not in SERVED_PROVIDERS:
            raise execution_routing.CredentialsUnavailable(f"{requested} is not served by this endpoint")
        if not shutil.which(binaries[base]):
            raise execution_routing.CredentialsUnavailable(f"{binaries[base]} is not on PATH")
    return check


def choose(core, body: dict[str, Any], *, client: str, conversation_id: str, claude_bin: str = "claude",
           codex_bin: str = "codex") -> dict[str, Any]:
    """Route a new conversation. Returns execution_routing.select's result: provider, model, route_id, band, … or error."""
    raw = json.dumps(body)
    fit = {"known": True, "assembled": True, "input_tokens": len(raw) // 4, "input_bytes": len(raw.encode()),
           "source": "provider_request_estimate"}         # chars / 4; the CLI's own tokenizer is not reachable here
    r = execution_routing.select(
        _NoClaudeWhenDegraded(core), prompt=request_text(body.get("messages") or [], standalone=not body.get("tools")),
        session_id=conversation_id, platform=PLATFORM, face="provider", entrypoint="provider", input_fit=fit,
        constraints={"allowed_routes": list(served_routes(core))},
        credential_resolver=_credentials(claude_bin, codex_bin), log_platform=f"provider:{client}")
    if r.get("error") and r.get("degraded"):
        r["error"] = ("S1 is unavailable, and a degraded scorer never defaults to Claude; no Codex lane survived either "
                      "(codex-cli missing from PATH, or its capacity spent). " + r["error"])
    return r
