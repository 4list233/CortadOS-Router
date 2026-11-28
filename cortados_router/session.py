"""Run a whole Claude Code session on the cortados provider.

    python3 -m cortados_router.session [--route R3:opus+sol|R4:*|R3|any|last] [--no-route-prompt]
        [--orchestrator fable|opus|sonnet|<model id>|cortados] [--orchestrator-band R3|R3-R4|any]
        [--provider-url URL] [--token-file PATH] [--no-delegate-directive] [--print-env] [-- <claude args>]

On a terminal with no route flag it first shows the route menu (docs/ORCHESTRATOR-ROUTE-PICKER.md §1.2): a band, any,
or a pin, then the band's models from the provider's cached readings; Enter keeps the last pick (first time: today's
default, the pinned orchestrator below). --route picks without the menu; --no-route-prompt skips it.

The main loop (the orchestrator) is pinned to a subscription model (`sub/<id>`, no S1) and delegates through the
cortados-route MCP tools (start_task / route_task); its own Agent calls are refused by the gate (orchestrator_gate.py).
Claude Code's NATIVE subagents, which keep every tool including MCP, are sent to `cortados` so S1 routes each one
(this matters for child tasks, launched through this module with the gate off, whose subagents are allowed). Stdlib only.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path

from cortados_router import core
from cortados_router.provider import routing
from cortados_router.subagent_mcp import resolve_provider_url, ProviderURLError

# Short names map to the ids the provider itself serves (routing.SUB_ALIASES is the provider's alias table, checked
# against the tables' claude-cli models), so the launcher can never pin a model the provider would refuse.
ORCHESTRATORS = {name: routing.SUB_PREFIX + model for name, model in routing.SUB_ALIASES.items()}
DEFAULT_ORCHESTRATOR = "fable"
DEFAULT_TOKEN_FILE = "~/.config/cortados/provider-token"
# Side calls (titles, summaries) skip S1: they are tiny, and S1 scoring would add latency to every one.
SMALL_FAST_MODEL = routing.SUB_PREFIX + routing.SUB_ALIASES["haiku"]
DIRECTIVE_FILE = Path(__file__).resolve().parent.parent / "integrations" / "orchestrator-directive.md"
# Same list the plugin blanks (integrations/claude-mod/cortados-routed-subagents/hooks/register.ts) so no
# alternative provider in the user's env can take the session away from ANTHROPIC_BASE_URL.
BLANKED = (
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
)
SECRET_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# The routed-subagents plugin would run subagents as separate `claude -p` processes without MCP tools and would add
# its directive a second time. It is turned off for this session only (`enabledPlugins` is the settings key in
# Claude Code 2.1.316; explicit enabledPlugins values win over defaults).
PLUGIN_ID = "cortados-routed-subagents@cortados-router"
# Claude Code applies a settings file's `env` over the process environment, so a user/project settings.json with its
# own ANTHROPIC_API_KEY or ANTHROPIC_BASE_URL would win over the launcher's env and could send the session (and a real
# key) elsewhere. Settings sources merge user < project < local < flag < policy (Claude Code 2.1.316), so the launcher
# writes its env into a private file passed with `--settings` (flag precedence; never secrets in argv). Only managed
# policy outranks it, so a managed env that sets any of these keys refuses the launch.
# Each launch gets its own file (mkstemp: O_EXCL, 0600) in this verified private directory, so concurrent launches never
# read each other's provider or token. The launcher execs claude and cannot delete its file, so each launch prunes the
# directory's own session-*.json files older than PRUNE_AGE. Seven days, not one: Claude Code may re-read its settings
# sources mid-session, so pruning the file of a session that is still open could drop its provider env; a week covers
# any realistic session while still clearing stale token copies.
SETTINGS_DIR = "~/.config/cortados/sessions"
SETTINGS_PATTERN = "session-*.json"
PRUNE_AGE = 7 * 24 * 3600
# Passthrough args that would add a settings source after ours or replace the launcher's model.
REFUSED_ARGS = ("--settings", "--setting-sources", "--model")
MANAGED_SETTINGS_DIRS = ("/Library/Application Support/ClaudeCode", "/etc/claude-code")


# The routed main loop's band window (--orchestrator cortados): S1 still routes it and scarcity picks the cheapest
# capable model, but inside this window (owner 2026-11-24: by default exactly R3, the band whose reference index Opus 5.5
# and Sol 6.1 clear (tiering.band_references R3 = claude-opus-5 / gpt-5.6-sol / gpt-6.1-sol); it was R2 from 2026-11-23.
# Not R4: R4 is the Fable / Astra escalation band, and core.required_index gives R4 no index floor of its own, so an R4
# window let easy turns fall to Haiku-class models). It rides on the main loop's model name, `cortados@R3`
# (routing.BAND_MODEL_PREFIX): Claude Code sends --model on
# main-loop requests only (checked with Claude Code 2.1.330 against a capture server, 2026-11-23: main loop
# `cortados@R1-R2`, its general-purpose subagent `cortados`), so subagents (CLAUDE_CODE_SUBAGENT_MODEL) and side calls
# (ANTHROPIC_SMALL_FAST_MODEL) stay unwindowed. `any` turns it off (plain `cortados`, S1 routes freely).
DEFAULT_ORCHESTRATOR_BAND = "R3"


class SessionError(Exception):
    pass


def windowed_model(model: str, band: str | None) -> str:
    """The main loop's model with its band window: `cortados` + R3 → `cortados@R3`. A routed (`cortados`) main loop
    gets DEFAULT_ORCHESTRATOR_BAND when `band` is None; `any` leaves it unwindowed. A pinned model is not routed, so a
    window there is refused rather than silently dropped."""
    if model != "cortados":
        if band is not None and band.strip().lower() != "any":
            raise SessionError("--orchestrator-band applies only to the routed orchestrator (--orchestrator cortados); "
                               f"{model} is a pinned model")
        return model
    try:
        window = core.parse_band_window(DEFAULT_ORCHESTRATOR_BAND if band is None else band)
    except ValueError:
        raise SessionError(f"--orchestrator-band {band!r} is not a band window: a band R0-R4 (R2), a range (R1-R2), "
                           "or any") from None
    return routing.BAND_MODEL_PREFIX + core.band_window_label(*window) if window else model


def resolve_model(name: str) -> str:
    name = name.strip()
    if not name:
        raise SessionError("empty --orchestrator")
    if name in ORCHESTRATORS:
        return ORCHESTRATORS[name]
    if name == "cortados" or name.startswith("sub/"):
        return name
    return "sub/" + name


def read_token(path: str) -> str:
    p = Path(os.path.expanduser(path))
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SessionError(f"cannot read provider token file {p}: {exc.strerror}") from None
    if not token:
        raise SessionError(f"provider token file {p} is empty")
    return token


RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")   # the provider's x-cortados-run-id pattern (provider/server.py)


# The Office tag headers a child task may carry (provider/office.py groups its traffic under its orchestrator's floor;
# x-cortados-origin names the orchestrator that started the task, the provider's own record of the run's owner).
# Only these names, and a value of printable ASCII up to 80 chars: the variable is newline-separated, so a newline in
# a value would inject a header of its own (an x-api-key overriding the provider token).
OFFICE_HEADERS = ("x-cortados-session", "x-cortados-project", "x-cortados-device", "x-cortados-task-title",
                  "x-cortados-origin")
_OFFICE_VALUE_RE = re.compile(r"[\x20-\x7e]{1,80}")


def parse_office_header(item: str) -> tuple[str, str]:
    name, sep, value = str(item).partition(":")
    name, value = name.strip().lower(), value.strip()
    if not sep or name not in OFFICE_HEADERS or not _OFFICE_VALUE_RE.fullmatch(value):
        raise SessionError("an --office-header must be `<office header>: <printable ASCII, 1-80 chars>`")
    return name, value


# The provider's x-cortados-local values (provider/routing.LOCAL_MODES; checked by a test): a child task started with
# start_task's `local` option sends it on every request, so the provider may run each one on the owner's local model.
LOCAL_MODES = ("sensitive", "prefer")


# The provider's effect_boundary.HOOK_HEADER (checked by a test): this session's PreToolUse boundary hook is installed,
# so a sensitive local-routed turn may offer file writes (docs/BOUNDARY-HOOK.md). Without it such a turn is read-only.
BOUNDARY_HOOK_HEADER = "x-cortados-boundary-hook"
# The file writers (path containment on this machine) and Bash (profile): every session and child task.
BOUNDARY_HOOK_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"
# A session launched with a local pick (step 8 of the route picker): every tool, so the hook also checks every other
# tool of a provider-marked call against the orchestrator profile (plan and delegate only, docs/BOUNDARY-HOOK.md). Only
# there (review localorch3): the hook is a Python start per call (about 158 ms per Read measured), and in any other
# session the provider's own check of a marked call stands alone for the tools outside BOUNDARY_HOOK_MATCHER.
BOUNDARY_HOOK_MATCHER_LOCAL = "*"


BOUNDARY_HOOK_FILE = Path(__file__).resolve().with_name("boundary_hook.py")
# Where the hook's interpreter is looked for, in order, after the interpreter running this launcher: absolute paths
# only, never a PATH lookup (a project's bin/, venv or node_modules/.bin may come first on PATH; docs/BOUNDARY-HOOK.md).
SYSTEM_PYTHONS = ("/usr/bin/python3",)
# The shell guard around the hook's interpreter (docs/BOUNDARY-HOOK.md "Isolation"). Claude Code treats any hook exit
# but 2 as a non-blocking error and runs the call, so an interpreter that cannot start (a missing path: 126/127; a
# broken /usr/bin/python3 shim) would let a provider-marked call through. The guard buffers the event, runs the
# interpreter in a subshell from /, passes 0 and 2 through, and on any other status decides from the event's
# tool_use_id FIELD alone: an id starting with toolu_cxb_ (effect_boundary.MARK_PREFIX) is denied (exit 2), any other
# id allowed (exit 0). An id it cannot read (no single "tool_use_id" key with a plain value; a missing grep or sed)
# denies when the raw event holds "toolu_cxb_ anywhere: a false block, never a false allow. Helpers by absolute path,
# never PATH. The mod's hooks/register.ts carries the same two strings (tests/test_boundary_hook.py keeps them equal).
BOUNDARY_HOOK_GUARD_HEAD = r"""ev=$(/bin/cat) || { printf 'cortados boundary: denied: the hook could not read its input. Nothing was run.\n' >&2; exit 2; }; printf '%s\n' "$ev" | (cd / && exec """
BOUNDARY_HOOK_GUARD_TAIL = r"""); rc=$?; [ "$rc" -eq 0 ] && exit 0; [ "$rc" -eq 2 ] && exit 2; f=$(printf '%s' "$ev" | /usr/bin/tr '\n\r\t' '   '); id=; printf '%s\n' "$f" | /usr/bin/grep -q '"tool_use_id"[[:space:]]*:.*"tool_use_id"[[:space:]]*:'; if [ "$?" -eq 1 ]; then id=$(printf '%s\n' "$f" | /usr/bin/sed -n 's/^.*"tool_use_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_-]\{1,\}\)".*$/\1/p'); fi; case $id in toolu_cxb_*) m=1;; ?*) m=0;; *) case $ev in *'"toolu_cxb_'*) m=1;; *) m=0;; esac;; esac; if [ "$m" = 1 ]; then printf 'cortados boundary: denied: the hook did not run (exit %s), and a provider-marked call fails closed. Nothing was run.\n' "$rc" >&2; exit 2; fi; exit 0"""


def guarded_hook_command(python: str, args: str) -> str:
    """The boundary hook's shell command: `<python> -I -S -B <args>` (args already shell-quoted) inside the guard."""
    return BOUNDARY_HOOK_GUARD_HEAD + shlex.quote(python) + " -I -S -B " + args + BOUNDARY_HOOK_GUARD_TAIL


