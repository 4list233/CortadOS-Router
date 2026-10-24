"""Task specs (closed schema, AC-03) and their disposable git workspaces (AC-05)."""
from __future__ import annotations

import os
import subprocess
from pathlib import PurePosixPath

import common

FIELDS = {"id", "seed_id", "prompt", "category", "work_kind", "fixture", "check", "rubric", "outward_tools"}
# Small repos keep generation fast and runs cheap (docs/S1-OUTCOME-LOOP.md, Limits). Too small: no room for a real
# module plus its tests. Too large: generation output truncates and every run reads more than the task needs.
MAX_FIXTURE_BYTES = 200_000
MAX_FIXTURE_FILES = 40
# A fixture's tests finish in seconds; five minutes allows a slow test suite, and more would hide a hung check.
MAX_CHECK_TIMEOUT_S = 300
MAX_PROMPT_CHARS = 6000          # S1's request clip
TOOL_NAME_CHARS = 64


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
        if p is None or not rel or p.is_absolute() or ".." in p.parts or p.parts[0] == ".git":
            raise SpecError(f"fixture path {rel!r} must be relative and stay inside the workspace")
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
    env = dict(os.environ, GIT_AUTHOR_NAME="outcome", GIT_AUTHOR_EMAIL="outcome@localhost",
               GIT_COMMITTER_NAME="outcome", GIT_COMMITTER_EMAIL="outcome@localhost")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "--allow-empty", "-m", "fixture"]):
        subprocess.run(argv, cwd=workspace, env=env, check=True, capture_output=True)


def run_check(spec: dict, workspace: str) -> dict:
    argv = spec["check"]["argv"]
    if not argv:
        return {"ran": False, "passed": None}
    try:
        p = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=spec["check"]["timeout_s"])
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": False, "exit": None, "why": "check timed out"}
    except OSError as exc:
        return {"ran": True, "passed": False, "exit": None, "why": f"check could not start: {exc}"}
    return {"ran": True, "passed": p.returncode == 0, "exit": p.returncode,
            "tail": (p.stdout + p.stderr)[-2000:]}


def workspace_diff(workspace: str, limit: int = 20_000) -> str:
    """What the agent changed, for the judge. Clipped: the judge needs the shape of the change, not every byte."""
    subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True)
    p = subprocess.run(["git", "diff", "--cached", "--stat", "-p"], cwd=workspace, capture_output=True, text=True)
    return p.stdout[:limit]
