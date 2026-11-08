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
and SIGCONTs it after RESUME_AFTER consecutive good probes. Latency = wall time of a fixed short POST /score probe (serve.py does
not log per-request latency), at most every CORTADOS_S1_PROBE_S (default 30) s; the baseline is the median of probes taken while
the tree is paused or no cycle is running.

Training only in the night window (Forest 2026-11-08): CORTADOS_S1_TRAIN_ONLY_IN_WINDOW (default "1"; "0" = train any time).
Labeling and the dataset refresh run all day; the Studio-heavy gated steps (LoRA, MLX preflight, Laya, evaluation) run only
inside CORTADOS_S1_NIGHT_WINDOW, so the Studio's local Ollama model is free during the day. Each cycle gets
CORTADOS_S1_TRAIN_ALLOWED=1/0 (+ CORTADOS_S1_TRAIN_SKIP_REASON); nightly.sh skips every gated step when it is 0, ends rc 0, and
the cycle is idle (sleeps CORTADOS_S1_CYCLE_IDLE_S) unless labels were added. An invalid window, or "off", means no training
(fail closed). A cycle allowed to train is stopped when the window ends (group SIGTERM, TRAIN_STOP_GRACE_S, then SIGKILL); the
trainer's checkpoints stay, and the next cycle starts at once (it labels, then skips training until the next window)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
import os
from pathlib import Path
import signal
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


