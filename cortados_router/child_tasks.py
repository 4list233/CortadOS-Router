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
  stdout.log    claude's `--output-format stream-json` events, ending in its result record;  stderr.log  claude's diagnostics
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
import socket
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
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
# Not every model can run in auto mode. Claude Code 2.1.316 checks the session model at startup and, for one it does not
# support, logs "auto mode disabled: model <m> does not support auto mode" and drops the session to `default`; with
# --permission-prompts none every approval-needing call is then denied ("no approval surface"). Verified 2026-11-02 on
# 2.1.316 with --debug-file against a local capture endpoint (modelSupported=false): sub/claude-haiku-4-5-20251001 and
# claude-haiku-4-5-20251001; supported: cortados, sub/claude-haiku-5-5, sonnet-4-6/5/5-5, opus-4-6/5-5, fable-5,
# gpt-5.6-luna, gpt-6.1-sol, qwen-27b. The CLI's rule is "a name containing haiku, except claude-haiku-5-5".
AUTO_MODE_EXCEPTIONS = ("claude-haiku-5-5",)
# How a requested mode appears in the stream's system events (2.1.316, checked 2026-11-02): `manual` is reported as
# `default` (the others as themselves), so a manual child is not warned about a mode it never left.
MODE_REPORTED_AS = {"manual": "default"}
# The mode a defaulted task gets on a model without auto mode: file edits in cwd still go through unattended.
NO_AUTO_FALLBACK = "acceptEdits"
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
# The Claude desktop app's Code session times every MCP request out at 60 s (observed 2026-11-03: "Request timed out"
# at ~61 s on wait_task calls). The reply must land before that, end to end: the polling loop AND the status() work
# after it (a crashed child's recovery: RECOVERY_GRACE_S + ps calls; the run-record fetch, which shrinks to what is
# left of the deadline, min 1 s). POST_WAIT_RESERVE_S covers that work plus a margin, so the default wait is 50 s.
# ASSUMPTION: ps returns promptly (well under 1 s on a healthy macOS). Its PS_TIMEOUT_S guard only stops a hung ps
# from blocking forever; under a stuck process table two ps calls can still push one reply past the client timeout.
# Not derived from the deadline because a ps cut short reads as UNKNOWN/False and would skip the recovery.
# Clients with a longer request timeout raise the cap with CORTADOS_WAIT_MAX_S (read via wait_max_s()).
CLIENT_REQUEST_TIMEOUT_S = 60
POST_WAIT_RESERVE_S = 10            # recovery (5 s grace + prompt ps calls) + >= 1 s run fetch + slack for disk and JSON-RPC
WAIT_MAX_S = CLIENT_REQUEST_TIMEOUT_S - POST_WAIT_RESERVE_S
# No MCP client the router serves holds a request open for an hour; a larger value is almost surely a unit slip
# (milliseconds), and taking it would let one wait_task call block its client far past any request timeout.
WAIT_MAX_S_CEILING = 3600
# The Office view (provider/office.py): a child's title and its orchestrator's session tag ride on its requests as
# headers and on its events. A title is the orchestrator's short label (the owner's cap: 80 printable ASCII chars; a
# longer or odd one is refused, not cut, so the caller sees why). The tag is an opaque hash (16-64 hex). Project and
# device are derived (cwd basename, short hostname) and simply left out when they do not fit the view's charset.
TITLE_RE = re.compile(r"[\x20-\x7e]{1,80}")
SESSION_TAG_RE = re.compile(r"[0-9a-f]{16,64}")
_OFFICE_NAME_RE = re.compile(r"[A-Za-z0-9._ -]{1,64}")
_DEVICE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
PS_TIMEOUT_S = 10                   # guards a hung ps only (see the ASSUMPTION above)
RUN_FETCH_MIN_S = 1                 # a fetch with less than this left is not worth starting short of zero


def wait_max_s(env=None) -> float:
    """The wait cap: WAIT_MAX_S, or CORTADOS_WAIT_MAX_S when set. A set value that is not a number of seconds in
    (0, WAIT_MAX_S_CEILING] raises TaskError (like CORTADOS_CHILD_MAX_S): a typo must not silently become 50 s."""
    raw = (os.environ if env is None else env).get("CORTADOS_WAIT_MAX_S")
    if raw is not None and not isinstance(raw, str):   # an injected dict may carry a non-str: refused like junk
        raise TaskError(f"CORTADOS_WAIT_MAX_S={raw!r} is not a number of seconds")
    if raw is None or raw.strip() == "":
        return float(WAIT_MAX_S)
    try:
        v = float(raw)
    except ValueError:
        raise TaskError(f"CORTADOS_WAIT_MAX_S={raw!r} is not a number of seconds") from None
    if v != v or not 0 < v <= WAIT_MAX_S_CEILING:
        raise TaskError(f"CORTADOS_WAIT_MAX_S={raw!r} must be more than 0 and at most {WAIT_MAX_S_CEILING} seconds")
    return v


