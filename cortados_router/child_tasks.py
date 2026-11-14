"""Child tasks: whole tasks handed to a separate headless Claude Code session on THIS machine, run through the CortadOS
provider, with only the result coming back. Backs the `start_task` / `task_status` / `wait_task` / `list_tasks` /
`cancel_task` tools of the `cortados-route` MCP server (subagent_mcp.py).

Why a separate module: route_task in subagent_mcp.py is standalone (stdlib, no package import). A child session needs
the whole-session launcher's env/argv construction (cortados_router.session.prepare: provider env, sub/<model> pin,
routed native subagents, 0600 settings file), so this module imports the package. subagent_mcp imports it lazily,
after putting the repo root on sys.path, so route_task keeps working even if this module cannot load.

Layout per task (all under a verified private 0700 directory, default ~/.config/cortados/tasks/<task_id>/):
  meta.json     what was asked (task text length, cwd, orchestrator, permission mode, runner pid, start time)
  prompt.txt    the task text, fed to `claude -p` on stdin (never argv: argv is world-readable in `ps` and size-capped);
                redacted in place by the runner with the logs once the child has exited
  session-*.json the launcher's 0600 settings (holds the provider token; never printed)
  stdout.log    claude's `--output-format stream-json` events, ending in its result record;  stderr.log  claude's diagnostics
  result.json   written by the runner when the child ends: exit code, end time, cancelled/timed-out flags
  cancelled     marker written by cancel_task before it signals the runner's process group

Run export (#91): when a task ends, the runner appends one finalized, redacted record of the run (prompt, assistant
text, tool calls and results, result, metadata, run-level model info) to the host's append-only export file
($CORTADOS_HOME/exports/task-runs.jsonl, or CORTADOS_TASK_EXPORT) for Grounds to capture; see "task run export" below.

The runner (`python3 -m cortados_router.child_tasks --run <dir>`) is a detached session leader (start_new_session), so
the child survives the MCP server (the desktop app may restart it) and cancel_task can signal exactly that process group.

Execution host (owner decision 2026-11-08, #89/#86): the child runs on the configured host (exec_hosts.py; default the
Air; host "here" is the machine running this server, 2026-11-13), not necessarily on the machine running this server. When that host is another machine, start() forwards the
request over ssh to this same module in --remote mode there, and keeps a stub (<task_id>/remote.json) so status, wait,
list and cancel know where to ask. On the host, a task gets a workspace under <root>/active/<task_id> (workspaces.py:
a git worktree of repos/<name>, or a plain folder) that the runner prepares before the child starts and settles
(archive/ or held/) after it ends. A chrome child on a `gui` host is launched as a per-task LaunchAgent in the
desktop session (gui/<uid>), because an ssh session cannot read the login Keychain that holds Claude's login; chrome
children take a host-wide lock one at a time (Chrome serves one client). A routed child that names `connectors` on a
`gui` host takes the same LaunchAgent path (the connectors' MCP OAuth tokens are in that Keychain too) but stays
routed: no chrome lock, no login mode; its env reaches the runner through 0600 task-dir files, never the plist.

Being told when a child ends (issue #87): `python3 cortados_router/child_tasks.py --watch <task_id> ...` is a blocking
CLI for the orchestrator's machine. It polls the same status path task_status uses (a remote task is asked over ssh;
never a local fallback), prints one JSON line of metadata per task as it ends, and exits with the outcome; run in the
background by a Claude Code session, its exit wakes the session. start_task returns the exact command as `watch_command`.
Stdlib only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import socket
import stat
import string
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

from cortados_router import exec_hosts, paths, redact, session, workspaces  # noqa: E402

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
# Orchestrator: 'cortados', an alias, a model id, or a friendly name with its spaces already turned into dashes
# (pin_model). The longest pinnable id is ~30 chars; 81 (1 + 80) is our bound that keeps a pasted blob out of argv,
# and the charset keeps it a plain model id.
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
# --watch (issue #87): the orchestrator's background watcher. It polls status() for each named task; a remote task
# costs one ssh round trip per poll, so the cadence is coarse (the session is woken when the command exits; a task is
# never missed, only seen up to one interval late).
WATCH_INTERVAL_S = 30
# A running task is "stalled" when nothing about it has changed for this long: state, phase, workspace state and
# last_output_at (the mtime of the child's stdout/stderr logs: claude -p streams an event per tool call and reply, so a
# child that is working writes). 30 minutes is well above any single tool call seen on a child (a full test suite, a
# clone, a long build run inside one Bash call) and far below the child's own 3 h cap (CHILD_MAX_S_DEFAULT), so a hung
# child surfaces hours earlier. The same window applies to a task whose status was readable and then stopped being
# (the host went away mid-run). Stalled is a REPORT, not a cancel: the task keeps running on its host; the orchestrator
# decides (task_status, cancel_task, or arm the watch again).
WATCH_STALL_S = 30 * 60
# A task with a `phase` (queued for the host's Chrome lock) gets a longer window: WATCH_QUEUED_STALL_S, defined with
# CHROME_QUEUE_MAX_S below.
# A task whose status has NEVER been read (bad host, host down from the start) is given this many consecutive attempts,
# one interval apart, before the watch reports that it cannot watch it (exit 2): a typo or a down host should fail in
# about a minute, not after the stall window.
WATCH_CONNECT_ATTEMPTS = 3
TERMINAL_STATES = ("succeeded", "failed", "cancelled")
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

# --- execution host, workspaces, launchd (2026-11-08) -------------------------------------------------------------
REMOTE_STUB = "remote.json"         # in a local task dir: {"host": <name>} — the task lives on that execution host
WORKSPACE_FILE = "workspace.json"   # in the task dir on the host: the workspaces.prepare()/settle() record
LAUNCHD_FILE = "launchd.json"       # in the task dir: {"label", "domain", "plist", "booted_out"} for a launchd task
QUEUED_MARKER = "queued"            # in the task dir while the runner waits for the host's Chrome lock
LAUNCHCTL = "/bin/launchctl"        # absolute, like PS: MCP servers start with a thin PATH
LAUNCHD_LABEL_PREFIX = "com.cortados.task."
# launchctl answers at once; a hung one must not hold start_task (the MCP client's 60 s) hostage.
LAUNCHCTL_TIMEOUT_S = 20
# One Chrome per host, one client at a time (Claude in Chrome): chrome children on a host serialize on this lock,
# in the private tasks dir. Polled, not blocking: the wait must notice a cancel (SIGTERM) and the cancelled marker.
CHROME_LOCK = "chrome.lock"
CHROME_QUEUE_POLL_S = 0.5
# A browser task that has waited this long for Chrome is stale (the orchestrator has moved on); it fails instead of
# starting hours later unattended. Separate from CHILD_MAX_S, which counts from the spawn.
CHROME_QUEUE_MAX_S = 6 * 3600
# --watch's window for a task with a `phase` (queued for that lock). A queued task is waiting, not working: no log is
# written while it waits, so the ordinary WATCH_STALL_S would call every queue a stall. The lock's holder is a chrome
# child that CHILD_MAX_S_DEFAULT ends within 3 h, but the queue is not one deep: a third chrome task legitimately waits
# behind two, up to the runner's own queue cap (CHROME_QUEUE_MAX_S, 6 h), after which the runner itself fails the task.
# So a phase unchanged for that cap plus the ordinary window is a runner that died waiting or a lock that nobody
# releases. Same report as a stall (not a cancel).
WATCH_QUEUED_STALL_S = CHROME_QUEUE_MAX_S + WATCH_STALL_S
# A routed launchd runner (connectors on a gui host) gets its env from this 0600 file, not from the plist: the plist
# would otherwise carry whatever the server's env holds. session-*.json, so _remove_settings deletes it with the
# settings file. It omits every key the launcher's 0600 settings file sets (the provider token among them): the runner
# takes those from the settings file, so the token is at rest in one file only.
RUNNER_ENV_FILE = "session-runner-env.json"
# Observed on the Air 2026-11-09: a child spawned from an ssh session got no tools from its connectors and
# CREDENTIAL_STORE_UNAVAILABLE, because Claude Code keeps MCP OAuth tokens in the login Keychain.
CONNECTORS_NO_GUI_WARNING = ("connectors were requested on an execution host without `gui: true`: the child is spawned "
                             "outside the desktop session and may not reach the login Keychain where Claude Code keeps "
                             "MCP OAuth tokens, so OAuth connectors may load no tools (CREDENTIAL_STORE_UNAVAILABLE); "
                             "set `gui: true` on the host if a user is logged in to its desktop")
# A remote wait is shortened by this much so the ssh round trip (connect + reply) still lands inside the local cap.
SSH_OVERHEAD_S = 3
# start_task runs the archive cleanup opportunistically (so it happens even where the daily job is not installed)
# but deletes at most this many entries per call: start_task must answer within the MCP client's timeout.
_REMOTE_SERVING = False             # set in --remote mode: a request that still resolves to another host is a loop


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
                   "CORTADOS_HOME", "CORTADOS_HERMES_HOME", "CORTADOS_TASKS_DIR", "CORTADOS_TASK_EXPORT",
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
    """`ambiguous` (start_task on a remote host): the launch request may have reached the host, so a child may be
    running although no task id came back. Callers that hold a concurrency slot keep it (bot_gateway)."""

    def __init__(self, *args, ambiguous: bool = False):
        super().__init__(*args)
        self.ambiguous = ambiguous


LAUNCHED = ("none", "maybe", "yes")     # StartFailed.launched: how far start() got before the unexpected exception
# A StartFailed type from an execution host's reply is shown to the caller: only a bounded identifier passes, anything
# else (long text, a non-string, a newline) becomes REMOTE_ERROR_TYPE.
EXC_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
REMOTE_ERROR_TYPE = "RemoteError"
# StartFailed types that are not a bug but a launch whose outcome is unknown: the guidance names the cause instead of
# "unexpected X (a cortados-router bug)". Keyed by type so an execution host's reply rebuilds the same words here.
LAUNCH_UNCERTAIN = {
    "LaunchctlTimeout": f"launchctl bootstrap did not answer within {LAUNCHCTL_TIMEOUT_S} s, so launchd may have "
                        "loaded the task's job",
    "LaunchdBootoutFailed": "the task's bookkeeping failed after its launchd job was loaded, and launchctl bootout "
                            "could not confirm the job was stopped",
    "ChildStopUnconfirmed": "the task's bookkeeping failed after its runner was started; the runner was stopped, but "
                            "its claude process group could not be confirmed stopped",
}


class LaunchUncertain(Exception):
    """Raised by _launch_launchd when the bootstrap request may have taken effect (launchd may have loaded the job and
    started the runner) although it did not answer: start() keeps the task record, so the task stays trackable and
    cancellable, and reports it as launched="yes" with its id. `kind` is the StartFailed type."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def start_failure_text(exc_type: str, where: str, launched: str, task_id=None) -> str:
    """The guidance for an unexpected exception inside start(): the exception TYPE (its text is unscrubbed and may
    quote record content, #94), where it happened, and what to do given how far the launch got. Built the same way
    for a local start and for one on an execution host (from the classification its --remote reply carries)."""
    cause = LAUNCH_UNCERTAIN.get(exc_type)
    if cause:
        head = f"start_task failed in {where}: {cause}"
        if launched == "yes" and task_id:
            return (f"{head}. Task {task_id} may be running: check it with task_status {task_id} (cancel_task "
                    f"{task_id} stops it) instead of calling start_task again.")
    else:
        head = (f"start_task failed: unexpected {exc_type} in {where} (a cortados-router bug: please report it with "
                "the time)")
    if launched == "yes" and task_id:
        return (f"{head}, after task {task_id} was launched: it may be running. Check it with task_status "
                f"{task_id} (cancel_task {task_id} stops it) instead of calling start_task again.")
    if launched == "none":
        return f"{head}, before any task was launched: nothing is running, and retrying may work."
    return (f"{head}, while a task was being launched. No task id came back, but a task may have started anyway: run "
            "list_tasks and look for it (by title or start time) before calling start_task again, so it does not run "
            "twice.")


class StartFailed(TaskError):
    """An unexpected (non-TaskError) exception inside start(), classified at the launch boundary: `launched` is
    "none" (nothing was spawned or sent: retrying is safe), "maybe" (it happened during the spawn or the request to
    the execution host) or "yes" (after the launch; `task_id` names the task). Only "maybe"/"yes" are ambiguous. The
    message is complete guidance (start_failure_text); `exc_type` is the original exception's class name."""

    def __init__(self, exc_type: str, where: str, launched: str, task_id=None):
        launched = launched if launched in LAUNCHED else "maybe"
        task_id = task_id if isinstance(task_id, str) and TASK_ID_RE.fullmatch(task_id) else None
        if launched == "yes" and task_id is None:
            launched = "maybe"
        super().__init__(start_failure_text(exc_type, where, launched, task_id), ambiguous=launched != "none")
        self.exc_type, self.where, self.launched, self.task_id = exc_type, where, launched, task_id

    def failure(self) -> dict:
        """The classification for a --remote reply (no text: the caller rebuilds the guidance with its own words)."""
        return {"type": self.exc_type, "launched": self.launched, "task_id": self.task_id}


class RecordError(TaskError):
    """A task record on disk (meta.json, result.json, remote.json, ...) that exists but cannot be read: corrupt JSON
    (a crash mid-write, a stray edit) or an OS error other than "missing". Names the task id, the file and the
    exception TYPE only: a JSONDecodeError's message quotes the content, which may be a path or a prompt (#94).
    A TaskError, so status() of that one task answers with a clear message; prune() and list_recent() skip the
    record instead of failing every later start_task, and --watch treats it as a local read error, not the host's
    answer."""

    def __init__(self, task_id: str, file: str, exc: BaseException):
        self.task_id, self.file, self.kind = task_id, file, type(exc).__name__
        super().__init__(f"task {task_id}: {file} is unreadable ({self.kind}); the record is left in place")


def _note_unreadable(exc: "RecordError") -> str:
    """One lifecycle-log line per unreadable record seen (id, file, type: never content); returns the errors entry."""
    log_event("record_unreadable", task_id=exc.task_id, file=exc.file, error=exc.kind)
    return str(exc)


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


