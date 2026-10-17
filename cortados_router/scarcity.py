"""Subscription scarcity, in SUBSCRIPTION UNITS (never dollars).

For every lane: how much of the plan's window is used (%), how much of the window's TIME has elapsed (%), when it resets,
and what follows from the two: the pace (projected % at reset) and the allowance (% per hour we can still spend until the reset).
scarcity ∈ [0,1] is derived from that; 1.0 = the provider is refusing or the window is spent. Local lane: free, scarce only in memory.

Sources (verified live 2026-09-21). ONE method for every subscription (Forest's ruling): open the vendor's own CLI in a PTY, type its
usage command, read the screen. No tokens are spent, no private API is called, and the figures are exactly the ones the vendor shows its user.
  claude       claude        /usage   → Current session (5 h) · Current week (all models / per model)  "% used · Resets …"
  codex        codex         /status  → "5h limit / Weekly limit: N% left (resets HH:MM on D Mon)"
  cursor       cursor-agent  /usage   → "Included N% used" (+ Auto and API pools) · "Resets Mon D" (monthly)
  antigravity  agy           /usage   → per model group: Weekly / Five Hour "Limit Remaining N%" · "Refreshes in …"
  local        free memory vs what the resident model needs (vm_stat + ollama ps)
"""
from __future__ import annotations

# This file is also loaded by path (Hermes plugin loader, s1/, tests, `python core.py`): make its package importable.
import sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_root = str(_CompatPath(__file__).resolve().parents[1])
if _compat_root not in _compat_sys.path:
    _compat_sys.path.insert(0, _compat_root)

from cortados_router.compat import getenv as _env_get

import fcntl
import json
import os
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

STATE = Path(_env_get("CORTADOS_HOME") or _env_get("CORTADOS_HERMES_HOME") or Path.home() / ".cortados")
CACHE = STATE / "scarcity.json"
HISTORY = STATE / "scarcity-history.jsonl"   # one line per sample: the series the unit-cost learner and the crons read
PROBE_DIR = STATE / "usage-probe"            # empty folder the interactive CLIs are opened in
MIN_INTERVAL = {"claude": 900, "codex": 900, "cursor": 1800, "antigravity": 1800, "local": 60}
H5, D7 = 5 * 3600.0, 7 * 86400.0
_lock = threading.Lock()
_refreshing = threading.Event()


def _load_probe_reaper():
    """Sibling module loaded by path (this plugin is not a package); cached per process so its atexit runs once."""
    name = "cortados_router_probe_reaper"
    if name not in sys.modules:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(name, str(Path(__file__).with_name("probe_reaper.py")))
        mod = _ilu.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return sys.modules[name]


_reaper = _load_probe_reaper()


def window_view(name: str, used_pct: float, reset_at: float | None, window_s: float, now: float | None = None) -> dict[str, Any]:
    """The one shape every lane reports: % used vs % of the window's time elapsed, and what that implies."""
    now = now or time.time()
    remaining_s = max(0.0, (reset_at or now + window_s) - now)
    elapsed = min(1.0, max(0.0, 1.0 - remaining_s / window_s)) if window_s else 1.0
    projected = used_pct / max(elapsed, 0.05)       # % of the window we are on pace to have used at the reset
    headroom = max(0.0, 100.0 - used_pct)
    if used_pct >= 99:
        s = 1.0
    else:
        s = min(0.9, (projected - 70.0) / 60.0)              # on pace for 70 % → 0, ≥124 % → 0.9; 1.0 is reserved for spent / refusing
        if s < 0:                                            # UNDERUSED (Forest 2026-09-24): a pool on pace for far less than its allowance attracts work;
            s = max(-0.6, s) if window_s >= 86400 else 0.0   # down to −0.6 for a 1 %-used week. 5-hour windows renew too fast to count as underused.
        if used_pct < 25:
            s = min(s, 0.3)                                  # early in a window the pace is noise (on the scarce side)
        if used_pct >= 90:
            s = max(s, 0.8)
    # USE IT OR LOSE IT (Forest 2026-09-22): headroom that the reset will wipe at today's pace is worth nothing tomorrow, so scarcity goes
    # NEGATIVE as the reset nears: 0 at half-way, down to −(wasted share) at the reset. Long windows only; 5-hour windows renew too fast to matter.
    wasted = max(0.0, 100.0 - projected) if used_pct < 99 else 0.0
    # Forest 2026-09-26 (bias strongly towards expiring credits): from a quarter of the window on, the share that will be wasted at today's pace
    # counts linearly against scarcity, reaching its full weight at the reset: a week 3 % used at 70 % elapsed reads ≈ −0.6, at 95 % ≈ −0.9.
    if reset_at and window_s >= 86400 and elapsed > 0.25:
        s = round(max(-1.0, s - (wasted / 100.0) * ((elapsed - 0.25) / 0.75)), 2)
    return {"window_s": window_s, "reset_epoch": reset_at, "window": name, "used_pct": round(used_pct, 1), "elapsed_pct": round(elapsed * 100, 1), "headroom_pct": round(headroom, 1),
            "projected_pct_at_reset": round(projected), "allowance_pct_per_h": round(headroom / max(remaining_s / 3600, 0.25), 2),
            "resets_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(reset_at)) if reset_at else None,
            "resets_in_h": round(remaining_s / 3600, 1) if reset_at else None, "scarcity": round(s, 2),
            # use-it-or-lose-it: headroom the reset will wipe (what we'd still have left at today's pace), only meaningful near the reset
            "expiring_pct": round(wasted) if reset_at and window_s >= 86400 and elapsed >= 0.85 else 0}


