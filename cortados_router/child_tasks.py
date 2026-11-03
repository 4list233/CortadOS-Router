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

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # The runner is started by file path (not `-m`), so no PYTHONPATH pointing at this repo leaks into the child's tool
    # environment (it would shadow `tests`, `s1`, `cortados_router` in unrelated repos); put the root on sys.path here.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cortados_router import paths, redact, session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = "~/.config/cortados/tasks"
# Strict: the id becomes a path component (no traversal), and it is the child's x-cortados-run-id, so it must also
# match the provider's run-id pattern [A-Za-z0-9_-]{8,64} (it does: 24 chars of digits, hex and one '-').
TASK_ID_RE = re.compile(r"t\d{14}-[0-9a-f]{8}")     # always used with fullmatch ($ would accept a trailing newline)
EVENT_LOG = "child-tasks.jsonl"     # under the router's state dir (paths.state_dir()): one line per lifecycle event
RUN_FETCH_TIMEOUT_S = 10            # the run record is a small in-memory lookup; do not hold task_status hostage
_urlopen = urllib.request.urlopen   # tests replace this
# Permission modes a child may run in (claude 2.1.316 `--permission-mode`). Nobody is there to answer a prompt, so:
#   auto (default): a safety classifier approves or denies each action, so an unattended child can run Bash/tests
#                   without a blanket bypass;
#   acceptEdits:    file edits in cwd go through, other approval-needing calls are denied;
#   manual:         every approval-needing call is denied (read-mostly; with --permission-prompts none nobody is asked);
#   dontAsk:        anything not pre-approved by permission rules (or `allowed_tools`) is denied without asking;
#   plan:           read-only planning.
# bypassPermissions is NOT offered: an unattended session with unreviewed shell access and the user's full MCP set is
# too much blast radius to enable by a tool argument; widen with `allowed_tools` instead.
PERMISSION_MODES = ("auto", "acceptEdits", "manual", "plan", "dontAsk")   # each is a 2.1.316 --permission-mode choice
DEFAULT_PERMISSION_MODE = "auto"
# claude.ai connectors are disabled in token-auth sessions (the child authenticates to the provider with a token), so
# the servers behind the user's connectors are loaded directly from this MCP config file when it exists.
CONNECTORS_CONFIG = "~/.config/cortados/connectors.mcp.json"
PRUNE_AGE_S = 14 * 24 * 3600        # finished task dirs (logs, prompts) accumulate; drop them after two weeks
MAX_TASK_CHARS = 200_000            # a prompt beyond this is a document dump, not a task; refuse rather than truncate
MAX_ALLOWED_TOOLS = 64              # an allowlist longer than this is not a reviewed allowlist
# Our bound, not claude's (it documents none): real patterns are short (`Bash(npm test:*)`, `mcp__server__tool`); 200
# leaves room for long MCP names while keeping a pasted blob out of argv and meta.json.
MAX_TOOL_PATTERN = 200
# Orchestrator: 'cortados', an alias, or a model id. The longest pinnable id is ~30 chars; 81 (1 + 80) is our bound
# that keeps a pasted blob out of argv, and the charset keeps it a plain model id.
ORCHESTRATOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,80}")
MIN_TOKEN_LEN = 8                   # same floor as subagent_mcp.route_task: shorter is not a provider token, and a
                                    # shorter one could not be redacted without mangling ordinary words
CHILD_MAX_S_DEFAULT = 3 * 3600      # an unattended child that never ends burns quota (override: CORTADOS_CHILD_MAX_S)
# Recovery runs inside status/list/prune/wait calls, so its TERM->KILL wait is short: a full KILL_GRACE_S per crashed
# task would stall every such call; a child that was going to exit cleanly on TERM does so within seconds.
RECOVERY_GRACE_S = 5
KILL_GRACE_S = 30                   # after SIGTERM to claude's process group, wait this long, then SIGKILL the group
META_GRACE_S = 60                   # a task dir with no meta.json older than this is a failed start, not "running"
RESULT_CHARS = 20_000               # result text returned through MCP; MCP clients choke on huge tool results
REDACT_FAILED_PLACEHOLDER = "(withheld: the runner could not redact this log)"
LOGS_WITHHELD_NOTE = ("logs withheld: the runner died before redaction, and the current credentials do not match the ones "
                      "this task was launched with. The raw stdout/stderr logs in the task dir contain the launch "
                      "credential and must not be read or shared.")
