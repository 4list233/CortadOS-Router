"""The search lane: a server-tool-only web search request (Claude Code's WebSearch) run on a headless Claude CLI whose
ONLY tool is the CLI's own WebSearch.

This is the one owner-approved exception (2026-11-03) to bridge.py's rule that the headless CLI never runs a tool
itself. It is narrow on purpose: the child gets no other tool, no MCP servers, no settings and no hooks, runs in an
empty scratch dir, and answers one prompt. Anthropic's server tools cannot be bridged (the client never runs them), so
without this lane the client could only be told the search did not happen.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Any

from cortados_router.provider import bridge

logger = logging.getLogger(__name__)

# The routing table's W-fast Claude route (tables/routing-table.json, lane `claude`): searching and summarising
# results needs no deep reasoning, and the small model spends the least subscription quota per search.
SEARCH_MODEL = "claude-haiku-4-5-20251001"
# A web search with a few fetches finishes well inside two minutes; past that the CLI is stuck, and the client (which
# waits on this HTTP request) is better served by the explicit unavailable reply than by an open-ended wait.
SEARCH_TIMEOUT_S = 120.0
# Each search is a whole CLI process on a subscription. A model that loops on WebSearch must not open unbounded
# CLIs; four covers Claude Code's parallel searches in one turn.
MAX_CONCURRENT = 4
# Optional: run searches on a named Claude account of tables/accounts.json (its config_dir), not the default login.
ACCOUNT_ENV = "CORTADOS_PROVIDER_SEARCH_ACCOUNT"
QUERY_PREFIX = "perform a web search for the query:"
PROMPT = ("Search the web for: {query}\n\n"
          "Use the WebSearch tool. Reply with the results as a list, one per line: title, URL, and a one-line summary. "
          "Then give a short answer to the query from those results. Only list URLs that the search actually "
          "returned; never invent or guess a URL. If the search returns nothing, say so.")

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class SearchFailed(Exception):
    """The search lane did not produce results; `kind` is the failure class shown to the model and logged."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


@dataclass
class SearchResult:
    text: str
    usage: dict[str, int]


def extract_query(body: dict[str, Any]) -> str:
    """The search query of a Messages body: the last user message's text, without Claude Code's "Perform a web
    search for the query:" lead-in. Empty when there is no user text."""
    msgs = body.get("messages") if isinstance(body, dict) else None
    for m in reversed(msgs if isinstance(msgs, list) else []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        text = c if isinstance(c, str) else " ".join(
            b.get("text", "") for b in (c if isinstance(c, list) else [])
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
        text = text.strip()
        if text.lower().startswith(QUERY_PREFIX):
            text = text[len(QUERY_PREFIX):].strip()
        return text
    return ""


def argv(claude_bin: str | list[str], query: str) -> list[str]:
    prefix = [claude_bin] if isinstance(claude_bin, str) else list(claude_bin)
    return prefix + ["-p", PROMPT.format(query=query), "--model", SEARCH_MODEL,
                     "--tools", "WebSearch", "--allowedTools", "WebSearch",
                     "--output-format", "json", "--setting-sources", "",
                     "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
                     "--permission-mode", "dontAsk"]


def run(claude_bin: str | list[str], query: str, config_dir: str | None, *,
        timeout: float | None = None) -> SearchResult:
    """One search on a fresh CLI. Raises SearchFailed (busy, no_query, timeout, exit_<n>, bad_output, empty,
    start_failed); never returns an empty success."""
    timeout = SEARCH_TIMEOUT_S if timeout is None else timeout
    if not query:
        raise SearchFailed("no_query", "the request carries no user text to search for")
    if not _slots.acquire(blocking=False):
        raise SearchFailed("busy", f"{MAX_CONCURRENT} searches already running")
    work = tempfile.mkdtemp(prefix="cortados-search-")
    try:
        try:
            p = subprocess.run(argv(claude_bin, query), cwd=work, env=bridge.claude_env(config_dir),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SearchFailed("timeout", f"no answer in {timeout:.0f}s") from None
        except OSError as exc:
            raise SearchFailed("start_failed", str(exc)) from None
        if p.returncode != 0:
            raise SearchFailed(f"exit_{p.returncode}", (p.stderr or p.stdout or "").strip()[-300:])
        try:
            out = json.loads(p.stdout)
        except ValueError:
            raise SearchFailed("bad_output", "the CLI's output is not JSON") from None
        if not isinstance(out, dict) or out.get("is_error"):
            raise SearchFailed("cli_error", str(out.get("result") if isinstance(out, dict) else out)[:300])
        text = out.get("result")
        if not isinstance(text, str) or not text.strip():
            raise SearchFailed("empty", "the CLI returned no text")
        u = out.get("usage") if isinstance(out.get("usage"), dict) else {}
        usage = {k: int(u[k]) for k in ("input_tokens", "output_tokens") if isinstance(u.get(k), (int, float))}
        return SearchResult(text.strip(), usage)
    finally:
        _slots.release()
        shutil.rmtree(work, ignore_errors=True)


def config_dir_for(registry) -> str | None:
    """The login the search runs on: the account named by CORTADOS_PROVIDER_SEARCH_ACCOUNT (resolved through the
    Registry, the same tables/accounts.json lookup routed Claude conversations use), else the default login, which
    is what the bridge uses for the bare `claude` lane. A named account that cannot be resolved fails the search
    rather than falling back to the default login."""
    account = os.environ.get(ACCOUNT_ENV, "").strip()
    if not account:
        return None
    cfg = registry.claude_config_dir(account)
    if not cfg:
        raise SearchFailed("no_claude_lane", f"account {account!r} has no resolvable login")
    return cfg
