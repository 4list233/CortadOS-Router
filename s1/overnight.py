"""Bound the existing nightly process tree to 01:30–07:00 America/Toronto."""
from __future__ import annotations

from datetime import datetime, time
import os
from pathlib import Path
import signal
import subprocess
import sys
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


def main(argv: list[str]) -> int:
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
        # The nightly shell normally restores the scorer in its EXIT trap; cover a forced kill.
        plist = Path.home() / "Library/LaunchAgents/com.cortados.s1-scorer.plist"
        if plist.exists():
            subprocess.run(["launchctl", "load", str(plist)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20, check=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
