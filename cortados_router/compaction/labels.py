"""Automatic keep labels for past tool calls, read off what the session did afterwards (stdlib only).

For each ToolEvent e of a Session:
- needed_result: dropping e's result would have cost something later. True when a later event re-reads the same file
  or re-runs the same normalized command/query (rule "rerun": the agent had to fetch it again), when a later message
  or later tool input quotes a distinctive line (>= QUOTE_MIN chars) of e's result (rule "quote"), or when it uses a
  distinctive identifier (hash, path, id-like token) whose first appearance in the session is e's result (rule
  "identifier"). False when none of that happens before the session ends.
- needed_call: needed_result, or later text names e's input target (path / command / query; rule "target"), or e had
  side effects (rule "side_effect": forgetting that a write or a commit happened misleads the agent).

Events whose issuing message lies in the last `horizon` messages are labelled "unknown": there is not enough future to
say they were never needed, and calling them "not needed" would teach the heads to drop recent work.

Only LATER information decides a label; the S1 dataset (s1/compaction_dataset.py) must never show it to the model.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any, Iterable, Optional

from .events import Session, ToolEvent

QUOTE_MIN = 40          # a 40-char verbatim line is very unlikely to recur by chance; shorter lines ("}", "OK") do
QUOTE_MAX = 200         # compare at most the first 200 chars of a line: long lines are often re-quoted only in part
DEFAULT_HORIZON = 4     # the last 4 messages (about two turns) have too little future to call anything "never needed"

PATH_KEYS = ("file_path", "path", "notebook_path", "filename")
COMMAND_KEYS = ("command", "cmd")
QUERY_KEYS = ("pattern", "query", "url", "q")

# Tools whose call changes something outside the context: the fact that they ran must survive compaction.
SIDE_EFFECT_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch", "str_replace_editor", "create_file",
                     "delete_file", "todowrite"}
# Shell commands that change state. Conservative on purpose: a missed side effect drops a call that mattered.
SIDE_EFFECT_COMMAND = re.compile(
    r"(^|[;&|]\s*)(git\s+(commit|push|merge|rebase|checkout|reset|add|rm|mv|tag|stash)|rm|mv|cp|mkdir|touch|chmod|"
    r"pip\s+install|npm\s+(install|i)|brew\s+install|sed\s+-i|tee|curl\s+.*-X\s*(POST|PUT|DELETE|PATCH))\b"
    r"|[^>]>{1,2}\s*[\w./~-]", re.I)

# Tokenized check (preferred; the regex above is the fallback for commands shlex cannot parse). Git takes global
# options before its subcommand (`git -C /repo commit`), which a "git <sub>" regex misses.
GIT_MUTATING = {"commit", "push", "merge", "rebase", "checkout", "switch", "reset", "add", "rm", "mv", "tag", "stash",
                "cherry-pick", "revert", "pull", "clean", "restore", "apply", "am", "init", "clone", "fetch"}
GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
                     "--super-prefix", "--list-cmds"}
MUTATING_PROGRAMS = {"rm", "mv", "cp", "mkdir", "touch", "chmod", "chown", "ln", "tee", "rmdir", "truncate", "dd"}
INSTALLERS = {"pip": {"install", "uninstall"}, "pip3": {"install", "uninstall"}, "npm": {"install", "i", "ci", "uninstall"},
              "brew": {"install", "uninstall", "upgrade"}, "uv": {"add", "remove", "pip"}}
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


def _git_subcommand(args: list[str]):
    i = 0
    while i < len(args):
        a = args[i]
        if a in GIT_OPTS_WITH_ARG:
            i += 2
        elif a.startswith("-"):
            i += 1                  # --no-pager, --bare, -p, --git-dir=/x, --work-tree=/y, ...
        else:
            return a
    return None


def _segment_mutates(seg: list[str]) -> bool:
    while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
        seg = seg[1:]               # leading VAR=value assignments
    while seg and seg[0] in ("sudo", "env", "command", "exec", "time", "nohup"):
        seg = seg[1:]
    if not seg:
        return False
    prog, args = os.path.basename(seg[0]), seg[1:]
    if prog == "git":
        return _git_subcommand(args) in GIT_MUTATING
    if prog in MUTATING_PROGRAMS:
        return True
    if prog in INSTALLERS:
        return bool(args) and args[0] in INSTALLERS[prog]
    if prog in ("sed", "perl") and any(a == "-i" or a.startswith("-i") for a in args):
        return True
    if prog == "curl":
        up = [a.upper() for a in args]
        return any(m in up for m in ("POST", "PUT", "DELETE", "PATCH")) or any(
            a.upper().startswith("-X") and a[2:].upper() in ("POST", "PUT", "DELETE", "PATCH") for a in args)
    return False


def command_has_side_effect(cmd: str) -> bool:
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return bool(SIDE_EFFECT_COMMAND.search(cmd))   # unbalanced quotes etc.: fall back to the regex
    seg: list[str] = []
    for i, t in enumerate(tokens):
        if t in WRITE_REDIRECTS:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt != "/dev/null" and not nxt.startswith("&"):
                return True
            continue
        if t in SEPARATORS:
            if _segment_mutates(seg):
                return True
            seg = []
            continue
        seg.append(t)
    return _segment_mutates(seg)


# Distinctive identifiers: git-like hashes, uuids, slash paths, long id-like tokens mixing letters and digits.
IDENT_PATTERNS = (
    re.compile(r"\b[0-9a-f]{7,40}\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"(?:~|\.{0,2})?/(?:[\w.-]+/)+[\w.-]+"),
    re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{10,}\b"),
)


def _first(d: dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def normalize_command(cmd: str) -> str:
    return " ".join(cmd.split())


def target(e: ToolEvent) -> Optional[tuple[str, str]]:
    """(kind, normalized value) of what the call acted on, or None."""
    inp = e.input if isinstance(e.input, dict) else {}
    p = _first(inp, PATH_KEYS)
    if p:
        return ("path", p)
    c = _first(inp, COMMAND_KEYS)
    if c:
        return ("command", normalize_command(c))
    q = _first(inp, QUERY_KEYS)
    if q:
        return ("query", f"{e.name.lower()}:{normalize_command(q)}")
    return None


def has_side_effect(e: ToolEvent) -> bool:
    if e.name.lower() in SIDE_EFFECT_TOOLS:
        return True
    c = _first(e.input if isinstance(e.input, dict) else {}, COMMAND_KEYS)
    return bool(c and command_has_side_effect(c))


def _strings(v: Any) -> Iterable[str]:
    """Every string leaf of a tool input, decoded. Searching the JSON dump instead would hide reuse behind escapes:
    an Edit old_string quoting a line with `"` or `\\` appears as `\\"` / `\\\\` in JSON and no longer matches."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k in sorted(v, key=str):
            yield from _strings(v[k])
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)
    elif v is not None and not isinstance(v, bool):
        yield str(v)