LOG_TAIL_CHARS = 2_000              # stderr tail shown on failure
WAIT_MAX_S = 540                    # MCP clients time a tool call out (~10 min); stay under it
NESTED_ENV = "CORTADOS_CHILD_TASK"  # set in every child: a child may not start children (runaway quota fan-out)
# Claude Code refuses to start inside another Claude Code session when it sees this; the child is a fresh session.
STRIP_ENV = ("CLAUDECODE",)
MANAGED_DIRS = session.MANAGED_SETTINGS_DIRS   # tests point this elsewhere
# Children load the same ~/.claude/CLAUDE.md as the user's top-level sessions, whose ORCHESTRATOR directive says to
# delegate via start_task/route_task; this tells the child unambiguously that it is the delegate.
# Native subagents are fine (S1 routes them); starting further child tasks or re-handing the whole task off is not.
CHILD_DIRECTIVE = ("You are a CortadOS CHILD TASK started by an orchestrator. Do the task yourself and completely. You may "
                   "use your own subagents for parts of it (they are routed by S1), but do not call start_task or hand "
                   "the whole task off again. Your final message is returned to the orchestrator: end with a concise "
                   "report of what you did, what you verified, and anything left undone.")


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
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
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
    # Unique temp file per write (concurrent writers never share one), then an atomic rename: no reader sees half a file.
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Redactor(redact.Redactor):
    """Removes provider credentials from model-visible output (the shared cortados_router.redact implementation).
    Every token must be at least MIN_TOKEN_LEN long (a shorter one is refused, not silently left in). Redact strings
    BEFORE serializing or truncating."""

    def __init__(self, tokens=()):
        toks = {t for t in tokens if t}
        if any(len(t) < MIN_TOKEN_LEN for t in toks):
            raise TaskError(f"a configured provider token is shorter than {MIN_TOKEN_LEN} characters; it is not a "
                            "provider token and could not be redacted safely (check CORTADOS_PROVIDER_TOKEN and the "
                            "token file)")
        super().__init__(toks)


def pin_model(name) -> str:
    """The ONE validation for a model name a tool accepts (start_task's orchestrator, route_task's model): 'cortados'
    (S1 routes) or a pinnable model or alias (fable, opus, sonnet, claude-*, gpt-*, ...). Returns the provider model id:
    'cortados' or 'sub/<id>' (session.resolve_model, which uses the provider's own alias table)."""
    if not isinstance(name, str) or not ORCHESTRATOR_RE.fullmatch(name):
        raise TaskError(f"invalid model {name!r}: use 'cortados' or a pinnable model name (e.g. fable, opus, gpt-6.1-sol)")
    try:
        return session.resolve_model(name)
    except session.SessionError as exc:
        raise TaskError(str(exc)) from None


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
    pin_model(orchestrator)
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


def prune(root: str, now: float | None = None, errors: list | None = None) -> list:
    """Remove task dirs whose task started more than PRUNE_AGE_S ago and is not running. Returns the removed ids. Each
    dir is handled on its own: one that cannot be read or removed is recorded in `errors` and skipped, so it never
    blocks every later start_task."""
    cutoff = (time.time() if now is None else now) - PRUNE_AGE_S
    removed = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        try:
            if not TASK_ID_RE.fullmatch(name) or os.path.islink(d) or not os.path.isdir(d):
                continue
            state, meta, _ = _state(d)
            started = meta.get("started_at")
            if not isinstance(started, (int, float)):
                started = os.path.getmtime(d)
            if state != "running" and started < cutoff:
                shutil.rmtree(d)
                removed.append(name)
        except OSError as exc:
            if errors is not None:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
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
    return {k: data[k] for k in ("model", "label", "provider", "models", "builder_models", "refusals") if k in data}


def _max_s() -> int:
    raw = os.environ.get("CORTADOS_CHILD_MAX_S")
    if raw is None or raw.strip() == "":
        return CHILD_MAX_S_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        raise TaskError(f"CORTADOS_CHILD_MAX_S={raw!r} is not a whole number of seconds") from None
    if v <= 0:
        raise TaskError(f"CORTADOS_CHILD_MAX_S={raw!r} must be positive")
    return v


MAX_CONNECTORS = 32                 # a selection longer than this is not a selection


def select_connectors(names) -> dict | None:
    """The connector servers a child gets: None for none (the default), else {name: server config} for exactly the
    named servers in the connectors MCP config. Why opt-in: every connected server's tool schemas go up front in
    every request (a custom provider URL gets no client-side tool search), and the full set measured ~1.1 MB (~290k
    tokens, 495 MCP tools) on 2026-11-02, over a 200k model's input ceiling: the provider refused every such child
    with 413 before it ran."""
    if names is None or names == []:
        return None
    if not isinstance(names, list) or len(names) > MAX_CONNECTORS or not all(isinstance(n, str) and n for n in names):
        raise TaskError(f"connectors must be a list of at most {MAX_CONNECTORS} server names from "
                        f"{connectors_config()}")
    path = connectors_config()
    try:
        with open(path, encoding="utf-8") as f:
            servers = (json.load(f) or {}).get("mcpServers") or {}
    except FileNotFoundError:
        raise TaskError(f"connectors were requested but {path} does not exist") from None
    except (OSError, ValueError, AttributeError) as exc:
        raise TaskError(f"cannot read the connectors MCP config {path}: {type(exc).__name__}") from None
    unknown = [n for n in names if n not in servers]
    if unknown:
        raise TaskError(f"unknown connector(s) {', '.join(unknown)}; available: {', '.join(sorted(servers)) or 'none'}")
    return {n: servers[n] for n in dict.fromkeys(names)}