# ------------------------------------------------------------------ PTY screen capture (interactive CLIs, no tokens spent)
_CSI = re.compile(rb"\x1b\[([0-9;?<>=]*)([A-Za-z])")


def _plain(raw: bytes) -> str:
    raw = re.sub(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x1b]*\x1b\\|\x1b[=>()][A-Za-z0-9]?", b"", raw)
    raw = _CSI.sub(lambda m: b" " * min(int(m.group(1) or 1), 200) if m.group(2) == b"C" and (m.group(1) or b"1").isdigit()
                   else (b"\n" if m.group(2) in b"HdG" else b""), raw)
    return re.sub(r"[ \t]+", " ", raw.decode("utf-8", "replace"))


def _boot_failure_kind(text: str) -> str:
    """Classify startup failures without persisting terminal text or credentials."""
    for kind, pattern in (
        ("cursor_position", r"cursor position.*(?:could not|cannot|timed out)|could not.*cursor position"),
        ("terminal", r"termios|inappropriate ioctl|terminal.*(?:failed|not supported)"),
        ("config", r"(?:error|failed).*config|config.*(?:error|invalid)"),
        ("auth", r"(?:authentication|authorization|login).*(?:failed|required|expired)"),
        ("panic", r"panicked at|fatal runtime error"),
        ("exec", r"FileNotFoundError|PermissionError|Exec format error"),
    ):
        if re.search(pattern, text, re.I):
            return kind
    return "unknown"


