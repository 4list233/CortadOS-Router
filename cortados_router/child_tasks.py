"""Child tasks: whole tasks handed to a separate headless Claude Code session on THIS machine, run through the CortadOS
provider, with only the result coming back. Backs the `start_task` / `task_status` / `wait_task` / `list_tasks` /
`cancel_task` tools of the `cortados-route` MCP server (subagent_mcp.py).

Why a separate module: route_task in subagent_mcp.py is standalone (stdlib, no package import). A child session needs
the whole-session launcher's env/argv construction (cortados_router.session.prepare: provider env, sub/<model> pin,
routed native subagents, 0600 settings file), so this module imports the package. subagent_mcp imports it lazily,
after putting the repo root on sys.path, so route_task keeps working even if this module cannot load.

Layout per task (all under a verified private 0700 directory, default ~/.config/cortados/tasks/<task_id>/):
  meta.json     what was asked (task text length, cwd, orchestrator, permission mode, runner pid, start time)
  prompt.txt    the task text, fed to `claude -p` on stdin (never argv: argv is world-readable in `ps` and size-capped)
  session-*.json the launcher's 0600 settings (holds the provider token; never printed)
  stdout.log    claude's `--output-format json` result;  stderr.log  claude's diagnostics
  result.json   written by the runner when the child ends: exit code, end time, cancelled/timed-out flags
  cancelled     marker written by cancel_task before it signals the runner's process group

The runner (`python3 -m cortados_router.child_tasks --run <dir>`) is a detached session leader (start_new_session), so
the child survives the MCP server (the desktop app may restart it) and cancel_task can signal exactly that process group.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from cortados_router import session

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = "~/.config/cortados/tasks"
TASK_ID_RE = re.compile(r"^t\d{14}-[0-9a-f]{8}$")      # strict: the id becomes a path component, so no traversal
# Permission modes a child may run in. acceptEdits (default): file edits in cwd go through, other tool calls that need
# approval are denied (nobody is there to approve). default: every approval-needing call is denied (read-mostly).
# plan: read-only planning. bypassPermissions is NOT offered: an unattended session with unreviewed shell access and the
# user's full MCP set is too much blast radius to enable by a tool argument; widen with `allowed_tools` instead.
PERMISSION_MODES = ("acceptEdits", "default", "plan")
DEFAULT_PERMISSION_MODE = "acceptEdits"
MAX_TASK_CHARS = 200_000            # a prompt beyond this is a document dump, not a task; refuse rather than truncate
MAX_ALLOWED_TOOLS = 64              # an allowlist longer than this is not a reviewed allowlist
MAX_TOOL_PATTERN = 200
MAX_RUNNING = int(os.environ.get("CORTADOS_CHILD_MAX_RUNNING", "4"))   # each child spends subscription quota
CHILD_MAX_S = int(os.environ.get("CORTADOS_CHILD_MAX_S", str(3 * 3600)))  # an unattended child that never ends burns quota
RESULT_CHARS = 20_000               # result text returned through MCP; MCP clients choke on huge tool results
LOG_TAIL_CHARS = 2_000              # stderr tail shown on failure
WAIT_MAX_S = 540                    # MCP clients time a tool call out (~10 min); stay under it
NESTED_ENV = "CORTADOS_CHILD_TASK"  # set in every child: a child may not start children (runaway quota fan-out)
# Claude Code refuses to start inside another Claude Code session when it sees this; the child is a fresh session.
STRIP_ENV = ("CLAUDECODE",)
MANAGED_DIRS = session.MANAGED_SETTINGS_DIRS   # tests point this elsewhere


class TaskError(Exception):
    pass


def tasks_dir() -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(os.environ.get("CORTADOS_TASKS_DIR") or TASKS_DIR)))


def _private_root() -> str:
    d = tasks_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        session.verify_private_dir(d, os.path.expanduser("~"))
    except session.SessionError as exc:
        raise TaskError(str(exc)) from None
    return d


def task_path(task_id) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise TaskError(f"invalid task_id {task_id!r}: expected the id start_task returned (t<14 digits>-<8 hex>)")
    p = os.path.join(_private_root(), task_id)
    if not os.path.isdir(p):
        raise TaskError(f"no such task {task_id}")
    return p


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)                       # atomic: a reader never sees half a file


def claude_bin() -> str:
    """The claude executable: CORTADOS_CHILD_CLAUDE_BIN, else `claude` on PATH. The desktop app starts MCP servers with
    a thin PATH, so a missing binary is reported (with the variable to set) instead of failing inside the runner."""
    name = os.environ.get("CORTADOS_CHILD_CLAUDE_BIN") or "claude"
    found = shutil.which(name)
    if not found:
        raise TaskError(f"claude executable {name!r} not found on PATH; set CORTADOS_CHILD_CLAUDE_BIN to its absolute path")
    return found


def _validate(task, cwd, orchestrator, permission_mode, allowed_tools):
    if not isinstance(task, str) or not task.strip():
        raise TaskError("start_task requires a non-empty 'task'")
    if len(task) > MAX_TASK_CHARS:
        raise TaskError(f"task is {len(task)} characters; the limit is {MAX_TASK_CHARS} (put large inputs in files under cwd)")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise TaskError("start_task requires 'cwd', an absolute directory path")
    if not os.path.isdir(cwd):
        raise TaskError(f"cwd {cwd!r} is not an existing directory")
    if not isinstance(orchestrator, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,80}", orchestrator):
        raise TaskError(f"invalid orchestrator {orchestrator!r}: use 'cortados' or a pinnable model name (e.g. fable, opus)")
    if permission_mode not in PERMISSION_MODES:
        raise TaskError(f"permission_mode {permission_mode!r} is not allowed; choose one of {', '.join(PERMISSION_MODES)} "
                        "(bypassPermissions is deliberately unavailable for unattended children)")
    tools = []
    if allowed_tools is not None:
        if not isinstance(allowed_tools, list) or len(allowed_tools) > MAX_ALLOWED_TOOLS:
            raise TaskError(f"allowed_tools must be a list of at most {MAX_ALLOWED_TOOLS} tool patterns")
        for t in allowed_tools:
            if (not isinstance(t, str) or not t.strip() or len(t) > MAX_TOOL_PATTERN or "," in t
                    or not t.isprintable()):
                raise TaskError(f"invalid allowed_tools entry {t!r}: a printable pattern such as 'Bash(git diff:*)', "
                                f"no commas, at most {MAX_TOOL_PATTERN} characters")
            tools.append(t.strip())
    return tools


def _new_id() -> str:
    return "t" + time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)


def _running_count(root: str) -> int:
    n = 0
    for name in os.listdir(root):
        if TASK_ID_RE.match(name) and _state(os.path.join(root, name))[0] == "running":
            n += 1
    return n


def start(task, cwd, orchestrator="cortados", permission_mode=DEFAULT_PERMISSION_MODE, allowed_tools=None,
          provider_url="", token_file=None) -> dict:
    if os.environ.get(NESTED_ENV):
        raise TaskError("start_task is not available inside a child task: children may not start children "
                        "(unbounded fan-out would spend subscription quota unattended)")
    tools = _validate(task, cwd, orchestrator, permission_mode, allowed_tools)
    if not provider_url:
        raise TaskError("start_task is not configured: set CORTADOS_PROVIDER_URL for the cortados-route MCP server")
    exe = claude_bin()
    root = _private_root()
    if _running_count(root) >= MAX_RUNNING:
        raise TaskError(f"{MAX_RUNNING} child tasks are already running (CORTADOS_CHILD_MAX_RUNNING); wait for or cancel one")
    task_id = _new_id()
    d = os.path.join(root, task_id)
    os.mkdir(d, 0o700)
    os.chmod(d, 0o700)                          # mkdir's mode is masked by umask; the launcher verifies exactly 0700
    launch = ["--orchestrator", orchestrator, "--provider-url", provider_url, "--settings-dir", d]
    if token_file:
        launch += ["--token-file", token_file]
    if orchestrator == "cortados":
        # S1 already routes every turn; the delegate-first directive and its gate exist to keep a pinned top model
        # from doing the work itself, which does not apply here.
        launch += ["--no-delegate-directive", "--no-gate"]
    extra = ["-p", "--output-format", "json", "--permission-mode", permission_mode]
    if tools:
        extra += ["--allowedTools", ",".join(tools)]
    base = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    try:
        _args, env, cmd = session.prepare(launch + ["--"] + extra, base, MANAGED_DIRS)
    except session.SessionError as exc:
        shutil.rmtree(d, ignore_errors=True)
        raise TaskError(f"cannot prepare the child session: {exc}") from None
    cmd[0] = exe
    env[NESTED_ENV] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with open(os.open(os.path.join(d, "prompt.txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
              encoding="utf-8") as f:
        f.write(task)
    meta = {"task_id": task_id, "cwd": cwd, "orchestrator": orchestrator, "permission_mode": permission_mode,
            "allowed_tools": tools, "task_chars": len(task), "task_preview": task.strip()[:200],
            "started_at": time.time(),
            # argv carries only the model, settings path and flags; the token lives in the 0600 settings file and env.
            "argv": cmd[1:]}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, "cwd": cwd, "max_s": CHILD_MAX_S})
    try:
        with open(os.path.join(d, "runner.log"), "ab") as rlog:
            runner = subprocess.Popen([sys.executable, "-B", "-m", "cortados_router.child_tasks", "--run", d],
                                      cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=rlog, start_new_session=True)
    except OSError as exc:
        _write_json(os.path.join(d, "meta.json"), meta)
        _write_json(os.path.join(d, "result.json"), {"exit_code": None, "ended_at": time.time(),
                                                     "error": f"could not start the runner: {type(exc).__name__}"})
        raise TaskError(f"could not start the child runner ({type(exc).__name__}: {exc})") from None
    _REAP[task_id] = runner
    meta["runner_pid"] = runner.pid
    _write_json(os.path.join(d, "meta.json"), meta)
    return {"task_id": task_id, "state": "running", "dir": d}


_REAP: dict = {}    # Popen handles of runners this server started: polled so an exited runner is reaped, not a zombie


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False                            # a pid owned by someone else is not our runner (pid reuse)
    return True


def _state(d: str):
    """(state, meta, result) where state is running / succeeded / failed / cancelled."""
    tid = os.path.basename(d)
    if tid in _REAP and _REAP[tid].poll() is not None:
        _REAP.pop(tid, None)
    meta = _read_json(os.path.join(d, "meta.json")) or {}
    res = _read_json(os.path.join(d, "result.json"))
    if res is None:
        if _alive(meta.get("runner_pid")):
            return "running", meta, None
        if not meta:
            return "running", meta, None        # start() is between mkdir and writing meta.json
        return "failed", meta, {"error": "the runner exited without recording a result (killed or crashed); see runner.log"}
    if res.get("cancelled"):
        return "cancelled", meta, res
    out = res.get("output") or {}
    if res.get("exit_code") == 0 and not out.get("is_error"):
        return "succeeded", meta, res
    return "failed", meta, res


def _tail(path: str, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def status(task_id, scrub=lambda s: s) -> dict:
    d = task_path(task_id)
    state, meta, res = _state(d)
    started = meta.get("started_at") or os.path.getmtime(d)
    ended = (res or {}).get("ended_at")
    out = {"task_id": task_id, "state": state, "elapsed_s": round((ended or time.time()) - started, 1),
           "cwd": meta.get("cwd"), "orchestrator": meta.get("orchestrator"),
           "permission_mode": meta.get("permission_mode"), "dir": d,
           "stdout_log": os.path.join(d, "stdout.log"), "stderr_log": os.path.join(d, "stderr.log")}
    if res is not None:
        o = res.get("output") or {}
        out["exit_code"] = res.get("exit_code")
        if res.get("timed_out"):
            out["timed_out"] = True
        if o:
            text = o.get("result")
            text = text if isinstance(text, str) else ""
            out["result"] = scrub(text[:RESULT_CHARS])
            if len(text) > RESULT_CHARS:
                out["result_truncated"] = f"{len(text)} characters; the full text is in stdout_log"
            usage = o.get("modelUsage")
            if isinstance(usage, dict):
                out["models"] = sorted(str(k) for k in usage)
            for k in ("num_turns", "total_cost_usd", "session_id", "subtype"):
                if k in o:
                    out[k] = o[k]
        elif res.get("output_error"):
            out["output_error"] = res["output_error"]
        if res.get("error"):
            out["error"] = res["error"]
        if state != "succeeded":
            out["stderr_tail"] = scrub(_tail(os.path.join(d, "stderr.log"), LOG_TAIL_CHARS))
    return out


def wait(task_id, timeout_s=WAIT_MAX_S, scrub=lambda s: s, poll_s=0.5) -> dict:
    try:
        t = float(timeout_s)
    except (TypeError, ValueError):
        raise TaskError("timeout_s must be a number of seconds") from None
    if t < 0 or t > WAIT_MAX_S:
        raise TaskError(f"timeout_s must be between 0 and {WAIT_MAX_S} (MCP clients time out longer tool calls); "
                        "call wait_task again to keep waiting")
    d = task_path(task_id)
    end = time.monotonic() + t
    while _state(d)[0] == "running" and time.monotonic() < end:
        time.sleep(poll_s)
    return status(task_id, scrub)


def list_recent(n=20) -> list:
    try:
        n = max(1, min(int(n), 200))            # bounded: the listing goes into a tool result
    except (TypeError, ValueError):
        raise TaskError("limit must be an integer") from None
    root = _private_root()
    ids = sorted((x for x in os.listdir(root) if TASK_ID_RE.match(x)), reverse=True)[:n]
    rows = []
    for tid in ids:
        state, meta, res = _state(os.path.join(root, tid))
        rows.append({"task_id": tid, "state": state, "cwd": meta.get("cwd"), "orchestrator": meta.get("orchestrator"),
                     "task_preview": meta.get("task_preview")})
    return rows


def cancel(task_id) -> dict:
    d = task_path(task_id)
    state, meta, _ = _state(d)
    if state != "running":
        return {"task_id": task_id, "state": state, "cancelled": False, "reason": f"task is already {state}"}
    pid = meta.get("runner_pid")
    if not _alive(pid):
        return {"task_id": task_id, "state": "failed", "cancelled": False, "reason": "runner is not running"}
    try:
        if os.getpgid(pid) != pid:
            # The runner is a session leader, so its pgid equals its pid; anything else means the pid was reused.
            return {"task_id": task_id, "state": state, "cancelled": False,
                    "reason": "pid no longer leads the task's process group; refusing to signal it"}
    except ProcessLookupError:
        return {"task_id": task_id, "state": "failed", "cancelled": False, "reason": "runner is not running"}
    open(os.path.join(d, "cancelled"), "w").close()
    os.killpg(pid, signal.SIGTERM)              # this task's process group only: the runner and its claude child
    return {"task_id": task_id, "state": "cancelling", "cancelled": True}


# --- runner (detached child process) -------------------------------------------------------------------------------

def _run(d: str) -> int:
    spec = _read_json(os.path.join(d, "cmd.json"))
    result = {"exit_code": None, "cancelled": False}
    proc = None

    def on_term(signum, frame):
        if proc is not None and proc.poll() is None:
            proc.terminate()
    signal.signal(signal.SIGTERM, on_term)
    try:
        with open(os.path.join(d, "prompt.txt"), "rb") as stdin, \
                open(os.path.join(d, "stdout.log"), "ab") as so, open(os.path.join(d, "stderr.log"), "ab") as se:
            proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdin=stdin, stdout=so, stderr=se)
            try:
                result["exit_code"] = proc.wait(timeout=spec.get("max_s") or CHILD_MAX_S)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                result["error"] = f"child exceeded {spec.get('max_s')} s (CORTADOS_CHILD_MAX_S) and was terminated"
                proc.terminate()
                try:
                    result["exit_code"] = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    result["exit_code"] = proc.wait()
    except OSError as exc:
        result["error"] = f"could not start the child: {type(exc).__name__}: {exc}"
    result["cancelled"] = os.path.exists(os.path.join(d, "cancelled"))
    raw = ""
    try:
        with open(os.path.join(d, "stdout.log"), encoding="utf-8", errors="replace") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        pass
    if raw:
        try:
            out = json.loads(raw.splitlines()[-1]) if not raw.startswith("{") else json.loads(raw)
            result["output"] = out if isinstance(out, dict) else {}
        except ValueError:
            result["output_error"] = "stdout is not the JSON result claude -p --output-format json prints; see stdout_log"
    result["ended_at"] = time.time()
    _write_json(os.path.join(d, "result.json"), result)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        sys.exit(_run(sys.argv[2]))
    print("usage: python3 -m cortados_router.child_tasks --run <task dir>", file=sys.stderr)
    sys.exit(2)