def check_login_settings(cwd=None) -> None:
    """Fail closed: refuse a chrome child if a settings file it would load sets an API key/token/base URL in env or an
    apiKeyHelper; each would disable Claude in Chrome or send the session away from the login. Managed policy files
    outrank every file checked here, so they are checked too (env keys via session.check_managed, plus apiKeyHelper).
    cwd None: the project files are not checked yet (the workspace does not exist until the runner prepares it; the
    runner calls this again with the real cwd before it spawns)."""
    try:
        session.check_managed(LOGIN_BAD_SETTINGS_ENV, MANAGED_DIRS)
        managed = [str(f) for f in session.managed_settings_files(MANAGED_DIRS)]
    except session.SessionError as exc:
        raise TaskError(f"chrome task refused: {exc}") from None
    project = [os.path.join(cwd, ".claude", "settings.json"), os.path.join(cwd, ".claude", "settings.local.json")] if cwd else []
    for path in managed + [os.path.expanduser(USER_CLAUDE_SETTINGS)] + project:
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
    """A task-dir record: None when the file does not exist, RecordError (a TaskError) when it exists but cannot be
    read or parsed. Never a raw JSONDecodeError: its text quotes the file."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        raise RecordError(os.path.basename(os.path.dirname(path)), os.path.basename(path), exc) from None


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
    if not isinstance(name, str):
        raise TaskError(f"invalid model {name!r}: use 'cortados' or a pinnable model name (e.g. fable, opus, gpt-6.1-sol)")
    # A friendly name's spaces become dashes (`GPT 6.1` -> `gpt-6.1`): the provider's resolver (routing.resolve_pin)
    # reads the two alike, and the id then stays one argv token for the child's --model.
    name = re.sub(r"\s+", "-", name.strip()).lower()
    if not ORCHESTRATOR_RE.fullmatch(name):
        raise TaskError(f"invalid model {name!r}: use 'cortados' or a pinnable model name (e.g. fable, opus, gpt-6.1-sol, "
                        "'GPT 6.1', 'ollama 27b q3km')")
    try:
        return session.resolve_model(name)
    except session.SessionError as exc:
        raise TaskError(str(exc)) from None


def claude_bin(host_claude=None) -> str:
    """The claude executable: CORTADOS_CHILD_CLAUDE_BIN, else the local host entry's `claude` (`host_claude`), else
    `claude` on PATH. The desktop app starts MCP servers with a thin PATH, so a missing binary is reported (with the
    variable to set) instead of failing inside the runner."""
    name = os.environ.get("CORTADOS_CHILD_CLAUDE_BIN") or host_claude or "claude"
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
    """Everything about a request that needs no filesystem (so the requesting machine can refuse before forwarding).
    cwd: None, or an absolute path; whether it exists is checked on the execution host (_check_cwd_here)."""
    if not isinstance(task, str) or not task.strip():
        raise TaskError("start_task requires a non-empty 'task'")
    if len(task) > MAX_TASK_CHARS:
        raise TaskError(f"task is {len(task)} characters; the limit is {MAX_TASK_CHARS} (put large inputs in files under cwd)")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        raise TaskError("cwd, when given, must be an absolute directory path on the execution host")
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


def _check_cwd_here(cwd: str, host: "exec_hosts.Host") -> None:
    """cwd must exist on THIS host (the execution host). The message names the host so a path that exists only on the
    requesting machine is understood as such (start() adds that it exists there when it does)."""
    if not os.path.isdir(cwd):
        raise TaskError(f"cwd {cwd!r} does not exist on execution host {host.describe()}; name a `repo` (and `branch`) "
                        f"for a workspace under {host.root or '<root>'}/active/<task_id>, or a cwd that exists there")


def _workspace_request(task_id, cwd, repo, branch, host: "exec_hosts.Host"):
    """The workspace spec for a task (None when cwd is given: an existing folder on the host is not ours to move)."""
    if cwd is not None and (repo is not None or branch is not None):
        raise TaskError("pass either cwd (an existing directory on the execution host) or repo [+ branch]; not both")
    if cwd is not None:
        return None
    try:
        spec = workspaces.workspace_spec(task_id, repo, branch)
    except workspaces.WorkspaceError as exc:
        raise TaskError(str(exc)) from None
    if not host.root:
        raise TaskError(f"execution host {host.describe()} has no workspace `root` configured; pass cwd, or add root "
                        f"to its entry in {exec_hosts.config_path()}")
    return spec


def connectors_config() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("CORTADOS_CONNECTORS_MCP_CONFIG") or CONNECTORS_CONFIG))


def _prune_export(d: str, meta: dict, res, red, budget: dict) -> str | None:
    """Before a finished task dir is deleted: its run export (#91) must have succeeded. If it has not, try once more
    (a recovered task's logs are released first when this process's credentials cover the launch ones, else the
    record is written with the logs withheld). All of one prune's retries share `budget`: at most
    PRUNE_EXPORT_RETRIES of them within PRUNE_EXPORT_BUDGET_S seconds (the export lock is polled only for what is
    left), so a stuck export lock cannot hold start_task. Returns None when the dir may go, else why it stays."""
    if isinstance(res, dict) and (res.get("export") or {}).get("ok"):
        return None
    if budget["left"] <= 0:
        return f"export not retried in this prune (at most {PRUNE_EXPORT_RETRIES} per call)"
    if budget["until"] - time.monotonic() <= 0:
        return f"export not retried in this prune (its {PRUNE_EXPORT_BUDGET_S:g} s budget for export retries is used up)"
    budget["left"] -= 1
    try:
        cur = _current_redactor(red)
    except TaskError as exc:
        return f"export not retried ({exc})"
    if isinstance(res, dict) and res.get("logs_unredacted"):
        try:
            _release_recovered_logs(d, meta, res, cur)   # on failure the record goes out with the logs withheld
        except OSError:
            pass
    ex = _export_locked(d, meta, cur, EXPORT_LATE_ROUTER_RUN, res, lock_wait_s=budget["until"] - time.monotonic())
    if ex is None:
        return "export not retried (the task's runner still holds its lock)"
    return None if ex.get("ok") else f"export failed ({ex.get('error')})"


def prune(root: str, now: float | None = None, errors: list | None = None, red=None) -> list:
    """Remove task dirs whose task started more than PRUNE_AGE_S ago and is not running. Returns the removed ids. Each
    dir is handled on its own: one that cannot be read or removed is recorded in `errors` and skipped, so it never
    blocks every later start_task (#94). An unreadable record (RecordError) is never deleted automatically, whatever
    its age or lock: a corrupt meta.json means the child's pid cannot be read, so the dir may still belong to a live
    child whose runner died (a free runner.lock and an old dir mtime prove nothing; appending to logs does not touch
    the dir's mtime), and removing it would take away the records _recover() / cancel need to stop that child. It
    stays, reported in `errors` and logged by id, file and type (never content), until a person removes it.
    A finished task whose run export (#91) has not succeeded is not deleted either: the export is retried once per
    prune (_prune_export, at most PRUNE_EXPORT_RETRIES tasks within PRUNE_EXPORT_BUDGET_S per call); while it fails
    the dir stays, reported in `errors` and logged as `task_kept_unexported`."""
    cutoff = (time.time() if now is None else now) - PRUNE_AGE_S
    removed = []
    budget = {"left": PRUNE_EXPORT_RETRIES, "until": time.monotonic() + PRUNE_EXPORT_BUDGET_S}
    for name in os.listdir(root):
        d = os.path.join(root, name)
        try:
            if not TASK_ID_RE.fullmatch(name) or os.path.islink(d) or not os.path.isdir(d):
                continue
            try:
                state, meta, res = _state(d)
            except RecordError as exc:
                if errors is not None:
                    errors.append(_note_unreadable(exc))
                else:
                    _note_unreadable(exc)
                continue                        # kept: see the docstring
            started = meta.get("started_at") if state != "remote" else meta.get("created_at")
            if not isinstance(started, (int, float)):
                started = os.path.getmtime(d)
            # A remote stub is only a pointer (the host prunes the task itself): dropped by age alone.
            if state != "running" and started < cutoff:
                why = _prune_export(d, meta, res, red, budget) if state != "remote" else None
                if why:
                    log_event("task_kept_unexported", task_id=name, reason=why)
                    if errors is not None:
                        errors.append(f"{name}: not pruned: {why}")
                    continue
                if meta.get("launch") == "launchd":
                    _launchd_bootout(d)         # idempotent; a corrupt launchd.json already raised from _state() above
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


# The one place the bot gateway's name is written: its client (bot_gateway.CLIENT), its tasks' origin
# (subagent_mcp.task_origin), the live view's ?session= rule and the server's 400 message all derive from it.
BOT_ORIGIN = "grokbot"
ORIGIN_NAMED = (BOT_ORIGIN,)    # origins that are not a session
ORIGIN_SESSION_RE = re.compile("|".join([SESSION_TAG_RE.pattern] + [re.escape(n) for n in ORIGIN_NAMED]))
ORIGIN_KEYS = ("origin_session", "origin_project", "origin_device")
OFFICE_NAME_MAX = 64            # cortados-routed-subagents' OFFICE_NAME_MAX


def office_name(s, device=False):
    """A project or device name normalised as cortados-routed-subagents' officeName does it (every run of characters
    outside [A-Za-z0-9._ -] becomes "-", device without spaces; trimmed; cut to 64), or None without a letter or digit.
    The one Python copy: subagent_mcp.origin_identity uses it, and tests/test_child_tasks.OFFICE_NAME_VECTORS pins it
    against the mod (hooks/office-vectors.fixture.ts)."""
    t = re.sub(r"[^A-Za-z0-9._-]+" if device else r"[^A-Za-z0-9._ -]+", "-", str(s or "")).strip()[:OFFICE_NAME_MAX]
    return t if re.search(r"[A-Za-z0-9]", t) else None


def clean_origin(origin) -> dict:
    """The main session that started a task, as {origin_session, origin_project, origin_device}: the session's opaque
    Office tag (16-64 lowercase hex) or a named origin (ORIGIN_NAMED), its folder name and device. Metadata only; a
    value that does not fit is None, never repaired or guessed, and never fails the start (subagent_mcp.task_origin)."""
    o = origin if isinstance(origin, dict) else {}
    sess, proj, dev = o.get("session"), o.get("project"), o.get("device")
    ok = lambda v, rule: v if isinstance(v, str) and rule.fullmatch(v) else None
    return {"origin_session": ok(sess, ORIGIN_SESSION_RE),
            "origin_project": ok(proj, _OFFICE_NAME_RE), "origin_device": ok(dev, _DEVICE_RE)}


_OFFICE_HEADER_OF = {"session": "x-cortados-session", "project": "x-cortados-project", "device": "x-cortados-device",
                     "title": "x-cortados-task-title"}


def _hosts() -> "exec_hosts.Hosts":
    try:
        return exec_hosts.load()
    except exec_hosts.HostError as exc:
        raise TaskError(str(exc)) from None


def _resolve_host(name):
    """The execution host named `name` (None: CORTADOS_EXEC_HOST or the file's default). An unknown name is a
    TaskError carrying the configured names: a bare HostError would escape start() and reach the caller as an
    'internal error (HostError)' with the reason dropped."""
    hosts = _hosts()
    try:
        return hosts.resolve(name)
    except exec_hosts.HostError as exc:
        raise TaskError(str(exc)) from None


def _remote(host_name: str, op: str, args: dict, timeout_s=None) -> dict:
    """One operation on another execution host. Unreachable or failing hosts are TaskErrors with the host named;
    nothing ever falls back to this machine."""
    host = _resolve_host(host_name)
    try:
        out = exec_hosts.call(host, op, args, **({"timeout_s": timeout_s} if timeout_s is not None else {}))
    except exec_hosts.HostError as exc:            # includes HostUnreachable
        failure = getattr(exc, "failure", None)
        if op == "start" and isinstance(failure, dict):
            # The host hit an unexpected exception inside its start(): the same guidance as here, naming the host.
            # Its type is shown to the caller: a bounded identifier only, never free text from the reply.
            exc_type = failure.get("type")
            if not (isinstance(exc_type, str) and EXC_TYPE_RE.fullmatch(exc_type)):
                exc_type = REMOTE_ERROR_TYPE
            raise StartFailed(exc_type, f"execution host {host.describe()}",
                              failure.get("launched"), failure.get("task_id")) from None
        raise TaskError(str(exc), ambiguous=getattr(exc, "ambiguous", False)) from None
    if not isinstance(out, (dict, list)):
        raise TaskError(f"execution host {host.describe()} answered {op} with {type(out).__name__}, not a record")
    return out


def _write_stub(root: str, task_id: str, host_name: str) -> None:
    """The local record of a task running on execution host `host_name` (status, wait and cancel go there)."""
    d = os.path.join(root, task_id)
    os.mkdir(d, 0o700)
    _write_json(os.path.join(d, REMOTE_STUB), {"host": host_name, "created_at": time.time()})


def _stub(d: str):
    """The remote stub of a local task dir ({"host": ...}), or None for a task that runs on this machine."""
    rec = _read_json(os.path.join(d, REMOTE_STUB))
    return rec if isinstance(rec, dict) and isinstance(rec.get("host"), str) else None


def start(*args, **kwargs) -> dict:
    """Start a child task (_start has the arguments). A TaskError passes through; any other exception becomes a
    StartFailed classified by how far the launch got (`progress`, advanced by _start at the launch boundary), so the
    caller is told whether a task may be running, and never the exception's text."""
    progress = {"launched": "none", "task_id": None}
    try:
        return _start(progress, *args, **kwargs)
    except TaskError:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)    # the full exception for the operator's log, not for the caller
        raise StartFailed(type(exc).__name__, _start_where(), progress["launched"], progress["task_id"]) from None


def _start_where() -> str:
    return (f"execution host {exec_hosts.short_hostname()}" if _REMOTE_SERVING
            else f"the cortados-route server on {exec_hosts.short_hostname()}")


def _start(progress, task, cwd=None, orchestrator="cortados", permission_mode=None, allowed_tools=None,
           provider_url="", token_file=None, chrome=False, red=None, connectors=None, client=None,
           title=None, session_tag=None, repo=None, branch=None, host=None, parent_task_id=None, local=None,
           origin=None) -> dict:
    """`client` (optional, e.g. "grokbot" from the bot gateway) is recorded in meta.json and every event line, so usage
    can be attributed to the caller. `red` carries the launch credentials (env token and token file's token as they are now). `permission_mode`
    None picks auto where the model supports it (resolve_permission_mode). `title` (<= 80 chars) and `session_tag`
    (the orchestrator's opaque tag) label the task in the Office view: on its events, and as headers on its routed
    requests. `host` names the execution host (default: the configured default); `cwd` an existing directory THERE,
    or `repo` [+ `branch`] a workspace the host prepares (neither: a plain folder under the host's active/).
    `parent_task_id` (else CORTADOS_PARENT_TASK_ID) names the task that started this one, for the run export (#91);
    none known is recorded as an explicit missing parent. `local` (session.LOCAL_MODES) makes every request of the
    child carry x-cortados-local (the provider's sensitive-request routing); login mode (chrome) is not routed, so the
    two are refused together. `origin` ({session, project, device}, subagent_mcp.task_origin) names the main session
    that started it: meta.json's `origin` and every event's origin_* fields (clean_origin)."""
    if os.environ.get(NESTED_ENV):
        raise TaskError("start_task is not available inside a child task: children may not start children "
                        "(unbounded fan-out would spend subscription quota unattended)")
    tools = _validate(task, cwd, orchestrator, permission_mode, allowed_tools)
    parent_id, parent_note = resolve_parent(parent_task_id)
    if not isinstance(chrome, bool):
        raise TaskError("chrome must be true or false")
    if local in ("", None):
        local = None
    elif local not in session.LOCAL_MODES:
        raise TaskError(f"local must be one of {', '.join(session.LOCAL_MODES)}")
    elif chrome:
        raise TaskError("local is not used with chrome: true (login mode is not routed, so no request could go local); "
                        "omit one of them")
    if host is not None and not isinstance(host, str):
        raise TaskError("host must be the name of a configured execution host, or 'here' for this machine")
    target = _resolve_host(host)
    if not target.is_local():
        if _REMOTE_SERVING:
            raise TaskError(f"this request reached {exec_hosts.short_hostname()} for host {target.describe()}, but that "
                            f"entry does not name this machine in `hostnames`; fix {exec_hosts.config_path()} here")
        # Cheap refusals first (the same checks the host makes), so a bad request never costs an ssh round trip.
        office_tags(None, title, session_tag)
        _workspace_request("t00000000000000-00000000", cwd, repo, branch, target)
        if chrome:
            login_model(orchestrator)
            if not target.gui:
                raise TaskError(f"chrome tasks on execution host {target.describe()} need `gui: true` in its entry: a "
                                "chrome child must start inside the desktop session (an ssh session cannot read the "
                                "login Keychain that holds Claude's login)")
        root = _private_root()  # fail before ssh: a remotely launched task must always be locally trackable
        # The host redacts with ITS credentials only: this machine's are checked here, by the local rules (#91 review
        # round 3, P1): what the task runs with is refused, labels and the prompt are redacted before they leave.
        start_red = _start_redactor(red)
        _refuse_credentials(start_red, {"cwd": cwd, "repo": repo, "branch": branch, "orchestrator": orchestrator,
                                        "allowed_tools": tools, "session": session_tag})
        args = {"task": start_red.text(task), "cwd": cwd, "orchestrator": orchestrator,
                "permission_mode": permission_mode, "allowed_tools": allowed_tools, "chrome": chrome,
                "connectors": connectors, "client": start_red.text(str(client)) if client else client,
                "title": start_red.text(title) if isinstance(title, str) else title,
                "session_tag": session_tag, "repo": repo, "branch": branch,
                "parent_task_id": parent_id,   # resolved here: the host's own env does not know this session
                "origin": origin, **({"local": local} if local else {})}
        progress["launched"] = "maybe"         # from here the request may reach the host and launch a child
        try:
            out = _remote(target.name, "start", args)
        except StartFailed as exc:
            if exc.launched == "yes":
                try:                            # the host has the task: make it trackable here, as a success would
                    _write_stub(root, exc.task_id, target.name)
                except Exception:
                    raise StartFailed(exc.exc_type, exc.where, "maybe") from None
            raise
        except TaskError as exc:
            msg = str(exc)
            if cwd and "does not exist on execution host" in msg and os.path.isdir(cwd):
                msg += (f" (it exists on this machine, {exec_hosts.short_hostname()}, only: child tasks run on "
                        f"{target.describe()}, not here)")
            raise TaskError(msg, ambiguous=exc.ambiguous) from None
        tid = out.get("task_id") if isinstance(out, dict) else None
        if not isinstance(tid, str) or not TASK_ID_RE.fullmatch(tid):
            raise TaskError(f"execution host {target.describe()} started a task but returned no valid task_id",
                            ambiguous=True)
        progress.update(launched="yes", task_id=tid)
        d = os.path.join(root, tid)
        try:
            _write_stub(root, tid, target.name)
        except BaseException as exc:
            shutil.rmtree(d, ignore_errors=True)
            try:
                _remote(target.name, "cancel", {"task_id": tid})
            except TaskError as cancel_exc:
                raise TaskError(f"remote task {tid} started but local bookkeeping failed ({type(exc).__name__}); "
                                f"remote cancellation also failed: {cancel_exc}", ambiguous=True) from None
            raise TaskError(f"remote task {tid} started but local bookkeeping failed ({type(exc).__name__}); "
                            "the remote task was cancelled") from None
        out["host"] = target.name
        out["watch_command"] = watch_command(tid)   # THIS machine's command (the host's own, if any, is not ours)
        if local and out.get("local") != local:
            # An execution host on an older checkout ignores the option it does not know: say so, never imply it held.
            out.setdefault("warnings", []).append(
                f"execution host {target.describe()} did not confirm local: {local} (an older cortados-router checkout "
                "there?); the child's requests may not carry x-cortados-local. Update the checkout on that host.")
        return out
    task_id = _new_id()
    ws_spec = _workspace_request(task_id, cwd, repo, branch, target)   # validated before anything is created
    # The Office "project" is the cwd's basename, or the repository name for a workspace the runner will create.
    office = office_tags(cwd if cwd else (ws_spec or {}).get("name"), title, session_tag)
    # Caller text is checked against every known credential before anything about the task exists: a credential in
    # what the task runs with is refused, one in a label (the title) is redacted (#91 review round 2, P1).
    caller_fields = {"cwd": cwd, "repo": repo, "branch": branch, "orchestrator": orchestrator, "allowed_tools": tools,
                     "session": session_tag}
    start_red = _start_redactor(red)
    _refuse_credentials(start_red, caller_fields)
    office = {k: start_red.text(v) for k, v in office.items()}
    if cwd is not None:
        _check_cwd_here(cwd, target)
    login = None
    if chrome:
        login = login_model(orchestrator)
        if connectors:
            # Refused, not ignored: in login mode the user's claude.ai connectors load through the login itself, and a
            # silently dropped list would hide that from the caller.
            raise TaskError("connectors are not used with chrome: true (login mode loads the user's claude.ai "
                            "connectors through the login itself); omit connectors")
        check_login_settings(cwd)               # the project files are checked by the runner once the cwd exists
    permission_mode, mode_note = resolve_permission_mode(permission_mode, login or orchestrator)
    selected = select_connectors(None if chrome else connectors)
    if not provider_url and not chrome:
        try:
            provider_url = session.resolve_provider_url()
        except session.ProviderURLError as exc:
            raise TaskError(f"start_task is not configured: {exc}") from None
    max_s = _max_s()
    exe = claude_bin(target.claude)
    root = _private_root()
    prune_errors: list = []
    prune(root, errors=prune_errors, red=red)
    # No cap on parallel children. Routed children: capacity is the router's job (S1 and the quota gates refuse or
    # redirect a spent lane). Chrome (login-mode) children bypass S1 and the quota gates, so NOTHING here bounds N of
    # them against the main Claude subscription; they do run one at a time per host (CHROME_LOCK: Chrome serves one
    # client), the rest queue. A cap on their number would be an [ESCALATE] decision, not added silently.
    if ws_spec is not None:
        try:
            workspaces.ensure_root(target.root)
        except workspaces.WorkspaceError as exc:
            raise TaskError(str(exc)) from None
    # chrome, or a routed task with connectors, on a gui host: a LaunchAgent in the desktop session (the only place
    # the login Keychain is readable: Claude's login for chrome, the connectors' MCP OAuth tokens otherwise). A
    # connectors task stays routed: no chrome lock, no login mode.
    launchd = bool(target.gui and (chrome or selected))
    launch_warning = CONNECTORS_NO_GUI_WARNING if (selected and not target.gui) else None
    d = os.path.join(root, task_id)
    os.mkdir(d, 0o700)
    lock_fd = None
    if not launchd:
        # The runner's liveness IS this lock. It is taken before anything about the task is published (so no reader
        # ever sees meta.json with an unheld lock and mistakes a starting task for a crashed one), handed to the runner
        # as an inherited fd (same open file description, so the lock moves with it, no gap), and held by the runner
        # until it has written result.json. A pid alone can be reused. (A launchd runner cannot inherit an fd: it
        # takes the lock itself, and readers give it META_GRACE_S to appear, see _recover.)
        lock_fd = os.open(os.path.join(d, "runner.lock"), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = None
    try:
        meta, env, cmd = _prepare(d, task_id, task, cwd, orchestrator, permission_mode, tools, provider_url, token_file,
                                  start_red,
                                  chrome, max_s, exe, selected, login, office,
                                  workspace=ws_spec, ws_root=target.root, host_name=target.name, launchd=launchd,
                                  local=local)
        # Again with the child's launch env known: its credentials are the ones the export is checked against.
        launch_red = _start_redactor(red, env)
        _refuse_credentials(launch_red, caller_fields)
        if client:
            meta["client"] = launch_red.text(str(client))[:64]     # redact, THEN slice (a cut token would pass)
        if meta.get("office"):
            meta["office"] = {k: launch_red.text(v) for k, v in meta["office"].items()}
        meta["task_preview"] = launch_red.text(task.strip())[:200]
        org = clean_origin(origin)
        if any(org.values()):
            meta["origin"] = {k: launch_red.text(v) if v else v for k, v in org.items()}
        # cmd.json's event fields were written by _prepare before the client was known; the runner logs the finished
        # event from them, so rewrite them or that event loses its attribution (review r2 P3-8).
        cmd_path = os.path.join(d, "cmd.json")
        with open(cmd_path, encoding="utf-8") as f:
            spec = json.load(f)
        spec["event"] = _event_fields(meta)
        _write_json(cmd_path, spec)
        if mode_note:
            meta["permission_mode_note"] = mode_note
        if launch_warning:
            meta["launch_warning"] = launch_warning
        # For the run export (#91): the task that started this one (or why none is known), and the Office title.
        meta["parent_task_id"] = parent_id
        if local:
            meta["local"] = local
        if parent_note:
            meta["parent_note"] = parent_note
        if office.get("title"):
            meta["title"] = launch_red.text(office["title"])
        meta[META_TEXT_MARK] = True             # every caller text above was redacted or refused with launch_red
        _write_json(os.path.join(d, "meta.json"), meta)   # before the spawn: the runner's export reads these
        progress["launched"] = "maybe"         # the spawn itself: a failure inside it may leave a runner behind
        if launchd:
            # chrome: the allowlisted login env (no credential) rides in the plist; routed: nothing does (_prepare
            # wrote it to RUNNER_ENV_FILE and the runner loads it).
            _launch_launchd(d, env if login else {})
        else:
            with open(os.path.join(d, "runner.log"), "ab") as rlog:
                runner = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--run", d,
                                           "--lock-fd", str(lock_fd)],
                                          cwd=d, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=rlog, start_new_session=True, pass_fds=(lock_fd,))
    except BaseException as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        if isinstance(exc, LaunchUncertain):
            # launchd may have loaded the job and started the runner: the record stays (meta.json and launchd.json are
            # written), so task_status follows it (a runner that never appears fails it after META_GRACE_S) and
            # cancel_task boots it out. Deleting it would leave a possibly running task untrackable, and a retry
            # would run the work twice.
            progress.update(launched="yes", task_id=task_id)
            log_event("started", **_event_fields(meta), launch_uncertain=exc.kind)
            raise StartFailed(exc.kind, _start_where(), "yes", task_id) from None
        # Failure before or in the spawn, with nothing left running: roll back completely (a half-made dir would list
        # forever and could hold a token-bearing settings file). A runner Popen did return is ours to stop first.
        if runner is not None and not _abort_runner(runner, d):
            progress.update(launched="yes", task_id=task_id)
            raise StartFailed("ChildStopUnconfirmed", _start_where(), "yes", task_id) from None
        shutil.rmtree(d, ignore_errors=True)
        if isinstance(exc, OSError):
            raise TaskError(f"could not start the child task ({type(exc).__name__}: {exc})") from None
        raise
    progress.update(launched="yes", task_id=task_id)
    if lock_fd is not None:
        os.close(lock_fd)                       # our copy only; the runner's inherited fd keeps the lock
    try:
        current = _read_json(os.path.join(d, "meta.json")) or {}
        meta = {**meta, **current}  # preserve fields a fast launchd runner wrote after bootstrap, especially its pid
        if runner is not None:
            meta["runner_pid"] = runner.pid
        meta["event_log_error"] = log_event("started", **_event_fields(meta))
        _write_json(os.path.join(d, "meta.json"), meta)
    except BaseException as exc:
        # Post-spawn failure: a runner (and maybe claude) is live. Stop and reap them before deleting the dir, so no
        # task keeps running undiscoverable.
        # Only a confirmed stop deletes the record; otherwise it is kept, so the task stays trackable and
        # cancellable. The reply names the exception TYPE only: its text may quote record content (#94).
        if runner is not None:
            if not _abort_runner(runner, d):
                raise StartFailed("ChildStopUnconfirmed", _start_where(), "yes", task_id) from None
        else:
            _launchd_bootout(d)
            if not _launchd_gone(d):
                raise StartFailed("LaunchdBootoutFailed", _start_where(), "yes", task_id) from None
        shutil.rmtree(d, ignore_errors=True)
        raise TaskError(f"child task bookkeeping failed after spawn; the task was stopped ({type(exc).__name__})"
                        ) from None
    if runner is not None:
        _REAP[task_id] = runner
    out = {"task_id": task_id, "state": "running", "dir": d, "permission_mode": permission_mode, "host": target.name,
           "launch": "launchd" if launchd else "spawn", "watch_command": watch_command(task_id)}
    if ws_spec is not None:
        out["workspace"] = {"kind": ws_spec["kind"], "path": os.path.join(target.root, "active", task_id),
                            **({"repo": ws_spec["name"], "branch": ws_spec.get("branch") or workspaces.TASK_BRANCH_PREFIX + task_id}
                               if ws_spec["kind"] == "repo" else {})}
    else:
        out["cwd"] = cwd
    if login:
        out.update(lane=LOGIN_LANE, model=login, chrome_queue="chrome tasks run one at a time on this host; this one "
                   "waits for the Chrome lock while another browser task runs (task_status shows phase 'queued')")
    if local:
        out["local"] = local
    if mode_note:
        out["permission_mode_note"] = mode_note
    if launch_warning:
        out["warnings"] = [launch_warning]
    if prune_errors:
        out["prune_errors"] = prune_errors[:20]     # bounded: it goes into a tool result
    return out


def _launch_launchd(d: str, env: dict) -> None:
    """Start the runner as a one-shot LaunchAgent in the desktop session (gui/<uid>), so the child it spawns (chrome,
    or routed with connectors) runs where Chrome and the unlocked login Keychain are. The plist (0600, in the task
    dir) carries only `env`: a chrome child's allowlisted login env, nothing for a routed child (whose env the runner
    loads from RUNNER_ENV_FILE and the settings file). It is bootstrapped by path; launchd forgets it at logout/reboot and we boot it out when the task
    is final (_launchd_bootout). Why a LaunchAgent and not a Terminal window or `launchctl asuser`: no TCC
    Automation prompt, no visible window to be closed by someone, and `asuser` needs root."""
    label = LAUNCHD_LABEL_PREFIX + os.path.basename(d)
    domain = f"gui/{os.getuid()}"
    plist = os.path.join(d, "launchd.plist")
    spec = {"Label": label,
            "ProgramArguments": [sys.executable, "-B", str(Path(__file__).resolve()), "--run", d],
            "WorkingDirectory": d,
            **({"EnvironmentVariables": {k: str(v) for k, v in env.items()}} if env else {}),
            "RunAtLoad": True, "KeepAlive": False,
            "StandardOutPath": os.path.join(d, "runner.log"), "StandardErrorPath": os.path.join(d, "runner.log")}
    with open(os.open(plist, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        plistlib.dump(spec, f)
    _write_json(os.path.join(d, LAUNCHD_FILE), {"label": label, "domain": domain, "plist": plist, "booted_out": False})
    try:
        r = subprocess.run([LAUNCHCTL, "bootstrap", domain, plist], capture_output=True, text=True,
                           timeout=LAUNCHCTL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Killed while waiting for launchd's answer: the request may already have loaded the job (RunAtLoad starts
        # the runner at once). Not "not started": start() keeps the record and reports the task id.
        raise LaunchUncertain("LaunchctlTimeout") from None
    except OSError:
        raise                                   # launchctl could not be run at all: nothing was sent to launchd
    except Exception as exc:
        # After launchctl ran (e.g. its output did not decode): it may have loaded the job.
        raise LaunchUncertain(type(exc).__name__) from None
    # A definite answer: a non-zero exit is launchd refusing the job before loading it, so nothing runs.
    if r.returncode != 0:
        raise TaskError(f"could not start the task in the desktop session (launchctl bootstrap {domain} exited "
                        f"{r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}); is a user logged in to the desktop "
                        "of this execution host?")


LAUNCHCTL_NO_SUCH_SERVICE = 113        # launchctl's exit status for "Could not find specified service"


def _launchd_bootout(d: str) -> str:
    """Unload a task's one-shot job once it is final (or when a start failed after bootstrap). Idempotent; a job that
    launchd no longer knows is fine. Returns what happened (recorded in launchd.json). `booted_out` is persisted only
    once the job is confirmed gone (bootout succeeded, or `launchctl print` no longer finds the label): after a failed
    bootout the job may still be loaded, so a later status, cancel or cleanup tries again."""
    rec = _read_json(os.path.join(d, LAUNCHD_FILE))
    if not isinstance(rec, dict) or rec.get("booted_out"):
        return "not a launchd task" if not isinstance(rec, dict) else "already booted out"
    gone = False
    try:
        target = f"{rec['domain']}/{rec['label']}"
        r = subprocess.run([LAUNCHCTL, "bootout", target], capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT_S)
        if r.returncode == 0:
            note, gone = "booted out", True
        else:
            note = f"launchctl bootout exited {r.returncode}: {(r.stderr or '').strip()[-200:]}"
            p = subprocess.run([LAUNCHCTL, "print", target], capture_output=True, text=True,
                               timeout=LAUNCHCTL_TIMEOUT_S)
            if p.returncode == LAUNCHCTL_NO_SUCH_SERVICE:
                note, gone = "not loaded (launchd no longer knows the job)", True
            else:
                note += "; the job may still be loaded"
    except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
        note = f"launchctl bootout failed: {type(exc).__name__}; the job may still be loaded"
    rec.update(booted_out=gone, bootout_note=note)
    _write_json(os.path.join(d, LAUNCHD_FILE), rec)
    return note


def _launchd_gone(d: str) -> bool:
    """Whether the task's launchd job is confirmed unloaded (as recorded by _launchd_bootout)."""
    rec = _read_json(os.path.join(d, LAUNCHD_FILE))
    return isinstance(rec, dict) and rec.get("booted_out") is True


# _stop_recorded_child notes that leave the child possibly running (its identity could not be checked, so no signal)
CHILD_STOP_UNCONFIRMED = ("start time unknown",)


def _abort_runner(runner, d: str) -> bool:
    """Stop a runner this process just started: SIGTERM (it stops claude's group itself), then SIGKILL it and claude's
    recorded group if it does not exit in time. True when the stop is confirmed: the runner is reaped and claude's
    group is gone or was signalled; False when the group was left alone because its identity could not be checked."""
    try:
        runner.terminate()
        runner.wait(timeout=KILL_GRACE_S + 10)
    except subprocess.TimeoutExpired:
        runner.kill()
        runner.wait()
    return not _stop_recorded_child(d, 0).startswith(CHILD_STOP_UNCONFIRMED)


def _run_spec(cwd, max_s, meta, workspace=None, ws_root=None, launchd=False, chrome=False) -> dict:
    """cmd.json: what the runner needs. cwd is None until the runner prepares the workspace (it then fills it in)."""
    return {"cwd": cwd, "max_s": max_s, "grace_s": KILL_GRACE_S, "event": _event_fields(meta),
            "started_at": meta["started_at"], "workspace": workspace, "workspace_root": ws_root,
            "launchd": launchd, "chrome": chrome,
            "chrome_lock": os.path.join(tasks_dir(), CHROME_LOCK) if chrome else None}


def _prepare(d, task_id, task, cwd, orchestrator, permission_mode, tools, provider_url, token_file, red, chrome, max_s,
             exe, selected=None, login=None, office=None, workspace=None, ws_root=None, host_name=None, launchd=False,
             local=None):
    """Everything before the spawn: settings, prompt, cmd.json, meta.json. Returns (meta, env, cmd)."""
    os.chmod(d, 0o700)                          # mkdir's mode is masked by umask; the launcher verifies exactly 0700
    if login:
        meta, env, cmd = _prepare_login(d, task_id, task, cwd, orchestrator, permission_mode, tools, red, max_s, exe,
                                        login, office, workspace, ws_root, host_name, launchd)
        return meta, env, cmd
    launch = ["--orchestrator", orchestrator, "--provider-url", provider_url, "--settings-dir", d, "--run-id", task_id]
    if token_file:
        launch += ["--token-file", token_file]
    for k, v in (office or {}).items():         # validated by office_tags; session.py checks them again
        launch += ["--office-header", f"{_OFFICE_HEADER_OF[k]}: {v}"]
    if local:                                   # validated by _start; session.py checks it again
        launch += ["--local", local]
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
    env_file = _write_runner_env(d, env, cmd) if launchd else None
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
            "host": host_name, "launch": "launchd" if launchd else "spawn",
            "workspace": {"kind": workspace["kind"], "repo": workspace.get("name"), "branch": workspace.get("branch")} if workspace else None,
            # argv carries only the model, settings path and flags; the token lives in the 0600 settings file and env.
            "argv": cmd[1:], **({"office": office} if office else {})}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, **_run_spec(cwd, max_s, meta, workspace, ws_root, launchd, chrome),
                                              **({"env_file": env_file} if env_file else {})})
    _write_json(os.path.join(d, "meta.json"), meta)    # before Popen: a task dir always has its meta
    return meta, env, cmd


def _settings_path(cmd: list) -> str:
    return cmd[cmd.index("--settings") + 1]


def _write_runner_env(d: str, env: dict, cmd: list) -> str:
    """A routed launchd runner's env, minus what the 0600 settings file already holds (the runner merges that back):
    a variable the settings file sets is dropped, and a LAUNCH_TOKEN_KEYS variable that equals the provider token is
    recorded by name only. So no copy of the provider token is written here, and nothing at all goes in the plist."""
    with open(_settings_path(cmd), encoding="utf-8") as f:
        senv = json.load(f).get("env") or {}
    token = senv.get("ANTHROPIC_AUTH_TOKEN") or ""
    from_token = sorted(k for k in LAUNCH_TOKEN_KEYS if k not in senv and token and env.get(k) == token)
    base = {k: str(v) for k, v in env.items() if k not in senv and k not in from_token}
    path = os.path.join(d, RUNNER_ENV_FILE)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        json.dump({"env": base, "from_settings_token": from_token}, f)
    return path


def _private_json(path: str):
    """A 0600 JSON file of this user (not a symlink), or TaskError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise TaskError(f"cannot open {os.path.basename(path)}: {type(exc).__name__}") from None
    with open(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise TaskError(f"{os.path.basename(path)} is not a private 0600 file of this user")
        return json.load(f)


def _load_runner_env(spec: dict) -> None:
    """In a routed launchd runner, before anything reads the environment: make os.environ exactly the env the spawned
    runner would have inherited (RUNNER_ENV_FILE + the settings file's env), so the child and the redaction see it."""
    rec = _private_json(spec["env_file"])
    senv = _private_json(_settings_path(spec["argv"])).get("env") or {}
    env = dict(rec.get("env") or {})
    env.update({k: str(v) for k, v in senv.items()})
    for k in rec.get("from_settings_token") or ():
        if k in LAUNCH_TOKEN_KEYS:
            env[k] = str(senv.get("ANTHROPIC_AUTH_TOKEN", ""))
    os.environ.clear()
    os.environ.update(env)


def _prepare_login(d, task_id, task, cwd, orchestrator, permission_mode, tools, red, max_s, exe, model, office=None,
                   workspace=None, ws_root=None, host_name=None, launchd=False):
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
            "max_s": max_s, "runner_pid": None, "cred_fingerprints": launch_fps, "argv": cmd[1:],
            "host": host_name, "launch": "launchd" if launchd else "spawn",
            "workspace": {"kind": workspace["kind"], "repo": workspace.get("name"), "branch": workspace.get("branch")} if workspace else None}
    _write_json(os.path.join(d, "cmd.json"), {"argv": cmd, **_run_spec(cwd, max_s, meta, workspace, ws_root, launchd, True)})
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
    if meta.get("host"):
        out["host"] = meta["host"]                  # the execution host's name slug (exec_hosts), for the live view
    # Office fields (title, session tag, project, device): metadata the view shows, never the prompt.
    for k, v in (meta.get("office") or {}).items():
        out[k] = v
    # The main session that started the task (clean_origin): which orchestrator's work this is, for the live view.
    for k, v in clean_origin_meta(meta.get("origin")).items():
        out[k] = v
    return out


def clean_origin_meta(rec) -> dict:
    """meta.json's `origin` re-checked (the record is a file): its valid origin_* fields, None ones dropped."""
    r = rec if isinstance(rec, dict) else {}
    o = clean_origin({"session": r.get("origin_session"), "project": r.get("origin_project"),
                      "device": r.get("origin_device")})
    return {k: v for k, v in o.items() if v}


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
    stub = _stub(d)
    if stub:
        return "remote", stub, None             # lives on another execution host; callers ask there
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
    if meta.get("launch") == "launchd":
        _launchd_bootout(d)                     # final: the one-shot job is unloaded (idempotent)
    return classify(res), meta, res


def classify(res: dict) -> str:
    """Final state from a result record. Cancel, timeout and any recorded error override the exit status: a child
    that exits 0 on the SIGTERM sent at its timeout did not succeed."""
    if res.get("cancelled"):
        return "cancelled"
    if res.get("timed_out") or res.get("error") or res.get("output_error") or res.get("redact_error"):
        return "failed"
    act = res.get("activity")
    if isinstance(act, dict) and act.get("browser_disconnected"):
        # The browser tools answered that no extension was connected: the browser work did not happen, so a clean
        # exit and a confident result record do not make this a success (read from the tool results, not the text).
        return "failed"
    # Success needs exit 0 AND a valid claude result record that says it is not an error (parse_output's rules):
    # empty stdout, malformed JSON, a scalar or a record of the wrong shape is a failure, not a quiet success.
    out = res.get("output")
    if result_error(out, act) is not None:
        # The record is an error, or its text is one (an "API Error: ..." the CLI printed as the result, #97, with
        # the stream showing it was the CLI's own message): the child's last request failed, whatever is_error and
        # the exit status say.
        return "failed"
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
        elif fd is None and meta.get("launch") == "launchd":
            res = {"error": f"the desktop-session runner (launchd) did not start within {META_GRACE_S} s; see "
                            "runner.log and `launchctl print gui/<uid>/" + LAUNCHD_LABEL_PREFIX + os.path.basename(d) + "`",
                   "settings_removed": removed, "ended_at": time.time()}
        else:
            res = {"error": "the runner exited without recording a result (killed or crashed); see runner.log",
                   "settings_removed": removed, "ended_at": time.time()}
        res["child_cleanup"] = child
        res["logs_unredacted"] = True           # the runner never redacted them; see status()
        res["workspace"] = _settle_workspace(d)  # the child is stopped: its folder is archived or held, never left behind
        # Its run export waits for the logs: status() exports it once it may release them (_release_recovered_logs),
        # prune() with the logs withheld if that never happens before the dir would be deleted.
        res["export"] = {"ok": False, "at": round(time.time(), 3), "error": EXPORT_PENDING}
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
    if not launch_covered(meta, red):
        return False
    stdout = os.path.join(d, "stdout.log")
    structural_error = None
    try:
        unparsed = redact_stream_file(stdout, red)
    except Exception as exc:                    # as in _run: recorded, and the raw pass below still runs
        unparsed, structural_error = 0, f"could not redact stdout.log structurally: {type(exc).__name__}"
    try:
        for name in ("stdout.log", "stderr.log", "prompt.txt"):
            redact_file(os.path.join(d, name), red)
    except OSError:
        return False
    res["prompt_redacted"] = True
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
    res["logs_redacted"] = True
    _write_json(os.path.join(d, "result.json"), res)
    return True


def launch_covered(meta: dict, red: "Redactor") -> bool:
    """Whether `red` provably holds every launch credential (sha256 fingerprints in meta.json). None = an old/odd
    meta: not covered. [] = the child ran with no launch credential (login mode): covered."""
    launch = meta.get("cred_fingerprints") if isinstance(meta, dict) else None
    return isinstance(launch, list) and set(launch) <= set(credential_fingerprints(red.raw))


def status(task_id, red=None, provider_url="", token="", deadline=None) -> dict:
    """deadline (time.monotonic()) bounds the run-record fetch: its timeout shrinks to what is left, min
    RUN_FETCH_MIN_S, so wait_task's reply lands inside the client's request timeout. A remote task's one ssh call is
    bounded the same way, and never ABOVE exec_hosts.SSH_RPC_TIMEOUT_S: a far deadline (--watch --timeout 7000) must
    not let one hung ssh hold the caller for hours (#87 review, P1)."""
    red = red or Redactor((token,) if token else ())
    d = task_path(task_id)
    stub = _stub(d)
    if stub:
        out = _remote(stub["host"], "status", {"task_id": task_id},
                      timeout_s=max(RUN_FETCH_MIN_S, min(exec_hosts.SSH_RPC_TIMEOUT_S, deadline - time.monotonic()))
                      if deadline else None)
        if not isinstance(out, dict):               # _remote lets a list through: not a status record either
            raise TaskError(f"execution host {stub['host']} answered status with {type(out).__name__}, not a record")
        out["host"] = stub["host"]
        return out
    state, meta, res = _state(d)
    withheld = bool(res and res.get("logs_unredacted")) and not _release_recovered_logs(d, meta, res, red)
    started = meta.get("started_at") or os.path.getmtime(d)
    ended = (res or {}).get("ended_at")
    out = {"task_id": task_id, "state": state, "elapsed_s": round((ended or time.time()) - started, 1),
           "cwd": meta.get("cwd"), "orchestrator": meta.get("orchestrator"),
           "permission_mode": meta.get("permission_mode"), "dir": d,
           "stdout_log": os.path.join(d, "stdout.log"), "stderr_log": os.path.join(d, "stderr.log"),
           "host": meta.get("host"), "launch": meta.get("launch")}
    if meta.get("lane"):
        out.update(lane=meta["lane"], model=meta.get("model"))
    ws = _workspace_record(d, task_id)
    if isinstance(ws, dict):
        out["workspace"] = {k: ws.get(k) for k in ("kind", "state", "path", "repo", "branch", "reason", "error") if ws.get(k) is not None}
    elif meta.get("workspace"):
        out["workspace"] = {**meta["workspace"], "state": "preparing" if state == "running" else "not created"}
    if state == "running" and os.path.exists(os.path.join(d, QUEUED_MARKER)):
        out["phase"] = "queued: waiting for the host's Chrome (one browser task at a time)"
    if state == "running":
        out["last_output_at"] = _last_output_at(d)    # progress signal for --watch (None until the child has written)
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
        warnings = ([meta["launch_warning"]] if meta.get("launch_warning") else []) + \
            _activity(out, o, res.get("activity"), meta)
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
        elif state == "failed" and result_error(o, res.get("activity")) is not None:
            # a record from a runner that did not surface it (#97); redacted below
            out["error"] = result_error(o, res.get("activity"))
        # The router's own record of what this child was routed to (its requests carried x-cortados-run-id: task_id).
        # A login-mode (chrome) task never went through the provider: there is no run record to fetch.
        out["router_run"] = ({"note": "login mode: not routed, no provider run record"} if meta.get("lane")
                             else fetch_run(provider_url, token, task_id, _fetch_timeout(deadline)))
        ex = res.get("export")
        if (ex is None or (isinstance(ex, dict) and ex.get("error") == EXPORT_PENDING)) and not res.get("logs_unredacted"):
            # Its run export (#91) is due here when the runner could not make it: a recovered task whose logs were just
            # released, or a runner that died between result.json and its export (logs already redacted). Under the
            # task lock: while the runner holds it, the runner is still exporting and this returns None.
            ex = _export_locked(d, meta, red, out["router_run"], res) or ex
        out["export"] = ex if isinstance(ex, dict) else {"pending": "the runner records its export right after the result"}
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
        if act.get("browser_disconnected"):
            out["browser_disconnected"] = act["browser_disconnected"]   # the state is failed; this says which calls
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
    stub = _stub(d)
    if stub:
        # The host waits (one ssh session held open), shortened by the round trip so the reply lands inside our cap;
        # its own cap is given explicitly so a raised CORTADOS_WAIT_MAX_S here is honoured there.
        out = _remote(stub["host"], "wait", {"task_id": task_id, "timeout_s": max(0.0, t - SSH_OVERHEAD_S), "max_s": cap},
                      timeout_s=t + POST_WAIT_RESERVE_S)
        out["host"] = stub["host"]
        if out.get("state") == "running" and "wait_ended" not in out:
            out["wait_ended"] = f"waited {t:g} s; the task is still running: call wait_task again to keep waiting"
        return out
    while time.monotonic() < end and _state(d)[0] == "running":     # deadline first: no state read past it
        time.sleep(max(0.0, min(poll_s, end - time.monotonic())))
    # The fetch gets what the reserve leaves after a possible recovery (RECOVERY_GRACE_S): at most ~5 s past the loop,
    # or RUN_FETCH_MIN_S after a recovery that used its grace. Worst case ~t + 6 s + a ps call, under t + reserve.
    out = status(task_id, red, provider_url, token, deadline=end + POST_WAIT_RESERVE_S - RECOVERY_GRACE_S)
    if out.get("state") == "running":
        out["wait_ended"] = (f"waited {t:g} s; the task is still running: call wait_task again to keep waiting "
                             "(or task_status to poll)")
    return out


def _last_output_at(d: str):
    """Epoch time of the child's latest output: the newer mtime of stdout.log / stderr.log (the runner appends claude's
    stream there as it works), or None before anything was written. A progress signal for --watch; never content."""
    best = None
    for name in ("stdout.log", "stderr.log"):
        try:
            t = os.path.getmtime(os.path.join(d, name))
        except OSError:
            continue
        best = t if best is None else max(best, t)
    return round(best, 3) if best is not None else None


def watch_command(task_id: str) -> str:
    """The shell command the orchestrator runs IN THE BACKGROUND on this machine to be woken when the task ends: this
    interpreter and this checkout's module, both absolute, in --watch mode. The two settings that decide where tasks
    and hosts are looked up ride along when they are set (paths, never secrets), so the command sees the same tasks
    the server does. Returned by start_task as `watch_command`."""
    parts = []
    for var in ("CORTADOS_TASKS_DIR", "CORTADOS_EXEC_HOSTS_FILE"):
        v = os.environ.get(var)
        if v:
            parts.append(f"{var}={shlex.quote(v)}")
    if parts:
        parts.insert(0, "env")
    parts += [shlex.quote(sys.executable), "-B", shlex.quote(str(Path(__file__).resolve())), "--watch", task_id]
    return " ".join(parts)


def _watch_row(task_id: str, st: dict, watched_s: float) -> dict:
    """One --watch line: what ended and how, as metadata. Never the result text, the stderr tail or the prompt (the
    orchestrator fetches those with task_status); warnings as a count, for the same reason."""
    row = {"task_id": task_id, "state": st.get("state"), "host": st.get("host"), "elapsed_s": st.get("elapsed_s"),
           "watched_s": round(watched_s, 1)}
    for k in ("exit_code", "timed_out", "error", "output_error", "provider_refusal", "lane", "model"):
        if st.get(k) is not None:
            row[k] = st[k]
    ws = st.get("workspace")
    if isinstance(ws, dict):
        row["workspace"] = {k: ws[k] for k in ("kind", "state", "path", "branch", "reason", "error") if ws.get(k) is not None}
    if st.get("warnings"):
        row["warnings"] = len(st["warnings"])
    return row


def watch(task_ids, timeout_s=None, interval_s=WATCH_INTERVAL_S, stall_s=WATCH_STALL_S, out=None, red=None,
          provider_url=None, token=None, sleep=time.sleep, clock=time.monotonic,
          queued_stall_s=WATCH_QUEUED_STALL_S) -> int:
    """Follow the named tasks until each is terminal or stalled, writing one compact JSON line per task to `out` as it
    ends. Every task is read through status(), exactly as task_status is: a remote task is asked on its host over
    ssh, never guessed at here. Exit status: 0 every task ended `succeeded` (the orchestrator still reads each line's
    `warnings` count and `workspace` state, or task_status, before trusting the result); 1 any failed, cancelled or
    stalled; 2 the watch itself could not run (an unknown id, a task whose status never became readable in
    WATCH_CONNECT_ATTEMPTS tries, or an unexpected error); 3 timeout_s passed with tasks still pending (those are
    written with "watch": "timeout"). 2 outranks 3 outranks 1, whichever way the watch ends: an orchestrator reading
    the code can read every line too. Ids must be known before the first poll: one bad id and nothing is watched.
    Stall rules: WATCH_STALL_S and WATCH_QUEUED_STALL_S above. A status() that raises anything but a TaskError (a task
    dir removed between two reads, a permission), or a RecordError (a corrupt meta.json: a TaskError by shape, a local
    read by nature), counts as an unreadable poll for that task, named by exception type only; so does a reply that is
    not a record (a host answering with a list). A task that was
    readable and then was not for the stall window is `stalled` (exit 1) when any of those polls was the host's or the
    id's answer (a TaskError: the task may still be running there), but `unwatchable` (exit 2) when every one of them
    was a local read error: this side cannot read the record, which says nothing about the task. The deadline is
    honoured before every poll, and a remote poll's ssh timeout shrinks to the time left (never above one ssh RPC,
    exec_hosts.SSH_RPC_TIMEOUT_S), so --timeout is overshot by at most one short poll, not by a full pass of ssh
    timeouts."""
    out = sys.stdout if out is None else out
    ids = list(dict.fromkeys(task_ids))             # duplicates watched once; order kept
    if not ids:
        raise TaskError("--watch needs at least one task_id")
    for v, name in ((interval_s, "interval"), (stall_s, "stall"), (queued_stall_s, "queued-stall")) + \
            (((timeout_s, "timeout"),) if timeout_s is not None else ()):
        try:
            ok = float(v) > 0 and float(v) == float(v) and float(v) != float("inf")
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise TaskError(f"--{name} must be a positive number of seconds")
    for tid in ids:
        task_path(tid)                              # unknown or malformed: refused before anything is watched
    if red is None:
        tokens = _host_tokens()
        red = Redactor(tokens)                      # a short token is refused here (nothing unredactable is printed)
        token = tokens[0] if tokens else ""
    if provider_url is None:
        try:
            provider_url = session.resolve_provider_url()
        except session.ProviderURLError:
            provider_url = ""                       # the run record is then reported as unavailable, nothing more

    def emit(row):
        out.write(json.dumps(red.tree(row), separators=(",", ":"), sort_keys=True) + "\n")
        out.flush()

    start = clock()
    deadline = None if timeout_s is None else start + float(timeout_s)

    def past_deadline(now):
        return deadline is not None and now >= deadline

    # per task: read = status has been readable at least once; fails = consecutive unreadable polls; local = every one
    # of those was a local read error (no TaskError among them); mark = the last progress fingerprint; since = when it
    # last changed (watcher's clock: the hosts' clocks are not compared)
    pending = {tid: {"read": False, "fails": 0, "local": True, "mark": None, "since": start, "state": None} for tid in ids}
    any_bad = cannot = False
    while pending:
        for tid in list(pending):
            p = pending[tid]
            now = clock()
            if past_deadline(now):
                break                               # checked before each poll, not once per pass (P2-2, #87 review)
            try:
                # status()'s deadline is on time.monotonic(): a remote poll's ssh timeout shrinks to what is left
                st = status(tid, red, provider_url, token,
                            deadline=None if deadline is None else time.monotonic() + (deadline - now))
                if not isinstance(st, dict):        # status() refuses a list itself; a patched status() may not
                    raise TaskError(f"status answered with {type(st).__name__}, not a record")
            except Exception as exc:
                # A TaskError carries the host's or the id's message (names, never credentials). Anything else is a
                # reading problem on this side (a dir that vanished mid-read, a permission): type only. A RecordError
                # (corrupt JSON in this machine's copy of the record) is a TaskError by shape but a local read too.
                why = str(exc) if isinstance(exc, TaskError) else type(exc).__name__
                p["fails"] += 1
                p["local"] = p["local"] and (isinstance(exc, RecordError) or not isinstance(exc, TaskError))
                if not p["read"] and p["fails"] >= WATCH_CONNECT_ATTEMPTS:
                    emit({"task_id": tid, "state": "unwatchable", "watched_s": round(now - start, 1),
                          "error": f"status could not be read in {p['fails']} attempts: {why}"})
                    pending.pop(tid)
                    cannot = True
                elif p["read"] and now - p["since"] >= float(stall_s):
                    row = {"task_id": tid, "last_state": p["state"], "watched_s": round(now - start, 1),
                           "stalled_after_s": round(now - p["since"], 1)}
                    if p["local"]:
                        # every failed poll since the last good read raised here, not on the host: the watcher cannot
                        # read the record (exit 2), which says nothing about the task itself
                        row["state"] = "unwatchable"
                        row["error"] = (f"status could not be read on this machine for {round(now - p['since'])} s "
                                        f"(local read error, last: {why}); not the host's answer, the task may still "
                                        "be running there (task_status / cancel_task to decide)")
                        cannot = True
                    else:
                        row["state"] = "stalled"
                        row["error"] = (f"status unreadable for {round(now - p['since'])} s (last: {why}); the task "
                                        "may still be running on its host")
                        any_bad = True
                    emit(row)
                    pending.pop(tid)
                continue
            p["read"], p["fails"], p["local"], p["state"] = True, 0, True, st.get("state")
            if p["state"] in TERMINAL_STATES:
                emit(_watch_row(tid, st, now - start))
                pending.pop(tid)
                any_bad = any_bad or p["state"] != "succeeded"
                continue
            phase = st.get("phase") or None
            mark = (p["state"], phase, (st.get("workspace") or {}).get("state") if isinstance(st.get("workspace"), dict) else None,
                    st.get("last_output_at"))
            if mark != p["mark"]:
                p["mark"], p["since"] = mark, now
                continue
            window = float(queued_stall_s if phase else stall_s)    # a queued task waits; its window is the long one
            if now - p["since"] >= window:
                row = {"task_id": tid, "state": "stalled", "last_state": p["state"], "host": st.get("host"),
                       "watched_s": round(now - start, 1), "stalled_after_s": round(now - p["since"], 1),
                       "elapsed_s": st.get("elapsed_s")}
                if phase:
                    row["last_phase"] = phase
                    row["error"] = (f"queued ({phase}) with no change for {round(now - p['since'])} s, longer than the "
                                    "runner's queue cap plus the stall window; the host's Chrome lock may be stuck "
                                    "(task_status / cancel_task to decide)")
                else:
                    row["error"] = (f"no change in state, phase, workspace or child output for {round(now - p['since'])} s; "
                                    "the task is still running on its host (task_status / cancel_task to decide)")
                emit(row)
                pending.pop(tid)
                any_bad = True
        if not pending:
            break
        now = clock()
        if past_deadline(now):
            for tid, p in pending.items():
                emit({"task_id": tid, "state": p["state"] or "unknown", "watch": "timeout", "watched_s": round(now - start, 1),
                      "error": f"--timeout {float(timeout_s):g} s passed with the task not yet ended"})
            return 2 if cannot else 3               # an unwatchable task outranks the timeout, as it does any ending
        sleep(max(0.0, min(float(interval_s), deadline - now) if deadline is not None else float(interval_s)))
    return 2 if cannot else 1 if any_bad else 0


def watch_main(argv: list) -> int:
    """`--watch <task_id> [<task_id> ...] [--timeout S] [--interval S] [--stall S]`: see watch()."""
    usage = ("usage: python3 cortados_router/child_tasks.py --watch <task_id> [<task_id> ...] [--timeout S] "
             f"[--interval S (default {WATCH_INTERVAL_S})] [--stall S (default {WATCH_STALL_S})]")
    ids, opts = [], {"timeout": None, "interval": WATCH_INTERVAL_S, "stall": WATCH_STALL_S}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--timeout", "--interval", "--stall"):
            if i + 1 >= len(argv):
                print(usage, file=sys.stderr)
                return 2
            try:
                opts[a[2:]] = float(argv[i + 1])
            except ValueError:
                print(f"--watch: {a} needs a number of seconds, got {argv[i + 1]!r}", file=sys.stderr)
                return 2
            i += 2
            continue
        if a.startswith("-"):
            print(usage, file=sys.stderr)
            return 2
        ids.append(a)
        i += 1
    if not ids:
        print(usage, file=sys.stderr)
        return 2
    try:
        return watch(ids, opts["timeout"], opts["interval"], opts["stall"])
    except TaskError as exc:
        print(f"--watch: {exc}", file=sys.stderr)     # task_path / option errors: ids and settings, never credentials
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Anything watch() did not handle per task is "the watch could not run" (2), never a traceback that exits 1
        # (= "a task failed"); the type names the problem, the message could carry paths or content.
        print(f"--watch: could not run: {type(exc).__name__}", file=sys.stderr)
        return 2


def peek_local(task_id) -> dict | None:
    """For GET /v1/cortados/live: a running task's phase and workspace state as this machine's task dir shows them, or
    None (an invalid id, no dir here, or a remote stub: the task lives on another host and is never asked over ssh).
    Read-only and cheap (a few stats and one small JSON read), unlike _state(), which reaps runners, recovers dead
    ones and unloads launchd jobs: a 5 s dashboard poll must change nothing. Never creates the tasks root."""
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        return None
    d = os.path.join(tasks_dir(), task_id)
    if not os.path.isdir(d) or os.path.exists(os.path.join(d, REMOTE_STUB)):
        return None
    out = {"phase": "queued" if os.path.exists(os.path.join(d, QUEUED_MARKER)) else None,
           "ended": os.path.exists(os.path.join(d, "result.json")), "workspace": None}
    try:
        ws = _workspace_record(d, task_id)
    except TaskError:                               # an unreadable record: no state, said as unknown by the view
        ws = None
    if isinstance(ws, dict) and isinstance(ws.get("state"), str):
        out["workspace"] = ws["state"]
    return out


REMOTE_STUBS_SCAN = 200            # newest task dirs looked at per call: a day of tasks at the owner's pace, a few stats each


def remote_stubs() -> list:
    """For GET /v1/cortados/live: the remote stubs among the newest REMOTE_STUBS_SCAN task dirs on this machine, as
    [{task_id, host, created_at}] — tasks started FROM here onto another execution host. Metadata only, read-only (no
    ssh, never creates the tasks root; a missing root is no stubs). A stub that cannot be read is skipped: it is a
    pointer, and the view says the section's scope rather than guessing its task's state."""
    root = tasks_dir()
    try:
        names = sorted((x for x in os.listdir(root) if TASK_ID_RE.fullmatch(x)), reverse=True)[:REMOTE_STUBS_SCAN]
    except FileNotFoundError:
        return []
    out = []
    for tid in names:
        try:
            rec = _stub(os.path.join(root, tid))
        except TaskError:
            continue
        if rec is not None:
            out.append({"task_id": tid, "host": rec.get("host"), "created_at": rec.get("created_at")})
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
    rows, by_host = [], {}
    for tid in ids:
        try:
            state, meta, res = _state(os.path.join(root, tid))
        except RecordError as exc:                  # one corrupt record must not hide every other task (#94)
            rows.append({"task_id": tid, "state": "unreadable", "error": _note_unreadable(exc)})
            continue
        if state == "remote":
            by_host.setdefault(meta["host"], []).append(tid)
            rows.append({"task_id": tid, "host": meta["host"], "state": "unknown"})   # filled in from the host below
            continue
        rows.append({"task_id": tid, "state": state, "cwd": meta.get("cwd"), "orchestrator": meta.get("orchestrator"),
                     "task_preview": meta.get("task_preview"), "host": meta.get("host"),
                     **({"workspace": meta["workspace"]} if meta.get("workspace") else {}),
                     **clean_origin_meta(meta.get("origin"))})
    for host, tids in by_host.items():         # one round trip per host, never one per task
        try:
            remote = _remote(host, "list", {"limit": len(tids), "ids": tids})
            found = {r.get("task_id"): r for r in remote if isinstance(r, dict)} if isinstance(remote, list) else {}
            err = None
        except TaskError as exc:
            found, err = {}, str(exc)
        for row in rows:
            if row.get("host") == host and row["state"] == "unknown":
                if row["task_id"] in found:
                    row.update(found[row["task_id"]], host=host)
                else:
                    row["error"] = err or f"not listed by execution host '{host}' (pruned there?)"
    return rows


def cancel(task_id) -> dict:
    d = task_path(task_id)
    state, meta, _ = _state(d)
    if state == "remote":
        out = _remote(meta["host"], "cancel", {"task_id": task_id})
        out["host"] = meta["host"]
        return out
    if state != "running":
        return {"task_id": task_id, "state": state, "cancelled": False, "reason": f"task is already {state}"}
    pid = meta.get("runner_pid")
    # Signal ONLY while the runner's lock is held AND the pid's command line is this task's runner: a recorded pid can
    # belong to an unrelated session leader after a reboot or a SIGKILL.
    if not _lock_held(d) or not _is_our_runner(pid, d):
        if meta.get("launch") == "launchd":
            # The desktop-session runner has not taken its lock yet (launchd has not started it, or it is between exec
            # and flock): the marker makes it exit before any spawn, and the job is unloaded so it does not start.
            open(os.path.join(d, "cancelled"), "w").close()
            note = _launchd_bootout(d)
            err = log_event("cancelled", **_event_fields(meta))
            out = {"task_id": task_id, "state": "cancelling", "cancelled": True, "note": f"runner not started yet; {note}"}
            if err:
                out["event_log_error"] = err
            return out
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


def _workspace_record(d: str, task_id: str):
    """The task's workspace record: workspace.json, except when it says the settle was deferred (`settle_deferred`,
    still active) and the host's index.jsonl says the cleanup has settled it since (#93 round 3): then the index
    record, which names the real state and path. Read-only; the runner's own retry rewrites workspace.json."""
    ws = _read_json(os.path.join(d, WORKSPACE_FILE))
    if not (isinstance(ws, dict) and ws.get("state") == "active" and ws.get("settle_deferred")):
        return ws
    root = (_read_json(os.path.join(d, "cmd.json")) or {}).get("workspace_root")
    if not isinstance(root, str):
        return ws
    try:
        done = workspaces.read_index(root).get(task_id)
    except (OSError, ValueError):
        return ws
    if isinstance(done, dict) and done.get("state") not in (None, "active"):
        return done
    return ws


def _settle_workspace(d: str):
    """Move the task's workspace out of active/ (workspaces.settle) once the child is over, and record it in the
    host's index.jsonl. Idempotent: a folder already settled (or a task with none) is left alone. Returns the
    workspace record, or None when the task has no managed workspace. Never raises: the task's result stands."""
    ws = _read_json(os.path.join(d, WORKSPACE_FILE))
    spec = _read_json(os.path.join(d, "cmd.json")) or {}
    root = spec.get("workspace_root")
    if not isinstance(ws, dict) or not isinstance(root, str):
        return None
    if ws.get("state") != "active":
        return ws
    try:
        ws = workspaces.settle(ws, root)
        _write_json(os.path.join(d, WORKSPACE_FILE), ws)
        workspaces.update_index(root, {**ws, "settled_from": "runner" if ws.get("state") != "active" else None})
    except (OSError, workspaces.WorkspaceError) as exc:
        ws = {**ws, "error": f"settle failed: {type(exc).__name__}: {exc}"}
        try:
            _write_json(os.path.join(d, WORKSPACE_FILE), ws)
        except OSError:
            pass
    return ws


# --- task run export (#91: a finalized, redacted, append-only record per run, for Grounds to capture) ---------------
#
# One JSONL file per execution host: $CORTADOS_HOME/exports/task-runs.jsonl (CORTADOS_TASK_EXPORT names another file).
# One line per finished task (any terminal state), written ONCE, only after the run's logs were redacted and its
# result.json is final: by the runner itself, else (a runner that died) by the first status call that can release the
# logs, else by prune just before the task dir would be deleted (then with the logs withheld). This code only ever
# appends: one os.write of the whole line, under an flock on <file>.lock, so concurrent runners never interleave. A
# sidecar <file>.ids (task id + file size after the line, under the same lock) makes a retry idempotent. Every text
# in the record went through the same Redactor that produced result.json, redacted BEFORE it was capped; the raw
# stdout.log content is never written. A task dir whose export has not succeeded is never pruned (it is reported).

EXPORT_SCHEMA = "cortados.task-run/1"
EXPORT_ENV = "CORTADOS_TASK_EXPORT"         # a file path; unset: paths.state_dir()/EXPORT_FILE
EXPORT_FILE = os.path.join("exports", "task-runs.jsonl")
PARENT_ENV = "CORTADOS_PARENT_TASK_ID"      # set by a launching session that is itself a task (start_task arg wins)
EXPORT_TEXT_CHARS = 32_000                  # per text field (prompt, a tool input string, a tool result, the result)
EXPORT_MAX_EVENTS = 2_000                   # per record; later events are counted in events_dropped, not written
EXPORT_LOCK_WAIT_S = 30                     # an export lock held longer than this is a stuck writer: fail, retry later
PRUNE_EXPORT_RETRIES = 10                   # export retries per prune: prune runs inside start_task, which must answer
PRUNE_EXPORT_BUDGET_S = 5.0                 # all of one prune's export retries together, lock waits included
EXPORT_PENDING = "LogsUnredacted"           # export error of a recovered task whose logs are not released yet
EXPORT_LATE_ROUTER_RUN = {"note": "not fetched: exported after the runner (the provider's run record is in memory "
                                  "only); see the router logs"}
EXPORT_LOGS_WITHHELD = "logs withheld: they could not be redacted with the launch credentials"
EXPORT_PROMPT_WITHHELD = ("prompt withheld: the runner did not redact it, and the current credentials do not match the "
                          "launch ones")
EXPORT_REDACT_FAILED = "logs withheld: the runner could not redact them"
EXPORT_FIELDS_WITHHELD = ("caller text withheld: it was not redacted with the launch credentials when the task started, "
                          "and the current credentials do not match the launch ones")
META_TEXT_MARK = "caller_text_redacted"      # meta.json: every caller-supplied text in it was checked at start (#91)
# Record fields that carry caller-supplied text (or text derived from it). A late export includes them only when the
# task's meta is marked (start_task redacted or refused every credential in them with the launch credentials) or the
# current credentials provably cover the launch ones; otherwise they are null and named in withheld_fields.
EXPORT_CALLER_TEXT = ("title", "client", "cwd", "orchestrator", "model", "error", "parent_note")
EXPORT_CALLER_WORKSPACE = ("repo", "branch", "reason")

# Best-effort scrub of well-known secret shapes, applied to export text only, AFTER the known-value redaction and
# BEFORE the cap. Not a guarantee: a secret of any other shape passes. Each match becomes [scrubbed:<type>], counted.
# Linear time on any input (#91 review round 2, P2: a lookahead over the whole run made `"sk-" * N` quadratic). Every
# pattern starts at a fixed prefix behind a word boundary and either fails within a bounded number of characters
# (bounded quantifiers only, no nested or overlapping repetition) or ends with a greedy run of ONE character class
# with nothing after it, so a match that reaches its run always succeeds and swallows the whole run: no start inside
# that run is ever tried again. A shape that needs more than its run (an OpenAI key's digit, a JWT's dotted parts)
# is checked by the replacement callback on the matched run, in one linear pass (_Cap._sub).
SCRUB_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----.*?"
                               r"(?:-----END [A-Z0-9 ]{0,64}PRIVATE KEY-----|\Z)", re.S)),
    ("authorization", re.compile(r"(?i)\b(?:proxy-)?authorization[\"']?\s{0,16}[:=]\s{0,16}[\"']?([^\r\n\"',;]+)")),
    ("bearer", re.compile(r"(?i)\bbearer\s{1,16}([A-Za-z0-9._~+/-]{8,}=*)")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_.-]{4,}")),                  # the whole dotted run; _Cap._jwt finds the JWTs
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),            # with a digit: checked on the match
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)
SCRUB_SLACK_CHARS = 4_096                    # scrubbed past the cap, so a secret straddling the cut is still caught
_TOKEN_CHARS = string.ascii_letters + string.digits + "._~+/=-"   # a secret's characters: stripped at a scrub cut
SCRUB_KEYS = ("authorization", "proxy-authorization", "x-api-key")   # a dict value under such a key is scrubbed whole
_ALREADY_MASKED = re.compile(r"\s*(?:[A-Za-z]+\s+)?(?:\*\*\*|\[scrubbed:[a-z_]+\])\s*")


