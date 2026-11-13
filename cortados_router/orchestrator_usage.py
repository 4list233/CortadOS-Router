"""Orchestrator tokens: how much the owner's MAIN Claude Code sessions spend themselves, read from local transcripts.

Main sessions call Anthropic directly, not through the provider, so their tokens exist only in Claude Code's own
transcripts on the machine that runs them: ~/.claude/projects/<slug>/<session id>.jsonl, one JSON object per line, an
`assistant` line carrying `message.id`, `message.model` and `message.usage` (input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens). This module sums them per session for a set of windows and
returns a metadata-only summary; live_heartbeat.py sends it to the provider (POST /v1/cortados/live/heartbeat,
`orchestrator` block) and the live view sets it against the routed tokens in router.db (provider/work_split.py).

What counts, and why:
  - Only top-level transcripts (<projects>/<slug>/*.jsonl). A session's subagents write their own files under
    <slug>/<session id>/subagents/; those, and any line marked isSidechain, are delegated work, not the orchestrator's.
    The Agent tool's subagents run on `cortados` (routed) and are counted on the routed side from router.db.
  - CortadOS child tasks are not orchestrators: they run in <task root>/{active,archive,held}/<task id> (workspaces.py),
    so a transcript whose cwd (or, without a cwd, project slug) is under such a folder is skipped. Their tokens are
    routed tokens (the provider served them).
  - A message whose model is `cortados`, a `sub/<model>` or an `api/<model>` pin was served by the provider (it echoes
    the requested model): routed work, so it is skipped here and counted (routed_model_entries) rather than counted
    twice. This is also what catches a child task started with a plain `cwd` outside the task root: CORTADOS_CHILD_TASK
    is in the child's environment, not its transcript, but every routed child asks the provider for one of these
    models. A child started on a direct (unrouted) model in such a folder is the one case still counted here.
  - Streaming writes one line per content block, each repeating the message's usage: a message counts once (its
    message.id, else requestId, else the line's uuid), last line wins (it carries the final output count). Ids are
    deduplicated across files, so a resumed session that copies earlier lines does not count them again.
  - `<synthetic>` messages (Claude Code's own placeholders, no API call) carry zero usage and are skipped.
A line is assigned to a window by its own `timestamp`, not the file's mtime; files whose mtime is older than the widest
window are not opened (nothing in them can be newer than the file).

Never sent: a raw session id (only the Office tag: sha256(salt:session id) cut to 16 hex, the same tag the
cortados-routed-subagents mod puts on routed requests, so the provider can join the two sides), prompt or reply text,
paths, tool inputs. A session's project is the cwd's last folder name, as the Office already shows it. Without the
mod's salt (mod not installed), sessions have no tag: they count in the totals only and `salt` says false.

CLI (prints the summary; reads only): python3 -B -m cortados_router.orchestrator_usage [--projects DIR] [--now EPOCH]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

VERSION = 1
DEFAULT_PROJECTS = "~/.claude/projects"
# The mod's $.store file (Claude Code keeps one JSON per plugin under plugins/store); key officeSalt, 32 hex.
SALT_STORE_GLOB = "~/.claude/plugins/store/cortados-routed-subagents_*.json"
SALT_KEY = "officeSalt"
TAG_HEX = 16                                    # register.ts OFFICE_TAG_HEX
# The owner's day and week: "today" answers "how did this session go", 7 d smooths over one heavy day.
WINDOWS: dict[str, int] = {"24h": 24 * 3600, "7d": 7 * 24 * 3600}
# Per-session rows sent: the heartbeat body is capped (live.HEARTBEAT_MAX_BYTES). 30 rows at ~300 bytes fit with room
# for the jobs and hardware; the owner runs a handful of main sessions a day, so 30 covers a busy week. The totals
# always include every session, the rows past the cap are counted in `sessions_hidden`.
SESSIONS_MAX = 30
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")
_USAGE_KEYS = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
               "cache_write": "cache_creation_input_tokens"}
TASK_DIRS = ("active", "archive", "held")       # workspaces.py: where a child task's working folder lives
_TASK_ID_RE = re.compile(r"t\d{14}-[0-9a-f]{8}")   # child_tasks task ids (t<UTC yyyymmddHHMMSS>-<8 hex>)
_SALT_RE = re.compile(r"[0-9a-f]{32}")
_PROJECT_RE = re.compile(r"[A-Za-z0-9._ -]{1,64}")   # office _HEADER_RULES[x-cortados-project]
_MODEL_RE = re.compile(r"[A-Za-z0-9._/\[\]:-]{1,64}")  # office._MODEL_RE
ROUTED_MODELS = ("cortados",)                   # the provider's model name
ROUTED_PREFIXES = ("sub/", "api/")              # the provider's subscription and API pins
MAX_LINE_BYTES = 4 * 1024 * 1024                # an assistant line with a huge tool input is still read; larger is not usage


def _zero() -> dict[str, int]:
    return {k: 0 for k in TOKEN_FIELDS}


def fresh_tokens(t: dict[str, int]) -> int:
    """input + output + cache writes: the tokens a turn actually processed anew. Cache reads are reported beside it;
    they are re-reads of a cached prefix (a long session re-reads its whole context every turn) and would otherwise
    swamp every comparison."""
    return int(t.get("input", 0)) + int(t.get("output", 0)) + int(t.get("cache_write", 0))


def all_tokens(t: dict[str, int]) -> int:
    return fresh_tokens(t) + int(t.get("cache_read", 0))


def session_tag(salt: str, session_id: str) -> str:
    """register.ts sessionTag: sha256(`salt:sessionId`) as hex, first 16 characters."""
    return hashlib.sha256(f"{salt}:{session_id}".encode()).hexdigest()[:TAG_HEX]


def read_salt(pattern: str = SALT_STORE_GLOB) -> str | None:
    """The mod's per-install salt, or None (no mod store, unreadable, or not 32 hex). With several stores (a reinstall),
    the most recently written one: that is the one the running mod reads."""
    paths = []
    for p in glob.glob(os.path.expanduser(pattern)):
        try:
            paths.append((os.stat(p).st_mtime, p))
        except OSError:
            continue
    for _, p in sorted(paths, reverse=True):
        try:
            v = json.loads(Path(p).read_text()).get(SALT_KEY)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(v, str) and _SALT_RE.fullmatch(v):
            return v
    return None


def _slug(path: str) -> str:
    """Claude Code's project folder name for a cwd: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def is_child_path(cwd: str | None, slug: str, task_roots: Iterable[str] = ()) -> bool:
    """True when the session ran in a child task's working folder: <anything>/{active,archive,held}/<task id>[/...],
    or under one of `task_roots`' task folders. Without a cwd the project slug is tested the same way."""
    roots = [os.path.normpath(os.path.expanduser(r)) for r in task_roots if r]
    if cwd:
        parts = Path(cwd).parts
        for i in range(len(parts) - 1):
            if parts[i] in TASK_DIRS and _TASK_ID_RE.fullmatch(parts[i + 1]):
                return True
        for r in roots:
            for d in TASK_DIRS:
                base = os.path.join(r, d) + os.sep
                if os.path.normpath(cwd).startswith(base):
                    return True
        return False
    if any(re.search(rf"-{d}-{_TASK_ID_RE.pattern}(?:-|$)", slug) for d in TASK_DIRS):
        return True
    return any(slug.startswith(_slug(os.path.join(r, d)) + "-") for r in roots for d in TASK_DIRS)


