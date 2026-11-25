#!/usr/bin/env python3
"""CortadOS boundary hook: a Claude Code PreToolUse hook run on the CLIENT machine (docs/BOUNDARY-HOOK.md).

When the CortadOS provider routes a turn automatically to a local model, its effect boundary limits what the model's
tool calls may do (cortados_router/provider/effect_boundary.py). The tools run here, on the client, which is often not
the provider's machine: a symlink that exists only here (/repo/escape -> /outside) is invisible to the provider. This
hook closes that gap. For each Write, Edit, MultiEdit, NotebookEdit and Bash call, and, in a session launched with a
local pick (session.py installs it there with matcher "*"), for every call of any tool whose id the provider minted, it:

  1. asks the provider about the call's tool_use id (POST <provider>/v1/cortados/boundary/check, the provider token,
     metadata only: the id);
  2. a call the provider does not know is not the boundary's: allowed unchanged (exit 0, no output);
  3. a call the provider knows: Bash is denied on a profile without a shell (sensitive); a file write is resolved with
     os.path.realpath HERE and denied unless it lies strictly inside the realpath of the project folder the provider
     assumed, and off the protected paths the provider names;
  4. fails CLOSED for a boundary call: an id starting with MARK_PREFIX (minted only by the provider's boundary) is
     denied on any error (provider unreachable, a bad answer, an expired or unknown record, a crash of this script);
  5. a known call of a tool off the record's profile (its tools list) is denied; on a local orchestrator's profile (the
     route pick "local", plan and delegate only) Bash is allowed only as start_task's exact watch_command (the
     --watch-prefix the launcher passes this hook, then WATCH_TAIL_RE, the provider's effect_boundary pattern), no
     BashOutput / KillShell / KillBash, and start_task may not ask for the inputs the provider names
     (delegate_refused: chrome, connectors).

A denial is exit 2 with the reason on stderr (Claude Code blocks the call and shows the reason to the model).

Provider and token: CORTADOS_BOUNDARY_URL / CORTADOS_BOUNDARY_TOKEN when set, else the session's own ANTHROPIC_BASE_URL
and ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY): the provider that served the turn. The token is never printed.

Standalone and stdlib only: the routed-subagents mod embeds this file verbatim (hooks/register.ts BOUNDARY_HOOK_PY,
kept identical by tests/test_boundary_hook.py), so it holds no backquote and no dollar-brace.

Isolation (docs/BOUNDARY-HOOK.md "Isolation"): the model may write files into the project, and the hook runs with the
project as its working directory. Every installer therefore runs it as "<absolute python3> -I -S -B ..." from /, inside
a shell guard: an absolute interpreter (no PATH lookup), isolated mode (neither the working directory nor the script's
folder on sys.path, no PYTHON* variables, no user site-packages), no site import (-S: no .pth file of the interpreter's
site-packages, e.g. an editable install pointing at the project, can run a project sitecustomize first). The guard
denies a provider-marked call when the interpreter cannot start or ends with anything but 0 or 2. Run as a program
WITHOUT -I, it drops the first sys.path entry (the working directory or its own folder) before importing anything
else and denies every provider-marked call; without -S it denies every provider-marked call too. And it denies a known
call whose project folder holds its interpreter or its own file."""
import sys

if __name__ == "__main__" and not sys.flags.isolated:   # an install without -I: no project module may load below
    del sys.path[0]                                      # '' (python -c) or this script's folder
    sys.path[:] = [p for p in sys.path if p and p != "."]

import json
import os
import re
import urllib.error
import urllib.request

MARK_PREFIX = "toolu_cxb_"
CHECK_PATH = "/v1/cortados/boundary/check"
TIMEOUT_S = 5.0
WRITE_KEYS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
HOOK_TOOLS = frozenset(WRITE_KEYS) | {"Bash"}
MAX_ANSWER = 65536
START_TASK_TOOL = "mcp__cortados-route__start_task"
# start_task's watch_command, the one shell command of a local orchestrator (review localorch3 P1): EXACTLY the watcher
# prefix the launcher gave this hook (--watch-prefix: child_tasks.watch_prefix of the delegation MCP server session.py
# configures, i.e. the interpreter real path and child_tasks.py path start_task itself puts in watch_command), then
# WATCH_TAIL_RE (effect_boundary.WATCH_TAIL_RE's pattern; tests/test_local_orchestrator.py keeps the two equal): task ids
# and --stall / --timeout / --interval with an integer, nothing else. Without a prefix (a session not launched with a
# local pick, the routed-subagents mod) no shell command is the watcher.
WATCH_TAIL_RE = re.compile(
    r" --watch(?: --(?:stall|timeout|interval) [0-9]{1,7})* t[0-9]{14}-[0-9a-f]{8}"
    r"(?: (?:t[0-9]{14}-[0-9a-f]{8}|--(?:stall|timeout|interval) [0-9]{1,7})){0,63}")
# The background-job tools a local orchestrator never has (effect_boundary.ORCHESTRATOR_SHELL, review localorch3 P2).
JOB_TOOLS = frozenset({"BashOutput", "KillShell", "KillBash"})
WATCH_PREFIX_FLAG = "--watch-prefix"