class _ExportLeak(Exception):
    """The encoded record still matched a credential: never written."""


def export_path():
    """(path, kind): the export file and whether it is the default or CORTADOS_TASK_EXPORT's."""
    raw = (os.environ.get(EXPORT_ENV) or "").strip()
    if raw:
        return os.path.abspath(os.path.expanduser(raw)), "env"
    return os.path.join(str(paths.state_dir()), EXPORT_FILE), "default"


def resolve_parent(arg):
    """(parent_task_id or None, note or None) for a new task: start_task's parent_task_id (validated: a bad one is
    refused), else CORTADOS_PARENT_TASK_ID when the launching session sets it (a malformed one is ignored, with a
    note: it must not block every start_task)."""
    if arg is not None:
        if not isinstance(arg, str) or not TASK_ID_RE.fullmatch(arg):
            raise TaskError("parent_task_id must be a task id (t<14 digits>-<8 hex>)")
        return arg, None
    env = (os.environ.get(PARENT_ENV) or "").strip()
    if not env:
        return None, None
    if TASK_ID_RE.fullmatch(env):
        return env, None
    return None, f"{PARENT_ENV} is set but is not a task id; the parent is recorded as missing"


def _start_redactor(red, env=None) -> "Redactor":
    """Every credential a new task can know of at start: `red`'s (the caller's env token and token file), this
    process's (launch env, host token env and file) and, once it exists, the child's launch env (LAUNCH_TOKEN_KEYS).
    Caller text is redacted, or refused, with this before meta.json holds it (#91 review round 2, P1)."""
    toks = set(red.raw if red is not None else ()) | set(launch_tokens(os.environ)) | set(_host_tokens())
    if env is not None:
        toks |= set(launch_tokens(env))
    return Redactor([t for t in toks if len(t) >= MIN_TOKEN_LEN])


