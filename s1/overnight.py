"""Bound the existing nightly process tree to 01:30–07:00 America/Toronto (default, window mode), or run it continuously.

Continuous mode (Forest 2026-11-03): `overnight.py --continuous -- /bin/bash <repo>/s1/nightly.sh` from a launchd KeepAlive job.
No window, no deadline: one cycle = one nightly.sh run. Only a cycle that exits 0 AND wrote "worked" to $CORTADOS_S1_CYCLE_RESULT
(labels added, training ran, an evaluation/promotion attempted) starts the next cycle at once; any other rc-0 exit is idle and sleeps
CORTADOS_S1_CYCLE_IDLE_S (default 900); a failure (rc 1/2/3/...) backs off exponentially from that interval, capped at 4 h; rc 130 ends. The live scorer stays up (nightly.sh skips
gpu_alone under CORTADOS_S1_CONTINUOUS=1); a guard SIGSTOPs the work process group while
  - scorer latency > baseline + CORTADOS_S1_LATENCY_BUDGET_S (default 1.0 s, Forest 2026-11-03), or, inside the local-time
    night window CORTADOS_S1_NIGHT_WINDOW (default 01:00-07:00, "off" disables), > baseline + CORTADOS_S1_NIGHT_LATENCY_BUDGET_S
    (default 6.0 s, Forest 2026-11-07), or
  - free memory (vm_stat free+inactive+speculative) < CORTADOS_S1_MIN_FREE_GB (default 8),
and SIGCONTs it after RESUME_AFTER consecutive good readings. Latency (#116) = the scorer's own p50/p90 of its recent /score
requests (serve.py GET /latency, i.e. what routed calls waited), topped up by a fixed short probe when it is quiet, read at most
every CORTADOS_S1_PROBE_S (default 30) s; a request or probe past the router's S1 timeout is over every budget. The baseline is
the median of readings taken while the tree is paused or no cycle is running. A pause stops every process group in the cycle's
tree (the studio gate runs its trainer in its own group and records it in data/trainer-group.json), each recorded in
data/latency-guard-stopped.json before its SIGSTOP so a restarted supervisor continues them all (recover_stopped_groups); a
pause whose process discovery failed is incomplete, retried at every reading and never a baseline. Every group is continued
before any stop. Still over budget when CORTADOS_S1_MAX_PAUSE_S ends in a cycle that may train: the cycle is stopped the
ordinary graceful way and training is held (training_resume_at: 1 h when the stop came inside the training window, else the
next window start, or midnight without a valid window; state "stopped_for_day"); labelling-only cycles keep running meanwhile
(CORTADOS_S1_TRAIN_ALLOWED=0) and are paused/released by the normal rules. Every graceful stop also TERMs, then KILLs, the
trainer group the gate recorded, should its gate have crashed (Guard.stop_trainer_group). The guard's state goes to
data/latency-guard.json for the live view. Design: docs/ROUTER-DESIGN.md §4d.1 "The latency guard".

Training only in the night window (Forest 2026-11-08): CORTADOS_S1_TRAIN_ONLY_IN_WINDOW (default "1"; "0" = train any time).
Labeling and the dataset refresh run all day; the Studio-heavy gated steps (LoRA, MLX preflight, Laya, evaluation) run only
inside CORTADOS_S1_NIGHT_WINDOW, so the Studio's local Ollama model is free during the day. Each cycle gets
CORTADOS_S1_TRAIN_ALLOWED=1/0 (+ CORTADOS_S1_TRAIN_SKIP_REASON); nightly.sh skips every gated step when it is 0, ends rc 0, and
the cycle is idle (sleeps CORTADOS_S1_CYCLE_IDLE_S) unless labels were added. An invalid window, or "off", means no training
(fail closed). A cycle allowed to train is stopped when the window ends (group SIGTERM, TRAIN_STOP_GRACE_S, then SIGKILL); the
trainer's checkpoints stay, and the next cycle starts at once (it labels, then skips training until the next window); nightly.sh
carries that run folder across nights and resumes it from its newest checkpoint (cross-night resume).
The window-end stop ends the whole cycle, including any labeling still running; the next cycle redoes it at once.
The window is read once per cycle at its start: a cycle begun just before the window opens does not train; the next one does.

Calibration sweep (#74): while s1/data/sweep-request.json exists, the next in-window cycle runs only the sweep (nightly.sh
"Sweep first"), which makes no labeling calls. So sweep_delay drops a 'quota' cooldown inside the window and caps any sleep
outside it at the next window start: an evening quota stop no longer eats the sweep's night. Each cycle also gets
CORTADOS_S1_TRAIN_UNTIL (the window end, epoch s; empty outside it), which keeps training off after a finished sweep."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import os
from pathlib import Path
import signal
import socket
import subprocess
import json
import math
import re
import statistics
import sys
import time as _time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")


def seconds_left(now: datetime | None = None) -> float:
    now = (now or datetime.now(TORONTO)).astimezone(TORONTO)
    if not time(1, 30) <= now.time().replace(tzinfo=None) < time(7):
        return 0
    end = datetime.combine(now.date(), time(7), TORONTO)
    return max(0, end.timestamp() - now.timestamp())


def signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def group_alive(pgid: int) -> bool:
    """Does process group `pgid` still have a member? EPERM counts as alive (a member we may not signal, or a zombie-only
    group not yet reaped): 'gone' must be certain before a record is cleared."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Grace between any supervisor SIGTERM (window end, deadline, launchd stop/restart) and SIGKILL. Longer than studio_capacity's
# TRAINING_STOP_GRACE_SECONDS (30 s), so the gate can TERM/KILL its own training group and nightly.sh's EXIT trap can run
# before this supervisor escalates to KILL (a shorter grace orphans the trainer with the node lock held).
TRAIN_STOP_GRACE_S = 60.0
# The checkpoint grace the guard gives a recorded trainer group it TERMs itself (stop_trainer_group, #116 owner 2026-11-22):
# the same as the gate gives its own trainer, studio_capacity.TRAINING_STOP_GRACE_SECONDS (a test pins the two together).
TRAINER_STOP_GRACE_S = 30.0
# After SIGKILL, how long to wait for the trainer group to disappear before keeping its record and alerting.
TRAINER_KILL_WAIT_S = 5.0


def run_bounded(command: list[str], seconds: float, *, grace: float = TRAIN_STOP_GRACE_S, pidfile: Path | None = None) -> int:
    if seconds <= 0:
        print("nightly: outside 01:30–07:00 Toronto; nothing started", flush=True)
        return 0
    env = dict(os.environ, CORTADOS_NIGHTLY_SUPERVISED="1", TZ="America/Toronto")
    child = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        try:
            return child.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            print("nightly: 07:00 Toronto deadline; stopping this run and preserving checkpoints", flush=True)
            # Drain the whole group (the shell can exit before its grandchildren, e.g. the studio gate stopping its trainer).
            stop_group_drained(child, grace=grace)
            return 124
    except BaseException:
        # launchd unload/restart (SIGTERM -> KeyboardInterrupt): same drain, or the gate is killed mid-stop and its trainer
        # is orphaned holding the studio lock.
        stop_group_drained(child, grace=grace)
        raise
    finally:
        if pidfile is not None and child.poll() is not None:
            try:
                if pidfile.read_text().strip() == str(child.pid):
                    pidfile.unlink()
            except FileNotFoundError:
                pass


# Night window (Forest 2026-11-07, option a): training alone pushes scorer latency to 4.4-5.5 s, so the +1 s day budget kept the
# trainer frozen almost all the time. At night there is almost no routing traffic, so a slower scorer costs little; the night
# budget is still a hard ceiling so a scorer that has stalled (not merely slowed) still pauses the trainer. Local wall time
# (not a fixed UTC offset) so the window follows DST; the memory guard is the same in both windows (it protects the machine).
DEFAULT_NIGHT_WINDOW = "01:00-07:00"
DEFAULT_NIGHT_BUDGET_S = 6.0


def parse_night_window(text: str) -> tuple[time, time] | None:
    """'HH:MM-HH:MM' -> (start, end) local times; 'off' -> None. start == end is rejected (empty or 24 h is ambiguous).
    A window whose end is before its start crosses midnight (e.g. 23:00-05:00). Raises ValueError on anything else."""
    text = text.strip()
    if text.lower() == "off":
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
    if not m:
        raise ValueError(f"night window {text!r} is not HH:MM-HH:MM or 'off'")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        raise ValueError(f"night window {text!r} has an hour outside 0-23 or a minute outside 0-59")
    start, end = time(h1, m1), time(h2, m2)
    if start == end:
        raise ValueError(f"night window {text!r} starts and ends at the same time")
    return start, end


# The zone the night window is read in. A named zone (not datetime.now().astimezone(), which is a FIXED offset) so window starts,
# ends and deadlines follow DST: with a fixed offset a 01:30 EST start on 2027-03-14 got an 08:00 EDT deadline, and a fall-back
# night stopped an hour early. The Studio is in Toronto. Tests inject another zone through the `zone` keyword arguments.
NIGHT_ZONE = TORONTO


def _wall(now: datetime, zone) -> datetime:
    """`now` as local wall time in `zone`: aware datetimes are converted; naive ones are already local wall time."""
    return now.astimezone(zone) if now.tzinfo is not None else now


def _at(day, t: time, zone, aware: bool) -> datetime:
    """Local wall time `t` on `day` in `zone`, aware (absolute instant) when the caller works in aware datetimes."""
    dt = datetime.combine(day, t)
    return dt.replace(tzinfo=zone) if aware else dt


def in_night_window(window: tuple[time, time] | None, now: datetime, *, zone=None) -> bool:
    """True when the local wall time of `now` (in `zone`, default NIGHT_ZONE) is in [start, end); handles windows crossing
    midnight."""
    if window is None:
        return False
    start, end = window
    t = _wall(now, zone or NIGHT_ZONE).time().replace(tzinfo=None)
    return start <= t < end if start < end else (t >= start or t < end)


def _check_night_window(window) -> tuple[time, time] | None:
    """Validate an injected window with the same rules parse_night_window applies to text. Without this, equal endpoints
    (time(1), time(1)) fall through in_night_window's midnight branch as all-day and silently apply the night budget in
    daytime. Raises ValueError for anything but None or a (start, end) pair of distinct naive datetime.time values."""
    if window is None:
        return None
    if not isinstance(window, tuple) or len(window) != 2:
        raise ValueError(f"night window {window!r} is not None or a (start, end) tuple")
    start, end = window
    if not (isinstance(start, time) and isinstance(end, time)):
        raise ValueError(f"night window {window!r} endpoints must be datetime.time values")
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ValueError(f"night window {window!r} endpoints must be naive local times")
    if start == end:
        raise ValueError(f"night window {window!r} starts and ends at the same time")
    return start, end


def _env_night_window() -> tuple[time, time] | None:
    """CORTADOS_S1_NIGHT_WINDOW, reported and replaced by the default when invalid (the _env_float convention)."""
    raw = os.environ.get("CORTADOS_S1_NIGHT_WINDOW")
    if raw is None or raw.strip() == "":
        return parse_night_window(DEFAULT_NIGHT_WINDOW)
    try:
        return parse_night_window(raw)
    except ValueError as e:
        print(f"overnight: CORTADOS_S1_NIGHT_WINDOW: {e}; using {DEFAULT_NIGHT_WINDOW}", flush=True)
        return parse_night_window(DEFAULT_NIGHT_WINDOW)


def _next_start(window: tuple[time, time], now: datetime, *, zone=None) -> datetime:
    """The next time `window` starts (today or tomorrow, local wall time in `zone`). Aware `now` -> aware result in `zone`."""
    zone = zone or NIGHT_ZONE
    local = _wall(now, zone)
    day = local.date() if local.time().replace(tzinfo=None) < window[0] else local.date() + timedelta(days=1)
    return _at(day, window[0], zone, now.tzinfo is not None)


def _window_end(window: tuple[time, time], now: datetime, *, zone=None) -> datetime:
    """The end of the window `now` is inside (assumes in_night_window(window, now)): the end's local wall time in `zone` on the
    right date (tomorrow when the window crosses midnight and `now` is before it), built on that date so it carries that date's
    UTC offset. Aware `now` -> aware result, so the caller compares absolute instants."""
    zone = zone or NIGHT_ZONE
    local = _wall(now, zone)
    day = local.date() if local.time().replace(tzinfo=None) < window[1] else local.date() + timedelta(days=1)
    return _at(day, window[1], zone, now.tzinfo is not None)


def train_policy(now: datetime, *, window_raw: str | None = None, switch_raw: str | None = None, zone=None
                 ) -> tuple[bool, str, datetime | None]:
    """(training allowed, reason, deadline) for a cycle starting at local wall time `now`. Reuses parse_night_window /
    in_night_window (one window implementation). Unlike the latency budget, an invalid window FAILS CLOSED here: training
    all day on a typo is exactly what the switch exists to prevent. deadline is the window end (None = no deadline)."""
    switch = (os.environ.get("CORTADOS_S1_TRAIN_ONLY_IN_WINDOW", "1") if switch_raw is None else switch_raw).strip()
    if switch == "0":
        return True, "CORTADOS_S1_TRAIN_ONLY_IN_WINDOW=0: training allowed any time", None
    if switch != "1":
        print(f"overnight: CORTADOS_S1_TRAIN_ONLY_IN_WINDOW={switch!r} is not 0 or 1; using 1 (train only in the window)",
              flush=True)
    raw = os.environ.get("CORTADOS_S1_NIGHT_WINDOW", "") if window_raw is None else window_raw
    raw = raw.strip() or DEFAULT_NIGHT_WINDOW
    try:
        window = parse_night_window(raw)
    except ValueError as e:
        return False, f"training skipped: CORTADOS_S1_NIGHT_WINDOW invalid ({e}); no training until it is fixed", None
    if window is None:
        return False, ("training skipped: CORTADOS_S1_NIGHT_WINDOW is off and CORTADOS_S1_TRAIN_ONLY_IN_WINDOW=1; "
                       "set CORTADOS_S1_TRAIN_ONLY_IN_WINDOW=0 to train any time"), None
    s, e = window
    if in_night_window(window, now, zone=zone):
        return True, f"inside night window {s:%H:%M}-{e:%H:%M}: training allowed", _window_end(window, now, zone=zone)
    return False, (f"training skipped: outside night window {s:%H:%M}-{e:%H:%M}; "
                   f"next window starts {_next_start(window, now, zone=zone).strftime('%Y-%m-%d %H:%M %Z').strip()}"), None


