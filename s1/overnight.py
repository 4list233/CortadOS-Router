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
import re
import statistics
import sys
import time as _time
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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        # No silent fallback: a typo'd guard setting is reported, then the documented default applies.
        print(f"overnight: {name}={raw!r} is not a number; using {default}", flush=True)
        return default


def probe_scorer_latency(url: str | None = None, timeout: float = 30.0) -> float | None:
    """Wall seconds for one fixed short /score request; None when the scorer cannot be reached (logged)."""
    url = url or f"http://127.0.0.1:{os.environ.get('CORTADOS_S1_PORT', '8431')}/score"
    body = json.dumps({"request": "list the files in this directory", "unit_kind": "background"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = _time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except OSError as e:
        print(f"overnight: scorer probe failed ({e}); latency unknown this probe", flush=True)
        return None
    return _time.monotonic() - t0


def free_memory_gb() -> float | None:
    """free+inactive+speculative pages from vm_stat, in GB; None (logged) when vm_stat is unavailable or unparseable."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"overnight: vm_stat failed ({e}); memory guard has no reading", flush=True)
        return None
    m = re.search(r"page size of (\d+) bytes", out)
    pages = {k: int(v) for k, v in re.findall(r"^Pages (free|inactive|speculative):\s+(\d+)\.", out, re.M)}
    if not m or len(pages) != 3:
        print("overnight: vm_stat output not understood; memory guard has no reading", flush=True)
        return None
    return sum(pages.values()) * int(m.group(1)) / 1e9


class Guard:
    """Pauses/resumes one process group on scorer latency and free memory. Sources, clock and signaller are injectable."""

    # Hysteresis: resume only after this many consecutive in-budget probes, so one lucky fast probe while the GPU is still
    # contended does not flap the trainer between STOP and CONT every 30 s.
    RESUME_AFTER = 3
    BASELINE_SAMPLES = 5

    def __init__(self, *, latency=probe_scorer_latency, memory=free_memory_gb, clock=_time.monotonic, signaller=None,
                 budget_s: float | None = None, min_free_gb: float | None = None, interval_s: float | None = None,
                 log=lambda m: print(m, flush=True)):
        self.latency, self.memory, self.clock, self.log = latency, memory, clock, log
        self.signaller = signaller or signal_group
        self.budget = _env_float("CORTADOS_S1_LATENCY_BUDGET_S", 1.0) if budget_s is None else budget_s
        self.min_free = _env_float("CORTADOS_S1_MIN_FREE_GB", 8.0) if min_free_gb is None else min_free_gb
        self.interval = _env_float("CORTADOS_S1_PROBE_S", 30.0) if interval_s is None else interval_s
        self.baseline_samples: list[float] = []
        self.paused = False
        self.good = 0
        self.last = None

    @property
    def baseline(self) -> float | None:
        return statistics.median(self.baseline_samples) if self.baseline_samples else None

    def _record_baseline(self, lat: float) -> None:
        self.baseline_samples = (self.baseline_samples + [lat])[-self.BASELINE_SAMPLES:]

    def idle_probe(self) -> None:
        """No work tree running: every probe is a clean baseline sample."""
        if self.last is not None and self.clock() - self.last < self.interval:
            return
        self.last = self.clock()
        lat = self.latency()
        if lat is not None:
            self._record_baseline(lat)

    def tick(self, pgid: int) -> None:
        """One guard step for the running work group (rate-limited to one probe per interval)."""
        if self.last is not None and self.clock() - self.last < self.interval:
            return
        self.last = self.clock()
        lat, mem = self.latency(), self.memory()
        if self.paused and lat is not None:
            self._record_baseline(lat)   # the tree is stopped: this probe is the scorer alone
        reasons = []
        base = self.baseline
        if lat is not None and base is not None and lat > base + self.budget:
            reasons.append(f"scorer latency {lat:.2f}s > baseline {base:.2f}s + budget {self.budget:g}s (Forest 2026-11-03)")
        if mem is not None and mem < self.min_free:
            reasons.append(f"free memory {mem:.1f} GB < {self.min_free:g} GB")
        if reasons:
            self.good = 0
            if not self.paused:
                self.signaller(pgid, signal.SIGSTOP)
                self.paused = True
                self.log("overnight: PAUSE work tree: " + "; ".join(reasons))
            return
        if lat is None and base is None and not self.paused:
            # Without a baseline the latency rule cannot be judged: pause once to measure the scorer alone.
            self.signaller(pgid, signal.SIGSTOP)
            self.paused = True
            self.log("overnight: PAUSE work tree: no scorer latency baseline yet; measuring it with the tree stopped")
            return
        if self.paused:
            if lat is None:
                self.good = 0   # an unreachable scorer is not evidence the budget is met
                return
            self.good += 1
            if self.good >= self.RESUME_AFTER:
                self.signaller(pgid, signal.SIGCONT)
                self.paused = False
                self.good = 0
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


def run_cycle(command: list[str], guard: Guard, *, result_file: Path, poll_s: float = 1.0, grace: float = 20,
              sleep=_time.sleep) -> tuple[int, bool]:
    """Run one nightly.sh cycle under the guard. Returns (rc, worked)."""
    try:
        result_file.unlink()
    except FileNotFoundError:
        pass
    env = dict(os.environ, CORTADOS_NIGHTLY_SUPERVISED="1", TZ="America/Toronto", CORTADOS_S1_CONTINUOUS="1",
               CORTADOS_S1_CYCLE_RESULT=str(result_file))
    child = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        while child.poll() is None:
            guard.tick(child.pid)
            sleep(poll_s)
    except BaseException:
        stop_tree(child, grace=grace)
        raise
    guard.paused = False
    guard.good = 0
    worked = result_file.exists() and result_file.read_text().strip() == "worked"
    return child.returncode, worked


# Failure backoff cap. Exit 3 (Claude quota headroom low/unknown) recovers on the accounts' ~5 h usage window, and other failures
# (provider down, base model missing, eval crash) need a human or a slow external recovery: retrying faster than every 4 h only
# burns quota and fills logs, while 4 h still gives several attempts per day once the cause clears.
BACKOFF_CAP_S = 4 * 3600.0


def next_delay(rc: int, worked: bool, idle_s: float, failures: int) -> tuple[float, int]:
    """(seconds to sleep before the next cycle, updated consecutive-failure count) for a finished cycle."""
    if rc != 0:
        failures += 1
        return min(idle_s * 2 ** (failures - 1), BACKOFF_CAP_S), failures
    return (0.0 if worked else idle_s), 0


def run_continuous(command: list[str], guard: Guard | None = None, *, result_file: Path, idle_s: float | None = None,
                   sleep=_time.sleep, cycles: int | None = None, poll_s: float = 1.0) -> int:
    guard = guard or Guard()
    idle_s = _env_float("CORTADOS_S1_CYCLE_IDLE_S", 900.0) if idle_s is None else idle_s
    n = failures = 0
    while cycles is None or n < cycles:
        n += 1
        guard.idle_probe()
        rc, worked = run_cycle(command, guard, result_file=result_file, poll_s=poll_s, sleep=sleep)
        if rc == 130:
            print(f"overnight: cycle {n} ended rc 130 (stop requested); not relaunching", flush=True)
            return rc   # nightly.sh was stopped by request (--stop): do not relaunch
        delay, failures = next_delay(rc, worked, idle_s, failures)
        what = f"failure #{failures}, backing off" if rc else ("worked" if worked else "idle")
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
        return run_continuous(command, result_file=Path(__file__).parent / "data/cycle-result")
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