def _refuse_credentials(red: "Redactor", fields: dict) -> None:
    """Caller strings the task runs with (a path, a branch, a model, tool patterns, a session tag) cannot be redacted
    without breaking the task, and would be stored in meta.json and exported: one that holds a credential is refused.
    The message names the field only, never its value."""
    for name, v in fields.items():
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, str) and red.text(x) != x:
                raise TaskError(f"{name} contains a provider credential; the task is refused (it would be stored in "
                                "the task record and its run export)")


class _Cap:
    """For already-redacted export text: scrubs well-known secret shapes (SCRUB_PATTERNS, counted by type in
    `scrubbed`), then caps it at EXPORT_TEXT_CHARS with a marker, counting every cut. Callers pass text only after the
    known-value Redactor ran on it, so a known credential is masked as such, never reshaped by the scrub first."""

    def __init__(self, limit=None):
        self.limit = EXPORT_TEXT_CHARS if limit is None else limit
        self.truncated = 0
        self.scrubbed = {}

    def _mark(self, kind: str) -> str:
        self.scrubbed[kind] = self.scrubbed.get(kind, 0) + 1
        return f"[scrubbed:{kind}]"

    def _jwt(self, run: str) -> str:
        """The JWTs (eyJ<4+>.eyJ<4+>.<sig>, the first part starting at a word boundary) in one dotted run of token
        characters, each replaced by its marker. One pass over the run's parts: linear in its length."""
        parts = run.split(".")
        out, i = [], 0
        while i < len(parts):
            p = parts[i]
            j = -1
            if i + 2 < len(parts) and parts[i + 1].startswith("eyJ") and len(parts[i + 1]) >= 7:
                k = p.find("eyJ")
                while k != -1 and j == -1:
                    if (k == 0 or p[k - 1] == "-") and len(p) - k >= 7:
                        j = k
                    else:
                        k = p.find("eyJ", k + 1)
            if j == -1:
                out.append(p)
                i += 1
                continue
            out.append(p[:j] + self._mark("jwt"))
            i += 3                              # header, payload and signature are the one marker
        return ".".join(out)

    def _sub(self, kind: str, m) -> str:
        if kind == "jwt":
            return self._jwt(m.group(0))
        if kind == "openai_key" and not any(c.isdigit() for c in m.group(0)[3:]):
            return m.group(0)                   # sk-<run> with no digit: prose such as sk-learn-style, left alone
        if m.lastindex:                         # the secret is group 1; the label before it stays readable
            if _ALREADY_MASKED.fullmatch(m.group(1)):
                return m.group(0)
            return m.group(0)[:m.start(1) - m.start(0)] + self._mark(kind)
        return self._mark(kind)

    def scrub_one(self, kind: str, rx, s: str) -> str:
        return rx.sub(lambda m: self._sub(kind, m), s)

    def scrub(self, s: str) -> str:
        for kind, rx in SCRUB_PATTERNS:
            s = self.scrub_one(kind, rx, s)
        return s

    def _keyed(self, key, v: str):
        """A value under an Authorization-like key is a credential whatever its shape: scrubbed whole."""
        if isinstance(key, str) and key.lower() in SCRUB_KEYS and v and not _ALREADY_MASKED.fullmatch(v):
            return self._mark("authorization")
        return None

    def scrub_tree(self, v, key=None):
        if isinstance(v, str):
            keyed = self._keyed(key, v)
            return keyed if keyed is not None else self.scrub(v)
        if isinstance(v, list):
            return [self.scrub_tree(x) for x in v]
        if isinstance(v, dict):
            return {self.scrub(str(k)): self.scrub_tree(x, str(k)) for k, x in v.items()}
        return v

    def cut(self, s: str, total=None) -> str:
        """s capped at the limit; `total` is the length of the text s was taken from (text() scrubs only a prefix)."""
        total = max(len(s), total or 0)
        if total <= self.limit:
            return s
        self.truncated += 1
        return s[:self.limit] + f"\n[truncated by cortados: {total - self.limit} of {total} characters cut]"

    def text(self, s: str) -> str:
        """Scrubbed, then capped. Only the first limit + SCRUB_SLACK_CHARS characters are scrubbed: the rest is cut
        anyway, and the slack still catches a secret that straddles the cut. When the prefix was cut and the scrub
        shrank it, its trailing run of token characters goes too: it may be a secret's start, too short for its
        pattern, that the shrink moved in front of the cap (#91 review round 3, P2)."""
        n, end = len(s), self.limit + SCRUB_SLACK_CHARS
        head = self.scrub(s[:end])
        if n > end and len(head) < end:
            head = head.rstrip(_TOKEN_CHARS)
        return self.cut(head, n)

    def tree(self, v, key=None):
        if isinstance(v, str):
            keyed = self._keyed(key, v)
            return keyed if keyed is not None else self.text(v)
        if isinstance(v, list):
            return [self.tree(x) for x in v]
        if isinstance(v, dict):
            return {self.text(str(k)): self.tree(x, str(k)) for k, x in v.items()}
        return v