def _input_text(e: ToolEvent) -> str:
    return "\n".join(_strings(e.input))


def distinctive_lines(text: str) -> list[str]:
    out, seen = [], set()
    for line in text.splitlines():
        s = line.strip()[:QUOTE_MAX]
        if len(s) >= QUOTE_MIN and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def identifiers(text: str) -> set[str]:
    found: set[str] = set()
    for pat in IDENT_PATTERNS:
        found.update(m.group(0) for m in pat.finditer(text))
    return {t for t in found if len(t) >= 7}


def label_session(session: Session, *, horizon: int = DEFAULT_HORIZON) -> list[dict[str, Any]]:
    """One label row per tool event, in event order."""
    msgs = sorted(session.messages, key=lambda m: m.index)
    events = sorted(session.events, key=lambda e: e.index)
    last_msg = msgs[-1].index if msgs else -1
    cutoff = last_msg - horizon          # events issued by a message after this index have too little future
    rows = []
    for e in events:
        later_msgs = [m for m in msgs if m.index > e.message_index]
        later_events = [x for x in events if x.index > e.index]
        row: dict[str, Any] = {
            "session": session.session, "source": session.source, "event_index": e.index,
            "message_index": e.message_index, "tool": e.name, "result_chars": e.result_chars,
            "horizon": {"messages": len(later_msgs), "events": len(later_events), "min_future_messages": horizon},
        }
        if e.message_index > cutoff:
            row.update(status="unknown", needed_call=None, needed_result=None,
                       evidence=[{"rule": "horizon", "detail": f"issued within the last {horizon} messages"}])
            rows.append(row)
            continue
        evidence = _result_evidence(e, events, msgs, later_msgs, later_events)
        needed_result = bool(evidence)
        call_ev = list(evidence)
        if has_side_effect(e):
            call_ev.append({"rule": "side_effect", "detail": e.name})
        tgt = target(e)
        if tgt and not evidence:
            hit = _mentions(tgt[1] if tgt[0] != "query" else tgt[1].split(":", 1)[1], later_msgs, later_events)
            if hit:
                call_ev.append({"rule": "target", **hit})
        row.update(status="labelled", needed_result=needed_result, needed_call=bool(call_ev), evidence=call_ev)
        rows.append(row)
    return rows


def _mentions(needle: str, later_msgs, later_events) -> Optional[dict[str, Any]]:
    for m in later_msgs:
        if needle in m.text:
            return {"later": "message", "index": m.index}
    for x in later_events:
        if needle in _input_text(x):
            return {"later": "event", "index": x.index}
    return None


def _result_evidence(e: ToolEvent, events, msgs, later_msgs, later_events) -> list[dict[str, Any]]:
    if e.interrupted or not e.result_text:
        return []                          # no result was ever in the context, so none can be needed
    ev: list[dict[str, Any]] = []
    tgt = target(e)
    if tgt:
        for x in later_events:
            # a later write to the same file is not a re-read; a re-run of a side-effect command is a new action
            if target(x) == tgt and not has_side_effect(x):
                ev.append({"rule": "rerun", "later": "event", "index": x.index, "detail": f"{tgt[0]} {tgt[1][:120]}"})
                break
    later_texts = [("message", m.index, m.text) for m in later_msgs] + \
                  [("event", x.index, _input_text(x)) for x in later_events]
    for line in distinctive_lines(e.result_text):
        hit = next(((k, i) for k, i, t in later_texts if line in t), None)
        if hit:
            ev.append({"rule": "quote", "later": hit[0], "index": hit[1], "detail": line[:80]})
            break
    # identifiers first seen in e's result: not in any earlier message, earlier event input/result, or e's own input
    before = [m.text for m in msgs if m.index <= e.message_index] + \
             [_input_text(x) + "\n" + x.result_text for x in events if x.index < e.index] + [_input_text(e)]
    before_text = "\n".join(before)
    fresh = sorted(t for t in identifiers(e.result_text) if t not in before_text)
    for tok in fresh:
        hit = next(((k, i) for k, i, t in later_texts if tok in t), None)
        if hit:
            ev.append({"rule": "identifier", "later": hit[0], "index": hit[1], "detail": tok[:80]})
            break
    return ev


def write_jsonl(rows: Iterable[dict[str, Any]], path) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
            n += 1
    return n