def run_bounded(command: list[str], seconds: float, *, grace: float = 20, pidfile: Path | None = None) -> int:
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
            signal_group(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                signal_group(child.pid, signal.SIGKILL)
                child.wait()
            # The shell can exit before its grandchildren; never leave the trainer behind.
            signal_group(child.pid, signal.SIGKILL)
            return 124
    except BaseException:
        signal_group(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            signal_group(child.pid, signal.SIGKILL)
            child.wait()
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


def in_night_window(window: tuple[time, time] | None, now: datetime) -> bool:
    """True when the local wall time of `now` is in [start, end); handles windows crossing midnight."""
    if window is None:
        return False
    start, end = window
    t = now.time().replace(tzinfo=None)
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


def _next_start(window: tuple[time, time], now: datetime) -> datetime:
    """The next local wall time at which `window` starts (today or tomorrow)."""
    start = now.replace(hour=window[0].hour, minute=window[0].minute, second=0, microsecond=0)
    return start if start > now else start + timedelta(days=1)


def _window_end(window: tuple[time, time], now: datetime) -> datetime:
    """The end of the window `now` is inside (assumes in_night_window(window, now))."""
    end = now.replace(hour=window[1].hour, minute=window[1].minute, second=0, microsecond=0)
    return end if end > now else end + timedelta(days=1)


def train_policy(now: datetime, *, window_raw: str | None = None, switch_raw: str | None = None
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
    if in_night_window(window, now):
        return True, f"inside night window {s:%H:%M}-{e:%H:%M}: training allowed", _window_end(window, now)
    return False, (f"training skipped: outside night window {s:%H:%M}-{e:%H:%M}; "
                   f"next window starts {_next_start(window, now):%Y-%m-%d %H:%M %Z}"), None


# Grace between the window-end SIGTERM and SIGKILL. Longer than studio_capacity's TRAINING_STOP_GRACE_SECONDS (30 s), so the gate
# can TERM/KILL its own training group and nightly.sh's EXIT trap can run before this supervisor escalates to KILL.
TRAIN_STOP_GRACE_S = 60.0


def _local_now() -> datetime:
    return datetime.now().astimezone()


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


def probe_scorer_latency(url: str | None = None, timeout: float = 30.0) -> float | None:
    """Wall seconds for one fixed short /score request; None when the scorer cannot be reached (logged).
    Raises ScorerRefused on 5xx and ScorerConfigError on 401/403/404 and other 4xx."""
    url = url or scorer_url()
    body = json.dumps({"request": "list the files in this directory", "unit_kind": "background"}).encode()
    headers = {"Content-Type": "application/json"}
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
        print(f"overnight: scorer probe failed ({_probe_failure(e)}); latency unknown this probe", flush=True)
        return None
    except ValueError as e:   # Request()/urlsplit rejected it, e.g. a missing IPv6 bracket; the message can quote the value
        raise ScorerConfigError(f"scorer URL from {scorer_url_source()} is malformed ({type(e).__name__}); "
                                f"{RESOLVE_HINT}") from None
    return _time.monotonic() - t0


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


class Guard:
    """Pauses/resumes one process group on scorer latency and free memory. Sources, clock and signaller are injectable."""

    # Hysteresis: resume only after this many consecutive in-budget probes, so one lucky fast probe while the GPU is still
    # contended does not flap the trainer between STOP and CONT every 30 s.
    RESUME_AFTER = 3
    BASELINE_SAMPLES = 5
    # While paused, this many consecutive unreadable probes (scorer down/unreachable/misconfigured) release the tree: a probe
    # that cannot read anything is no evidence of contention, and holding the trainer on it would stall the cycle forever.
    MAX_UNREADABLE = 10

    def __init__(self, *, latency=probe_scorer_latency, memory=free_memory_gb, clock=_time.monotonic, signaller=None,
                 budget_s: float | None = None, min_free_gb: float | None = None, interval_s: float | None = None,
                 max_pause_s: float | None = None, release_hold_s: float | None = None, log=lambda m: print(m, flush=True),
                 alerter=None, wall=_local_now, night_window="env", night_budget_s: float | None = None):
        self.latency, self.memory, self.clock, self.log = latency, memory, clock, log
        self.signaller = signaller or signal_group
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
        self.baseline_samples: list[float] = []
        self.last = None
        self.new_cycle()

    def new_cycle(self) -> None:
        """Forget per-cycle state (pause, hysteresis, once-per-cycle config log, forced-release blindness)."""
        self.paused = False
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

    def _record_baseline(self, lat: float) -> None:
        self.baseline_samples = (self.baseline_samples + [lat])[-self.BASELINE_SAMPLES:]

    def _read_latency(self):
        """(latency or None, refusal reason or None). Config errors are logged once per cycle, alerted, and read as None."""
        try:
            return self.latency(), None
        except ScorerRefused as e:
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
        lat, _ = self._read_latency()
        if lat is not None:
            self._record_baseline(lat)

    def _pause(self, pgid: int, why: str) -> None:
        self.signaller(pgid, signal.SIGSTOP)
        self.paused = True
        self.paused_at = self.clock()
        self.unreadable = 0
        self.log("overnight: PAUSE work tree: " + why)

    def _release(self, pgid: int, why: str) -> None:
        self.signaller(pgid, signal.SIGCONT)
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

    def tick(self, pgid: int) -> None:
        """One guard step for the running work group (rate-limited to one probe per interval)."""
        if self.last is not None and self.clock() - self.last < self.interval:
            return
        self.last = self.clock()
        (lat, refused), mem = self._read_latency(), self.memory()
        if lat is not None:
            self.blind = False
        if self.paused:
            if lat is not None:
                self._record_baseline(lat)   # the tree is stopped: this probe is the scorer alone
            self.unreadable = 0 if (lat is not None or refused) else self.unreadable + 1
            held = self.clock() - self.paused_at
            if held >= self.max_pause:
                return self._release(pgid, f"paused {held:.0f}s >= CORTADOS_S1_MAX_PAUSE_S {self.max_pause:g}s")
            if self.unreadable >= self.MAX_UNREADABLE:
                return self._release(pgid, f"{self.unreadable} consecutive unreadable scorer probes while paused")
        elif self.hold_until is not None:
            if self.clock() < self.hold_until:
                return   # release hold-off: the tree runs at least as long as it was held, whatever the probes say
            self.hold_until = None
        reasons = [refused] if refused else []
        base = self.baseline
        budget, budget_label = self.current_budget()
        if lat is not None and base is not None and lat > base + budget:
            reasons.append(f"scorer latency {lat:.2f}s > baseline {base:.2f}s + {budget_label}")
        if mem is not None and mem < self.min_free:
            reasons.append(f"free memory {mem:.1f} GB < {self.min_free:g} GB")
        if reasons:
            self.good = 0
            if not self.paused:
                self._pause(pgid, "; ".join(reasons) + f" [latency budget in force: {budget_label}]")
            return
        if lat is None and base is None and not self.paused and not self.blind:
            # Without a baseline the latency rule cannot be judged: pause once to measure the scorer alone.
            self._pause(pgid, "no scorer latency baseline yet; measuring it with the tree stopped")
            return
        if self.paused:
            if lat is None:
                self.good = 0   # an unreachable scorer is not evidence the budget is met
                return
            self.good += 1
            if self.good >= self.RESUME_AFTER:
                self.signaller(pgid, signal.SIGCONT)
                self.paused = False
                self.paused_at = None
                self.good = 0
                self.forced_releases = 0
                self.log(f"overnight: RESUME work tree: latency {lat:.2f}s within {budget_label} for {self.RESUME_AFTER} probes"
                         + (f", free memory {mem:.1f} GB" if mem is not None else ""))


def stop_tree(child, *, grace: float = 20) -> None:
    """SIGCONT first (a stopped process cannot handle TERM), then TERM so nightly.sh/the trainer save checkpoints, then KILL."""
    signal_group(child.pid, signal.SIGCONT)
    signal_group(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        signal_group(child.pid, signal.SIGKILL)
        child.wait()
    signal_group(child.pid, signal.SIGKILL)   # grandchildren can outlive the shell


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


def run_cycle(command: list[str], guard: Guard, *, result_file: Path, poll_s: float = 1.0, grace: float = 20,
              sleep=_time.sleep, pidfile: Path | None = None, stopflag: Path | None = None,
              grace_end: float = TRAIN_STOP_GRACE_S) -> tuple[int, str]:
    """Run one nightly.sh cycle under the guard. Returns (rc, result) with result 'worked', 'quota' or 'idle'.
    guard.stop_seen is set when stopflag existed at any poll of this cycle (and after the child exits), so an operator who
    removes it during the TERM grace (to resume) is still read as a stop, not as a failure. Residual race: a flag created
    and removed entirely inside one poll_s window is never seen, and that rc 130 is read as an external TERM."""
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
    print(f"overnight: {why}", flush=True)
    env = dict(os.environ, CORTADOS_NIGHTLY_SUPERVISED="1", TZ="America/Toronto", CORTADOS_S1_CONTINUOUS="1",
               CORTADOS_S1_CYCLE_RESULT=str(result_file), CORTADOS_S1_TRAIN_ALLOWED="1" if allowed else "0",
               CORTADOS_S1_TRAIN_SKIP_REASON="" if allowed else why)
    guard.new_cycle()
    child = subprocess.Popen(command, env=env, start_new_session=True)
    window_ended = False
    try:
        while child.poll() is None:
            watch_stop()
            if deadline is not None and guard.wall() >= deadline:
                # The deadline lives here, not in nightly.sh: this supervisor already owns the window and group signalling, and
                # the gate (studio_capacity) forwards our TERM to its own training group. The whole cycle ends; the next one
                # starts at once, labels, and skips training until the next window. mlx_lm checkpoints (save-every 100) stay.
                print(f"overnight: night window ended at {deadline:%H:%M}; stopping this cycle's training "
                      f"(TERM, {grace_end:g}s grace, then KILL); checkpoints are kept", flush=True)
                window_ended = True
                stop_tree(child, grace=grace_end)
                break
            guard.tick(child.pid)
            sleep(poll_s)
        watch_stop()
    except BaseException:
        stop_tree(child, grace=grace)
        raise
    finally:
        # Never leave a group stopped: the shell can exit while a paused grandchild (the trainer) is still SIGSTOPped.
        guard.signaller(child.pid, signal.SIGCONT)
        guard.new_cycle()
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
    result = _read_result(result_file)
    if stale and result == "worked":
        # A stale 'worked' would relaunch at once forever: degrade it to idle. 'quota' is honoured even if stale, because
        # over-cooling is safe and dropping a real quota stop would relaunch into the exhausted window every idle interval.
        print(f"overnight: WARNING: cycle result {result_file} may be the previous cycle's; treating this cycle as idle",
              flush=True)
        return child.returncode, "idle"
    return child.returncode, result


def recover_pidfile(pidfile: Path, *, ps=None, signaller=None, alive=None, log=lambda m: print(m, flush=True)) -> None:
    """Continuous startup: a pidfile left by a supervisor that died mid-pause can name a live, SIGSTOPped nightly.sh, which
    makes every new cycle exit 'already running' forever. SIGCONT such a group; drop a pidfile whose process is gone."""
    signaller = signaller or signal_group
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
    if not alive(pid):
        log(f"overnight: stale pidfile {pidfile} (pid {pid} is gone); removing it")
        pidfile.unlink(missing_ok=True)
        return
    try:
        stat = ps(pid)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"overnight: ps for pid {pid} failed ({e}); cannot tell whether it is stopped")
        return
    if stat.startswith("T"):
        signaller(pid, signal.SIGCONT)
        log(f"overnight: pid {pid} from {pidfile} was left stopped (state {stat}); sent SIGCONT to its group so it can finish")
    else:
        log(f"overnight: pid {pid} from {pidfile} is running (state {stat}); nightly.sh will refuse a second run until it ends")


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
                   pidfile: Path | None = None, quota_s: float | None = None) -> int:
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
        n += 1
        guard.idle_probe()
        rc, result = run_cycle(command, guard, result_file=result_file, poll_s=poll_s, sleep=sleep, pidfile=pidfile,
                               stopflag=stopflag)
        if rc == 130 and (stopflag is None or guard.stop_seen or stopflag.exists()):
            print(f"overnight: cycle {n} ended rc 130 (stop requested); not relaunching", flush=True)
            return rc   # nightly.sh was stopped by request (--stop): do not relaunch
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
        what = f"failure #{failures}, backing off" if rc else {"worked": "worked", "quota": "quota stop, cooling down"}.get(
            result if isinstance(result, str) else ("worked" if result else "idle"), "idle")
        print(f"overnight: cycle {n} ended rc {rc} ({what}); next cycle in {delay:g}s", flush=True)
        if delay:
            sleep(delay)
    return 0


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
        recover_pidfile(data / "nightly.pid")
        # Same PIDFILE/STOPFLAG paths nightly.sh uses (PIDFILE=data/nightly.pid; STOPFLAG=data/nightly.stop, run in s1/).
        return run_continuous(command, result_file=data / "cycle-result", stopflag=data / "nightly.stop",
                              pidfile=data / "nightly.pid")
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
