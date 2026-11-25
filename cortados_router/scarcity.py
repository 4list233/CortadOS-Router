"""Subscription scarcity, in SUBSCRIPTION UNITS (never dollars).

For every lane: how much of the plan's window is used (%), how much of the window's TIME has elapsed (%), when it resets,
and what follows from the two: the pace (projected % at reset) and the allowance (% per hour we can still spend until the reset).
scarcity ∈ [0,1] is derived from that; 1.0 = the provider is refusing or the window is spent. Local lane: GPU memory, compute and deployment availability.

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
from cortados_router import agy_isolation as _agy_iso

import fcntl
import json
import logging
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
from typing import Any, Callable
from zoneinfo import ZoneInfo

STATE = Path(_env_get("CORTADOS_HOME") or _env_get("CORTADOS_HERMES_HOME") or Path.home() / ".cortados")
CACHE = STATE / "scarcity.json"
HISTORY = STATE / "scarcity-history.jsonl"   # one line per sample: the series the unit-cost learner and the crons read
PROBE_DIR = STATE / "usage-probe"            # empty folder the interactive CLIs are opened in
MIN_INTERVAL = {"claude": 900, "codex": 900, "cursor": 1800, "antigravity": 1800, "local": 60, "anthropic-api": 300}
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
    # counts linearly against scarcity, reaching its full weight at the reset: for a week 3 % used this term is ≈ −0.57 at 70 % elapsed and
    # ≈ −0.90 at 95 %; on top of the underused floor (−0.6) both clamp to −1.0.
    if reset_at and window_s >= 86400 and elapsed > 0.25:
        s = round(max(-1.0, s - (wasted / 100.0) * ((elapsed - 0.25) / 0.75)), 2)
    return {"window": name, "used_pct": round(used_pct, 1), "elapsed_pct": round(elapsed * 100, 1), "headroom_pct": round(headroom, 1),
            "projected_pct_at_reset": round(projected), "allowance_pct_per_h": round(headroom / max(remaining_s / 3600, 0.25), 2),
            "resets_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(reset_at)) if reset_at else None,
            "resets_in_h": round(remaining_s / 3600, 1) if reset_at else None, "scarcity": round(s, 2),
            # The absolute reset time: resets_in_h is relative to the sample and goes stale as the reading ages; a reader
            # that acts between samples (the S1 outcome loop's quota door) recomputes the countdown from this.
            "reset_epoch": float(reset_at) if reset_at else None,
            # use-it-or-lose-it: headroom the reset will wipe (what we'd still have left at today's pace), only meaningful near the reset
            "expiring_pct": round(wasted) if reset_at and window_s >= 86400 and elapsed >= 0.85 else 0}


# ------------------------------------------------------------------ PTY screen capture (interactive CLIs, no tokens spent)
_CSI = re.compile(rb"\x1b\[([0-9;?<>=]*)([A-Za-z])")


def _plain(raw: bytes) -> str:
    raw = re.sub(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x1b]*\x1b\\|\x1b[=>()][A-Za-z0-9]?", b"", raw)
    raw = _CSI.sub(lambda m: b" " * min(int(m.group(1) or 1), 200) if m.group(2) == b"C" and (m.group(1) or b"1").isdigit()
                   else (b"\n" if m.group(2) in b"HdG" else b""), raw)
    return re.sub(r"[ \t]+", " ", raw.decode("utf-8", "replace"))


# The trust prompt's Enter (#144 fix round 2, review P1): the prompt is answerable only while it is the LAST thing the CLI
# painted. Round 1 looked at the text since the last full redraw, but a line-by-line erase (cursor up + erase line) or
# cursor home + SGR + erase-below removed the visible prompt and left it counted, so the Enter reached whatever screen
# came next. Now: after the end of the last trust-prompt match, only bytes that cannot remove or replace it may follow.
# Every other byte, escape sequence (erase, cursor move, scroll, alt-screen, reset, title, anything not understood,
# an unfinished one) or line of text waits until the full prompt is painted again. Fail closed: a missed probe costs one
# sample, a stray Enter answers a screen nobody read.
_ESC_TOKEN = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[^\[\]])?")
_HARMLESS_ESC = re.compile(rb"\x1b\[[0-9;:]*m"            # SGR colour / weight: changes no character on screen
                           rb"|\x1b\[\?25[hl]"             # cursor show / hide
                           rb"|\x1b\[>?0?c|\x1b\[6n")      # terminal identity and cursor-position queries: paint nothing
_TRUST_OPTION_LINE = re.compile(          # the prompt's own option and hint lines, e.g. "> 1. Yes, proceed", "Enter to confirm"
    r"\s*(?:[>❯›▸•*-]\s*)?(?:\[?\d\]?[.):]?\s*)?"
    r"(?:yes|no|y/n|\[y/n\]|exit|quit|cancel|(?:do not|don'?t) trust|trust|(?:press |use )?(?:enter|esc|↑|↓))"
    r"(?:[\s,.!:;/·()-]|$)(?!.*(?:sign|log\s?in|account|auth|google|consent|access|load|wait|connect))"
    r"[^\n]{0,60}", re.I)
_TRUST_TAIL_LINES = 6        # option and hint lines allowed after the prompt; more is another screen
# The probe's PTY (tui_capture sets it with TIOCSWINSZ) and the bounds of what may follow the prompt (#144 fix round 3,
# review P1: 70 blank lines, or 10,000 spaces before an option line, scrolled the prompt off the 60-row screen yet left it
# "last"). Rows are modelled as the PTY would: every LF moves the cursor down one row (blank lines count), a line wider
# than the PTY wraps (indentation counts), and the prompt's row stays on screen only while the cursor has moved at most
# rows - 1 below it (whatever row it started on: a scroll of k rows needs the cursor k rows past the bottom).
_PTY_ROWS, _PTY_COLS = 60, 160
_TRUST_TAIL_MAX_BYTES = 1024  # everything after the prompt, escapes included; more is another screen


def _cells(text: str, col: int = 0) -> int | None:
    """The column the cursor reaches printing `text` from `col` without wrapping, or None for a character whose width
    the probe does not know (a control character): fail closed. Tabs go to the next stop of 8, East Asian wide and full
    width characters take 2 cells, combining marks none."""
    import unicodedata
    for ch in text:
        if ch == "\t":
            col += 8 - col % 8
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            return None
        elif unicodedata.combining(ch):
            continue
        else:
            col += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return col


def _trust_prompt_last(raw: bytes, prompt: str) -> bool:
    """True only if `prompt` (a regex) was painted and nothing painted after its last match can have removed, replaced
    or scrolled it off the screen (see the comments above). The match is searched in the printed text alone, SGR
    dropped; any other escape splits it."""
    pieces: list[tuple[bool, bytes]] = []      # (is_escape, bytes) in stream order
    pos = 0
    for m in _ESC_TOKEN.finditer(raw):
        if m.start() > pos:
            pieces.append((False, raw[pos:m.start()]))
        pieces.append((True, m.group()))
        pos = m.end()
    if pos < len(raw):
        pieces.append((False, raw[pos:]))
    flat: list[str] = []
    where: list[tuple[int, int]] = []          # flat char -> (piece index, byte offset after it within the piece)
    for i, (esc, b) in enumerate(pieces):
        if esc:
            if not re.fullmatch(rb"\x1b\[[0-9;:]*m", b):
                flat.append("\x00")
                where.append((i, len(b)))
            continue
        for j in range(len(b)):
            flat.append(chr(b[j]))             # bytes as latin-1 chars: the prompt is ASCII, offsets stay byte offsets
            where.append((i, j + 1))
    text = "".join(flat)
    last = None
    for last in re.finditer(prompt, text, re.I):
        pass
    if last is None or last.end() == 0:
        return False
    i, off = where[last.end() - 1]
    tail = [(False, pieces[i][1][off:])] + pieces[i + 1:]
    if sum(len(b) for _, b in tail) > _TRUST_TAIL_MAX_BYTES:
        return False
    # the column the prompt's match ends on: the printed text since the last CR or LF; unknown when an escape other than
    # SGR stands in between (the cursor may have been placed anywhere): then the rest of its line counts as one wrap
    head = text[:last.end()]
    cut = max(head.rfind("\r"), head.rfind("\n"), head.rfind("\x00"))
    end_col = None
    if cut < 0 or head[cut] != "\x00":
        try:
            end_col = _cells(head[cut + 1:].encode("latin-1").decode("utf-8"))
        except UnicodeDecodeError:
            end_col = None
    line, lines, first = b"", 0, True
    rows = 0                                   # rows the cursor has moved below the prompt's last row
    for k, (esc, b) in enumerate(tail):
        if esc:
            if not _HARMLESS_ESC.fullmatch(b):
                return False
            continue
        for j, c in enumerate(b):
            if c == 0x0D:                      # CR only before LF or at the very end: never a carriage return to overwrite
                rest = b[j + 1:] or b"".join(x for e, x in tail[k + 1:] if not e)
                if rest and rest[:1] != b"\n":
                    return False
            elif c == 0x0A:
                if first:
                    if not _first_line_ok(line):
                        return False
                    rows += 1 + _rest_wraps(line, end_col)
                else:
                    if _line_width(line) is None:
                        return False
                    if line.strip():
                        lines += 1
                        if lines > _TRUST_TAIL_LINES or not _trust_line_ok(line):
                            return False
                    rows += 1                  # a blank line moves the cursor a row like any other
                if rows > _PTY_ROWS - 1:       # the prompt's row has scrolled off the top of the screen
                    return False
                line, first = b"", False
            elif c == 0x09 or c >= 0x20:
                line += bytes([c])
                if len(line) > 4 * _PTY_COLS:  # far wider than the screen whatever it decodes to
                    return False
            else:                              # backspace, BEL, any other control: not understood, so not answerable
                return False
    if first:
        return _first_line_ok(line) and _rest_wraps(line, end_col) <= _PTY_ROWS - 1
    if _line_width(line) is None:
        return False
    return not line.strip() or (lines < _TRUST_TAIL_LINES and _trust_line_ok(line))


def _line_width(line: bytes) -> int | None:
    """A line after the prompt in cells from column 0, or None if it is not UTF-8 or wider than the PTY (it would wrap:
    indentation counts like any other character)."""
    try:
        cells = _cells(line.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    return cells if cells is not None and cells <= _PTY_COLS else None


def _rest_wraps(rest: bytes, end_col: int | None) -> int:
    """Rows the rest of the prompt's own line adds by wrapping (it is at most 80 bytes, so at most one): one if the
    prompt's column is unknown and the rest is not empty (fail closed), else whether it passes the PTY's last column."""
    if not rest:
        return 0
    cells = _cells(rest.decode("utf-8", "replace"), end_col or 0)
    return 1 if end_col is None or cells is None or cells > _PTY_COLS else 0


def _first_line_ok(rest: bytes) -> bool:
    """The rest of the prompt's own line ("[Y/n]", " folder?"): plain UTF-8 text, short."""
    try:
        shown = rest.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return len(rest) <= 80 and _cells(shown) is not None


