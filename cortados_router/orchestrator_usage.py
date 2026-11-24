"""Orchestrator tokens: how much the owner's MAIN Claude Code sessions spend themselves, read from local transcripts.

Main sessions call Anthropic directly, not through the provider, so their tokens exist only in Claude Code's own
transcripts on the machine that runs them: ~/.claude/projects/<slug>/<session id>.jsonl, one JSON object per line, an
`assistant` line carrying `message.id`, `message.model` and `message.usage` (input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens). This module sums them per session for a set of windows and
returns a metadata-only summary; live_heartbeat.py sends it to the provider (POST /v1/cortados/live/heartbeat,
`orchestrator` block) and the live view sets it against the routed tokens in router.db (provider/work_split.py).

What counts, and why:
  - Top-level transcripts (<projects>/<slug>/*.jsonl) and the session's subagent transcripts
    (<slug>/<session id>/subagents/*.jsonl, lines marked isSidechain). A subagent the Agent tool ran on a direct
    Anthropic model was paid for by the main session's login and served by nobody else, so it is orchestrator work:
    counted in the totals and its session's row, and also in a separate `subagent` sub-total per window. A subagent
    routed through the provider asks for `cortados` or a pin (below), so it is skipped here by its model and counted on
    the routed side from router.db.
  - The provider's own headless CLIs are not orchestrators: the Claude lane (provider/bridge.py) and the search lane
    (provider/search_lane.py) run `claude -p` in a scratch dir named bridge.SCRATCH_PREFIX / search_lane.SCRATCH_PREFIX
    + random, and on the default login (no routed account config dir) their transcripts land in this projects dir on
    the provider host, on a real model. That work is routed (core.log_tokens records the bridge's turns), so a
    transcript whose cwd (or, without a cwd, project slug) is in such a scratch dir is skipped (provider_bridge_files).
    The Codex lane (provider/codex_bridge.py) writes only under its CODEX_HOME, never here.
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
paths, tool inputs. A session's project is the cwd's last folder name, as the Office already shows it.

The salt (#127): the machine's SHARED Office salt (~/.config/cortados/office-salt, subagent_mcp.read_office_salt), the
one start_task's task origins, route_task's session header, the pulse pane and the mod (once the file exists) all hash
with. Before #127 this read the mod's per-install store salt, which the mod uses only for a session that started
before the shared file existed: every other session's row then carried a tag no routed request ever had. Now the
shared salt names `session`; when the mod's store salt also exists and differs, `session_alt` carries the tag under it,
so a session the mod pinned to its store salt still joins (both are exact hashes of the same session id; nothing is
guessed). `salt_source` says which salt `session` is under ("shared", "store" when only the store salt exists, or
None). Without any salt, sessions have no tag: they count in the totals only and `salt` says false.

Per session, `models` breaks the session's tokens down by model (the main loop and its direct subagents together), per
window, as [input, output, cache_read, cache_write]: at most MODELS_PER_SESSION models, the rest summed under "other".

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

if __package__ in (None, ""):                       # run as a file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cortados_router.provider import bridge as _bridge, routing as _routing, search_lane as _search_lane  # noqa: E402

VERSION = 2                                     # 2: shared salt, session_alt, salt_source, per-session models (#127)
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
# The cap keeps the sessions active in the last ACTIVE_S first (most recent first), then the rest by tokens: a pane's
# own session is an active one, so a quiet session the owner is in never loses its row to heavier old ones (review
# round 3, #127: the caps bound the fleet display only). The sender does not know which session a pane asks for; more
# than SESSIONS_MAX sessions active at once is the one case left, and the pane says its native side is not reported.
ACTIVE_S = 30 * 60
MODELS_PER_SESSION = 3                          # models listed per session row; the rest are summed under OTHER_MODEL
OTHER_MODEL = "other"
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
# The provider's headless CLIs' scratch dir prefixes (module doc): one shared constant each, never a copy of the string.
PROVIDER_SCRATCH_PREFIXES = (_bridge.SCRATCH_PREFIX, _search_lane.SCRATCH_PREFIX)
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


def read_salt(env: dict | None = None) -> str | None:
    """The salt `session` tags are under: the machine's shared Office salt (module doc), else the mod's store salt
    (read_store_salt), else None."""
    return read_shared_salt(env) or read_store_salt()


def read_shared_salt(env: dict | None = None) -> str | None:
    """~/.config/cortados/office-salt (CORTADOS_OFFICE_SALT_FILE overrides), read as subagent_mcp reads it; None when
    absent or not 32 hex. Never creates it (the cortados-route MCP server does)."""
    from cortados_router import subagent_mcp
    return subagent_mcp.read_office_salt(env)


def read_store_salt(pattern: str = SALT_STORE_GLOB) -> str | None:
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


def is_provider_path(cwd: str | None, slug: str) -> bool:
    """True when the session ran in one of the provider's scratch dirs (<tmp>/<bridge prefix><random>/work,
    <tmp>/<search prefix><random>): the scratch folder is the cwd's last part (the search lane) or the one before
    bridge.SCRATCH_WORK (the bridge), so a real project folder named like a prefix (~/code/cortados-bridge-v2) is
    kept. Without a cwd the project slug is tested the same way; tempfile's random suffix may hold '_', which the
    slug turns into '-', so the suffix is matched as [A-Za-z0-9-]+."""
    if cwd:
        parts = Path(os.path.normpath(cwd)).parts
        def scratch(part: str, prefix: str) -> bool:
            return part.startswith(prefix) and len(part) > len(prefix)
        if len(parts) >= 2 and parts[-1] == _bridge.SCRATCH_WORK and scratch(parts[-2], _bridge.SCRATCH_PREFIX):
            return True
        return bool(parts) and scratch(parts[-1], _search_lane.SCRATCH_PREFIX)
    suffix = r"[A-Za-z0-9-]+"
    return bool(re.search(rf"(?:^|-){re.escape(_slug(_bridge.SCRATCH_PREFIX))}{suffix}-"
                          rf"{re.escape(_slug(_bridge.SCRATCH_WORK))}$", slug)
                or re.search(rf"(?:^|-){re.escape(_slug(_search_lane.SCRATCH_PREFIX))}{suffix}$", slug))


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
    # `cortados@<window>` (a windowed whole-session main loop) is the provider's model too: the provider echoes the
    # suffixed name, so without this its tokens would count twice, here and as routed tokens (review of b1d3075).
    return (model in ROUTED_MODELS or model.startswith(ROUTED_PREFIXES)
            or _routing.band_model_window(model) is not None)


def scan(projects_dir: str | os.PathLike = DEFAULT_PROJECTS, *, now: float | None = None,
         windows: dict[str, int] | None = None, salt: str | None = None,
         task_roots: Iterable[str] = (), sessions_max: int = SESSIONS_MAX, alt_salt: str | None = None,
         salt_source: str | None = None) -> dict[str, Any]:
    """The summary (module doc). Reads only; a file that cannot be read is counted in `unreadable_files`."""
    now = time.time() if now is None else now
    windows = dict(WINDOWS if windows is None else windows)
    widest = max(windows.values())
    root = Path(os.path.expanduser(str(projects_dir)))
    excl = {"child_task_files": 0, "provider_bridge_files": 0, "routed_model_entries": 0, "unreadable_files": 0,
            "bad_lines": 0}
    # message key -> (session id, ts, model, tokens, cwd, sidechain); the last line of a message wins
    msgs: dict[str, tuple[str, float, str, dict[str, int], str | None, bool]] = {}
    files = []
    if root.is_dir():
        files = [(f, f.parent.name, f.stem) for f in root.glob("*/*.jsonl")]
        # a session's subagents: <slug>/<session id>/subagents/*.jsonl (their session id is the parent's)
        files += [(f, f.parents[2].name, f.parents[1].name) for f in root.glob("*/*/subagents/*.jsonl")]
        files.sort()
    for f, slug, file_sid in files:
        try:
            st = f.stat()
        except OSError:
            excl["unreadable_files"] += 1
            continue
        if st.st_mtime < now - widest:
            continue
        if is_provider_path(None, slug):
            excl["provider_bridge_files"] += 1
            continue
        if is_child_path(None, slug, task_roots):
            excl["child_task_files"] += 1
            continue
        file_msgs: dict[str, tuple[str, float, str, dict[str, int], str | None, bool]] = {}
        child = provider = False
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
                    if cwd and is_provider_path(cwd, slug):
                        provider = True
                        break
                    if cwd and is_child_path(cwd, slug, task_roots):
                        child = True
                        break
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
                    sid = e.get("sessionId") if isinstance(e.get("sessionId"), str) else file_sid
                    if ts is None or ts < now - widest or ts > now + 300:
                        continue
                    key = m.get("id") or e.get("requestId") or e.get("uuid")
                    if not isinstance(key, str) or not key:
                        continue
                    u = m["usage"]
                    file_msgs[key] = (sid, ts, model, {k: _count(u.get(src)) for k, src in _USAGE_KEYS.items()}, cwd,
                                      e.get("isSidechain") is True)
        except OSError:
            excl["unreadable_files"] += 1
            continue
        if provider:
            excl["provider_bridge_files"] += 1
            continue
        if child:
            excl["child_task_files"] += 1
            continue
        msgs.update(file_msgs)
    return summarize(msgs.values(), now=now, windows=windows, salt=salt, sessions_max=sessions_max, excluded=excl,
                     alt_salt=alt_salt, salt_source=salt_source)


def summarize(messages: Iterable[tuple], *, now: float,
              windows: dict[str, int], salt: str | None, sessions_max: int, excluded: dict[str, int],
              alt_salt: str | None = None, salt_source: str | None = None) -> dict[str, Any]:
    """messages: (session id, ts, model, tokens, cwd[, sidechain]). Sidechain (subagent) tokens count in the totals and
    their session's row like the main loop's, and again in `subagent` (a sub-total of `totals`, per window). A row's
    `models`: its tokens per model and window (module doc)."""
    widest_name = max(windows, key=windows.get)
    totals = {w: {**_zero(), "sessions": 0} for w in windows}
    subagent = {w: _zero() for w in windows}
    sessions: dict[str, dict[str, Any]] = {}
    for sid, ts, model, tok, cwd, *rest in messages:
        side = bool(rest and rest[0])
        s = sessions.setdefault(sid, {"windows": {w: _zero() for w in windows}, "models": {}, "last_at": 0.0,
                                      "project": None, "by_model": {}})
        s["last_at"] = max(s["last_at"], ts)
        if cwd and s["project"] is None:
            s["project"] = os.path.basename(os.path.normpath(cwd))
        bm = s["by_model"].setdefault(model or "?", {w: _zero() for w in windows})
        for w, span in windows.items():
            if ts >= now - span:
                for k in TOKEN_FIELDS:
                    bm[w][k] += tok[k]
                    s["windows"][w][k] += tok[k]
                    totals[w][k] += tok[k]
                    if side:
                        subagent[w][k] += tok[k]
        if model:
            s["models"][model] = s["models"].get(model, 0) + fresh_tokens(tok)
    rows = []
    for sid, s in sessions.items():
        for w in windows:
            if any(s["windows"][w].values()):
                totals[w]["sessions"] += 1
        model = max(s["models"], key=s["models"].get) if s["models"] else None
        project = s["project"] if s["project"] and _PROJECT_RE.fullmatch(s["project"]) else None
        row = {"session": session_tag(salt, sid) if salt else None, "project": project,
               "model": model if model and _MODEL_RE.fullmatch(model) else None,
               "last_at": round(s["last_at"], 3), **{w: dict(s["windows"][w]) for w in windows},
               "models": _row_models(s["by_model"], windows)}
        if salt and alt_salt and alt_salt != salt:
            row["session_alt"] = session_tag(alt_salt, sid)
        rows.append(row)
    rows.sort(key=lambda r: (fresh_tokens(r[widest_name]), r["last_at"]), reverse=True)
    active = sorted((r for r in rows if r["last_at"] >= now - ACTIVE_S), key=lambda r: r["last_at"], reverse=True)
    if len(rows) > sessions_max and active:             # only the cut changes: under the cap the order is unchanged
        ids = {id(r) for r in active}
        rows = active + [r for r in rows if id(r) not in ids]
    return {"version": VERSION, "sampled_at": round(now, 3), "salt": bool(salt),
            "salt_source": salt_source if salt and salt_source in ("shared", "store") else None,
            "windows": {w: int(span) for w, span in windows.items()}, "totals": totals,
            "subagent": subagent,
            "sessions": rows[:sessions_max], "sessions_hidden": max(0, len(rows) - sessions_max),
            "excluded": dict(excluded)}


def _row_models(by_model: dict[str, dict[str, dict[str, int]]], windows: dict[str, int]) -> dict[str, Any]:
    """A session's tokens per model (module doc): the MODELS_PER_SESSION largest by fresh tokens over the widest window,
    each {window: [input, output, cache_read, cache_write]}; the rest, and any model id the provider would refuse,
    summed under OTHER_MODEL. Models with nothing in any window are left out."""
    widest = max(windows, key=windows.get)
    named = [(m, t) for m, t in by_model.items() if any(any(t[w].values()) for w in windows)]
    named.sort(key=lambda mt: (fresh_tokens(mt[1][widest]), all_tokens(mt[1][widest])), reverse=True)
    out: dict[str, Any] = {}
    other = {w: _zero() for w in windows}
    has_other = False
    for m, t in named:
        if len(out) < MODELS_PER_SESSION and m != OTHER_MODEL and _MODEL_RE.fullmatch(m):
            out[m] = {w: [t[w][k] for k in TOKEN_FIELDS] for w in windows}
        else:
            has_other = True
            for w in windows:
                for k in TOKEN_FIELDS:
                    other[w][k] += t[w][k]
    if has_other:
        out[OTHER_MODEL] = {w: [other[w][k] for k in TOKEN_FIELDS] for w in windows}
    return out


def salts(env: dict | None = None) -> tuple[str | None, str | None, str | None]:
    """(salt, alt_salt, salt_source) for scan(): the shared salt with the store salt as the alternative, else the store
    salt alone, else nothing (module doc)."""
    shared, store = read_shared_salt(env), read_store_salt()
    if shared:
        return shared, (store if store and store != shared else None), "shared"
    if store:
        return store, None, "store"
    return None, None, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sum this machine's main Claude Code sessions' tokens (metadata only).")
    ap.add_argument("--projects", default=DEFAULT_PROJECTS)
    ap.add_argument("--task-root", action="append", default=[], help="a child-task workspace root (repeatable)")
    ap.add_argument("--now", type=float, default=None)
    a = ap.parse_args(argv)
    t0 = time.time()
    salt, alt, source = salts()
    out = scan(a.projects, now=a.now, salt=salt, alt_salt=alt, salt_source=source, task_roots=a.task_root)
    out["scan_ms"] = int((time.time() - t0) * 1000)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
