"""Fail-open Claude Code PreToolUse delegation gate; standalone, stdlib only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import urllib.parse

PLANNING_TOOLS = frozenset((
    "TodoWrite", "Read", "Grep", "Glob", "ToolSearch", "Skill",
    "AskUserQuestion", "ExitPlanMode", "EnterPlanMode",
))
# The delegation path of a top-level session is the cortados-route MCP server (owner rule, CORTADOS-ORCHESTRATOR.md):
# its tools never spend the budget. The prefix carries the whole server name plus the `__` separator, so no other
# server's tool (a workspace connector's own `start_task`) can ride it.
DELEGATION_TOOL_PREFIX = "mcp__cortados-route__"
# Agent and Task are the native subagent tools. On the main loop they are refused outright, whatever the budget: the
# owner's rule names start_task / route_task as the only delegation path, and a routed Agent child dies with the
# session's hooks (2026-11-08: six builders aborted with `$.process.run(claude) aborted`). A fork subagent is covered
# by this too (it used to be refused separately: it inherits the pinned orchestrator model and history, skips S1 and
# collides with the parent's backend conversation, 409).
SUBAGENT_TOOLS = frozenset(("Agent", "Task"))
PRUNE_AGE = 7 * 24 * 3600
AGENT_DENY_REASON = (
    "orchestrator: the Agent tool is not the delegation path on this session (owner rule, CORTADOS-ORCHESTRATOR.md; "
    "a routed Agent subagent dies with this session's hooks). Delegate with mcp__cortados-route__start_task (a "
    "CortadOS child session with files, shell and tools on this machine) or mcp__cortados-route__route_task (a text "
    "answer, no tools); if they are deferred, load them with ToolSearch first. If cortados-route is not connected, "
    "tell the owner in one line and do the work directly."
)


def budget_deny_reason(budget, provider=None):
    where = f" at {provider}" if provider else ""
    return (
        f"orchestrator: you have used your {budget} direct work calls for this prompt; delegate this with "
        "mcp__cortados-route__start_task (a CortadOS child session with every tool, MCP included) or "
        "mcp__cortados-route__route_task (a text answer). If cortados-route is not connected, tell the owner in one "
        f"line, then: while the provider{where} is unreachable this gate releases the budget by itself and your "
        "direct calls go through (Agent and Task stay refused); if the provider is up but cortados-route is still "
        "missing, ask the owner to relaunch with --no-gate or a larger --delegate-budget."
    )


# Outage recovery (owner rule: when cortados-route is not connected, say so in one line and do the work directly).
# Without it, a router outage left the session with neither delegation nor direct work. The release is decided by a
# real check, not a switch the model can flip: only when the budget is spent, the gate opens a TCP connection to the
# provider that cortados-route delegates through; if that fails, the call is allowed and the owner sees a stderr line
# (exit 1, as for fail-open). The only override is the owner's own launcher flags (--no-gate, --delegate-budget),
# which the model cannot change from inside the session. Agent/Task stay refused either way (see SUBAGENT_TOOLS).
PROBE_TIMEOUT = 2.0  # seconds; bounded so a black-holed host costs at most this per call past the budget


def provider_url(env):
    """The provider cortados-route delegates through: CORTADOS_PROVIDER_URL, else the launcher's ANTHROPIC_BASE_URL
    (session.launcher_env sets it to the provider). None when neither is set: no target, so no release."""
    for key in ("CORTADOS_PROVIDER_URL", "ANTHROPIC_BASE_URL"):
        url = (env.get(key) or "").strip()
        if url:
            return url
    return None


def display(url):
    """scheme://host:port only: a URL's userinfo, path or query never reaches a model-visible reason or the log."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = f":{parts.port}" if parts.port else ""
        if parts.hostname:
            return f"{parts.scheme}://{parts.hostname}{port}"
    except ValueError:
        pass
    return "(malformed URL)"