def _trust_line_ok(line: bytes) -> bool:
    try:
        shown = line.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(_TRUST_OPTION_LINE.fullmatch(shown))


def _boot_failure_kind(text: str) -> str:
    """Classify startup failures without persisting terminal text or credentials."""
    for kind, pattern in (
        ("package", r"no complete local package"),
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


# Written by the forked child to the PTY when exec fails, so the parent can tell "the CLI never started" from "the CLI
# started and printed nothing" (#144: a missing agy read as an empty screen and was reported as a parser failure).
EXEC_FAILED_MARK = b"\n[cortados-probe] exec failed: "


class ProbeExecFailed(RuntimeError):
    """The probe's CLI could not be started (exec failed, or it exited at once with no output)."""
    kind = "exec-failed"


class ProbePartialReading(RuntimeError):
    """The quota screen was read, but not all of it (a pager not paged to its end, a group without its windows)."""
    kind = "partial-reading"


class ProbeRefused(RuntimeError):
    """The CLI showed a screen the probe never answers (a login, a first-run choice). The probe stopped at once, before
    its next write; `typed` is what it had sent before the screen appeared (named keys, never terminal replies)."""

    def __init__(self, kind: str, stage: str, typed: list[str] | tuple[str, ...] = ()):
        did = ("nothing was typed" if not typed
               else "stopped at once; typed before it appeared: " + ", ".join(typed))
        super().__init__(f"probe refused at {stage}: the CLI is waiting for {kind}; {did}")
        self.kind = kind
        self.stage = stage
        self.typed = list(typed)
        self.facts: dict[str, Any] = {}   # set by tui_capture: fixed names and counts only (#144)


def _screen_has(test: str | Callable[[str], bool], shown: str, flags: int = 0) -> bool:
    """A screen test: a regex searched in `shown`, or a predicate called with it (agy's account-line proof, #144)."""
    return bool(test(shown)) if callable(test) else bool(re.search(test, shown, flags))


def tui_capture(argv: list[str], command: str, done: str, env: dict[str, str], boot_s: float = 12, run_s: float = 20,
                ready: str | Callable[[str], bool] | None = None,
                nudge: tuple[str, str, bytes, int, float] | None = None, diagnostics: dict | None = None,
                refuse: tuple[tuple[str, str], ...] = (), inherit_path: bool = True,
                page: Callable[[str], tuple[bytes, str] | None] | None = None, page_max: int = 40,
                cwd_check: Callable[[Path], None] | None = None, on_boot: Callable[[str], None] | None = None,
                refuse_unsettled: tuple[tuple[str, str | Callable[[str], bool]], ...] = (),
                refusal_facts: Callable[[str], dict[str, Any]] | None = None,
                ready_after_trust: str | Callable[[str], bool] | None = None) -> str:
    """Open the CLI in a PTY, accept a folder-trust prompt for our own empty probe folder if shown, type `command`, return the screen text.

    Deny-all: Enter is only ever sent for that trust prompt. `refuse` is ((kind, regex), ...): the screen is checked on
    every read and again before every write (#144 review: a sign-in screen that appeared only after `command` used to
    receive the retry's typing); on a match ProbeRefused(kind) is raised at once and the CLI is closed. Its message says
    truly what had been typed by then. An exec failure raises ProbeExecFailed instead of returning an empty screen.
    inherit_path=False runs argv[0] as given (an absolute path), never searched on PATH.
    `page(text)` reads a scrollable answer: called with the answer so far, it returns (key, name) to press next (a
    navigation key, never text) or None when done; at most `page_max` presses.
    `cwd_check(folder)` runs on the per-probe folder after it is made and before the fork; raising refuses the start
    (the agy lane's dotenv guard, #144 round 3).
    `on_boot(text)` gets the start-up screen (banner, trust prompt) once, just before `command` is typed: what the
    returned text (the answer only) does not hold (the agy lane reads its subscription tier there, #144).
    `refuse_unsettled` is ((kind, regex), ...) like `refuse`, but checked only when the probe is about to act (at the end
    of the boot / trust stage and before every write), never mid-read: for a transient screen that may still resolve
    while the CLI paints (agy's "not signed in" notice while it signs in from the keyring, #144).
    The trust prompt counts only while it is the LAST thing painted (`_trust_prompt_last`: after it only SGR, cursor
    show/hide, terminal queries, whitespace and its own option lines, within the PTY's rows and columns and 1 KB), checked when the boot read ends and again in the
    write path itself: a prompt erased or replaced (by an account picker, or by anything at all) gets no Enter, and
    every check applies. The read after the answer gets what is left of the one boot_s window.
    `ready` and each `refuse_unsettled` test are a regex or a predicate on the plain screen text (_screen_has).
    `on_boot` may raise to refuse: it runs before the first keystroke of `command`.
    The trust prompt's Enter is checked against `refuse` only, not `refuse_unsettled`: the prompt is not a sign-in screen,
    and a CLI may paint it before it has finished signing in (agy 1.5.16 on the Air, 2026-11-25: notice, spinner, trust
    prompt, and only then the banner). After the answer the read goes on until `ready_after_trust` (default `ready`)
    is painted, within boot_s, and `refuse_unsettled` applies before the first keystroke of `command` as always.
    A ProbeRefused carries `facts`: fixed names and numbers only (which check, mid-read or about to act, ready seen,
    seconds since start), plus `refusal_facts(screen)` when given; never screen text."""
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    my_dir = PROBE_DIR / f"p{os.getpid()}-{int(time.time() * 1000) % 100000}"     # per-probe folder: the reaper kills by cwd and must never hit another probe
    my_dir.mkdir(exist_ok=True)
    if cwd_check is not None:
        try:
            cwd_check(my_dir)
        except BaseException:
            import shutil
            shutil.rmtree(my_dir, ignore_errors=True)
            raise
    started = time.time()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(my_dir)
            child_env = {**env, "TERM": "xterm-256color"}
            if inherit_path:
                os.execvpe(argv[0], argv, child_env)
            else:
                os.execve(argv[0], argv, child_env)
        except BaseException as exc:
            try:
                os.write(1, EXEC_FAILED_MARK + type(exc).__name__.encode() + b"\n")
            except BaseException:
                pass
        finally:
            os._exit(127)    # exec failed (e.g. missing binary): never let the forked copy run the sampler's code
    _reaper.track(pid, fd, my_dir, PROBE_DIR)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0))
    buf = b""
    eof = False
    stage, startup_chars, ready_seen, trust_seen = "boot", 0, False, False
    typed: list[str] = []          # what the probe sent, by name, for a truthful refusal message

    def refusal(kind: str, where: str, check: str, acting: bool, shown: str) -> ProbeRefused:
        exc = ProbeRefused(kind, where, typed)
        facts: dict[str, Any] = {"check": check, "acting": acting, "after_s": round(time.time() - started, 2),
                                 "ready_seen": bool(ready and _screen_has(ready, shown)), "trust_prompt": trust_seen}
        if refusal_facts is not None:
            try:
                facts.update(refusal_facts(shown))
            except Exception:                  # diagnostics never change the refusal itself
                facts["facts_error"] = True
        exc.facts = facts
        return exc

    def refused(where: str, acting: bool = True) -> None:
        if EXEC_FAILED_MARK in buf:
            raise ProbeExecFailed(f"{Path(argv[0]).name} could not be started: "
                                  + buf.split(EXEC_FAILED_MARK, 1)[1].split(b"\n", 1)[0].decode("ascii", "replace")[:60])
        if refuse:
            shown = _plain(buf)
            for kind, pattern in refuse:
                if re.search(pattern, shown, re.I):
                    raise refusal(kind, where, "refuse", acting, shown)
        if acting and refuse_unsettled:
            shown = _plain(buf)
            for kind, pattern in refuse_unsettled:
                if _screen_has(pattern, shown, re.I):
                    raise refusal(kind, where, "unsettled", acting, shown)

    def trust_on_screen() -> bool:
        """The trust prompt is the last thing painted (_trust_prompt_last), not merely somewhere in the transcript."""
        return _trust_prompt_last(buf, _TRUST_PROMPT)

    def send(data: bytes, name: str, unsettled: bool = True) -> bool:
        """Every keystroke the probe types goes through here: the screen is checked first (deny-all). unsettled=False
        only for the workspace-trust answer, and only while the trust prompt is the last thing painted: `refuse` still
        applies; `refuse_unsettled` waits for the next keystroke. If the prompt is no longer last, every check applies
        and the answer is NOT written (returns False): it was meant for that prompt and nothing else."""
        if not unsettled and not trust_on_screen():
            refused(stage, acting=True)
            return False
        refused(stage, acting=unsettled)
        os.write(fd, data)
        typed.append(name)
        return True

    def pump(max_s: float, quiet_s: float = 1.5, until: str | Callable[[str], bool] | None = None, mark: int = 0) -> None:
        """Read until the screen goes quiet, or (with `until`) until that text is on screen and has had a moment to finish painting."""
        nonlocal buf, eof
        end, last, hit = time.time() + max_s, time.time(), 0.0
        while time.time() < end:
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
                refused(stage, acting=False)                 # stop the moment a refused screen is painted, mid-pump
                if b"\x1b[>c" in d or b"\x1b[>0c" in d:      # terminal identity queries some TUIs block on
                    os.write(fd, b"\x1b[>0;276;0c")
                if b"\x1b[c" in d:
                    os.write(fd, b"\x1b[?62;c")
                if b"\x1b[6n" in d:
                    os.write(fd, b"\x1b[1;1R")
            if until:
                if not hit and len(buf) > mark and _screen_has(until, _plain(buf[mark:])):
                    hit = time.time()
                if hit and time.time() - hit > 1.5:
                    return
            elif len(buf) > mark and time.time() - last > quiet_s:
                return
    mark = 0
    # One boot window for the read before AND after the trust answer, started when the CLI first paints: its own
    # start-up (a cold binary, an interpreter on a loaded machine) is waited for separately, up to boot_s, so it never
    # eats the window in which its screens are judged (#144 fix round 3: a fake agy that started 3 s late was read as an
    # empty screen). Nothing painted within boot_s: an empty window, as before. Deny-all is unchanged by when it starts.
    boot_end = time.time() + boot_s

    def boot_left() -> float:
        return max(0.0, boot_end - time.time())
    try:
        first_paint, _, _ = select.select([fd], [], [], boot_s)
        boot_end = time.time() + (boot_s if first_paint else 0.0)
        pump(boot_left(), until=ready)
        startup_chars = len(_plain(buf))
        ready_seen = bool(ready and _screen_has(ready, _plain(buf)))
        trust_seen = trust_on_screen()               # the prompt as the last thing painted; an erased one is not answered
        refused("boot", acting=not trust_seen)        # a trust prompt is answered first; unsettled is judged after it
        if eof:
            mark = len(buf)
            return ""
        if trust_seen:
            stage = "trust"
            if not send(b"\r", "Enter (workspace trust)", unsettled=False):   # no longer last: no Enter, full checks
                trust_seen = False
            if ready_after_trust is not None and _screen_has(ready_after_trust, _plain(buf)):
                pump(boot_left(), mark=len(buf))        # already painted before the prompt: read until quiet
            else:
                pump(boot_left(), until=ready_after_trust or ready, mark=len(buf))
            startup_chars = len(_plain(buf))
            ready_seen = bool(ready and _screen_has(ready, _plain(buf)))
            refused("trust")
            if eof:
                mark = len(buf)
                return ""
        mark = len(buf)
        if on_boot is not None:
            on_boot(_plain(buf))
        stage = "command"
        send(command.encode(), repr(command))
        pump(3, quiet_s=1.0, mark=mark)
        send(b"\r", "Enter")
        pump(run_s, until=done, mark=mark)
        if not eof and not re.search(done, _plain(buf[mark:])):      # some screens answer "refreshing, ask again": ask once more
            stage = "retry"
            send(b"\x1b", "Esc")
            time.sleep(3)
            pump(0.5, quiet_s=0.3, mark=len(buf))        # read (and check) what arrived while waiting
            send(command.encode(), repr(command))
            pump(3, quiet_s=1.0, mark=len(buf))
            send(b"\r", "Enter")
            pump(run_s, until=done, mark=mark)
        if page and not eof:
            stage = "page"
            for _ in range(page_max):
                step = page(_plain(buf[mark:]))
                if not step:
                    break
                mark2 = len(buf)
                send(step[0], step[1])
                pump(4, quiet_s=0.6, mark=mark2)
                if eof or len(buf) == mark2:      # gone, or the key changed nothing on screen: stop pressing
                    break
        if nudge:                                        # (want, blocked_by, key, max_presses, wait_s): e.g. press r while a panel says "rate limited"
            want, blocked, key, presses, wait_s = nudge
            for _ in range(presses):
                shown = _plain(buf[mark:])
                if re.search(want, shown) or not re.search(blocked, shown):
                    break
                time.sleep(wait_s)
                mark2 = len(buf)
                try:
                    send(key, repr(key))
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


