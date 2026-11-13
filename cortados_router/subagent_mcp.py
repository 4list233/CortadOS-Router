"""Stdio MCP server that gives a Claude Code (or any MCP client) session ONE tool, `route_task`, for delegating a
self-contained subtask to the CortadOS router. The main session keeps its own model; when it wants to offload work it
calls this tool, which posts the subtask to the `cortados` provider endpoint (S1 scores it, the router picks the
cheapest capable lane in the resource pool, and the chosen model answers), and returns the answer as text. It does not
replace the session's model or touch its login.

It also serves the child-task tools (start_task, task_status, wait_task, list_tasks, cancel_task): whole tasks run as
separate headless Claude Code sessions on this machine (see cortados_router/child_tasks.py, imported lazily because it
needs the package; route_task does not).

Stdlib only; runs standalone (the MCP client launches it as a subprocess over stdio); route_task imports nothing from the
package. It forwards to the provider — it does NOT need to sit next to S1 or the provider; it only needs to reach the
provider URL.

Register with Claude Code, e.g. (the token is read from ~/.config/cortados/provider-token by default, so only the URL
need be set; CORTADOS_PROVIDER_TOKEN or CORTADOS_PROVIDER_TOKEN_FILE override it):
  claude mcp add cortados-route \\
    --env CORTADOS_PROVIDER_URL=http://<provider-ip>:8432 \\
    -- python3 /path/to/cortados_router/subagent_mcp.py
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import os
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request
import urllib.parse

PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18"}   # only versions whose required behaviour (no mandatory JSON-RPC batching) we implement
DEFAULT_TIMEOUT_S = 300.0          # a routed subtask can take a while (cold lane, large output)
DEFAULT_MAX_TOKENS = 8000
MIN_TOKEN_LEN = 8                 # shorter is not a provider token (see route_task)

TOOL = {
    "name": "route_task",
    "description": ("Delegate ONE self-contained subtask to the CortadOS router and get the answer back as text. S1 "
                    "scores the subtask and routes it to the cheapest capable model in the resource pool; the chosen "
                    "model runs it and returns its answer. Use this to offload work (analysis, drafting, a focused "
                    "reasoning or coding question) when you want a result, not to run tools on this machine. The "
                    "delegate has NO access to this conversation or your files — put everything it needs in `task`."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The complete, self-contained subtask or question to delegate."},
            "context": {"type": "string", "description": "Optional extra input or background to include with the task."},
            "max_tokens": {"type": "integer", "description": f"Max tokens for the answer (default {DEFAULT_MAX_TOKENS})."},
            "model": {"type": "string", "description": "Optional. Omitted or 'cortados': S1 routes the subtask. Otherwise "
                      "a pinnable model or alias (fable, opus, sonnet, claude-*, gpt-*) or a friendly name ('GPT 6.1', "
                      "'Sonnet 5', 'ollama 27b q3km': every word must belong to exactly one pool model; an ambiguous "
                      "name is refused with its candidates), sent as sub/<id> (no S1)."},
        },
        "required": ["task"],
    },
}


# Set in every child task (mirrors child_tasks.NESTED_ENV; checked by a test). A child must not route: start_task is
# refused there already, and route_task is refused the same way, so a child (above all an unrouted chrome/login child
# holding a signed-in browser) cannot reach the provider through this server.
NESTED_ENV = "CORTADOS_CHILD_TASK"

# Whole-task delegation: a separate headless Claude Code session on this machine (cortados_router/child_tasks.py).
_MODES = ["auto", "acceptEdits", "manual", "plan", "dontAsk"]   # mirrors child_tasks.PERMISSION_MODES (checked by a test)
CHILD_TOOLS = [
    {"name": "start_task",
     "description": ("Start a WHOLE task as a separate headless Claude Code session on the configured EXECUTION HOST "
                     "(default: the always-on Air; `host` picks another configured one), routed through CortadOS (S1 "
                     "picks the model per turn unless an orchestrator model is pinned). The child has every tool and MCP "
                     "server Claude Code has THERE, and runs unattended; only its final result comes back. Where it "
                     "works: give `repo` (+ optional `branch`) and the host clones/fetches the repository and creates a "
                     "git worktree under its CortadOS/active/<task_id>; give nothing and it gets a plain folder there; "
                     "give `cwd` only for a directory that ALREADY EXISTS ON THE EXECUTION HOST (a path that exists only "
                     "on this machine is refused). When the task ends its folder moves to archive/ (pushed, or no repo; "
                     "deleted after 3 days) or held/ (unpushed work; kept). Returns a task_id at once, plus "
                     "`watch_command`: a shell command to run IN THE BACKGROUND on this machine right after every "
                     "start_task. It blocks until the task ends (one JSON line of metadata; exit 0 every task ended "
                     "`succeeded`, 1 failed/cancelled/stalled, 2 could not watch, 3 its --timeout), so the session is "
                     "woken instead of polling task_status / wait_task. Exit 0 is not yet a checked result: read the "
                     "line's `warnings` count and `workspace` state (`held` = unpushed work), or call task_status, "
                     "before trusting it. Watching several ids in ONE command costs less than one watch per task "
                     "(each remote poll is one ssh call). Put everything the child needs in `task`: it does not see "
                     "this conversation."),
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The complete, self-contained task prompt."},
         "repo": {"type": "string", "description": "Optional: a clone URL (https://github.com/owner/name.git, "
                  "git@github.com:owner/name.git) or the name of a repository already cloned on the host. The host "
                  "clones it to repos/<name> if needed, fetches, and gives the task a worktree in active/<task_id>."},
         "branch": {"type": "string", "description": "Optional, with repo: the branch to check out (created from the "
                    "default branch if it does not exist; refused if another task has it checked out). Omitted: a new "
                    "branch task/<task_id> from the default branch."},
         "cwd": {"type": "string", "description": "Optional: absolute path of a directory that already exists ON THE "
                 "EXECUTION HOST. Not for repositories (use repo); a path that exists only on the requesting machine "
                 "is refused with a message saying so."},
         "host": {"type": "string", "description": "Optional: the name of a configured execution host "
                  "(~/.config/cortados/exec-hosts.json). Default: the file's default, the Air. 'here' (or 'local'): "
                  "the machine this server runs on; use it when the task needs this machine's signed-in browser "
                  "(e.g. Duo/SSO sites like Quercus), local files or local apps. 'studio' for heavy local-model/GPU "
                  "work once set up. Chrome serves one client at a time per machine: while a 'here' chrome task "
                  "runs, do not use Chrome yourself. No entry for this machine is an error, never another host."},
         "title": {"type": "string", "description": "Optional short label for the task (1-80 printable ASCII "
                   "characters), shown in the CortadOS Office view. A label only: never secrets or file contents."},
         "parent_task_id": {"type": "string", "description": "Optional: the task id (t<14 digits>-<8 hex>) of the "
                            "task this one is part of, recorded in the run export. Omitted: CORTADOS_PARENT_TASK_ID "
                            "if this session sets it, else the export marks the parent as missing."},
         "orchestrator": {"type": "string", "description": "'cortados' (default: S1 routes every turn) or a pinnable "
                          "model such as fable, opus, sonnet, gpt-6.1-sol, or a friendly name ('GPT 6.1', 'Sonnet 5', "
                          "'ollama 27b q3km'; an ambiguous name is refused with its candidates): that pins the child's "
                          "MAIN model; the child's subagents are still routed by S1."},
         "permission_mode": {"type": "string", "enum": _MODES, "description": "auto (default where the model "
                             "supports it): a safety classifier approves or denies each action (haiku models other "
                             "than claude-haiku-5-5 have no auto mode: unset, they get acceptEdits; 'auto' is refused); acceptEdits: edits in cwd allowed, other approval-needing "
                             "tools denied; manual: approval-needing tools denied; dontAsk: only pre-approved tools "
                             "run; plan: read-only. bypassPermissions is deliberately not offered."},
         "chrome": {"type": "boolean", "description": "Opt-in (default false): LOGIN MODE. The child runs directly on "
                    "the user's claude.ai login (NOT routed by S1; spends the main Claude subscription) so Claude in "
                    "Chrome loads; it acts in the user's real signed-in Chrome in its own tab group. Model: cortados -> "
                    "claude-sonnet-5-5, a claude-* or sub/claude-* model as given; non-Claude models are refused. "
                    "osascript/open -a/Playwright/Puppeteer are blocked. connectors are refused (the login loads the "
                    "user's claude.ai connectors). Refused if Claude settings set an API key/token/base URL."},
         "connectors": {"type": "array", "items": {"type": "string"}, "description": "Optional: names of servers "
                        "from ~/.config/cortados/connectors.mcp.json the child needs (default none). Each server's "
                        "tool schemas are sent with every request; loading them all can exceed the model's context "
                        "(the provider then refuses with 413)."},
         "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional extra tool patterns "
                           "pre-approved for the child, e.g. ['Bash(npm test:*)', 'Bash(git diff:*)']."}},
         "required": ["task"]}},
    {"name": "task_status",
     "description": "State of a child task (running/succeeded/failed/cancelled), elapsed time, models used, and the final "
                    "result text when done (bounded; the full log path is included).",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "wait_task"},              # built at tools/list time by _wait_task_tool(): its cap comes from the env
    {"name": "list_tasks",
     "description": "Most recent child tasks, newest first, with state and a short preview.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "1-200, default 20"}}}},
    {"name": "cancel_task",
     "description": "Terminate a running child task (its own process group only, on whichever execution host runs it).",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
]
_CHILD_NAMES = {t["name"] for t in CHILD_TOOLS}


def _wait_task_tool() -> dict:
    """The wait_task entry with the cap in force now (CORTADOS_WAIT_MAX_S, or the 50 s default) interpolated. A bad
    CORTADOS_WAIT_MAX_S is named in the description; wait_task itself then fails with the same error."""
    try:
        ct = _child_module()
        default = f"{ct.WAIT_MAX_S:g}"
        try:
            cap, bad = f"{ct.wait_max_s():g}", ""
        except ct.TaskError as exc:
            cap, bad = default, f" Misconfigured: {exc}; wait_task returns this error until it is fixed."
    except ImportError:
        cap = default = "50"
        bad = ""
    dflt = default if float(default) <= float(cap) else cap
    return {"name": "wait_task",
            "description": f"Block until a child task ends or timeout_s passes (default {dflt} s, max {cap} s: the "
                           "Claude desktop app times MCP requests out at 60 s; CORTADOS_WAIT_MAX_S raises the cap for "
                           "other clients), then return what task_status returns. If the result says state "
                           "'running', the task is not done: call wait_task again to keep waiting." + bad,
            "inputSchema": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "timeout_s": {"type": "number", "description": f"0-{cap}, default {dflt}"}},
                "required": ["task_id"]}}


def _child_tools() -> list:
    return [_wait_task_tool() if t["name"] == "wait_task" else t for t in CHILD_TOOLS]


def _child_module():
    """Import cortados_router.child_tasks. This file is normally launched as a script, so its directory, not the repo
    root, is on sys.path; the root is added here, lazily, so route_task never depends on the package importing."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from cortados_router import child_tasks
    return child_tasks