# The rule slugs training_resume_at answers with; the state file's `until_rule` and the live view accept only these.
DAY_STOP_RULES = ("window_start", "retry_1h", "midnight")
# A stop INSIDE the training window holds training this long, not until the next night (owner 2026-11-22).
DAY_STOP_RETRY_IN_WINDOW_S = 3600.0


def training_resume_at(now: datetime, *, window_raw: str | None = None, zone=None) -> tuple[datetime, str, str]:
    """(when training may run again, the rule's slug (DAY_STOP_RULES), the rule in words) after the guard stopped it for the
    day (#116, owner 2026-11-18: still over budget when the longest pause ends -> no more training today; scoring speed
    wins over daytime training). Rules (owner 2026-11-22):
      - inside the training window CORTADOS_S1_NIGHT_WINDOW (default 01:00-07:00 America/Toronto): one hour from `now`
        ("retry_1h"). Reason: the window is the time set aside for training, and its routing traffic is light, so the
        contention that held the trainer (a burst of routed calls, a one-off job on the Studio) often clears within the
        night; waiting for the next night would throw away the rest of this one. Cycles may start again after the hour,
        and the guard still judges them (a second stop inside the window waits another hour).
      - outside it: the next START of the window ("window_start").
      - with the window off or invalid there is no training window, so the stop lasts the rest of the local day: the next
        local midnight ("midnight").
    Only training is held: labelling-only cycles keep running during the stop (run_cycle). Aware `now` -> aware result in
    `zone` (the hour is added on absolute time, so a DST change inside it does not stretch or shrink it)."""
    zone = zone or NIGHT_ZONE
    raw = os.environ.get("CORTADOS_S1_NIGHT_WINDOW", "") if window_raw is None else window_raw
    try:
        window = parse_night_window(raw.strip() or DEFAULT_NIGHT_WINDOW)
    except ValueError:
        window = None
    if window is not None:
        s, e = window
        if in_night_window(window, now, zone=zone):
            if now.tzinfo is not None:
                until = datetime.fromtimestamp(now.timestamp() + DAY_STOP_RETRY_IN_WINDOW_S, zone)
            else:
                until = now + timedelta(seconds=DAY_STOP_RETRY_IN_WINDOW_S)
            return until, "retry_1h", f"1 hour: the stop came inside the training window ({s:%H:%M}-{e:%H:%M} local)"
        return (_next_start(window, now, zone=zone), "window_start",
                f"next training window start ({s:%H:%M}-{e:%H:%M} local)")
    local = _wall(now, zone)
    return (_at(local.date() + timedelta(days=1), time(0), zone, now.tzinfo is not None), "midnight",
            "next local midnight (no valid training window)")


def _local_now() -> datetime:
    return datetime.now(NIGHT_ZONE)   # aware, named zone; compare deadlines with deadline_reached (UTC), not >= (wall time)


def _env_float(name: str, default: float, *, minimum: float | None = None, above: float | None = None) -> float:
    """A float guard setting from the environment. Non-numbers, nan/inf and values below `minimum` (or not above `above`)
    are reported and replaced by the documented default: no silent fallback, and no setting that disables a guard by accident
    (0 s idle = a hot relaunch loop, nan budget = every comparison false = never pause)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"overnight: {name}={raw!r} is not a number; using {default}", flush=True)
        return default
    if not math.isfinite(value):
        print(f"overnight: {name}={raw!r} is not finite; using {default}", flush=True)
        return default
    if minimum is not None and value < minimum:
        print(f"overnight: {name}={raw!r} is below the minimum {minimum:g}; using {default}", flush=True)
        return default
    if above is not None and value <= above:
        print(f"overnight: {name}={raw!r} must be > {above:g}; using {default}", flush=True)
        return default
    return value


S1_DIR = Path(__file__).resolve().parent
REPO = S1_DIR.parent


def status_path() -> Path:
    """The status.json nightly.sh and the dashboard use (nightly.sh: STATUS=${CORTADOS_S1_STATUS:-data/status.json}, run in s1/)."""
    raw = os.environ.get("CORTADOS_S1_STATUS")
    if not raw:
        return S1_DIR / "data/status.json"
    p = Path(raw)
    return p if p.is_absolute() else S1_DIR / p


def alert(name: str, detail: str, *, status: Path | None = None, log=lambda m: print(m, flush=True)) -> str:
    """Print an ALERT line and keep it in status["alerts"][name], the same place nightly_state.py's streak alerts live,
    so the dashboard shows supervisor alerts next to the nightly ones. A failed record is logged, never swallowed."""
    line = f"ALERT: {name} {detail}"
    log(line)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("nightly_state_for_overnight", S1_DIR / "nightly_state.py")
        ns = importlib.util.module_from_spec(spec); spec.loader.exec_module(ns)
        path = status or status_path()
        st = ns.load_status(path)
        st.setdefault("alerts", {})[name] = f"{line} ({ns.now()})"
        ns.save_status(path, st)
    except (OSError, ValueError) as e:
        log(f"overnight: WARNING: recording alert {name} failed ({e}); its alert state is unknown")
    return line


class ScorerRefused(Exception):
    """The scorer answered 5xx: it is up but overloaded or failing. Counts as a latency violation (back-pressure)."""


class ScorerConfigError(Exception):
    """401/403/404 (or an unresolvable URL): the probe is misconfigured. Not back-pressure; logged once per cycle and alerted."""


def scorer_url_source() -> str:
    """Which setting chose the scorer URL, for alerts and logs. The URL itself is never written there: it names a host on the
    tailnet (e.g. the Studio's 100.x address), and status.json alerts are shown on the dashboard and copied into handbacks."""
    if os.environ.get("CORTADOS_S1_URL"):
        return "CORTADOS_S1_URL"
    if os.environ.get("CORTADOS_S1_ENDPOINT"):
        return "CORTADOS_S1_ENDPOINT"
    return "the default endpoint (s1-endpoints.json default, else loopback)"


# A command that prints the resolver's full message (value included) on the operator's own terminal. Alerts and logs carry
# only a value-free classification, because the value names a tailnet host and alerts reach the dashboard and handbacks.
RESOLVE_HINT = ('run `python3 -c "from cortados_router import s1_endpoint; print(s1_endpoint.resolve_url())"` '
                "from the repo root for the detail")

# Value-free classes of the resolver's ValueError messages (cortados_router/s1_endpoint.py), matched on its fixed wording.
_RESOLVE_CLASSES = (
    ("is not an http(s) URL", "the value is not an http(s) URL"),
    ("does not exist", "the endpoints file named by CORTADOS_S1_ENDPOINTS does not exist"),
    ("is not readable JSON", "the endpoints file is not readable JSON"),
    ("must be an object", "the endpoints file does not hold an 'endpoints' object"),
    ("non-string 'default'", "the endpoints file has a non-string 'default'"),
    ("is not in", "the endpoint name is not in the endpoints file"),
)


def _resolve_class(message: str) -> str:
    for needle, label in _RESOLVE_CLASSES:
        if needle in message:
            return label
    return "the resolver rejected it"


def scorer_url() -> str:
    """The /score URL through the router's resolver (CORTADOS_S1_URL -> endpoint name -> endpoints default -> loopback)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from cortados_router import s1_endpoint
    try:
        return s1_endpoint.resolve_url().rstrip("/") + "/score"
    except ValueError as e:   # the resolver's message repr's the value, scheme-less hosts included: forward only its class
        raise ScorerConfigError(f"scorer URL from {scorer_url_source()} does not resolve ({_resolve_class(str(e))}); "
                                f"{RESOLVE_HINT}") from None


def _probe_failure(e: BaseException) -> str:
    """Type name (and errno) of a probe failure. Never the message: OSError/SSL texts can carry a bare host
    ("hostname 'studio.<tailnet>.ts.net' doesn't match"), which no URL pattern catches."""
    inner = getattr(e, "reason", None) if isinstance(e, urllib.error.URLError) else None
    base = inner if isinstance(inner, BaseException) else e
    errno = getattr(base, "errno", None)
    return type(base).__name__ + (f" errno {errno}" if errno is not None else "")


class ScorerTimedOut(Exception):
    """The scorer did not answer within the router's own S1 timeout: a routed call would have been degraded. A latency
    violation (#116), never "unknown": before #116 a timed-out probe read as no reading, so the guard never paused the
    trainer while 63% of routed decisions timed out."""


def router_s1_timeout() -> float:
    """The router's S1 timeout (core.S1_TIMEOUT_S: CORTADOS_S1_TIMEOUT_S, default 15 s). A scorer slower than this degrades
    routed calls, so it is also the probe's timeout: the guard waits exactly as long as a routed call does."""
    return _env_float("CORTADOS_S1_TIMEOUT_S", 15.0, above=0)


def _is_timeout(e: BaseException) -> bool:
    """A socket timeout, raised directly (read) or wrapped in URLError (connect). socket.timeout is TimeoutError from 3.10."""
    inner = getattr(e, "reason", None) if isinstance(e, urllib.error.URLError) else None
    base = inner if isinstance(inner, BaseException) else e
    return isinstance(base, (socket.timeout, TimeoutError))


def probe_scorer_latency(url: str | None = None, timeout: float | None = None) -> float | None:
    """Wall seconds for one fixed short /score request; None when the scorer cannot be reached (logged).
    Raises ScorerTimedOut past `timeout` (default: the router's S1 timeout), ScorerRefused on 5xx and ScorerConfigError on
    401/403/404 and other 4xx."""
    url = url or scorer_url()
    timeout = router_s1_timeout() if timeout is None else timeout
    body = json.dumps({"request": "list the files in this directory", "unit_kind": "background"}).encode()
    # X-Request-Timeout-S: the scorer drops the probe when we have given up (as for a routed call); X-CortadOS-Probe marks
    # it in the scorer's own latency window.
    headers = {"Content-Type": "application/json", "X-Request-Timeout-S": f"{timeout:g}", "X-CortadOS-Probe": "1"}
    token = os.environ.get("CORTADOS_S1_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    t0 = _time.monotonic()
    try:
        # Built inside the try: Request() raises ValueError for what urlsplit rejects (e.g. 'http://[2001:db8::9:8431').
        req = urllib.request.Request(url, data=body, headers=headers)
        # hostname, not req.host: for 'http://:8431/score' req.host is ':8431' (truthy), yet there is no host to reach.
        # 'http://', 'http:///score' and 'http://:8431' pass _normalize's scheme check; urlopen would call them an outage.
        if not urllib.parse.urlsplit(url).hostname:
            raise ScorerConfigError(f"scorer URL from {scorer_url_source()} has no host; {RESOLVE_HINT}") from None
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:   # before OSError: HTTPError is an OSError subclass
        if e.code >= 500:
            raise ScorerRefused(f"scorer refused (HTTP {e.code})") from None
        raise ScorerConfigError(f"scorer probe got HTTP {e.code} from the URL set by {scorer_url_source()}: "
                                "check CORTADOS_S1_URL/ENDPOINT/TOKEN") from None
    except http.client.InvalidURL as e:   # 'http://host:abc': a setting, not an outage (before HTTPException, its base)
        raise ScorerConfigError(f"scorer URL from {scorer_url_source()} is malformed ({type(e).__name__}); "
                                f"{RESOLVE_HINT}") from None
    # Before ValueError: ssl.SSLCertVerificationError is both an OSError and a ValueError, and a bad certificate is a
    # transient/remote problem, not a malformed setting.
    except (OSError, http.client.HTTPException) as e:   # reset, RemoteDisconnected, IncompleteRead: an outage, not a setting
        if _is_timeout(e):   # slower than a routed call waits: back-pressure, not an outage
            raise ScorerTimedOut(f"scorer probe timed out after {timeout:g}s (the router's S1 timeout)") from None
        print(f"overnight: scorer probe failed ({_probe_failure(e)}); latency unknown this probe", flush=True)
        return None
    except ValueError as e:   # Request()/urlsplit rejected it, e.g. a missing IPv6 bracket; the message can quote the value
        raise ScorerConfigError(f"scorer URL from {scorer_url_source()} is malformed ({type(e).__name__}); "
                                f"{RESOLVE_HINT}") from None
    return _time.monotonic() - t0


class Reading(NamedTuple):
    """One latency reading: p50/p90 seconds of the /score samples it covers (inf = a sample past the router's timeout or
    refused busy), how many samples, how many of them timed out, and where they came from ("scorer" = the scorer's own
    timing of routed calls, GET /latency; "probe" = the guard's own probe only)."""
    p50: float
    p90: float
    n: int
    timeouts: int
    source: str


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) of a non-empty list; inf values sort last, so a timeout lifts the high end."""
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, math.ceil(q / 100.0 * len(xs)) - 1))
    return xs[k]


def reading_of(values: list[float], source: str) -> Reading | None:
    if not values:
        return None
    return Reading(percentile(values, 50), percentile(values, 90), len(values), sum(1 for v in values if math.isinf(v)),
                   source)


def _fmt_s(x: float) -> str:
    return "timed out" if math.isinf(x) else f"{x:.2f}s"


def _fetch_latency(url: str, timeout: float = 5.0) -> dict | None:
    """GET <scorer>/latency as JSON; None when the scorer predates it (#116: an older serve.py answers every GET with its
    health, which has no samples) or cannot be reached. /latency is answered by the HTTP thread, never queued behind MLX."""
    headers = {}
    token = os.environ.get("CORTADOS_S1_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            body = json.loads(r.read(1 << 20))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return body if isinstance(body, dict) and isinstance(body.get("samples"), list) else None


class ScorerLatency:
    """The guard's default latency source (#116): the scorer's OWN timing of its recent /score requests (serve.py
    GET /latency), i.e. what routed calls actually waited, queueing behind training included. When fewer than
    `min_samples` requests started since `since` within the last `window_s` (a quiet scorer), one probe tops the reading up;
    a probe past the router's timeout or refused is an inf sample. A scorer without /latency falls back to the probe alone.
    Samples are placed on the guard's clock by their age, so no clock is shared with the scorer."""

    def __init__(self, *, url: str | None = None, fetch=_fetch_latency, probe=None, clock=_time.monotonic,
                 window_s: float | None = None, min_samples: int = 3):
        self.url, self.fetch, self.clock, self.min_samples = url, fetch, clock, min_samples
        self.probe = probe or (lambda: probe_scorer_latency(self.url))
        self.window = _env_float("CORTADOS_S1_GUARD_WINDOW_S", 180.0, minimum=30) if window_s is None else window_s

    def _scorer_samples(self, now: float, since: float | None) -> list[float] | None:
        url = (self.url or scorer_url())
        body = self.fetch(url[:-len("/score")] + "/latency" if url.endswith("/score") else url.rstrip("/") + "/latency")
        if body is None:
            return None
        out = []
        for row in body["samples"]:
            try:
                age, sec, outcome = float(row["age_s"]), float(row["s"]), row.get("outcome")
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(age) and math.isfinite(sec)) or age < 0 or sec < 0 or age > self.window:
                continue
            start = now - age - sec
            if since is not None and start < since:   # began before the state we are judging (pause, resume, cycle start)
                continue
            out.append(math.inf if outcome in ("timeout", "busy") else sec)
        return out

    def read(self, since: float | None = None) -> Reading | None:
        now = self.clock()
        samples = self._scorer_samples(now, since)
        source = "scorer" if samples is not None else "probe"
        samples = samples or []
        if len(samples) < self.min_samples:
            try:
                lat = self.probe()
            except (ScorerTimedOut, ScorerRefused):
                lat = math.inf
            if lat is not None:
                samples.append(lat)
        return reading_of(samples, source)


def parse_vm_stat(out: str) -> float | None:
    """free+inactive+speculative pages in GB from `vm_stat` text; None when the text lacks the page size or any of the three."""
    m = re.search(r"page size of (\d+) bytes", out)
    pages = {k: int(v) for k, v in re.findall(r"^Pages (free|inactive|speculative):\s+(\d+)\.", out, re.M)}
    if not m or len(pages) != 3:
        return None
    return sum(pages.values()) * int(m.group(1)) / 1e9


def free_memory_gb() -> float | None:
    """free+inactive+speculative pages from vm_stat, in GB; None (logged) when vm_stat is unavailable or unparseable."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"overnight: vm_stat failed ({e}); memory guard has no reading", flush=True)
        return None
    gb = parse_vm_stat(out)
    if gb is None:
        print("overnight: vm_stat output not understood; memory guard has no reading", flush=True)
    return gb


# ps runs in the C locale: its lstart column ("Sun Nov 22 14:57:48 2026") is the identity token the studio gate writes too
# (studio_capacity._proc_start), and a localized date would never compare equal across the two writers.
_PS_ENV = {"LC_ALL": "C", "LANG": "C"}


def _ps_tree() -> str:
    return subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,lstart="], capture_output=True, text=True, check=True,
                          timeout=10, env=dict(os.environ, **_PS_ENV)).stdout


def _norm_start(text: str | None) -> str | None:
    """One spelling of an lstart value (ps pads single-digit days with a second space in some columns)."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text or None


def proc_start(pid: int) -> str | None:
    """The start time (`ps -o lstart=`) of process `pid`, or None when there is no such process. A ps that cannot run (or
    times out) raises: 'unknown' must never read as 'gone', or a reused pid would be signalled as ours."""
    p = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10,
                       env=dict(os.environ, **_PS_ENV))
    start = _norm_start(p.stdout)
    if start:
        return start
    if p.returncode in (0, 1):   # BSD/macOS ps: exit 1 and no row for a pid that does not exist
        return None
    raise subprocess.CalledProcessError(p.returncode, "ps")


