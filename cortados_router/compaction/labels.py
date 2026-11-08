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
# Characters shlex emits as punctuation tokens. "\n" is among them (and removed from the whitespace set) so an
# unquoted newline survives as a command boundary: as whitespace it vanished and `git status\ngit -C /repo commit`
# read as one `git status` with extra arguments.
PUNCTUATION = "();<>|&\n"
# Any token made only of PUNCTUATION chars that is not a redirect ends the command (";", "&&", "|&", "(", "\n", ...).
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
# `>&` writes a file (`echo hi >&out.txt`) unless its target is a descriptor: digits (`2>&1`, `>&2`) or `-` (close).
DUP_REDIRECT = ">&"
# Wrappers whose arguments run the real program; each maps to the wrapper options that take a separate argument,
# skipped together with their value so `sudo -u user rm x` or `env -u HOME rm x` still reach `rm`.
WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U"},
    "env": {"-u", "-C", "-S", "-P", "--unset", "--chdir", "--split-string"},
    "exec": {"-a"},
    "time": set(),
    "command": set(),
    "nohup": set(),
    "nice": {"-n", "--adjustment"},
}
# `timeout DURATION cmd`: the first non-option argument is the duration, not the program.
WRAPPERS_WITH_POSITIONAL = {"timeout": {"-s", "-k", "--signal", "--kill-after"}}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


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


def _unwrap(seg: list[str]) -> list[str]:
    """Strip VAR=value assignments and wrapper programs (with their options) until the real program is first.
    Iterative on purpose: `env FOO=1 git commit` carries the assignment AFTER the wrapper, and `sudo env X=1 cmd`
    nests wrappers. Every iteration drops at least one token, so the loop ends."""
    i = 0
    while i < len(seg):
        t = seg[i]
        if ASSIGNMENT.match(t):
            i += 1
            continue
        name = os.path.basename(t)
        if name not in WRAPPERS and name not in WRAPPERS_WITH_POSITIONAL:
            break
        with_arg = WRAPPERS[name] if name in WRAPPERS else WRAPPERS_WITH_POSITIONAL[name]
        i += 1
        while i < len(seg) and seg[i].startswith("-"):
            if seg[i] == "--":
                i += 1
                break
            i += 2 if seg[i] in with_arg else 1
        if name in WRAPPERS_WITH_POSITIONAL:
            i += 1                  # the positional (timeout's DURATION)
    return seg[i:]


def _segment_mutates(seg: list[str]) -> bool:
    seg = _unwrap(seg)
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


def _tokens(cmd: str) -> tuple[list[str], bool]:
    """Shell tokens of `cmd` and whether the whole command parsed. On a parse error (unbalanced quote, trailing
    backslash) the tokens emitted BEFORE the error are returned: those commands are complete and a shell may well have
    run them before reaching the bad line, so they still get classified instead of being lost to the fallback."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=PUNCTUATION)
    lex.whitespace_split = True
    lex.whitespace = " \t\r"       # not "\n": the newline is a punctuation token (command boundary), see PUNCTUATION
    lex.commenters = ""            # a comment would swallow its newline and glue the next line onto this command
    tokens: list[str] = []
    try:
        for t in lex:
            tokens.append(t)
    except ValueError:
        return tokens, False
    return tokens, True


def _tokens_mutate(tokens: list[str]) -> bool:
    seg: list[str] = []
    for i, t in enumerate(tokens):
        if t and all(c in PUNCTUATION for c in t):
            op = t.replace("\n", "")   # ";\n", "&&\n", "\n>" ... : the operator, if any, decides
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if op in WRITE_REDIRECTS:
                if nxt != "/dev/null":
                    return True
                continue
            if op == DUP_REDIRECT:
                if not (nxt.isdigit() or nxt == "-") and nxt != "/dev/null":
                    return True    # `>&file` creates/truncates file; `2>&1` / `>&-` only move descriptors
                continue
            if op.startswith("<"):
                continue           # input redirects (`<`, `<<`, `<<<`, `<&`) read; the word after them stays an argument
            if _segment_mutates(seg):
                return True
            seg = []
            continue
        seg.append(t)
    return _segment_mutates(seg)


def command_has_side_effect(cmd: str) -> bool:
    tokens, complete = _tokens(cmd)
    if _tokens_mutate(tokens):
        return True
    if not complete:
        return bool(SIDE_EFFECT_COMMAND.search(cmd))   # the unparsed tail: regex is the best remaining evidence
    return False


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