_TRUST_PROMPT = r"trust (this|the contents)|Trust this workspace"     # the one prompt tui_capture answers (Enter)


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


# agy 1.5.16's /usage answer is a scrollable pager: a footer "(1–17 of 30 lines)" under each painted view, ↑/↓, pgdown,
# ctrl+end. Each painted view is parsed on its own, so a window is only ever attributed to a group header shown above
# it in the same view (a view that starts below a header holds orphan windows, which are ignored).
AGY_PAGER = re.compile(r"\(\s*(\d+)\s*[–—-]\s*(\d+)\s+of\s+(\d+)\s+lines?\s*\)")
AGY_WINDOW_KINDS = ("week", "5-hour")


def _agy_views(text: str) -> list[str]:
    """The painted views of the /usage pager, in order: the text up to and including each footer; without a footer the
    whole text is one view."""
    views, start = [], 0
    for m in AGY_PAGER.finditer(text):
        views.append(text[start:m.end()])
        start = m.end()
    tail = text[start:]
    if tail.strip() or not views:
        views.append(tail)
    return views


def parse_agy_usage(text: str) -> dict[str, list[dict[str, Any]]]:
    """{group title (lower case): [week, 5-hour] windows found}. Views are merged per group and window kind, the first
    reading of each kind kept (a repaint shows the same numbers): a first page that shows only part of a group no longer
    hides the rest of it on a later page (#144 review round 1)."""
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for view in _agy_views(text):
        # Group headers: "GEMINI MODELS", "CLAUDE AND GPT MODELS" (2026-10 and 1.5.16 screens); "THIRD-PARTY MODELS" allowed
        parts = re.split(r"([A-Z][A-Z0-9 &/-]{2,40} MODELS) (?=Models within)", re.sub(r"\s+", " ", view))
        for title, chunk in zip(parts[1::2], parts[2::2]):
            got = found.setdefault(title.strip().lower(), {})
            for m in re.finditer(r"(Weekly|Five Hour) Limit Remaining\D{0,200}?(\d{1,3}(?:\.\d+)?)\s*%\s*(Refreshes in ([0-9dhm ]+)|Quota available)?", chunk):
                kind = "week" if m.group(1) == "Weekly" else "5-hour"
                if kind in got:
                    continue
                secs = sum(int(n) * {"d": 86400, "h": 3600, "m": 60}[u] for n, u in re.findall(r"(\d+)\s*([dhm])", m.group(4) or ""))
                got[kind] = window_view(kind, 100.0 - float(m.group(2)), time.time() + secs if secs else None,
                                        D7 if kind == "week" else H5)
    return {t: [w[k] for k in AGY_WINDOW_KINDS if k in w] for t, w in found.items() if w}


def agy_usage_gaps(text: str) -> list[str]:
    """What a /usage answer still lacks before it is a full quota reading; [] = complete. Every expected pool
    (AGY_EXPECTED_POOLS) and every group shown needs its week and its 5-hour window, and a pager must have been read to
    its last line. A partial reading is an error, never a healthy reading (#144 review round 1)."""
    groups = parse_agy_usage(text)
    gaps = []
    labels = agy_group_labels(groups)
    for pool in AGY_EXPECTED_POOLS:
        if pool not in labels.values():
            gaps.append(f"no {pool} group")
    for title, wins in groups.items():
        have = {w["window"] for w in wins}
        # the fixed label, never the title as shown: a gap goes into the error, the state and stderr (#144 rounds 3-4)
        gaps += [f"{labels[title]}: no {k} window" for k in AGY_WINDOW_KINDS if k not in have]
    pages = list(AGY_PAGER.finditer(text))
    if pages and int(pages[-1].group(2)) < int(pages[-1].group(3)):
        gaps.append(f"pager not read to the end (last view {pages[-1].group(1)}-{pages[-1].group(2)} of {pages[-1].group(3)})")
    return gaps


def _agy_next_page(text: str) -> tuple[bytes, str] | None:
    """The /usage pager's next key: ↓ (one line, read-only navigation) until the reading is complete or the footer shows
    the last line. One line at a time, not pgdown or ctrl+end: a group is attributed only from a view that shows its
    header above its windows, and stepping by one line shows every group that fits in a view whole in some view (a
    page jump can land with a header just above the top). No footer: not a pager, nothing to press."""
    pages = list(AGY_PAGER.finditer(text))
    if not pages or not agy_usage_gaps(text):
        return None
    if int(pages[-1].group(2)) >= int(pages[-1].group(3)):
        return None
    if len(pages) >= 2 and pages[-1].group(1, 2) == pages[-2].group(1, 2):
        return None                    # the last press did not move the view: stop (the reading stays partial)
    return b"\x1b[B", "Down"


def _env_file_vars() -> dict[str, str]:
    """The router's shared env file ($CORTADOS_HOME/env, KEY=value lines) applied to every vendor CLI probe."""
    out: dict[str, str] = {}
    f = STATE / "env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v.strip().strip('"')
    return out


def _cli_env() -> dict[str, str]:
    """The env of the claude / codex / cursor probes: the whole process env plus the env file. Never used for agy, which
    runs on agy_isolation.child_env's allowlist (#144)."""
    env = {**os.environ, **_env_file_vars()}
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    return env


def _accounts(vendor: str) -> list[dict[str, Any]]:
    """Configured accounts for a vendor from tables/accounts.json, or [] (the single default login). Shape:
    {"claude": [{"id": "a", "config_dir": "~/.claude-acctA"}, {"id": "b", "config_dir": "~/.claude-acctB"}]}."""
    from cortados_router.paths import tables_dir
    p = tables_dir() / "accounts.json"
    if not p.exists():
        return []                                              # absent: single default login
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"accounts.json present but unreadable: {exc}")   # broken: caller refuses Claude sampling, never samples the wrong login
    if not isinstance(data, dict):
        raise ValueError("accounts.json: root must be a JSON object")
    if vendor not in data:
        return []
    raw = data[vendor]
    if not isinstance(raw, list):                              # explicit null / scalar / object → malformed
        raise ValueError(f"accounts.json: {vendor!r} must be a list of accounts")
    for a in raw:
        if not isinstance(a, dict) or not (isinstance(a.get("config_dir"), str) and a["config_dir"].strip()):
            raise ValueError(f"accounts.json: each {vendor!r} account needs a non-empty config_dir")
    return raw


def claude_lanes() -> list[tuple[str, str | None]]:
    """(lane_key, config_dir) per Claude account. The first account keeps the bare `claude` key (back-compat); the rest
    are `claude#<id>`. No accounts.json → [("claude", None)], i.e. today's single default login, unchanged."""
    try:
        accts = _accounts("claude")
    except Exception:
        return []                                              # broken config: skip Claude sampling rather than read the wrong login
    if not accts:
        return [("claude", None)]
    lanes = [("claude" if i == 0 else f"claude#{a.get('id') or i}", os.path.expanduser(a["config_dir"]))
             for i, a in enumerate(accts)]
    if len({k for k, _ in lanes}) != len(lanes):               # duplicate account id → collapsed sampling key: refuse rather than sample one dir for two
        return []
    return lanes


# Env that makes a Claude CLI authenticate as something other than the login stored in its config dir, or send the turn
# to another transport/provider: with any of them set, the config dir no longer names the effective login. Kept in step
# with what the routed-subagents mod blanks on the child (a custom x-api-key header, a Unix socket, the alternative-
# provider selectors), since any of them means the reading cannot be pinned to one stored login.
_LOGIN_OVERRIDES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_UNIX_SOCKET", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
                    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD")


