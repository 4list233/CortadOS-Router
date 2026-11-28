"""Steering notes for a running child task (docs/STEER-TASK.md): the per-task note store and the child's hook.

The requester (child_tasks.steer, on the task's own host) appends a note to `<task dir>/steer.jsonl`. The child's
settings file runs this file as its PostToolUse / PostToolUseFailure hook (`--hook <task dir>`): after each tool call of
the child's MAIN loop it takes the pending notes, marks them delivered in `<task dir>/steer-delivered.jsonl`, then
prints them as the hook's `additionalContext`. Marked before printed, under one flock: a note is delivered at most once,
and once in practice.

STANDALONE: the hook runs as `python3 -I -S -B steer.py --hook <dir>` (isolated mode: no script dir on sys.path, no
site), so this file imports the standard library only; child_tasks imports it as cortados_router.steer.

A note is text for the model, never a permission: the hook returns `additionalContext` only (never a decision,
permissionDecision, updatedInput, updatedToolOutput or classifierContext) and always exits 0.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import uuid

NOTES = "steer.jsonl"                   # in the task dir: {"id", "at", "text"} per line, appended by the requester
DELIVERED = "steer-delivered.jsonl"     # in the task dir: {"id", "at"} per line, appended by the hook before it prints
LOCK = "steer.lock"                     # in the task dir: exclusive flock around every read-modify-write of the two
MAX_NOTE_CHARS = 2000                   # owner decision 2026-11-27 ("e.g. 2000 chars, printable")
MAX_PENDING = 10                        # notes waiting per task: more would pile up unread ([ESCALATE] 2)
MAX_CONTEXT_CHARS = 8000                # injected per tool boundary; the rest waits for the next one ([ESCALATE] 2)
MAX_FILE_BYTES = 1 << 20                # a notes file past this is not ours to parse (10 x 2000 chars is ~80 KiB)
PREFIX = "Steering note from the owner's orchestrator: "
HOOK_EVENTS = ("PostToolUse", "PostToolUseFailure")
ID_HEX = 32


class SteerError(ValueError):
    """A note or a task dir this module refuses; the message says why."""


def check_text(text) -> str:
    """The note as it will be stored: trimmed, 1..MAX_NOTE_CHARS characters, printable plus newline and tab."""
    if not isinstance(text, str):
        raise SteerError("message must be a string")
    t = text.strip()
    if not t:
        raise SteerError("message is empty")
    if len(t) > MAX_NOTE_CHARS:
        raise SteerError(f"message is {len(t)} characters; at most {MAX_NOTE_CHARS}")
    bad = next((c for c in t if not (c.isprintable() or c in "\n\t")), None)
    if bad is not None:
        raise SteerError(f"message holds a non-printable character (U+{ord(bad):04X}); only printable text, newlines "
                         "and tabs")
    return t


def _lock(d: str) -> int:
    fd = os.open(os.path.join(d, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _lines(path: str) -> list:
    """The JSON objects of a jsonl file (a missing file is none; a line that is not an object is skipped)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    with open(fd, "rb") as f:
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise SteerError(f"{os.path.basename(path)} is larger than {MAX_FILE_BYTES} bytes")
    out = []
    for ln in raw.decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out.append(rec)
    return out


def _append(path: str, recs: list) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, "".join(json.dumps(r) + "\n" for r in recs).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _state_locked(d: str):
    notes = _lines(os.path.join(d, NOTES))
    done = {r["id"] for r in _lines(os.path.join(d, DELIVERED))}
    return notes, done


def add(d: str, text: str) -> tuple:
    """Append one checked note; returns (steer id, pending count after it). SteerError past MAX_PENDING."""
    text = check_text(text)
    fd = _lock(d)
    try:
        notes, done = _state_locked(d)
        pending = [n for n in notes if n["id"] not in done]
        if len(pending) >= MAX_PENDING:
            raise SteerError(f"{len(pending)} steering notes are already waiting for this task (at most "
                             f"{MAX_PENDING}): it has not reached a tool boundary since; wait, or cancel it")
        sid = uuid.uuid4().hex
        _append(os.path.join(d, NOTES), [{"id": sid, "at": time.time(), "text": text}])
        return sid, len(pending) + 1
    finally:
        os.close(fd)


def counts(d: str) -> dict:
    """{"pending": n, "delivered": n} (no lock: a read of two append-only files)."""
    notes = _lines(os.path.join(d, NOTES))
    done = {r["id"] for r in _lines(os.path.join(d, DELIVERED))}
    ids = {n["id"] for n in notes}
    return {"pending": len(ids - done), "delivered": len(ids & done)}


def delivered_ids(d: str) -> list:
    """Ids marked delivered, in order (the runner logs steer_delivered for the new ones)."""
    return [r["id"] for r in _lines(os.path.join(d, DELIVERED))]


def take(d: str) -> list:
    """The pending notes the next boundary delivers (oldest first, up to MAX_CONTEXT_CHARS of rendered text, at least
    one), marked delivered BEFORE they are returned."""
    fd = _lock(d)
    try:
        notes, done = _state_locked(d)
        out, size = [], 0
        for n in notes:
            if n["id"] in done or not isinstance(n.get("text"), str):
                continue
            cost = len(PREFIX) + len(n["text"]) + 2
            if out and size + cost > MAX_CONTEXT_CHARS:
                break
            out.append(n)
            size += cost
        if out:
            _append(os.path.join(d, DELIVERED), [{"id": n["id"], "at": time.time()} for n in out])
        return out
    finally:
        os.close(fd)


def render(notes: list) -> str:
    return "\n\n".join(PREFIX + n["text"] for n in notes)


def hook(d: str, stdin, stdout, stderr) -> int:
    """One PostToolUse / PostToolUseFailure call. Always 0: steering never blocks or alters the child's tool call."""
    try:
        raw = stdin.read()
        try:
            ev = json.loads(raw) if raw.strip() else {}
        except ValueError:
            ev = {}
        if not isinstance(ev, dict):
            ev = {}
        if ev.get("agent_id"):
            return 0                                # a subagent's tool call: the note is for the main loop
        name = ev.get("hook_event_name")
        name = name if name in HOOK_EVENTS else HOOK_EVENTS[0]
        if not (os.path.isabs(d) and os.path.isdir(d)):
            raise SteerError("the task dir is not an existing absolute directory")
        notes = take(d)
        if notes:
            stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": name,
                                                            "additionalContext": render(notes)}}))
            stdout.flush()
    except Exception as exc:                        # one line, no note text; the notes not marked stay pending
        stderr.write(f"cortados steer hook: {type(exc).__name__}: {exc}\n"[:300])
    return 0


def main(argv) -> int:
    if len(argv) == 2 and argv[0] == "--hook":
        return hook(argv[1], sys.stdin, sys.stdout, sys.stderr)
    sys.stderr.write("usage: steer.py --hook <task dir>\n")
    return 0                                        # a misconfigured hook must not fail the child's tool call


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
