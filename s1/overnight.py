"""Bound the existing nightly process tree to 01:30–07:00 America/Toronto (default, window mode), or run it continuously.

Continuous mode (Forest 2026-11-03): `overnight.py --continuous -- /bin/bash <repo>/s1/nightly.sh` from a launchd KeepAlive job.
No window, no deadline: one cycle = one nightly.sh run. Only a cycle that exits 0 AND wrote "worked" to $CORTADOS_S1_CYCLE_RESULT
(labels added, training ran, an evaluation/promotion attempted) starts the next cycle at once; any other rc-0 exit is idle and sleeps
CORTADOS_S1_CYCLE_IDLE_S (default 900); a failure (rc 1/2/3/...) backs off exponentially from that interval, capped at 4 h; rc 130 ends. The live scorer stays up (nightly.sh skips
gpu_alone under CORTADOS_S1_CONTINUOUS=1); a guard SIGSTOPs the work process group while
  - scorer latency > baseline + CORTADOS_S1_LATENCY_BUDGET_S (default 1.0 s, Forest 2026-11-03), or
  - free memory (vm_stat free+inactive+speculative) < CORTADOS_S1_MIN_FREE_GB (default 8),
and SIGCONTs it after RESUME_AFTER consecutive good probes. Latency = wall time of a fixed short POST /score probe (serve.py does
not log per-request latency), at most every CORTADOS_S1_PROBE_S (default 30) s; the baseline is the median of probes taken while
the tree is paused or no cycle is running."""
from __future__ import annotations

from datetime import datetime, time
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
import urllib.error
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


_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]*")


def _redact_urls(text: str) -> str:
    return _URL_RE.sub("<url>", text)


def scorer_url() -> str:
    """The /score URL through the router's resolver (CORTADOS_S1_URL -> endpoint name -> endpoints default -> loopback)."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from cortados_router import s1_endpoint
    try:
        return s1_endpoint.resolve_url().rstrip("/") + "/score"
    except ValueError as e:   # the resolver's message can quote the URL: redact it and name the setting instead
        raise ScorerConfigError(f"scorer URL from {scorer_url_source()} does not resolve ({_redact_urls(str(e))})") from e


def probe_scorer_latency(url: str | None = None, timeout: float = 30.0) -> float | None:
    """Wall seconds for one fixed short /score request; None when the scorer cannot be reached (logged).
    Raises ScorerRefused on 5xx and ScorerConfigError on 401/403/404 and other 4xx."""
    url = url or scorer_url()
    body = json.dumps({"request": "list the files in this directory", "unit_kind": "background"}).encode()
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("CORTADOS_S1_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)
    t0 = _time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:   # before OSError: HTTPError is an OSError subclass
        if e.code >= 500:
            raise ScorerRefused(f"scorer refused (HTTP {e.code})") from None
        raise ScorerConfigError(f"scorer probe got HTTP {e.code} from the URL set by {scorer_url_source()}: "
                                "check CORTADOS_S1_URL/ENDPOINT/TOKEN") from None
    except OSError as e:
        print(f"overnight: scorer probe failed ({_redact_urls(str(e))}); latency unknown this probe", flush=True)
        return None
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
                 alerter=None):
        self.latency, self.memory, self.clock, self.log = latency, memory, clock, log
        self.signaller = signaller or signal_group
        self.alerter = alerter or (lambda name, detail: alert(name, detail, log=self.log))
        self.budget = _env_float("CORTADOS_S1_LATENCY_BUDGET_S", 1.0, minimum=0) if budget_s is None else budget_s
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
            if not self.config_logged:
                self.config_logged = True
                self.log(f"overnight: CONFIG ERROR: {e}; the latency guard has no reading (not treated as back-pressure)")
                self.alerter("scorer_probe_config", str(e))
            return None, None

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
        if lat is not None and base is not None and lat > base + self.budget:
            reasons.append(f"scorer latency {lat:.2f}s > baseline {base:.2f}s + budget {self.budget:g}s (Forest 2026-11-03)")
        if mem is not None and mem < self.min_free:
            reasons.append(f"free memory {mem:.1f} GB < {self.min_free:g} GB")
        if reasons:
            self.good = 0
            if not self.paused:
                self._pause(pgid, "; ".join(reasons))
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
                self.log(f"overnight: RESUME work tree: latency {lat:.2f}s within budget for {self.RESUME_AFTER} probes"
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
              sleep=_time.sleep, pidfile: Path | None = None) -> tuple[int, str]:
    """Run one nightly.sh cycle under the guard. Returns (rc, result) with result 'worked', 'quota' or 'idle'."""
    stale = False
    try:
        result_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:   # a stale marker we cannot remove would be misread as this cycle's: the cycle is read as idle
        stale = True
        print(f"overnight: WARNING: cannot remove stale cycle result {result_file} ({e}); this cycle will be treated as idle",
              flush=True)
    env = dict(os.environ, CORTADOS_NIGHTLY_SUPERVISED="1", TZ="America/Toronto", CORTADOS_S1_CONTINUOUS="1",
               CORTADOS_S1_CYCLE_RESULT=str(result_file))
    guard.new_cycle()
    child = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        while child.poll() is None:
            guard.tick(child.pid)
            sleep(poll_s)
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
    if stale:
        print(f"overnight: WARNING: cycle result {result_file} may be the previous cycle's; treating this cycle as idle",
              flush=True)
        return child.returncode, "idle"
    return child.returncode, _read_result(result_file)


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
        rc, result = run_cycle(command, guard, result_file=result_file, poll_s=poll_s, sleep=sleep, pidfile=pidfile)
        if rc == 130:
            print(f"overnight: cycle {n} ended rc 130 (stop requested); not relaunching", flush=True)
            return rc   # nightly.sh was stopped by request (--stop): do not relaunch
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
