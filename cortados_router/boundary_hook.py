#!/usr/bin/env python3
"""CortadOS boundary hook: a Claude Code PreToolUse hook run on the CLIENT machine (docs/BOUNDARY-HOOK.md).

When the CortadOS provider routes a turn automatically to a local model, its effect boundary limits what the model's
tool calls may do (cortados_router/provider/effect_boundary.py). The tools run here, on the client, which is often not
the provider's machine: a symlink that exists only here (/repo/escape -> /outside) is invisible to the provider. This
hook closes that gap. For each Write, Edit, MultiEdit, NotebookEdit and Bash call it:

  1. asks the provider about the call's tool_use id (POST <provider>/v1/cortados/boundary/check, the provider token,
     metadata only: the id);
  2. a call the provider does not know is not the boundary's: allowed unchanged (exit 0, no output);
  3. a call the provider knows: Bash is denied on a profile without a shell (sensitive); a file write is resolved with
     os.path.realpath HERE and denied unless it lies strictly inside the realpath of the project folder the provider
     assumed, and off the protected paths the provider names;
  4. fails CLOSED for a boundary call: an id starting with MARK_PREFIX (minted only by the provider's boundary) is
     denied on any error (provider unreachable, a bad answer, an expired or unknown record, a crash of this script).

A denial is exit 2 with the reason on stderr (Claude Code blocks the call and shows the reason to the model).

Provider and token: CORTADOS_BOUNDARY_URL / CORTADOS_BOUNDARY_TOKEN when set, else the session's own ANTHROPIC_BASE_URL
and ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY): the provider that served the turn. The token is never printed.

Standalone and stdlib only: the routed-subagents mod embeds this file verbatim (hooks/register.ts BOUNDARY_HOOK_PY,
kept identical by tests/test_boundary_hook.py), so it holds no backquote and no dollar-brace."""
import json
import os
import sys
import urllib.error
import urllib.request

MARK_PREFIX = "toolu_cxb_"
CHECK_PATH = "/v1/cortados/boundary/check"
TIMEOUT_S = 5.0
WRITE_KEYS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
HOOK_TOOLS = frozenset(WRITE_KEYS) | {"Bash"}
MAX_ANSWER = 65536


class Denied(Exception):
    """The call must not run; the message is the reason (metadata only)."""


def provider() -> tuple:
    env = os.environ
    url = (env.get("CORTADOS_BOUNDARY_URL") or env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    token = (env.get("CORTADOS_BOUNDARY_TOKEN") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
             or "").strip()
    return url, token


def ask(tool_use_id: str) -> dict:
    """The provider's record of tool_use_id (check_answer's shape). Raises on any failure."""
    url, token = provider()
    if not url.startswith(("http://", "https://")) or not token:
        raise RuntimeError("no provider URL or token in this session")
    req = urllib.request.Request(url + CHECK_PATH, data=json.dumps({"tool_use_id": tool_use_id}).encode(),
                                 method="POST", headers={"content-type": "application/json",
                                                         "authorization": "Bearer " + token, "x-api-key": token})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        if resp.status != 200:
            raise RuntimeError("the provider answered HTTP %d" % resp.status)
        raw = resp.read(MAX_ANSWER + 1)
    if len(raw) > MAX_ANSWER:
        raise RuntimeError("the provider's answer is too large")
    answer = json.loads(raw)
    if not isinstance(answer, dict) or not isinstance(answer.get("known"), bool):
        raise RuntimeError("the provider's answer is not a boundary record")
    return answer


def inside(path: str, folder: str):
    """path's components strictly below folder (both absolute, normalised), or None."""
    root = folder.rstrip("/") + "/"
    return path[len(root):].split("/") if path.startswith(root) and len(path) > len(root) else None


def protected(parts: list, rules: dict) -> bool:
    """The provider's PROTECTED_PATHS rules (effect_boundary.protected_rules) on a path relative to the project."""
    parts = [p.casefold() for p in parts]
    if not parts or rules.get("root_dotfiles", True) and parts[0].startswith("."):
        return True
    if any(p in set(rules.get("components") or ()) for p in parts):
        return True
    pairs = {tuple(x) for x in rules.get("component_pairs") or () if isinstance(x, list) and len(x) == 2}
    if any(pair in pairs for pair in zip(parts, parts[1:])):
        return True
    return parts[-1] in set(rules.get("names") or ()) or parts[-1] in set(rules.get("build_files") or ())


def check_write(target, project, rules) -> None:
    """Deny unless target, resolved on THIS machine, lies strictly inside the real project folder and off the
    protected paths."""
    if not isinstance(project, str) or not project.startswith("/"):
        raise Denied("the provider names no project folder for this call")
    if not isinstance(rules, dict):
        raise Denied("the provider's answer has no path rules")
    if not isinstance(target, str) or not target.strip() or "\x00" in target:
        raise Denied("the call names no usable file path")
    if target.startswith("~") or "$" in target:
        raise Denied("a home or variable path is not resolved here")
    full = target if target.startswith("/") else os.path.join(project, target)
    real, real_project = os.path.realpath(full), os.path.realpath(project)
    rel = inside(real, real_project)
    if rel is None:
        raise Denied("the file resolves outside the project folder on this machine (a symlink or ..)")
    if protected(rel, rules):
        raise Denied("the file resolves onto a protected path of the project on this machine")


def decide(event: dict) -> None:
    """Raise Denied when the call must not run; return when it may."""
    tool = event.get("tool_name")
    if tool not in HOOK_TOOLS:
        return
    tid = event.get("tool_use_id")
    marked = isinstance(tid, str) and tid.startswith(MARK_PREFIX)
    if not isinstance(tid, str) or not tid:
        if marked:
            raise Denied("no tool_use id")
        return                                           # nothing to ask about: not the boundary's
    try:
        answer = ask(tid)
    except Exception as exc:
        if marked:
            raise Denied("the provider could not confirm this local-routed call (%s)" % type(exc).__name__)
        return                                           # not a boundary id: the provider's silence changes nothing
    if not answer["known"]:
        if marked:
            raise Denied("the provider has no live record of this local-routed call (expired or unknown)")
        return
    if answer.get("tool") != tool:
        raise Denied("the call does not match the provider's record of it")
    if tool == "Bash":
        if answer.get("bash") is not True:
            raise Denied("no shell on a sensitive local-routed turn")
        return
    inp = event.get("tool_input")
    key = (answer.get("write_tools") or {}).get(tool) or WRITE_KEYS[tool]
    check_write(inp.get(key) if isinstance(inp, dict) else None, answer.get("project"), answer.get("protected"))


def main(stdin=None, stderr=None) -> int:
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    marked = False
    try:
        event = json.loads(stdin.read())
        if not isinstance(event, dict):
            raise ValueError("the hook input is not an object")
        tid = event.get("tool_use_id")
        marked = isinstance(tid, str) and tid.startswith(MARK_PREFIX)
        decide(event)
        return 0
    except Denied as exc:
        stderr.write("cortados boundary: denied: %s. Nothing was run.\n" % exc)
        return 2
    except BaseException as exc:                         # a crash: fail closed for a boundary call only
        if marked:
            stderr.write("cortados boundary: denied: the check failed (%s). Nothing was run.\n" % type(exc).__name__)
            return 2
        return 0


if __name__ == "__main__":
    sys.exit(main())
