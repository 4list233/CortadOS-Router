"""Fail-open Claude Code PreToolUse delegation gate; standalone, stdlib only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unicodedata

PLANNING_TOOLS = frozenset((
    "Agent", "Task", "TodoWrite", "Read", "Grep", "Glob", "ToolSearch", "Skill",
    "AskUserQuestion", "ExitPlanMode", "EnterPlanMode",
))
PRUNE_AGE = 7 * 24 * 3600
FORK_DENY_REASON = (
    "orchestrator: do not use fork subagents in this mode. A fork inherits this orchestrator's model and "
    "conversation history, so it is not routed by S1 and it collides with this session's backend conversation "
    "(the session then fails with a 409). Spawn a fresh subagent instead (e.g. subagent_type \"general-purpose\") "
    "with a self-contained prompt."
)


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


def agent_type_key(name):
    # Mirrors how Claude Code 2.1.316 resolves an agent type (NFKC, lowercase, whitespace, Unicode dashes and
    # underscores removed), so spellings like "f_ork", "f-ork" or a full-width "ｆｏｒｋ" cannot slip a fork past us.
    name = unicodedata.normalize("NFKC", name).lower()
    return "".join(c for c in name if not c.isspace() and c != "_" and unicodedata.category(c) != "Pd")


def decide(event, env, directory=None):
    if "agent_id" in event:
        return None
    tool = event["tool_name"]
    tool_input = event.get("tool_input")
    subagent_type = tool_input.get("subagent_type") if isinstance(tool_input, dict) else None
    # Forks are denied outright, whatever the budget and without spending it: a fork inherits the pinned orchestrator
    # model and its full history, so it skips S1, and the provider matches its requests to the parent's held backend
    # conversation by history fingerprint, advances it, and the parent's next request then gets a 409 (seen live, e2e C5).
    if tool in ("Agent", "Task") and isinstance(subagent_type, str) and agent_type_key(subagent_type) == "fork":
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": FORK_DENY_REASON,
        }}
    allowed_mcp = {name.strip() for name in env.get("CORTADOS_DELEGATE_ALLOW_MCP", "").split(",")}
    if tool in PLANNING_TOOLS or (tool.startswith("mcp__") and tool in allowed_mcp):
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
        if count >= budget:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"orchestrator: you have used your {budget} direct work calls for this prompt; "
                    "delegate this to a subagent with the Agent tool "
                    "(subagents have every tool, MCP included)"
                ),
            }}
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
    return None


def main(stdin=None, stdout=None, stderr=None, env=None, directory=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        result = decide(json.load(stdin), os.environ if env is None else env, directory)
        if result is not None:
            print(json.dumps(result), file=stdout)
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