def _home_relative(p):
    """An absolute path under $HOME as ~/...: the export carries no home directory beyond that."""
    if not isinstance(p, str):
        return p
    home = os.path.expanduser("~")
    if home and home != "/" and (p == home or p.startswith(home.rstrip("/") + "/")):
        return "~" + p[len(home.rstrip("/")):]
    return p


def _export_events(path: str, red: "Redactor", cap):
    """(events, counts, extra_withheld, dropped) from a REDACTED stdout.log: assistant text, tool_use (name + input),
    tool_result (content as text, is_error) and the result record, in stream order. Each record is redacted again
    (redact_record, the runner's own rules) and only then capped. A line that does not parse, and the runner's
    WITHHELD_EVENT, become a `withheld` event; a record the second pass withholds is counted in extra_withheld.
    cap None: counts only, for logs that are withheld. Their content is then neither redacted, scrubbed nor kept (the
    events come back empty), so withheld material costs one JSON parse per line and nothing else."""
    events, counts = [], {}
    extra = dropped = 0
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            whole = f.read()
    except FileNotFoundError:
        return events, counts, 0, 0
    try:
        legacy = json.loads(whole) if whole.strip() else None
    except (ValueError, RecursionError):
        legacy = None
    lines = [None] if isinstance(legacy, dict) else whole.split("\n")

    kept = 0

    def add(ev):
        nonlocal dropped, kept
        if kept >= EXPORT_MAX_EVENTS:
            dropped += 1
            return
        kept += 1
        if cap is not None:
            events.append(ev)
        counts[ev["type"]] = counts.get(ev["type"], 0) + 1

    if cap is None:
        def text_(_s):
            return None

        def tree(_v, key=None):
            return None
    else:
        text_, tree = cap.text, cap.tree

    for line in lines:
        if line is None:
            record = legacy
        else:
            body = line.rstrip("\r")
            if not body.strip():
                continue
            try:
                record = json.loads(body)
            except (ValueError, RecursionError):
                extra += 1                       # never redacted structurally (no credential to redact with): not shown
                add({"type": "withheld"})
                continue
        if isinstance(record, dict) and record.get("type") == "cortados_withheld":
            add({"type": "withheld"})            # the runner withheld it (already counted in redact_unparsed_lines)
            continue
        if cap is None:
            cleaned, n = record, 0               # counted, never shown: nothing to redact
        else:
            try:
                cleaned, n = redact_record(red, record)
            except (ValueError, RecursionError):
                cleaned, n = redact.WITHHELD, 1
        extra += n
        if not isinstance(cleaned, dict):
            if cleaned == redact.WITHHELD:
                add({"type": "withheld"})
            continue
        kind = cleaned.get("type")
        if kind == "result":
            t = cleaned.get("result")
            add({"type": "result", "subtype": cleaned.get("subtype"), "is_error": cleaned.get("is_error"),
                 "num_turns": cleaned.get("num_turns"), "text": text_(t) if isinstance(t, str) else None})
            continue
        message = cleaned.get("message")
        content = message.get("content") if isinstance(message, dict) and kind in ("assistant", "user") else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if kind == "assistant" and b.get("type") == "text" and isinstance(b.get("text"), str):
                add({"type": "text", "text": text_(b["text"])})
            elif kind == "assistant" and b.get("type") == "tool_use":
                add({"type": "tool_use", "id": tree(b.get("id")), "name": text_(str(b.get("name") or "?")),
                     "input": tree(b.get("input"))})
            elif kind == "user" and b.get("type") == "tool_result":
                c = b.get("content")
                ev = {"type": "tool_result", "tool_use_id": tree(b.get("tool_use_id")), "is_error": b.get("is_error") is True,
                      "content": text_(_result_text(b)) if cap is not None else None}
                if isinstance(c, list):
                    other = sum(1 for p in c if not (isinstance(p, dict) and isinstance(p.get("text"), str)))
                    if other:
                        ev["omitted_parts"] = other  # images and other non-text parts are not exported
                add(ev)
    return events, counts, extra, dropped


