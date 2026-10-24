"""Task specs (closed schema, AC-03) and their disposable git workspaces (AC-05)."""
from __future__ import annotations

import os
import subprocess
import unicodedata
from pathlib import PurePosixPath

import common
from outcome import containment

FIELDS = {"id", "seed_id", "prompt", "category", "work_kind", "fixture", "check", "rubric", "outward_tools"}
# Small repos keep generation fast and runs cheap (docs/S1-OUTCOME-LOOP.md, Limits). Too small: no room for a real
# module plus its tests. Too large: generation output truncates and every run reads more than the task needs.
MAX_FIXTURE_BYTES = 200_000
MAX_FIXTURE_FILES = 40
# A fixture's tests finish in seconds; five minutes allows a slow test suite, and more would hide a hung check.
MAX_CHECK_TIMEOUT_S = 300
MAX_PROMPT_CHARS = 6000          # S1's request clip
TOOL_NAME_CHARS = 64
# Directories a fixture may not create: git's own state, and the agent CLIs' config folders, which could reconfigure
# the run (settings, hooks, MCP servers) from inside the task.
RESERVED_DIRS = (".git", ".claude", ".codex", ".tmp", ".gitmodules", ".gitattributes")


class SpecError(ValueError):
    """A task spec that cannot be run; the message names the rule."""


def validate(spec: dict) -> dict:
    if not isinstance(spec, dict) or set(spec) != FIELDS:
        raise SpecError(f"fields must be exactly {sorted(FIELDS)}")
    for key in ("id", "seed_id", "prompt", "category"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise SpecError(f"{key} must be a non-empty string")
    if len(spec["prompt"]) > MAX_PROMPT_CHARS:
        raise SpecError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
    if spec["work_kind"] not in common.core.WORK_KINDS:
        raise SpecError("work_kind is not one of the router's work kinds")
    files = (spec["fixture"] or {}).get("files") if isinstance(spec["fixture"], dict) else None
    if not isinstance(files, dict) or set(spec["fixture"]) != {"files"}:
        raise SpecError("fixture must be {files: {path: text}}")
    if len(files) > MAX_FIXTURE_FILES:
        raise SpecError(f"fixture has more than {MAX_FIXTURE_FILES} files")
    size = 0
    for rel, text in files.items():
        p = PurePosixPath(rel) if isinstance(rel, str) else None
        if p is None or not rel or p.is_absolute() or ".." in p.parts:
            raise SpecError(f"fixture path {rel!r} must be relative and stay inside the workspace")
        # APFS is case- and normalisation-insensitive: `.GIT/config` is `.git/config` (Codex review), at any depth.
        if any(unicodedata.normalize("NFC", part).casefold() in RESERVED_DIRS for part in p.parts):
            raise SpecError(f"fixture path {rel!r} is under a reserved directory {RESERVED_DIRS}")
        if not isinstance(text, str):
            raise SpecError(f"fixture file {rel!r} must be text")
        size += len(text.encode())
    if size > MAX_FIXTURE_BYTES:
        raise SpecError(f"fixture exceeds {MAX_FIXTURE_BYTES} bytes")
    check = spec["check"]
    if not isinstance(check, dict) or set(check) != {"argv", "timeout_s"} or not isinstance(check["argv"], list) \
            or any(not isinstance(a, str) or not a for a in check["argv"]):
        raise SpecError("check must be {argv: [str], timeout_s: int}")
    if type(check["timeout_s"]) is not int or not 1 <= check["timeout_s"] <= MAX_CHECK_TIMEOUT_S:
        raise SpecError(f"check.timeout_s must be 1..{MAX_CHECK_TIMEOUT_S}")
    if not isinstance(spec["rubric"], list) or not spec["rubric"] or any(not isinstance(r, str) or not r for r in spec["rubric"]):
        raise SpecError("rubric must be a non-empty list of criteria")
    tools = spec["outward_tools"]
    if not isinstance(tools, list) or any(not isinstance(t, str) or not t.replace("_", "").isalnum()
                                          or len(t) > TOOL_NAME_CHARS for t in tools):
        raise SpecError("outward_tools must be simple tool names")
    return spec


def materialize(spec: dict, workspace: str) -> None:
    """Write the fixture into a fresh git repo at `workspace` (which must exist and be empty) and commit it."""
    if os.listdir(workspace):
        raise SpecError("workspace must be empty")
    for rel, text in spec["fixture"]["files"].items():
        target = os.path.join(workspace, *PurePosixPath(rel).parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(text)
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "fixture"]):
        _git(argv, workspace, check=True)
    with open(os.path.join(workspace, ".git", "info", "exclude"), "a") as f:
        f.write("\n.tmp/\n")                          # the run's private temp dir is not part of the work product


# Git never runs user, system or repository-supplied commands for the loop: no global/system config, no hooks,
# no fsmonitor, and every call contained like a check (the agent may have rewritten .git/config).
_GIT_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=false",
              "-c", "protocol.allow=never", "-c", "user.name=outcome", "-c", "user.email=outcome@localhost"]


def _git(args: list[str], workspace: str, check: bool = False) -> subprocess.CompletedProcess:
    env = dict(containment.env(workspace), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0")
    return subprocess.run(containment.wrap(["git", *_GIT_FLAGS, *args], workspace), cwd=workspace, env=env,
                          check=check, capture_output=True, text=True)


def run_check(spec: dict, workspace: str) -> dict:
    argv = spec["check"]["argv"]
    if not argv:
        return {"ran": False, "passed": None}
    try:
        p = subprocess.run(containment.wrap(argv, workspace), cwd=workspace, env=containment.env(workspace),
                           capture_output=True, text=True, timeout=spec["check"]["timeout_s"])
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": False, "exit": None, "why": "check timed out"}
    except OSError as exc:
        return {"ran": True, "passed": False, "exit": None, "why": f"check could not start: {exc}"}
    return {"ran": True, "passed": p.returncode == 0, "exit": p.returncode,
            "tail": (p.stdout + p.stderr)[-2000:]}


def workspace_diff(workspace: str, limit: int = 20_000) -> str:
    """What the agent changed, for the judge. Clipped: the judge needs the shape of the change, not every byte.

    The agent may have edited .git/config: git runs contained, with every command-running setting off (_git)."""
    _git(["add", "-A"], workspace)
    return _git(["diff", "--cached", "--stat", "-p"], workspace).stdout[:limit]