NESTED_ENV = "CORTADOS_CHILD_TASK"  # set in every child: a child may not start children (runaway quota fan-out)
# Claude Code refuses to start inside another Claude Code session when it sees this; the child is a fresh session.
STRIP_ENV = ("CLAUDECODE",)
MANAGED_DIRS = session.MANAGED_SETTINGS_DIRS   # tests point this elsewhere
# Children load the same ~/.claude/CLAUDE.md as the user's top-level sessions, whose ORCHESTRATOR directive says to
# delegate via start_task/route_task; this tells the child unambiguously that it is the delegate.
# Native subagents are fine (S1 routes them); starting further child tasks or re-handing the whole task off is not.
CHILD_DIRECTIVE = ("You are a CortadOS CHILD TASK started by an orchestrator. Do the task yourself and completely. You may "
                   "use your own subagents for parts of it (they are routed by S1), but do not call start_task or "
                   "route_task (both are refused inside a child task) or hand the whole task off again. Your final message is returned to the orchestrator: end with a concise "
                   "report of what you did, what you verified, and anything left undone.")


# --- chrome: true = LOGIN MODE (Forest's decision, 2026-11-03) ---------------------------------------------------
# Measured with Claude Code 2.1.316: a session pointed at the provider (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, or
# ANTHROPIC_API_KEY set, even empty) loads 30 tools and NO claude-in-chrome server; a plain claude.ai-login session loads
# claude-in-chrome (22 mcp__claude-in-chrome__* tools). So a chrome child runs directly on the user's login, unrouted by
# S1, spending the main Claude subscription. chrome: false is untouched.
LOGIN_LANE = "login (unrouted, chrome)"
# S1 cannot route a login-mode session, so the routed orchestrator `cortados` maps to this fixed model.
LOGIN_DEFAULT_MODEL = "claude-sonnet-5-5"
# A chrome child's env is built from an ALLOWLIST, not a denylist: the session holds a signed-in browser, so it must
# not inherit the provider credential (CORTADOS_PROVIDER_TOKEN/_FILE), the provider's internal address
# (CORTADOS_PROVIDER_URL), API keys/tokens/base URLs that drop the login auth (which disables Claude in Chrome), model
# overrides (ANTHROPIC_MODEL, ANTHROPIC_SMALL_FAST_MODEL, ANTHROPIC_DEFAULT_*_MODEL, CLAUDE_CODE_SUBAGENT_MODEL) or
# CLAUDE_CONFIG_DIR. A denylist misses whatever is added next; an allowlist cannot. Passed: what a shell session needs,
# plus the bookkeeping paths the runner (same env) reads so its state stays in the caller's (test's) throwaway dirs.
LOGIN_ENV_ALLOW = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM", "COLORTERM",
                   "__CF_USER_TEXT_ENCODING",
                   "CORTADOS_HOME", "CORTADOS_HERMES_HOME", "CORTADOS_TASKS_DIR",
                   # Network config: without it a proxied or custom-CA network cannot reach claude.ai. These carry no
                   # credential, EXCEPT a proxy URL with userinfo (http://user:pass@host): login_env drops those.
                   "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
                   "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
LOGIN_PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
LOGIN_ENV_ALLOW_PREFIXES = ("LC_",)   # tests add their fake binary's control vars here
# Never passed even if a future allowlist edit would admit them (asserted by tests).
LOGIN_FORBIDDEN_ENV = ("CORTADOS_PROVIDER_URL", "CORTADOS_PROVIDER_TOKEN", "CORTADOS_PROVIDER_TOKEN_FILE",
                       "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                       "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CONFIG_DIR",
                       # the ssh agent signs as the user (git push, ssh host): a browser task does not need it and an
                       # unattended auto-mode child must not hold it; git over ssh would be an explicit per-task opt-in
                       "SSH_AUTH_SOCK",
                       # Claude Code refuses to start inside another session when it sees this (STRIP_ENV)
                       "CLAUDECODE")
LOGIN_FORBIDDEN_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CORTADOS_PROVIDER_")
# Settings files a login child reads; any of these keys in them would disable Chrome or send the session elsewhere.
# The child always uses ~/.claude: CLAUDE_CONFIG_DIR is not in LOGIN_ENV_ALLOW, so it never reaches the child.
USER_CLAUDE_SETTINGS = "~/.claude/settings.json"   # tests point this elsewhere
LOGIN_BAD_SETTINGS_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
# Browser control other than Claude in Chrome: a routed child without Chrome tools once drove the real Chrome through
# osascript (17 Bash calls) and kept stealing screen focus; Playwright/Puppeteer are unwanted (security). These Bash
# patterns only raise the cost of the osascript path (a shell can always spell it another way); CHROME_DIRECTIVE is the
# real control. mcp__cortados-route is removed because a child must not route or start tasks: this session is unrouted
# on the user's login and holds a signed-in browser, and the server would hand it the provider credential path.
CHROME_DISALLOWED_TOOLS = ("Bash(osascript:*)", "Bash(osascript *)", "Bash(*osascript*)", "Bash(open -a *)",
                           "Bash(*open -a*)", "Bash(npx playwright*)", "Bash(playwright*)", "Bash(*puppeteer*)",
                           "mcp__playwright", "mcp__puppeteer", "mcp__cortados-route")
CHROME_DIRECTIVE = ("BROWSER RULES: for any browser work use only the mcp__claude-in-chrome__* tools, working in your "
                    "own tab group. Never control a browser through AppleScript/osascript, Playwright, Puppeteer, "
                    "Selenium or by opening apps. If the Claude in Chrome tools are unavailable, stop and report that "
                    "instead of improvising.")


class TaskError(Exception):
    pass


