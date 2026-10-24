"""The outcome loop's append-only stores (Canonical, ENG-09): one writer per file, rows never rewritten.

A crash mid-append leaves at most one unterminated last line; readers skip it and count it, so a stage resumes by
re-reading which ids it already wrote.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import common

_write = threading.Lock()


def home() -> Path:
    """The data home: s1/data/outcome unless CORTADOS_OUTCOME_HOME points elsewhere (tests, the Studio deploy)."""
    root = Path(os.environ.get("CORTADOS_OUTCOME_HOME") or common.DATA / "outcome")
    root.mkdir(parents=True, exist_ok=True)
    return root


def path(name: str) -> Path:
    return home() / f"{name}.jsonl"


def read(name: str) -> list[dict]:
    rows = []
    p = path(name)
    if not p.exists():
        return rows
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue                                     # the one torn last line a crash can leave
    return rows


def ids(name: str, key: str = "id") -> set:
    return {r[key] for r in read(name) if key in r}


def _repair_tail(p: Path) -> None:
    """Move a crash's unterminated last line to <name>.torn before appending, so the next row is not glued to it
    (Codex review 2026-10-24: a row appended after a torn line was silently lost on read)."""
    if not p.exists() or p.stat().st_size == 0:
        return
    with p.open("rb+") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return
        f.seek(0)
        data = f.read()
        cut = data.rfind(b"\n") + 1
        with p.with_suffix(".torn").open("ab") as q:
            q.write(data[cut:] + b"\n")
        f.truncate(cut)
        os.fsync(f.fileno())


def append(name: str, row: dict) -> None:
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with _write:
        _repair_tail(path(name))
        with path(name).open("a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def digest_id(prefix: str, *parts: str) -> str:
    return prefix + "." + hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


class Claims:
    """In-process claims on task ids, so two workers never launch paid work for the same task (Codex review)."""

    def __init__(self):
        self._lock, self._held = threading.Lock(), set()

    def take(self, key: str) -> bool:
        with self._lock:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._held.discard(key)


def single_instance():
    """An exclusive lock on the data home: one loop per home, across processes. Returns the open lock file."""
    import fcntl
    f = (home() / "loop.lock").open("a")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError(f"another outcome loop holds {home() / 'loop.lock'}")
    return f