def build_export_record(d: str, meta: dict, res: dict, red: "Redactor", router_run) -> dict:
    """The export record of a finished task (schema EXPORT_SCHEMA). Prompt and logs are read only when they are
    marked redacted with the launch credentials (`prompt_redacted` / `logs_redacted`, by the runner or a recovery
    that proved the launch fingerprints), or when `red` itself provably covers the launch credentials (then it
    redacts them here); otherwise they are withheld (`withheld_reason`; event counts and the run's metadata stay).
    `red` alone is never trusted with unredacted material: after a rotation it no longer knows the old token.
    The caller-supplied text in meta (title, client, cwd, workspace names, ...: EXPORT_CALLER_TEXT) follows the same
    rule: included when meta.json is marked META_TEXT_MARK (start_task redacted it, or refused the task, with the
    launch credentials) or `red` covers the launch ones, else null and named in `withheld_fields`.
    The caller encodes it; nothing here is the raw stdout.log."""
    cap = _Cap()
    tid = os.path.basename(d)
    covered = launch_covered(meta, red)
    logs = (not res.get("redact_error") and not res.get("logs_unredacted")
            and (res.get("logs_redacted") is True or covered))
    prompt_ok = res.get("prompt_redacted") is True or covered
    # Caller text (title, client, cwd, workspace names, ...): the same rule as prompt and logs. It is only ever
    # redacted here with `red`, which after a rotation no longer knows the old token (#91 review round 2, P1).
    text_ok = covered or meta.get(META_TEXT_MARK) is True
    started, ended = meta.get("started_at"), res.get("ended_at")
    parent = meta.get("parent_task_id")
    parent = parent if isinstance(parent, str) and TASK_ID_RE.fullmatch(parent) else None
    ws = res.get("workspace") if isinstance(res.get("workspace"), dict) else None
    ws_fallback = ws is None                    # workspace.json / meta: never redacted with the launch credentials
    if ws_fallback:
        ws = _read_json(os.path.join(d, WORKSPACE_FILE))
        if not isinstance(ws, dict):
            ws = meta.get("workspace") if isinstance(meta.get("workspace"), dict) else None
    act = res.get("activity") if isinstance(res.get("activity"), dict) else {}
    # Metadata: ids, names, flags and the provider's run record; redacted as text (a title such as "[WIP] x" is not
    # JSON to decode), never capped.
    head = {
        "task_id": tid, "parent_task_id": parent, "parent": "known" if parent else "missing",
        **({"parent_note": meta["parent_note"]} if meta.get("parent_note") else {}),
        "title": meta.get("title"), "host": meta.get("host"), "client": meta.get("client"),
        "state": classify(res), "exit_code": res.get("exit_code"), "cancelled": bool(res.get("cancelled")),
        "timed_out": bool(res.get("timed_out")),
        "started_at": started, "ended_at": ended,
        "elapsed_s": round(ended - started, 1) if isinstance(started, (int, float)) and isinstance(ended, (int, float)) else None,
        "cwd": _home_relative(meta.get("cwd")),
        "workspace": {k: ws.get(k) for k in ("kind", "state", "repo", "branch", "reason") if ws.get(k) is not None} if ws else None,
        "orchestrator": meta.get("orchestrator"), "permission_mode": meta.get("permission_mode"),
        "effective_permission_mode": act.get("effective_permission_mode"),
        "chrome": bool(meta.get("chrome")), "launch": meta.get("launch"),
        **({"lane": meta.get("lane"), "model": meta.get("model")} if meta.get("lane") else {}),
        "router_run": router_run if isinstance(router_run, dict) else None,
        "tool_uses": act.get("tool_uses") or {}, "tool_errors": act.get("tool_errors") or 0,
    }
    error = res.get("error") or res.get("output_error") or res.get("redact_error")
    if isinstance(error, str):
        head["error"] = error
    withheld_fields = []
    if not text_ok:                             # dropped before any processing: nothing of it reaches the record
        for k in EXPORT_CALLER_TEXT:
            if head.get(k) is not None:
                head[k] = None
                withheld_fields.append(k)
        for k in EXPORT_CALLER_WORKSPACE:
            if isinstance(head.get("workspace"), dict) and head["workspace"].get(k) is not None:
                head["workspace"][k] = None
                withheld_fields.append("workspace." + k)
    if ws_fallback and not covered and isinstance(head.get("workspace"), dict):
        # Only `red` could redact it, and after a rotation it no longer knows the launch token (#91 review round 3, P2).
        for k in EXPORT_CALLER_WORKSPACE:
            if head["workspace"].get(k) is not None:
                head["workspace"][k] = None
                withheld_fields.append("workspace." + k)
    extra = 0
    error = head.pop("error", None)
    head = cap.scrub_tree(red.tree(head))
    if isinstance(error, str):
        text, n = _text_result(red, error)
        head["error"], extra = cap.text(text), extra + n
    else:
        head["error"] = None                    # always present: null when there is none or it is withheld
    prompt = None
    if logs and prompt_ok:
        try:
            with open(os.path.join(d, "prompt.txt"), encoding="utf-8", errors="replace") as f:
                raw_prompt = f.read()
        except FileNotFoundError:
            raw_prompt = None
        if raw_prompt is not None:
            text, n = _text_result(red, raw_prompt)
            prompt, extra = cap.text(text), extra + n
    stdout = os.path.join(d, "stdout.log")
    if logs:
        events, counts, more, dropped = _export_events(stdout, red, cap)
        extra += more
    else:
        # Counts only (event types): no content of it goes into the record, so none of it is redacted or scrubbed
        # either (withheld material must not cost the export time, #91 review round 2, P2).
        _, counts, _, dropped = _export_events(stdout, red, None)
        events = []
    reason = (EXPORT_FIELDS_WITHHELD if withheld_fields else None) if logs and prompt_ok else (
              EXPORT_REDACT_FAILED if res.get("redact_error") else
              EXPORT_LOGS_WITHHELD if not logs else EXPORT_PROMPT_WITHHELD)
    unparsed = res.get("redact_unparsed_lines") or 0
    return {"schema": EXPORT_SCHEMA, **head, "exported_at": round(time.time(), 3),
            "logs": "included" if logs else EXPORT_LOGS_WITHHELD,
            **({"withheld_reason": reason} if reason else {}),
            **({"withheld_fields": withheld_fields} if withheld_fields else {}),
            "prompt": prompt, "events": events, "event_counts": counts,
            **({"events_dropped": dropped} if dropped else {}),
            "redact_unparsed_lines": unparsed, "withheld": unparsed + extra, "truncated": cap.truncated,
            "scrubbed": dict(sorted(cap.scrubbed.items()))}