def login_model(name) -> str:
    """The model a chrome (login-mode) child runs: `cortados` -> LOGIN_DEFAULT_MODEL (S1 cannot route a login session);
    a Claude model or sub/<claude model> -> that model id; anything else is refused (the login only runs Claude)."""
    if name == "cortados":
        return LOGIN_DEFAULT_MODEL
    model = pin_model(name)
    model = model.split("/", 1)[1] if model.startswith("sub/") else model
    if not model.startswith("claude-"):
        raise TaskError(f"chrome tasks run on the user's claude.ai login, which can only run Claude models; {name!r} "
                        f"({model}) is not one. Use cortados (= {LOGIN_DEFAULT_MODEL}) or a claude-* model.")
    return model


def check_login_settings(cwd: str) -> None:
    """Fail closed: refuse a chrome child if a settings file it would load sets an API key/token/base URL in env or an
    apiKeyHelper; each would disable Claude in Chrome or send the session away from the login. Managed policy files
    outrank every file checked here, so they are checked too (env keys via session.check_managed, plus apiKeyHelper)."""
    try:
        session.check_managed(LOGIN_BAD_SETTINGS_ENV, MANAGED_DIRS)
        managed = [str(f) for f in session.managed_settings_files(MANAGED_DIRS)]
    except session.SessionError as exc:
        raise TaskError(f"chrome task refused: {exc}") from None
    for path in managed + [os.path.expanduser(USER_CLAUDE_SETTINGS), os.path.join(cwd, ".claude", "settings.json"),
                           os.path.join(cwd, ".claude", "settings.local.json")]:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError) as exc:
            raise TaskError(f"chrome task refused: cannot read {path} to check it ({type(exc).__name__})") from None
        if not isinstance(cfg, dict):
            continue
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        bad = [k for k in LOGIN_BAD_SETTINGS_ENV if k in env] + (["apiKeyHelper"] if "apiKeyHelper" in cfg else [])
        if bad:
            raise TaskError(f"chrome task refused: {path} sets {', '.join(bad)}, which would disable Claude in Chrome "
                            "or send the session away from the claude.ai login; remove it or run without chrome")


def login_env(environ) -> dict:
    """The chrome child's env: only LOGIN_ENV_ALLOW / LOGIN_ENV_ALLOW_PREFIXES, never LOGIN_FORBIDDEN_* (see above)."""
    return {k: v for k, v in environ.items()
            if (k in LOGIN_ENV_ALLOW or k.startswith(LOGIN_ENV_ALLOW_PREFIXES))
            and k not in LOGIN_FORBIDDEN_ENV and not k.startswith(LOGIN_FORBIDDEN_PREFIXES)
            and not (k in LOGIN_PROXY_ENV and _has_userinfo(v))}


def _has_userinfo(url) -> bool:
    """A proxy URL that embeds a credential (user[:pass]@host). Unparseable counts as yes: fail closed."""
    u = str(url)
    try:
        return "@" in urllib.parse.urlsplit(u if "://" in u else "//" + u).netloc
    except ValueError:
        return True


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


def auto_mode_supported(model: str) -> bool:
    """Whether Claude Code (2.1.316) keeps a session on `model` (a launcher model: cortados or sub/<id>) in auto mode;
    see AUTO_MODE_EXCEPTIONS for the verified rule."""
    name = model.split("/", 1)[1] if model.startswith("sub/") else model
    name = name.strip().lower()
    return "haiku" not in name or any(name == e or name.startswith(e + "-") for e in AUTO_MODE_EXCEPTIONS)


def resolve_permission_mode(permission_mode, orchestrator):
    """(mode, note) for the child. Unset means auto where the child's model supports it, else NO_AUTO_FALLBACK, and the
    note says why (returned by start_task and kept in meta). An explicit `auto` on a model without auto mode is
    refused: the child would silently run in `default` and be denied every action that needs approval."""
    model = pin_model(orchestrator)
    if permission_mode is None:
        if auto_mode_supported(model):
            return DEFAULT_PERMISSION_MODE, None
        return NO_AUTO_FALLBACK, (f"{model} has no auto mode in Claude Code, so this task runs in {NO_AUTO_FALLBACK}: "
                                  "file edits in cwd go through; other actions that need approval (Bash that changes "
                                  "files, network) are denied. Pass allowed_tools to widen, or pick another model.")
    if permission_mode == "auto" and not auto_mode_supported(model):
        raise TaskError(f"permission_mode 'auto' is not available for {model}: Claude Code drops a session on this model "
                        "out of auto mode, and an unattended child would then be denied every action that needs "
                        f"approval. Use {NO_AUTO_FALLBACK} (edits in cwd) or dontAsk with allowed_tools, or a model with "
                        "auto mode (any non-haiku model, or claude-haiku-5-5).")
    return permission_mode, None


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
    if permission_mode is not None and permission_mode not in PERMISSION_MODES:   # None: resolve_permission_mode picks
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


def fetch_run(provider_url: str, token: str, run_id: str, timeout_s: float = RUN_FETCH_TIMEOUT_S) -> dict:
    """The provider's record of this run (GET /v1/cortados/runs/<id>): models and lanes it routed to. Failures come back
    as {"error": ...}, never raised; the token never appears in the text."""
    scrub = (lambda s: s.replace(token, "***")) if token else (lambda s: s)
    if not (provider_url and token):
        return {"error": "provider URL or token not configured"}
    req = urllib.request.Request(provider_url.rstrip("/") + "/v1/cortados/runs/" + run_id, method="GET",
                                 headers={"x-api-key": token})
    try:
        with _urlopen(req, timeout=timeout_s) as resp:
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