# ---- Task origin: which main session started a task (live view, dashboard, cortados-pulse) -------------------------
# The orchestrating session is named by the SAME opaque tag its Office floor has: sha256("<salt>:<session id>") cut to
# 16 hex, as cortados-routed-subagents computes it (hooks/register.ts sessionTag). The salt is one random value per
# machine in OFFICE_SALT_FILE (0600), created here at server start when missing; the mod and the cortados-pulse pane
# read the same file, so the three agree on the tag without ever sending the raw session id (which `claude --resume`
# takes). No salt file or no session id: the origin session is None, never guessed.
OFFICE_SALT_ENV = "CORTADOS_OFFICE_SALT_FILE"
OFFICE_SALT_DEFAULT = "~/.config/cortados/office-salt"
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"          # set by Claude Code for the MCP servers a session launches
ORIGIN_TAG_HEX = 16                                # the mod's OFFICE_TAG_HEX
BOT_ORIGIN = "grokbot"                             # a bot-gateway task's origin (bot_gateway.CLIENT)
_SALT_RE = re.compile(r"[0-9a-f]{32}")
_SESSION_ID_RE = re.compile(r"[0-9A-Za-z-]{8,128}")


def office_salt_path(env=None) -> str:
    env = os.environ if env is None else env
    path = (env.get(OFFICE_SALT_ENV) or OFFICE_SALT_DEFAULT).strip()
    if path == "~" or path.startswith("~/"):
        path = (env.get("HOME") or os.path.expanduser("~")) + path[1:]
    return path