def claude_login_identity(env: dict[str, str]) -> str | None:
    """The login a Claude CLI started with `env` runs on: its resolved config dir (CLAUDE_CONFIG_DIR, else
    $HOME/.claude). None when the env cannot be pinned to one stored login (an override above is set)."""
    if any(env.get(k) for k in _LOGIN_OVERRIDES):
        return None
    ccd = env.get("CLAUDE_CONFIG_DIR")
    if ccd:
        d = os.path.expanduser(ccd)
        if not os.path.isabs(d):
            return None                 # a relative CLAUDE_CONFIG_DIR resolves against each process's cwd, which the
                                        # sampler and the bridge do not share -> cannot pin one login, so do not claim one
        return os.path.realpath(d)
    home = env.get("HOME") or os.path.expanduser("~")
    if not os.path.isabs(home):
        return None
    return os.path.realpath(os.path.join(home, ".claude"))


# Env that makes a Codex CLI authenticate with something other than the stored login of its CODEX_HOME (an API key, an
# access token, another endpoint): with any set, the env cannot be pinned to one stored login. The provider's Codex
# backend runs on an allowlisted env that holds none of them (codex_isolation.child_env, child_env.py, #106), so its
# login is always the home's auth.json.
_CODEX_LOGIN_OVERRIDES = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL")


def codex_login_identity(env: dict[str, str]) -> str | None:
    """The login a Codex CLI started with `env` runs on: its resolved CODEX_HOME (else $HOME/.codex), whose auth.json
    holds the login. None when the env cannot be pinned to one stored login (an override above, or a relative path).
    Two homes holding the same ChatGPT account are two identities here: conservative, never wrongly equal."""
    if any(env.get(k) for k in _CODEX_LOGIN_OVERRIDES):
        return None
    ch = env.get("CODEX_HOME")
    if ch:
        d = os.path.expanduser(ch)
        return os.path.realpath(d) if os.path.isabs(d) else None
    home = env.get("HOME") or os.path.expanduser("~")
    if not os.path.isabs(home):
        return None
    return os.path.realpath(os.path.join(home, ".codex"))


def _ensure_trust(config_dir: str) -> None:
    """A fresh per-account CLAUDE_CONFIG_DIR has not accepted the workspace-trust dialog, which blocks the /usage TUI on
    the folder the probe runs in. Pre-accept it for the home folder so the probe reaches the usage screen."""
    try:
        p = Path(config_dir) / ".claude.json"
        cfg = json.loads(p.read_text()) if p.exists() else {}
        proj = cfg.setdefault("projects", {}).setdefault(str(Path.home()), {})
        if not proj.get("hasTrustDialogAccepted"):
            proj["hasTrustDialogAccepted"] = True
            p.write_text(json.dumps(cfg, indent=1))
    except Exception:
        pass                                                   # best-effort; a real block still surfaces as a probe error


def _claude(prev: dict[str, Any] | None = None, config_dir: str | None = None) -> dict[str, Any]:
    # the per-model weeks (Fable, Opus, …) load from a rate-limited endpoint; the screen offers "r to retry". Two gentle presses, never more.
    env = _cli_env()
    if config_dir:                                             # a named account: read THIS login's usage, not the default
        env["CLAUDE_CONFIG_DIR"] = config_dir
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)               # an inherited ambient token would override the account's stored login
        _ensure_trust(config_dir)
    text = tui_capture(["claude", "--strict-mcp-config"], "/usage", r"% used[\s\S]*% used", env, run_s=25,
                       nudge=(r"Current week \((?!all models)[^)]+\)\D{0,160}?\d+\s*% used", r"breakdown unavailable", b"r", 2, 8.0))
    wins = parse_claude_usage(text)
    if not wins:
        raise RuntimeError("no '% used' lines on the /usage screen: " + text[-200:].replace("\n", " "))
    # `login`: the login this CLI EFFECTIVELY ran on (its env, not just the explicit config_dir arg, which is None both
    # for "the default login" and for an inherited CLAUDE_CONFIG_DIR): what a reader confirms before showing the %.
    out: dict[str, Any] = {"source": "claude /usage (pty)", "config_dir": config_dir, "login": claude_login_identity(env)}
    if not any(w["pool"] != "all" for w in wins) and (prev or {}).get("config_dir") == config_dir:
        # carry the last per-model reading forward only when it was sampled for THIS same account (config_dir): after a
        # reorder, the positional lane key may now point to a different login, and its old windows must not cross over.
        # (This is main's behaviour, kept byte-for-byte so the cosmetic reveal never changes a routing input: it carries
        # ONLY when no per-model panel showed at all. The reveal's % is withheld for any model whose own separate pool
        # is not observed this read; it does not need a routing-visible carry to do that.)
        out["per_model"] = "not shown this read (vendor rate limit on the per-model panel)"
        for w in (prev or {}).get("windows") or []:          # keep the last per-model reading until its own reset passes
            if w.get("pool", "all") != "all" and w.get("resets_at") and w["resets_at"] > time.strftime("%Y-%m-%d %H:%M"):
                wins.append({**w, "stale": True})
    return {**out, "windows": wins}


# Boot screens the agy probe never answers (deny-all, #144): it closes the CLI at once and records the kind. PROVISIONAL
# wording: the first-run prompts are from Google's getting-started page ("Color scheme", "Rendering mode: Alt-Screen or
# Inline"); the sign-in screen is the documented flow (a browser sign-in, or over SSH an authorization URL and a code to
# paste back). Pin both to captured screens once the owner sends them.
#
# agy 1.5.16 SIGNED IN still paints, while it loads and refreshes its keyring token, a transient start-up notice
# (captured on the Air 2026-11-25 under the probe's env, email-free):
#     Welcome to the Antigravity CLI. You are currently not signed in.
#     ⣷ Signing in...
# then the banner with the account line `<email> (Google AI Pro)`. Refusing on that notice mid-read closed agy ~0.4 s
# before its token refresh finished, so a signed-in agy always read "not logged in" (#144). The notice is therefore not
# refused while agy says it is signing in or nothing follows it yet; AGY_UNSETTLED refuses it when the probe is about to
# act (end of boot, before any keystroke) and no account line has appeared after it. Deny-all holds: nothing is typed
# past an unresolved "not signed in".
#
# "Account line" is the banner's `<email> (Google AI Pro|Ultra)` on one line, proven by agy_isolation.banner_tier, the
# same function the tier check uses: a bare email elsewhere (a "Choose account: x@y" picker, a sign-in prompt) neither
# ends the boot read nor clears the notice (#144 fix round 1: a picker's email let the probe type "/usage" into it).
_AGY_NOT_SIGNED_IN = r"not (?:signed|logged) in"
# The sign-in alternatives by a fixed name: AGY_REFUSE's not-logged-in pattern is exactly their join (same order), and the
# name of the one that matched goes into a refusal's diagnostics (agy_refusal_facts), never the screen text (#144).
_AGY_SIGN_IN_RULES = (
    ("google-accounts-url", r"accounts\.google\.com"),
    ("sign-in-with-google", r"sign in with google"),
    ("authorization-url-or-code", r"authori[sz]ation (?:url|code)|paste (?:the|your) (?:authori[sz]ation )?code"),
    ("not-signed-in-notice", _AGY_NOT_SIGNED_IN + r"(?![.\s]*\S?\s*signing in)(?![.\s]*$)"),
    ("please-sign-in", r"please (?:sign|log) in|login required"),
    # an account choice or a consent screen is part of signing in, never answered (#144 fix round: a picker painted
    # after a cleared trust prompt matched no pattern and got the trust Enter)
    ("account-picker", r"choose (?:an? |your )?account|select (?:an? |your )?account|use another account"),
    ("consent", r"wants to access your google account|(?:grant|allow) (?:access|permission)|\bconsent\b"),
    # agy 1.5.16 signed out, as drawn to a headless child on the Air (2026-11-25, described, email-free): the notice, a
    # "Signing in..." spinner, then a bare-CR overwrite to "Select login method:" / "> 1. Google OAuth" / "2. Use a Google
    # Cloud project" / "↑/↓ Navigate · enter Select". Refused mid-read: no answer to it is ever typed (#144 fix round 3)
    ("login-method-picker", r"select (?:a |your )?log\s?in method|google oauth|use a google cloud project"),
)
AGY_REFUSE = (
    ("not-logged-in", "|".join(rule for _, rule in _AGY_SIGN_IN_RULES)),
    ("first-run-pending", r"color scheme|colou?r theme|rendering mode|alt-screen"),
)


def _agy_unsettled(shown: str) -> bool:
    """True while the last "not signed in" notice on screen has no banner account line after it."""
    hits = list(re.finditer(_AGY_NOT_SIGNED_IN, shown, re.I))
    return bool(hits) and _agy_iso.banner_tier(shown[hits[-1].end():]) is None


AGY_UNSETTLED = (("not-logged-in", _agy_unsettled),)


def agy_refusal_facts(shown: str) -> dict[str, Any]:
    """What a refused agy boot screen held, as fixed names and counts only (never screen text or an email): which sign-in
    rule matched, how many "not signed in" notices, "Signing in" spinners and banner account lines were painted, and
    whether an account line came before the last notice. Kept on the failed record so a refusal of a signed-in agy can
    be told apart from a real sign-in screen without a hand run (#144, the 06:41 sample on the Air)."""
    scrubbed = _agy_iso.scrub_account(shown)        # one text for every offset below
    notices = list(re.finditer(_AGY_NOT_SIGNED_IN, scrubbed, re.I))
    accounts = list(_agy_iso.TIER_PATTERN.finditer(scrubbed))
    last_notice = notices[-1] if notices else None
    return {
        "rules": [name for name, rule in _AGY_SIGN_IN_RULES if re.search(rule, shown, re.I)],
        "notices": len(notices),
        "signing_in": len(re.findall(r"signing in", shown, re.I)),
        "account_lines": len(accounts),
        "account_line_before_last_notice": bool(last_notice and accounts and accounts[0].start() < last_notice.end()),
        "account_line_after_last_notice": bool(last_notice and accounts and accounts[-1].start() >= last_notice.end()),
        "chars_after_last_notice": len(scrubbed) - last_notice.end() if last_notice else None,
        "chars": len(scrubbed),
    }


def AGY_BOOT_READY(shown: str) -> bool:
    """The boot read stops once the banner's account line (signed in) or the trust prompt is painted, else after boot_s."""
    return _agy_iso.banner_tier(shown) is not None or bool(re.search(r"trust (?:this|the contents)", shown, re.I))


def AGY_SIGNED_IN(shown: str) -> bool:
    """After the trust answer the read waits for the banner's account line only: agy 1.5.16 paints the trust prompt
    while it is still signing in, before the banner (Air, 2026-11-25 10:12, #144)."""
    return _agy_iso.banner_tier(shown) is not None


