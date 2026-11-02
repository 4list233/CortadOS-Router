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
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router import paths, session

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = "~/.config/cortados/tasks"
# Strict: the id becomes a path component (no traversal), and it is the child's x-cortados-run-id, so it must also
# match the provider's run-id pattern [A-Za-z0-9_-]{8,64} (it does: 24 chars of digits, hex and one '-').
TASK_ID_RE = re.compile(r"^t\d{14}-[0-9a-f]{8}$")
EVENT_LOG = "child-tasks.jsonl"     # under the router's state dir (paths.state_dir()): one line per lifecycle event
RUN_FETCH_TIMEOUT_S = 10            # the run record is a small in-memory lookup; do not hold task_status hostage
_urlopen = urllib.request.urlopen   # tests replace this
# Permission modes a child may run in (claude 2.1.316 `--permission-mode`). Nobody is there to answer a prompt, so:
#   auto (default): a safety classifier approves or denies each action, so an unattended child can run Bash/tests
#                   without a blanket bypass;
#   acceptEdits:    file edits in cwd go through, other approval-needing calls are denied;
#   default:        every approval-needing call is denied (read-mostly);
#   dontAsk:        anything not pre-approved by permission rules (or `allowed_tools`) is denied without asking;
#   plan:           read-only planning.
# bypassPermissions is NOT offered: an unattended session with unreviewed shell access and the user's full MCP set is
# too much blast radius to enable by a tool argument; widen with `allowed_tools` instead.
PERMISSION_MODES = ("auto", "acceptEdits", "default", "plan", "dontAsk")
DEFAULT_PERMISSION_MODE = "auto"
# claude.ai connectors are disabled in token-auth sessions (the child authenticates to the provider with a token), so
# the servers behind the user's connectors are loaded directly from this MCP config file when it exists.
CONNECTORS_CONFIG = "~/.config/cortados/connectors.mcp.json"
PRUNE_AGE_S = 14 * 24 * 3600        # finished task dirs (logs, prompts) accumulate; drop them after two weeks
MAX_TASK_CHARS = 200_000            # a prompt beyond this is a document dump, not a task; refuse rather than truncate
MAX_ALLOWED_TOOLS = 64              # an allowlist longer than this is not a reviewed allowlist
MAX_TOOL_PATTERN = 200
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


def connectors_config() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("CORTADOS_CONNECTORS_MCP_CONFIG") or CONNECTORS_CONFIG))


def prune(root: str, now: float | None = None) -> list:
    """Remove task dirs whose task started more than PRUNE_AGE_S ago and is not running. Returns the removed ids."""
    cutoff = (time.time() if now is None else now) - PRUNE_AGE_S
    removed = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not TASK_ID_RE.match(name) or os.path.islink(d) or not os.path.isdir(d):
            continue
        state, meta, _ = _state(d)
        started = meta.get("started_at")
        if not isinstance(started, (int, float)):
            started = os.path.getmtime(d)
        if state != "running" and started < cutoff:
            shutil.rmtree(d)
            removed.append(name)
    return removed


def _remove_settings(d: str) -> list:
    """Delete the launcher's token-bearing settings file(s) in a task dir; returns the names removed."""
    gone = []
    for name in os.listdir(d):
        if name.startswith("session-") and name.endswith(".json"):
            try:
                os.unlink(os.path.join(d, name))
                gone.append(name)
            except FileNotFoundError:
                pass
    return gone


def log_event(event: str, **fields):
    """Append one JSON line to $CORTADOS_HOME/child-tasks.jsonl. Only ids, settings, status and timings: never the prompt
    or a secret. Returns None, or the error text (the caller records it; a log failure must not kill a task)."""
    rec = {"ts": round(time.time(), 3), "event": event, **fields}
    try:
        d = paths.state_dir()
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / EVENT_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")   # one short write per line: O_APPEND keeps lines whole
    except OSError as exc:
        return f"could not append to the child-task event log: {type(exc).__name__}: {exc}"
    return None