def office_tags(cwd, title=None, session_tag=None) -> dict:
    """The Office fields of a task: title and session (validated, TaskError when bad), project and device (derived,
    omitted when they do not fit). Empty when neither title nor tag is given and nothing could be derived."""
    out = {}
    if title is not None:
        if not isinstance(title, str) or not TITLE_RE.fullmatch(title.strip()):
            raise TaskError("title must be 1-80 printable ASCII characters")
        out["title"] = title.strip()
    if session_tag is not None:
        if not isinstance(session_tag, str) or not SESSION_TAG_RE.fullmatch(session_tag):
            raise TaskError("session must be 16-64 lowercase hex characters (an opaque session tag)")
        out["session"] = session_tag
    project = os.path.basename(os.path.normpath(str(cwd))) if cwd else ""
    if _OFFICE_NAME_RE.fullmatch(project):
        out["project"] = project
    device = socket.gethostname().split(".")[0]
    if _DEVICE_RE.fullmatch(device):
        out["device"] = device
    return out


_OFFICE_HEADER_OF = {"session": "x-cortados-session", "project": "x-cortados-project", "device": "x-cortados-device",
                     "title": "x-cortados-task-title"}


def start(task, cwd, orchestrator="cortados", permission_mode=None, allowed_tools=None,
          provider_url="", token_file=None, chrome=False, red=None, connectors=None, client=None,
          title=None, session_tag=None) -> dict:
    """`client` (optional, e.g. "grokbot" from the bot gateway) is recorded in meta.json and every event line, so usage
    can be attributed to the caller. `red` carries the launch credentials (env token and token file's token as they are now). `permission_mode`
    None picks auto where the model supports it (resolve_permission_mode). `title` (<= 80 chars) and `session_tag`
    (the orchestrator's opaque tag) label the task in the Office view: on its events, and as headers on its routed
    requests."""
    if os.environ.get(NESTED_ENV):
        raise TaskError("start_task is not available inside a child task: children may not start children "
                        "(unbounded fan-out would spend subscription quota unattended)")
    tools = _validate(task, cwd, orchestrator, permission_mode, allowed_tools)
    office = office_tags(cwd, title, session_tag)
    if not isinstance(chrome, bool):
        raise TaskError("chrome must be true or false")
    login = None
    if chrome:
        login = login_model(orchestrator)
        if connectors:
            # Refused, not ignored: in login mode the user's claude.ai connectors load through the login itself, and a
            # silently dropped list would hide that from the caller.
            raise TaskError("connectors are not used with chrome: true (login mode loads the user's claude.ai "
                            "connectors through the login itself); omit connectors")
        check_login_settings(cwd)
    permission_mode, mode_note = resolve_permission_mode(permission_mode, login or orchestrator)
    selected = select_connectors(None if chrome else connectors)
    if not provider_url and not chrome:
        try:
            provider_url = session.resolve_provider_url()
        except session.ProviderURLError as exc:
            raise TaskError(f"start_task is not configured: {exc}") from None
    max_s = _max_s()
    exe = claude_bin()
    root = _private_root()
    prune_errors: list = []
    prune(root, errors=prune_errors)
    # No cap on parallel children. Routed children: capacity is the router's job (S1 and the quota gates refuse or
    # redirect a spent lane). Chrome (login-mode) children bypass S1 and the quota gates, so NOTHING here bounds N of
    # them against the main Claude subscription; the orchestrator decides how many to run (Claude in Chrome gives each
    # session its own tab group). A cap would be an [ESCALATE] decision, not added silently.
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
                                  chrome, max_s, exe, selected, login, office)
        if client:
            meta["client"] = str(client)[:64]
            # cmd.json's event fields were written by _prepare before the client was known; the runner logs the
            # finished event from them, so rewrite them or that event loses its attribution (review r2 P3-8).
            cmd_path = os.path.join(d, "cmd.json")
            with open(cmd_path, encoding="utf-8") as f:
                spec = json.load(f)
            spec["event"] = _event_fields(meta)
            _write_json(cmd_path, spec)
        if mode_note:
            meta["permission_mode_note"] = mode_note
        if client or mode_note:
            _write_json(os.path.join(d, "meta.json"), meta)
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
    out = {"task_id": task_id, "state": "running", "dir": d, "permission_mode": permission_mode}
    if login:
        out.update(lane=LOGIN_LANE, model=login)
    if mode_note:
        out["permission_mode_note"] = mode_note
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
             exe, selected=None, login=None, office=None):
    """Everything before the spawn: settings, prompt, cmd.json, meta.json. Returns (meta, env, cmd)."""
    os.chmod(d, 0o700)                          # mkdir's mode is masked by umask; the launcher verifies exactly 0700
    if login:
        meta, env, cmd = _prepare_login(d, task_id, task, cwd, orchestrator, permission_mode, tools, red, max_s, exe,
                                        login, office)
        return meta, env, cmd
    launch = ["--orchestrator", orchestrator, "--provider-url", provider_url, "--settings-dir", d, "--run-id", task_id]
    if token_file:
        launch += ["--token-file", token_file]
    for k, v in (office or {}).items():         # validated by office_tags; session.py checks them again
        launch += ["--office-header", f"{_OFFICE_HEADER_OF[k]}: {v}"]
    # Never the whole-session ORCHESTRATOR directive or its delegation gate, whatever the orchestrator: a child is the
    # delegate and does the work itself (CHILD_DIRECTIVE). A pinned orchestrator only pins the child's main model; its
    # native subagents are still routed by S1 (CLAUDE_CODE_SUBAGENT_MODEL).
    launch += ["--no-delegate-directive", "--no-gate"]
    # --permission-prompts none: nobody is there to answer, so anything that would prompt is denied deterministically
    # (as s1/outcome/agents.py runs its headless claude).
    # stream-json (which -p only allows with --verbose): every event, not only the result record, so status can say
    # which tools the child really used, what it wrote where, and which permission mode it actually ran in (a
    # system event names it, also when Claude Code leaves the requested mode).
    extra = ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", permission_mode,
             "--permission-prompts", "none"]
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
            "argv": cmd[1:], **({"office": office} if office else {})}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, "cwd": cwd, "max_s": max_s, "grace_s": KILL_GRACE_S,
                                              "event": _event_fields(meta), "started_at": meta["started_at"]})
    _write_json(os.path.join(d, "meta.json"), meta)    # before Popen: a task dir always has its meta
    return meta, env, cmd


