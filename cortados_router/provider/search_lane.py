"""The search lane: a server-tool-only web search request (Claude Code's WebSearch) run on a headless Claude CLI whose
ONLY tool is the CLI's own WebSearch.

This is the one owner-approved exception (2026-11-03) to bridge.py's rule that the headless CLI never runs a tool
itself. It is narrow on purpose: the child gets no other tool, no MCP servers, no settings and no hooks, runs in an
empty scratch dir, and answers one prompt. Anthropic's server tools cannot be bridged (the client never runs them), so
without this lane the client could only be told the search did not happen.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

from cortados_router.provider import bridge

# The one-shot search CLI's scratch dir prefix (tempfile adds a random suffix). Its transcript, on the default login,
# lands under ~/.claude/projects/<slug of that dir>/; orchestrator_usage skips it by this prefix (provider work).
SCRATCH_PREFIX = "cortados-search-"

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
# A search query is a sentence; anything longer is a pasted document, and it would ride in argv (below) and spend
# quota on a prompt no search engine takes. Refused rather than truncated, so the model learns why.
MAX_QUERY_CHARS = 2000
QUERY_PREFIX = "perform a web search for the query:"
PROMPT = ("Search the web for: {query}\n\n"
          "Use the WebSearch tool. Reply with the results as a list, one per line: title, URL, and a one-line summary. "
          "Then give a short answer to the query from those results. Only list URLs that the search actually "
          "returned; never invent or guess a URL. If the search returns nothing, say so.")
# The client's web_search tool may restrict domains (allowed_domains / blocked_domains). The CLI's WebSearch takes no
# such flag here, so the restriction is carried in the prompt: a best-effort instruction to the search model, not an
# enforced filter.
ALLOWED_NOTE = "\n\nOnly use results from these domains: {domains}."
BLOCKED_NOTE = "\n\nNever use results from these domains: {domains}."

# Byte caps on what the child may write, drained incrementally (never buffered whole): a stream-json run of one search
# with a few fetched pages is well under 1 MiB, so 4 MiB of stdout is a runaway or hostile CLI, and up to
# MAX_CONCURRENT of them must not hold unbounded memory in the provider. stderr only feeds a 300-char diagnostic, so
# 256 KiB is already generous. Past a cap the child is killed and the search fails as output_too_large.
STDOUT_CAP = 4 * 1024 * 1024
STDERR_CAP = 256 * 1024
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


def _domains(v: Any) -> list[str]:
    return [d.strip() for d in v if isinstance(d, str) and d.strip()] if isinstance(v, list) else []


def domain_filters(body: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(allowed_domains, blocked_domains) of the request's web_search_* server tool; empty lists when it sets none."""
    for t in (body.get("tools") if isinstance(body, dict) and isinstance(body.get("tools"), list) else []):
        if isinstance(t, dict) and str(t.get("type", "")).startswith("web_search"):
            return _domains(t.get("allowed_domains")), _domains(t.get("blocked_domains"))
    return [], []


def prompt(query: str, allowed: list[str] | None = None, blocked: list[str] | None = None) -> str:
    text = PROMPT.format(query=query)
    if allowed:
        text += ALLOWED_NOTE.format(domains=", ".join(allowed))
    if blocked:
        text += BLOCKED_NOTE.format(domains=", ".join(blocked))
    return text


def argv(claude_bin: str | list[str], query: str, allowed: list[str] | None = None,
         blocked: list[str] | None = None) -> list[str]:
    prefix = [claude_bin] if isinstance(claude_bin, str) else list(claude_bin)
    # The query rides in argv, so it is visible to `ps` on the provider host: the same exposure bridge.py accepts for
    # the client's system prompt. Kept deliberately; the provider host is the owner's own machine.
    return prefix + ["-p", prompt(query, allowed, blocked), "--model", SEARCH_MODEL,
                     "--tools", "WebSearch", "--allowedTools", "WebSearch",
                     # stream-json (needs --verbose in -p mode) so the WebSearch tool_use/tool_result events are seen:
                     # a final result alone cannot show that a search actually ran (see search_evidence)
                     "--output-format", "stream-json", "--verbose", "--setting-sources", "",
                     # no hooks either (as bridge.py): a user hook must not run inside the one lane that runs a tool
                     "--settings", json.dumps({"disableAllHooks": True}),
                     "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
                     "--permission-mode", "dontAsk"]


