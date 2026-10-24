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

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


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