AGY_DONE = r"Limit Remaining[\s\S]*Limit Remaining"     # the answer is on screen; complete only by agy_usage_gaps
# The quota groups agy 1.5.16 shows on Google AI Pro (owner's screen, 2026-11-24): GEMINI MODELS and CLAUDE AND GPT
# MODELS. A reading without either is partial.
AGY_EXPECTED_POOLS = ("gemini", "claude-gpt")
# Human words for a lane's state, by scarcity error_kind / record state: what the live view, the dashboard and the pulse
# pane print instead of "FAILED" when the cause is known (#144).
LANE_STATE_TEXT = {
    "not-configured": "not configured",
    "binary-missing": "not installed",
    "exec-failed": "could not start",
    "not-logged-in": "not logged in",
    "first-run-pending": "first-run setup pending",
    "forbidden-env": "refused: API key or ADC variable configured",
    "settings-refused": "refused: unsafe agy settings",
    "subscription-unproven": "refused: subscription login not proven",
    "partial-reading": "quota screen read only in part",
}


def lane_state(v: dict[str, Any] | None) -> str | None:
    """The human state of a cached lane record when its cause is known (LANE_STATE_TEXT), else None."""
    if not isinstance(v, dict):
        return None
    if v.get("state") in LANE_STATE_TEXT:
        return LANE_STATE_TEXT[v["state"]]
    return LANE_STATE_TEXT.get(v.get("error_kind")) if v.get("error") else None


def _agy_pool_of_group(title: str) -> str | None:
    """The fixed pool name of a known quota group, else None. Never derived from the title's text (#144 round 4)."""
    g = title.lower()
    if "gemini" in g:
        return "gemini"
    if re.search(r"claude|gpt|third|partner", g):
        return "claude-gpt"       # Pro's "paid-tier third-party models" (Claude Opus / Sonnet 5.5, GPT-OSS 120B on 2026-11-24)
    return None


def agy_group_labels(titles) -> dict[str, str]:
    """{title as shown: label} for every quota group. A known group keeps its fixed pool name (gemini, claude-gpt); any
    other gets "unknown-group-N" by order of first appearance. A label is never built from screen text: a slug of a title
    such as "PRIVATE CUSTOMER ACME MODELS" reached the error, the state and stderr (#144 review round 4)."""
    labels: dict[str, str] = {}
    n = 0
    for title in titles:
        pool = _agy_pool_of_group(title)
        if pool is None:
            n += 1
            pool = f"unknown-group-{n}"
        labels[title] = pool
    return labels


def _antigravity() -> dict[str, Any]:
    """agy /usage through the isolation of agy_isolation (#144): a configured absolute binary (unset: not sampled), an
    allowlisted env (a Google-family variable in the env file: refused), the settings guard, and a deny-all PTY run."""
    from cortados_router import agy_isolation as iso
    env_file = _env_file_vars()
    # the probe runs in a fresh folder under PROBE_DIR: the dotenv guard checks PROBE_DIR's walk now and the folder itself
    # (cwd_check) just before the fork (#144 round 3)
    binary, env = iso.prepare(_env_get("CORTADOS_AGY_BIN") or env_file.get("CORTADOS_AGY_BIN"), os.environ, env_file,
                              workdir=PROBE_DIR)
    diag: dict = {}
    boot: list[str] = []          # the start-up banner, in memory only: the tier is read from it below, never stored

    def proven_boot(shown: str) -> None:
        # the subscription is proven from the banner BEFORE "/usage" is typed (#144 fix round 1): a screen without the
        # banner's account line (a "Choose account" picker, a sign-in prompt) gets no keystroke at all
        boot.append(shown)
        iso.check_subscription(shown)
    # A refused screen at any point (boot, after trust, after /usage, while paging) raises ProbeRefused from inside the
    # capture, before the probe's next keystroke.
    text = tui_capture([binary], "/usage", AGY_DONE, env, refuse=AGY_REFUSE, refuse_unsettled=AGY_UNSETTLED,
                       ready=AGY_BOOT_READY, ready_after_trust=AGY_SIGNED_IN, inherit_path=False, diagnostics=diag,
                       page=_agy_next_page, cwd_check=lambda d: iso.check_dotenv(env["HOME"], d), on_boot=proven_boot,
                       refusal_facts=agy_refusal_facts)
    if not text.strip():
        if diag.get("eof") and diag.get("stage") in {"boot", "trust"}:
            raise ProbeExecFailed(f"agy exited at {diag.get('stage')} with no usable output")
        raise RuntimeError("agy /usage screen empty (stage=" + str(diag.get("stage")) + ")")
    groups = parse_agy_usage(text)
    if not groups:
        # a fixed diagnostic: never screen text (#144 round 3: a private line survived the email scrub into the record and
        # stderr). Counts only.
        raise RuntimeError(f"no quota groups on the /usage screen: {len(text.splitlines())} lines, "
                           f"{len(AGY_PAGER.findall(text))} pager footers read")
    gaps = agy_usage_gaps(text)
    if gaps:
        raise ProbePartialReading("agy /usage read only in part: " + "; ".join(gaps))
    # agy stores no auth mode in its settings (#144 round 2): the personal subscription is proven from its own screen:
    # from the banner before any keystroke (proven_boot) and again here, last, with the /usage answer, so a screen problem above is reported as itself. The tier is on the start-up banner (`<email> (Google AI
    # Pro)`), not in the /usage box; both are checked for a non-subscription login.
    tier = iso.check_subscription("\n".join(boot), text)
    wins = []
    labels = agy_group_labels(groups)     # unknown groups kept under fixed labels only (#144 round 4)
    for g, ws in groups.items():
        pool = labels[g]
        wins += [{**w, "window": f"{w['window']} ({pool})", "pool": pool} for w in ws]
    return {"source": "agy /usage (pty)", "windows": wins, "binary": binary, "tier": tier}


def _load_codex_screen():
    """Sibling module loaded by path (this plugin is not a package): the one Codex /status parser."""
    name = "cortados_router_scarcity_codex"
    if name not in sys.modules:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(name, str(Path(__file__).with_name("scarcity_codex.py")))
        mod = _ilu.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return sys.modules[name]


codex_screen = _load_codex_screen()


def parse_codex_status(text: str, now: float | None = None) -> list[dict[str, Any]]:
    """The Codex /status screen as windows. Raises on a screen it cannot read (scarcity_codex): the sample then keeps
    its last good reading marked with the error, and goes stale rather than fresh-and-wrong."""
    wins = []
    for e in codex_screen.parse_status(text, now):
        name = e["span"] if not e["model"] else f"{e['span']} ({e['model']})"
        w = window_view(name, 100.0 - float(e["left_pct"]), e["reset_at"], H5 if e["span"] == "5-hour" else D7, now)
        w["pool"] = "all" if not e["model"] else re.sub(r"[^a-z0-9]+", "-", e["model"].lower()).strip("-")
        wins.append(w)
    return wins


# The app bundles that may ship a Codex CLI. Only the bundle roots are hardcoded: what lives inside them is read from
# the bundle's own codex-package.json, so the next relocation inside Contents/Resources needs no code change here.
CODEX_APP_BUNDLES = ("/Applications/ChatGPT.app", "~/Applications/ChatGPT.app", "/Applications/Codex.app")
# Last resort only, for a bundle that ships no manifest. Until 2026-10 the CLI was Resources/codex; since then it lives in
# Resources/codex-cli/bin/codex. The Air has no `codex` on PATH, so the old single default exec'd a missing file: the
# forked child exited 127 at once and every probe read "stage=boot, eof=True".
CODEX_APP_BINARIES = ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                      "/Applications/ChatGPT.app/Contents/Resources/codex")


def _is_exe(path: str | None) -> bool:
    """An executable *file*. os.access(X_OK) alone is true for a directory named `codex`, which would then be forked."""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _bundle_codex_candidates(bundles, note, problem=None) -> list[str]:
    """Codex binaries inside app bundles: each Contents/Resources/codex-package.json (or one directory below it) names
    its CLI in "entrypoint", resolved against that manifest's own directory; then a bounded Resources/codex*/bin/codex
    for a bundle that ships no manifest. Depth is bounded on purpose: an unbounded walk of a bundle is slow and would
    happily find an unrelated `codex`. Every path considered is recorded via note(); every unreadable manifest or
    unsearchable directory via problem() (default: note()), so a caller can keep those even when discovery succeeds.

    Enumeration is explicit os.scandir (not Path.glob, which hides a PermissionError as "no match"): a directory that
    cannot be listed is recorded as "<dir>: <error>" and skipped, and the rest of the search carries on.

    Precedence holds ACROSS bundles: every bundle's manifest entrypoint comes before any bundle's glob match, so one
    bundle's last-resort guess never outranks another bundle's own declaration."""
    problem = problem or note

    def listing(d: Path) -> dict[str, bool] | None:
        """{name: is_dir} for one directory, or None with the reason recorded. Path.glob() swallows PermissionError,
        so a denied directory would look merely empty; scandir raises, and the reason lands in the search list."""
        try:
            with os.scandir(d) as it:
                out = {}
                for e in it:
                    try:
                        out[e.name] = e.is_dir()
                    except OSError:
                        out[e.name] = False
                return out
        except (FileNotFoundError, NotADirectoryError) as exc:   # an absent bundle: searched, not a problem
            note(f"{d}: {type(exc).__name__}")
        except OSError as exc:
            problem(f"{d}: {type(exc).__name__}: {exc.strerror or exc}")
        return None

    entries: list[str] = []
    globbed_all: list[str] = []
    for bundle in bundles:
        res = Path(os.path.expanduser(bundle)) / "Contents" / "Resources"
        patterns = ("codex-package.json", "*/codex-package.json", "codex*/bin/codex")
        # Name every root and pattern searched, so a failure that matched nothing still says where it looked.
        note(f"{res}/{{{','.join(patterns)}}}")
        # Bounded explicit enumeration: Resources itself and one level below, nothing deeper.
        top = listing(res)
        if top is None:
            continue
        manifests = [res / "codex-package.json"] if top.get("codex-package.json") is False else []
        for name in sorted(n for n, is_dir in top.items() if is_dir):
            sub = listing(res / name)
            if sub is None:
                continue
            if sub.get("codex-package.json") is False:
                manifests.append(res / name / "codex-package.json")
            if name.startswith("codex") and sub.get("bin"):
                cand = res / name / "bin" / "codex"
                if os.path.lexists(cand):
                    globbed_all.append(str(cand))
        for manifest in manifests:
            try:
                entry = json.loads(manifest.read_text())
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                problem(f"{manifest} (unreadable: {type(exc).__name__})")
                continue
            target = entry.get("entrypoint") if isinstance(entry, dict) else None
            if not isinstance(target, str) or not target:
                problem(f'{manifest} (no "entrypoint")')
                continue
            entries.append(os.path.normpath(os.path.join(str(manifest.parent), target)))
    return entries + [g for g in globbed_all if g not in entries]