def _within(path: str, folder: str) -> bool:
    folder = folder.rstrip("/") or "/"
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _inside_project(path: str, project: str | None) -> bool:
    """path (lexically or with links followed) is the project folder or lies below it (lexically or real)."""
    if not project:
        return False
    roots = {os.path.normpath(os.path.abspath(project)), os.path.realpath(project)}
    forms = {os.path.normpath(os.path.abspath(path)), os.path.realpath(path)}
    return any(_within(p, r) for p in forms for r in roots)


# Why a project folder holding this router checkout is refused, and what to do instead (#128, docs/BOUNDARY-HOOK.md
# "Hook location"). No copy of the hook made from that checkout can be trusted: the child may rewrite the checkout,
# and every later launcher, MCP server and hook copy would run its bytes.
BOUNDARY_HOOK_INSIDE_PROJECT = (
    "the project folder holds the router checkout this launcher runs from (the boundary hook script lies inside "
    "it). A child may write its project folder, and that checkout holds the launcher, MCP server and boundary hook "
    "code every later task trusts, so it cannot be a task's project folder. Instead give `repo` (+ `branch`) for a "
    "git worktree under CortadOS/active, or a `cwd` outside the router checkout (e.g. a notes folder)")


def boundary_hook_python(project: str | None = None, candidates=None) -> str:
    """The absolute interpreter for the boundary hook: the real path of the interpreter running this launcher, else
    SYSTEM_PYTHONS; one inside the project folder is refused (the model may write there). SessionError when none."""
    if candidates is None:
        candidates = ([os.path.realpath(sys.executable)] if sys.executable else []) + list(SYSTEM_PYTHONS)
    for c in candidates:
        if (c and os.path.isabs(c) and os.path.isfile(c) and os.access(c, os.X_OK)
                and not _inside_project(c, project)):
            return c
    raise SessionError("no python3 for the boundary hook outside the project folder (tried "
                       + ", ".join(map(str, candidates)) + ")")


def boundary_hook_entry(project: str | None = None, python: str | None = None, local_pick: bool = False,
                        watch_prefix: str | None = None) -> dict:
    """The PreToolUse entry running boundary_hook.py: `<absolute python3> -I -S -B <absolute script>` (shell-quoted),
    from / inside the shell guard (guarded_hook_command). No PATH lookup, isolated mode, no site import, fail closed for
    a marked call when the interpreter cannot start (docs/BOUNDARY-HOOK.md "Isolation"). A hook script inside
    the project folder is refused. Timeout 30 s, well past the script's own 5 s provider timeout, so the script always
    answers (and fails closed) itself. local_pick: matcher BOUNDARY_HOOK_MATCHER_LOCAL, and watch_prefix (the delegation
    MCP server's child_tasks.watch_prefix) goes to the hook as --watch-prefix: the only program its watcher may run."""
    if _inside_project(str(BOUNDARY_HOOK_FILE), project):
        raise SessionError(BOUNDARY_HOOK_INSIDE_PROJECT)
    python = python or boundary_hook_python(project)
    args = shlex.quote(str(BOUNDARY_HOOK_FILE))
    if local_pick and watch_prefix:
        args += " --watch-prefix " + shlex.quote(watch_prefix)
    command = guarded_hook_command(python, args)
    return {"matcher": BOUNDARY_HOOK_MATCHER_LOCAL if local_pick else BOUNDARY_HOOK_MATCHER,
            "hooks": [{"type": "command", "command": command, "timeout": 30}]}