def safe_group(pgid) -> bool:
    """A process group the guard may ever signal: a real group id (> 1: 0 is 'my own group' to killpg, 1 is launchd) and
    never this supervisor's own group, which would stop the guard itself and leave everything stopped for good."""
    return isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1 and pgid != os.getpgrp()


def group_is_ours(rec: dict, *, leader_start=None) -> bool | None:
    """Is the group in record {"pgid", "start"} (start = its leader's lstart when recorded) still the one recorded?
    True: the leader is alive with the recorded start time, or the leader is gone. A pid is never reused while a process
    group with that id still exists (POSIX fork: a new pid matches no active process group id), so with the leader gone the
    group is either still ours or gone entirely (killpg then answers ESRCH, harmless). False: a live leader with another
    start time (the pgid was reused by an unrelated process). None: undecidable (ps failed, or no start was recorded while
    a leader is alive): the caller must not signal it on a guess."""
    leader_start = leader_start or proc_start
    try:
        now = leader_start(rec["pgid"])
    except (OSError, subprocess.SubprocessError):
        return None
    if now is None:
        return True
    want = _norm_start(rec.get("start"))
    if want is None:
        return None
    return _norm_start(now) == want


class Discovery(NamedTuple):
    """The cycle tree's process groups. complete=False when ps failed: then `groups` is only what is known (the root), and
    a pause built on it may have left the trainer running (#116 review: such a pause was reported as complete and its
    still-contended readings became the baseline). starts: each group leader's lstart, where the leader was listed."""
    groups: list[int]
    complete: bool
    starts: dict[int, str]


def discover_groups(root: int, *, ps=_ps_tree, log=lambda m: print(m, flush=True)) -> Discovery:
    """Every process group in the cycle's tree: the cycle's own group (`root`, its leader) first, then the group of each
    descendant that leads another one. The studio gate runs each gated step (LoRA, evaluation, the sweep) with
    start_new_session=True (studio_capacity.run_holding_lock), so a SIGSTOP to the cycle's group alone froze the gate wrapper
    and left the trainer running (#116). Never this supervisor's own group, nor 0/1. A ps failure answers [root] with
    complete=False (logged), so the caller retries instead of trusting a partial pause."""
    try:
        out = ps()
    except (OSError, subprocess.SubprocessError) as e:
        log(f"overnight: ps failed ({type(e).__name__}); the cycle's process groups are not all known this time")
        return Discovery([root], False, {})
    kids: dict[int, list[tuple[int, int]]] = {}
    starts: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or not all(x.lstrip("-").isdigit() for x in parts[:3]):
            continue
        pid, ppid, pgid = (int(x) for x in parts[:3])
        kids.setdefault(ppid, []).append((pid, pgid))
        start = _norm_start(" ".join(parts[3:]))
        if start:
            starts[pid] = start
    groups, seen, todo = [root], {root}, [root]
    while todo:
        for pid, pgid in kids.get(todo.pop(), []):
            if pid in seen:
                continue
            seen.add(pid)
            todo.append(pid)
            if pgid not in groups and safe_group(pgid):
                groups.append(pgid)
    return Discovery(groups, True, {g: starts[g] for g in groups if g in starts})


def work_groups(root: int, *, ps=_ps_tree, log=lambda m: print(m, flush=True)) -> list[int]:
    """discover_groups' group list alone (a ps failure answers [root])."""
    return discover_groups(root, ps=ps, log=log).groups


def _as_discovery(found) -> Discovery:
    """An injected `groups` callable may answer a plain list (taken as complete) or a Discovery."""
    if isinstance(found, Discovery):
        return found
    return Discovery(list(found), True, {})


# The file the studio gate writes its trainer's process group to (#116, owner 2026-11-18): {"pgid", "start", "gate_pid"}.
# run_cycle passes its path to nightly.sh (and so to every gate) as CORTADOS_S1_TRAINER_GROUP_FILE.
TRAINER_GROUP_NAME = "trainer-group.json"
# The groups a pause stopped, written BEFORE each SIGSTOP (#116 review): a supervisor killed mid-pause loses its memory, and
# a restarted one continues every group listed here that still matches (group_is_ours) before resuming the cycle's root.
STOPPED_GROUPS_NAME = "latency-guard-stopped.json"
# Small files: a record bigger than this is not ours.
GROUP_FILE_MAX_BYTES = 65536


def read_group_record(path: Path) -> tuple[dict | None, bool]:
    """(the trainer-group record or None, readable). Absent: (None, True). Unreadable, oversized or malformed: (None, False),
    so the caller knows a trainer may exist that it cannot see."""
    try:
        if path.stat().st_size > GROUP_FILE_MAX_BYTES:
            return None, False
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None, True
    except (OSError, ValueError, RecursionError):
        return None, False
    if not isinstance(raw, dict) or not isinstance(raw.get("pgid"), int) or isinstance(raw.get("pgid"), bool):
        return None, False
    return {"pgid": raw["pgid"], "start": _norm_start(raw.get("start"))}, True


def read_stopped_groups(path: Path) -> tuple[list[dict], bool]:
    """(the stopped-groups records, readable). Absent: ([], True)."""
    try:
        if path.stat().st_size > GROUP_FILE_MAX_BYTES:
            return [], False
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return [], True
    except (OSError, ValueError, RecursionError):
        return [], False
    recs = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(recs, list):
        return [], False
    out = [{"pgid": r["pgid"], "start": _norm_start(r.get("start"))} for r in recs
           if isinstance(r, dict) and isinstance(r.get("pgid"), int) and not isinstance(r.get("pgid"), bool)]
    return out, len(out) == len(recs)