def _prepare_login(d, task_id, task, cwd, orchestrator, permission_mode, tools, red, max_s, exe, model, office=None):
    """chrome: true. No launcher, no --settings provider file, no provider env: claude on the user's own login."""
    cmd = [exe, "--model", model, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode",
           permission_mode, "--permission-prompts", "none"]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    # Only the routed-subagents plugin switch: no env, no secret (it would route this session's subagents through the
    # provider, which a login session neither has nor should reach).
    # named session-*.json so _remove_settings deletes it like the routed child's file when the child exits
    settings = os.path.join(d, "session-login.json")
    with open(os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        json.dump({"enabledPlugins": {session.PLUGIN_ID: False}}, f)
    cmd += ["--settings", settings, "--disallowedTools", ",".join(CHROME_DISALLOWED_TOOLS), "--chrome",
            "--append-system-prompt", CHILD_DIRECTIVE + "\n\n" + CHROME_DIRECTIVE]
    env = login_env(os.environ)
    env[NESTED_ENV] = "1"
    launch_fps = credential_fingerprints(launch_tokens(env))   # the allowlist admits none, so this is []
    with open(os.open(os.path.join(d, "prompt.txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
              encoding="utf-8") as f:
        f.write(task)
    meta = {"task_id": task_id, "cwd": cwd, "orchestrator": orchestrator, "permission_mode": permission_mode,
            "allowed_tools": tools, "task_chars": len(task), "task_preview": red.text(task.strip())[:200],
            "started_at": time.time(), "connectors": [], "chrome": True, "lane": LOGIN_LANE, "model": model,
            # Login-mode children never route through CortadOS, so carry no Office identity into lifecycle events.
            "max_s": max_s, "runner_pid": None, "cred_fingerprints": launch_fps, "argv": cmd[1:]}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, "cwd": cwd, "max_s": max_s, "grace_s": KILL_GRACE_S,
                                              "event": _event_fields(meta), "started_at": meta["started_at"]})
    _write_json(os.path.join(d, "meta.json"), meta)
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
    out = {k: meta.get(k) for k in ("task_id", "cwd", "orchestrator", "permission_mode", "chrome")}
    if meta.get("client"):
        out["client"] = meta["client"]
    if meta.get("lane"):
        out.update(lane=meta["lane"], model=meta.get("model"))
    # Office fields (title, session tag, project, device): metadata the view shows, never the prompt.
    for k, v in (meta.get("office") or {}).items():
        out[k] = v
    return out


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
                                 timeout=PS_TIMEOUT_S).stdout
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
    # None = an old/odd meta: withhold. [] = the child provably ran with no launch credential (login mode), so nothing
    # needs covering and the logs are released after the usual redaction instead of being withheld forever.
    launch = meta.get("cred_fingerprints")
    if not isinstance(launch, list) or not set(launch) <= set(credential_fingerprints(red.raw)):
        return False
    stdout = os.path.join(d, "stdout.log")
    structural_error = None
    try:
        unparsed = redact_stream_file(stdout, red)
    except Exception as exc:                    # as in _run: recorded, and the raw pass below still runs
        unparsed, structural_error = 0, f"could not redact stdout.log structurally: {type(exc).__name__}"
    try:
        for name in ("stdout.log", "stderr.log"):
            redact_file(os.path.join(d, name), red)
    except OSError:
        return False
    if structural_error:
        res["redact_error"] = structural_error   # the task fails and its logs stay withheld
        _write_json(os.path.join(d, "result.json"), res)
        return False
    if unparsed:
        res["redact_unparsed_lines"] = unparsed
    try:
        res["activity"], n = red.deep_count(summarize_stream(stdout, meta.get("cwd") or d))
        if n:
            res["redact_unparsed_lines"] = res.get("redact_unparsed_lines", 0) + n
    except OSError as exc:
        res["activity_error"] = f"could not read stdout.log: {type(exc).__name__}"
    res["logs_unredacted"] = False
    _write_json(os.path.join(d, "result.json"), res)
    return True