def is_watch_command(command, prefix):
    """Whether command is exactly prefix + WATCH_TAIL_RE (no prefix: never)."""
    return (isinstance(command, str) and isinstance(prefix, str) and bool(prefix) and command.startswith(prefix)
            and bool(WATCH_TAIL_RE.fullmatch(command[len(prefix):])))


def watch_prefix_from(argv):
    """The --watch-prefix value of the hook's own command line (written by session.py into the private settings
    file), or None."""
    argv = list(argv or ())
    for i, a in enumerate(argv[:-1]):
        if a == WATCH_PREFIX_FLAG:
            return argv[i + 1] or None
    return None


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


def within(path: str, folder: str) -> bool:
    """path is folder itself or lies below it (both absolute, normalised)."""
    return path == folder or inside(path, folder) is not None


def own_paths() -> list:
    """What this run loads code from: its interpreter and, run as a file, that file and its folder; each lexically and
    with symlinks followed."""
    found = []
    for what, path in (("interpreter", sys.executable), ("hook file", globals().get("__file__"))):
        if not path:
            if what == "interpreter":
                raise Denied("the hook cannot name its own interpreter")
            continue
        for p in {os.path.abspath(path), os.path.realpath(path)}:
            found.append((what, p))
            if what == "hook file":
                found.append(("hook folder", os.path.dirname(p)))
    return found


def check_self(project) -> None:
    """Deny when the project folder of this call holds the interpreter running this hook, this hook's file or its
    folder: the model may write there, so the hook could be the model's own code."""
    if not isinstance(project, str) or not project.startswith("/"):
        return                                           # check_write denies a write without a project folder
    roots = {os.path.normpath(project), os.path.realpath(project)}
    for what, path in own_paths():
        if any(within(path, root) for root in roots):
            raise Denied("the boundary hook's %s lies inside the project folder, which the model may write" % what)


def decide(event: dict, watch_prefix=None) -> None:
    """Raise Denied when the call must not run; return when it may. watch_prefix: the launcher's watcher prefix
    (watch_prefix_from), the only program a local orchestrator's Bash may run."""
    tool = event.get("tool_name")
    tid = event.get("tool_use_id")
    marked = isinstance(tid, str) and tid.startswith(MARK_PREFIX)
    if tool not in HOOK_TOOLS and not marked:
        return                                           # nothing the boundary minted: no question to ask
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
    tools = answer.get("tools")
    if tools is not None and (not isinstance(tools, list) or tool not in tools):
        raise Denied("%s is not allowed on this local-routed turn's profile (%s)"
                     % ("no shell: Bash" if tool == "Bash" else "the tool", answer.get("profile")))
    inp = event.get("tool_input")
    if tool in JOB_TOOLS and answer.get("bash") is not True:        # only a profile with a free shell has jobs
        raise Denied("a local orchestrator does not read or stop background jobs (task_status / wait_task do)")
    if tool not in HOOK_TOOLS:
        if tool == START_TASK_TOOL:
            refused = answer.get("delegate_refused") or []
            if not isinstance(inp, dict) or not isinstance(refused, list):
                raise Denied("the delegation call cannot be checked")
            for key in refused:
                if isinstance(key, str) and inp.get(key):
                    raise Denied("a local orchestrator may not start a task with %s" % key)
        return
    check_self(answer.get("project"))
    if tool == "Bash":
        if answer.get("bash") is True:
            return
        if answer.get("bash_watch") is True:
            command = inp.get("command") if isinstance(inp, dict) else None
            if is_watch_command(command, watch_prefix):
                return
            raise Denied("a local orchestrator runs no shell command but start_task's exact watch_command"
                         + ("" if watch_prefix else " (this session was not launched with a local pick)"))
        raise Denied("no shell on a sensitive local-routed turn")
    key = (answer.get("write_tools") or {}).get(tool) or WRITE_KEYS[tool]
    check_write(inp.get(key) if isinstance(inp, dict) else None, answer.get("project"), answer.get("protected"))


def main(stdin=None, stderr=None, standalone=False, argv=None) -> int:
    """standalone: run as the hook program (not imported), where a run without -I denies every marked call. argv: the
    hook's arguments (default sys.argv[1:] when standalone), read for --watch-prefix."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    marked = False
    try:
        event = json.loads(stdin.read())
        if not isinstance(event, dict):
            raise ValueError("the hook input is not an object")
        tid = event.get("tool_use_id")
        marked = isinstance(tid, str) and tid.startswith(MARK_PREFIX)
        if standalone and marked and not sys.flags.isolated:
            raise Denied("the hook runs without python -I, so files in the project could have replaced its code")
        if standalone and marked and not sys.flags.no_site:
            raise Denied("the hook runs without python -S, so a site-packages .pth file could have run project code "
                         "before it")
        decide(event, watch_prefix_from(sys.argv[1:] if argv is None and standalone else argv))
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
    sys.exit(main(standalone=True))