def _write_json_durable(path: Path, obj) -> None:
    """tmp + fsync + rename + directory fsync: a record written before a SIGSTOP must survive the supervisor's death."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass   # some filesystems refuse a directory fsync; the rename itself is still atomic


# The guard's state file, in s1/data next to the pipeline's flags; live_heartbeat.s1_block reads it by this name.
GUARD_STATE_NAME = "latency-guard.json"

# Reason slugs of the guard's state file (data/latency-guard.json), the only reasons the live view shows (#116).
GUARD_REASONS = ("latency", "timeout", "refused", "memory", "no_baseline", "forced_release")


class Guard:
    """Pauses/resumes the cycle's process groups on scorer latency and free memory. Sources, clock and signaller are
    injectable. Latency (#116): the default source is ScorerLatency (the scorer's own p50/p90 of routed /score calls, a
    probe when it is quiet); a plain callable returning seconds is one sample (p50 = p90). Over budget when the reading's
    p50 is above the baseline p50 + budget OR its p90 above the baseline p90 + budget; a sample past the router's S1 timeout
    is over every budget. The baseline is the median of readings taken with the tree stopped or no cycle running."""

    # Hysteresis: resume only after this many consecutive in-budget probes, so one lucky fast probe while the GPU is still
    # contended does not flap the trainer between STOP and CONT every 30 s.
    RESUME_AFTER = 3
    BASELINE_SAMPLES = 5
    # While paused, this many consecutive unreadable probes (scorer down/unreachable/misconfigured) release the tree: a probe
    # that cannot read anything is no evidence of contention, and holding the trainer on it would stall the cycle forever.
    MAX_UNREADABLE = 10

    def __init__(self, *, latency=None, memory=free_memory_gb, clock=_time.monotonic, signaller=None,
                 budget_s: float | None = None, min_free_gb: float | None = None, interval_s: float | None = None,
                 max_pause_s: float | None = None, release_hold_s: float | None = None, log=lambda m: print(m, flush=True),
                 alerter=None, wall=_local_now, night_window="env", night_budget_s: float | None = None,
                 groups=None, state_path: Path | None = None, epoch=_time.time, stopped_path: Path | None = None,
                 trainer_file: Path | None = None, leader_start=None, live_members=None):
        if latency is None:
            latency = ScorerLatency(clock=clock)
        self.latency, self.memory, self.clock, self.log = latency, memory, clock, log
        # An injected signaller without injected groups signals the cycle group alone (tests use fake pids, which must
        # never be walked through the real ps).
        self.groups = groups or (discover_groups if signaller is None else (lambda pgid: [pgid]))
        self.signaller = signaller or signal_group
        self.state_path, self.epoch = state_path, epoch
        # #116 review round 1: where the stopped groups are recorded before each SIGSTOP (None: not recorded, as in tests),
        # and the studio gate's trainer-group file (None: none read). leader_start checks a recorded group's identity.
        self.stopped_path, self.trainer_file = stopped_path, trainer_file
        # Same rule for the identity check and the EPERM check (#116 review round 2): an injected signaller means fake pids,
        # which must never reach the real ps. Uninjected there, a leader reads as gone (the group, if any, is ours) and an
        # EPERM group as having live members (never 'drained' on a guess).
        if leader_start is None:
            leader_start = proc_start if signaller is None else (lambda pid: None)
        if live_members is None:
            live_members = _live_group_members if signaller is None else (lambda pgid: [pgid])
        self.leader_start, self.live_members = leader_start, live_members
        self.group_alive = group_alive   # 'does this group still exist' for stop_trainer_group (tests replace it)
        # Whether the running cycle may train (run_cycle sets it from train_policy and the day stop). A labelling-only cycle
        # is never stopped for the day: there is no training in it to stop (owner 2026-11-22, item 2).
        self.cycle_trains = True
        self.stopped_groups: list[int] = []   # the groups the last pause stopped (each gets SIGCONT on resume or stop)
        self.stopped_starts: dict[int, str | None] = {}   # their leaders' start times, as recorded in stopped_path
        self.trainer_unreadable_logged = False
        # #116 review round 2: groups the guard stopped that could not be confirmed continued or gone (EPERM from a live
        # group, or an identity that cannot be checked), with their leaders' recorded start times. They survive new_cycle(),
        # stay in stopped_path (so a restart sees them too), show in the state file and are retried at every reading until
        # each is continued, gone, or found to be another process's group.
        self.unresolved: dict[int, str | None] = {}
        self.unresolved_alerted = False
        # Stopped for the day (#116, owner 2026-11-18): epoch until which no cycle starts, the pause's reason slug and the
        # rule that set it. Survives new_cycle() and, through the state file, a supervisor restart (load_day_stop).
        self.day_stop_until: float | None = None
        self.day_stop_reason: str | None = None
        self.day_stop_rule: str | None = None
        self.pauses: list[float] = []         # epoch of each pause in the last 24 h (state file)
        self.reading: Reading | None = None   # the last reading (state file)
        self.reason: str | None = None        # slug of the current pause (GUARD_REASONS)
        self.state_failed = False
        self.alerter = alerter or (lambda name, detail: alert(name, detail, log=self.log))
        self.budget = _env_float("CORTADOS_S1_LATENCY_BUDGET_S", 1.0, minimum=0) if budget_s is None else budget_s
        # Night window: read from the environment only when the day budget is (an injected day budget without an injected
        # window means a test pinned the budget, and the real wall clock must not loosen it between 01:00 and 07:00).
        if night_window == "env":
            night_window = _env_night_window() if budget_s is None else None
        self.night_window = _check_night_window(night_window)
        self.wall = wall
        night = (_env_float("CORTADOS_S1_NIGHT_LATENCY_BUDGET_S", DEFAULT_NIGHT_BUDGET_S, minimum=0)
                 if night_budget_s is None else night_budget_s)
        if not math.isfinite(night) or night < self.budget:
            # The night budget is the looser one by definition; a stricter or broken value is a misconfiguration. Injected
            # (code) values raise; an environment value is reported and replaced, as _env_float does, so a bad launchd
            # setting cannot crash-loop the supervisor.
            if night_budget_s is not None:
                raise ValueError(f"night latency budget {night!r} must be finite and >= the day budget {self.budget:g}s")
            fixed = max(DEFAULT_NIGHT_BUDGET_S, self.budget)
            print(f"overnight: CORTADOS_S1_NIGHT_LATENCY_BUDGET_S={night:g} is below the day budget {self.budget:g}s; "
                  f"using {fixed:g}", flush=True)
            night = fixed
        self.night_budget = night
        self.min_free = _env_float("CORTADOS_S1_MIN_FREE_GB", 8.0, minimum=0) if min_free_gb is None else min_free_gb
        self.interval = _env_float("CORTADOS_S1_PROBE_S", 30.0, above=0) if interval_s is None else interval_s
        # A trainer stopped longer than this is a stalled cycle, not back-pressure: release it, log loudly and alert.
        # minimum=60: two probes at the default 30 s interval; a shorter cap releases before the guard can see whether the
        # contention cleared, so the pause would be noise (and 0 would disable pausing entirely).
        self.max_pause = (_env_float("CORTADOS_S1_MAX_PAUSE_S", 1800.0, minimum=60) if max_pause_s is None
                          else max_pause_s)
        # After a forced release, no re-pause for this long (default = max_pause: training runs at least as long as it was
        # held). Without it a persistent 503 or low memory re-paused on the very next probe and the tree ran ~1% of the time
        # (2 h simulation, review 2026-11-03). minimum=60 for the same two-probe reason as MAX_PAUSE_S.
        default_hold = max(self.max_pause, 60.0)
        self.release_hold = (_env_float("CORTADOS_S1_RELEASE_HOLD_S", default_hold, minimum=60) if release_hold_s is None
                             else release_hold_s)
        self.forced_releases = 0   # consecutive forced releases (supervisor lifetime); a normal resume resets it
        self.stuck_alerted = False   # 'cycle result marker cannot be removed' alerted once per supervisor lifetime
        self.stop_seen = False   # the stop flag existed at some poll of the last run_cycle (set there, not by new_cycle)
        self.baseline_samples: list[float] = []       # baseline p50s
        self.baseline_p90_samples: list[float] = []   # baseline p90s (absent for a legacy float source: p90 = p50)
        self.last = None
        self.since = None    # clock time of the last state change: a reading covers only requests that started after it
        self.in_cycle = False
        self.new_cycle()

    def new_cycle(self) -> None:
        """Forget per-cycle state (pause, hysteresis, once-per-cycle config log, forced-release blindness). A stopped group
        never confirmed continued is not forgotten: it moves to the unresolved set, retried at every reading."""
        self.since = self.clock()
        leftover = [g for g in self.stopped_groups if g not in self.unresolved]
        for g in leftover:
            self.unresolved[g] = self.stopped_starts.get(g)
        self.stopped_groups = []
        self.stopped_starts = {}
        if leftover:
            self._persist_stopped()
            self._report_unresolved()
        self.reason = None
        self.paused = False
        self.pause_complete = False   # every relevant group confirmed stopped (discovery complete, every SIGSTOP landed)
        self.stop_requested = False   # set by _stop_for_day: run_cycle ends this cycle through the graceful stop
        self.good = 0
        self.paused_at = None
        self.unreadable = 0
        self.config_logged = False
        self.blind = False   # set after a forced release: an unreadable scorer must not re-pause the tree at once
        self.hold_until = None   # clock time before which a forced release is not re-paused (release_hold)

    def current_budget(self) -> tuple[float, str]:
        """(latency budget in seconds, label naming which budget applies) at the current local wall time."""
        if in_night_window(self.night_window, self.wall()):
            s, e = self.night_window
            return self.night_budget, f"night budget {self.night_budget:g}s ({s:%H:%M}-{e:%H:%M} local, Forest 2026-11-07)"
        return self.budget, f"day budget {self.budget:g}s (Forest 2026-11-03)"

    @property
    def baseline(self) -> float | None:
        return statistics.median(self.baseline_samples) if self.baseline_samples else None

    @property
    def baseline_p90(self) -> float | None:
        if self.baseline_p90_samples:
            return statistics.median(self.baseline_p90_samples)
        return self.baseline

    def _record_baseline(self, r: Reading) -> None:
        if not (math.isfinite(r.p50) and math.isfinite(r.p90)):
            return   # a timed-out scorer with the tree stopped is not a baseline (it is down or overloaded by routing)
        self.baseline_samples = (self.baseline_samples + [r.p50])[-self.BASELINE_SAMPLES:]
        self.baseline_p90_samples = (self.baseline_p90_samples + [r.p90])[-self.BASELINE_SAMPLES:]

    def _read_latency(self):
        """(Reading or None, refusal reason or None). Config errors are logged once per cycle, alerted, and read as None.
        A refusal's kind ("timeout" or "refused") is left in self.refused_kind for the state file's reason slug."""
        self.refused_kind = None
        try:
            if hasattr(self.latency, "read"):
                r = self.latency.read(self.since)
            else:
                lat = self.latency()
                r = None if lat is None else Reading(lat, lat, 1, 1 if math.isinf(lat) else 0, "probe")
            if r is not None:
                self.reading = r
            return r, None
        except ScorerTimedOut as e:
            self.reading = Reading(math.inf, math.inf, 1, 1, "probe")
            self.refused_kind = "timeout"
            return None, str(e)
        except ScorerRefused as e:
            self.refused_kind = "refused"
            return None, str(e)
        except ScorerConfigError as e:
            self._config_error(str(e))
            return None, None
        # Exception on purpose, NEVER BaseException: an unexpected probe fault must not kill the supervisor (launchd
        # would crash-loop it), but main() turns SIGTERM (launchd unload, Ctrl-C) into KeyboardInterrupt, and a probe can
        # block up to 30 s in urlopen; swallowing that (or SystemExit) would keep the cycle running past a stop.
        except Exception as e:
            # Type name only: an unexpected exception's message can quote the scorer URL. A distinct alert name, so a
            # code fault (TypeError, ImportError) is not triaged as an operator setting.
            self._config_error(f"scorer probe raised {type(e).__name__} (URL from {scorer_url_source()})",
                               name="scorer_probe_crashed", kind="PROBE CRASHED")
            return None, None

    def _config_error(self, detail: str, *, name: str = "scorer_probe_config", kind: str = "CONFIG ERROR") -> None:
        if not self.config_logged:
            self.config_logged = True
            self.log(f"overnight: {kind}: {detail}; the latency guard has no reading (not treated as back-pressure)")
            self.alerter(name, detail)

    def idle_probe(self) -> None:
        """No work tree running: every probe is a clean baseline sample."""
        if self.last is not None and self.clock() - self.last < self.interval:
            return
        self.last = self.clock()
        self.retry_unresolved()   # #116 review round 2: between cycles too
        lat, _ = self._read_latency()
        if lat is not None:
            self._record_baseline(lat)
        self.write_state()

    def _signal_groups(self, groups: list[int], sig: int) -> list[int]:
        """Signal each group; one that is already gone is skipped, never fatal. Returns the groups NOT confirmed signalled
        (EPERM: a zombie-only group whose leader is mid-exit, or a member we may not signal), so a pause knows it is
        incomplete. Never this supervisor's own group, nor 0/1 (safe_group)."""
        unconfirmed = []
        for g in groups:
            if not safe_group(g):
                continue
            try:
                self.signaller(g, sig)
            except ProcessLookupError:
                pass   # gone: nothing left to stop or continue
            except PermissionError:
                unconfirmed.append(g)
        return unconfirmed

    def _trainer_group(self) -> tuple[int | None, str | None, bool]:
        """(pgid, leader start, ok) of the trainer group the studio gate recorded in trainer_file (#116, owner 2026-11-18:
        found even when the gate wrapper crashed and the trainer left the cycle's tree). ok=False: a record exists that
        cannot be read or verified, so a trainer may be running that this pause cannot see. A record whose pgid now names
        another process (group_is_ours False) is stale and ignored."""
        if self.trainer_file is None:
            return None, None, True
        rec, readable = read_group_record(self.trainer_file)
        if not readable:
            if not self.trainer_unreadable_logged:
                self.trainer_unreadable_logged = True
                self.log(f"overnight: trainer group record {self.trainer_file.name} is unreadable; a pause cannot be "
                         "confirmed complete while it is")
            return None, None, False
        self.trainer_unreadable_logged = False
        if rec is None or not safe_group(rec["pgid"]):
            return None, None, True
        ours = group_is_ours(rec, leader_start=self.leader_start)
        if ours is None:
            return None, None, False
        if ours is False:
            return None, None, True   # pgid reused by an unrelated process: never signal it
        return rec["pgid"], rec["start"], True

    def _discover(self, pgid: int) -> tuple[list[int], bool, dict[int, str]]:
        """(groups, complete, leader starts): the cycle tree's groups plus the gate's recorded trainer group. complete=False
        when ps failed or the trainer record could not be checked."""
        try:
            d = _as_discovery(self.groups(pgid))
        except Exception as e:   # an injected or real discovery fault must never crash the supervisor mid-pause
            self.log(f"overnight: process discovery failed ({type(e).__name__}); not every group is known")
            d = Discovery([pgid], False, {})
        groups, starts = list(d.groups), dict(d.starts)
        tg, tstart, ok = self._trainer_group()
        if tg is not None:
            if tg not in groups:
                groups.append(tg)
            if tstart:
                starts[tg] = tstart
        return groups, d.complete and ok, starts

    def _record_stopped(self, groups: list[int], starts: dict[int, str]) -> None:
        """Add `groups` to the stopped set and persist it (stopped_path) BEFORE they are signalled (#116 review round 1):
        a supervisor killed mid-pause forgets stopped_groups, and recover_stopped_groups reads this file on restart. A
        failed write is logged (the trainer-group file and the tree walk still cover a restart), never fatal."""
        groups = [g for g in groups if safe_group(g)]
        for g in groups:
            if self.stopped_starts.get(g) is None:
                # An unresolved group stopped again keeps the start it was first recorded with (its identity).
                self.stopped_starts[g] = starts.get(g) or self.unresolved.get(g)
        new = [g for g in groups if g not in self.stopped_groups]
        self.stopped_groups = self.stopped_groups + new
        for g in new:
            self.unresolved.pop(g, None)   # this pause owns it now and continues it when it ends
        if new:
            self._persist_stopped()

    def _persist_stopped(self) -> None:
        """Write stopped_path: the current pause's groups plus every unresolved one (#116 review round 2: an unresolved
        group stays recorded until it is continued or gone). Nothing left: the file is removed."""
        if self.stopped_path is None:
            return
        recs = [{"pgid": g, "start": self.stopped_starts.get(g)} for g in self.stopped_groups]
        recs += [{"pgid": g, "start": st} for g, st in self.unresolved.items() if g not in self.stopped_groups]
        try:
            if recs:
                _write_json_durable(self.stopped_path, {"version": 1, "groups": recs})
            else:
                self.stopped_path.unlink(missing_ok=True)
        except OSError as e:
            self.log(f"overnight: cannot {'record' if recs else 'remove'} stopped groups in {self.stopped_path.name} "
                     f"({type(e).__name__}); a supervisor restart relies on the trainer-group file and the tree walk")

    def _zombie_group(self, g: int) -> bool:
        """An EPERM answer for group `g` means drained only when ps shows no live member left (the rule of _zombie_only).
        A ps failure is not 'drained'."""
        try:
            return not self.live_members(g)
        except (OSError, subprocess.SubprocessError):
            return False

    def _continue_group(self, g: int, start: str | None, *, verified: bool = False) -> str:
        """SIGCONT one group the guard stopped, checking first that it is still the group recorded (#116 review round 2:
        a group that exited during the pause may have had its id reused). verified: its identity is known right now (in
        the freshly walked tree, the gate's checked trainer record, or the cycle's unreaped root). Returns 'done'
        (continued, or gone), 'stale' (the id now names another process's group: never signalled) or 'keep' (identity
        undecidable, or EPERM with a live member: not confirmed continued)."""
        if not safe_group(g):
            return "done"   # never signalled by the guard, so never stopped by it
        if not verified:
            ours = group_is_ours({"pgid": g, "start": start}, leader_start=self.leader_start)
            if ours is False:
                return "stale"
            if ours is None:
                return "keep"
        try:
            self.signaller(g, signal.SIGCONT)
        except ProcessLookupError:
            return "done"
        except PermissionError:
            return "done" if self._zombie_group(g) else "keep"
        return "done"

    def _settle(self, g: int, outcome: str, start: str | None) -> None:
        """Record one continuation's outcome: the group leaves the pause's set; 'keep' puts it in the unresolved set."""
        if g in self.stopped_groups:
            self.stopped_groups = [x for x in self.stopped_groups if x != g]
        self.stopped_starts.pop(g, None)
        if outcome == "keep":
            if g not in self.unresolved:
                self.log(f"overnight: process group {g} could not be confirmed continued (EPERM from a live member, or "
                         "its identity cannot be checked); kept and retried at every reading")
            self.unresolved[g] = start
            return
        if outcome == "stale":
            self.log(f"overnight: stopped group {g} is gone and its id now belongs to another process; not signalled")
        self.unresolved.pop(g, None)

    def _report_unresolved(self, name: str = "guard_continue_incomplete") -> None:
        """Alert once when stopped groups are left unresolved; log once when every one has been resolved."""
        if self.unresolved and not self.unresolved_alerted:
            self.unresolved_alerted = True
            detail = (f"{len(self.unresolved)} process group(s) the guard stopped are not confirmed continued (EPERM from "
                      "a live member, or an identity ps cannot check); retried at every guard reading")
            self.log(f"overnight: !!! CONTINUE INCOMPLETE: {detail}")
            self.alerter(name, detail)
        elif not self.unresolved and self.unresolved_alerted:
            self.unresolved_alerted = False
            self.log("overnight: every process group the guard stopped is now confirmed continued or gone")

    def retry_unresolved(self, pgid: int | None = None) -> None:
        """Retry every unresolved group (#116 review round 2), at every reading: until each is continued, gone or found
        stale. pgid: the running cycle's root (our unreaped child, so its identity is certain). A group the current pause
        stopped again is skipped (_record_stopped took it out of the set)."""
        if not self.unresolved:
            return
        for g, start in list(self.unresolved.items()):
            if g in self.stopped_groups:
                continue
            self._settle(g, self._continue_group(g, start, verified=(g == pgid)), start)
        self._persist_stopped()
        self._report_unresolved()

    def load_unresolved(self) -> None:
        """Startup, after recover_stopped_groups: whatever it could not resolve is still in stopped_path. Those groups
        become this guard's unresolved set, retried at every reading (the recovery already alerted)."""
        if self.stopped_path is None:
            return
        recs, _ = read_stopped_groups(self.stopped_path)
        for r in recs:
            if safe_group(r["pgid"]):
                self.unresolved.setdefault(r["pgid"], r["start"])
        if self.unresolved:
            self.unresolved_alerted = True
            self.log(f"overnight: {len(self.unresolved)} process group(s) from a previous supervisor are not confirmed "
                     "continued; retried at every guard reading")

    def _pause(self, pgid: int, why: str, reason: str = "latency") -> None:
        # The cycle group first (nightly.sh then starts nothing new), then every gated step's own group (the trainer).
        # Each is recorded before its SIGSTOP. The root's start is looked up only when it is recorded (one ps call).
        root_start = {}
        if self.stopped_path is not None:
            try:
                root_start = {pgid: self.leader_start(pgid)}
            except (OSError, subprocess.SubprocessError):
                pass
        self._record_stopped([pgid], root_start)
        bad = self._signal_groups([pgid], signal.SIGSTOP)   # a leader that just exited (or is zombie-only) is no fault here
        groups, complete, starts = self._discover(pgid)
        others = [g for g in groups if g != pgid]
        self._record_stopped(others, starts)
        bad += self._signal_groups(others, signal.SIGSTOP)
        # #116 review round 1: a pause is complete only when discovery was complete AND every SIGSTOP landed; until then no
        # reading is a baseline and the discovery is retried at every reading (_complete_pause).
        self.pause_complete = complete and not bad
        self.paused = True
        self.paused_at = self.clock()
        self.since = self.paused_at
        self.unreadable = 0
        self.reason = reason
        now = self.epoch()
        self.pauses = [t for t in self.pauses if now - t < 86400] + [now]
        extra = len(self.stopped_groups) - 1
        self.log("overnight: PAUSE work tree: " + why + (f" (+{extra} gated step group(s) stopped)" if extra else "")
                 + ("" if self.pause_complete else "; NOT every process group is confirmed stopped: discovery is retried "
                    "at every reading and no reading counts as a baseline until it is"))
        self.write_state()

    def _complete_pause(self, pgid: int) -> None:
        """Retry an incomplete pause: discover again and SIGSTOP every group found (again: stopping a stopped group is a
        no-op, and a group that appeared since is caught). Complete -> readings from now on are the scorer alone."""
        groups, complete, starts = self._discover(pgid)
        self._record_stopped(groups, starts)
        bad = self._signal_groups(groups, signal.SIGSTOP)
        if complete and not bad:
            self.pause_complete = True
            self.since = self.clock()   # samples that began while something still ran are not the scorer alone
            self.good = 0
            self.log(f"overnight: pause complete: all {len(self.stopped_groups)} process group(s) of the cycle are stopped")

    def continue_all(self, pgid: int) -> None:
        """SIGCONT every group a pause stopped, every group in the tree now and the gate's recorded trainer group, the cycle
        group last (callers send that one themselves: continue_root). Before any TERM: a stopped trainer cannot act on it
        and would be KILLed at the gate's grace, losing its unsaved steps.
        #116 review round 2: a remembered group is signalled only when its identity is confirmed (in the tree walked now,
        or group_is_ours against its recorded start); one found to be another process's group is dropped unsignalled.
        A group not confirmed continued (identity undecidable, EPERM from a live member) stays in the stopped-groups record
        as unresolved, alerted, and retried at every reading; the record holds nothing else once this returns."""
        try:
            now, _, _ = self._discover(pgid)
        except Exception:   # never let a ps fault leave the trainer stopped
            now = []
        now = [g for g in now if g != pgid]
        remembered = [g for g in self.stopped_groups if g != pgid]
        for g in remembered:
            start = self.stopped_starts.get(g)
            self._settle(g, self._continue_group(g, start, verified=g in now), start)
        for g in now:
            if g in remembered:
                continue
            if g in self.unresolved:
                start = self.unresolved[g]
                self._settle(g, self._continue_group(g, start, verified=True), start)
            else:
                self._signal_groups([g], signal.SIGCONT)   # never stopped by this pause: a best-effort CONT
        self.retry_unresolved()
        self._persist_stopped()

    def continue_root(self, pgid: int, child=None) -> bool:
        """SIGCONT the cycle's own group (#116 review round 2: never the raw signaller, whose EPERM crashed the guard).
        Same EPERM rule as every cycle-group signal (_zombie_only): drained when no live member is left (with `child`, its
        leader first gets EPERM_SETTLE_S to become waitable); a live member that refuses leaves the group unresolved,
        alerted and retried at every reading, never a crash nor a claimed success. True when continued or gone."""
        start = self.stopped_starts.get(pgid, self.unresolved.get(pgid))
        try:
            self.signaller(pgid, signal.SIGCONT)
            ok = True
        except ProcessLookupError:
            ok = True
        except PermissionError:
            ok = True
            if child is not None:
                try:
                    child.wait(timeout=EPERM_SETTLE_S)
                except subprocess.TimeoutExpired:
                    ok = False   # the leader itself is running and refuses: a genuine failure
            ok = ok and self._zombie_group(pgid)
        self._settle(pgid, "done" if ok else "keep", start)
        self._persist_stopped()
        self._report_unresolved()
        return ok

    def _clear_trainer_record(self, pgid: int) -> None:
        """Remove the trainer-group record, only while it still names `pgid` (as the gate does: never another gate's)."""
        rec, _ = read_group_record(self.trainer_file)
        if rec is None or rec["pgid"] != pgid:
            return
        try:
            self.trainer_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log(f"overnight: cannot remove {self.trainer_file.name} ({type(e).__name__}); its group is gone, so a "
                     "later check finds nothing to stop")

    def stop_trainer_group(self, grace: float | None = None, *, clock=_time.monotonic, sleep=_time.sleep,
                           poll_s: float = 0.1, kill_wait_s: float | None = None) -> bool:
        """Stop the trainer group the studio gate recorded in trainer_file (#116, owner 2026-11-22: the guard must always be
        able to stop the trainer). run_cycle calls it on every graceful stop (window end, stop for the day, supervisor
        stop) AFTER the cycle's own group drained: a live gate has stopped its trainer and removed the record by then, so
        this finds nothing; a gate that crashed left its trainer running in its own group (start_new_session) with the
        training lock, outside the cycle's group, and the cycle's TERM never reached it. Same identity check as a pause
        (safe_group: never 0/1 or this supervisor's group; group_is_ours: the leader's start time). SIGCONT, SIGTERM, the
        gate's checkpoint grace (TRAINER_STOP_GRACE_S), SIGKILL if still alive. The record is cleared only when the group is
        gone. True when no recorded trainer group is left running, False when one may be (logged and alerted)."""
        grace = TRAINER_STOP_GRACE_S if grace is None else grace
        kill_wait_s = TRAINER_KILL_WAIT_S if kill_wait_s is None else kill_wait_s
        if self.trainer_file is None:
            return True
        rec, readable = read_group_record(self.trainer_file)
        if not readable:
            self.log(f"overnight: trainer group record {self.trainer_file.name} is unreadable; cannot stop a trainer the "
                     "gate may have left running")
            self.alerter("guard_trainer_unknown", f"{self.trainer_file.name} unreadable at a graceful stop")
            return False
        if rec is None:
            return True
        g = rec["pgid"]
        if not safe_group(g):
            self.log(f"overnight: trainer group record names group {g}, which the guard never signals; record kept")
            return False
        ours = group_is_ours(rec, leader_start=self.leader_start)
        if ours is None:
            self.log(f"overnight: recorded trainer group {g} cannot be verified (ps failed or no start recorded); not "
                     "signalled, record kept")
            self.alerter("guard_trainer_unknown", "recorded trainer group cannot be verified at a graceful stop")
            return False
        if ours is False or not self.group_alive(g):
            self._clear_trainer_record(g)   # gone (a reused leader pid means our group no longer exists)
            return True
        self.log(f"overnight: trainer group {g} outlived its gate; TERM, {grace:g}s to save its checkpoint, then KILL")
        for sig in (signal.SIGCONT, signal.SIGTERM):
            self._signal_groups([g], sig)
        end = clock() + grace
        while self.group_alive(g) and clock() < end:
            sleep(poll_s)
        if self.group_alive(g):
            # Re-check the identity before the KILL: the group may have drained and its id been taken in the meantime.
            if group_is_ours(rec, leader_start=self.leader_start) is not True:
                self.log(f"overnight: trainer group {g} changed identity during its grace; not killed, record kept")
                return False
            self.log(f"overnight: trainer group {g} still running {grace:g}s after TERM; sending KILL")
            self._signal_groups([g], signal.SIGKILL)
            end = clock() + kill_wait_s
            while self.group_alive(g) and clock() < end:
                sleep(poll_s)
        if self.group_alive(g):
            self.log(f"overnight: trainer group {g} still present after KILL; record kept so the guard still finds it")
            self.alerter("guard_trainer_survived", "a trainer group survived SIGKILL at a graceful stop")
            return False
        self._clear_trainer_record(g)
        return True

    def _continue(self, pgid: int) -> None:
        self.continue_root(pgid)
        self.continue_all(pgid)
        self.pause_complete = False
        self.since = self.clock()

    def _over_budget(self, lat: Reading | None, refused: str | None) -> str | None:
        """The reason slug when scoring is over its latency budget now (refused/timeout/latency), else None. Memory is not
        scoring: low memory alone at the pause limit is a forced release, not a stop for the day."""
        if refused:
            return getattr(self, "refused_kind", None) or "refused"
        if lat is None:
            return None
        if lat.timeouts:
            return "timeout"
        base, base90 = self.baseline, self.baseline_p90
        budget, _ = self.current_budget()
        if base is not None and (lat.p50 > base + budget or lat.p90 > base90 + budget):
            return "latency"
        return None

    def day_stop_active(self) -> bool:
        return self.day_stop_until is not None and self.epoch() < self.day_stop_until

    def _stop_for_day(self, pgid: int, why: str, slug: str) -> None:
        """Still over budget when the longest pause ends in a cycle that may train (#116, owner 2026-11-18): no
        release-and-re-pause. Training stops: run_cycle ends this cycle through the ordinary graceful stop (SIGCONT every
        stopped group, stop_group_drained: TERM, the gate gives its trainer TRAINING_STOP_GRACE_SECONDS to save its
        checkpoint, then stop_trainer_group for a trainer whose gate is gone), and until training_resume_at (1 h inside the
        window, else the next window start or midnight) every cycle runs labelling-only (owner 2026-11-22)."""
        until, rule_slug, rule = training_resume_at(self.wall())
        self.day_stop_until = until.timestamp()
        self.day_stop_reason = slug if slug in GUARD_REASONS else "latency"
        self.day_stop_rule = rule_slug
        self.stop_requested = True
        detail = (f"{why}; training stopped for the day (the trainer saves its checkpoint on the normal stop); no training "
                  f"until {until.strftime('%Y-%m-%d %H:%M %Z').strip()} ({rule}); labelling-only cycles continue")
        self.log(f"overnight: !!! STOPPED FOR THE DAY: {detail}")
        self.alerter("guard_stopped_for_day", detail)
        self.write_state()

    def clear_day_stop(self) -> None:
        self.log(f"overnight: day stop over (until rule {self.day_stop_rule or 'unknown'}); cycles may train again")
        self.day_stop_until = self.day_stop_reason = self.day_stop_rule = None
        self.write_state()

    def day_stop_expired(self) -> bool:
        """A day stop is still recorded but its time has passed (clear_day_stop is due)."""
        return self.day_stop_until is not None and not self.day_stop_active()

    # A day stop read back from the state file further ahead than this is not one the guard wrote (the next window start is
    # at most ~25 h away, DST included): a corrupt or hand-edited file must not stop training for days.
    DAY_STOP_MAX_S = 2 * 86400.0

    def load_day_stop(self) -> None:
        """Restore a day stop from the state file after a supervisor restart (every deploy restarts it): without this, a
        restart in the afternoon would start daytime cycles again the owner's rule forbids."""
        if self.state_path is None:
            return
        try:
            st = json.loads(self.state_path.read_text())
        except (OSError, ValueError, RecursionError):
            return
        # `until` is written for the whole day stop, also while a labelling cycle in it is paused (state "paused").
        if not isinstance(st, dict) or st.get("until") is None:
            return
        until, now = st.get("until"), self.epoch()
        if not (isinstance(until, (int, float)) and not isinstance(until, bool) and math.isfinite(until)):
            return
        if now < until <= now + self.DAY_STOP_MAX_S:
            self.day_stop_until = float(until)
            reason = st.get("reason")
            self.day_stop_reason = reason if reason in GUARD_REASONS else "latency"
            self.day_stop_rule = st.get("until_rule") if st.get("until_rule") in DAY_STOP_RULES else None
            self.log(f"overnight: training stays stopped (from the guard's state file; labelling continues) for "
                     f"{until - now:.0f}s more")

    def _release(self, pgid: int, why: str) -> None:
        self._continue(pgid)
        self.reason = "forced_release"
        self.paused = False
        self.good = 0
        self.paused_at = None
        self.unreadable = 0
        self.blind = True
        self.hold_until = self.clock() + self.release_hold
        self.forced_releases += 1
        detail = (f"{why}; forced release #{self.forced_releases} in a row; no re-pause for "
                  f"CORTADOS_S1_RELEASE_HOLD_S {self.release_hold:g}s")
        self.log(f"overnight: !!! FORCED RESUME of work tree: {detail}")
        self.alerter("guard_forced_resume", detail)
        self.write_state()

    def state(self) -> dict:
        """The guard's state for the live view (data/latency-guard.json -> live_heartbeat's s1 block). Metadata only: no
        URL, path, pid or request text; reasons are GUARD_REASONS slugs. Seconds are None when a percentile timed out."""
        budget, label = self.current_budget()
        r = self.reading
        fin = lambda x: round(x, 3) if x is not None and math.isfinite(x) else None
        now = self.epoch()
        day_stop = self.day_stop_active()
        # A pause shows first: during a day stop labelling cycles still run and the guard pauses them like any cycle
        # (owner 2026-11-22); `until`/`until_rule` say the day stop holds training meanwhile.
        # A cycle being stopped for the day (stop_requested) is no longer a pause: its groups are about to be continued.
        paused = self.paused and not self.stop_requested
        state = ("paused" if paused else "stopped_for_day" if day_stop else
                 "running" if self.in_cycle else "idle")
        if paused or (not day_stop and self.reason == "forced_release"):
            reason = self.reason
        elif day_stop:
            reason = self.day_stop_reason
        else:
            reason = None
        return {"version": 1, "updated_at": round(now, 3), "state": state, "reason": reason,
                # #116 review round 1: whether every process group of a pause is confirmed stopped ("incomplete": ps failed
                # or a SIGSTOP did not land; retried at every reading, and no baseline is taken meanwhile).
                "pause": ("complete" if self.pause_complete else "incomplete") if paused else None,
                # A day stop (#116, owner 2026-11-18/22): no training before `until` (epoch s), by the rule `until_rule`;
                # labelling-only cycles run meanwhile.
                "until": round(self.day_stop_until, 3) if day_stop else None,
                "until_rule": self.day_stop_rule if day_stop else None,
                "paused_s": round(self.clock() - self.paused_at, 1) if paused and self.paused_at is not None else None,
                "budget": "night" if label.startswith("night") else "day", "budget_s": budget,
                "baseline_p50_s": fin(self.baseline), "baseline_p90_s": fin(self.baseline_p90),
                "p50_s": fin(r.p50) if r else None, "p90_s": fin(r.p90) if r else None,
                "samples": r.n if r else 0, "timeouts": r.timeouts if r else 0, "source": r.source if r else None,
                "pauses_24h": sum(1 for t in self.pauses if now - t < 86400),
                # #116 review round 2: stopped groups not confirmed continued or gone (retried at every reading).
                "unresolved_groups": len(self.unresolved)}

    def write_state(self) -> None:
        """Atomic write of state() to state_path (None: no file, as in tests). A failure is logged once, never fatal."""
        if self.state_path is None:
            return
        try:
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(self.state()) + "\n")
            os.replace(tmp, self.state_path)
            self.state_failed = False
        except OSError as e:
            if not self.state_failed:
                self.state_failed = True
                self.log(f"overnight: cannot write guard state {self.state_path.name} ({type(e).__name__}); "
                         "the live view shows it stale")

    def tick(self, pgid: int) -> None:
        """One guard step for the running work group (rate-limited to one probe per interval)."""
        if self.stop_requested:
            return   # stopped for the day: run_cycle is ending the cycle; no further pause or release
        if self.last is not None and self.clock() - self.last < self.interval:
            return
        self.last = self.clock()
        try:
            self.retry_unresolved(pgid)   # #116 review round 2: until each is continued, gone or another process's
            self._step(pgid)
        finally:
            self.write_state()   # every reading, the release hold-off included: the live view marks a silent guard stale

    def _step(self, pgid: int) -> None:
        if self.paused and not self.pause_complete:
            self._complete_pause(pgid)   # #116 review round 1: an incomplete pause retries discovery at every reading
        (lat, refused), mem = self._read_latency(), self.memory()
        if lat is not None:
            self.blind = False
        if self.paused:
            self.unreadable = 0 if (lat is not None or refused) else self.unreadable + 1
            held = self.clock() - self.paused_at
            if held >= self.max_pause:
                why = f"paused {held:.0f}s >= CORTADOS_S1_MAX_PAUSE_S {self.max_pause:g}s"
                # Judged against the baseline as it stood, before this reading joins it (owner 2026-11-18: still over
                # budget at the limit -> stop for the day; otherwise the forced release below, as before).
                over = self._over_budget(lat, refused)
                if over is not None and self.cycle_trains:
                    return self._stop_for_day(pgid, f"{why} and scoring still over budget ({over})", over)
                if over is not None:
                    # A labelling-only cycle (outside the window, or during a day stop): the normal pause rules apply, and
                    # their end at the limit is the forced release with its hold-off (owner 2026-11-22, item 2).
                    why += f" and scoring still over budget ({over}); a labelling-only cycle is released, not stopped"
                return self._release(pgid, why)
            if lat is not None and self.pause_complete:
                self._record_baseline(lat)   # every group is stopped: this reading is the scorer alone
            if self.unreadable >= self.MAX_UNREADABLE:
                return self._release(pgid, f"{self.unreadable} consecutive unreadable scorer probes while paused")
        elif self.hold_until is not None:
            if self.clock() < self.hold_until:
                return   # release hold-off: the tree runs at least as long as it was held, whatever the probes say
            self.hold_until = None
        self._judge(pgid, lat, refused, mem)

    def _judge(self, pgid: int, lat: Reading | None, refused: str | None, mem: float | None) -> None:
        reasons = [refused] if refused else []
        slug = (getattr(self, "refused_kind", None) or "refused") if refused else None
        base, base90 = self.baseline, self.baseline_p90
        budget, budget_label = self.current_budget()
        if lat is not None and lat.timeouts:
            # A routed call waiting this long was degraded: over every budget, baseline or not.
            reasons.append(f"{lat.timeouts} of {lat.n} scorer request(s) past the router's S1 timeout "
                           f"(p50 {_fmt_s(lat.p50)}, p90 {_fmt_s(lat.p90)}, {lat.source})")
            slug = slug or "timeout"
        elif lat is not None and base is not None and (lat.p50 > base + budget or lat.p90 > base90 + budget):
            reasons.append(f"scorer latency p50 {_fmt_s(lat.p50)} / p90 {_fmt_s(lat.p90)} ({lat.n} {lat.source} sample(s)) "
                           f"> baseline p50 {base:.2f}s / p90 {base90:.2f}s + {budget_label}")
            slug = slug or "latency"
        if mem is not None and mem < self.min_free:
            reasons.append(f"free memory {mem:.1f} GB < {self.min_free:g} GB")
            slug = slug or "memory"
        if reasons:
            self.good = 0
            if not self.paused:
                self._pause(pgid, "; ".join(reasons) + f" [latency budget in force: {budget_label}]", slug)
            return
        if lat is None and base is None and not self.paused and not self.blind:
            # Without a baseline the latency rule cannot be judged: pause once to measure the scorer alone.
            self._pause(pgid, "no scorer latency baseline yet; measuring it with the tree stopped", "no_baseline")
            return
        if lat is not None and base is None and not self.paused and not self.blind:
            # A reading but no baseline to judge it by (#116: before, this case let the trainer run unjudged): pause once
            # to measure the scorer alone.
            self._pause(pgid, f"no scorer latency baseline yet (p50 {_fmt_s(lat.p50)}, p90 {_fmt_s(lat.p90)} with the tree "
                        "running); measuring it with the tree stopped", "no_baseline")
            return
        if self.paused:
            if lat is None or base is None:
                # An unreachable scorer is not evidence the budget is met; nor is a reading with no baseline to judge it by
                # (an incomplete pause takes none, #116 review round 1): keep measuring rather than resume unjudged.
                self.good = 0
                return
            self.good += 1
            if self.good >= self.RESUME_AFTER:
                self._continue(pgid)
                self.paused = False
                self.paused_at = None
                self.good = 0
                self.reason = None
                self.forced_releases = 0
                self.log(f"overnight: RESUME work tree: latency p50 {_fmt_s(lat.p50)} / p90 {_fmt_s(lat.p90)} within "
                         f"{budget_label} for {self.RESUME_AFTER} readings"
                         + (f", free memory {mem:.1f} GB" if mem is not None else ""))