def fetch_run(provider_url: str, token: str, run_id: str) -> dict:
    """The provider's record of this run (GET /v1/cortados/runs/<id>): models and lanes it routed to. Failures come back
    as {"error": ...}, never raised; the token never appears in the text."""
    scrub = (lambda s: s.replace(token, "***")) if token else (lambda s: s)
    if not (provider_url and token):
        return {"error": "provider URL or token not configured"}
    req = urllib.request.Request(provider_url.rstrip("/") + "/v1/cortados/runs/" + run_id, method="GET",
                                 headers={"x-api-key": token})
    try:
        with _urlopen(req, timeout=RUN_FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"error": f"provider returned HTTP {exc.code}" + (" (no routed request recorded under this run id)"
                                                                 if exc.code == 404 else "")}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": scrub(f"could not reach the provider ({type(exc).__name__}: {exc})")[:300]}
    if not isinstance(data, dict):
        return {"error": f"unexpected run record ({type(data).__name__})"}
    return {k: data[k] for k in ("model", "label", "provider", "models", "builder_models") if k in data}


def start(task, cwd, orchestrator="cortados", permission_mode=DEFAULT_PERMISSION_MODE, allowed_tools=None,
          provider_url="", token_file=None, chrome=False) -> dict:
    if os.environ.get(NESTED_ENV):
        raise TaskError("start_task is not available inside a child task: children may not start children "
                        "(unbounded fan-out would spend subscription quota unattended)")
    tools = _validate(task, cwd, orchestrator, permission_mode, allowed_tools)
    if not isinstance(chrome, bool):
        raise TaskError("chrome must be true or false")
    if not provider_url:
        raise TaskError("start_task is not configured: set CORTADOS_PROVIDER_URL for the cortados-route MCP server")
    exe = claude_bin()
    root = _private_root()
    prune(root)
    # No cap on parallel children, chrome ones included: capacity is the router's job (S1 and the quota gates refuse or
    # redirect a spent lane), and Claude in Chrome gives each session its own tab group.
    task_id = _new_id()
    d = os.path.join(root, task_id)
    os.mkdir(d, 0o700)
    os.chmod(d, 0o700)                          # mkdir's mode is masked by umask; the launcher verifies exactly 0700
    launch = ["--orchestrator", orchestrator, "--provider-url", provider_url, "--settings-dir", d, "--run-id", task_id]
    if token_file:
        launch += ["--token-file", token_file]
    if orchestrator == "cortados":
        # S1 already routes every turn; the delegate-first directive and its gate exist to keep a pinned top model
        # from doing the work itself, which does not apply here.
        launch += ["--no-delegate-directive", "--no-gate"]
    extra = ["-p", "--output-format", "json", "--permission-mode", permission_mode]
    if tools:
        extra += ["--allowedTools", ",".join(tools)]
    if chrome:
        extra.append("--chrome")    # opt-in per task: the child acts in the user's real, signed-in Chrome (own tab group)
    conn = connectors_config()
    conn_note = None
    if os.path.isfile(conn):
        extra += ["--mcp-config", conn]
    else:
        # Not an error: the child still gets the user's Claude Code MCP servers, just not the connector ones.
        conn_note = f"connectors MCP config {conn} not found; the child runs without connector servers\n"
    base = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    try:
        _args, env, cmd = session.prepare(launch + ["--"] + extra, base, MANAGED_DIRS)
    except session.SessionError as exc:
        shutil.rmtree(d, ignore_errors=True)
        raise TaskError(f"cannot prepare the child session: {exc}") from None
    cmd[0] = exe
    env[NESTED_ENV] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if conn_note:
        with open(os.path.join(d, "runner.log"), "a", encoding="utf-8") as f:
            f.write(conn_note)
    with open(os.open(os.path.join(d, "prompt.txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
              encoding="utf-8") as f:
        f.write(task)
    meta = {"task_id": task_id, "cwd": cwd, "orchestrator": orchestrator, "permission_mode": permission_mode,
            "allowed_tools": tools, "task_chars": len(task), "task_preview": task.strip()[:200],
            "started_at": time.time(), "connectors_config": conn if not conn_note else None,
            "chrome": chrome,
            # argv carries only the model, settings path and flags; the token lives in the 0600 settings file and env.
            "argv": cmd[1:]}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, "cwd": cwd, "max_s": CHILD_MAX_S,
                                                    # for the runner's "finished" event (meta.json may not exist yet)
                                                    "event": _event_fields(meta), "started_at": meta["started_at"]})
    try:
        with open(os.path.join(d, "runner.log"), "ab") as rlog:
            runner = subprocess.Popen([sys.executable, "-B", "-m", "cortados_router.child_tasks", "--run", d],
                                      cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=rlog, start_new_session=True)
    except OSError as exc:
        meta["settings_removed"] = _remove_settings(d)
        _write_json(os.path.join(d, "meta.json"), meta)
        _write_json(os.path.join(d, "result.json"), {"exit_code": None, "ended_at": time.time(),
                                                     "error": f"could not start the runner: {type(exc).__name__}"})
        raise TaskError(f"could not start the child runner ({type(exc).__name__}: {exc})") from None
    _REAP[task_id] = runner
    meta["runner_pid"] = runner.pid
    meta["event_log_error"] = log_event("started", **_event_fields(meta))
    _write_json(os.path.join(d, "meta.json"), meta)
    return {"task_id": task_id, "state": "running", "dir": d}