def provider_unreachable(url, timeout=PROBE_TIMEOUT):
    """None when a TCP connection to the provider succeeds, else a one-line reason. A malformed URL is an outage too:
    cortados-route cannot delegate through it either."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "provider URL is malformed"
    if parts.scheme not in ("http", "https") or not host:
        return "provider URL is malformed"
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as exc:
        return f"cannot connect to {host}:{port} ({type(exc).__name__})"
    return None


def state_path(directory, session_id):
    # Never let a hook-supplied session id become a filesystem path.
    return Path(directory) / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")


def private_directory(directory):
    directory = Path(os.path.abspath(os.path.expanduser(str(directory))))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for component in (directory, *directory.parents):
        st = component.lstat()
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise ValueError("gate directory component is not a real directory")
        if st.st_uid not in (os.getuid(), 0):
            raise ValueError("gate directory component has an untrusted owner")
    st = directory.lstat()
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError("gate directory must be owned by this user and mode 0700")
    return directory


def private_file(path, flags):
    fd = os.open(str(path), flags | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1):
            raise ValueError("gate file must be a private regular 0600 file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def deny(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}


class OutageRelease(Exception):
    """The budget is spent but the provider is down: allow the call, and tell the owner (main prints the reason)."""


def decide(event, env, directory=None, probe=provider_unreachable):
    # A subagent (the hook input carries agent_id) is never gated: it does its work directly, with every tool. A
    # CortadOS child task is launched with --no-gate (child_tasks._prepare), so this hook is not even installed there.
    if "agent_id" in event:
        return None
    tool = event["tool_name"]
    # Native subagents are refused on the main loop, whatever the budget and without spending it (see SUBAGENT_TOOLS).
    if tool in SUBAGENT_TOOLS:
        return deny(AGENT_DENY_REASON)
    allowed_mcp = {name.strip() for name in env.get("CORTADOS_DELEGATE_ALLOW_MCP", "").split(",")}
    if (tool in PLANNING_TOOLS or tool.startswith(DELEGATION_TOOL_PREFIX)
            or (tool.startswith("mcp__") and tool in allowed_mcp)):
        return None
    budget = int(env.get("CORTADOS_DELEGATE_BUDGET", "3"))
    if budget < 0:
        raise ValueError("delegate budget must be nonnegative")
    session_id = event["session_id"]
    prompt_id = event.get("prompt_id", "none")
    if not isinstance(session_id, str) or not session_id or not isinstance(prompt_id, str):
        raise ValueError("invalid session_id or prompt_id")
    directory = private_directory(directory or "~/.config/cortados/gate")
    # A stable shared lock protects read/modify/replace AND pruning. Locking the replaced
    # counter inode would let parallel hooks overspend the budget.
    with os.fdopen(private_file(directory / ".lock", os.O_CREAT | os.O_RDWR), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cutoff = time.time() - PRUNE_AGE
        for path in directory.glob("*.json"):
            st = path.lstat()
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_mtime < cutoff:
                path.unlink()
        path = state_path(directory, session_id)
        try:
            fd = private_file(path, os.O_RDONLY)
        except FileNotFoundError:
            counts = {}
        else:
            with os.fdopen(fd, encoding="utf-8") as src:
                counts = json.load(src)
        if not isinstance(counts, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0 for k, v in counts.items()
        ):
            raise ValueError("invalid gate counter state")
        count = counts.get(prompt_id, 0)
        exhausted = count >= budget
        if not exhausted:
            counts[prompt_id] = count + 1
            fd, name = tempfile.mkstemp(prefix=".counter-", dir=str(directory))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as dst:
                    json.dump(counts, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
    if not exhausted:
        return None
    # Probed after the lock is released, so a slow probe never stalls parallel hooks. An outage call is not counted:
    # the budget is suspended while the provider is down, not spent.
    url = provider_url(env)
    outage = probe(url) if url else None
    if outage:
        raise OutageRelease(f"orchestrator gate: provider {display(url)} unreachable: {outage}; direct work allowed past the "
                            "budget until it is back (Agent stays refused)")
    return deny(budget_deny_reason(budget, display(url) if url else None))


def main(stdin=None, stdout=None, stderr=None, env=None, directory=None, probe=provider_unreachable):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        result = decide(json.load(stdin), os.environ if env is None else env, directory, probe)
        if result is not None:
            print(json.dumps(result), file=stdout)
    except OutageRelease as exc:
        # Same visibility as fail-open: exit 1 shows the owner this line while Claude Code still allows the tool.
        print(str(exc).replace("\n", " ").replace("\r", " "), file=stderr)
        return 1
    except Exception as exc:
        # A broken gate must NEVER brick a session: all internal errors fail OPEN,
        # visibly, rather than turning corrupt state or an I/O failure into denial.
        detail = str(exc).replace("\n", " ").replace("\r", " ")
        print(f"orchestrator gate: fail open: {type(exc).__name__}: {detail}", file=stderr)
        # Exit 1, not 0: Claude Code hides a PreToolUse hook's stderr on exit 0, but shows it on a non-blocking
        # error (any code other than 0 and 2) while still allowing the tool, so the user sees the gate is off.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