def stop_tree(child, *, grace: float = TRAIN_STOP_GRACE_S) -> None:
    """SIGCONT first (a stopped process cannot handle TERM), then TERM so nightly.sh/the trainer save checkpoints, then KILL
    whatever of the group is left after `grace`. One implementation: stop_group_drained (waits for the whole group, not just
    the shell leader)."""
    stop_group_drained(child, grace=grace)


# Poll interval while waiting for a TERMed group to drain: short against the 60 s grace, cheap (one kill(2) per poll).
DRAIN_POLL_S = 0.1


# While draining, the cycle group gets SIGTERM again every DRAIN_RESEND_TERM_S until it drains or the grace ends. A single TERM
# that lands while nightly.sh is between steps is latched by bash, which then starts the next gated() step: that new
# studio_capacity gate joins the group after the TERM, never sees one, runs on, and is SIGKILLed at the grace, orphaning its
# own-session trainer with the training lock fd. Re-sending reaches such a late joiner with ~55 s of the 60 s grace left (the
# gate needs up to 30 s to stop its trainer). The gate treats repeated TERMs as one stop request, so re-sending is harmless.
DRAIN_RESEND_TERM_S = 5.0


# Seen on Darwin: killpg answers EPERM for a group whose leader is exiting but not yet waitable; that window is milliseconds.
EPERM_SETTLE_S = 1.0