def tui_capture(argv: list[str], command: str, done: str, env: dict[str, str], boot_s: float = 12, run_s: float = 20, ready: str | None = None,
                nudge: tuple[str, str, bytes, int, float] | None = None, diagnostics: dict | None = None) -> str:
    """Open the CLI in a PTY, accept a folder-trust prompt for our own empty probe folder if shown, type `command`, return the screen text."""
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    my_dir = PROBE_DIR / f"p{os.getpid()}-{int(time.time() * 1000) % 100000}"     # per-probe folder: the reaper kills by cwd and must never hit another probe
    my_dir.mkdir(exist_ok=True)
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(my_dir)
            os.execvpe(argv[0], argv, {**env, "TERM": "xterm-256color"})
        finally:
            os._exit(127)    # exec failed (e.g. missing binary): never let the forked copy run the sampler's code
    _reaper.track(pid, fd, my_dir, PROBE_DIR)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 160, 0, 0))
    buf = b""
    eof = False
    stage, startup_chars, ready_seen = "boot", 0, False

    def pump(max_s: float, quiet_s: float = 1.5, until: str | None = None, mark: int = 0) -> None:
        """Read until the screen goes quiet, or (with `until`) until that text is on screen and has had a moment to finish painting."""
        nonlocal buf, eof
        end, last, hit = time.time() + max_s, time.time(), 0.0
        while time.time() < end:
            _reaper.sample(pid)
            r, _, _ = select.select([fd], [], [], 0.25)
            if r:
                try:
                    d = os.read(fd, 65536)
                except OSError:
                    eof = True
                    return
                if not d:
                    eof = True
                    return
                buf += d
                last = time.time()
                if b"\x1b[>c" in d or b"\x1b[>0c" in d:      # terminal identity queries some TUIs block on
                    os.write(fd, b"\x1b[>0;276;0c")
                if b"\x1b[c" in d:
                    os.write(fd, b"\x1b[?62;c")
                if b"\x1b[6n" in d:
                    os.write(fd, b"\x1b[1;1R")
            if until:
                if not hit and len(buf) > mark and re.search(until, _plain(buf[mark:])):
                    hit = time.time()
                if hit and time.time() - hit > 1.5:
                    return
            elif len(buf) > mark and time.time() - last > quiet_s:
                return
    mark = 0
    try:
        pump(boot_s, until=ready)
        startup_chars = len(_plain(buf))
        ready_seen = bool(ready and re.search(ready, _plain(buf)))
        if eof:
            mark = len(buf)
            return ""
        if re.search(r"trust (this|the contents)|Trust this workspace", _plain(buf), re.I):
            stage = "trust"
            return ""
        mark = len(buf)
        stage = "command"
        os.write(fd, command.encode())
        pump(3, quiet_s=1.0, mark=mark)
        os.write(fd, b"\r")
        pump(run_s, until=done, mark=mark)
        if not eof and not re.search(done, _plain(buf[mark:])):      # some screens answer "refreshing, ask again": ask once more
            stage = "retry"
            os.write(fd, b"\x1b")
            time.sleep(3)
            os.write(fd, command.encode())
            pump(3, quiet_s=1.0, mark=len(buf))
            os.write(fd, b"\r")
            pump(run_s, until=done, mark=mark)
        if nudge:                                        # (want, blocked_by, key, max_presses, wait_s): e.g. press r while a panel says "rate limited"
            want, blocked, key, presses, wait_s = nudge
            for _ in range(presses):
                shown = _plain(buf[mark:])
                if re.search(want, shown) or not re.search(blocked, shown):
                    break
                time.sleep(wait_s)
                mark2 = len(buf)
                try:
                    os.write(fd, key)
                except OSError:
                    break  # child already exited; use what we captured instead of crashing
                pump(8, until=want, mark=mark2)
        return _plain(buf[mark:])
    except OSError:
        return _plain(buf[mark:])  # child exited mid-write somewhere in this flow (closed PTY); use what we captured instead of crashing
    finally:
        if diagnostics is not None:
            diagnostics.update(stage=stage, eof=eof, startup_chars=startup_chars,
                               response_chars=len(_plain(buf[mark:])), ready_seen=ready_seen)
            if eof and stage in {"boot", "trust"}:
                diagnostics["boot_failure"] = _boot_failure_kind(_plain(buf))
        _reaper.reap(pid, fd, PROBE_DIR, my_dir)
        try:
            os.rmdir(my_dir)
        except OSError:
            pass