def start(task, cwd, orchestrator="cortados", permission_mode=DEFAULT_PERMISSION_MODE, allowed_tools=None,
          provider_url="", token_file=None, chrome=False, red=None, connectors=None) -> dict:
    """`red` carries the launch credentials (env token and token file's token as they are now)."""
    if os.environ.get(NESTED_ENV):
        raise TaskError("start_task is not available inside a child task: children may not start children "
                        "(unbounded fan-out would spend subscription quota unattended)")
    tools = _validate(task, cwd, orchestrator, permission_mode, allowed_tools)
    selected = select_connectors(connectors)
    if not isinstance(chrome, bool):
        raise TaskError("chrome must be true or false")
    if not provider_url:
        raise TaskError("start_task is not configured: set CORTADOS_PROVIDER_URL for the cortados-route MCP server")
    max_s = _max_s()
    exe = claude_bin()
    root = _private_root()
    prune_errors: list = []
    prune(root, errors=prune_errors)
    # No cap on parallel children, chrome ones included: capacity is the router's job (S1 and the quota gates refuse or
    # redirect a spent lane), and Claude in Chrome gives each session its own tab group.
    task_id = _new_id()
    d = os.path.join(root, task_id)
    os.mkdir(d, 0o700)
    # The runner's liveness IS this lock. It is taken before anything about the task is published (so no reader ever
    # sees meta.json with an unheld lock and mistakes a starting task for a crashed one), handed to the runner as an
    # inherited fd (same open file description, so the lock moves with it, no gap), and held by the runner until it has
    # written result.json. A pid alone can be reused.
    lock_fd = os.open(os.path.join(d, "runner.lock"), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        meta, env, cmd = _prepare(d, task_id, task, cwd, orchestrator, permission_mode, tools, provider_url, token_file,
                                  red or Redactor(),
                                  chrome, max_s, exe, selected)
        with open(os.path.join(d, "runner.log"), "ab") as rlog:
            runner = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--run", d,
                                       "--lock-fd", str(lock_fd)],
                                      cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=rlog, start_new_session=True, pass_fds=(lock_fd,))
    except BaseException as exc:
        # Pre-spawn failure: nothing runs, so roll back completely (a half-made dir would list forever and could hold
        # a token-bearing settings file).
        os.close(lock_fd)
        shutil.rmtree(d, ignore_errors=True)
        if isinstance(exc, OSError):
            raise TaskError(f"could not start the child task ({type(exc).__name__}: {exc})") from None
        raise
    os.close(lock_fd)                           # our copy only; the runner's inherited fd keeps the lock
    try:
        meta["runner_pid"] = runner.pid
        meta["event_log_error"] = log_event("started", **_event_fields(meta))
        _write_json(os.path.join(d, "meta.json"), meta)
    except BaseException as exc:
        # Post-spawn failure: a runner (and maybe claude) is live. Stop and reap them before deleting the dir, so no
        # task keeps running undiscoverable.
        _abort_runner(runner, d)
        shutil.rmtree(d, ignore_errors=True)
        raise TaskError(f"child task bookkeeping failed after spawn; the task was stopped ({type(exc).__name__}: "
                        f"{exc})") from None
    _REAP[task_id] = runner
    out = {"task_id": task_id, "state": "running", "dir": d}
    if prune_errors:
        out["prune_errors"] = prune_errors[:20]     # bounded: it goes into a tool result
    return out


def _abort_runner(runner, d: str) -> None:
    """Stop a runner this process just started: SIGTERM (it stops claude's group itself), then SIGKILL it and claude's
    recorded group if it does not exit in time."""
    try:
        runner.terminate()
        runner.wait(timeout=KILL_GRACE_S + 10)
    except subprocess.TimeoutExpired:
        runner.kill()
        runner.wait()
    _stop_recorded_child(d, 0)