def _zombie_only(child, err: PermissionError) -> bool:
    """The one EPERM rule for every killpg on a cycle group. Darwin answers EPERM (not ESRCH) when every remaining member is a
    zombie (seen with the just-exited leader not yet reaped): nothing in the group can run any more, so it has drained. Same uid,
    so EPERM cannot hide a live member of ours; but if the leader is still running the group is NOT zombie-only and the EPERM is
    a genuine error, so it is re-raised. A leader that is mid-exit (TERM just landed) also answers EPERM before it is
    reapable, so the leader gets EPERM_SETTLE_S to become a zombie before the EPERM counts as genuine. On True it is reaped."""
    try:
        child.wait(timeout=EPERM_SETTLE_S)
    except subprocess.TimeoutExpired:
        raise err from None
    # The leader exiting does not prove the rest of the group is zombie-only: a live member we cannot signal also answers EPERM.
    # Ask ps for the group's members and treat the EPERM as "drained" only when every one left is a zombie (Codex review r3).
    live = _live_group_members(child.pid)
    if live:
        raise PermissionError(err.errno, f"{err.strerror}: group {child.pid} still has live members {live}") from err
    return True


def _live_group_members(pgid: int) -> list[int]:
    """Pids in process group `pgid` that are not zombies, from `ps -A -o pid=,pgid=,stat=`. A ps failure raises: guessing
    "drained" here could leave training running with the node lock held."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, check=True, timeout=10).stdout
    live = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(pgid) and not parts[2].startswith("Z"):
            live.append(int(parts[0]))
    return live


def send_group_drained(child, sig: int) -> bool:
    """killpg(child's group, sig). Returns True when the group has already drained (ESRCH, or EPERM on a zombie-only group,
    see _zombie_only), with the leader reaped; False when the signal was delivered. Any other error propagates."""
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        child.wait()
        return True
    except PermissionError as e:
        return _zombie_only(child, e)
    return False


def stop_group_drained(child, *, grace: float, poll_s: float = DRAIN_POLL_S, clock=_time.monotonic,
                       sleep=_time.sleep, resend_s: float = DRAIN_RESEND_TERM_S) -> bool:
    """Window-end stop: SIGCONT + SIGTERM the cycle's group, then wait for the WHOLE group to drain (not just the shell leader)
    for up to `grace`, and only then SIGKILL survivors. stop_tree's KILL right after the leader exits killed studio_capacity's gate
    wrapper while it was still stopping its separately grouped trainer: the trainer was orphaned and kept the inherited LOCK_EX.
    Every send (CONT, TERM, the 0 probe, the final CONT/KILL) goes through send_group_drained, so a group that is already
    zombie-only at any point ends the stop normally instead of raising. While waiting, TERM is re-sent every `resend_s`
    (see DRAIN_RESEND_TERM_S) so a process that joined the group after the first TERM is stopped too.
    Returns True when the group drained within the grace, False when survivors were killed."""
    pgid = child.pid
    if send_group_drained(child, signal.SIGCONT) or send_group_drained(child, signal.SIGTERM):
        return True
    start = clock()
    end = start + grace
    next_term = start + resend_s
    while True:
        child.poll()   # reap the leader: an unreaped zombie leader would keep the group "alive" to kill(pgid, 0)
        if send_group_drained(child, 0):
            return True
        now = clock()
        if now >= end:
            break
        if now >= next_term:
            if send_group_drained(child, signal.SIGTERM):
                return True
            next_term = now + resend_s
        sleep(poll_s)
    print(f"overnight: cycle group {pgid} still running {grace:g}s after TERM; sending KILL", flush=True)
    if send_group_drained(child, signal.SIGKILL):
        return True
    child.wait()
    return False


def stop_cycle_group(child, guard, *, grace: float) -> None:
    """stop_group_drained for run_cycle's stop paths, EPERM-safe (#116 review round 2, #131 item 3): a cycle group with a
    live member that refuses a signal (genuine EPERM, re-raised by _zombie_only) is logged and alerted, never a supervisor
    crash (launchd would restart it into the same group); the finally's continue_root then keeps it unresolved."""
    try:
        stop_group_drained(child, grace=grace)
    except PermissionError as e:
        print(f"overnight: !!! cannot stop the cycle's process group {child.pid}: {e.strerror or e} (a live member "
              "refuses the signal)", flush=True)
        guard.alerter("guard_stop_incomplete", "the cycle's process group refused a stop signal (EPERM) with live members")


def deadline_reached(now: datetime, deadline: datetime) -> bool:
    """now >= deadline as instants. Aware datetimes sharing one ZoneInfo compare by WALL time and ignore fold, so during the
    repeated fall-back hour 01:30 EDT (fold=0) and 01:30 EST (fold=1) compare equal; going through UTC compares real instants.
    Naive datetimes (tests' fixed clocks) compare as they are; mixing naive and aware still raises TypeError."""
    if now.tzinfo is None or deadline.tzinfo is None:
        return now >= deadline
    return now.astimezone(timezone.utc) >= deadline.astimezone(timezone.utc)


RESULT_MARKERS = ("worked", "quota")


