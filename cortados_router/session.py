"""Run a whole Claude Code session on the cortados provider.

    python3 -m cortados_router.session [--orchestrator fable|opus|sonnet|<model id>|cortados]
        [--provider-url URL] [--token-file PATH] [--no-delegate-directive] [--print-env] [-- <claude args>]

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


class SessionError(Exception):
    pass


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
# The tools the boundary hook checks: the file writers (path containment on this machine) and Bash (profile).
BOUNDARY_HOOK_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"


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


def boundary_hook_entry(project: str | None = None, python: str | None = None) -> dict:
    """The PreToolUse entry running boundary_hook.py: `<absolute python3> -I -S -B <absolute script>` (shell-quoted),
    from / inside the shell guard (guarded_hook_command). No PATH lookup, isolated mode, no site import, fail closed for
    a marked call when the interpreter cannot start (docs/BOUNDARY-HOOK.md "Isolation"). A hook script inside
    the project folder is refused. Timeout 30 s, well past the script's own 5 s provider timeout, so the script always
    answers (and fails closed) itself."""
    if _inside_project(str(BOUNDARY_HOOK_FILE), project):
        raise SessionError("the boundary hook script lies inside the project folder; run the launcher from a router "
                           "checkout outside it")
    python = python or boundary_hook_python(project)
    command = guarded_hook_command(python, shlex.quote(str(BOUNDARY_HOOK_FILE)))
    return {"matcher": BOUNDARY_HOOK_MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 30}]}


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
                     project: str | None = None, eager_tools: bool = False) -> dict:
    """project: the session's project folder when known (child tasks: --project-dir); the boundary hook's interpreter
    and script must lie outside it."""
    # disableAllHooks false: a user settings file cannot switch the boundary hook off while the header says it runs.
    content = {"env": launcher_env(provider_url, token, delegate_budget, run_id, office_headers, local,
                                   eager_tools=eager_tools),
               "enabledPlugins": {PLUGIN_ID: False}, "disableAllHooks": False}
    pre = [boundary_hook_entry(project)]         # always: the header in env promises it (no hook, no edits)
    if gate:
        # Absolute standalone script: hook cwd need not be this checkout; shell-quote paths with spaces.
        command = "python3 -B " + shlex.quote(str(Path(__file__).resolve().with_name("orchestrator_gate.py")))
        pre.append({"matcher": "*", "hooks": [{"type": "command", "command": command}]})
    content["hooks"] = {"PreToolUse": pre}
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


def parse(argv: list):
    extra: list = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="python3 -m cortados_router.session", description=__doc__.split("\n")[0])
    ap.add_argument("--orchestrator", default=DEFAULT_ORCHESTRATOR)
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
    return ap.parse_args(argv), extra


def prepare(argv: list, base_env: dict, managed_dirs=MANAGED_SETTINGS_DIRS, home: str | None = None):
    args, extra = parse(argv)
    check_passthrough(extra)
    if args.delegate_budget < 0:
        raise SessionError("--delegate-budget must be nonnegative")
    try:
        url = resolve_provider_url({**base_env, **({"CORTADOS_PROVIDER_URL": args.provider_url} if args.provider_url else {})})
    except ProviderURLError as exc:
        raise SessionError(str(exc)) from None
    model = resolve_model(args.orchestrator)
    token = read_token(args.token_file)
    directive = None
    if not args.no_delegate_directive:
        directive = DIRECTIVE_FILE.read_text(encoding="utf-8").strip()
        if not args.no_gate:
            directive += f"\nThis session allows {args.delegate_budget} direct work calls per prompt before delegation is required."
    office = args.office_header
    eager = bool(args.eager_tools or mcp_filtered(extra))      # a bounded MCP catalog only (TOOL_SEARCH_OFF)
    check_managed(set(launcher_env(url, token, args.delegate_budget, args.run_id, office, args.local,
                                   eager_tools=eager)), managed_dirs)
    settings_path = write_settings(args.settings_dir or SETTINGS_DIR,
                                   settings_content(url, token, args.delegate_budget, not args.no_gate, args.run_id,
                                                    office, args.local, args.project_dir, eager_tools=eager),
                                   home)
    return (args, build_env(base_env, url, token, args.delegate_budget, args.run_id, office, args.local,
                            eager_tools=eager),
            build_argv(model, directive, extra, settings_path))


def main(argv=None, base_env=None, out=sys.stdout, err=sys.stderr, managed_dirs=MANAGED_SETTINGS_DIRS,
         home=None) -> int:
    base = dict(os.environ if base_env is None else base_env)
    try:
        args, env, cmd = prepare(list(sys.argv[1:] if argv is None else argv), base, managed_dirs, home)
    except SessionError as exc:
        print(f"cortados session: {exc}", file=err)
        return 2
    if args.print_env:
        print(format_env(env, base), file=out)
        print("argv: " + " ".join(repr(a) if " " in a else a for a in cmd), file=out)
        return 0
    os.execvpe(cmd[0], cmd, env)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
