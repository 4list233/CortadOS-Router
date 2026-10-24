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


def append(name: str, row: dict) -> None:
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with _write, path(name).open("a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def digest_id(prefix: str, *parts: str) -> str:
    return prefix + "." + hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]
