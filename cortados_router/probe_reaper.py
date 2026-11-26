"""Cleanup for the usage probe's PTY-hosted vendor CLIs (claude, codex, cursor-agent, agy).

Owner of one rule: a probe CLI never outlives the probe that opened it. scarcity.tui_capture opens each CLI in a
PTY inside its own folder under the probe directory; this module kills that CLI and every helper it spawned.
Three paths enforce the rule:
  reap        the normal end of a probe (tui_capture's finally);
  atexit      a process that exits while a probe thread is still running. Samplers are daemon threads, and Python
              kills daemon threads at exit without running their finally, which orphaned 47 CLIs by 2026-10-13
              and exhausted the Mac's PTYs (every usage probe then failed with "out of pty devices");
  sweep_stale folders left by a process that was SIGKILLed or crashed, which atexit cannot cover.
Probe folders are judged stale by age, never by the owner pid in their name: macOS reuses pids, and 179
leftover folders on 2026-10-13 carried "live" owner pids that belonged to unrelated processes.
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

# Folder age (seconds) after which no probe can still be using it. The longest legitimate tui_capture is about
# 3 minutes (boot 20 s + trust-prompt boot 20 s + command and retry with run_s up to 25 s each + up to two
# rate-limit nudges of ~10 s), and the sampler stops waiting on a probe after 240 s. Too small: a sweep from a
# second process kills a live probe (EIO, lost reading). Too large: orphans keep their PTYs longer before reclaim.
STALE_PROBE_AGE_S = 600
# Executable names the vendor CLIs and their helpers run under; lsof matches these as name prefixes.
PROBE_COMMANDS = ("claude", "agy", "cursor-agent", "codex", "node")

class Child:
    """One tracked probe child and the single claim on its cleanup (#144 fix round 8). The normal end of a probe and the
    exit-time reaper can run reap() on the same child at once; reap() holds `lock` for its whole run and sets `reaped`
    when done, so exactly one path signals, closes the PTY fd and waits, and the other then does nothing. Signals are
    sent only under the lock while `reaped` is unset, i.e. while the child is still our unreaped child: an unreaped
    (even zombie) child keeps its pid and pgid, so neither can have been reused. A second close of the fd could close
    an unrelated file that reused the number; the claim prevents that too."""
    __slots__ = ("pid", "fd", "my_dir", "probe_dir", "lock", "reaped")

    def __init__(self, pid: int, fd: int, my_dir: Path | None, probe_dir: Path | None):
        self.pid, self.fd, self.my_dir, self.probe_dir = pid, fd, my_dir, probe_dir
        self.lock = threading.Lock()
        self.reaped = False


_active: dict[int, Child] = {}      # child pid -> its claim, while its probe runs
_active_lock = threading.Lock()


def track(pid: int, fd: int, my_dir: Path | None, probe_dir: Path | None) -> Child:
    """Record a running probe so a process exit mid-probe still reaps it. Returns the claim; the probe passes it to
    reap() so both cleanup paths share it even after the entry left _active."""
    child = Child(pid, fd, my_dir, probe_dir)
    with _active_lock:
        _active[pid] = child
    return child


def untrack(pid: int) -> None:
    with _active_lock:
        _active.pop(pid, None)


def _reap_active() -> None:
    """atexit: reap every probe whose thread did not finish before the interpreter began shutting down. Entries stay
    in _active until their reap finishes, so a probe thread finishing at the same time finds the same claim."""
    with _active_lock:
        pending = list(_active.values())
    for child in pending:
        reap(child.pid, child.fd, child.probe_dir, child.my_dir, claim=child)


atexit.register(_reap_active)


def descendants(root: int) -> list[int]:
    """All descendants of root, from one `ps` snapshot (the CLIs spawn helpers that setsid themselves, so a process-group kill misses them)."""
    kids: dict[int, list[int]] = {}
    try:
        for line in subprocess.run(["ps", "-Ao", "pid,ppid"], capture_output=True, text=True, timeout=10).stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    except Exception:
        return []
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        for k in kids.get(p, []):
            out.append(k); stack.append(k)
    return out


def probe_cwd_pids(probe_dir: Path, only_dir: Path | None = None) -> list[int]:
    """Any CLI helper still living in the probe folder (orphans re-parented to launchd); with only_dir, just that probe's own folder."""
    argv = ["lsof", "-a", "-d", "cwd"]
    for name in PROBE_COMMANDS:
        argv += ["-c", name]
    try:
        r = subprocess.run([*argv, "-Fpn"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    pids, cur = [], None
    for line in r.splitlines():
        if line.startswith("p"):
            cur = int(line[1:])
        elif line.startswith("n") and cur and _inside(line[1:], only_dir or probe_dir):
            pids.append(cur)
    return pids


# Absolute paths (macOS's own): the probe's checks never depend on the caller's PATH, which a sampler may have narrowed.
LSOF = "/usr/sbin/lsof"
PS = "/bin/ps"


def cwd_pids_in(folder: Path) -> list[int]:
    """Every process (any command) whose cwd is `folder` or inside it, as lsof lists them. Raises when lsof could not run
    (the caller must not read a failed listing as "nobody there"). Read-only: the agy probe skips its sample on a hit and
    never kills what it finds (#144 fix round 6). As a non-root user lsof lists the user's own processes; another user
    cannot enter the 0700 folder anyway."""
    r = subprocess.run([LSOF, "-a", "-d", "cwd", "-Fpn"], capture_output=True, text=True, timeout=20)
    if r.returncode not in (0, 1):            # 1: nothing matched (or some processes not readable); output still valid
        raise RuntimeError(f"lsof exited {r.returncode}")
    pids, cur = [], None
    for line in r.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            cur = int(line[1:])
        elif line.startswith("n") and cur and _inside(line[1:], folder):
            pids.append(cur)
    return sorted(set(pids))


def process_identity(pid: int) -> tuple[int, str] | None:
    """(uid, start time) of a live process from ps, or None when no such process. The start time is ps's `lstart` (to the
    second): with the pid and uid it tells the probe's own child from an unrelated process that reused its pid. Raises
    when ps could not run."""
    r = subprocess.run([PS, "-o", "uid=,lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10,
                       env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"})
    line = r.stdout.strip()
    if r.returncode != 0 or not line:
        if r.returncode in (0, 1) and not line:
            return None
        raise RuntimeError(f"ps exited {r.returncode}")
    uid, _, start = line.partition(" ")
    return int(uid), " ".join(start.split())


def _inside(path: str, folder: Path) -> bool:
    """lsof prints resolved paths (/private/var/… for /var/…), so compare against the resolved folder."""
    folder_s = os.path.realpath(folder)
    return path == folder_s or path.startswith(folder_s + os.sep)


def reap(pid: int, fd: int, probe_dir: Path | None, my_dir: Path | None = None, claim: Child | None = None) -> None:
    """Kill the probe CLI and EVERY descendant, then anything left with cwd in the probe folder. 2026-09-26: the old killpg
    left 437 orphaned claude/agy/cursor/codex helpers (load 63, gateway liveness kills). Order matters on macOS: close the
    PTY master before waiting.
    probe_dir None (the agy probe's shared fixed folder, #144 fix rounds 6-7): only the spawned child and its own process
    group (pty.fork made it a session leader, so its pgid is its pid) are signalled, while the child is still unreaped,
    so neither id can have been reused. No descendant scan (a scanned pid can be reused before the kill) and no kill by
    cwd. A helper that left the group and stays in the folder makes the next sample skip `probe_folder_busy`.
    claim: the Child that track() returned; without it the tracked entry for pid is used, or a fresh one for an
    untracked child. Whichever caller takes the claim first does the whole cleanup; a later one returns at once."""
    if claim is None:
        with _active_lock:
            claim = _active.get(pid)
        if claim is None or claim.fd != fd:
            claim = Child(pid, fd, my_dir, probe_dir)
    with claim.lock:
        if claim.reaped:
            return
        try:
            _reap_claimed(pid, fd, probe_dir, my_dir)
        finally:
            claim.reaped = True
            with _active_lock:
                if _active.get(pid) is claim:
                    del _active[pid]


def _reap_claimed(pid: int, fd: int, probe_dir: Path | None, my_dir: Path | None) -> None:
    """reap()'s work, run only by the holder of the child's claim."""
    tree = descendants(pid) if probe_dir is not None else []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in [pid, *tree]:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        if sig == signal.SIGTERM:
            time.sleep(1.5)
    try:
        os.close(fd)
    except OSError:
        pass
    for _ in range(30):
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
        except ChildProcessError:
            break
        time.sleep(0.1)
    if probe_dir is None:                          # a shared fixed folder (the agy probe): never kill by cwd (#144 round 6)
        return
    for p in probe_cwd_pids(probe_dir, my_dir):     # this probe's own leftovers only (2026-09-28: a shared folder let concurrent probes kill each other → EIO)
        if p != os.getpid():
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def sweep_stale(probe_dir: Path, max_age_s: float = STALE_PROBE_AGE_S, now: float | None = None) -> dict[str, int]:
    """Kill CLIs left in probe folders older than max_age_s and remove those folders. Returns counts for the caller to log.
    Called under the scarcity sampler's cross-process lock, so no other sampler is starting probes meanwhile."""
    now = time.time() if now is None else now
    with _active_lock:
        mine = {c.my_dir for c in _active.values() if c.my_dir is not None}
    stale = []
    for folder in probe_dir.glob("p*"):
        try:
            if folder.is_dir() and folder not in mine and now - folder.stat().st_mtime > max_age_s:
                stale.append(folder)
        except OSError:
            continue
    killed = 0
    for folder in stale:
        for p in probe_cwd_pids(probe_dir, folder):
            if p == os.getpid():
                continue
            try:
                os.kill(p, signal.SIGKILL)      # orphaned vendor CLIs ignore SIGTERM (observed 2026-10-13: 47 of 47)
                killed += 1
            except OSError:
                pass
    removed = 0
    for folder in stale:
        try:
            folder.rmdir()
            removed += 1
        except OSError:
            pass                                # not empty yet (a killed CLI still exiting); the next sweep retries
    return {"stale": len(stale), "killed": killed, "removed": removed}