def _ts(value: Any) -> float | None:
    """An ISO-8601 timestamp ("2026-11-13T15:34:31.123Z") as epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?([+-]\d\d:\d\d)?$", v)
    if not m:
        return None
    frac = (m.group(2) or "")[:7].ljust(7, "0") if m.group(2) else ""   # Python 3.9 takes 3 or 6 fraction digits only
    try:
        dt = datetime.fromisoformat(m.group(1) + frac + (m.group(3) or "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def _count(v: Any) -> int:
    return int(v) if type(v) in (int, float) and v == v and 0 <= v < 1e12 else 0


def _routed_model(model: str) -> bool:
    return model in ROUTED_MODELS or model.startswith(ROUTED_PREFIXES)


def scan(projects_dir: str | os.PathLike = DEFAULT_PROJECTS, *, now: float | None = None,
         windows: dict[str, int] | None = None, salt: str | None = None,
         task_roots: Iterable[str] = (), sessions_max: int = SESSIONS_MAX) -> dict[str, Any]:
    """The summary (module doc). Reads only; a file that cannot be read is counted in `unreadable_files`."""
    now = time.time() if now is None else now
    windows = dict(WINDOWS if windows is None else windows)
    widest = max(windows.values())
    root = Path(os.path.expanduser(str(projects_dir)))
    excl = {"child_task_files": 0, "sidechain_entries": 0, "routed_model_entries": 0, "unreadable_files": 0,
            "bad_lines": 0}
    # message key -> (session id, ts, model, tokens, cwd); the last line of a message wins
    msgs: dict[str, tuple[str, float, str, dict[str, int], str | None]] = {}
    files = sorted(root.glob("*/*.jsonl")) if root.is_dir() else []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            excl["unreadable_files"] += 1
            continue
        if st.st_mtime < now - widest:
            continue
        slug = f.parent.name
        if is_child_path(None, slug, task_roots):
            excl["child_task_files"] += 1
            continue
        file_msgs: dict[str, tuple[str, float, str, dict[str, int], str | None]] = {}
        child = False
        try:
            with open(f, "rb") as fh:
                for raw in fh:
                    if len(raw) > MAX_LINE_BYTES or b'"assistant"' not in raw:
                        continue
                    try:
                        e = json.loads(raw)
                    except ValueError:
                        excl["bad_lines"] += 1
                        continue
                    if not isinstance(e, dict) or e.get("type") != "assistant":
                        continue
                    cwd = e.get("cwd") if isinstance(e.get("cwd"), str) else None
                    if cwd and is_child_path(cwd, slug, task_roots):
                        child = True
                        break
                    if e.get("isSidechain") is True:
                        excl["sidechain_entries"] += 1
                        continue
                    m = e.get("message")
                    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                        continue
                    model = m.get("model") if isinstance(m.get("model"), str) else ""
                    if model == "<synthetic>":
                        continue
                    if _routed_model(model):
                        excl["routed_model_entries"] += 1
                        continue
                    ts = _ts(e.get("timestamp"))
                    sid = e.get("sessionId") if isinstance(e.get("sessionId"), str) else f.stem
                    if ts is None or ts < now - widest or ts > now + 300:
                        continue
                    key = m.get("id") or e.get("requestId") or e.get("uuid")
                    if not isinstance(key, str) or not key:
                        continue
                    u = m["usage"]
                    file_msgs[key] = (sid, ts, model, {k: _count(u.get(src)) for k, src in _USAGE_KEYS.items()}, cwd)
        except OSError:
            excl["unreadable_files"] += 1
            continue
        if child:
            excl["child_task_files"] += 1
            continue
        msgs.update(file_msgs)
    return summarize(msgs.values(), now=now, windows=windows, salt=salt, sessions_max=sessions_max, excluded=excl)


def summarize(messages: Iterable[tuple[str, float, str, dict[str, int], str | None]], *, now: float,
              windows: dict[str, int], salt: str | None, sessions_max: int, excluded: dict[str, int]) -> dict[str, Any]:
    widest_name = max(windows, key=windows.get)
    totals = {w: {**_zero(), "sessions": 0} for w in windows}
    sessions: dict[str, dict[str, Any]] = {}
    for sid, ts, model, tok, cwd in messages:
        s = sessions.setdefault(sid, {"windows": {w: _zero() for w in windows}, "models": {}, "last_at": 0.0,
                                      "project": None})
        s["last_at"] = max(s["last_at"], ts)
        if cwd and s["project"] is None:
            s["project"] = os.path.basename(os.path.normpath(cwd))
        for w, span in windows.items():
            if ts >= now - span:
                for k in TOKEN_FIELDS:
                    s["windows"][w][k] += tok[k]
                    totals[w][k] += tok[k]
        if model:
            s["models"][model] = s["models"].get(model, 0) + fresh_tokens(tok)
    rows = []
    for sid, s in sessions.items():
        for w in windows:
            if any(s["windows"][w].values()):
                totals[w]["sessions"] += 1
        model = max(s["models"], key=s["models"].get) if s["models"] else None
        project = s["project"] if s["project"] and _PROJECT_RE.fullmatch(s["project"]) else None
        rows.append({"session": session_tag(salt, sid) if salt else None, "project": project,
                     "model": model if model and _MODEL_RE.fullmatch(model) else None,
                     "last_at": round(s["last_at"], 3), **{w: dict(s["windows"][w]) for w in windows}})
    rows.sort(key=lambda r: (fresh_tokens(r[widest_name]), r["last_at"]), reverse=True)
    return {"version": VERSION, "sampled_at": round(now, 3), "salt": bool(salt),
            "windows": {w: int(span) for w, span in windows.items()}, "totals": totals,
            "sessions": rows[:sessions_max], "sessions_hidden": max(0, len(rows) - sessions_max),
            "excluded": dict(excluded)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sum this machine's main Claude Code sessions' tokens (metadata only).")
    ap.add_argument("--projects", default=DEFAULT_PROJECTS)
    ap.add_argument("--task-root", action="append", default=[], help="a child-task workspace root (repeatable)")
    ap.add_argument("--now", type=float, default=None)
    a = ap.parse_args(argv)
    t0 = time.time()
    out = scan(a.projects, now=a.now, salt=read_salt(), task_roots=a.task_root)
    out["scan_ms"] = int((time.time() - t0) * 1000)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
