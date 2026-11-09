"""Deterministic shadow pruning rules (phase 1): a KeepDecision for every past tool event of a session.

SHADOW ONLY: decisions are appended to $CORTADOS_HOME/compaction/decisions/decisions-<UTC day>.<n>.jsonl (day shards,
rolled at store.DECISIONS_SHARD_MAX_BYTES, deleted by the store's retention like session files) with the chars/tokens
they WOULD have saved; nothing here edits what any backend sees. Shared thresholds (keep the last N results, the pruning start
min(cap, pct x W), the minimum tokens freed, chars per token by source) come from tables/compaction.json through
compaction/policy.py (one source of truth with phase 0); tables/compaction-prune.json holds only this phase's rule data
(tool lists, read-only Bash command shapes, MCP side-effect words, small-result N, dropped-result head H, assumed window).

Rules, first match wins:
  pinned          events of the first message, and the last `pruning.keep_last_results` tool events: keep both
  error           an error result is kept (the model must remember what failed)
  side_effect     Edit/Write/..., non-read-only Bash, MCP tools that send/post/create: keep call (and result unless
                  a later rule says the result is a re-runnable read, which a side effect never is)
  superseded      a re-runnable read later repeated with the same path / command: keep the call, drop the result
  small           result under N chars: keep (dropping it saves nothing)
  not_rerunnable  a tool whose result cannot be fetched again: keep
  rerunnable_read Read/Grep/Glob/LS/WebFetch/WebSearch/read-only Bash outside the pin window: drop, keep a head of H
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
import shlex
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from cortados_router.compaction import store
from cortados_router.compaction.events import KeepDecision, Session, ToolEvent

logger = logging.getLogger(__name__)

TABLE_PATH = Path(__file__).resolve().parents[2] / "tables" / "compaction-prune.json"
# Chains, pipes, redirections, substitutions and line breaks: not obviously read-only. Newline and CR are separators
# to the shell but whitespace to shlex.split, which would turn "ls\nrm x" into the tokens of one ls call.
_CHAIN = re.compile(r"[;&|<>`\n\r]|\$\(")
# Characters the shell expands OUTSIDE quotes into words shlex never sees: globs (`*?[`: a file named "-delete" turns
# `find . -name x *` into `find . -name x -delete`), brace expansion (`{-delete,}`) and history (`!`).
_UNQUOTED_EXPANSION = frozenset("*?[{!")


def _has_shell_expansion(cmd: str) -> bool:
    """True when the shell could rewrite the words of `cmd` before running it, so its shlex tokens are not the argv
    the command gets and its shape is uncertain. Why: `find . $FIND_ARGS` tokenized as `find . $FIND_ARGS` (one
    positional) and passed the allowlist, while the shell ran it with FIND_ARGS='-exec touch marker {} +'. Rejected:
    `$` and backticks anywhere except inside single quotes (parameter, command and arithmetic expansion, $'..' and
    $".." quoting; double quotes still expand them, and an escaped one in double quotes is rejected too rather than
    modelled), and the characters of _UNQUOTED_EXPANSION outside any quotes unless backslash-escaped. Process
    substitution <( >( is rejected by _CHAIN. An unquoted tilde is rejected where the shell expands it: at the start
    of a word (`~`, `~user`, `~/x`, `~+`, `~-`) and right after an unquoted `=` or `:` (bash expands assignment-shaped
    arguments such as `x=~` by default; zsh expands `""~`, so empty quotes do not end that position). Why: tilde
    expansion yields the CLIENT's $HOME, which the prune cannot see; with HOME=-delete, `find . ~` deleted a marker
    under bash and zsh while the shape check saw a positional. A quoted or escaped tilde, or one inside a word
    (`HEAD~1`, `a\ ~`), is literal and allowed. Keeping `cat ~/notes.txt` costs tokens; pruning a destructive call
    as a re-runnable read loses state, so the cheaper error is chosen."""
    quote = None                                  # None, "'" or '"'
    word_start = True                             # nothing but whitespace, = / : or empty quotes since the last word
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                word_start = False
        elif quote == '"':
            if c in "$`":
                return True
            if c == "\\":
                if cmd[i + 1:i + 2] in ("$", "`"):
                    return True                   # escaped expansion: rejected, not modelled (see the docstring)
                i += 1                            # \" and \\ do not end or change the double-quoted string
                word_start = False
            elif c == '"':
                quote = None
            else:
                word_start = False
        else:
            if c in "$`" or c in _UNQUOTED_EXPANSION:
                return True
            if c == "~" and word_start:
                return True
            if c == "\\":
                i += 1                            # the next character is literal
                word_start = False
            elif c in "'\"":
                quote = c                         # an empty quoted string keeps the tilde-prefix position (zsh)
            else:
                word_start = c in " \t=:"
        i += 1
    return False


@lru_cache(maxsize=4)
def load_rules(path: str = str(TABLE_PATH)) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v["value"] for k, v in raw.items() if isinstance(v, dict) and "value" in v}


def load_table() -> dict[str, Any]:
    """Phase-1 rule data plus the shared values from tables/compaction.json (policy.py), merged read-only."""
    pol = store.load_policy()
    return {**load_rules(), "pruning": dict(pol["pruning"]), "chars_per_token": dict(pol["chars_per_token"])}


def _bash_command(ev: ToolEvent) -> Optional[str]:
    if ev.name in ("Bash", "shell", "exec_command", "local_shell"):
        cmd = ev.input.get("command", ev.input.get("cmd"))
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return str(cmd or "").strip()
    return None


def readonly_bash(cmd: str, table: dict[str, Any]) -> bool:
    """True only when the command matches an allowlisted shape (tables/compaction-prune.json readonly_bash): the
    shlex tokens start with a listed command ("git log" takes two tokens), every flag is listed, and positionals
    appear only where the shape allows them. Everything else, including any command holding a shell separator or a
    line break (a second command may hide after it) or a shell expansion that could change its words
    (_has_shell_expansion), is not read-only: the shape is checked only once the tokens are the real argv."""
    if not cmd or _CHAIN.search(cmd) or _has_shell_expansion(cmd):
        return False
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    if not toks:
        return False
    shapes = table["readonly_bash"]
    if len(toks) >= 2 and f"{toks[0]} {toks[1]}" in shapes:
        shape, args = shapes[f"{toks[0]} {toks[1]}"], toks[2:]
    elif toks[0] in shapes and toks[0] != "git":
        shape, args = shapes[toks[0]], toks[1:]
    else:
        return False
    flags = set(shape["flags"])
    for a in args:
        if a == "--":
            continue
        if a.startswith("--"):
            if a.split("=", 1)[0] not in flags:
                return False
        elif a.startswith("-") and len(a) > 1:
            if a in flags:
                continue
            if shape.get("numeric") and a[1:].isdigit():
                continue
            # a short-flag cluster (-rn) is allowed when every letter is a listed single-letter flag
            if not all(f"-{c}" in flags for c in a[1:]):
                return False
        elif not shape.get("positionals"):
            return False
    return True


def is_side_effect(ev: ToolEvent, table: dict[str, Any]) -> bool:
    if ev.name in table["side_effect_tools"]:
        return True
    cmd = _bash_command(ev)
    if cmd is not None:
        return not readonly_bash(cmd, table)
    if ev.name.startswith("mcp__"):
        words = re.split(r"[_\-]+", ev.name.split("__")[-1].lower())
        return any(w in table["mcp_side_effect_words"] for w in words)
    return False


def is_rerunnable(ev: ToolEvent, table: dict[str, Any]) -> bool:
    if ev.name in table["rerunnable_tools"]:
        return True
    cmd = _bash_command(ev)
    return cmd is not None and readonly_bash(cmd, table)


def read_key(ev: ToolEvent) -> str:
    """What a later identical read would repeat: the path for file reads, the command for shell, else the input."""
    cmd = _bash_command(ev)
    if cmd is not None:
        return "cmd:" + cmd
    for k in ("file_path", "path", "notebook_path", "url"):
        if k in ev.input and ev.name in ("Read", "NotebookRead", "LS", "WebFetch"):
            return f"path:{ev.input[k]}"
    return ev.name + ":" + json.dumps(ev.input, sort_keys=True, default=str)


def decide(session: Session, *, upto_message: Optional[int] = None,
           table: Optional[dict[str, Any]] = None) -> list[tuple[KeepDecision, int]]:
    """(decision, chars it would save) for every tool event issued at or before message `upto_message` (default:
    the whole session)."""
    t = table or load_table()
    n_msgs = len(session.messages) if upto_message is None else upto_message + 1
    events = [e for e in session.events if e.message_index < n_msgs]
    keep_last = int(t["pruning"]["keep_last_results"])
    recent = {e.index for e in events[-keep_last:]} if keep_last > 0 else set()
    head = int(t["dropped_head_chars"])
    later: dict[str, int] = {}
    for e in events:
        if is_rerunnable(e, t):
            later[read_key(e)] = e.index                 # last index of each read key
    out = []
    for e in events:
        def mk(keep_call: bool, keep_result: bool, rule: str, reason: str) -> tuple[KeepDecision, int]:
            saved = 0 if keep_result else max(0, e.result_chars - head)
            return (KeepDecision(session=session.session, event_index=e.index, keep_call=keep_call,
                                 keep_result=keep_result, decided_by="rule:" + rule, reason=reason), saved)
        if e.message_index == 0 or e.index in recent:
            out.append(mk(True, True, "pinned", f"in the first message or one of the last {keep_last} tool results"))
        elif e.is_error:
            out.append(mk(True, True, "error", "error results are kept"))
        elif is_side_effect(e, t):
            out.append(mk(True, True, "side_effect", f"{e.name} changed state"))
        elif is_rerunnable(e, t) and later.get(read_key(e), e.index) > e.index:
            out.append(mk(True, False, "superseded", "a later call repeats this read"))
        elif e.result_chars < int(t["small_result_chars"]):
            out.append(mk(True, True, "small", f"result under {t['small_result_chars']} chars"))
        elif not is_rerunnable(e, t):
            out.append(mk(True, True, "not_rerunnable", f"{e.name} cannot be re-run for the same result"))
        else:
            out.append(mk(True, False, "rerunnable_read", f"re-runnable read outside the pin window; keep a {head}-char head"))
    return out


def decisions_dir() -> Path:
    return store.decisions_dir()


def decision_files() -> list[Path]:
    """The decisions shards, oldest day and lowest sequence first (the harvest reads them in this order)."""
    d = decisions_dir()
    if not d.is_dir():
        return []
    found = []
    for p in d.iterdir():
        m = store.SHARD_NAME.match(p.name)
        if m:
            found.append((m.group(1), int(m.group(2)), p))
    return [p for _, _, p in sorted(found)]


def session_chars(sess: Session) -> int:
    """Characters the session's context carries (message text, tool inputs, tool results as recorded lengths)."""
    return (sum(len(m.text) for m in sess.messages) + sum(e.result_chars for e in sess.events)
            + sum(len(json.dumps(e.input, default=str)) for e in sess.events))


def gate(sess: Session, pairs: list, t: dict[str, Any]) -> dict[str, Any]:
    """Would phase 1 have pruned here? The session's estimated tokens against the start threshold
    min(start_cap_tokens, start_pct x W), and the tokens the drops free against min_freed_tokens."""
    cpt = float(t["chars_per_token"].get(sess.source, t["chars_per_token"]["unknown"]))
    pr = t["pruning"]
    window = float(t["assumed_window_tokens"])
    start = min(float(pr["start_cap_tokens"]), float(pr["start_pct"]) * window / 100.0)
    tokens = math.ceil(session_chars(sess) / cpt)
    freed = math.ceil(sum(saved for _, saved in pairs) / cpt)
    crossed = tokens >= start
    return {"chars_per_token": cpt, "session_tokens_est": tokens, "start_threshold_tokens": start,
            "assumed_window_tokens": window, "would_start": crossed, "freed_tokens_est": freed,
            "min_freed_tokens": pr["min_freed_tokens"], "would_prune": crossed and freed >= pr["min_freed_tokens"]}


def shadow_decide(session_path, *, now: Optional[float] = None) -> list[KeepDecision]:
    """Decide for every past tool event of the stored session and append the decisions (0600) with the chars and
    estimated tokens they WOULD have saved, each carrying the session-level gate (would the pruning start threshold
    have been crossed here, would the min-freed rule have allowed the prune) so labels can be joined later."""
    sess = store.read_session(session_path)
    t = load_table()
    pairs = decide(sess, table=t)
    g = gate(sess, pairs, t)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    cpt = g["chars_per_token"]
    lines = "".join(json.dumps({**dataclasses.asdict(d), "ts": ts, "source": sess.source, "saved_chars": saved,
                                "saved_tokens_est": math.ceil(saved / cpt), "mode": "shadow", "gate": g}) + "\n"
                    for d, saved in pairs)
    if lines:
        store.append_decisions(lines, now)
    return [d for d, _ in pairs]


def shadow_decide_async(conversation_id: str) -> bool:
    """Queue shadow_decide for a closed conversation on the store's single writer thread (serialized with writes and
    retention, off the request path). Never raises and never blocks: called from Registry cleanup, which must always
    finish. No-op when the store is off; a conversation that left no file is skipped on the writer."""
    try:
        if not store.enabled():
            return False
        name = store.session_file_name(str(conversation_id))

        def job():
            path = store.sessions_dir() / name
            try:
                shadow_decide(path)
            except FileNotFoundError:
                return                      # the conversation recorded nothing (or retention removed it)

        return store.STORE.submit(f"shadow decisions for {name}", job)
    except Exception as exc:   # contained: Registry cleanup must not be interrupted
        logger.warning("compaction shadow decisions for %s not scheduled: %s: %s",
                       str(conversation_id)[:8], type(exc).__name__, exc)
        return False