def _read_result(result_file: Path) -> str:
    """The cycle-result marker: 'worked', 'quota', or 'idle' (absent, empty, unknown or unreadable; the last two logged)."""
    try:
        text = result_file.read_text().strip()
    except FileNotFoundError:
        return "idle"
    except OSError as e:
        print(f"overnight: cannot read cycle result {result_file} ({e}); treating the cycle as idle", flush=True)
        return "idle"
    if text in RESULT_MARKERS:
        return text
    if text:
        print(f"overnight: unknown cycle result {text!r} in {result_file}; treating the cycle as idle", flush=True)
    return "idle"


def run_cycle(command: list[str], guard: Guard, *, result_file: Path, poll_s: float = 1.0, grace: float = TRAIN_STOP_GRACE_S,
              sleep=_time.sleep, pidfile: Path | None = None, stopflag: Path | None = None,
              grace_end: float = TRAIN_STOP_GRACE_S, trainer_grace: float | None = None) -> tuple[int, str]:
    """Run one nightly.sh cycle under the guard. Returns (rc, result) with result 'worked', 'quota' or 'idle'.
    guard.stop_seen is set when stopflag existed at any poll of this cycle (and after the child exits), so an operator who
    removes it during the TERM grace (to resume) is still read as a stop, not as a failure. Residual race: a flag created
    and removed entirely inside one poll_s window is never seen, and that rc 130 is read as an external TERM.
    During the guard's day stop the cycle is labelling-only (CORTADOS_S1_TRAIN_ALLOWED=0, the same switch the night window
    uses; owner 2026-11-22). Every graceful stop ends with guard.stop_trainer_group (trainer_grace: its checkpoint grace)."""
    guard.stop_seen = False
    def watch_stop():
        if stopflag is not None and not guard.stop_seen and stopflag.exists():
            guard.stop_seen = True
    stale = False
    try:
        result_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:   # a stale marker we cannot remove would be misread as this cycle's: 'worked' is read as idle
        stale = True
        print(f"overnight: WARNING: cannot remove stale cycle result {result_file} ({e}); a 'worked' result will be treated "
              "as idle, a 'quota' result is still honoured", flush=True)
        if not guard.stuck_alerted:
            # Silent otherwise: back-to-back cycling degrades to one cycle per idle interval (or a stale quota pins the
            # 5 h cooldown) every cycle until someone fixes the permissions.
            guard.stuck_alerted = True
            # The file name only: status alerts reach the dashboard and handbacks; the full path (account name) is logged above.
            guard.alerter("cycle_result_stuck", f"cannot remove {result_file.name} ({type(e).__name__}); cycle results may be a "
                          "cycle old: 'worked' is read as idle, 'quota' still cools down")
    allowed, why, deadline = train_policy(guard.wall())
    if guard.day_stop_active():
        # Owner 2026-11-22 (#116, item 2): a day stop holds TRAINING only. The cycle still runs, labelling-only, through the
        # existing switch (nightly.sh skips every gated step, the sweep included, and labels; label.py keeps its 15% per-
        # account quota floor), and the guard still pauses it like any cycle.
        until = datetime.fromtimestamp(guard.day_stop_until, NIGHT_ZONE).strftime("%Y-%m-%d %H:%M %Z").strip()
        allowed, deadline = False, None
        why = (f"training skipped: stopped for the day by the S1 latency guard until {until} "
               f"(rule {guard.day_stop_rule or 'unknown'}); labelling only")
    print(f"overnight: {why}", flush=True)
    env = dict(os.environ, CORTADOS_NIGHTLY_SUPERVISED="1", TZ="America/Toronto", CORTADOS_S1_CONTINUOUS="1",
               CORTADOS_S1_CYCLE_RESULT=str(result_file), CORTADOS_S1_TRAIN_ALLOWED="1" if allowed else "0",
               CORTADOS_S1_TRAIN_SKIP_REASON="" if allowed else why,
               # the window end (epoch s): a finished sweep keeps training off until then (nightly.sh "Sweep first")
               CORTADOS_S1_TRAIN_UNTIL=str(int(deadline.timestamp())) if deadline is not None else "")
    if guard.trainer_file is not None:
        # #116 (owner 2026-11-18): the studio gate records its trainer's process group here, so the guard finds it even
        # after the gate wrapper crashes (studio_capacity.run_holding_lock).
        env["CORTADOS_S1_TRAINER_GROUP_FILE"] = str(guard.trainer_file)
    guard.new_cycle()
    guard.cycle_trains = allowed
    child = subprocess.Popen(command, env=env, start_new_session=True)
    guard.in_cycle = True
    guard.write_state()
    window_ended = day_stopped = False
    try:
        while child.poll() is None:
            watch_stop()
            if deadline is not None and deadline_reached(guard.wall(), deadline):
                # The deadline lives here, not in nightly.sh: this supervisor already owns the window and group signalling, and
                # the gate (studio_capacity) forwards our TERM to its own training group. The whole cycle ends; the next one
                # starts at once, labels, and skips training until the next window. mlx_lm checkpoints (save-every 100) stay.
                print(f"overnight: night window ended at {deadline:%H:%M}; stopping this cycle's training "
                      f"(TERM, {grace_end:g}s grace, then KILL); checkpoints are kept", flush=True)
                window_ended = True
                guard.continue_all(child.pid)   # a paused trainer's own group: CONT before the gate TERMs it
                stop_cycle_group(child, guard, grace=grace_end)
                guard.stop_trainer_group(trainer_grace)   # a trainer whose gate crashed is outside the drained group
                break
            guard.tick(child.pid)
            if guard.stop_requested:
                # Stopped for the day (#116, owner 2026-11-18): the same graceful stop as the window end, so the gate
                # TERMs its trainer and gives it its grace to save the checkpoint; every stopped group is continued first.
                print(f"overnight: scoring still over budget at the pause limit; stopping this cycle (TERM, {grace_end:g}s "
                      "grace, then KILL); checkpoints are kept", flush=True)
                day_stopped = True
                guard.continue_all(child.pid)
                stop_cycle_group(child, guard, grace=grace_end)
                guard.stop_trainer_group(trainer_grace)
                break
            sleep(poll_s)
        watch_stop()
    except BaseException:
        # launchd stop/restart of com.cortados.s1-continuous (every deploy) arrives here as SIGTERM -> KeyboardInterrupt.
        guard.continue_all(child.pid)
        stop_cycle_group(child, guard, grace=grace)
        try:
            guard.stop_trainer_group(trainer_grace)
        except Exception as e:   # never hide the stop that brought us here
            print(f"overnight: stopping the recorded trainer group failed ({type(e).__name__})", flush=True)
        raise
    finally:
        # Never leave a group stopped: the shell can exit while a paused grandchild (the trainer) is still SIGSTOPped, and
        # a gated step's trainer leads its own group (#116), so every group a pause stopped gets SIGCONT too.
        guard.in_cycle = False
        guard.cycle_trains = True
        guard.continue_all(child.pid)
        # Zombie-only group (leader gone): nothing left to continue. A live member refusing: unresolved and retried, never
        # a crash out of this finally (#116 review round 2).
        guard.continue_root(child.pid, child)
        guard.new_cycle()
        guard.write_state()
        if pidfile is not None and child.poll() is not None:
            try:
                if pidfile.read_text().strip() == str(child.pid):
                    pidfile.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"overnight: cannot clear pidfile {pidfile} ({e})", flush=True)
    if window_ended:
        return child.returncode, "window_end"
    if day_stopped:
        return child.returncode, "stopped_for_day"
    result = _read_result(result_file)
    if stale and result == "worked":
        # A stale 'worked' would relaunch at once forever: degrade it to idle. 'quota' is honoured even if stale, because
        # over-cooling is safe and dropping a real quota stop would relaunch into the exhausted window every idle interval.
        print(f"overnight: WARNING: cycle result {result_file} may be the previous cycle's; treating this cycle as idle",
              flush=True)
        return child.returncode, "idle"
    return child.returncode, result