def read_office_salt(env=None):
    """The machine's Office salt (32 lowercase hex), or None when the file is missing or holds anything else."""
    try:
        with open(office_salt_path(env), encoding="ascii") as f:
            salt = f.read(256).strip()
    except (OSError, UnicodeError, ValueError):
        return None
    return salt if _SALT_RE.fullmatch(salt) else None


def ensure_office_salt(env=None):
    """The salt, created (0600, exclusive: a racing creator's value wins and is read back) when missing. None when it
    cannot be read or made; never raises, since start_task must work without an origin."""
    salt = read_office_salt(env)
    if salt is not None:
        return salt
    path = office_salt_path(env)
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_office_salt(env)
    except OSError:
        return None
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(secrets.token_hex(16) + "\n")
    return read_office_salt(env)


def origin_tag(salt: str, session_id: str) -> str:
    """The session's opaque Office tag (cortados-routed-subagents' sessionTag)."""
    return hashlib.sha256(f"{salt}:{session_id}".encode()).hexdigest()[:ORIGIN_TAG_HEX]


def _office_name(s, device=False):
    """A project or device name as the mod's officeName makes it ([A-Za-z0-9._ -], device without spaces), or None."""
    t = re.sub(r"[^A-Za-z0-9._-]+" if device else r"[^A-Za-z0-9._ -]+", "-", str(s or "")).strip()[:64]
    return t if re.search(r"[A-Za-z0-9]", t) else None


