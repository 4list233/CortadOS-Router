"""OS containment for code the loop does not trust: generated checks and the fixture code they run.

macOS Seatbelt (`sandbox-exec`): no network at all, and file writes only inside the workspace (children included).
Reads stay allowed, as Python and the test runner need the system's files. Verified 2026-10-24 on the M5 Max: a write
inside succeeds; writes outside the workspace or to the home folder, a child's write outside, and a TCP connection are
all denied, and `python3 -m unittest` still runs.
"""
from __future__ import annotations

import os

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def profile(workspace: str) -> str:
    ws = os.path.realpath(workspace)                # Seatbelt matches real paths (/var is /private/var)
    if '"' in ws or "\\" in ws:
        raise ValueError("workspace path cannot be expressed in a sandbox profile")
    return ('(version 1)(allow default)(deny network*)(deny file-write*)'
            f'(allow file-write* (subpath "{ws}") (literal "/dev/null") (regex #"^/dev/(tty|fd/|stdout|stderr)"))')


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