def status(task_id, red=None, provider_url="", token="", deadline=None) -> dict:
    """deadline (time.monotonic()) bounds the run-record fetch: its timeout shrinks to what is left, min
    RUN_FETCH_MIN_S, so wait_task's reply lands inside the client's request timeout."""
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
    if meta.get("lane"):
        out.update(lane=meta["lane"], model=meta.get("model"))
    if res is not None:
        o = res.get("output") or {}
        out["exit_code"] = res.get("exit_code")
        if res.get("timed_out"):
            out["timed_out"] = True
        out["settings_removed"] = bool(res.get("settings_removed"))
        if o:
            text = o.get("result")
            text = text if isinstance(text, str) else ""
            redacted, n = _text_result(red, text)            # redact BEFORE truncating: a cut never splits a token
            out["result"] = redacted[:RESULT_CHARS]
            if n:
                out["redact_unparsed_lines"] = out.get("redact_unparsed_lines", 0) + n
            if len(text) > RESULT_CHARS:
                out["result_truncated"] = f"{len(text)} characters; the full text is in stdout_log"
            usage = o.get("modelUsage")
            if isinstance(usage, dict):
                out["models"] = sorted(str(k) for k in usage)
            for k in ("num_turns", "total_cost_usd", "session_id", "subtype"):
                if k in o:
                    out[k] = o[k]
        warnings = _activity(out, o, res.get("activity"), meta)
        if warnings:
            out["warnings"] = warnings
        if res.get("output_error"):
            out["output_error"] = res["output_error"]     # reported even when a (malformed) record was parsed
        if res.get("redact_error"):
            out["redact_error"] = res["redact_error"]
        if res.get("redact_unparsed_lines"):
            # what the structural pass withheld (lines, strings or objects it could not parse or walk): say so
            out["redact_unparsed_lines"] = out.get("redact_unparsed_lines", 0) + res["redact_unparsed_lines"]
        if res.get("error"):
            out["error"] = res["error"]
        # The router's own record of what this child was routed to (its requests carried x-cortados-run-id: task_id).
        # A login-mode (chrome) task never went through the provider: there is no run record to fetch.
        out["router_run"] = ({"note": "login mode: not routed, no provider run record"} if meta.get("lane")
                             else fetch_run(provider_url, token, task_id, _fetch_timeout(deadline)))
        # Why the provider refused (Claude Code replaces a 413's message with its own "images" text, so the child's
        # result never says): the most recent refusal recorded under this run, plus a hint for the known cause.
        refusals = out["router_run"].get("refusals") if isinstance(out["router_run"], dict) else None
        if refusals:
            last = refusals[-1]
            out["provider_refusal"] = f"HTTP {last.get('status')} {last.get('type')}: {last.get('reason')}"
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
    # Every other string structurally (before any serialization; fails closed). The result text is already redacted
    # as text and possibly truncated: never reparsed structurally (a cut JSON result would no longer decode).
    result = out.pop("result", None)
    out = red.deep(out)
    if result is not None and isinstance(out, dict):
        out["result"] = result
    return out


def _activity(out: dict, o: dict, act, meta: dict) -> list:
    """Copy what the child really did into the status `out`, and return the warnings a caller must not miss: a
    "succeeded" child whose result text is only the model's claim (denied tools, no file written where it said, a
    different permission mode)."""
    warnings = []
    denials = o.get("permission_denials") if isinstance(o, dict) else None
    if isinstance(denials, list) and denials:
        names = [str((x or {}).get("tool_name") or "?") for x in denials if isinstance(x, dict)]
        out["permission_denials"] = names
        warnings.append(f"{len(names)} tool call(s) were denied ({', '.join(dict.fromkeys(names))}): nobody can "
                        "approve a prompt in a child, so the work they were for was NOT done, whatever the result says")
    if isinstance(act, dict):
        out["tool_uses"] = act.get("tool_uses") or {}
        out["tool_errors"] = act.get("tool_errors") or 0
        if not out["tool_uses"]:
            # A heuristic (found live, 2026-11-02): a routed Codex child "succeeded" saying its filesystem was
            # read-only, with no tool call and no denial; it had believed the relay backend's own sandbox text. Any
            # claim in the result that something was done, or could not be, is then the model's word alone.
            warnings.append("no tool calls were made: whatever the result says was done (or was blocked) was not "
                            "attempted through a tool; check before trusting it")
        if act.get("files_written"):
            out["files_written"] = act["files_written"]
        mode = act.get("effective_permission_mode")
        if mode:
            out["effective_permission_mode"] = mode
            requested = meta.get("permission_mode")
            if mode != MODE_REPORTED_AS.get(requested, requested):
                warnings.append(f"the child ran in permission mode {mode!r}, not the requested "
                                f"{meta.get('permission_mode')!r} (Claude Code left it)")
        if act.get("writes_outside_cwd"):
            out["writes_outside_cwd"] = act["writes_outside_cwd"]
            warnings.append(f"the child wrote {len(act['writes_outside_cwd'])} file(s) outside its cwd "
                            f"{meta.get('cwd')} (confirmed by their tool results): check writes_outside_cwd before "
                            "trusting the result")
        attempts = act.get("write_attempts_unconfirmed") or []
        if attempts:
            out["write_attempts_unconfirmed"] = attempts
            failed = sum(1 for a in attempts if a.get("outcome") == "failed")
            warnings.append(f"{len(attempts)} file write(s) were attempted but not confirmed ({failed} failed, "
                            f"{len(attempts) - failed} without a result): those files may not exist")
    if meta.get("permission_mode_note"):
        out["permission_mode_note"] = meta["permission_mode_note"]
    return warnings


def _fetch_timeout(deadline) -> float:
    if deadline is None:
        return RUN_FETCH_TIMEOUT_S
    return max(RUN_FETCH_MIN_S, min(RUN_FETCH_TIMEOUT_S, deadline - time.monotonic()))


