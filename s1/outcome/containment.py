"""OS containment for code the loop does not trust: generated checks, the fixture code they run, and git on a repo the
agent could rewrite.

macOS Seatbelt (`sandbox-exec`) with a default-deny profile: processes may start and fork, signal only their own
sandbox, read files, and write only inside the workspace; no network, no Mach services beyond the directory lookup
libc needs, so no LaunchServices, Apple Events or other host services that could start something unsandboxed (Codex
review 2026-10-24: an allow-default profile left those open). Verified 2026-10-24 on the M5 Max: python -m unittest,
git init/add/commit/diff, node and writes inside work; writes outside or to the home folder, a TCP connection,
`open -a`, Apple Events to Finder and System Events, and signalling an outside process are all refused.
"""
from __future__ import annotations

import os
import signal
import subprocess

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# After the group is killed, the remaining output is already buffered; ten seconds means a pipe holder escaped the
# sweep (it left the workspace), and waiting longer would only hold the worker.
DRAIN_S = 10


def sweep(workspace: str) -> int:
    """Kill every process still holding the workspace open (cwd or a file): what a child that left the process group
    (setsid) would otherwise keep running after the workspace is removed. Returns how many were killed."""
    try:
        out = subprocess.run(["/usr/sbin/lsof", "-t", "+D", os.path.realpath(workspace)], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    killed = 0
    for pid in {int(p) for p in out.split() if p.isdigit()} - {os.getpid()}:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    return killed


# Every contained process carries this env var set to a per-run token. A descendant inherits it and cannot change it,
# so a kill-by-token scan finds a child even after it leaves the process group (setsid), is reparented, changes its
# working folder and closes its descriptors — the cases killpg and the workspace sweep miss (Codex reviews 2026-10-24).
RUN_TOKEN_VAR = "CORTADOS_OUTCOME_RUN"


def _kill_by_token(token: str) -> int:
    """Kill every process whose environment carries RUN_TOKEN_VAR=token. `ps -axEww` prints each process's env."""
    try:
        out = subprocess.run(["/bin/ps", "-axEww", "-o", "pid=,command="], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    marker = f"{RUN_TOKEN_VAR}={token}"
    killed = 0
    for line in out.splitlines():
        pid = line.split(None, 1)[0] if line.split() else ""
        if marker in line and pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    return killed


def run_group(argv: list[str], *, cwd: str, env: dict, timeout: float, stdin: str | None = None):
    """Run in a new process group; on every exit path (success, failure, timeout) kill every process carrying this
    run's token, then the group, then sweep the workspace, so no fixture descendant outlives its run (Codex reviews
    2026-10-24). Returns (exit or None, out, err)."""
    token = os.urandom(16).hex()
    env = dict(env, **{RUN_TOKEN_VAR: token})
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    code = None
    try:
        out, err = p.communicate(stdin, timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, err = "", "timeout"
    finally:
        _kill_by_token(token)                             # primary: catches detached, reparented, moved descendants
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        sweep(cwd)                                        # secondary: anything still holding the workspace open
        if code is None:
            try:
                more_out, more_err = p.communicate(timeout=DRAIN_S)
                out, err = (out or "") + (more_out or ""), (err or "") + (more_err or "")
            except subprocess.TimeoutExpired:
                for pipe in (p.stdin, p.stdout, p.stderr):
                    if pipe:
                        pipe.close()
                p.wait()
    return code, out or "", err or ""


def profile(workspace: str) -> str:
    ws = os.path.realpath(workspace)                # Seatbelt matches real paths (/var is /private/var)
    if '"' in ws or "\\" in ws:
        raise ValueError("workspace path cannot be expressed in a sandbox profile")
    return ('(version 1)(deny default)'
            '(allow process-fork)(allow process-exec)(allow signal (target same-sandbox))'
            '(allow file-read*)(allow file-ioctl)(allow sysctl-read)'
            f'(allow file-write* (subpath "{ws}") (literal "/dev/null") (regex #"^/dev/(tty|fd/|stdout|stderr)"))'
            '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))'
            '(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))')


def wrap(argv: list[str], workspace: str) -> list[str]:
    if not os.path.exists(SANDBOX_EXEC):
        # Without the sandbox a generated check would run on the host unconfined: refuse rather than run it.
        raise RuntimeError(f"{SANDBOX_EXEC} is missing; generated checks are never run unconfined")
    return [SANDBOX_EXEC, "-p", profile(workspace), *argv]


def env(workspace: str) -> dict:
    """A minimal environment: no credentials, and HOME/TMPDIR inside the workspace so tools write there."""
    tmp = os.path.join(workspace, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "HOME": workspace, "TMPDIR": tmp,
            "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