def parse_reset(text: str, tz: str | None, now: datetime | None = None) -> float | None:
    """'12pm' · '3:30pm' · 'Sep 21 at 3pm' → epoch, in the zone the CLI printed."""
    m = re.search(r"(?:([A-Z][a-z]{2})[a-z]* (\d{1,2})(?:,| at)? )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if not m:
        return None
    try:
        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    now = now or datetime.now(zone)
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    t = now.replace(hour=hour, minute=int(m.group(4) or 0), second=0, microsecond=0)
    if m.group(1):
        mon = datetime.strptime(m.group(1)[:3].title(), "%b").month
        t = t.replace(month=mon, day=int(m.group(2)))
        if t < now - timedelta(days=1):
            t = t.replace(year=t.year + 1)
    elif t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def parse_claude_usage(text: str) -> list[dict[str, Any]]:
    wins, text = [], re.sub(r"\s+", " ", text)       # the TUI paints by cursor moves; after flattening, words are single-space separated
    for m in re.finditer(r"Current (session|week)\s*(\([^)]*\))?\D{0,120}?(\d{1,3})\s*% used\s*(?:Resets?\s+([^()\n]{2,40}?)\s*\(([^)]+)\))?", text):
        kind, scope, pct, when, tz = m.groups()
        name = "5-hour session" if kind == "session" else "week " + (scope or "(all models)")
        if any(w["window"] == name for w in wins):
            continue                                       # the TUI repaints; first full paint wins
        w = window_view(name, float(pct), parse_reset(when, tz) if when else None, H5 if kind == "session" else D7)
        scope_l = (scope or "").strip("()").lower()
        w["pool"] = "all" if kind == "session" or not scope_l or "all" in scope_l else re.sub(r"[^a-z0-9]+", "-", scope_l.replace("only", "")).strip("-")
        wins.append(w)
    return wins


def parse_agy_usage(text: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    parts = re.split(r"([A-Z][A-Z ]{3,40} MODELS) (?=Models within)", re.sub(r"\s+", " ", text))
    for title, chunk in zip(parts[1::2], parts[2::2]):
        wins = []
        for m in re.finditer(r"(Weekly|Five Hour) Limit Remaining\D{0,200}?(\d{1,3}(?:\.\d+)?)\s*%\s*(Refreshes in ([0-9dhm ]+)|Quota available)?", chunk):
            secs = sum(int(n) * {"d": 86400, "h": 3600, "m": 60}[u] for n, u in re.findall(r"(\d+)\s*([dhm])", m.group(4) or ""))
            wins.append(window_view("week" if m.group(1) == "Weekly" else "5-hour", 100.0 - float(m.group(2)),
                                    time.time() + secs if secs else None, D7 if m.group(1) == "Weekly" else H5))
        if wins and title.strip().lower() not in groups:
            groups[title.strip().lower()] = wins[:2]
    return groups


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    f = STATE / "env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v.strip().strip('"')
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    return env


def _claude(prev: dict[str, Any] | None = None, *, environment=None) -> dict[str, Any]:
    # the per-model weeks (Fable, Opus, …) load from a rate-limited endpoint; the screen offers "r to retry". Two gentle presses, never more.
    text = tui_capture(["claude", "--strict-mcp-config"], "/usage", r"% used[\s\S]*% used", _cli_env() if environment is None else environment, run_s=25,
                       nudge=(r"Current week \((?!all models)[^)]+\)\D{0,160}?\d+\s*% used", r"breakdown unavailable", b"r", 2, 8.0))
    wins = parse_claude_usage(text)
    if not wins:
        raise RuntimeError("no '% used' lines on the /usage screen: " + text[-200:].replace("\n", " "))
    out: dict[str, Any] = {"source": "claude /usage (pty)"}
    if not any(w["pool"] != "all" for w in wins):
        out["per_model"] = "not shown this read (vendor rate limit on the per-model panel)"
        for w in (prev or {}).get("windows") or []:          # keep the last per-model reading until its own reset passes
            if w.get("pool", "all") != "all" and w.get("resets_at") and w["resets_at"] > time.strftime("%Y-%m-%d %H:%M"):
                wins.append({**w, "stale": True})
    return {**out, "windows": wins}


def _antigravity(*, environment=None) -> dict[str, Any]:
    text = tui_capture(["agy"], "/usage", r"Limit Remaining[\s\S]*Limit Remaining", _cli_env() if environment is None else environment)
    groups = parse_agy_usage(text)
    if not groups:
        raise RuntimeError("no quota groups on the /usage screen: " + text[-200:].replace("\n", " "))
    wins = []
    for g, ws in groups.items():
        pool = "gemini" if "gemini" in g else "claude-gpt" if "claude" in g or "gpt" in g else re.sub(r"[^a-z0-9]+", "-", g.replace("models", "")).strip("-")
        wins += [{**w, "window": f"{w['window']} ({pool})", "pool": pool} for w in ws]
    return {"source": "agy /usage (pty)", "windows": wins}


def parse_codex_status(text: str) -> list[dict[str, Any]]:
    wins, text = [], re.sub(r"\s+", " ", text)
    for m in re.finditer(r"((?:(?:[Gg][Pp][Tt]-[A-Za-z0-9.-]+|o[1-9][A-Za-z0-9.-]*) )?(?:5h|[Ww]eekly)) limit: \[[^\]]*\] (\d{1,3})% left(?: \(resets ([^)]+)\))?", text):
        label, left, when = m.groups()
        reset = None
        t = re.fullmatch(r"(\d{1,2}):(\d{2})(?: (AM|PM))?(?: on (\d{1,2}) ([A-Z][a-z]{2}))?", when or "")
        if t:
            try:
                now = datetime.now()
                hour = int(t.group(1))
                if t.group(3):
                    if not 1 <= hour <= 12:
                        raise ValueError("Invalid AM/PM clock")
                    hour = hour % 12 + (12 if t.group(3) == "PM" else 0)
                reset_dt = now.replace(hour=hour, minute=int(t.group(2)), second=0, microsecond=0)
                if t.group(4):
                    reset_dt = reset_dt.replace(month=datetime.strptime(t.group(5), "%b").month, day=int(t.group(4)))
                    if reset_dt < now - timedelta(days=1):
                        reset_dt = reset_dt.replace(year=now.year + 1)
                elif reset_dt <= now:
                    reset_dt += timedelta(days=1)
                reset = reset_dt.timestamp()
            except ValueError:
                reset = None
        span = "5-hour" if label.endswith("5h") else "week"
        model = re.sub(r"\s*(5h|[Ww]eekly)$", "", label).strip()
        name = span if not model else f"{span} ({model})"
        if not any(w["window"] == name for w in wins):
            w = window_view(name, 100.0 - float(left), reset, H5 if span == "5-hour" else D7)
            w["pool"] = "all" if not model else re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
            wins.append(w)
    return wins


def _codex_binary(env: dict[str, str]) -> tuple[str, str | None]:
    """The configured Codex CLI if it exists, else `codex` on PATH. Returns (binary, missing configured path or None):
    ChatGPT.app stopped shipping Resources/codex (2026-10), and a dead configured path must not blind the probe."""
    import shutil
    configured = env.get("CORTADOS_CODEX_BIN") or "/Applications/ChatGPT.app/Contents/Resources/codex"
    if os.access(configured, os.X_OK):
        return configured, None
    return shutil.which("codex", path=env.get("PATH")) or configured, configured


def _codex() -> dict[str, Any]:
    env = _cli_env()
    binary, missing_bin = _codex_binary(env)
    retries: list[dict] = []
    for attempt in range(2):
        diagnostic: dict = {}
        text = tui_capture([binary], "/status", r"limit: \[[^\]]*\] \d+% left", env, boot_s=20,
                           ready=r"model: +(?!loading)\S+", diagnostics=diagnostic)
        wins = parse_codex_status(text)
        if (not wins and not text.strip() and attempt == 0 and diagnostic.get("eof")
                and diagnostic.get("stage") in {"boot", "trust"}
                and diagnostic.get("boot_failure") not in {"auth", "config", "exec"}):
            # The old PTY has already been reaped. One new CLI can recover a
            # transient startup exit; never retry malformed panels or change auth.
            retries.append(dict(diagnostic))
            continue
        break
    if not wins:
        if not text.strip():
            # Metadata only: distinguish startup exit from an unresponsive TUI
            # without persisting boot screens, credentials or prompt contents.
            raise RuntimeError(f"Codex /status empty (attempts={1 + len(retries)}): " + ", ".join(f"{k}={v}" for k, v in diagnostic.items()))
        raise RuntimeError("no 'limit: … % left' lines on the /status screen: " + re.sub(r"\s+", " ", text)[-200:])
    return {"source": "codex /status (pty)", "windows": wins, "startup_retries": retries,
            **({"configured_binary_missing": missing_bin, "binary": binary} if missing_bin else {})}


def parse_cursor_usage(text: str) -> list[dict[str, Any]]:
    text = re.sub(r"\s+", " ", text)
    r = re.search(r"Resets ([A-Z][a-z]{2}) (\d{1,2})", text)
    reset = None
    if r:
        now = datetime.now()
        end = now.replace(month=datetime.strptime(r.group(1), "%b").month, day=int(r.group(2)), hour=23, minute=59, second=0, microsecond=0)
        if end < now:
            end = end.replace(year=now.year + 1)
        reset = end.timestamp()
    wins = []
    for label, name, pool in (("Included", "month (included, both pools)", "info"), ("Auto", "month (Cursor models: Grok, Composer, Auto)", "cursor-models"),
                              ("API", "month (other models: Claude, GPT, Gemini …)", "other-models")):
        m = re.search(label + r" (\d{1,3})% used", text)
        if m:
            wins.append({**window_view(name, float(m.group(1)), reset, 30.4 * 86400), "pool": pool})
    return wins


def _cursor(*, environment=None) -> dict[str, Any]:
    from integrations.mcp.config import load_config
    from cortados_router.quota_observations import status_record
    return status_record("cursor", config=load_config("/Users/velox/.config/cortados-workers/config.json"), environment=environment)


def _local() -> dict[str, Any]:
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in (l.split(":") for l in vm.splitlines()[1:] if ":" in l) if v.strip().rstrip(".").isdigit()}
        free_gb = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)) * 16384 / 2**30
        ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=5).stdout
        resident = [l.split()[0] for l in ps.splitlines()[1:] if l.strip()]
        need = 0.0 if any("14b" in r for r in resident) else 12.0 * 1.5  # 2.0 RAM gate: footprint × factor when cold
        s = 0.0 if free_gb >= need + 2 else min(1.0, (need + 2 - free_gb) / 8)
        return {"source": "vm_stat + ollama ps", "windows": [], "free_gb": round(free_gb, 1), "resident": resident, "scarcity": round(s, 2)}
    except Exception as exc:
        raise