def _prepare(d, task_id, task, cwd, orchestrator, permission_mode, tools, provider_url, token_file, red, chrome, max_s,
             exe, selected=None):
    """Everything before the spawn: settings, prompt, cmd.json, meta.json. Returns (meta, env, cmd)."""
    os.chmod(d, 0o700)                          # mkdir's mode is masked by umask; the launcher verifies exactly 0700
    launch = ["--orchestrator", orchestrator, "--provider-url", provider_url, "--settings-dir", d, "--run-id", task_id]
    if token_file:
        launch += ["--token-file", token_file]
    # Never the whole-session ORCHESTRATOR directive or its delegation gate, whatever the orchestrator: a child is the
    # delegate and does the work itself (CHILD_DIRECTIVE). A pinned orchestrator only pins the child's main model; its
    # native subagents are still routed by S1 (CLAUDE_CODE_SUBAGENT_MODEL).
    launch += ["--no-delegate-directive", "--no-gate"]
    # --permission-prompts none: nobody is there to answer, so anything that would prompt is denied deterministically
    # (as s1/outcome/agents.py runs its headless claude).
    extra = ["-p", "--output-format", "json", "--permission-mode", permission_mode, "--permission-prompts", "none"]
    if tools:
        extra += ["--allowedTools", ",".join(tools)]
    # Opt-in per task, enforced both ways: --chrome when asked (the child acts in the user's real, signed-in Chrome, in
    # its own tab group), else an explicit --no-chrome, so a claudeInChromeDefaultEnabled setting cannot turn it on.
    extra.append("--chrome" if chrome else "--no-chrome")
    conn, conn_note = None, None
    if selected:
        # Only the named servers, in a private file (session-*.json: removed with the settings when the child exits,
        # since server entries may carry their own credentials).
        conn = os.path.join(d, "session-connectors.json")
        with open(os.open(conn, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": selected}, f)
        extra += ["--mcp-config", conn]
    else:
        conn_note = "no connectors selected; the child has the user's Claude Code MCP servers only\n"
    base = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    try:
        _args, env, cmd = session.prepare(launch + ["--"] + extra, base, MANAGED_DIRS)
    except session.SessionError as exc:
        raise TaskError(f"cannot prepare the child session: {exc}") from None
    cmd[0] = exe
    _add_child_directive(cmd)
    env[NESTED_ENV] = "1"
    # Fingerprints (sha256, never the secrets) of the credentials this child runs with: if the runner dies before it
    # redacts the logs, they may be shown later only when today's credentials provably cover all of these.
    launch_fps = credential_fingerprints(launch_tokens(env))
    if conn_note:
        with open(os.path.join(d, "runner.log"), "a", encoding="utf-8") as f:
            f.write(conn_note)
    with open(os.open(os.path.join(d, "prompt.txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
              encoding="utf-8") as f:
        f.write(task)
    meta = {"task_id": task_id, "cwd": cwd, "orchestrator": orchestrator, "permission_mode": permission_mode,
            "allowed_tools": tools, "task_chars": len(task), "task_preview": red.text(task.strip())[:200],   # redact, THEN slice
            "started_at": time.time(), "connectors": sorted(selected) if selected else [],
            "chrome": chrome, "max_s": max_s, "runner_pid": None, "cred_fingerprints": launch_fps,
            # argv carries only the model, settings path and flags; the token lives in the 0600 settings file and env.
            "argv": cmd[1:]}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, "cwd": cwd, "max_s": max_s, "grace_s": KILL_GRACE_S,
                                              "event": _event_fields(meta), "started_at": meta["started_at"]})
    _write_json(os.path.join(d, "meta.json"), meta)    # before Popen: a task dir always has its meta
    return meta, env, cmd


def _add_child_directive(cmd: list) -> None:
    """Append CHILD_DIRECTIVE to the system prompt, LAST. Order: claude's own prompt, then CLAUDE.md, then the
    --append-system-prompt text; within that text a pinned orchestrator's whole-session directive comes first and the
    child directive after it, so the child directive is the final word. One flag only: the launcher's value is
    extended rather than a second --append-system-prompt added (whether a repeated flag concatenates or replaces is
    not documented)."""
    if "--append-system-prompt" in cmd:
        i = cmd.index("--append-system-prompt") + 1
        cmd[i] = cmd[i] + "\n\n" + CHILD_DIRECTIVE
    else:
        cmd += ["--append-system-prompt", CHILD_DIRECTIVE]


def _event_fields(meta: dict) -> dict:
    return {k: meta.get(k) for k in ("task_id", "cwd", "orchestrator", "permission_mode", "chrome")}


_REAP: dict = {}    # Popen handles of runners this server started: polled so an exited runner is reaped, not a zombie


def _lock_held(d: str):
    """True if a live runner holds <d>/runner.lock, False if not (the lock was acquirable), None if there is no lock
    file. The probe takes the lock for an instant only."""
    try:
        fd = os.open(os.path.join(d, "runner.lock"), os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)                            # closing releases a lock we took
    return False


def _is_our_runner(pid, d: str) -> bool:
    """The process `pid` is this task's runner: its command line names `--run <d>`. Checked with the lock before any
    signal, so a reused pid (after a reboot or SIGKILL) is never signalled."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        cmdline = subprocess.run([PS, "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f"--run {d} " in cmdline + " "


def _state(d: str):
    """(state, meta, result) where state is running / succeeded / failed / cancelled."""
    tid = os.path.basename(d)
    if tid in _REAP and _REAP[tid].poll() is not None:
        _REAP.pop(tid, None)
    meta = _read_json(os.path.join(d, "meta.json"))
    res = _read_json(os.path.join(d, "result.json"))
    if res is None and meta is None:
        if time.time() - os.path.getmtime(d) < META_GRACE_S:
            return "running", {}, None          # start() is between mkdir and writing meta.json
        return "failed", {}, {"error": "the task never started (no meta.json)"}
    meta = meta or {}
    if res is None:
        res = _recover(d, meta)
        if res is None:
            return "running", meta, None
    return classify(res), meta, res


def classify(res: dict) -> str:
    """Final state from a result record. Cancel, timeout and any recorded error override the exit status: a child
    that exits 0 on the SIGTERM sent at its timeout did not succeed."""
    if res.get("cancelled"):
        return "cancelled"
    if res.get("timed_out") or res.get("error") or res.get("output_error") or res.get("redact_error"):
        return "failed"
    # Success needs exit 0 AND a valid claude result record that says it is not an error (parse_output's rules):
    # empty stdout, malformed JSON, a scalar or a record of the wrong shape is a failure, not a quiet success.
    out = res.get("output")
    if (res.get("exit_code") == 0 and isinstance(out, dict) and out.get("type") == "result"
            and out.get("is_error") is False and isinstance(out.get("result"), str)):
        return "succeeded"
    return "failed"


def _recover(d: str, meta: dict):
    """No result.json yet: None while the task runs, else the result of a runner that died without writing one. The
    whole check-and-write runs holding the task lock, so it is serialized against the runner and other recoverers."""
    try:
        fd = os.open(os.path.join(d, "runner.lock"), os.O_RDWR)
    except FileNotFoundError:
        if time.time() - meta.get("started_at", 0) < META_GRACE_S:
            return None                         # a task from before the lock existed, still within its start grace
        fd = None
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            # Held: by the live runner, or by another recoverer right now; either way, read what is there.
            return _read_json(os.path.join(d, "result.json"))
    try:
        res = _read_json(os.path.join(d, "result.json"))    # recheck under the lock: the runner may just have finished
        if res is not None:
            return res
        # The runner died without writing result.json: its claude group may still be running (stop it before the
        # terminal state is published, or it could never be cancelled), and it never removed the settings file.
        child = _stop_recorded_child(d, RECOVERY_GRACE_S)
        removed = _remove_settings(d)
        if os.path.exists(os.path.join(d, "cancelled")):
            res = {"cancelled": True, "settings_removed": removed, "ended_at": time.time(),
                   "error": "cancelled before the runner recorded a result; no child output"}
        else:
            res = {"error": "the runner exited without recording a result (killed or crashed); see runner.log",
                   "settings_removed": removed, "ended_at": time.time()}
        res["child_cleanup"] = child
        res["logs_unredacted"] = True           # the runner never redacted them; see status()
        _write_json(os.path.join(d, "result.json"), res)
        return res
    finally:
        if fd is not None:
            os.close(fd)


def _tail(path: str, n: int, red: "Redactor") -> str:
    """The last ~n characters, redacted: whole tokens are replaced first, then a token fragment at the cut (the read
    may start mid-token) is masked, and only then is the text trimmed."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - n)
            f.seek(start)
            text = red.text(f.read().decode("utf-8", "replace"))
    except FileNotFoundError:
        return ""
    return red.leading_fragment(text) if start > 0 else text


def _release_recovered_logs(d: str, meta: dict, res: dict, red: "Redactor") -> bool:
    """A crashed runner left its logs unredacted. They may be shown only after redaction with credentials that
    provably include every launch credential (by fingerprint); then they are redacted on disk and released."""
    launch = set(meta.get("cred_fingerprints") or [])
    if not launch or not launch <= set(credential_fingerprints(red.raw)):
        return False
    try:
        for name in ("stdout.log", "stderr.log"):
            redact_file(os.path.join(d, name), red)
    except OSError:
        return False
    res["logs_unredacted"] = False
    _write_json(os.path.join(d, "result.json"), res)
    return True


def status(task_id, red=None, provider_url="", token="") -> dict:
    red = red or Redactor((token,) if token else ())
    d = task_path(task_id)
    state, meta, res = _state(d)
    withheld = bool(res and res.get("logs_unredacted")) and not _release_recovered_logs(d, meta, res, red)
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
            out["result"] = red.text(text)[:RESULT_CHARS]   # redact BEFORE truncating: a cut never splits a token
            if len(text) > RESULT_CHARS:
                out["result_truncated"] = f"{len(text)} characters; the full text is in stdout_log"
            usage = o.get("modelUsage")
            if isinstance(usage, dict):
                out["models"] = sorted(str(k) for k in usage)
            for k in ("num_turns", "total_cost_usd", "session_id", "subtype"):
                if k in o:
                    out[k] = o[k]
        if res.get("output_error"):
            out["output_error"] = res["output_error"]     # reported even when a (malformed) record was parsed
        if res.get("redact_error"):
            out["redact_error"] = res["redact_error"]
        if res.get("error"):
            out["error"] = res["error"]
        # The router's own record of what this child was routed to (its requests carried x-cortados-run-id: task_id).
        out["router_run"] = fetch_run(provider_url, token, task_id)
        # Why the provider refused (Claude Code replaces a 413's message with its own "images" text, so the child's
        # result never says): the most recent refusal recorded under this run, plus a hint for the known cause.
        refusals = out["router_run"].get("refusals") if isinstance(out["router_run"], dict) else None
        if refusals:
            last = refusals[-1]
            detail = f" ({last['detail']})" if last.get("detail") else ""
            out["provider_refusal"] = f"HTTP {last.get('status')} {last.get('type')}: {last.get('reason')}{detail}"
        api_status = o.get("api_error_status") if isinstance(o, dict) else None
        if api_status is not None:
            out["api_error_status"] = api_status
        if api_status == 413 or (refusals and refusals[-1].get("status") == 413):
            out["hint"] = ("the request was too large for the model: usually the tool schemas of the connected MCP "
                           "servers; start the task with fewer `connectors` (default none)")
        if state != "succeeded":
            # A log the runner could not redact is never shown, not even through this call's own redaction (it only
            # knows today's credentials).
            out["stderr_tail"] = (REDACT_FAILED_PLACEHOLDER if res.get("redact_error") else
                                  LOGS_WITHHELD_NOTE if withheld else
                                  _tail(os.path.join(d, "stderr.log"), LOG_TAIL_CHARS, red))
        if withheld:
            out["logs_withheld"] = LOGS_WITHHELD_NOTE
            # No pointer to the files that hold the raw credential: not the logs, and not the task dir that holds them.
            for k in ("result", "stdout_log", "stderr_log", "dir"):
                out.pop(k, None)
    return red.tree(out)                        # every string (structured, before any serialization)


def wait(task_id, timeout_s=WAIT_MAX_S, red=None, poll_s=0.5, provider_url="", token="") -> dict:
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
    return status(task_id, red, provider_url, token)


def list_recent(n=20) -> list:
    try:
        n = max(1, min(int(n), 200))            # bounded: the listing goes into a tool result
    except (TypeError, ValueError):
        raise TaskError("limit must be an integer") from None
    root = _private_root()
    ids = sorted((x for x in os.listdir(root) if TASK_ID_RE.fullmatch(x)), reverse=True)[:n]
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
    # Signal ONLY while the runner's lock is held AND the pid's command line is this task's runner: a recorded pid can
    # belong to an unrelated session leader after a reboot or a SIGKILL.
    if not _lock_held(d) or not _is_our_runner(pid, d):
        return {"task_id": task_id, "state": state, "cancelled": False,
                "reason": "the recorded pid is not this task's runner; refusing to signal it"}
    open(os.path.join(d, "cancelled"), "w").close()
    try:
        os.kill(pid, signal.SIGTERM)            # the runner stops claude's own process group (SIGTERM, then SIGKILL)
    except ProcessLookupError:
        return {"task_id": task_id, "state": "failed", "cancelled": False, "reason": "runner is not running"}
    err = log_event("cancelled", **_event_fields(meta))
    out = {"task_id": task_id, "state": "cancelling", "cancelled": True}
    if err:
        out["event_log_error"] = err
    return out


# --- runner (detached child process) -------------------------------------------------------------------------------

def _killpg(pgid: int, sig) -> bool:
    """Signal claude's process group; False once the group is empty."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


PS = "/bin/ps"           # absolute: MCP servers start with a thin PATH, and an unfound ps must never look like "no process"
UNKNOWN = object()       # _proc_start could not tell (ps failed or hung): never a reason to signal


def _probe_group(pgid: int) -> str:
    """'alive', 'gone', or 'foreign' (EPERM: the group exists but belongs to another user, so the id was reused)."""
    try:
        os.killpg(pgid, 0)
        return "alive"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "foreign"


def _proc_start(pid: int):
    """The process's start time as ps prints it (lstart); None if ps ran and found no such process; UNKNOWN if ps could
    not be run or timed out. With the pid it names one process: a reused pid has a different start time."""
    try:
        # timeout guards against a hung ps; the same bound as _is_our_runner's ps call
        r = subprocess.run([PS, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN
    out, err = r.stdout.strip(), (r.stderr or "").strip()
    if r.returncode == 0 and out:
        return out
    if r.returncode == 1 and not out and not err:
        return None                             # macOS ps for a pid with no process: exit 1, nothing printed
    return UNKNOWN                              # any other outcome (a signal, a diagnostic) is a failed ps, not "gone"


def _child_record_writer(d: str):
    """preexec_fn for claude's Popen: runs in the forked child AFTER setsid (start_new_session) and BEFORE exec, so
    child.json names the new group before Popen even returns: no window in which a dying runner leaves no record.
    Plain os calls only (it runs between fork and exec); the start time is added by _note_child afterwards."""
    path = os.path.join(d, "child.json")

    def write():
        pid = os.getpid()
        tmp = f"{path}.{pid}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps({"pgid": pid, "start": None}).encode())
        finally:
            os.close(fd)
        os.rename(tmp, path)
    return write


def _note_child(d: str, pgid: int) -> None:
    # pid + start time: lets start() or crash recovery stop exactly this group, never a reused pid's. (The child's
    # preexec_fn already wrote the pgid before Popen returned; the pgid-only rewrite here keeps the record correct even
    # if that write was skipped, then the start time is added.)
    path = os.path.join(d, "child.json")
    _write_json(path, {"pgid": pgid, "start": None})
    start = _proc_start(pgid)
    _write_json(path, {"pgid": pgid, "start": start if isinstance(start, str) else None})


def _stop_recorded_child(d: str, grace: float) -> str:
    """Stop the claude process group a dead or aborted runner left behind. Safe against pid reuse: if the leader is
    alive it must have the recorded start time; if the leader is gone but the group still has members, the group id
    cannot have been reused (a pid is not reissued while it names a live process group). TERM, then KILL after grace.
    Returns what was done."""
    rec = _read_json(os.path.join(d, "child.json"))
    if not isinstance(rec, dict) or not isinstance(rec.get("pgid"), int) or rec["pgid"] <= 1:
        return "no child recorded"
    pgid = rec["pgid"]
    probe = _probe_group(pgid)
    if probe == "gone":
        return "child group already gone"
    if probe == "foreign":
        return "pgid reused by another user's process; not signalled"
    now_start = _proc_start(pgid)
    if now_start is UNKNOWN:
        return "start time unknown (ps failed); not signalled; the child may still be running"
    if now_start is not None:                   # the leader is alive: it must be the very process recorded
        if rec.get("start") is None:
            return "start time unknown (not recorded); not signalled; the child may still be running"
        if now_start != rec["start"]:
            return "pid reused by another process; not signalled"
    # (leader gone but group alive: the id cannot have been reused while the group exists)
    _killpg(pgid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end and _probe_group(pgid) == "alive":
        time.sleep(0.1)
    if _probe_group(pgid) == "alive":
        _killpg(pgid, signal.SIGKILL)
    return "child group stopped"


def _stop_group(proc, pgid: int, grace: float) -> None:
    """SIGTERM claude's process group, wait up to `grace` for claude and the rest of the group to exit, then SIGKILL the
    group; claude is reaped either way."""
    _killpg(pgid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if proc.poll() is not None and not _killpg(pgid, 0):
            return
        time.sleep(0.1)
    _killpg(pgid, signal.SIGKILL)               # escalate: something ignored SIGTERM
    proc.wait()


LAUNCH_TOKEN_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CORTADOS_PROVIDER_TOKEN")


def launch_tokens(env) -> list:
    """The credentials in a launch environment (the runner's env is the child's launch env). Never written anywhere."""
    return [env.get(k, "") for k in LAUNCH_TOKEN_KEYS if len(env.get(k, "")) >= MIN_TOKEN_LEN]


def _launch_tokens() -> list:
    return launch_tokens(os.environ)


def credential_fingerprints(tokens) -> list:
    return sorted({hashlib.sha256(t.encode("utf-8")).hexdigest() for t in tokens if t})


def redact_file(path: str, red: "Redactor", chunk: int = 1 << 20) -> None:
    """Rewrite `path` with every token replaced, streaming (a carried overlap keeps a token split across chunks whole),
    via a unique temp file and an atomic rename."""
    if not red.tokens or not os.path.exists(path):
        return
    longest = max(len(t) for t in red.tokens)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as src, \
                os.fdopen(fd, "w", encoding="utf-8", newline="") as dst:
            buf = ""
            while True:
                block = src.read(chunk)
                buf += block
                # Decide matches only where a longest credential would fit entirely inside the buffer: a match that
                # starts before `cut` is final; text from the first undecided position on is carried, still raw.
                cut = len(buf) if not block else max(0, len(buf) - longest + 1)
                out, pos = [], 0
                for m in red._re.finditer(buf):
                    if m.start() >= cut:
                        break
                    out.append(buf[pos:m.start()])
                    out.append("***")
                    pos = m.end()
                emit_to = max(pos, cut)
                out.append(buf[pos:emit_to])
                dst.write("".join(out))
                buf = buf[emit_to:]
                if not block:
                    break
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def parse_output(raw: str):
    """(output, error): claude's `-p --output-format json` result record, or why stdout is not one. A valid record is
    an object with type "result", a boolean is_error and a string result."""
    raw = raw.strip()
    if not raw:
        return None, "the child printed nothing on stdout (no result record)"
    try:
        out = json.loads(raw) if raw.startswith("{") else json.loads(raw.splitlines()[-1])
    except ValueError:
        return None, "stdout is not the JSON result claude -p --output-format json prints; see stdout_log"
    if not isinstance(out, dict):
        return None, f"stdout is JSON but not a result record ({type(out).__name__})"
    if out.get("type") != "result" or not isinstance(out.get("is_error"), bool) or not isinstance(out.get("result"), str):
        return out, "stdout is not a claude result record (needs type 'result', boolean is_error, string result)"
    return out, None


class _CancelledBeforeSpawn(Exception):
    pass


def _run(d: str, lock_fd=None) -> int:
    flag = {"term": False, "pre": False}
    holder = {"proc": None}

    def on_term(signum, frame):
        p = holder["proc"]
        if p is None:
            flag["pre"] = True                  # before or during the spawn: latched, acted on once proc exists
        elif p.poll() is None:
            flag["term"] = True                 # a live child is being stopped (a cancel racing a clean exit is not one)
    signal.signal(signal.SIGTERM, on_term)
    if lock_fd is None:                         # started by hand: take the lock ourselves
        lock_fd = os.open(os.path.join(d, "runner.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    spec = _read_json(os.path.join(d, "cmd.json"))
    max_s, grace = spec.get("max_s") or CHILD_MAX_S_DEFAULT, spec.get("grace_s") or KILL_GRACE_S
    result = {"exit_code": None, "cancelled": False}
    try:
        with open(os.path.join(d, "prompt.txt"), "rb") as stdin, \
                open(os.path.join(d, "stdout.log"), "ab") as so, open(os.path.join(d, "stderr.log"), "ab") as se:
            if flag["pre"] or os.path.exists(os.path.join(d, "cancelled")):
                raise _CancelledBeforeSpawn()
            # claude leads its own process group, so stopping it also stops the tools and servers it spawned.
            proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdin=stdin, stdout=so, stderr=se,
                                    start_new_session=True, preexec_fn=_child_record_writer(d))
            holder["proc"] = proc
            pgid = proc.pid
            try:
                if flag["pre"]:
                    flag["term"] = True         # the SIGTERM landed while Popen ran: stop the child now
                _note_child(d, pgid)
                deadline = time.monotonic() + max_s
                while proc.poll() is None:
                    if flag["term"] or time.monotonic() >= deadline:
                        if not flag["term"]:
                            result["timed_out"] = True
                            result["error"] = f"child exceeded {max_s} s (CORTADOS_CHILD_MAX_S) and was terminated"
                        _stop_group(proc, pgid, grace)
                        break
                    time.sleep(0.1)
                if _killpg(pgid, 0):
                    # On EVERY exit (clean, crash, stop): a tool process left in claude's group would be orphaned.
                    _stop_group(proc, pgid, grace)
            except BaseException as exc:
                # Anything failing after the spawn (even writing child.pid) must not orphan claude: stop its group first.
                result["error"] = f"runner error after the child started: {type(exc).__name__}: {exc}"
                _stop_group(proc, pgid, grace)
            result["exit_code"] = proc.returncode
            result["cancelled"] = flag["term"]
    except _CancelledBeforeSpawn:
        result["cancelled"] = True
        result["error"] = "cancelled before the child was started"
    except (OSError, subprocess.SubprocessError) as exc:   # SubprocessError: e.g. preexec_fn failed in the child
        result["error"] = f"could not start the child: {type(exc).__name__}: {exc}"
    finally:
        # The settings file holds the provider token; once the child has exited (normally, cancelled or timed out)
        # nothing needs it, so it does not stay at rest. (A SIGKILLed runner cannot do this; _state() does it then.)
        result["settings_removed"] = _remove_settings(d)
    # Redact the child's stored output with the exact launch credentials while the runner still has them: a later
    # status call only knows the token file as it is THEN (it may have been replaced or removed).
    red = Redactor([t for t in _launch_tokens() if len(t) >= MIN_TOKEN_LEN])
    for name in ("stdout.log", "stderr.log"):
        try:
            redact_file(os.path.join(d, name), red)
        except OSError as exc:
            result["redact_error"] = f"could not redact {name}: {type(exc).__name__}"
    raw = ""
    try:
        with open(os.path.join(d, "stdout.log"), encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except FileNotFoundError:
        pass
    out, err = parse_output(raw)
    if out is not None:
        result["output"] = red.tree(out)
    if err:
        result["output_error"] = err
    result["ended_at"] = time.time()
    final = classify(result)
    started = spec.get("started_at")
    result["event_log_error"] = log_event(
        "finished", **(spec.get("event") or {}), status=final, exit_code=result["exit_code"],
        timed_out=bool(result.get("timed_out")),
        duration_s=round(result["ended_at"] - started, 1) if isinstance(started, (int, float)) else None)
    _write_json(os.path.join(d, "result.json"), result)
    os.close(lock_fd)                           # release only after result.json exists
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) in (2, 4) and a[0] == "--run" and (len(a) == 2 or a[2] == "--lock-fd"):
        sys.exit(_run(a[1], int(a[3]) if len(a) == 4 else None))
    print("usage: python3 cortados_router/child_tasks.py --run <task dir> [--lock-fd N]", file=sys.stderr)
    sys.exit(2)