def _flock_wait(fd: int, wait_s: float) -> None:
    end = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= end:
                raise TimeoutError("export lock busy") from None
            time.sleep(0.05)


def _open_private(path: str, flags: int, label: str, tightened: list) -> int:
    """Open (creating 0600) an export file without following a symlink; it must be a regular file of this user. A
    mode with any group/other bit is tightened to 0600 through the open fd (recorded in `tightened`); if that fails,
    the file is refused."""
    fd = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError(f"export {label} is not a regular file of this user")
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o600)                # an OSError here refuses the file: never appended to while readable
            tightened.append(label)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _export_dir(parent: str, kind: str, tightened: list) -> None:
    """The default export dir is ours: a real directory of this user, exactly 0700 (tightened if looser). An override
    (CORTADOS_TASK_EXPORT) names any directory, but never one that group or others can write (they could swap the
    file or its sidecars)."""
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    st = os.lstat(parent) if kind == "default" else os.stat(parent)
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError("the export directory is not a directory")
    if kind == "default":
        if st.st_uid != os.getuid():
            raise PermissionError("the export directory is not owned by this user")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(parent, 0o700)             # makedirs' mode is masked by umask; an existing one may be looser
            if st.st_mode & 0o077:
                tightened.append("dir")
    elif st.st_mode & 0o022:
        raise PermissionError(f"the {EXPORT_ENV} directory is writable by group or others")


def _exported_ids(fd: int, ids_fd: int) -> set:
    """Task ids already in the export file (open as `fd`), from the sidecar <path>.ids (`ids_fd`); lines the sidecar
    does not cover (a writer that died between the line and its id) are read from the file's tail and added to it.
    Call under the lock."""
    ids, end = set(), 0
    with os.fdopen(os.dup(ids_fd), "rb") as f:
        f.seek(0)
        for row in f:
            parts = row.decode("ascii", "replace").split()
            if parts and TASK_ID_RE.fullmatch(parts[0]):
                ids.add(parts[0])
            if len(parts) > 1 and parts[1].isdigit():
                end = int(parts[1])             # "-" rows only move the baseline (a rescanned tail with no ids)
    size = os.fstat(fd).st_size
    if size == end:
        return ids
    start = end if end < size else 0            # smaller: the file was replaced by its reader; rescan it whole
    found = []
    with os.fdopen(os.dup(fd), "rb") as f:
        f.seek(start)
        for line in f:
            try:
                tid = json.loads(line).get("task_id")
            except (ValueError, RecursionError, AttributeError):
                continue                        # a partial first line of the tail, or not ours
            if isinstance(tid, str) and TASK_ID_RE.fullmatch(tid) and tid not in ids:
                ids.add(tid)
                found.append(tid)
    _append_ids(ids_fd, found or [None], size)
    return ids


def _append_ids(ids_fd: int, tids, end: int) -> None:
    os.write(ids_fd, "".join(f"{t or '-'} {end}\n" for t in tids).encode("ascii"))


def append_export(line: bytes, task_id: str, lock_wait_s=None):
    """Append one encoded record to the export file, once per task id. Returns (path_kind, already_there, tightened):
    tightened names what had group/other permission bits and was set to 0600/0700 ("dir", "file", "lock", "ids").
    lock_wait_s (prune's remaining budget) shortens the lock wait below EXPORT_LOCK_WAIT_S."""
    path, kind = export_path()
    tightened: list = []
    _export_dir(os.path.dirname(path), kind, tightened)
    lock = _open_private(path + ".lock", os.O_RDWR, "lock", tightened)
    try:
        _flock_wait(lock, EXPORT_LOCK_WAIT_S if lock_wait_s is None else max(0.0, min(EXPORT_LOCK_WAIT_S, lock_wait_s)))
        fd = _open_private(path, os.O_RDWR | os.O_APPEND, "file", tightened)
        try:
            ids_fd = _open_private(path + ".ids", os.O_RDWR | os.O_APPEND, "ids", tightened)
            try:
                if task_id in _exported_ids(fd, ids_fd):
                    return kind, True, tightened
                st = os.fstat(fd)
                if st.st_size and os.pread(fd, 1, st.st_size - 1) != b"\n":
                    os.write(fd, b"\n")         # a torn line from a dead writer: ours starts on a line of its own
                if os.write(fd, line) != len(line):
                    raise OSError("short write to the export file")
                _append_ids(ids_fd, [task_id], os.fstat(fd).st_size)
                return kind, False, tightened
            finally:
                os.close(ids_fd)
        finally:
            os.close(fd)
    finally:
        os.close(lock)                          # releases the flock


def _export_task(d: str, meta: dict, res: dict, red: "Redactor", router_run, lock_wait_s=None) -> dict:
    """Build, check and append the task's export record; returns the `export` status for result.json and logs
    task_exported / task_export_failed. Never raises (the task's result stands); errors are recorded by type only."""
    tid = os.path.basename(d)
    try:
        rec = build_export_record(d, meta, res, red, router_run)
        line = json.dumps(rec, ensure_ascii=True, separators=(",", ":")) + "\n"
        if red._re is not None and red._re.search(line):
            raise _ExportLeak()                 # belt and braces: a credential spelling survived; write nothing
        kind, already, tightened = append_export(line.encode("ascii"), tid, lock_wait_s)
    except Exception as exc:
        err = type(exc).__name__
        log_event("task_export_failed", task_id=tid, error=err)
        return {"ok": False, "at": round(time.time(), 3), "error": err}
    extra = {**({"already": True} if already else {}), **({"tightened": tightened} if tightened else {})}
    log_event("task_exported", task_id=tid, path_kind=kind, events=len(rec["events"]), logs=rec["logs"] == "included",
              **extra)
    return {"ok": True, "at": round(time.time(), 3), "path_kind": kind, **extra,
            **({"logs_withheld": True} if rec["logs"] != "included" else {}),
            **({"prompt_withheld": True} if rec["prompt"] is None
               and rec.get("withheld_reason") not in (None, EXPORT_FIELDS_WITHHELD) else {}),
            **({"fields_withheld": rec["withheld_fields"]} if rec.get("withheld_fields") else {})}


def _export_locked(d: str, meta: dict, red: "Redactor", router_run, res=None, lock_wait_s=None):
    """Export a finished task from outside its runner (status after a recovered task's logs were released, prune),
    holding the task's runner.lock so it never races the runner's own export or a recoverer. Returns the export
    status, or None when the lock is held (the runner is still finishing: it exports itself)."""
    fd = None
    try:
        fd = os.open(os.path.join(d, "runner.lock"), os.O_RDWR)
    except FileNotFoundError:
        pass
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
    try:
        cur = _read_json(os.path.join(d, "result.json"))
        if isinstance(cur, dict):
            res = cur
        if not isinstance(res, dict):
            return None
        if (res.get("export") or {}).get("ok"):
            return res["export"]
        res["export"] = _export_task(d, meta, res, red, router_run, lock_wait_s)
        _write_json(os.path.join(d, "result.json"), res)
        return res["export"]
    finally:
        if fd is not None:
            os.close(fd)


def _current_redactor(red=None) -> "Redactor":
    """This process's credentials (its launch env, the host's token env and file) plus `red`'s."""
    toks = set(launch_tokens(os.environ)) | set(_host_tokens()) | set(red.raw if red is not None else ())
    return Redactor([t for t in toks if len(t) >= MIN_TOKEN_LEN])


def _runner_router_run(meta: dict, task_id: str) -> dict:
    """The provider's run record, fetched by the runner with its own launch env (the provider forgets it on restart,
    so it is taken while the run is fresh). A login-mode task never went through the provider."""
    if meta.get("lane"):
        return {"note": "login mode: not routed, no provider run record"}
    return fetch_run(os.environ.get("ANTHROPIC_BASE_URL") or "", os.environ.get("ANTHROPIC_AUTH_TOKEN") or "", task_id)


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
# Claude in Chrome's tools (a chrome child's only browser surface) and what they answer while the extension is not
# attached to the browser. A child whose browser calls all came back with this did no browser work, whatever its result
# text then claims (seen live 2026-11-08: `tabs_context_mcp` answered it and the task ended "succeeded"). It is matched
# in the TOOL RESULTS of those tools only, never in the model's own text.
CHROME_TOOL_PREFIX = "mcp__claude-in-chrome__"
BROWSER_DISCONNECTED = "Browser extension is not connected"
# The whole result text of a disconnected tool, as Claude in Chrome returns it (tabs_context_mcp, 2026-11-08). A
# successful result is matched only against these exactly (stripped): a page that merely quotes the phrase (read_page,
# get_page_text) is website content, not a disconnection.
BROWSER_DISCONNECTED_TEXTS = frozenset({
    BROWSER_DISCONNECTED, BROWSER_DISCONNECTED + ".",
    BROWSER_DISCONNECTED + ". Please ensure the Claude in Chrome extension is installed and connected.",
})


def _result_text(block: dict) -> str:
    """The text of a tool_result block: a plain string, or the text parts of a content list."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p["text"] for p in c if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _is_browser_disconnected(block: dict) -> bool:
    """A chrome tool_result that is structurally the extension's disconnect error: an error result naming it, or a
    result whose entire text is the known message. Never a substring of ordinary (page) content."""
    text = _result_text(block)
    if block.get("is_error") is True:
        return BROWSER_DISCONNECTED in text
    return text.strip() in BROWSER_DISCONNECTED_TEXTS


def browser_disconnected_reason(calls: dict) -> str:
    """The task's error text when its Claude in Chrome tools reported no connected extension."""
    names = ", ".join(sorted(t[len(CHROME_TOOL_PREFIX):] if t.startswith(CHROME_TOOL_PREFIX) else t for t in calls))
    return (f"Claude in Chrome was not connected: {sum(calls.values())} browser tool call(s) ({names}) returned "
            f"'{BROWSER_DISCONNECTED}', so no browser work was done, whatever the result text says. Open Chrome on the "
            "host with the Claude extension attached and start the task again.")


# A child whose last request failed at the API (#97). Claude Code then prints a result record whose TEXT is the error
# ("API Error: <status> <message>"), with subtype "success" and is_error true or false depending on its version (seen
# live 2026-11-10: a pinned gpt-6.1-sol child's Codex turn failed, the provider closed the conversation, and the result
# text was exactly the provider's terminal refusal, printed twice; is_error false, exit 0, so the task "succeeded").
# The error is matched on the FIRST LINE of the result only: an answer that merely quotes an API error further down is
# a normal answer.
API_ERROR_PREFIX = "API Error:"
# The provider's own terminal (never retried) refusals of a conversation it closed, as a client prints them: with or
# without Claude Code's prefix (provider/server.py: Registry.failure, the "closed while waiting" LostSession).
TERMINAL_CONVERSATION_RE = re.compile(
    r"\bconversation (?:[0-9a-f]+ failed at \d\d:\d\d:\d\d \([^\n]*\) and was closed: "
    r"|was closed while this request waited for it; )start a new conversation\b")
# The refusal reason the provider records for a request on a failed conversation (server._failed_reason's fixed
# prefix; the allowlisted diagnostic follows). TERMINAL_STATUS / TERMINAL_ERROR there.
TERMINAL_REFUSAL_PREFIX = "the conversation already failed (terminal)"
TERMINAL_REFUSAL_STATUS = 400
TERMINAL_REFUSAL_TYPE = "invalid_request_error"
ERROR_LINE_CHARS = 500              # the `error` text made from a result: one line, capped (the full text stays in result)
# The `message.model` Claude Code puts on an assistant message IT wrote (its API-error text), not the model: the
# text rule of result_error() needs the child's own last message to be one (summarize_stream's last_message_synthetic),
# because a real answer can begin with "API Error: 429 means ..." (a false positive reported on the first version).
SYNTHETIC_MODEL = "<synthetic>"


def _error_line(text: str) -> str:
    """The first non-empty line of a result text, capped at ERROR_LINE_CHARS (the full text stays in `result`)."""
    first = text.strip().split("\n", 1)[0].strip()
    return first if len(first) <= ERROR_LINE_CHARS else first[:ERROR_LINE_CHARS - 1] + "…"


def result_error(out, activity=None) -> "str | None":
    """Why a parsed result record is an error, else None (#97): the record says so (is_error true), or its text IS an
    API error (first line "API Error: ..." as Claude Code prints one, or the provider's terminal-conversation refusal)
    AND the stream summary (`activity`) shows the child's own last message was Claude Code's synthetic error message
    (last_message_synthetic). The text alone never decides: a model's answer may begin "API Error: 429 means ...",
    and a stored record with no stream evidence (an older runner's) keeps its state. A result that only quotes an
    API error inside a normal answer is not matched either: the first line decides."""
    if not isinstance(out, dict) or out.get("type") != "result":
        return None
    text = out.get("result")
    first = _error_line(text) if isinstance(text, str) else ""
    if out.get("is_error") is True:
        return first or f"the child's result record is an error (subtype {out.get('subtype')!r}) with no text"
    synthetic = isinstance(activity, dict) and activity.get("last_message_synthetic") is True
    if synthetic and (first.startswith(API_ERROR_PREFIX) or TERMINAL_CONVERSATION_RE.search(first)):
        return first
    return None


def terminal_refusal(router_run) -> "str | None":
    """The provider's last recorded refusal of this run, if it was a terminal one (a request on a conversation the
    provider had closed: TERMINAL_REFUSAL_*), as text; else None."""
    refusals = router_run.get("refusals") if isinstance(router_run, dict) else None
    if not isinstance(refusals, list) or not refusals or not isinstance(refusals[-1], dict):
        return None
    last = refusals[-1]
    reason = last.get("reason")
    if (last.get("status") == TERMINAL_REFUSAL_STATUS and last.get("type") == TERMINAL_REFUSAL_TYPE
            and isinstance(reason, str) and reason.startswith(TERMINAL_REFUSAL_PREFIX)):
        return f"HTTP {last.get('status')} {last.get('type')}: {reason}"
    return None