def pool_for(lane: str, model: str, pools: list[str]) -> str | None:
    """Which separately-metered pool a model draws from, beyond the lane's general windows."""
    m = (model or "").lower()
    if lane == "cursor":
        return "cursor-models" if re.search(r"grok|composer|^auto$|^default$|vega", m) else "other-models"
    if lane == "antigravity":
        return "gemini" if "gemini" in m else "claude-gpt"
    hits = [p for p in pools if p not in ("all", "info") and all(tok in m for tok in p.split("-") if tok)]
    return max(hits, key=len) if hits else None


def _roll(wins: list[dict[str, Any]]) -> dict[str, Any]:
    tight = max(wins, key=lambda w: w["scarcity"], default=None)     # the binding window; a 5-hour window at 0 never hides a week at −0.9 …
    if tight and tight["scarcity"] <= 0.3:
        low = min(wins, key=lambda w: w["scarcity"])
        if low["scarcity"] < 0:                                       # … because when nothing is tight, the expiring credit wins
            tight = low
    return {"scarcity": tight["scarcity"] if tight else None, "tightest": tight, "expiring_pct": min([w.get("expiring_pct", 0) for w in wins if not w["window"].startswith("5-hour")], default=0),   # only if EVERY long window it draws on has spare
            "spent": any(w["used_pct"] >= 99 for w in wins)}