def _usage_int(v: Any) -> int | None:
    """A usage count from the CLI's JSON; None for non-numbers and for NaN/Infinity (json.loads accepts both)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return max(0, int(v))


def _events(stdout: str) -> list[dict[str, Any]]:
    """The JSON objects of a stream-json run, one per line; lines that are not JSON objects are skipped."""
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    msg = event.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def search_evidence(events: list[dict[str, Any]]) -> bool:
    """True when the run shows a WebSearch that ran: an assistant `tool_use` named WebSearch answered by a user
    `tool_result` for the same id that is not an error and carries content. A nonempty final text is not evidence:
    the model can answer from memory (and invent URLs) without searching. The result's usage.server_tool_use
    .web_search_requests is not used: a live run (2026-11-03) reported 0 there although a search happened."""
    ids = {b.get("id") for e in events if e.get("type") == "assistant" for b in _blocks(e)
           if b.get("type") == "tool_use" and b.get("name") == "WebSearch" and b.get("id")}
    for e in events:
        if e.get("type") != "user":
            continue
        for b in _blocks(e):
            if (b.get("type") == "tool_result" and b.get("tool_use_id") in ids and not b.get("is_error")
                    and bridge._result_text(b.get("content")).strip()):
                return True
    return False


def _kill_group(p: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (it was started in its own session). Called BEFORE the child is reaped:
    while the leader is unreaped (alive or a zombie) its pid, and so the group id, cannot be reused, so the signal can
    only reach this child's group. ProcessLookupError means the group is already empty; PermissionError is what macOS
    reports for a group whose only member is the unreaped zombie leader (seen 2026-11-03). Both: nothing to kill."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _too_large() -> SearchFailed:
    return SearchFailed("output_too_large", f"the CLI wrote past {STDOUT_CAP} bytes of stdout or {STDERR_CAP} bytes "
                                            "of stderr")


def _run_cli(cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """Run the CLI and drain both pipes on THIS thread: one selectors loop over the two non-blocking pipes, under one
    absolute deadline and STDOUT_CAP / STDERR_CAP. Returns (returncode, stdout, stderr) only when stdout reached EOF
    before the deadline. Raises SearchFailed("output_too_large"), ("timeout") or ("incomplete_output": the deadline
    passed with stdout still open, e.g. a detached grandchild in another session holds the pipe, so the capture may be
    cut short and is never taken as an answer), and SearchFailed("start_failed") when it cannot be started.

    No reader threads: a reader blocked on a pipe that something outside the killed group keeps open would never end,
    leaking its fd and buffer. Cleanup order: signal the group first, reap the child second (_kill_group says why),
    close both pipes last, on every path."""
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise SearchFailed("start_failed", str(exc)) from None
    deadline = time.monotonic() + timeout
    bufs = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": STDOUT_CAP, "stderr": STDERR_CAP}
    failure = None
    sel = None
    try:                                            # from here on every path signals, reaps and closes (finally)
        sel = selectors.DefaultSelector()           # can fail (EMFILE: kqueue needs an fd) with the child running
        for name, pipe in (("stdout", p.stdout), ("stderr", p.stderr)):
            os.set_blocking(pipe.fileno(), False)
            sel.register(pipe.fileno(), selectors.EVENT_READ, name)
        while sel.get_map():                        # until both pipes reach EOF
            left = deadline - time.monotonic()
            if left <= 0:
                failure = "deadline"
                break
            for key, _ in sel.select(timeout=left):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:             # spurious wakeup: nothing to read yet
                    continue
                if not chunk:
                    sel.unregister(key.fd)
                    continue
                buf = bufs[key.data]
                if len(buf) + len(chunk) > caps[key.data]:
                    failure = _too_large()          # stop holding its bytes; the finally kills the child
                    break
                buf += chunk
            if failure is not None:
                break
    finally:
        # Each step is guarded on its own, so one failing (a selector close error, an interrupted wait) can neither
        # skip the kill and reap nor leave either pipe open.
        for step in ((sel.close if sel is not None else None), lambda: _kill_group(p), p.wait,
                     p.stdout.close, p.stderr.close):
            if step is None:
                continue
            try:
                step()                              # order: kill before the reap (_kill_group says why), pipes last
            except Exception:
                logger.exception("search lane: cleanup step failed")
    if failure == "deadline":
        # Killed by us (still running at the deadline): a timeout. Exited by itself with stdout still held open
        # elsewhere: what was captured may be a prefix, so it is not an answer.
        failure = (SearchFailed("timeout", f"no answer in {timeout:.0f}s") if p.returncode == -signal.SIGKILL
                   else SearchFailed("incomplete_output", f"stdout still open {timeout:.0f}s after the start"))
    if failure is not None:
        raise failure
    return (p.returncode, bufs["stdout"].decode("utf-8", "replace"), bufs["stderr"].decode("utf-8", "replace"))


def check_query(query: str) -> None:
    """The query checks run() applies, callable before any quota is reserved: raises SearchFailed no_query /
    query_too_long."""
    if not query:
        raise SearchFailed("no_query", "the request carries no user text to search for")
    if len(query) > MAX_QUERY_CHARS:
        raise SearchFailed("query_too_long", f"{len(query)} chars, the limit is {MAX_QUERY_CHARS}")


@contextlib.contextmanager
def capacity():
    """Hold one search slot (non-blocking; raises SearchFailed busy when all are taken) for a run(..., slot_held=True)
    inside the block. The provider takes it before the quota floor reserves anything, so saturated search slots can
    never leave a reservation behind for a search that was refused."""
    if not _slots.acquire(blocking=False):
        raise SearchFailed("busy", f"{MAX_CONCURRENT} searches already running")
    try:
        yield
    finally:
        _slots.release()


def run(claude_bin: str | list[str], query: str, config_dir: str | None, *,
        timeout: float | None = None, allowed: list[str] | None = None,
        blocked: list[str] | None = None, slot_held: bool = False) -> SearchResult:
    """One search on a fresh CLI. Raises SearchFailed (busy, no_query, query_too_long, timeout, output_too_large,
    incomplete_output, exit_<n>, bad_output, cli_error, no_search, empty, start_failed); never returns an empty success."""
    timeout = SEARCH_TIMEOUT_S if timeout is None else timeout
    check_query(query)
    try:
        work = tempfile.mkdtemp(prefix=SCRATCH_PREFIX)
    except OSError as exc:
        raise SearchFailed("start_failed", f"no scratch dir: {exc}") from None
    # The slot is taken only after the scratch dir exists, and released in the same finally that removes it, so no
    # path between acquire and release can leave a slot taken forever. A caller already holding one (capacity())
    # does not take a second: the provider takes it before reserving quota, so a busy refusal reserves nothing.
    if not slot_held and not _slots.acquire(blocking=False):
        shutil.rmtree(work, ignore_errors=True)
        raise SearchFailed("busy", f"{MAX_CONCURRENT} searches already running")
    try:
        code, stdout, stderr = _run_cli(argv(claude_bin, query, allowed, blocked), cwd=work,
                                        env=bridge.claude_env(config_dir), timeout=timeout)
        if code != 0:
            raise SearchFailed(f"exit_{code}", (stderr or stdout or "").strip()[-300:])
        events = _events(stdout)
        out = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if out is None:
            raise SearchFailed("bad_output", "the CLI's output has no result event")
        if out.get("is_error"):
            raise SearchFailed("cli_error", str(out.get("result"))[:300])
        if not search_evidence(events):
            raise SearchFailed("no_search", "no search was performed")
        text = out.get("result")
        if not isinstance(text, str) or not text.strip():
            raise SearchFailed("empty", "the CLI returned no text")
        u = out.get("usage") if isinstance(out.get("usage"), dict) else {}
        usage = {k: n for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                 "cache_creation_input_tokens") if (n := _usage_int(u.get(k))) is not None}
        return SearchResult(text.strip(), usage)
    finally:
        if not slot_held:
            _slots.release()
        shutil.rmtree(work, ignore_errors=True)


@dataclass
class Login:
    """Where a search runs and what it is charged to, resolved together before launch so the two cannot disagree.
    config_dir None = the CLI runs on the provider's own default login. ledger_lane is always a lane: a login that
    cannot be tied to one fails in login() before launch (charging it to `claude` would be a guess)."""
    config_dir: str | None
    ledger_lane: str
    account: str | None = None                  # the accounts.json id the login belongs to (None: the default login)

    def floor_candidate(self) -> dict[str, Any]:
        """This login as the executing account floor.reserve checks (routing.floor_candidate's shape). The CLI runs on
        bridge.claude_env(config_dir) whether or not an account id is known, and floor.executing_login applies the
        config_dir only for a named account, so a config_dir always comes with one."""
        account = self.account or (self.ledger_lane if self.config_dir else None)
        suffix = self.ledger_lane.partition("#")[2]
        return {"lane": self.ledger_lane, "provider": "claude-cli" + (f"#{suffix}" if suffix else ""),
                "account": account, "config_dir": self.config_dir}


def login() -> Login:
    """The login a search runs on and the scarcity lane it draws on.

    - CORTADOS_PROVIDER_SEARCH_ACCOUNT names an account: its config_dir and its lane (claude#b), both taken from ONE
      row of ONE read of tables/accounts.json (core.account_row, the lookup Registry.claude_config_dir also uses), so a
      concurrent edit of the file cannot pair one snapshot's login with another's lane. A named account that cannot be
      resolved fails the search rather than falling back to the default login.
    - Otherwise, with accounts configured: the FIRST account, which is the bare `claude` lane, passed as an explicit
      config_dir. Explicit because the child would otherwise inherit whatever CLAUDE_CONFIG_DIR the provider runs
      with (possibly account b) while the ledger said `claude`; bridge.claude_env overrides an inherited one.
    - Otherwise (no accounts.json): the provider's own login, charged to `claude`, unless the provider's environment
      carries a CLAUDE_CONFIG_DIR. That login matches no configured account, so the search fails with no_claude_lane
      before the CLI is launched: no quota is spent that the ledger cannot record."""
    from cortados_router import core
    account = os.environ.get(ACCOUNT_ENV, "").strip()
    try:
        accounts = core._vendor_accounts("claude", "claude", "claude-cli")
    except Exception as exc:
        raise SearchFailed("no_claude_lane", f"tables/accounts.json is unreadable: {exc}") from None
    if account:
        row = core.account_row(accounts, account)
        cfg = row.get("config_dir") if row else None
        if not cfg:
            raise SearchFailed("no_claude_lane", f"account {account!r} has no resolvable login")
        lane = row.get("lane")
        if not lane:                                # same rule as below: never spend quota no ledger row can record
            raise SearchFailed("no_claude_lane", f"account {account!r} has no lane in tables/accounts.json")
        return Login(cfg, lane, row.get("account") or account)
    first = accounts[0]
    if first.get("config_dir"):
        return Login(first["config_dir"], first.get("lane") or "claude", first.get("account"))
    inherited = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if inherited:
        # A search spends quota; one that could not be charged to any lane is refused before launch, not run
        # unaccounted (the scarcity learner would never see that spend).
        raise SearchFailed("no_claude_lane", f"the provider's CLAUDE_CONFIG_DIR ({inherited}) matches no configured "
                                             "account, so the search could not be charged to a lane")
    return Login(None, "claude")