STEER_HOOK_FILE = Path(__file__).resolve().with_name("steer.py")
# docs/STEER-TASK.md: after each tool call (also a failed one) of a child's main loop, steer.py hands the model the
# owner's pending steering notes as additionalContext. 10 s: the hook only reads and appends two small files.
STEER_HOOK_EVENTS = ("PostToolUse", "PostToolUseFailure")
STEER_HOOK_TIMEOUT_S = 10


def steer_hook_entries(steer_dir: str, project: str | None = None, python: str | None = None) -> dict:
    """{event: [entry]} running `<absolute python3> -I -S -B <absolute steer.py> --hook <task dir>` (shell-quoted) for
    every STEER_HOOK_EVENTS event, matcher `*`. Like the boundary hook, the script and interpreter must lie outside the
    project folder (the child may write there). Not guarded: the hook always exits 0, and a hook that cannot start is a
    non-blocking error, so the tool result stands and the note waits."""
    if not (isinstance(steer_dir, str) and os.path.isabs(steer_dir)):
        raise SessionError("--steer-dir must be an absolute path")
    if _inside_project(str(STEER_HOOK_FILE), project):
        raise SessionError(BOUNDARY_HOOK_INSIDE_PROJECT)
    python = python or boundary_hook_python(project)
    command = (shlex.quote(python) + " -I -S -B " + shlex.quote(str(STEER_HOOK_FILE)) + " --hook "
               + shlex.quote(steer_dir))
    entry = {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": STEER_HOOK_TIMEOUT_S}]}
    return {ev: [entry] for ev in STEER_HOOK_EVENTS}


def custom_headers(run_id: str | None = None, office_headers=None, local: str | None = None) -> str:
    """ANTHROPIC_CUSTOM_HEADERS (newline-separated `name: value` lines). With a run id, every request of the session is
    recorded by the provider under it (GET /v1/cortados/runs/<id>); office headers (`name: value` items, OFFICE_HEADERS
    only) tag it for the Office view; `local` (LOCAL_MODES only) adds x-cortados-local."""
    # The boundary hook (BOUNDARY_HOOK_HEADER): settings_content always installs boundary_hook.py next to this header.
    lines = ["x-cortados-reveal: 1", BOUNDARY_HOOK_HEADER + ": 1"]
    if run_id:
        if not RUN_ID_RE.fullmatch(run_id):
            raise SessionError(f"run id {run_id!r} does not match the provider's run-id pattern")
        lines.append("x-cortados-run-id: " + run_id)
    for item in office_headers or ():
        name, value = parse_office_header(item)
        lines.append(f"{name}: {value}")
    if local is not None:
        if local not in LOCAL_MODES:
            raise SessionError(f"--local must be one of {', '.join(LOCAL_MODES)}")
        lines.append("x-cortados-local: " + local)
    return "\n".join(lines)


# Deferred tool loading (#117). With tool search on, Claude Code (2.1.325, measured on a fake API 2026-11-14) opens a
# conversation with only ToolSearch + a DeferredToolPlaceholder and adds an MCP tool's schema to `tools` only after
# ToolSearch loads it. The provider starts its backend with the opening request's tools and never re-declares them
# (server.py "tool set changed mid-conversation"; Codex takes dynamic tools once, on thread/start), so the backend
# model saw a loaded connector tool's name and every call ended in the backend CLI's own "No such tool available"
# (task t20261114164728-86cbe366). ENABLE_TOOL_SEARCH=false puts every MCP tool in the opening request instead, which
# is only safe when the MCP catalog is BOUNDED: the full connector catalog measured ~290k tokens across 495 tools
# (README "Connectors"), over the 200k routes and the 128k local lane, and routing.request_fit counts the whole request,
# so eager schemas there would exclude routes or refuse the session outright. So the flag is set only for
#   - child tasks (`--eager-tools`, passed by child_tasks.start only): with connectors only those load (plus
#     `--strict-mcp-config`, so not the host's own MCP servers); without, the host's own servers load;
#   - whole sessions whose MCP set is explicitly filtered: `--strict-mcp-config` among the claude args (only the
#     `--mcp-config` servers given load, so the user chose and can see the catalog).
# Either way the provider bounds what arrives: every request's tools are checked against its tool-schema budget on
# their real serialized size (provider/server.py TOOL_SCHEMA_BUDGET_BYTES), and an oversized set is refused naming its
# largest namespaces. Any other whole session keeps Claude Code's deferred loading; a tool it loads later still cannot
# be called through the provider (the provider's warning says so) until provider-side tool-set growth lands (#121).
TOOL_SEARCH_OFF = {"ENABLE_TOOL_SEARCH": "false"}
STRICT_MCP_FLAG = "--strict-mcp-config"


def mcp_filtered(extra) -> bool:
    """Whether the claude args restrict the session to an explicit MCP set (STRICT_MCP_FLAG)."""
    return STRICT_MCP_FLAG in (extra or ())


def launcher_env(provider_url: str, token: str, delegate_budget: int = 3, run_id: str | None = None,
                 office_headers=None, local: str | None = None, eager_tools: bool = False) -> dict:
    """Every variable the launcher sets (BLANKED ones as empty). `eager_tools` turns tool search off (TOOL_SEARCH_OFF):
    only for a bounded MCP catalog."""
    env = dict(
        {
            "ANTHROPIC_BASE_URL": provider_url,
            # Both, so a saved Console key cannot leak in through either variable.
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_API_KEY": token,
            "CLAUDE_CODE_SUBAGENT_MODEL": "cortados",
            # INFERRED, NOT VERIFIED: the Claude Code 2.1.316 binary lists CLAUDE_CODE_SUBAGENT_MODEL_FORCE next to
            # CLAUDE_CODE_SUBAGENT_MODEL among its model env vars. From that and its name we infer it makes subagents
            # use CLAUDE_CODE_SUBAGENT_MODEL even when an Agent call or agent definition names a model (e.g.
            # `model: haiku`), which would otherwise bypass S1. That behaviour is pending an end-to-end live check;
            # the unit and provider tests only submit model=cortados directly and do not prove it.
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
            "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "cortados",
            "ANTHROPIC_SMALL_FAST_MODEL": SMALL_FAST_MODEL,
            "ANTHROPIC_CUSTOM_HEADERS": custom_headers(run_id, office_headers, local),
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CORTADOS_WHOLE_SESSION": "1",
            "CORTADOS_DELEGATE_BUDGET": str(delegate_budget),
        },
        **(TOOL_SEARCH_OFF if eager_tools else {}),
    )
    for key in BLANKED:
        env[key] = ""
    return env


def build_env(base: dict, provider_url: str, token: str, delegate_budget: int = 3, run_id: str | None = None,
              office_headers=None, local: str | None = None, eager_tools: bool = False) -> dict:
    # Kept in the process env too, so anything that reads the environment before settings load sees the same values.
    return {**base, **launcher_env(provider_url, token, delegate_budget, run_id, office_headers, local,
                                   eager_tools=eager_tools)}


def settings_content(provider_url: str, token: str, delegate_budget: int = 3, gate: bool = True,
                     run_id: str | None = None, office_headers=None, local: str | None = None,
                     project: str | None = None, eager_tools: bool = False, local_pick: bool = False,
                     watch_prefix: str | None = None, steer_dir: str | None = None) -> dict:
    """project: the session's project folder when known (child tasks: --project-dir); the boundary hook's interpreter
    and script must lie outside it. local_pick / watch_prefix: boundary_hook_entry's (a session launched with a local
    pick). steer_dir: a child task's dir; its steering hook (steer_hook_entries) is installed."""
    # disableAllHooks false: a user settings file cannot switch the boundary hook off while the header says it runs.
    content = {"env": launcher_env(provider_url, token, delegate_budget, run_id, office_headers, local,
                                   eager_tools=eager_tools),
               "enabledPlugins": {PLUGIN_ID: False}, "disableAllHooks": False}
    # always: the header in env promises it (no hook, no edits)
    pre = [boundary_hook_entry(project, local_pick=local_pick, watch_prefix=watch_prefix)]
    if gate:
        # Absolute standalone script: hook cwd need not be this checkout; shell-quote paths with spaces.
        command = "python3 -B " + shlex.quote(str(Path(__file__).resolve().with_name("orchestrator_gate.py")))
        pre.append({"matcher": "*", "hooks": [{"type": "command", "command": command}]})
    content["hooks"] = {"PreToolUse": pre}
    if steer_dir is not None:
        content["hooks"].update(steer_hook_entries(steer_dir, project))
    return content


def _lexical(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def verify_private_dir(path: str, home: str) -> None:
    """The private dir and every ancestor must be a real directory (never a symlink). The dir and ancestors up to and
    including $HOME must be owned by this uid; ancestors above $HOME (or of a dir outside $HOME) by this uid or root.
    The dir itself must be mode 0700. Otherwise another user could substitute a path component."""
    uid = os.getuid()
    home = _lexical(home)
    p = path
    while True:
        try:
            st = os.lstat(p)
        except OSError as exc:
            raise SessionError(f"cannot check settings directory component {p}: {exc.strerror}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise SessionError(f"settings directory component {p} is a symlink or not a directory; refusing to launch")
        inside_home = p == home or p.startswith(home.rstrip(os.sep) + os.sep)
        allowed = (uid,) if inside_home else (uid, 0)
        if st.st_uid not in allowed:
            raise SessionError(f"settings directory component {p} is owned by uid {st.st_uid}; refusing to launch")
        if p == path and stat.S_IMODE(st.st_mode) != 0o700:
            raise SessionError(f"settings directory {p} has mode {stat.S_IMODE(st.st_mode):o}, not 700; "
                               "refusing to launch")
        parent = os.path.dirname(p)
        if parent == p:
            return
        p = parent


def prune_settings(path: str, now: float | None = None) -> None:
    """Remove this uid's regular session-*.json files older than PRUNE_AGE; nothing else is touched."""
    uid, cutoff = os.getuid(), (time.time() if now is None else now) - PRUNE_AGE
    for name in os.listdir(path):
        if not fnmatch.fnmatchcase(name, SETTINGS_PATTERN):
            continue
        f = os.path.join(path, name)
        try:
            st = os.lstat(f)
            if stat.S_ISREG(st.st_mode) and st.st_uid == uid and st.st_mtime < cutoff:
                os.unlink(f)
        except FileNotFoundError:
            pass


def write_settings(directory: str, content: dict, home: str | None = None) -> str:
    """Write this launch's settings to a new unique file (0600, O_EXCL) inside a verified private 0700 directory."""
    d = _lexical(directory)
    os.makedirs(d, mode=0o700, exist_ok=True)
    verify_private_dir(d, os.path.expanduser("~") if home is None else home)
    prune_settings(d)
    fd, name = tempfile.mkstemp(prefix="session-", suffix=".json", dir=d)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or not stat.S_ISREG(st.st_mode):
            raise SessionError(f"settings file {name} is not a private 0600 file of this user; refusing to launch")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(content, f, indent=2, sort_keys=True)
            f.write("\n")
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return name


def managed_settings_files(dirs=MANAGED_SETTINGS_DIRS) -> list:
    files = []
    for d in dirs:
        root = Path(d)
        files.append(root / "managed-settings.json")
        drop = root / "managed-settings.d"
        try:
            files += sorted(f for f in drop.iterdir() if f.name.endswith(".json") and not f.name.startswith("."))
        except (FileNotFoundError, NotADirectoryError):
            pass                                # no drop-in directory
        except OSError as exc:
            # Fail closed: an unlistable policy directory may hold a drop-in that overrides the launcher.
            raise SessionError(f"cannot list managed settings directory {drop}: {exc.strerror or exc}") from None
    return files


def check_managed(keys, dirs=MANAGED_SETTINGS_DIRS) -> None:
    """Managed policy settings outrank `--settings`; refuse to launch if one sets any variable the launcher owns."""
    for f in managed_settings_files(dirs):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError) as exc:
            raise SessionError(f"cannot read managed settings {f}: {exc}") from None
        env = data.get("env") if isinstance(data, dict) else None
        clash = sorted(k for k in (env or {}) if k in keys) if isinstance(env, dict) else []
        if clash:
            raise SessionError(
                f"managed settings {f} set {', '.join(clash)} in env; they override the launcher, so the session "
                "would not run on the cortados provider. Ask the administrator to remove them."
            )


def check_passthrough(extra: list) -> None:
    for a in extra:
        for flag in REFUSED_ARGS:
            if a == flag or a.startswith(flag + "="):
                raise SessionError(f"refusing to pass {flag} to claude: the launcher owns the settings source and the "
                                   "model (use --orchestrator to choose the model)")


def build_argv(model: str, directive: str | None, extra: list, settings_path: str) -> list:
    argv = ["claude", "--model", model, "--settings", settings_path]
    if directive:
        argv += ["--append-system-prompt", directive]
    return argv + list(extra)


def mask(env: dict) -> dict:
    return {k: ("***" if k in SECRET_KEYS and v else v) for k, v in env.items()}


def format_env(env: dict, base: dict) -> str:
    shown = mask({k: v for k, v in env.items() if base.get(k) != v or k in SECRET_KEYS})
    return "\n".join(f"{k}={shown[k]}" for k in sorted(shown))


# ------------------------------------------------------------------------------------ route picker (step 6)
# The owner picks the main loop's route at launch (docs/ORCHESTRATOR-ROUTE-PICKER.md §1.2; E1, owner 2026-11-24: the
# menu on every TTY launch, the first-time Enter is today's behaviour until a pick is saved). The pick rides on the
# main loop's model name, `cortados@<label>` (routing.BAND_MODEL_PREFIX), the provider resolves and enforces it; the
# launcher only offers the list and writes canonical table ids. No prompt without a TTY, under --print-env, or for child
# tasks (they pass --orchestrator-band any and --run-id), nor when any route flag was given.
ROUTE_PICK_FILE = "~/.config/cortados/route-pick.json"
ROUTE_PICK_MAX_BYTES = 4096
ROUTE_OPTIONS_PATH = "/v1/cortados/route-options"   # provider/route_options.PATH (a test keeps them equal)
ROUTE_OPTIONS_TIMEOUT_S = 2.0      # the menu must not hang a launch on a sleeping provider: offline menu after this
ROUTE_PROMPT_TRIES = 8             # invalid answers per question before the launch is refused (never an endless loop)
# Friendly names the launcher resolves itself, before the provider's list. SUB_ALIASES are the provider's own; `sol` is
# the launcher's spelling of the owner's `R3:opus+sol` (design §1.2, §7 step 6): on the provider `sol` is ambiguous
# (gpt-5.6-sol, gpt-6-sol, gpt-6.1-sol), so it is pinned here to the R3 reference Sol. [ESCALATE] E12: whether `sol`
# should follow the newest Sol, or become a provider alias (it would then also pin `sub/sol`).
PICK_ALIASES = {**routing.SUB_ALIASES, "sol": "gpt-6.1-sol"}
PIN_PREFIX = routing.SUB_PREFIX
_PIN_RE = re.compile(r"sub/[a-z0-9][a-z0-9._-]{0,63}")
# Printed at launch for a local pick (E8, step 8): the provider serves it under the orchestrator profile.
LOCAL_PICK_NOTE = ("local orchestrator: plan and delegate only (it reads, plans and delegates with start_task / "
                   "route_task; no edits, no shell but the task watcher, no other tool)")
# The one MCP server a local orchestrator session loads (the delegation tools, effect_boundary.DELEGATION_SERVER), with
# tool search off so its tools are declared up front: the orchestrator profile has no ToolSearch, and the full
# connector catalog would not fit a local model's context (TOOL_SEARCH_OFF's comment). Not when the claude args
# already choose the MCP set (--strict-mcp-config).
DELEGATION_SERVER = "cortados-route"
SUBAGENT_MCP_FILE = Path(__file__).resolve().with_name("subagent_mcp.py")


class RouteOptionsUnavailable(Exception):
    """The provider's route options could not be read (unreachable, refused, unreadable): the menu goes offline."""


def fetch_route_options(url: str, token: str, band: str, timeout: float = ROUTE_OPTIONS_TIMEOUT_S) -> dict:
    """GET <provider>/v1/cortados/route-options?band=<band> with the owner token, within `timeout` seconds."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url.rstrip("/") + ROUTE_OPTIONS_PATH + "?band=" + band,
                                 headers={"authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(1 << 20).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RouteOptionsUnavailable(f"the provider answered HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RouteOptionsUnavailable(f"provider unreachable ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise RouteOptionsUnavailable("the provider sent an unreadable answer")
    return data


def served_ids(view: dict | None) -> list[str] | None:
    """Every served model id an options view names (its band's list and the served models below it), or None."""
    cloud = (view or {}).get("cloud")
    if not isinstance(cloud, dict):
        return None
    ids = [m.get("id") for m in (cloud.get("models") or []) + (cloud.get("below_band") or []) if isinstance(m, dict)]
    return sorted({i for i in ids if isinstance(i, str) and i})


def resolve_pick_name(name: str, served: list[str] | None) -> str:
    """One model name of a pick as a served table id: PICK_ALIASES, then an exact id (release date optional), then
    the friendly words (every word of the name a word of exactly one id, routing.name_tokens). Offline (`served`
    None) a name that is no alias goes as typed, lower-cased: the provider checks it on the first request."""
    key = name.strip().casefold()
    if key in PICK_ALIASES:
        return PICK_ALIASES[key]
    if served is None:
        return key
    exact = [m for m in served if routing.model_key(m) == routing.model_key(key)]
    if len(exact) == 1:
        return exact[0]
    words = routing.name_tokens(key)
    hits = sorted({m for m in served if words and all(w in routing.name_tokens(m) for w in words)})
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SessionError(f"route pick: {name} is ambiguous: it could be {', '.join(hits)}; name one of them")
    raise SessionError(f"route pick: {name} is not a model the provider serves; name one of: {', '.join(served)}")


def local_ids(view: dict | None) -> list[tuple[str, str]] | None:
    """(pin id, tag) of every local model a `local` options view lists, or None when it lists none readable."""
    loc = (view or {}).get("local")
    if not isinstance(loc, dict):
        return None
    got = [(m.get("id"), m.get("tag") or m.get("id")) for m in loc.get("models") or [] if isinstance(m, dict)]
    got = [(i, t) for i, t in got if isinstance(i, str) and i and isinstance(t, str)]
    return got or None


def resolve_local_name(name: str, local: list[tuple[str, str]] | None) -> str:
    """One model name of a local pick as its pin id (the alias, else the tag; case-insensitive on either). Offline
    (`local` None) it goes as typed: the provider checks it against its local pins on the first request."""
    key = name.strip()
    if local is None:
        return key
    hits = sorted({i for i, t in local if key.casefold() in (i.casefold(), t.casefold())})
    if len(hits) == 1:
        return hits[0]
    raise SessionError(f"route pick: {name} is not a local model of the provider; name one of: "
                       f"{', '.join(i for i, _t in local)}")


def canonical_route(text: str, served: list[str] | None = None, local: list[tuple[str, str]] | None = None) -> str:
    """A --route value (the pick grammar of docs/ORCHESTRATOR-ROUTE-PICKER.md §3.2 without `cortados@`) as the
    launcher writes it: `any`, a window label (`R3`), `<window>:*`, `<window>:<ids sorted, joined by +>`, `local` or
    `local:<pin ids in the order typed, duplicates dropped, joined by +>` (step 8: the provider serves a local pick
    under the orchestrator profile and takes the first available model in that order, design §4.5; review localorch3:
    sorting them put the wrong model first. The provider's identity label sorts them itself, routing.Pick.label)."""
    try:
        window, kind, names = routing.parse_pick(text)
    except routing.PickError as exc:
        raise SessionError(f"--route {str(text)[:40]!r}: {exc}") from None
    if kind == "local":
        ids = list(dict.fromkeys(resolve_local_name(n, local) for n in names))
        return check_route("local" + (":" + "+".join(ids) if ids else ""), "--route")
    wlabel = core.band_window_label(*window) if window else None
    if kind == "window":
        return wlabel or "any"
    if kind == "star":
        return f"{wlabel}:*"
    ids = sorted({resolve_pick_name(n, served) for n in names})
    return check_route(f"{wlabel or 'any'}:{'+'.join(ids)}", "--route")


def check_route(value: str, where: str) -> str:
    """`value` (a canonical pick label) if the provider's own parser takes it, else SessionError. Reason: resolving
    friendly names to table ids can lengthen a pick past routing.PIN_NAME_MAX (`R1:fable+…+sol`, 79 typed characters,
    becomes 130), and the menu can select more than routing.PICK_MAX_MODELS models; either would be refused by the
    provider, or crash route_notes, after the pick was saved. Checked with routing.parse_pick, the server's grammar and
    limits, never a copy of them."""
    try:
        routing.parse_pick(value)
    except routing.PickError as exc:
        raise SessionError(f"{where}: {exc}") from None
    return value


def route_model(value: str) -> str:
    """The main loop's --model for a saved or canonical route value: `sub/<id>` (a pin), `cortados` (any), or
    `cortados@<label>`."""
    if value.startswith(PIN_PREFIX):
        return value
    return "cortados" if value == "any" else routing.BAND_MODEL_PREFIX + value


def route_notes(model: str) -> list[str]:
    """The lines the launcher prints at start for a picked route: the route, the exact /model lines that widen it, the
    E11 Haiku warning (accepted by the owner and the reviewer, doc §8) and the degraded-scorer note for a Claude-only
    pick (doc §4.2)."""
    if not model.startswith(routing.BAND_MODEL_PREFIX):
        return ["main loop: " + ("free S1 routing (cortados)" if model == "cortados" else model + " (pinned, no S1)")]
    label = model[len(routing.BAND_MODEL_PREFIX):]
    window, kind, names = routing.parse_pick(label)
    w = core.band_window_label(*window) if window else None
    lines = ["main loop: " + model]
    if kind == "local":
        lines.append(LOCAL_PICK_NOTE)
        lines.append("widen mid-session: " + routing.pick_widen(routing.Pick(None, names, local=True, label=label))
                     .replace("`", ""))
        return lines
    if kind == "window":
        lines.append(f"widen mid-session: /model cortados@{w}:* (only the models clearing {w}'s reference) or "
                     "/model cortados (free routing)")
    else:
        lines.append("widen mid-session: " + routing.pick_widen(routing.Pick(window, names, star=kind == "star",
                                                                               label=label)).replace("`", ""))
    if "haiku" in model.casefold():
        lines.append("warning: this pick names a Haiku-family model, so Claude Code drives the main loop like a Haiku "
                     "client (fewer betas: no effort, inline tools or mid-conversation tool changes)")
    if kind == "set" and names and all(n.startswith("claude-") for n in names):
        lines.append("note: a Claude-only pick has no route while S1 is degraded (a degraded scorer never defaults "
                     "to Claude)")
    return lines


def pick_file(home: str | None = None) -> str:
    return (os.path.join(home, ".config", "cortados", "route-pick.json") if home is not None
            else os.path.expanduser(ROUTE_PICK_FILE))


def _valid_saved(value) -> bool:
    return isinstance(value, str) and (value == "any" or bool(_PIN_RE.fullmatch(value))
                                       or bool(routing.view_pick_label(value)))


def load_pick(home: str | None = None) -> str | None:
    """The last pick saved by an interactive launch, or None (none saved). The file must be a regular 0600 file of
    this user in a directory that passes the settings-dir rules (verify_private_dir); anything else is a
    SessionError the caller reports, never read."""
    path = pick_file(home)
    d = os.path.dirname(path)
    if not os.path.lexists(path):
        return None
    verify_private_dir(d, os.path.expanduser("~") if home is None else home)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise SessionError(f"cannot read the saved route pick {path}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise SessionError(f"the saved route pick {path} is not a private 0600 file of this user; not read")
        raw = os.read(fd, ROUTE_PICK_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= ROUTE_PICK_MAX_BYTES else None
    except ValueError:
        data = None
    value = data.get("pick") if isinstance(data, dict) else None
    if not _valid_saved(value):
        raise SessionError(f"the saved route pick {path} is unreadable; pick again (it is replaced on the next pick)")
    return value


def save_pick(value: str, home: str | None = None, now: float | None = None) -> str:
    """Remember an interactive pick: route-pick.json, 0600, written to a new file and renamed over the old one, in a
    0700 directory checked by the settings-dir rules (no symlinked component, owned by this user)."""
    if not _valid_saved(value):
        raise SessionError(f"route pick {value!r} is not one the launcher writes")
    path = pick_file(home)
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    verify_private_dir(d, os.path.expanduser("~") if home is None else home)
    fd, tmp = tempfile.mkstemp(prefix=".route-pick-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump({"pick": value, "saved_at": round(time.time() if now is None else now, 3)}, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _route_text(r: dict) -> str:
    """One route of the menu: `claude A 62% left`, `codex spent`, `API credit $142 left`, `claude B` (unread)."""
    if r.get("provider") == routing.API_PROVIDER:
        usd = r.get("left_usd")
        return "API credit" + (f" ${usd:.0f} left" if isinstance(usd, (int, float)) else "")
    lane = str(r.get("lane") or r.get("provider") or "?")
    who = lane.split("#", 1)[0] + (" " + str(r["account"]) if r.get("account") else "")
    st = r.get("state")
    if st == "ok" and isinstance(r.get("left_pct"), (int, float)):
        return f"{who} {r['left_pct']:.0f}% left"
    return f"{who} {st}" if st in ("spent", "refusing") else who


class RoutePrompt:
    """The interactive menu (doc §1.2), on injected streams. `fetch(band)` returns an options view or raises
    RouteOptionsUnavailable; `saved` is the last saved pick (or None: Enter keeps today's default)."""

    def __init__(self, inp, out, fetch, saved: str | None, default_model: str):
        self.inp, self.out, self.fetch, self.saved, self.default_model = inp, out, fetch, saved, default_model
        self.offline: str | None = None          # why the provider's list is not available, once known
        self.views: dict[str, dict] = {}

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")
        self.out.flush()

    def ask(self, prompt: str = "> ") -> str:
        self.out.write(prompt)
        self.out.flush()
        line = self.inp.readline()
        if line == "":
            raise SessionError("route prompt closed (end of input): no route picked, nothing launched")
        return line.strip()

    def view(self, band: str) -> dict | None:
        if self.offline is not None:
            return None
        if band not in self.views:
            try:
                self.views[band] = self.fetch(band)
            except RouteOptionsUnavailable as exc:
                self.offline = str(exc)
                self.say(f"CortadOS: {self.offline}; offline menu: the band choices, the last pick and typed model "
                         "names (checked by the provider on the first request)")
                return None
        return self.views[band]

    def run(self) -> str | None:
        """The picked route value (route_model takes it), or None for today's default (first-time Enter)."""
        enter = (f"Enter = last: {self.saved}" if self.saved else f"Enter = today's default: {self.default_model}")
        for _ in range(ROUTE_PROMPT_TRIES):
            self.say(f"CortadOS: route for this session's main loop        [{enter}]")
            self.say("  1 R1   2 R2   3 R3   4 R4   5 local   6 any (S1 routes freely)   7 pin one model")
            got = self.ask().casefold()
            if got == "":
                return self.saved
            if got in ("1", "2", "3", "4"):
                value = self.band("R" + got)
                if value is not None:
                    return value
            elif got == "5":
                value = self.local()
                if value is not None:
                    return value
            elif got == "6":
                return "any"
            elif got == "7":
                value = self.pin()
                if value is not None:
                    return value
            else:
                self.say("choose 1-7, or Enter")
        raise SessionError("route prompt: no route picked after several tries; nothing launched")

    def band(self, band: str) -> str | None:
        v = self.view(band)
        cloud = (v or {}).get("cloud")
        if not isinstance(cloud, dict) or not cloud.get("models"):
            if v is not None:
                self.say(f"{band}: the provider lists no model (" + ", ".join(
                    str(e.get("source")) for e in v.get("errors") or [] if isinstance(e, dict)) + ")")
            return self.band_offline(band)
        models = [m for m in cloud["models"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
        pre = set()
        if self.saved and self.saved.startswith(band + ":") and not self.saved.endswith(":*"):
            pre = set(self.saved.split(":", 1)[1].split("+"))
        chosen = [m["id"] in pre for m in models]
        ref = (cloud.get("reference") or {}).get("general")
        width = max(len(str(m.get("name") or m["id"])) for m in models)
        for _ in range(ROUTE_PROMPT_TRIES):
            self.say(f"{band} models (general index ≥ {ref}; state from cached readings, no vendor CLI opened)")
            for i, m in enumerate(models):
                flags = [f for f, on in (("frontier", m.get("ceiling")),
                                         ("short of " + ", ".join(m.get("short_of") or []), m.get("short_of")),
                                         ("below the face floor", m.get("below_face_floor"))) if on]
                routes = " · ".join(_route_text(r) for r in m.get("routes") or [] if isinstance(r, dict))
                self.say(f"  [{'x' if chosen[i] else ' '}] {i + 1} {str(m.get('name') or m['id']).ljust(width)}  "
                         f"{routes}" + (f"   {', '.join(flags)}" if flags else ""))
            self.say(f"  a = any capable {band} model     b = band only (the {band} window)")
            self.say("  numbers toggle, Enter accepts")
            got = self.ask().casefold()
            if got == "a":
                return f"{band}:*"
            if got == "b":
                return band
            if got == "":
                ids = sorted(m["id"] for m, on in zip(models, chosen) if on)
                if not ids:
                    self.say("select at least one model, or a / b")
                    continue
                try:
                    return check_route(f"{band}:{'+'.join(ids)}", "route pick")
                except SessionError as exc:          # too many models, or too long: say so and ask again
                    self.say(f"{exc}; deselect some models, or a / b")
                    continue
            nums = got.replace(",", " ").split()
            if all(n.isdigit() and 1 <= int(n) <= len(models) for n in nums):
                for n in nums:
                    chosen[int(n) - 1] = not chosen[int(n) - 1]
            else:
                self.say(f"numbers 1-{len(models)}, a, b, or Enter")
        raise SessionError("route prompt: no route picked after several tries; nothing launched")

    def band_offline(self, band: str) -> str | None:
        for _ in range(ROUTE_PROMPT_TRIES):
            self.say(f"{band}: type model names joined by + (opus+gpt-6.1-sol), a = any capable {band} model, "
                     f"b = band only")
            got = self.ask()
            if got.casefold() == "a":
                return f"{band}:*"
            if got.casefold() == "b":
                return band
            if got:
                try:
                    return canonical_route(f"{band}:{got}", None)
                except SessionError as exc:
                    self.say(str(exc))
        raise SessionError("route prompt: no route picked after several tries; nothing launched")

    def local(self) -> str | None:
        """A local pick (step 8): the local models with their node's state, numbers toggle, a = any local model (the
        first available, in config order), Enter accepts the selection, b = back to the menu. Offline, or with no
        local model listed: names typed joined by +, or a."""
        v = self.view("local")
        loc = (v or {}).get("local") or {}
        self.say(LOCAL_PICK_NOTE)
        if loc.get("error"):
            self.say("  " + str(loc["error"]))
        models = [m for m in loc.get("models") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if not models:
            if v is not None:
                self.say("  the provider lists no local model (" + str(loc.get("status") or "no local section") + ")")
            for _ in range(ROUTE_PROMPT_TRIES):
                got = self.ask("local models joined by + (a = any local model, b = back): ")
                if got.casefold() == "a":
                    return "local"
                if got.casefold() in ("b", ""):
                    return None
                try:
                    return canonical_route("local:" + got, None, None)
                except SessionError as exc:
                    self.say(str(exc))
            return None
        # The order models are selected in is the preference order (design §4.5: first available wins): a saved
        # pick's order first, then each number as it is toggled on.
        ids = [m["id"] for m in models]
        order = []
        if self.saved and self.saved.startswith("local:"):
            order = [i for i in dict.fromkeys(self.saved.split(":", 1)[1].split("+")) if i in ids]
        for _ in range(ROUTE_PROMPT_TRIES):
            self.say("local models (state from the nodes' gates)")
            for i, m in enumerate(models):
                self.say(f"  [{'x' if m['id'] in order else ' '}] {i + 1} {m['id']} on "
                         f"{m.get('node_label') or m.get('node')}: {m.get('state')}")
            self.say("  a = any local model (the first available)     b = back")
            self.say("  numbers toggle, Enter accepts")
            got = self.ask().casefold()
            if got == "a":
                return "local"
            if got == "b":
                return None
            if got == "":
                if not order:
                    self.say("select at least one model, or a / b")
                    continue
                try:                                 # review localorch4 P2: the same limits as the cloud menu
                    return check_route("local:" + "+".join(order), "route pick")
                except SessionError as exc:          # too many models, or too long: say so and ask again
                    self.say(f"{exc}; deselect some models, or a / b")
                    continue
            nums = got.replace(",", " ").split()
            if all(n.isdigit() and 1 <= int(n) <= len(models) for n in nums):
                for n in nums:
                    mid = ids[int(n) - 1]
                    if mid in order:
                        order.remove(mid)
                    else:
                        order.append(mid)
            else:
                self.say(f"numbers 1-{len(models)}, a, b, or Enter")
        return None

    def pin(self) -> str | None:
        served = served_ids(self.view("R0"))
        for _ in range(ROUTE_PROMPT_TRIES):
            got = self.ask("model to pin (opus, gpt-6.1-sol, …; Enter = back): ")
            if got == "":
                return None
            try:
                return PIN_PREFIX + resolve_pick_name(got, served)
            except SessionError as exc:
                self.say(str(exc))
        return None


def _tty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def parse(argv: list):
    extra: list = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="python3 -m cortados_router.session", description=__doc__.split("\n")[0])
    ap.add_argument("--orchestrator", default=None, help=f"pin the main loop (default {DEFAULT_ORCHESTRATOR})")
    ap.add_argument("--orchestrator-band", default=None,
                    help=f"band window of a routed main loop: R3, R2-R3 or any (default {DEFAULT_ORCHESTRATOR_BAND} "
                         "with --orchestrator cortados); subagents and side calls are not windowed")
    ap.add_argument("--route", default=None,
                    help="the main loop's route pick: R3:opus+sol, R4:*, R3, any, or last (the last saved pick); "
                         "refused with --orchestrator / --orchestrator-band")
    ap.add_argument("--no-route-prompt", action="store_true", help="never show the route menu (today's default)")
    ap.add_argument("--provider-url")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--no-delegate-directive", action="store_true")
    ap.add_argument("--delegate-budget", type=int, default=3, help="direct work calls per prompt (0 = strict)")
    ap.add_argument("--no-gate", action="store_true", help="disable the delegation enforcement hook")
    ap.add_argument("--print-env", action="store_true")
    ap.add_argument("--settings-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--run-id", default=None, help=argparse.SUPPRESS)   # child tasks: record the session under this id
    # child tasks: `name: value` Office tags (OFFICE_HEADERS) sent on every request of the session
    ap.add_argument("--office-header", action="append", default=[], help=argparse.SUPPRESS)
    # child tasks: x-cortados-local on every request of the session (start_task's `local` option)
    ap.add_argument("--local", default=None, help=argparse.SUPPRESS)   # checked by custom_headers (SessionError)
    # child tasks: the child's project folder; the boundary hook's interpreter and script must lie outside it
    ap.add_argument("--project-dir", default=None, help=argparse.SUPPRESS)
    # child tasks only (child_tasks.start, after its connector budget check): tool search off, TOOL_SEARCH_OFF
    ap.add_argument("--eager-tools", action="store_true", help=argparse.SUPPRESS)
    # child tasks only: the task dir whose steering notes the child's PostToolUse hook delivers (docs/STEER-TASK.md)
    ap.add_argument("--steer-dir", default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv), extra


def pick_route(args, url: str, token: str, *, home: str | None, stdin, out, fetch=None) -> str | None:
    """The main loop's route value from --route or the interactive menu (route_model turns it into --model), or None
    for the --orchestrator / --orchestrator-band path. Saves an interactive pick (save_pick)."""
    explicit = args.orchestrator is not None or args.orchestrator_band is not None   # prepare refused it with --route
    fetch = fetch or (lambda band: fetch_route_options(url, token, band))
    if args.route is not None:
        if args.route.strip().casefold() == "last":
            saved = load_pick(home)
            if saved is None:
                raise SessionError("--route last: no route pick has been saved yet (pick one at an interactive launch)")
            return saved
        served = None
        try:
            _w, kind, names = routing.parse_pick(args.route)
        except routing.PickError:
            kind, names = None, ()               # canonical_route says what is wrong with it
        if kind == "set" and any(n.casefold() not in PICK_ALIASES for n in names):
            try:                                 # the provider's list resolves the friendly names (R0: every model)
                served = served_ids(fetch("R0"))
            except RouteOptionsUnavailable:
                served = None                    # offline: aliases and ids as typed; the provider checks them
        local = None
        if kind == "local" and names:
            try:                                 # the provider's local pins resolve aliases and tags
                local = local_ids(fetch("local"))
            except RouteOptionsUnavailable:
                local = None                     # offline: as typed; the provider checks them
        return canonical_route(args.route, served, local)
    if (explicit or args.no_route_prompt or args.print_env or args.run_id or args.project_dir or args.eager_tools
            or args.steer_dir
            or not (_tty(stdin) and _tty(out))):
        return None
    err_saved = None
    try:
        saved = load_pick(home)
    except SessionError as exc:
        saved, err_saved = None, str(exc)
    if err_saved:
        out.write(f"cortados session: {err_saved}\n")
    value = RoutePrompt(stdin, out, fetch, saved, resolve_model(DEFAULT_ORCHESTRATOR)).run()
    if value is not None and value != saved:
        try:
            save_pick(value, home)
        except (OSError, SessionError) as exc:   # the launch goes on; the pick is not remembered, and that is said
            out.write(f"cortados session: the pick was not saved: {getattr(exc, 'strerror', None) or exc}\n")
    if value is not None:
        out.write("→ claude --model " + route_model(value) + "\n")
    return value


def is_local_route(route: str | None) -> bool:
    """Whether a route value is a local pick (`local`, `local:<ids>`)."""
    return isinstance(route, str) and (route == "local" or route.startswith("local:"))


def delegation_mcp_config(provider_url: str, token_file: str, base_env: dict | None = None) -> dict:
    """The --mcp-config of a local orchestrator session: only DELEGATION_SERVER, this checkout's subagent_mcp.py run
    by this launcher's interpreter (both absolute), told the provider's URL and the token FILE (never the token), and
    the watcher settings of base_env (child_tasks.WATCH_ENV_VARS) set explicitly, so the server's watch_command is
    the one local_watch_prefix hands the boundary hook."""
    from cortados_router import child_tasks          # deferred: child_tasks imports this module
    env = {"CORTADOS_PROVIDER_URL": provider_url,
           "CORTADOS_PROVIDER_TOKEN_FILE": os.path.abspath(os.path.expanduser(token_file))}
    env.update({k: base_env[k] for k in child_tasks.WATCH_ENV_VARS if (base_env or {}).get(k)})
    return {"mcpServers": {DELEGATION_SERVER: {
        "command": os.path.realpath(sys.executable), "args": ["-B", str(SUBAGENT_MCP_FILE)], "env": env}}}


def local_watch_prefix(base_env: dict) -> str:
    """The watcher program of a local orchestrator session (review localorch3 P1): child_tasks.watch_prefix as the
    delegation MCP server this launcher configures will build it (this interpreter's real path, this checkout's
    child_tasks.py, base_env's watcher settings). The boundary hook gets it as --watch-prefix."""
    from cortados_router import child_tasks
    return child_tasks.watch_prefix({k: base_env.get(k) for k in child_tasks.WATCH_ENV_VARS})


def prepare(argv: list, base_env: dict, managed_dirs=MANAGED_SETTINGS_DIRS, home: str | None = None,
            stdin=None, out=None, fetch=None):
    """`stdin` / `out`: the route menu's streams (no menu unless both are TTYs); `fetch(band)`: the route options
    reader (default: GET /v1/cortados/route-options on the provider, ROUTE_OPTIONS_TIMEOUT_S)."""
    args, extra = parse(argv)
    check_passthrough(extra)
    if args.delegate_budget < 0:
        raise SessionError("--delegate-budget must be nonnegative")
    try:
        url = resolve_provider_url({**base_env, **({"CORTADOS_PROVIDER_URL": args.provider_url} if args.provider_url else {})})
    except ProviderURLError as exc:
        raise SessionError(str(exc)) from None
    if args.route is not None and (args.orchestrator is not None or args.orchestrator_band is not None):
        raise SessionError("--route cannot be combined with --orchestrator or --orchestrator-band: it already names "
                           "the main loop's route")
    token = read_token(args.token_file)
    route = pick_route(args, url, token, home=home, stdin=stdin, out=out, fetch=fetch)
    if route is not None:
        model = route_model(route)
        args.route_notes = route_notes(model)
    else:
        model = windowed_model(resolve_model(args.orchestrator or DEFAULT_ORCHESTRATOR), args.orchestrator_band)
        args.route_notes = []
    directive = None
    if not args.no_delegate_directive:
        directive = DIRECTIVE_FILE.read_text(encoding="utf-8").strip()
        if not args.no_gate:
            directive += f"\nThis session allows {args.delegate_budget} direct work calls per prompt before delegation is required."
    office = args.office_header
    if is_local_route(route) and not mcp_filtered(extra):
        # A local orchestrator: only the delegation server, its tools declared up front (DELEGATION_SERVER's comment).
        mcp_path = write_settings(args.settings_dir or SETTINGS_DIR,
                                  delegation_mcp_config(url, args.token_file, base_env), home)
        extra = list(extra) + [STRICT_MCP_FLAG, "--mcp-config", mcp_path]
    eager = bool(args.eager_tools or mcp_filtered(extra))      # a bounded MCP catalog only (TOOL_SEARCH_OFF)
    check_managed(set(launcher_env(url, token, args.delegate_budget, args.run_id, office, args.local,
                                   eager_tools=eager)), managed_dirs)
    settings_path = write_settings(args.settings_dir or SETTINGS_DIR,
                                   settings_content(url, token, args.delegate_budget, not args.no_gate, args.run_id,
                                                    office, args.local, args.project_dir, eager_tools=eager,
                                                    local_pick=is_local_route(route),
                                                    watch_prefix=(local_watch_prefix(base_env)
                                                                  if is_local_route(route) else None),
                                                    steer_dir=args.steer_dir),
                                   home)
    return (args, build_env(base_env, url, token, args.delegate_budget, args.run_id, office, args.local,
                            eager_tools=eager),
            build_argv(model, directive, extra, settings_path))


def main(argv=None, base_env=None, out=sys.stdout, err=sys.stderr, managed_dirs=MANAGED_SETTINGS_DIRS,
         home=None, stdin=None, fetch=None) -> int:
    base = dict(os.environ if base_env is None else base_env)
    try:
        args, env, cmd = prepare(list(sys.argv[1:] if argv is None else argv), base, managed_dirs, home,
                                 stdin=sys.stdin if stdin is None else stdin, out=out, fetch=fetch)
    except SessionError as exc:
        print(f"cortados session: {exc}", file=err)
        return 2
    if args.print_env:
        print(format_env(env, base), file=out)
        print("argv: " + " ".join(repr(a) if " " in a else a for a in cmd), file=out)
        return 0
    for line in args.route_notes:
        print(f"cortados session: {line}", file=err)
    os.execvpe(cmd[0], cmd, env)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