def finish(lane: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Lane scarcity = its general windows; every other pool is scored on its own and combined with the general ones when a model draws on it."""
    wins = rec.get("windows") or []
    general = [w for w in wins if w.get("pool", "all") == "all"]
    pools = {}
    for p in sorted({w.get("pool", "all") for w in wins} - {"all", "info"}):
        pools[p] = _roll(general + [w for w in wins if w.get("pool") == p])
    if lane == "cursor" and pools.get("cursor-models", {}).get("spent") and "other-models" in pools:
        pools["cursor-models"] = {**pools["other-models"], "note": "Cursor-models pool spent: overflow draws on other-models"}
    base = _roll(general) if general else _roll([w for w in wins if w.get("pool") == {"cursor": "cursor-models", "antigravity": "gemini"}.get(lane)])
    return {**rec, "scarcity": 1.0 if base["spent"] else base["scarcity"], "refusing": base["spent"], "pools": {
        p: {**v, "scarcity": 1.0 if v["spent"] else v["scarcity"]} for p, v in pools.items()}}


def _codex_native():
    from integrations.mcp.config import load_config
    from cortados_router.quota_observations import observe_lane
    value = observe_lane("codex", load_config("/Users/velox/.config/cortados-workers/config.json"))
    if value["error"] is not None:
        raise RuntimeError(value["error"]["reason"])
    return value["record"]


SAMPLERS = {"claude": _claude, "antigravity": _antigravity, "codex": _codex_native, "cursor": _cursor, "local": _local}


def _plans() -> dict[str, str]:
    try:
        from cortados_router.paths import tables_dir
        lanes = json.loads((tables_dir() / "budgets.json").read_text())["lanes"]
        return {k: v.get("plan") for k, v in lanes.items()}
    except Exception:
        return {}


def sample(force: bool = False, only: list[str] | None = None) -> dict[str, Any]:
    import fcntl as _f
    STATE.mkdir(parents=True, exist_ok=True)
    lockf = open(STATE / "scarcity.lock", "w")
    try:
        _f.flock(lockf, _f.LOCK_EX | _f.LOCK_NB)          # one sampler at a time across processes (gateway, route-crons, crons' refresh)
    except OSError:
        _f.flock(lockf, _f.LOCK_EX)                       # someone else is sampling: wait for them, then return their fresh cache
        lockf.close()
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            return {"fetched_at": 0, "lanes": {}}
    try:
        return _sample_locked(force, only)
    finally:
        _f.flock(lockf, _f.LOCK_UN); lockf.close()


def _sample_locked(force: bool = False, only: list[str] | None = None) -> dict[str, Any]:
    with _lock:
        prev: dict[str, Any] = {}
        try:
            prev = json.loads(CACHE.read_text())
        except Exception:
            pass
        sweep = _reaper.sweep_stale(PROBE_DIR)     # reclaim CLIs orphaned by a sampler process that died mid-probe
        now, plans = time.time(), _plans()
        lanes = dict(prev.get("lanes") or {})
        due = [l for l in SAMPLERS if (not only or l in only) and (force or now - float((lanes.get(l) or {}).get("tried_at") or 0) >= MIN_INTERVAL[l])]
        results: dict[str, Any] = {}

        def run(lane: str) -> None:
            try:
                rec = SAMPLERS[lane](lanes.get(lane)) if lane == "claude" else SAMPLERS[lane]()
                results[lane] = {**(finish(lane, rec) if lane != "local" else rec), "fetched_at": time.time(), "tried_at": time.time(), "error": None}
            except Exception as exc:      # keep the last good reading, say so
                old = lanes.get(lane) or {}
                results[lane] = {**old, "tried_at": time.time(), "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                if lane == "claude":
                    try:
                        import traceback as _tb
                        with open("/tmp/claude_probe_tb.log", "a") as _f:
                            _f.write(f"--- {time.ctime()} ---\n"); _tb.print_exc(file=_f); _f.write("\n")
                    except Exception:
                        pass
        threads = [threading.Thread(target=run, args=(l,), daemon=True) for l in due]
        [t.start() for t in threads]
        [t.join(240) for t in threads]          # the Claude /usage probe can take ~100 s when it retries the per-model panel
        for lane, v in results.items():
            v.setdefault("plan", plans.get(lane))
            v["plan"] = plans.get(lane) or v.get("plan")
            lanes[lane] = v
        out = {"fetched_at": now, "lanes": lanes, "learned_at": prev.get("learned_at"), "probe_sweep": sweep}
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1))
        tmp.replace(CACHE)
        if results and "--learn" in sys.argv or (results and time.time() - float(prev.get("learned_at") or 0) > 6 * 3600):
            try:
                import importlib.util as _ilu
                spec = _ilu.spec_from_file_location("cortados_unitcost", str(Path(__file__).with_name("unitcost.py"))); um = _ilu.module_from_spec(spec); spec.loader.exec_module(um)
                um.learn(); out["learned_at"] = time.time()
            except Exception:
                pass
        if results:
            with HISTORY.open("a") as h:
                h.write(json.dumps({"ts": now, "lanes": {l: {"scarcity": v.get("scarcity"), "windows": [[w["window"], w["used_pct"], w["elapsed_pct"]] for w in v.get("windows") or []]}
                                                        for l, v in results.items() if not v.get("error")}}) + "\n")
        return out


def current(max_age_s: int = 1200) -> dict[str, Any]:
    """Never blocks a turn: returns what is on disk and refreshes in the background when it is getting old."""
    try:
        d = json.loads(CACHE.read_text())
    except Exception:
        d = {"fetched_at": 0, "lanes": {}}
    if time.time() - float(d.get("fetched_at") or 0) > max_age_s and not _refreshing.is_set():
        _refreshing.set()

        def bg() -> None:
            try:
                sample()
            finally:
                _refreshing.clear()
        threading.Thread(target=bg, daemon=True, name="cortados-scarcity").start()
    return d


def lane_scarcity(max_reading_age_s: int = 6 * 3600) -> dict[str, dict[str, Any]]:
    out = {}
    for lane, v in (current().get("lanes") or {}).items():
        fresh = time.time() - float(v.get("fetched_at") or 0) < max_reading_age_s
        general = [w for w in v.get("windows") or [] if w.get("pool", "all") == "all"] or (v.get("windows") or [])
        roll = _roll(general)
        out[lane] = {"scarcity": v.get("scarcity") if fresh else None, "tightest": roll["tightest"], "expiring_pct": roll["expiring_pct"],
                     "refusing": bool(v.get("refusing")) and fresh, "pools": (v.get("pools") or {}) if fresh else {}}
    return out


def change_since(hours: float = 24) -> dict[tuple[str, str], float]:
    """Points of each window used over the last `hours`, from our own sample history (negative = the window reset in between)."""
    target, best = time.time() - hours * 3600, None
    try:
        for line in HISTORY.read_text().splitlines():
            row = json.loads(line)
            if best is None or abs(row["ts"] - target) < abs(best["ts"] - target):
                best = row
    except Exception:
        return {}
    if not best or abs(best["ts"] - target) > hours * 3600 * 0.25:
        return {}
    return {(lane, w[0]): w[1] for lane, v in best["lanes"].items() for w in v.get("windows") or []}


def stance(s: float | None) -> str:
    if s is None:
        return "unknown: routed on price only"
    if s >= 1.0:
        return "SPENT: skipped until the reset"
    if s >= 0.7:
        return "AVOID: burning fast, work moves to other lanes"
    if s >= 0.4:
        return "cautious: only when clearly cheapest"
    if s <= -0.4:
        return "ATTRACT: underused or expiring, work is pushed here"
    if s < 0:
        return "attract: some spare allowance"
    return "neutral"


def unit_costs() -> dict[str, Any]:
    try:
        return json.loads((STATE / "unit-cost.json").read_text()).get("pools") or {}
    except Exception:
        return {}


POOL_KEY = {("claude", "week (all models)"): "claude:all", ("claude", "week (fable)"): "claude:fable", ("codex", "week"): "codex:all",
            ("antigravity", "week (gemini)"): "antigravity:gemini", ("antigravity", "week (claude-gpt)"): "antigravity:claude-gpt",
            ("cursor", "month (Cursor models: Grok, Composer, Auto)"): "cursor:cursor-models", ("cursor", "month (other models: Claude, GPT, Gemini …)"): "cursor:other-models"}


def tokens_left(lane: str, window: str, used_pct: float) -> float | None:
    uc = unit_costs().get(POOL_KEY.get((lane, window), ""))
    return (100 - used_pct) * uc["tokens_per_pct"] if uc and uc.get("tokens_per_pct") else None


def _fmt_tokens(n: float | None) -> str:
    return "?" if n is None else (f"{n / 1e9:.1f} B" if n >= 1e9 else f"{n / 1e6:.0f} M" if n >= 1e6 else f"{n / 1e3:.0f} k")


def summary_lines(d: dict[str, Any]) -> list[str]:
    lines, before = [], change_since(24)
    for lane, v in (d.get("lanes") or {}).items():
        s = v.get("scarcity")
        lines.append(f"{lane}  [{v.get('plan')}]  scarcity {'?' if s is None else s} → router: {stance(s)}" + (f"  (last try failed: {v['error']})" if v.get("error") else ""))
        for w in v.get("windows") or []:
            reset = f"until it resets {w['resets_at']} (in {w['resets_in_h']} h)" if w.get("resets_at") else "(untouched: no reset clock running)"
            tl = tokens_left(lane, w["window"], w["used_pct"])
            lines.append(f"    {w['window']}{' (carried from last read)' if w.get('stale') else ''}: {w['used_pct']}% used at {w['elapsed_pct']}% of the window elapsed → on pace for {w['projected_pct_at_reset']}% at reset; "
                         f"{w['headroom_pct']}% left" + (f" ≈ {_fmt_tokens(tl)} tokens" if tl else "") + f" = {w['allowance_pct_per_h']}%/h {reset}"
                         + (f"  ← about {w['expiring_pct']}% will expire unused" if w.get("expiring_pct", 0) >= 10 else "")
                         + (f"  [24 h: {w['used_pct'] - before[(lane, w['window'])]:+.0f} pts]" if (lane, w["window"]) in before else ""))
        if lane == "local":
            lines.append(f"    free memory {v.get('free_gb')} GB, resident {v.get('resident')}")
        for pname, pv in (v.get("pools") or {}).items():
            lines.append(f"    pool {pname}: scarcity {pv.get('scarcity')}" + ("  SPENT" if pv.get("spent") else "") + (f"  ({pv['note']})" if pv.get("note") else ""))
        if v.get("per_model"):
            lines.append(f"    per-model weeks: {v['per_model']}")
    return lines


if __name__ == "__main__":
    only = [a for a in sys.argv[1:] if not a.startswith("-")] or None
    d = sample(force="--force" in sys.argv, only=only)
    print(json.dumps(d, indent=1) if "--json" in sys.argv else "\n".join(summary_lines(d)))