def origin_identity(env=None) -> dict:
    """{session, project, device} of the session this server serves: session the Office tag (None without a salt or a
    session id), project the session's folder name, device this machine's short hostname. Metadata only."""
    env = os.environ if env is None else env
    sid = (env.get(SESSION_ID_ENV) or "").strip()
    salt = read_office_salt(env) if _SESSION_ID_RE.fullmatch(sid) else None
    root = env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return {"session": origin_tag(salt, sid) if salt else None,
            "project": _office_name(os.path.basename(os.path.normpath(root))),
            "device": _office_name(socket.gethostname().split(".")[0], device=True)}


def task_origin(cfg: dict) -> dict:
    """The origin recorded on a start_task: the bot gateway's tasks are the bot's; anything else is this session's."""
    if cfg.get("client") == BOT_ORIGIN:
        return {"session": BOT_ORIGIN, "project": None, "device": None}
    return origin_identity()


def call_child_tool(name: str, a: dict, cfg: dict) -> dict:
    """Run one child-task tool. Errors come back as isError tool results; the provider token is scrubbed from every
    model-visible string (a child could echo it)."""
    if name == "start_task" and not a.get("chrome", False) and cfg.get("url_error"):
        return _err(cfg["url_error"])
    token = (cfg.get("token") or "").strip()
    try:
        ct = _child_module()
    except ImportError as exc:
        return _err(f"child tasks unavailable: cannot import cortados_router.child_tasks ({exc})")
    # Redact BOTH secrets: the env token (what route_task uses) and the token file's (what the child launcher uses);
    # they can differ, and a child could echo the one it was given. A token shorter than MIN_TOKEN_LEN is refused
    # outright (route_task refuses it too): it could not be redacted without mangling ordinary words.
    tokens = [token]
    try:
        tokens.append(ct.session.read_token(cfg.get("token_file") or ct.session.DEFAULT_TOKEN_FILE))
    except ct.session.SessionError:
        pass                                    # no token file: nothing more to redact (start_task reports it if needed)
    try:
        red = ct.Redactor(tokens)
    except ct.TaskError as exc:
        return _err(f"{name}: {exc}")
    try:
        if name == "start_task":
            out = ct.start(a.get("task"), a.get("cwd"), a.get("orchestrator") or "cortados",
                           a.get("permission_mode") or None, a.get("allowed_tools"),
                           cfg.get("url", ""), cfg.get("token_file"), a.get("chrome", False), red,
                           a.get("connectors"), client=cfg.get("client"), title=a.get("title"),
                           session_tag=cfg.get("session_tag"), repo=a.get("repo"), branch=a.get("branch"),
                           host=a.get("host"), parent_task_id=a.get("parent_task_id"), origin=task_origin(cfg))
        elif name == "task_status":
            out = ct.status(a.get("task_id"), red, cfg.get("url", ""), token)
        elif name == "wait_task":
            out = ct.wait(a.get("task_id"), a.get("timeout_s"), red, provider_url=cfg.get("url", ""),
                          token=token)              # wait() reads the cap via wait_max_s(); a bad one is a TaskError
        elif name == "list_tasks":
            ids = a.get("ids")                  # internal (bot gateway): not advertised in the schema
            out = {"tasks": ct.list_recent(a.get("limit", 20), ids if isinstance(ids, list) else None)}
        else:
            out = ct.cancel(a.get("task_id"))
    except ct.TaskError as exc:
        ambiguous = getattr(exc, "ambiguous", False)
        if isinstance(exc, ct.StartFailed):
            # An unexpected exception inside start(), here or on the execution host, classified there by how far the
            # launch got: its message is already the whole guidance (type, where, what to do).
            text = str(exc)
        else:
            detail = (str(exc).strip()
                      or f"{type(exc).__name__} without a message (a cortados-router bug: please report it)")
            text = f"{name}: {detail}"
            if ambiguous and name == "start_task":
                text += AMBIGUOUS_START_HINT
        res = _err(red.text(text))
        if ambiguous:
            # The request may have reached the execution host (e.g. it timed out after sending a start), or the
            # failure came after a possible spawn: a child may be running. Machine-readable for callers that hold a
            # slot for it (bot_gateway); MCP's _meta is the protocol's place for such extensions, and clients ignore
            # keys they do not know.
            res["_meta"] = {AMBIGUOUS_META: True}
            if getattr(exc, "task_id", None):
                res["_meta"][TASK_ID_META] = exc.task_id
        return res
    except Exception as exc:
        # Anything else is a bug, but the caller must still get a specific, non-empty answer as a tool result (a
        # JSON-RPC 'internal error (X)' reads as an empty failure to a model). The exception TEXT is not passed on:
        # it is unscrubbed and may quote a record's content (#94); its type and where it happened are. Not ambiguous:
        # start() classifies every exception from the launch itself (StartFailed, above), so one that reaches here
        # happened outside it, before anything was launched.
        text = (f"{name} failed: unexpected {type(exc).__name__} in the cortados-route server on "
                f"{_short_host()} (a cortados-router bug: please report it with the time).")
        return _err(text + " Retrying may work.")
    # Redact the structure's strings BEFORE serializing: json.dumps escapes quotes and backslashes, so a token holding
    # either would no longer match once serialized.
    return {"content": [{"type": "text", "text": json.dumps(red.tree(out), indent=2)}]}