def wait(task_id, timeout_s=None, red=None, poll_s=0.5, provider_url="", token="", max_s=None) -> dict:
    """Poll for up to timeout_s, then status(). End to end the call takes at most timeout_s + POST_WAIT_RESERVE_S
    (the status() work after the loop is bounded by the same deadline), under CLIENT_REQUEST_TIMEOUT_S at the default."""
    start = time.monotonic()
    try:
        cap = wait_max_s() if max_s is None else float(max_s)
    except (TypeError, ValueError):
        raise TaskError("max_s must be a number of seconds") from None
    if cap != cap or cap <= 0:
        raise TaskError("max_s must be a positive number of seconds")
    try:
        t = float(min(WAIT_MAX_S, cap) if timeout_s is None else timeout_s)
    except (TypeError, ValueError):
        raise TaskError("timeout_s must be a number of seconds") from None
    if t != t or t < 0 or t > cap:
        raise TaskError(f"timeout_s must be between 0 and {cap:g} (MCP clients time out longer tool calls); "
                        "call wait_task again to keep waiting")
    d = task_path(task_id)
    end = start + t
    while time.monotonic() < end and _state(d)[0] == "running":     # deadline first: no state read past it
        time.sleep(max(0.0, min(poll_s, end - time.monotonic())))
    # The fetch gets what the reserve leaves after a possible recovery (RECOVERY_GRACE_S): at most ~5 s past the loop,
    # or RUN_FETCH_MIN_S after a recovery that used its grace. Worst case ~t + 6 s + a ps call, under t + reserve.
    out = status(task_id, red, provider_url, token, deadline=end + POST_WAIT_RESERVE_S - RECOVERY_GRACE_S)
    if out.get("state") == "running":
        out["wait_ended"] = (f"waited {t:g} s; the task is still running: call wait_task again to keep waiting "
                             "(or task_status to poll)")
    return out


def list_recent(n=20, ids=None) -> list:
    """`ids` (optional): list only these task ids (the bot gateway passes the ids it started, so a client's tasks
    stay listed however many newer tasks the owner has)."""
    try:
        n = max(1, min(int(n), 200))            # bounded: the listing goes into a tool result
    except (TypeError, ValueError):
        raise TaskError("limit must be an integer") from None
    root = _private_root()
    names = (x for x in os.listdir(root) if TASK_ID_RE.fullmatch(x))
    if ids is not None:
        wanted = {x for x in ids if isinstance(x, str)}
        names = (x for x in names if x in wanted)
    ids = sorted(names, reverse=True)[:n]
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
        r = subprocess.run([PS, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=PS_TIMEOUT_S)
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


# A stdout line that could not be redacted structurally is replaced by this event (still one JSON object per line, so
# the file stays a valid stream; readers skip its type).
WITHHELD_EVENT = json.dumps({"type": "cortados_withheld", "reason": redact.WITHHELD})


def _text_result(red: "Redactor", text):
    """The child's final result text, redacted as TEXT (each credential at every ESCAPE_DEPTH escaping): (text, 0),
    or (WITHHELD, 1) only if text() cannot run on it."""
    try:
        return red.text(text), 0
    except Exception:
        return redact.WITHHELD, 1


def redact_record(red: "Redactor", value):
    """(cleaned, withheld) for one decoded stdout record. Everything goes through red.deep_count (strict: JSON-looking
    text that does not decode is withheld), EXCEPT the free-form `result` string of a type "result" record: that is
    what the orchestrator reads, and prose may well start with `[`, `{` or `"`, so it gets text() redaction instead."""
    if isinstance(value, dict) and value.get("type") == "result" and isinstance(value.get("result"), str):
        rest = {k: v for k, v in value.items() if k != "result"}
        cleaned, withheld = red.deep_count(rest)
        if not isinstance(cleaned, dict) or "result" in cleaned:   # withheld, or a key that redacted onto "result"
            return redact.WITHHELD, withheld + (0 if cleaned == redact.WITHHELD else 1)
        text, n = _text_result(red, value["result"])
        cleaned["result"] = text
        return cleaned, withheld + n
    return red.deep_count(value)


def _structural(red: "Redactor", value):
    """(serialized, withheld) for one decoded record: redacted by redact_record and re-serialized ASCII-only (so no
    U+2028 or other line separator is written literally); WITHHELD_EVENT if it cannot be walked or serialized."""
    try:
        cleaned, withheld = redact_record(red, value)
        return json.dumps(cleaned), withheld
    except (ValueError, RecursionError):
        return WITHHELD_EVENT, 1


def redact_stream_file(path: str, red: "Redactor") -> int:
    """Structural redaction of claude's stdout, returning how much was withheld (result.json records it as
    redact_unparsed_lines). A legacy `--output-format json` record (one object, maybe pretty-printed) is parsed whole;
    otherwise each stream-json record (split on "\n" only: a JSON line holds no raw newline, but may hold U+2028) is
    parsed, its strings redacted (red.deep_count, which also decodes JSON held inside strings) and re-serialized. Fail
    closed: a record that does not parse, or is too deep to decode or walk, is replaced wholesale by WITHHELD_EVENT
    and counted, never redacted as text (a credential escaped more often than the raw pass matches would survive).
    redact_file runs after this as the second layer."""
    if not red.tokens or not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8", errors="replace", newline="") as src:
        whole = src.read()
    withheld = 0
    try:
        legacy = json.loads(whole) if whole.strip() else None
    except (ValueError, RecursionError):
        legacy = None
    if isinstance(legacy, dict):
        text, withheld = _structural(red, legacy)
        text += "\n"
    else:
        out = []
        for line in whole.split("\n"):
            body = line.rstrip("\r")
            if not body.strip():
                out.append(body)
                continue
            try:
                record = json.loads(body)
            except (ValueError, RecursionError):
                out.append(WITHHELD_EVENT)
                withheld += 1
                continue
            cleaned, n = _structural(red, record)
            out.append(cleaned)
            withheld += n
        text = "\n".join(out)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as dst:
            dst.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return withheld


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
    """(output, error): claude's result record, or why stdout is not one. stdout is `--output-format stream-json`
    (one event per line, the result record last) or, for a task started before that, the single `json` record. A
    valid record is an object with type "result", a boolean is_error and a string result."""
    raw = raw.strip()
    if not raw:
        return None, "the child printed nothing on stdout (no result record)"
    # The whole of stdout first: a legacy record (also pretty-printed over several lines) or a one-line stream. Then
    # stream records, split on "\n" only (str.splitlines would also split inside a record at U+2028/U+2029).
    try:
        out = json.loads(raw)
    except (ValueError, RecursionError):
        out = None
        lines = raw.split("\n")
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):  # not the result record (a deep line overflows json.loads)
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                out = ev
                break
        if out is None:
            if len(lines) == 1:
                return None, "stdout is not the JSON result claude -p --output-format json prints; see stdout_log"
            return None, "stdout has no result record (the event stream ended without one); see stdout_log"
    if not isinstance(out, dict):
        return None, f"stdout is JSON but not a result record ({type(out).__name__})"
    if out.get("type") != "result" or not isinstance(out.get("is_error"), bool) or not isinstance(out.get("result"), str):
        return out, "stdout is not a claude result record (needs type 'result', boolean is_error, string result)"
    return out, None