def _codex_binary(env: dict[str, str], bundles=None, searched: list[str] | None = None,
                  notes: list[str] | None = None) -> tuple[str | None, str | None]:
    """The Codex CLI to run, as (binary or None, missing configured path or None), appending everything considered to
    `searched` so a failure can say where it looked, and every discovery problem (a dead configured path, a malformed
    or unreadable manifest, an unsearchable directory) to `notes` too, which outlive a successful pick.

    Precedence: CORTADOS_CODEX_BIN > `codex` on PATH > an app bundle's codex-package.json "entrypoint" > a bounded
    Resources/codex*/bin/codex glob > the legacy CODEX_APP_BINARIES tuple. PATH beats the bundle because an explicitly
    installed CLI is the one the user maintains and upgrades, while the bundled copy moves whenever the app is rebuilt;
    both read the same ~/.codex credentials and config, so the reading is the same either way. A dead configured path
    must not blind the probe: it is reported, not obeyed."""
    import shutil
    seen = searched if searched is not None else []

    def problem(msg: str) -> None:
        seen.append(msg)
        if notes is not None:
            notes.append(msg)
    configured = env.get("CORTADOS_CODEX_BIN")
    if configured:
        if _is_exe(configured):
            return configured, None
        problem(f"CORTADOS_CODEX_BIN={configured} (not an executable file)")
    on_path = shutil.which("codex", path=env.get("PATH"))
    if _is_exe(on_path):
        return on_path, configured
    seen.append("`codex` on PATH")
    cands = _bundle_codex_candidates(CODEX_APP_BUNDLES if bundles is None else bundles, seen.append, problem)
    for cand in cands + [b for b in CODEX_APP_BINARIES if b not in cands]:
        if _is_exe(cand):
            return cand, configured
        seen.append(cand)
    return None, configured


def failure_kind(err: str) -> str:
    """A stable label for a failed reading (no counts, times or diagnostics), so the log line fires once per kind."""
    head, _, rest = err.partition(": ")
    msg = rest.strip() or head        # raise RuntimeError() with no message: label it by exception type, not "unknown"
    if msg.startswith("Codex /status empty"):
        m = re.search(r"stage=(\w+)", msg)
        b = re.search(r"boot_failure=(\w+)", msg)
        return "codex-status-empty:" + (m.group(1) if m else "?") + (f":{b.group(1)}" if b else "")
    # Drop digit runs before slugging: a version or a count in the first clause ("CLI v0.170.0", "3 nodes") would
    # otherwise make a new kind of the same failure and re-log it on every attempt.
    prefix = re.sub(r"\d+", "", msg.split(":", 1)[0].lower())
    return re.sub(r"[^a-z0-9]+", "-", prefix).strip("-")[:60] or "unknown"