AMBIGUOUS_META = "cortados/ambiguous"     # call_child_tool's _meta key: the failed operation may have taken effect
TASK_ID_META = "cortados/task_id"         # with AMBIGUOUS_META: the id of the task that was launched, when one is known
AMBIGUOUS_START_HINT = (" No task id came back, but a task may have started anyway: run list_tasks and look for it "
                        "(by title or start time) before calling start_task again, so it does not run twice.")


def _short_host() -> str:
    try:
        return socket.gethostname().split(".")[0] or "this machine"
    except OSError:
        return "this machine"


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def route_task(provider_url: str, token: str, task: str, context: str = "", max_tokens: int = DEFAULT_MAX_TOKENS,
               urlopen=urllib.request.urlopen, model=None, client=None) -> dict:
    """Post the subtask to the provider's /v1/messages and return the routed model's text as an MCP tool result."""
    provider_url, token = (provider_url or "").strip(), (token or "").strip()   # strip stray whitespace/newlines that break header validation
    # Never let the token reach model-visible text: every error below is scrubbed. A token too short to be a provider
    # token (a real one is a long random string) is refused instead of sent, so the scrub never has to choose between
    # leaking a short credential and mangling ordinary words that happen to contain it.
    scrub = lambda s: s.replace(token, "***") if token else s
    if os.environ.get(NESTED_ENV):
        return _err("route_task is not available inside a CortadOS child task: do the work yourself")
    if not (provider_url and token):
        return _err("cortados route_task is not configured: set CORTADOS_PROVIDER_URL and CORTADOS_PROVIDER_TOKEN")
    if len(token) < MIN_TOKEN_LEN:
        return _err(f"cortados route_task: the provider token is shorter than {MIN_TOKEN_LEN} characters; it is not a "
                    f"provider token (check CORTADOS_PROVIDER_TOKEN or the token file)")
    if not (token.isascii() and token.isprintable()):
        # Reject a token that is not printable ASCII (control chars such as an embedded newline from a wrapped paste, or
        # non-ASCII) BEFORE building the request: urllib's header validator / latin-1 encoder would otherwise raise with
        # the token's escaped bytes or one of its characters, which the whole-token scrub misses.
        return _err("cortados route_task: CORTADOS_PROVIDER_TOKEN must be printable ASCII (no control characters such as "
                    "an embedded newline, no non-ASCII); fix it")
    if not (task and str(task).strip()):
        return _err("route_task requires a non-empty 'task'")
    requested = "cortados"
    if model not in (None, "", "cortados"):
        # A pin: validated by the same function start_task's orchestrator uses (child_tasks.pin_model), so both tools
        # accept exactly the same names. Only this path imports the package; plain route_task stays standalone.
        try:
            ct = _child_module()
        except ImportError as exc:
            return _err(f"route_task: model pins need cortados_router.child_tasks ({exc})")
        try:
            requested = ct.pin_model(model)
        except ct.TaskError as exc:
            return _err(f"route_task: {exc}")
    prompt = str(task) if not context else f"{task}\n\nContext:\n{context}"
    try:
        mx = int(max_tokens) if max_tokens else DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        mx = DEFAULT_MAX_TOKENS
    body = json.dumps({"model": requested, "max_tokens": mx,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}).encode()
    req = urllib.request.Request(provider_url.rstrip("/") + "/v1/messages", data=body, method="POST",
                                 # reveal: the provider answers with the backend model (and the lane's % left)
                                 # instead of `cortados`, so the trailer below can name what really ran
                                 headers={"content-type": "application/json", "x-api-key": token,
                                          "x-cortados-reveal": "1",
                                          # attribution: which front door asked (e.g. the bot gateway's "grokbot")
                                          **({"x-cortados-client": str(client)} if client else {})})
    try:
        # Parse this operation-specific setting only when route_task needs it. Importers use the URL resolver too;
        # unrelated ambient route settings must not break those imports.
        timeout_s = float(os.environ.get("CORTADOS_ROUTE_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
        with urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            # Scrub the FULL message before truncating: cutting first could split the token at the 300-char boundary,
            # leaving a prefix of it that the whole-token scrub no longer matches (and so would leak).
            # Only a plain string message is passed on (as for routing errors below): str() of a list or dict is its
            # repr, whose escaping could carry the token past the scrub.
            msg = json.loads(exc.read()).get("error", {}).get("message")
            detail = (": " + scrub(msg)[:300]) if isinstance(msg, str) and msg else ""
        except Exception:
            pass
        return _err(scrub(f"cortados route_task: provider returned HTTP {exc.code}{detail}"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # http.client.HTTPException covers a truncated body (IncompleteRead) after a dropped connection; ValueError covers
        # urllib's header validation, whose message would otherwise echo the token (scrubbed above).
        return _err(scrub(f"cortados route_task: could not reach the provider ({type(exc).__name__}: {exc})"))
    if not isinstance(data, dict) or data.get("type") == "error" or data.get("error"):
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        # The provider's error text is not ours to trust: it may echo the request's credential, so scrub it. Only a plain
        # string message is passed on; a raw payload is never formatted into the result, because repr/str escaping
        # (backslashes, quotes) would carry a token in a form the scrub no longer matches.
        if isinstance(msg, str) and msg:
            return _err("cortados route_task: routing error: " + scrub(msg)[:400])   # scrub, then truncate
        return _err(f"cortados route_task: routing error: unexpected provider response ({type(data).__name__})")
    blocks = [b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(b for b in blocks if b).strip()
    model = data.get("model")
    model = model if isinstance(model, str) and model else "?"    # a non-string would be repr'd past the scrub
    # The delegate's own text is model-visible too: scrub it in case the provider or the delegate echoes the credential.
    # One trailing line names the model that really ran, so the orchestrator can see and report it (MCP clients may not
    # show _meta to the model).
    shown = scrub(model)
    shown = f"S1 → {shown}" if requested == "cortados" else shown      # routed by S1, or the pin that ran
    body_text = (scrub(text) or "(the delegate returned no text)") + f"\n\n[cortados: {shown}]"
    return {"content": [{"type": "text", "text": body_text}],
            "_meta": {"cortados/routed_model": scrub(model), "cortados/requested_model": requested}}


def handle(msg: dict, cfg: dict) -> dict | None:
    """One JSON-RPC message in, one reply message out (None for a notification). Pure, for tests. Validates shapes so a
    malformed request yields a JSON-RPC error instead of crashing the session."""
    if not isinstance(msg, dict):
        return None                                     # a non-object line: no id to answer, drop it
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if mid is None:
        return None                                     # notifications need no answer
    base = {"jsonrpc": "2.0", "id": mid}
    if not isinstance(method, str):
        return {**base, "error": {"code": -32600, "message": "invalid request: 'method' must be a string"}}
    if method == "initialize":
        req = params.get("protocolVersion")
        version = req if req in SUPPORTED_PROTOCOLS else PROTOCOL   # only echo versions we actually implement
        return {**base, "result": {"protocolVersion": version, "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "cortados-route", "version": "0"}}}
    if method == "tools/list":
        return {**base, "result": {"tools": [TOOL] + _child_tools()}}
    if method == "tools/call":
        a = params.get("arguments")
        if not isinstance(a, dict):
            a = {}
        if params.get("name") in _CHILD_NAMES:
            return {**base, "result": call_child_tool(params["name"], a, cfg)}
        if params.get("name") != "route_task":
            return {**base, "result": _err(f"unknown tool {params.get('name')!r}")}
        if cfg.get("url_error"):
            return {**base, "result": _err(cfg["url_error"])}
        return {**base, "result": route_task(cfg.get("url", ""), cfg.get("token", ""), a.get("task", ""),
                                              a.get("context", ""), a.get("max_tokens", DEFAULT_MAX_TOKENS),
                                              model=a.get("model"))}
    if method == "ping":
        return {**base, "result": {}}
    return {**base, "error": {"code": -32601, "message": f"method not found: {method}"}}


_out_lock = threading.Lock()


def _write(reply: dict) -> None:
    with _out_lock:                                     # serialize stdout across concurrent workers
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def _dispatch(msg, cfg, write=_write) -> None:
    """Run one message's handler on its own thread so a slow route_task never blocks the stdin reader (pings stay
    prompt). Replies are matched by id, so out-of-order writes are fine."""
    def run():
        try:
            reply = handle(msg, cfg)
        except Exception as exc:                        # never let one bad request kill the session; the reply names only
                                                        # the exception type, as its text is unscrubbed and may echo config
            mid = msg.get("id") if isinstance(msg, dict) else None
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal error ({type(exc).__name__})"}} if mid is not None else None
        if reply is not None:
            write(reply)
    threading.Thread(target=run, daemon=True).start()


def _resolve_token() -> str:
    """The provider token: CORTADOS_PROVIDER_TOKEN if set, else the token file (CORTADOS_PROVIDER_TOKEN_FILE, default
    ~/.config/cortados/provider-token) — the same file the routed-subagents mod reads, so route_task needs only the URL
    configured, not the secret inlined in .mcp.json."""
    tok = (os.environ.get("CORTADOS_PROVIDER_TOKEN") or "").strip()
    if tok:
        return tok
    path = os.path.expanduser(os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or "~/.config/cortados/provider-token")
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):                       # unreadable, or undecodable (UnicodeDecodeError is a ValueError):
        return ""                                       # never crash the stdio server; route_task reports 'not configured'


class ProviderURLError(ValueError):
    """Invalid endpoint configuration; messages name the source, never its contents."""


def resolve_provider_url(environ=None, *, required=True) -> str:
    """Resolve env > explicit file > per-machine file (stdlib, standalone-safe).

    Only an absent DEFAULT file may mean unconfigured for optional clients. An
    explicitly selected file or malformed value must never silently fall back.
    """
    env = os.environ if environ is None else environ
    source = "CORTADOS_PROVIDER_URL"
    url = (env.get(source) or "").strip()
    if not url:
        selected = env.get("CORTADOS_PROVIDER_URL_FILE")
        path = selected or "~/.config/cortados/provider-url"
        if path.startswith("~/"):
            path = os.path.join(env.get("HOME") or os.path.expanduser("~"), path[2:])
        source = f"provider URL file {path}"
        try:
            with open(path, encoding="utf-8") as f:
                url = f.read().strip()
        except FileNotFoundError:
            if not selected and not required:
                return ""
            raise ProviderURLError(f"{source}: not found") from None
        except (OSError, UnicodeError, ValueError):
            raise ProviderURLError(f"{source}: cannot read URL") from None
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (url.startswith(("http://", "https://")) and bool(parsed.hostname)
                 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url))
        parsed.port  # malformed ports must fail before any request
    except ValueError:
        valid = False
    if not valid:
        raise ProviderURLError(f"{source}: expected one http:// or https:// URL with a host")
    return url.rstrip("/")


def _resolve_url() -> str:
    return resolve_provider_url()


def main() -> None:
    cfg = {"url": "", "token": _resolve_token(),
           "token_file": os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or None,   # the child launcher reads the file
           # One opaque Office tag per server process: Claude Code starts one cortados-route server per session, so
           # the children it starts share a floor (provider/office.py). Random, not derived from any session id.
           "session_tag": secrets.token_hex(8)}
    ensure_office_salt()                                # the task origin's salt, shared with the mods (task_origin)
    try:
        cfg["url"] = _resolve_url()
    except ProviderURLError as exc:
        # Keep task management and unrouted Chrome/login tasks available without a provider.
        # Only provider-dependent calls report the retained, source-named configuration error.
        cfg["url_error"] = str(exc)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            continue                                    # a malformed or pathologically nested line is skipped, not fatal
        for one in (msg if isinstance(msg, list) else [msg]):   # tolerate a JSON-RPC batch by dispatching each element
            _dispatch(one, cfg)


if __name__ == "__main__":
    main()