def unanswered_terminal_refusal(router_run, activity) -> "str | None":
    """Rule (c) of #97: the run's last request was refused with a terminal error and the child's own conversation never
    got a later answer (its last message event is not an assistant message: the tool results it delivered, or its
    first request, were never answered). A terminal refusal of a subagent's conversation while the child's own went on
    to answer is NOT this: the child's last message event is its answer. Only the runner can apply it (it has the run
    record and the stream); a result text that is the error is caught by result_error() without it."""
    reason = terminal_refusal(router_run)
    if reason is None or not isinstance(activity, dict):
        return None
    if activity.get("last_message") == "assistant":
        return None
    return (f"the provider refused the run's last request with a terminal error and the child's last request was never "
            f"answered: {reason}")


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
    names: dict = {}                        # tool_use id -> tool name (to read a tool_result as its tool's answer)
    disconnected: dict = {}                 # chrome tool name -> results saying BROWSER_DISCONNECTED
    last_message = None                     # type of the child's OWN last message event (subagent events excluded)
    last_synthetic = None                   # ... and whether it is Claude Code's own (model "<synthetic>") message
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
            if kind in ("assistant", "user") and not ev.get("parent_tool_use_id"):
                # A conversation that ended answered ends with an assistant message; one whose last request failed
                # ends with the tool results it delivered (a user message), or with no message at all (#97, rule c).
                # A subagent's events carry parent_tool_use_id and say nothing about the child's own conversation.
                # An assistant message the CLI wrote itself (its "API Error: ..." text) carries model "<synthetic>";
                # that, not the text, is what lets result_error() call a result text an error.
                last_message = kind
                last_synthetic = kind == "assistant" and (ev.get("message") or {}).get("model") == SYNTHETIC_MODEL
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
                    if isinstance(b.get("id"), str):
                        names[b["id"]] = name
                elif kind == "user" and b.get("type") == "tool_result":
                    if b.get("is_error"):
                        errors += 1
                    tid = b.get("tool_use_id")
                    if tid in attempts and tid not in outcome:
                        outcome[tid] = "failed" if b.get("is_error") else "ok"
                    tool = names.get(tid)
                    if (isinstance(tool, str) and tool.startswith(CHROME_TOOL_PREFIX)
                            and _is_browser_disconnected(b)):
                        disconnected[tool] = disconnected.get(tool, 0) + 1
    written = [p for i, p in attempts.items() if outcome.get(i) == "ok"]
    unconfirmed = [{"path": p, "outcome": outcome.get(i, "unresolved")} for i, p in attempts.items()
                   if outcome.get(i) != "ok"]
    outside = [p for p in written if os.path.commonpath([root, p]) != root]
    out = {"tool_uses": tool_uses, "tool_errors": errors, "effective_permission_mode": mode,
           "files_written": written[:MAX_ACTIVITY_PATHS], "writes_outside_cwd": outside[:MAX_ACTIVITY_PATHS],
           "write_attempts_unconfirmed": unconfirmed[:MAX_ACTIVITY_PATHS], "last_message": last_message,
           "last_message_synthetic": last_synthetic}
    if disconnected:
        out["browser_disconnected"] = disconnected   # present only when seen: classify() fails the task on it
    return out


class _CancelledBeforeSpawn(Exception):
    pass


class _QueueTimeout(Exception):
    pass


def _acquire_chrome_lock(d: str, path: str, flag: dict):
    """Take the host's Chrome lock (one browser child at a time), polling so a cancel is noticed while queued. The
    QUEUED_MARKER in the task dir shows the wait in task_status. Returns the lock fd (closing it releases the lock)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    marker = os.path.join(d, QUEUED_MARKER)
    end = time.monotonic() + CHROME_QUEUE_MAX_S
    queued = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                pass
            if not queued:
                open(marker, "w").close()
                queued = True
            if flag["pre"] or os.path.exists(os.path.join(d, "cancelled")):
                raise _CancelledBeforeSpawn()
            if time.monotonic() >= end:
                raise _QueueTimeout()
            time.sleep(CHROME_QUEUE_POLL_S)
    except BaseException:
        os.close(fd)
        raise
    finally:
        if queued:
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass


def _prepare_run(d: str, spec: dict) -> None:
    """Before the spawn, on the execution host: create the workspace (clone/fetch/worktree or plain folder) and
    point the spec and meta at it; then, for a chrome child, the fail-closed project settings check the start could
    not make while the folder did not exist. Raises WorkspaceError / TaskError: the task fails without a spawn."""
    ws_spec, root = spec.get("workspace"), spec.get("workspace_root")
    if ws_spec:
        ws = workspaces.prepare(ws_spec, root)
        _write_json(os.path.join(d, WORKSPACE_FILE), ws)
        ev = spec.get("event") or {}
        workspaces.update_index(root, {**ws, **({"client": ev["client"]} if ev.get("client") else {})})
        spec["cwd"] = ws["path"]
        spec["event"]["cwd"] = ws["path"]
        _write_json(os.path.join(d, "cmd.json"), spec)
        meta = _read_json(os.path.join(d, "meta.json")) or {}
        meta["cwd"] = ws["path"]
        _write_json(os.path.join(d, "meta.json"), meta)
    if spec.get("chrome"):
        check_login_settings(spec["cwd"])


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
    if spec.get("launchd"):
        # launchd spawned this runner, so start() could not record the pid; cancel() needs it (with the lock and the
        # command-line check, as for a spawned runner).
        meta = _read_json(os.path.join(d, "meta.json")) or {}
        meta["runner_pid"] = os.getpid()
        _write_json(os.path.join(d, "meta.json"), meta)
    chrome_fd = None
    try:
        if spec.get("env_file"):
            _load_runner_env(spec)              # a routed launchd runner: launchd gave it no env of the task's
        if flag["pre"] or os.path.exists(os.path.join(d, "cancelled")):
            raise _CancelledBeforeSpawn()       # checked before any clone: a cancelled task costs no fetch
        _prepare_run(d, spec)
        if spec.get("chrome_lock"):
            chrome_fd = _acquire_chrome_lock(d, spec["chrome_lock"], flag)
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
    except _QueueTimeout:
        result["error"] = (f"waited {CHROME_QUEUE_MAX_S} s for the host's Chrome (another browser task held it the whole "
                           "time) and gave up; the child was never started")
    except (workspaces.WorkspaceError, TaskError) as exc:
        result["error"] = f"could not prepare the task on this host: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:   # SubprocessError: e.g. preexec_fn failed in the child
        result["error"] = f"could not start the child: {type(exc).__name__}: {exc}"
    finally:
        if chrome_fd is not None:
            os.close(chrome_fd)                 # releases the Chrome lock to the next queued browser task
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
    result["logs_redacted"] = not result.get("redact_error")
    # The prompt too (in place: temp file 0600, atomic rename): a late export (status after a crash, prune after the
    # credentials rotated) may only know other credentials, so nothing raw may stay for it to read (#91 review, P1).
    try:
        redact_file(os.path.join(d, "prompt.txt"), red)
        result["prompt_redacted"] = True
    except OSError as exc:
        result["prompt_redacted"] = False
        result["prompt_redact_error"] = f"could not redact prompt.txt: {type(exc).__name__}"
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
        result["activity"], n = red.deep_count(summarize_stream(os.path.join(d, "stdout.log"), spec.get("cwd") or d))
        if n:
            result["redact_unparsed_lines"] = result.get("redact_unparsed_lines", 0) + n
        calls = result["activity"].get("browser_disconnected") if isinstance(result["activity"], dict) else None
        if calls and not result.get("error"):
            result["error"] = browser_disconnected_reason(calls)   # the task fails (classify) with this as its reason
    except OSError as exc:                       # no stdout at all (the child never started): nothing to summarize
        result["activity_error"] = f"could not read stdout.log: {type(exc).__name__}"
    # The provider's record of the run, taken now while it is fresh (the provider forgets it on restart): read for the
    # failure check below and handed to the export after the result is final.
    try:
        meta = _read_json(os.path.join(d, "meta.json")) or {}
    except RecordError:
        meta = {}
    router_run = _runner_router_run(meta, os.path.basename(d))
    if not result.get("error"):
        # #97: a child whose last request failed at the API. The result record says so (is_error), or its text IS the
        # error (Claude Code's "API Error: ...", with is_error false in some versions), or the provider's record shows
        # the run's last request refused as terminal and the child's own conversation never answered after it. The
        # reason is the task's error (classify fails it on `error`); the result text is kept as it is.
        reason = result_error(result.get("output"), result.get("activity"))
        if reason is None:
            reason = unanswered_terminal_refusal(router_run, result.get("activity"))
        if reason is not None:
            result["error"] = reason
    # The child is over: its workspace leaves active/ (archive/ when pushed or no repository, else held/) before the
    # result is published, so a caller that sees the final state also sees where the folder went.
    ws = _settle_workspace(d)
    if ws is not None:
        result["workspace"] = ws
    # Free text of the result redacted on disk with the launch credentials too: a late export only knows the
    # credentials of its own time (#91 review round 2, P1).
    for k in ("error", "output_error", "activity_error"):
        if isinstance(result.get(k), str):
            result[k] = red.text(result[k])
    if isinstance(result.get("workspace"), dict):
        result["workspace"] = red.tree(result["workspace"])
    result["ended_at"] = time.time()
    final = classify(result)
    started = spec.get("started_at")
    result["event_log_error"] = log_event(
        "finished", **(spec.get("event") or {}), status=final, exit_code=result["exit_code"],
        timed_out=bool(result.get("timed_out")),
        workspace_state=ws.get("state") if isinstance(ws, dict) else None,
        duration_s=round(result["ended_at"] - started, 1) if isinstance(started, (int, float)) else None)
    _write_json(os.path.join(d, "result.json"), result)
    # The run export (#91): only now, with the logs redacted and result.json final, and still under the task lock (so
    # no status call or prune exports it in parallel). Its outcome is added to result.json; a failure is retried by
    # prune, which never deletes an unexported task.
    result["export"] = _export_task(d, meta, result, red, router_run)
    try:
        _write_json(os.path.join(d, "result.json"), result)
    except OSError:
        pass                                    # the export line stands; prune finds it by id and marks it then
    os.close(lock_fd)                           # release only after result.json exists
    return 0


# --- remote mode (the execution host's side of exec_hosts.call) ---------------------------------------------------

def _apply_host_env(host: "exec_hosts.Host") -> None:
    """An ssh session gets a bare environment: the host's entry supplies what children need there."""
    if host.path:
        os.environ["PATH"] = host.path
    if host.claude:
        os.environ["CORTADOS_CHILD_CLAUDE_BIN"] = host.claude
    if host.provider_url:
        os.environ["CORTADOS_PROVIDER_URL"] = host.provider_url


def _host_tokens() -> list:
    """This host's provider credentials (env and token file), for redaction of what leaves over ssh."""
    toks = [(os.environ.get("CORTADOS_PROVIDER_TOKEN") or "").strip()]
    try:
        toks.append(session.read_token(os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or session.DEFAULT_TOKEN_FILE))
    except session.SessionError:
        pass
    return [t for t in toks if t]


def serve_remote(stdin=None, stdout=None) -> int:
    """Answer ONE request from exec_hosts.call: {"v", "op", "host", "args"} on stdin, one JSON line on stdout. This
    machine must be the named host (its entry's hostnames include this hostname), else the request is refused: a
    misrouted ssh alias must not run tasks on the wrong machine or bounce them on again."""
    global _REMOTE_SERVING
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    def reply(ok, payload, failure=None):
        rec = {"v": exec_hosts.PROTOCOL, "ok": ok, ("result" if ok else "error"): payload}
        if failure is not None:
            rec["failure"] = failure            # an unexpected exception's classification (exec_hosts.call carries it)
        stdout.write(json.dumps(rec) + "\n")
        stdout.flush()
        return 0 if ok else 1

    try:
        req = json.load(stdin)
    except (ValueError, RecursionError):
        return reply(False, "remote request is not JSON")
    if not isinstance(req, dict) or req.get("v") != exec_hosts.PROTOCOL:
        return reply(False, f"remote request protocol {req.get('v') if isinstance(req, dict) else None!r} != {exec_hosts.PROTOCOL}: "
                            "update the cortados-router checkout on both machines")
    op, args = req.get("op"), req.get("args") if isinstance(req.get("args"), dict) else {}
    try:
        host = exec_hosts.load().resolve(req.get("host"))
        if not host.is_local():
            raise exec_hosts.HostError(f"this machine ({exec_hosts.short_hostname()}) is not execution host "
                                       f"{host.describe()}: add its hostname to that entry's `hostnames` in "
                                       f"{exec_hosts.config_path()} here, or fix the ssh alias")
    except exec_hosts.HostError as exc:
        return reply(False, str(exc))
    _REMOTE_SERVING = True
    _apply_host_env(host)
    tokens = _host_tokens()
    try:
        red = Redactor(tokens)
        token = tokens[0] if tokens else ""
        try:
            url = session.resolve_provider_url()
        except session.ProviderURLError:
            url = ""
        if op == "start":
            out = start(args.get("task"), args.get("cwd"), args.get("orchestrator") or "cortados",
                        args.get("permission_mode") or None, args.get("allowed_tools"), "", None,
                        args.get("chrome", False), red, args.get("connectors"), client=args.get("client"),
                        title=args.get("title"), session_tag=args.get("session_tag"), repo=args.get("repo"),
                        branch=args.get("branch"), host=host.name, parent_task_id=args.get("parent_task_id"),
                        local=args.get("local"), origin=args.get("origin"))
        elif op == "status":
            out = status(args.get("task_id"), red, url, token)
        elif op == "wait":
            out = wait(args.get("task_id"), args.get("timeout_s"), red, provider_url=url, token=token,
                       max_s=args.get("max_s"))
        elif op == "list":
            ids = args.get("ids")
            out = list_recent(args.get("limit", 20), ids if isinstance(ids, list) else None)
        elif op == "cancel":
            out = cancel(args.get("task_id"))
        else:
            return reply(False, f"unknown remote operation {op!r}")
    except StartFailed as exc:                  # classified by start() itself: carry it so the caller can say what to do
        return reply(False, str(exc), exc.failure())
    except TaskError as exc:
        text = str(exc).strip() or (f"{op} failed on execution host {host.name}: {type(exc).__name__} without a "
                                    "message (a cortados-router bug: please report it)")
        return reply(False, Redactor(tokens).text(text) if tokens else text)
    except Exception as exc:                    # the type only: the text is unscrubbed and could echo configuration
        import traceback
        traceback.print_exc(file=sys.stderr)
        # Outside start() (start classifies its own): for a start request nothing was launched yet.
        return reply(False, f"internal error on execution host {host.name} ({type(exc).__name__})",
                     {"type": type(exc).__name__, "launched": "none", "task_id": None})
    return reply(True, red.tree(out))


def cleanup_main(argv: list) -> int:
    """`--cleanup-archive [--root <dir>]`: the daily archive cleanup (integrations/launchd/com.cortados.archive-cleanup.plist).
    The root is the local host entry's unless given. Prints the report as JSON; exit 1 if anything errored."""
    root = None
    if argv[:1] == ["--root"] and len(argv) == 2:
        root = argv[1]
    elif argv:
        print("usage: python3 cortados_router/child_tasks.py --cleanup-archive [--root <workspace root>]", file=sys.stderr)
        return 2
    if root is None:
        try:
            me = exec_hosts.load().local_entry()
        except exec_hosts.HostError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if me is None or not me.root:
            print(f"no entry in {exec_hosts.config_path()} names this machine with a workspace root; pass --root", file=sys.stderr)
            return 2
        root = me.root
    report = workspaces.cleanup_archive(root)
    print(json.dumps({"root": root, **report}, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) in (2, 4) and a[0] == "--run" and (len(a) == 2 or a[2] == "--lock-fd"):
        sys.exit(_run(a[1], int(a[3]) if len(a) == 4 else None))
    if a == ["--remote"]:
        sys.exit(serve_remote())
    if a[:1] == ["--cleanup-archive"]:
        sys.exit(cleanup_main(a[1:]))
    if a[:1] == ["--watch"]:
        sys.exit(watch_main(a[1:]))
    print("usage: python3 cortados_router/child_tasks.py --run <task dir> [--lock-fd N] | --remote | "
          "--cleanup-archive [--root <dir>] | --watch <task_id> [...] [--timeout S] [--interval S] [--stall S]",
          file=sys.stderr)
    sys.exit(2)