def _failed_record(lane: str, old: dict[str, Any] | None, exc: BaseException) -> dict[str, Any]:
    """One lane's failed try: keep the last good reading's values for diagnostics, stamp error + error_kind (which
    makes the lane stale at once in the routing view), and log loudly once per kind rather than every attempt. Shared
    by every sampler path so a failure looks the same, and is as visible, wherever it happened."""
    old = old or {}
    err = f"{type(exc).__name__}: {str(exc)[:200]}"
    # a probe that knows its cause names it (#144: binary-missing, exec-failed, not-logged-in, ...); else the slug
    own = getattr(exc, "kind", None)
    kind = own if isinstance(own, str) and own else failure_kind(err)
    rec = {**old, "tried_at": time.time(), "error": err, "error_kind": kind}
    rec.pop("state", None)            # a "not configured" record is not carried into a failure: the failure is the state now
    rec.pop("note", None)
    # The full list of places looked, beyond the 200-char summary above; dropped when this failure carries none, so a
    # stale list from an earlier failure never describes a different one.
    # The same for the binary a probe ran and the discovery problems met on the way (a malformed manifest that fell
    # back to a glob): a boot or parser failure after discovery must still show how the binary was chosen.
    # A refusal's facts (fixed names and counts, never screen text: tui_capture, #144) say which screen rule fired.
    facts = getattr(exc, "facts", None)
    if isinstance(facts, dict) and facts:
        rec["refusal"] = dict(facts)
    else:
        rec.pop("refusal", None)
    for field in ("searched", "binary", "discovery_notes"):
        value = getattr(exc, field, None)
        if value:
            rec[field] = list(value) if isinstance(value, (list, tuple)) else value
        else:
            rec.pop(field, None)
    if kind != old.get("error_kind"):
        try:
            print(f"cortados scarcity: {lane} reading failed ({kind}): {err}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            # Diagnostics must never block recording the failure: a closed stderr or a broken pipe would otherwise
            # raise out of here, no failed record would be published, and the stale quota would stay routable.
            pass
    return rec


class CodexNotFound(RuntimeError):
    """No Codex CLI anywhere; `searched` carries every place looked, in full, for the persisted record."""

    def __init__(self, searched: list[str]):
        super().__init__("Codex CLI not found: searched " + "; ".join(searched))
        self.searched = list(searched)


MAX_DISCOVERY_NOTES = 10        # a short list: enough to name the broken manifests, never a whole directory listing


def _codex() -> dict[str, Any]:
    env = _cli_env()
    # Resolve and freeze the login BEFORE the sampler starts, and run the sampler on exactly that directory: a CODEX_HOME
    # symlink that moves A -> B mid-probe must not stamp A's usage as B's (Codex review fu1). Metadata only.
    login = codex_login_identity(env)
    if login:
        env = {**env, "CODEX_HOME": login}
    searched: list[str] = []
    notes: list[str] = []
    binary, missing_bin = _codex_binary(env, searched=searched, notes=notes)
    if not binary:
        # Say so plainly instead of forking a child that can only exit 127 and read as an empty boot screen, and name
        # every place looked so the next relocation is a one-line read of the error rather than a debugging session.
        raise CodexNotFound(searched)
    notes = [n[:200] for n in notes[:MAX_DISCOVERY_NOTES]]
    try:
        return _codex_probe(env, binary, missing_bin, notes, login=login)
    except Exception as exc:
        # Discovery succeeded but the probe did not: keep which binary ran and what discovery tripped over, so a
        # fallback past a malformed manifest is visible next to the boot or parser failure it may explain.
        try:
            exc.binary, exc.discovery_notes = binary, notes
        except AttributeError:          # an exception type that refuses attributes still propagates unchanged
            pass
        raise


def _codex_probe(env: dict[str, str], binary: str, missing_bin: str | None, notes: list[str],
                 login: str | None = None) -> dict[str, Any]:
    retries: list[dict] = []
    for attempt in range(2):
        diagnostic: dict = {}
        # The screen prints "Model:" (0.163); the old lowercase pattern never matched, so every probe sat out the full
        # boot wait and retried (67 s a sample on 2026-10-21). The entry's "% left" may wrap onto its own line.
        # The standalone installer may ship the CLI without its daemon package; its sibling code-mode host identifies
        # that layout.  --no-daemon keeps /status available without changing auth or requiring a package install.
        args = [binary]
        if Path(binary).with_name("codex-code-mode-host").is_file():
            args.append("--no-daemon")
        text = tui_capture(args, "/status", r"limit: \[[^\]]*\]\s+\d+%\s+left", env, boot_s=20,
                           ready=r"(?i)model: +(?!loading)\S+", diagnostics=diagnostic)
        if text.strip():
            break
        if (attempt == 0 and diagnostic.get("eof") and diagnostic.get("stage") in {"boot", "trust"}
                and diagnostic.get("boot_failure") not in {"auth", "config", "exec"}):
            # The old PTY has already been reaped. One new CLI can recover a
            # transient startup exit; never retry malformed panels or change auth.
            retries.append(dict(diagnostic))
            continue
        break
    if not text.strip():
        # Metadata only: distinguish startup exit from an unresponsive TUI
        # without persisting boot screens, credentials or prompt contents.
        raise RuntimeError(f"Codex /status empty (attempts={1 + len(retries)}): " + ", ".join(f"{k}={v}" for k, v in diagnostic.items()))
    wins = parse_codex_status(text)       # an unreadable screen raises: the sample is then stale, never fresh-and-wrong
    # `login`: the canonical CODEX_HOME this sampler's CLI was started on, frozen by _codex before the launch (metadata
    # only), so a reading can be proven to belong to the login a consumer executes on (provider/floor.py).
    return {"source": "codex /status (pty)", "windows": wins, "startup_retries": retries, "cli_version": codex_screen.version(text),
            "login": login,
            "binary": binary, "discovery_notes": notes,
            **({"configured_binary_missing": missing_bin} if missing_bin else {})}


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


def _cursor() -> dict[str, Any]:
    env = {**_cli_env(), "AGENT_CLI_CREDENTIAL_STORE": "file"}
    text = tui_capture(["cursor-agent"], "/usage", r"Included \d+% used[\s\S]*Esc to close", env, boot_s=15, ready=r"Cursor Agent")
    wins = parse_cursor_usage(text)
    if not wins:
        raise RuntimeError("no '% used' lines on the /usage screen: " + re.sub(r"\s+", " ", text)[-200:])
    return {"source": "cursor-agent /usage (pty)", "windows": wins}


def _local() -> dict[str, Any]:
    fleet_config = _env_get("CORTADOS_LOCAL_RESOURCES_CONFIG")
    if fleet_config:
        from cortados_router.local_resources import sample as sample_resources
        return sample_resources(fleet_config)
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
    """Which separately-metered pool a model draws from, beyond the lane's general windows. Base-lane aware, so a
    per-account lane (claude#b) maps the same way as its vendor (claude)."""
    m = (model or "").lower()
    lane = (lane or "").split("#", 1)[0]
    if lane == "cursor":
        return "cursor-models" if re.search(r"grok|composer|^auto$|^default$|vega", m) else "other-models"
    if lane == "antigravity":
        return agy_pool(model)
    if lane == "claude" and "fable" in m:
        return "fable"                                   # Forest 2026-10-25: Fable is separately metered; true even when the per-model panel is rate-limited so no pool is observed this read
    hits = [p for p in pools if p not in ("all", "info") and all(tok in m for tok in p.split("-") if tok)]
    return max(hits, key=len) if hits else None


class UnknownAgyModel(ValueError):
    """A model the antigravity inventory does not list: its pool is unknown, and no pool is assumed for it."""


def _agy_inventory() -> dict[str, str]:
    """{lower-cased id or display name: pool} from routing-table.json inventory.antigravity.models, cached by mtime."""
    from cortados_router.paths import tables_dir
    p = tables_dir() / "routing-table.json"
    key = (str(p), p.stat().st_mtime_ns)
    if _AGY_INV.get("key") != key:
        models = (json.loads(p.read_text()).get("inventory") or {}).get("antigravity", {}).get("models") or {}
        idx = {}
        for pool, rows in models.items():
            for r in rows or []:
                for k in (r.get("id"), r.get("name")):
                    if isinstance(k, str) and k.strip():
                        idx[k.strip().lower()] = pool
        _AGY_INV.update(key=key, idx=idx)
    return _AGY_INV["idx"]


_AGY_INV: dict[str, Any] = {}


def agy_pool(model: str) -> str:
    """The antigravity pool of `model` (id or display name, as `agy models` lists them) by the inventory, not by a
    substring (#144: a model whose id lacks "gemini" would have been charged to the wrong pool). Unknown: raises."""
    pool = _agy_inventory().get((model or "").strip().lower())
    if pool is None:
        raise UnknownAgyModel(f"{model!r} is not in inventory.antigravity (tables/routing-table.json)")
    return pool


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


def _anthropic_api() -> dict[str, Any]:
    """The API-credit lane (#114): dollars of promotional grants, read cheaply from the local ledger and the owner's
    grants file (no PTY, no network, no quota spent). Its one window runs from the grant to the point it stops counting
    (credit does not refill), 100 % used = the 95 % stop line; reuses window_view so its use-it-or-lose-it pacing matches
    every other lane."""
    from cortados_router import api_credits
    now = time.time()
    used_pct, reset_at, window_s = api_credits.window_numbers(now)
    st = api_credits.state(now)
    return {"source": "self-metered (api credit grants)", "budget_usd": api_credits.budget_usd(now),
            # #143: the dollars the window's 100 % stands for (the stop line), which core sizes the lane by when it
            # competes as a scored candidate; None when no grant counts.
            "stop_line_usd": (st.get("grant") or {}).get("stop_line_usd"),
            "spent_usd": round(float((st.get("grant") or {}).get("used_usd") or 0.0), 4),
            "lane_on": bool(st.get("on")), "off_reason": st.get("reason"),
            "windows": [window_view("grant", used_pct, reset_at, window_s, now)]}


def _lane_not_configured() -> type:
    from cortados_router.agy_isolation import LaneNotConfigured
    return LaneNotConfigured


SAMPLERS = {"claude": _claude, "antigravity": _antigravity, "codex": _codex, "cursor": _cursor, "local": _local,
            "anthropic-api": _anthropic_api}


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


_write_lock = threading.Lock()


def _write_cache(update: dict[str, Any], stamp: float | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the lanes this run sampled onto whatever is on disk NOW and write atomically. Two samplers can run at once
    (the managed local refresh never waits for the vendor probes), so neither may write back the lanes it read at its
    start. `stamp` sets the top-level fetched_at; None keeps the one on disk."""
    STATE.mkdir(parents=True, exist_ok=True)
    with _write_lock, open(STATE / "scarcity-write.lock", "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            disk = json.loads(CACHE.read_text())
        except Exception:
            disk = {"fetched_at": 0, "lanes": {}}
        out = {**disk, **(extra or {}), "lanes": {**(disk.get("lanes") or {}), **update}}
        if stamp is not None:
            out["fetched_at"] = stamp
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1))
        tmp.replace(CACHE)
    return out


def _learn_unit_costs() -> None:
    """Re-learn unit costs from the history. unitcost resolves its own home (CORTADOS_HOME or ~/.cortados) at load time,
    independent of this module's STATE; a named seam so tests can replace it and never reach a live home."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("cortados_unitcost", str(Path(__file__).with_name("unitcost.py")))
    um = _ilu.module_from_spec(spec); spec.loader.exec_module(um)
    um.learn()


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
        # Expand the one `claude` sampler into one lane per configured account (claude, claude#b, …); every other vendor
        # is unchanged. Each account lane reads its own login's /usage, so another process spending on an account shows
        # up in that account's own reading. A run that wants no Claude lane (only=["codex"]) never reads accounts.json:
        # that file can live outside this run's home (CORTADOS_ROUTER_TABLES), and nothing here needs it.
        wants_claude = not only or any(o == "claude" or o.startswith("claude#") for o in only)
        cl = claude_lanes() if wants_claude else []
        claude_dir = {k: cd for k, cd in cl}
        plan = {k: SAMPLERS["claude"] for k, _ in cl}
        plan.update({name: fn for name, fn in SAMPLERS.items() if name != "claude"})

        def _base(lane: str) -> str:
            return lane.split("#", 1)[0]

        def _interval(lane: str) -> float:
            return MIN_INTERVAL.get(lane) or MIN_INTERVAL.get(_base(lane), 900)

        def _wanted(lane: str) -> bool:
            return not only or lane in only or _base(lane) in only
        due = [l for l in plan if _wanted(l) and (force or now - float((lanes.get(l) or {}).get("tried_at") or 0) >= _interval(l))]
        results: dict[str, Any] = {}

        def run(lane: str) -> None:
            try:
                rec = plan[lane](lanes.get(lane), claude_dir[lane]) if lane in claude_dir else plan[lane]()
                results[lane] = {**(finish(lane, rec) if lane != "local" else rec), "fetched_at": time.time(), "tried_at": time.time(), "error": None, "error_kind": None}
            except _lane_not_configured() as exc:
                # Not a failure and not a reading (#144): no CLI was opened. No windows, no fetched_at (never fresh), so
                # nothing routes on it, and the views say "not configured" instead of "FAILED".
                results[lane] = {"state": "not-configured", "note": str(exc)[:200], "windows": [], "pools": {},
                                 "scarcity": None, "refusing": False, "fetched_at": None, "tried_at": time.time(),
                                 "error": None, "error_kind": None}
            except Exception as exc:      # keep the last good reading, say so
                results[lane] = _failed_record(lane, lanes.get(lane), exc)
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
            v["plan"] = plans.get(lane) or plans.get(_base(lane)) or v.get("plan")   # account lanes inherit the vendor's plan
            lanes[lane] = v
        # The top-level stamp means "last sample that covered the subscription lanes": a partial run (e.g. the 60-second
        # local refresh) keeps the previous one, or current() would never find the probes due again (Codex review 2026-10-21).
        covers_subscriptions = not only or all(l in only for l in SAMPLERS if l != "local")
        out = _write_cache({l: lanes[l] for l in results}, now if covers_subscriptions else None, {"probe_sweep": sweep})
        if results and "--learn" in sys.argv or (results and time.time() - float(prev.get("learned_at") or 0) > 6 * 3600):
            try:
                _learn_unit_costs()
                # Persisted, not only returned: the 6-hour gate reads learned_at from disk, and a stale learn_error on disk
                # would keep reporting a failure that a later learn already recovered from.
                out = _write_cache({}, None, {"learned_at": time.time(), "learn_error": None, "learn_error_at": None})
            except Exception as exc:      # visible, not swallowed: the router falls back to price-only weights while this fails
                msg = f"{type(exc).__name__}: {str(exc)[:160]}"
                logging.getLogger("cortados.scarcity").warning("unit-cost learning failed (%s)", msg)
                out["learn_error"] = msg
                try:
                    _write_cache({}, None, {"learn_error": msg, "learn_error_at": time.time()})
                except Exception as wexc:
                    logging.getLogger("cortados.scarcity").warning("could not record the learn error in the cache (%s)", type(wexc).__name__)
        if results:
            with HISTORY.open("a") as h:
                h.write(json.dumps({"ts": now, "lanes": {l: {"scarcity": v.get("scarcity"), "windows": [[w["window"], w["used_pct"], w["elapsed_pct"]] for w in v.get("windows") or []]}
                                                        for l, v in results.items() if not v.get("error") and not v.get("state")}}) + "\n")
        return out


def current(max_age_s: int = 1200, *, refresh: bool = True) -> dict[str, Any]:
    """Never blocks a turn: returns what is on disk and refreshes in the background when it is getting old. refresh=False
    is a guaranteed cache-only read (the dashboard's GET must open no vendor CLI), independent of CORTADOS_SCARCITY_REFRESH."""
    try:
        d = json.loads(CACHE.read_text())
    except Exception:
        d = {"fetched_at": 0, "lanes": {}}
    if not refresh or _env_get("CORTADOS_SCARCITY_REFRESH") == "off":
        # refresh=False: a guaranteed cache-only read. Also: verification and ad-hoc `core.py decide` runs use a throwaway
        # home, always "due", and the background sample would open the user's real vendor CLIs; the switch routes on disk only.
        return d
    managed_local = bool(_env_get("CORTADOS_LOCAL_RESOURCES_CONFIG"))
    global_due = time.time() - float(d.get("fetched_at") or 0) > max_age_s
    local_due = managed_local and time.time() - float((d.get("lanes", {}).get("local") or {}).get("tried_at") or 0) >= MIN_INTERVAL["local"]
    if global_due and not _refreshing.is_set():
        _refreshing.set()

        def bg() -> None:
            try:
                # Managed local telemetry refreshes on its own (below): inside the full run it would be published only
                # after every vendor probe has finished, up to 240 s, past its own 120-second expiry.
                sample(only=[l for l in SAMPLERS if not (managed_local and l == "local")])
            finally:
                _refreshing.clear()
        threading.Thread(target=bg, daemon=True, name="cortados-scarcity").start()
    if local_due and not _refreshing_local.is_set():
        _refreshing_local.set()

        def bg_local() -> None:
            try:
                _sample_local_managed()
            finally:
                _refreshing_local.clear()
        threading.Thread(target=bg_local, daemon=True, name="cortados-scarcity-local").start()
    return d


_refreshing_local = threading.Event()


def _sample_local_managed() -> dict[str, Any]:
    """The managed local refresh: node telemetry only, no vendor CLI, so it takes neither the sampler lock nor its
    240-second join. It publishes through _write_cache like any other run and never touches the top-level stamp."""
    try:
        rec = {**_local(), "fetched_at": time.time(), "tried_at": time.time(), "error": None}
    except Exception as exc:      # keep the last good reading, say so (lane_scarcity treats a failed try as stale at once)
        try:
            old = (json.loads(CACHE.read_text()).get("lanes") or {}).get("local") or {}
        except Exception:
            old = {}
        rec = _failed_record("local", old, exc)      # same error_kind and once-per-kind log as the locked sampler
    rec["plan"] = _plans().get("local") or rec.get("plan")
    # No history line: the series feeds the unit-cost learner and the 24-hour change readout, both per window, and
    # local telemetry has no windows. A line a minute would only crowd the vendor samples out of the readout.
    return _write_cache({"local": rec}, None)


def reading_freshness(lane: str, v: dict[str, Any], max_reading_age_s: int = 6 * 3600, now: float | None = None) -> tuple[bool, bool]:
    """(fresh, failed) of one lane's cached reading: the routing view's freshness rule (lane_scarcity), shared so every
    reader of a reading (core.headroom_pct_for) trusts it by the same standard routing does."""
    now = time.time() if now is None else now
    if lane == "local" and bool(_env_get("CORTADOS_LOCAL_RESOURCES_CONFIG")):
        # A managed local reading expires with its node telemetry: `expires_at` is the earliest member node's sample
        # plus the configured max_age_s (local_resources.sample), never later than the sampler's own 120-second cap.
        fresh = 0 <= now - float(v.get("fetched_at") or 0) < min(max_reading_age_s, 120) and now < float(v.get("expires_at") or 0)
    else:
        fresh = 0 <= now - float(v.get("fetched_at") or 0) < max_reading_age_s
    # A try that failed after the last good reading (the parser refused the screen, the CLI did not boot) makes the
    # lane stale in the routing view at once; the reading stays on disk for diagnostics (Codex adversarial review
    # 2026-10-21: a refused screen had kept a 15-minute-old reading "fresh" for up to six hours).
    failed = bool(v.get("error")) and float(v.get("tried_at") or 0) > float(v.get("fetched_at") or 0)
    return fresh and not failed, failed


def lane_scarcity(max_reading_age_s: int = 6 * 3600, *, refresh: bool = True) -> dict[str, dict[str, Any]]:
    out = {}
    managed_local = bool(_env_get("CORTADOS_LOCAL_RESOURCES_CONFIG"))
    for lane, v in (current(refresh=refresh).get("lanes") or {}).items():
        fresh, failed = reading_freshness(lane, v, max_reading_age_s)
        general = [w for w in v.get("windows") or [] if w.get("pool", "all") == "all"] or (v.get("windows") or [])
        # Every routing signal goes with freshness: a stale lane's window and expiring credit are as unknown as its
        # scarcity (second adversarial review: a refused sample still won on its old "expiring unused" bonus).
        roll = _roll(general) if fresh else {"tightest": {}, "expiring_pct": 0}
        out[lane] = {"scarcity": v.get("scarcity") if fresh else None, "tightest": roll["tightest"], "expiring_pct": roll["expiring_pct"],
                     "refusing": bool(v.get("refusing")) and fresh, "pools": (v.get("pools") or {}) if fresh else {},
                     "error": v.get("error") if failed else None, "config_dir": v.get("config_dir")}
        if lane == "local" and managed_local:
            usable = fresh and v.get("resource_managed") is True
            out[lane].update(resource_managed=True, deployments=v.get("deployments", {}) if usable else {},
                             scarcity=v.get("scarcity") if usable else 1.0,
                             refusing=bool(v.get("refusing")) if usable else True,
                             why=v.get("why") if usable else "local resource telemetry is missing or stale")
    if managed_local and "local" not in out:
        out["local"] = {"resource_managed": True, "deployments": {}, "scarcity": 1.0,
                        "refusing": True, "why": "local resource telemetry has not been sampled"}
    return out


def change_since(hours: float = 24) -> dict[tuple[str, str], float]:
    """Points of each window used over the last `hours`, from our own sample history (negative = the window reset in between)."""
    # The baseline is chosen per lane: samples are partial (the local refresh writes its own lines every minute, the
    # vendor probes every 15–30), so the row nearest the target rarely holds every lane (Codex review 2026-10-21).
    target, best = time.time() - hours * 3600, {}
    try:
        for line in HISTORY.read_text().split("\n"):   # "\n" only: splitlines() also cuts at U+2028/U+2029/\x85 inside a JSON string
            if not line.strip():
                continue
            row = json.loads(line)
            for lane, v in (row.get("lanes") or {}).items():
                if lane not in best or abs(row["ts"] - target) < abs(best[lane][0] - target):
                    best[lane] = (row["ts"], v)
    except Exception:
        return {}
    return {(lane, w[0]): w[1] for lane, (ts, v) in best.items() if abs(ts - target) <= hours * 3600 * 0.25
            for w in v.get("windows") or []}


BURN_HOURS = 24.0          # the recent burn rate is learned over this many hours of samples …
BURN_MIN_SPAN_H = 6.0      # … from at least this span since the window's last reset (a shorter one is noise)
BURN_TAIL_BYTES = 4 << 20  # only the history's tail is read (the local refresh appends a line a minute)
_burn_cache: dict[str, Any] = {"key": None, "rates": {}}
_burn_lock = threading.Lock()


def recent_burn_rates(hours: float = BURN_HOURS, now: float | None = None) -> dict[tuple[str, str], float]:
    """(lane, window name) -> % of that window used per hour over the last `hours` of our own samples
    (scarcity-history.jsonl), counted only since the window's last reset (used % or elapsed % going down marks one).
    Used by core._expected_loss_pct to project what an underused pool will still use before its reset (#132).
    A series spanning less than BURN_MIN_SPAN_H is left out (the caller falls back to the window's pace). Cached on the
    history's size and mtime; never raises."""
    try:
        st = HISTORY.stat()
    except OSError:
        return {}
    now = time.time() if now is None else now
    key = (st.st_size, st.st_mtime_ns, round(now / 300), hours)   # re-read when the file changes, at most every 5 min otherwise
    with _burn_lock:
        if _burn_cache["key"] == key:
            return dict(_burn_cache["rates"])
    try:
        with HISTORY.open("rb") as f:
            if st.st_size > BURN_TAIL_BYTES:
                f.seek(st.st_size - BURN_TAIL_BYTES)
                f.readline()                                       # drop the partial first line
            lines = f.read().decode("utf-8", "replace").split("\n")   # "\n" only, as every history reader
    except OSError:
        return {}
    since = now - hours * 3600
    series: dict[tuple[str, str], list[tuple[float, float, float | None]]] = {}
    for line in lines:
        try:
            row = json.loads(line)
            ts = float(row["ts"])
        except Exception:
            continue
        if ts < since or ts > now:
            continue
        for lane, v in (row.get("lanes") or {}).items():
            for w in (v or {}).get("windows") or []:
                try:
                    series.setdefault((lane, str(w[0])), []).append(
                        (ts, float(w[1]), float(w[2]) if len(w) > 2 and w[2] is not None else None))
                except (TypeError, ValueError, IndexError):
                    continue
    rates: dict[tuple[str, str], float] = {}
    for k, pts in series.items():
        pts.sort()
        start = 0
        for i in range(1, len(pts)):
            (_, u0, e0), (_, u1, e1) = pts[i - 1], pts[i]
            if u1 < u0 - 0.5 or (e0 is not None and e1 is not None and e1 < e0 - 1.0):
                start = i                                          # the window reset between these two samples
        seg = pts[start:]
        span_h = (seg[-1][0] - seg[0][0]) / 3600 if len(seg) > 1 else 0.0
        if span_h >= BURN_MIN_SPAN_H:
            rates[k] = max(0.0, seg[-1][1] - seg[0][1]) / span_h
    with _burn_lock:
        _burn_cache.update(key=key, rates=rates)
    return dict(rates)


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
    # account lanes (claude#b) share the base pool-size key; the window name is matched without case (#143: the usage
    # screen says "week (Fable)", the key "week (fable)")
    key = next((v for (l, w), v in POOL_KEY.items() if l == lane.split("#", 1)[0] and w.lower() == (window or "").lower()), "")
    uc = unit_costs().get(key)
    return (100 - used_pct) * uc["tokens_per_pct"] if uc and uc.get("tokens_per_pct") else None


def _fmt_tokens(n: float | None) -> str:
    return "?" if n is None else (f"{n / 1e9:.1f} B" if n >= 1e9 else f"{n / 1e6:.0f} M" if n >= 1e6 else f"{n / 1e3:.0f} k")


def summary_lines(d: dict[str, Any]) -> list[str]:
    lines, before = [], change_since(24)
    for lane, v in (d.get("lanes") or {}).items():
        s = v.get("scarcity")
        local_stance = ("UNAVAILABLE: deployment or resource check failed" if v.get("refusing")
                        else "BUSY: memory or GPU utilization is high" if s is not None and s >= 0.7
                        else "available: measured local resources") if lane == "local" and v.get("resource_managed") else None
        lines.append(f"{lane}  [{v.get('plan')}]  scarcity {'?' if s is None else s} → router: {local_stance or stance(s)}" + (f"  (last try failed: {v['error']})" if v.get("error") else ""))
        for w in v.get("windows") or []:
            reset = f"until it resets {w['resets_at']} (in {w['resets_in_h']} h)" if w.get("resets_at") else "(untouched: no reset clock running)"
            tl = tokens_left(lane, w["window"], w["used_pct"])
            lines.append(f"    {w['window']}{' (carried from last read)' if w.get('stale') else ''}: {w['used_pct']}% used at {w['elapsed_pct']}% of the window elapsed → on pace for {w['projected_pct_at_reset']}% at reset; "
                         f"{w['headroom_pct']}% left" + (f" ≈ {_fmt_tokens(tl)} tokens" if tl else "") + f" = {w['allowance_pct_per_h']}%/h {reset}"
                         + (f"  ← about {w['expiring_pct']}% will expire unused" if w.get("expiring_pct", 0) >= 10 else "")
                         + (f"  [24 h: {w['used_pct'] - before[(lane, w['window'])]:+.0f} pts]" if (lane, w["window"]) in before else ""))
        if lane == "local":
            if v.get("resource_managed"):
                lines.append("    GPU memory, host RAM and utilization are separate; no quota reset")
                for node in v.get("nodes", []):
                    memory = node.get("gpu_headroom_estimate_bytes")
                    headroom = "unknown" if memory is None else f"{memory / 2**30:.1f} GiB estimated"
                    utilization = node.get("gpu_utilization_pct")
                    used = "unknown" if utilization is None else f"{utilization}%"
                    lines.append(f"    {node['id']}: {node['status']}, GPU headroom {headroom}, utilization {used}, pool={node['pool_eligible']}")
                if v.get("why"):
                    lines.append("    " + v["why"])
            else:
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