# Tools whose input names the file they change, and the input key holding it (Claude Code 2.1.316 built-ins).
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
MAX_ACTIVITY_PATHS = 50     # paths kept per list: enough to see what happened, bounded because it goes into status


def summarize_stream(path: str, cwd: str) -> dict:
    """What the child really did, from its stream-json events: tool_use counts by tool, how many tool results were
    errors, the permission mode it actually ran in (the last one a system event named), and its file writes. A write
    counts as done only when its tool_result (matched by tool_use id) came back without an error: `files_written` and
    `writes_outside_cwd` hold those; a write whose result was an error, or that never got one, is in
    `write_attempts_unconfirmed` with its outcome. A `result` saying "done" proves nothing on its own; this does."""
    tool_uses: dict = {}
    errors = 0
    mode = None
    attempts: dict = {}                     # tool_use id -> resolved path, in call order (dicts keep insertion order)
    outcome: dict = {}                      # tool_use id -> "ok" | "failed"
    root = os.path.realpath(cwd)
    with open(path, encoding="utf-8", errors="replace", newline="\n") as f:   # records end in "\n" only
        for line in f:
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):  # not an event, or nested too deep to be one: skipped
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "system" and isinstance(ev.get("permissionMode"), str):
                mode = ev["permissionMode"]
            content = (ev.get("message") or {}).get("content") if kind in ("assistant", "user") else None
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if kind == "assistant" and b.get("type") == "tool_use":
                    name = str(b.get("name") or "?")
                    tool_uses[name] = tool_uses.get(name, 0) + 1
                    key = WRITE_TOOLS.get(name)
                    target = (b.get("input") or {}).get(key) if key and isinstance(b.get("input"), dict) else None
                    if isinstance(target, str) and target and isinstance(b.get("id"), str):
                        attempts[b["id"]] = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
                elif kind == "user" and b.get("type") == "tool_result":
                    if b.get("is_error"):
                        errors += 1
                    tid = b.get("tool_use_id")
                    if tid in attempts and tid not in outcome:
                        outcome[tid] = "failed" if b.get("is_error") else "ok"
    written = [p for i, p in attempts.items() if outcome.get(i) == "ok"]
    unconfirmed = [{"path": p, "outcome": outcome.get(i, "unresolved")} for i, p in attempts.items()
                   if outcome.get(i) != "ok"]
    outside = [p for p in written if os.path.commonpath([root, p]) != root]
    return {"tool_uses": tool_uses, "tool_errors": errors, "effective_permission_mode": mode,
            "files_written": written[:MAX_ACTIVITY_PATHS], "writes_outside_cwd": outside[:MAX_ACTIVITY_PATHS],
            "write_attempts_unconfirmed": unconfirmed[:MAX_ACTIVITY_PATHS]}


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
    try:
        unparsed = redact_stream_file(os.path.join(d, "stdout.log"), red)   # structural first, then the raw pass
        if unparsed:
            result["redact_unparsed_lines"] = unparsed
    except Exception as exc:
        # Any failure of the structural pass fails the task and withholds its logs (redact_error), and the raw pass
        # below still runs: a runner that died here would leave the raw token in stdout.log.
        result["redact_error"] = f"could not redact stdout.log structurally: {type(exc).__name__}"
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
        result["output"], n = redact_record(red, out)  # fails closed: what cannot be walked is WITHHELD, and counted
        if n:
            result["redact_unparsed_lines"] = result.get("redact_unparsed_lines", 0) + n
    if err:
        result["output_error"] = err
    try:
        result["activity"], n = red.deep_count(summarize_stream(os.path.join(d, "stdout.log"), spec["cwd"]))
        if n:
            result["redact_unparsed_lines"] = result.get("redact_unparsed_lines", 0) + n
    except OSError as exc:                       # no stdout at all (the child never started): nothing to summarize
        result["activity_error"] = f"could not read stdout.log: {type(exc).__name__}"
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