def recover_pidfile(pidfile: Path, *, ps=None, signaller=None, alive=None, log=lambda m: print(m, flush=True),
                    stopped_path: Path | None = None, leader_start=None, live_members=None, alerter=None) -> None:
    """Continuous startup: a pidfile left by a supervisor that died mid-pause can name a live, SIGSTOPped nightly.sh, which
    makes every new cycle exit 'already running' forever. SIGCONT such a group; drop a pidfile whose process is gone.
    EPERM (#116 review round 2): drained when no live member is left; otherwise logged, alerted and the group added to
    stopped_path as unresolved, so the guard (load_unresolved) retries it at every reading. Never a crash.
    #116 review round 3: recover_stopped_groups leaves the root's own record in stopped_path for this function, which drops
    it only once the root is confirmed continued, running or gone. A status it cannot read (ps failed or timed out) keeps
    the root as unresolved (recorded if it was not), logged and alerted, so it is never forgotten while it may be stopped."""
    signaller = signaller or signal_group
    live_members = live_members or _live_group_members
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    def _ps(pid):
        return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout.strip()
    alive, ps = alive or _alive, ps or _ps
    try:
        raw = pidfile.read_text().strip()
    except FileNotFoundError:
        return
    except OSError as e:
        log(f"overnight: cannot read {pidfile} ({e}); leaving it")
        return
    try:
        pid = int(raw)
    except ValueError:
        log(f"overnight: {pidfile} holds {raw!r}, not a pid; removing it")
        pidfile.unlink(missing_ok=True)
        return
    def _resolved():
        if stopped_path is not None:
            _drop_recorded(stopped_path, pid, log)
    if not alive(pid):
        log(f"overnight: stale pidfile {pidfile} (pid {pid} is gone); removing it")
        pidfile.unlink(missing_ok=True)
        _resolved()
        return
    try:
        stat = ps(pid)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"overnight: !!! ps for pid {pid} failed ({type(e).__name__}); cannot tell whether it is stopped; kept as "
            "unresolved, retried at every guard reading")
        if alerter is not None:
            alerter("guard_recovery_incomplete", "the previous cycle's root status could not be read at startup")
        if stopped_path is not None:
            _add_unresolved(stopped_path, pid, leader_start or proc_start, log)
        return
    if stat.startswith("T"):
        try:
            signaller(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                drained = not live_members(pid)
            except (OSError, subprocess.SubprocessError):
                drained = False
            if not drained:
                log(f"overnight: !!! pid {pid} from {pidfile} is stopped and its group refused SIGCONT (EPERM); kept as "
                    "unresolved, retried at every guard reading")
                if alerter is not None:
                    alerter("guard_recovery_incomplete", "the previous cycle's group refused SIGCONT (EPERM) at startup")
                if stopped_path is not None:
                    _add_unresolved(stopped_path, pid, leader_start or proc_start, log)
                return
        log(f"overnight: pid {pid} from {pidfile} was left stopped (state {stat}); sent SIGCONT to its group so it can finish")
    else:
        log(f"overnight: pid {pid} from {pidfile} is running (state {stat}); nightly.sh will refuse a second run until it ends")
    _resolved()


def _drop_recorded(stopped_path: Path, pgid: int, log) -> None:
    """Remove group `pgid` from the stopped-groups record once it is confirmed continued, running or gone (the file is
    removed when nothing else is left in it). An unreadable record is left as it is."""
    recs, ok = read_stopped_groups(stopped_path)
    if not ok or not any(r["pgid"] == pgid for r in recs):
        return
    rest = [r for r in recs if r["pgid"] != pgid]
    try:
        if rest:
            _write_json_durable(stopped_path, {"version": 1, "groups": rest})
        else:
            stopped_path.unlink(missing_ok=True)
    except OSError as e:
        log(f"overnight: cannot update {stopped_path.name} ({type(e).__name__}); the guard retries group {pgid} harmlessly")


def _add_unresolved(stopped_path: Path, pgid: int, leader_start, log) -> None:
    """Append group `pgid` (with its leader's start, if ps can tell) to the stopped-groups record as unresolved."""
    recs, _ = read_stopped_groups(stopped_path)
    if any(r["pgid"] == pgid for r in recs):
        return
    try:
        start = _norm_start(leader_start(pgid))
    except (OSError, subprocess.SubprocessError):
        start = None
    try:
        _write_json_durable(stopped_path, {"version": 1, "groups": recs + [{"pgid": pgid, "start": start}]})
    except OSError as e:
        log(f"overnight: cannot record unresolved group in {stopped_path.name} ({type(e).__name__})")


def recover_stopped_groups(stopped_path: Path, trainer_file: Path | None, pidfile: Path, *, signaller=None,
                           leader_start=None, discover=None, alive=None, log=lambda m: print(m, flush=True),
                           live_members=None, alerter=None) -> list[int]:
    """Continuous startup, BEFORE recover_pidfile resumes the cycle's root (#116 review round 1): a supervisor killed
    mid-pause forgot which groups it stopped, and recover_pidfile alone continued only nightly.sh's group, leaving the gated
    trainer (its own group) SIGSTOPped forever with the training lock and GPU memory. SIGCONT, in this order of discovery:
    every group in stopped_path that is still the one recorded (group_is_ours), the trainer group the studio gate recorded
    in trainer_file (same check), and every group still in the tree under the pidfile's live root. Never the root itself
    (recover_pidfile's job, after these), never this process's own group. Returns the groups continued.
    #116 review round 2: a group not confirmed continued (its identity undecidable, or EPERM from a live member) is NOT
    forgotten: stopped_path is rewritten with exactly those groups (removed when there are none), the recovery is logged
    and alerted as incomplete, and the new guard retries them at every reading (Guard.load_unresolved). A group found to
    be another process's now is dropped unsignalled.
    #116 review round 3: the live root's own record is kept in stopped_path (not counted as incomplete here):
    recover_pidfile, which runs next, drops it only once the root is confirmed continued, running or gone."""
    signaller = signaller or signal_group
    discover = discover or discover_groups
    leader_start = leader_start or proc_start
    live_members = live_members or _live_group_members
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    alive = alive or _alive
    root = None
    try:
        root = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        pass
    if root is not None and not alive(root):
        root = None
    targets: list[int] = []
    starts: dict[int, str | None] = {}
    kept: list[dict] = []
    root_kept: list[dict] = []
    recs, ok = read_stopped_groups(stopped_path)
    if not ok:
        log(f"overnight: {stopped_path.name} is unreadable; continuing every group that can still be found")
    if trainer_file is not None:
        tr, tok = read_group_record(trainer_file)
        if not tok:
            log(f"overnight: trainer group record {trainer_file.name} is unreadable; its group is found by the tree walk only")
        if tr is not None:
            recs.append(tr)
    for rec in recs:
        g = rec["pgid"]
        if g == root and safe_group(g) and not root_kept:
            root_kept.append({"pgid": g, "start": rec["start"]})   # recover_pidfile's to resolve
            continue
        if not safe_group(g) or g == root or g in targets:
            continue
        if any(k["pgid"] == g for k in kept):
            continue
        ours = group_is_ours(rec, leader_start=leader_start)
        if ours:
            targets.append(g)
            starts[g] = rec["start"]
        elif ours is False:
            log(f"overnight: recorded group {g} now belongs to another process; not signalled")
        else:
            log(f"overnight: recorded group {g} cannot be verified (ps failed or no start recorded); not signalled, kept "
                "as unresolved")
            kept.append({"pgid": g, "start": rec["start"]})
    if root is not None:
        d = discover(root)
        for g in d.groups:
            if g != root and g not in targets and safe_group(g):
                targets.append(g)
                starts[g] = d.starts.get(g)
                kept = [k for k in kept if k["pgid"] != g]   # in the live tree now: its identity is certain
        if not d.complete:
            log("overnight: the cycle's tree could not be walked; only recorded groups were continued")
    done = []
    for g in targets:
        try:
            signaller(g, signal.SIGCONT)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                drained = not live_members(g)
            except (OSError, subprocess.SubprocessError):
                drained = False
            if not drained:
                log(f"overnight: group {g} refused SIGCONT (EPERM) with a live member; kept as unresolved")
                kept.append({"pgid": g, "start": starts.get(g)})
                continue
        done.append(g)
    if done:
        log(f"overnight: continued {len(done)} process group(s) a previous supervisor may have left stopped")
    try:
        if kept or root_kept:
            _write_json_durable(stopped_path, {"version": 1, "groups": kept + root_kept})
        else:
            stopped_path.unlink(missing_ok=True)
    except OSError as e:
        log(f"overnight: cannot {'record' if kept or root_kept else 'remove'} {stopped_path.name} ({type(e).__name__})")
    if kept or not ok:
        detail = (f"{len(kept)} process group(s) a previous supervisor stopped are not confirmed continued; retried at "
                  "every guard reading" if kept else f"{stopped_path.name} was unreadable; only the live tree was continued")
        log(f"overnight: !!! RECOVERY INCOMPLETE: {detail}")
        if alerter is not None:
            alerter("guard_recovery_incomplete", detail)
    return done


# Failure backoff cap. Exit 3 (Claude quota headroom low/unknown) recovers on the accounts' ~5 h usage window, and other failures
# (provider down, base model missing, eval crash) need a human or a slow external recovery: retrying faster than every 4 h only
# burns quota and fills logs, while 4 h still gives several attempts per day once the cause clears.
BACKOFF_CAP_S = 4 * 3600.0
# 'quota' cooldown default: the accounts' 5 h usage window. A cycle that stopped on the labeler's headroom floor (15%) must not
# relaunch at once, or the next cycle labels again on the same window and the floor is defeated.
QUOTA_COOLDOWN_S = 5 * 3600.0
# Backoff exponent and persisted failure count are clamped: 2**20 * 60 s already exceeds BACKOFF_CAP_S by far, and an unclamped
# count (a long outage, a corrupt .failures file) makes idle_s * 2**n overflow a float and crash the supervisor.
MAX_BACKOFF_EXP = 20
MAX_FAILURES = 1000


def next_delay(rc: int, worked, idle_s: float, failures: int, quota_s: float = QUOTA_COOLDOWN_S) -> tuple[float, int]:
    """(seconds to sleep before the next cycle, updated consecutive-failure count) for a finished cycle.
    `worked` is the cycle result: 'worked', 'quota' or 'idle' (True/False are accepted as worked/idle)."""
    result = "worked" if worked is True else ("idle" if worked is False else worked)
    if rc != 0:
        failures = min(failures + 1, MAX_FAILURES)
        delay = min(idle_s * 2 ** min(failures - 1, MAX_BACKOFF_EXP), BACKOFF_CAP_S)
        return (max(delay, quota_s) if result == "quota" else delay), failures
    if result == "quota":
        return quota_s, 0
    return (0.0 if result == "worked" else idle_s), 0


def sweep_delay(delay: float, rc: int, result, now: datetime, *, window_raw: str | None = None,
                switch_raw: str | None = None, zone=None) -> tuple[float, str | None]:
    """The sleep before the next cycle while a calibration sweep request is pending (#74): (delay, note or None).
    The next in-window cycle runs ONLY the sweep, which makes no labeling calls, so the labeler's quota cooldown has nothing to
    protect there: inside the window (or with training allowed any time) a rc-0 'quota' cycle is followed at once. Outside the
    window the next cycle labels, so the cooldown stands, but no delay (cooldown, failure backoff or idle) may run past the
    next window start: the sweep needs the whole window (2026-11-09: a 5 h cooldown from an evening quota stop ate 3 h of it).
    A failure inside the window keeps its backoff: retrying a failing sweep at once would hot-loop."""
    allowed, _, _ = train_policy(now, window_raw=window_raw, switch_raw=switch_raw, zone=zone)
    if allowed:
        if rc == 0 and result == "quota" and delay:
            return 0.0, "a sweep request is pending: the next cycle runs only the sweep (no labeling), so no quota cooldown"
        return delay, None
    raw = os.environ.get("CORTADOS_S1_NIGHT_WINDOW", "") if window_raw is None else window_raw
    try:
        window = parse_night_window(raw.strip() or DEFAULT_NIGHT_WINDOW)
    except ValueError:
        return delay, None      # no valid window: nothing to wait for (train_policy already fails closed)
    if window is None:
        return delay, None
    start = _next_start(window, now, zone=zone)
    # aware: absolute instants (same-tzinfo subtraction would use wall time and be an hour off across DST); naive: test clocks
    gap = start.timestamp() - now.timestamp() if now.tzinfo is not None else (start - now).total_seconds()
    left = max(0.0, gap) + 1.0    # +1 s: land inside the window, not on its edge
    if delay > left:
        return left, (f"a sweep request is pending: next cycle at the night window start "
                      f"{start.strftime('%H:%M')} instead of in {delay:g}s")
    return delay, None


def _failures_file(result_file: Path) -> Path:
    return result_file.with_name(result_file.name + ".failures")


def _load_failures(result_file: Path) -> int:
    """Consecutive-failure count persisted next to the result marker, so a supervisor restart keeps the backoff."""
    path = _failures_file(result_file)
    try:
        return min(max(0, int(path.read_text().strip())), MAX_FAILURES)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        print(f"overnight: cannot read failure count {path} ({e}); starting the backoff from 0", flush=True)
        return 0


def _save_failures(result_file: Path, failures: int) -> None:
    path = _failures_file(result_file)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(f"{failures}\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"overnight: cannot persist failure count to {path} ({e}); a restart would reset the backoff", flush=True)


def run_continuous(command: list[str], guard: Guard | None = None, *, result_file: Path, idle_s: float | None = None,
                   sleep=_time.sleep, cycles: int | None = None, poll_s: float = 1.0, stopflag: Path | None = None,
                   pidfile: Path | None = None, quota_s: float | None = None, sweep_request: Path | None = None) -> int:
    """sweep_request: data/sweep-request.json; while it exists, sweep_delay shortens the sleep between cycles."""
    guard = guard or Guard()
    # >= 60 s: a 0 s idle interval is a hot relaunch loop against the scorer and the accounts.
    idle_s = _env_float("CORTADOS_S1_CYCLE_IDLE_S", 900.0, minimum=60) if idle_s is None else idle_s
    # >= 60 s: a 0 s cooldown relaunches straight into the same exhausted usage window, defeating the labeler's headroom floor.
    quota_s = _env_float("CORTADOS_S1_QUOTA_COOLDOWN_S", QUOTA_COOLDOWN_S, minimum=60) if quota_s is None else quota_s
    n = 0
    failures = _load_failures(result_file)
    while cycles is None or n < cycles:
        if stopflag is not None and stopflag.exists():
            print(f"overnight: stop flag {stopflag} present (nightly.sh --stop); not relaunching. Remove it to resume.",
                  flush=True)
            return 0
        if guard.day_stop_expired():
            guard.clear_day_stop()   # the next cycle may train again (train_policy permitting)
        n += 1
        guard.idle_probe()
        rc, result = run_cycle(command, guard, result_file=result_file, poll_s=poll_s, sleep=sleep, pidfile=pidfile,
                               stopflag=stopflag)
        if rc == 130 and (stopflag is None or guard.stop_seen or stopflag.exists()):
            print(f"overnight: cycle {n} ended rc 130 (stop requested); not relaunching", flush=True)
            return rc   # nightly.sh was stopped by request (--stop): do not relaunch
        if result == "stopped_for_day":
            # Not a failure: the guard stopped it on purpose. The next cycle starts at once, labelling-only until the day
            # stop ends (owner 2026-11-22: only training is held).
            print(f"overnight: cycle {n} stopped for the day by the latency guard (rc {rc}); next cycle at once, "
                  "labelling only", flush=True)
            _save_failures(result_file, 0)
            failures = 0
            continue
        if result == "window_end":
            # Not a failure: we stopped it on purpose. No sleep: the next cycle sees the window closed and only labels.
            print(f"overnight: cycle {n} stopped at the night-window end (rc {rc}); next cycle at once", flush=True)
            _save_failures(result_file, 0)
            failures = 0
            continue
        if rc == 130:   # TERM from someone other than --stop (e.g. pkill -f nightly.sh): a hand-run supervisor must survive it
            print(f"overnight: cycle {n} ended 130 without a stop flag (external TERM); treating as a failure", flush=True)
        delay, failures = next_delay(rc, result, idle_s, failures, quota_s)
        _save_failures(result_file, failures)
        # During a day stop no cycle may sweep (the sweep is a gated step), so sweep_delay's "the next cycle only sweeps,
        # no cooldown needed" would relaunch straight into labelling: the delay stands as computed.
        if sweep_request is not None and delay and sweep_request.exists() and not guard.day_stop_active():
            delay, note = sweep_delay(delay, rc, result, guard.wall())
            if note:
                print(f"overnight: {note}", flush=True)
        what = f"failure #{failures}, backing off" if rc else {"worked": "worked", "quota": "quota stop, cooling down"}.get(
            result if isinstance(result, str) else ("worked" if result else "idle"), "idle")
        print(f"overnight: cycle {n} ended rc {rc} ({what}); next cycle in {delay:g}s", flush=True)
        if delay:
            sleep_between_cycles(guard, delay, sleep=sleep)
    return 0


def sleep_between_cycles(guard: Guard, delay: float, *, sleep=_time.sleep) -> None:
    """Sleep `delay` before the next cycle. While the guard's day stop lasts (#116), in steps of at most its reading
    interval with an idle reading each: the state file stays fresh (a day stop that goes silent reads as stale in the live
    view) and the baseline keeps learning the scorer alone. The same steps retry any unresolved stopped group (#116 review
    round 2). The day stop ending mid-sleep is cleared at once; the sleep itself is not shortened (the delay protects the labeler's quota, the failure backoff, or the idle interval)."""
    if not guard.day_stop_active() and not guard.unresolved:
        sleep(delay)
        return
    left = delay
    while left > 0:
        if guard.day_stop_expired():
            guard.clear_day_stop()
        if guard.day_stop_active():
            guard.idle_probe()
        elif guard.unresolved:
            guard.retry_unresolved()   # #116 review round 2: a stopped group not confirmed continued is retried meanwhile
        # >= 1 s: an interval of 0 (tests) must not spin.
        step = min(left, max(1.0, guard.interval))
        sleep(step)
        left -= step


def scorer_plist() -> Path:
    raw = os.environ.get("CORTADOS_S1_SCORER_PLIST")
    return Path(raw) if raw else Path.home() / "Library/LaunchAgents/com.cortados.s1-scorer.plist"


def main(argv: list[str]) -> int:
    if argv[:1] == ["--continuous"]:
        command = argv[2:] if argv[1:2] == ["--"] else argv[1:]
        if not command:
            raise SystemExit("usage: overnight.py --continuous -- command [args...]")
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        data = Path(__file__).parent / "data"
        # #116 review round 1: every group a previous supervisor may have left stopped (its record, the gate's trainer
        # record, the live tree) is continued BEFORE the cycle's root, so the trainer is never left SIGSTOPped.
        recover_stopped_groups(data / STOPPED_GROUPS_NAME, data / TRAINER_GROUP_NAME, data / "nightly.pid", alerter=alert)
        recover_pidfile(data / "nightly.pid", stopped_path=data / STOPPED_GROUPS_NAME, alerter=alert)
        # Same PIDFILE/STOPFLAG paths nightly.sh uses (PIDFILE=data/nightly.pid; STOPFLAG=data/nightly.stop, run in s1/).
        guard = Guard(state_path=data / GUARD_STATE_NAME,   # #116: the live view's S1 block reads this file
                      stopped_path=data / STOPPED_GROUPS_NAME, trainer_file=data / TRAINER_GROUP_NAME)
        guard.load_day_stop()   # a restart (every deploy) keeps a day stop
        guard.load_unresolved()   # #116 review round 2: groups the recovery could not confirm continued, retried
        return run_continuous(command, guard, result_file=data / "cycle-result", stopflag=data / "nightly.stop",
                              pidfile=data / "nightly.pid", sweep_request=data / "sweep-request.json")
    if argv == ["--check-window"]:
        print(f"seconds left in Toronto overnight window: {seconds_left():.0f}")
        return 0
    command = argv[1:] if argv[:1] == ["--"] else argv
    if not command:
        raise SystemExit("usage: overnight.py -- command [args...]")
    budget = seconds_left()
    if not budget:
        return run_bounded(command, 0)
    # Terminating the supervisor (e.g. launchd unload) also stops its own process tree.
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_bounded(command, budget, pidfile=Path(__file__).parent / 'data/nightly.pid')
    finally:
        # The nightly shell normally restores the scorer in its EXIT trap; cover a forced kill. Same path nightly.sh unloads
        # (SCORER_PLIST from CORTADOS_S1_SCORER_PLIST). Window mode only: continuous mode returns above and never runs this,
        # since nightly.sh never unloads the scorer under CORTADOS_S1_CONTINUOUS=1 (gpu_alone keeps it up).
        plist = scorer_plist()
        if plist.exists():
            subprocess.run(["launchctl", "load", str(plist)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20, check=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