def _event_fields(meta: dict) -> dict:
    return {k: meta.get(k) for k in ("task_id", "cwd", "orchestrator", "permission_mode", "chrome")}


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
        # The runner died without writing result.json, so it never removed the token-bearing settings file either.
        removed = _remove_settings(d)
        if os.path.exists(os.path.join(d, "cancelled")):
            # cancel_task signalled it before it had installed its SIGTERM handler (it was still starting up)
            return "cancelled", meta, {"cancelled": True, "settings_removed": removed,
                                       "error": "cancelled while the runner was starting; no child output"}
        return "failed", meta, {"error": "the runner exited without recording a result (killed or crashed); see runner.log",
                                "settings_removed": removed}
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


def status(task_id, scrub=lambda s: s, provider_url="", token="") -> dict:
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
        out["settings_removed"] = bool(res.get("settings_removed"))
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
        # The router's own record of what this child was routed to (its requests carried x-cortados-run-id: task_id).
        out["router_run"] = fetch_run(provider_url, token, task_id)
        if state != "succeeded":
            out["stderr_tail"] = scrub(_tail(os.path.join(d, "stderr.log"), LOG_TAIL_CHARS))
    return out


def wait(task_id, timeout_s=WAIT_MAX_S, scrub=lambda s: s, poll_s=0.5, provider_url="", token="") -> dict:
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
    return status(task_id, scrub, provider_url, token)


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
    err = log_event("cancelled", **_event_fields(meta))
    out = {"task_id": task_id, "state": "cancelling", "cancelled": True}
    if err:
        out["event_log_error"] = err
    return out


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
    finally:
        # The settings file holds the provider token; once the child has exited (normally, cancelled or timed out)
        # nothing needs it, so it does not stay at rest. (A SIGKILLed runner cannot do this; prune() removes the dir.)
        result["settings_removed"] = _remove_settings(d)
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
    out = result.get("output") or {}
    final = ("cancelled" if result["cancelled"] else
             "succeeded" if result["exit_code"] == 0 and not out.get("is_error") else "failed")
    started = spec.get("started_at")
    result["event_log_error"] = log_event(
        "finished", **(spec.get("event") or {}), status=final, exit_code=result["exit_code"],
        timed_out=bool(result.get("timed_out")),
        duration_s=round(result["ended_at"] - started, 1) if isinstance(started, (int, float)) else None)
    _write_json(os.path.join(d, "result.json"), result)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        sys.exit(_run(sys.argv[2]))
    print("usage: python3 -m cortados_router.child_tasks --run <task dir>", file=sys.stderr)
    sys.exit(2)
