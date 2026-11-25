"""The effect boundary of AUTOMATICALLY local-routed turns (docs/LOCAL-RESOURCES.md "Effect boundary").

Owner policy (Forest): a request may reach a local uncensored model automatically (R0 auto_route, the sensitive flag,
prefer, keyword, the refusal fallback, and the published index route) only when the work has no effect outside
the Pro. local_admission scores the request; scoring alone cannot stop a model from escalating beyond what was asked
("draft it, do not send" answered with a send_email call). So every automatic-local turn also passes this boundary:

  1. tools_for_node  the declared `tools` are cut to the turn's profile (PROFILE_TOOLS) before the request reaches the
                     node, so the model cannot even name a tool that reaches outside the machine. Allowlist, not
                     denylist: a tool not listed (any `mcp__*` connector, WebFetch, WebSearch, Task/Agent, an unknown
                     name) is dropped.
  2. blocked_call    every tool call the local model emits is inspected before the reply reaches the client: a tool
                     not in the profile (defence in depth), a Bash command matching OUTWARD_BASH_RULES /
                     OUTWARD_BASH_TEXT_RULES, or a file write outside the conversation's project folder or onto a
                     PROTECTED_PATHS entry blocks the whole reply (local_lane.serve / complete; server.py answers it).

Profiles by WHY the turn is local (owner decision, Forest 2026-11-14, "split by reason"; `prefer` moved to cost on the
review of 0961c80): the route carries its bind reason (routing._moved's `bind_reason`, kept by every continuation of the
conversation), BIND_PROFILES maps it:
  sensitive  the sensitive flag, the keyword list, the refusal fallback: no shell at all (the Bash family is not
             offered and an emitted Bash call is blocked), file writes inside the project only, and every
             PROTECTED_PATHS entry refused (package.json / pyproject.toml / Makefile included).
  cost       `prefer` (x-cortados-local: prefer asks for local because it is cheaper, not because the work is
             sensitive), R0 auto_route and the published-score index route: Bash stays, inspected against the pattern
             lists, which are BEST-EFFORT (see the honest limit below); the same path rules, with package.json /
             pyproject.toml / Makefile allowed (normal coding targets).
An unknown or missing reason gets the strictest profile (sensitive): fail closed.

The local orchestrator (docs/ORCHESTRATOR-ROUTE-PICKER.md §5, step 8; E8, owner 2026-11-24): a `local[:<models>]` route
pick runs the main loop of a Claude Code session on a local model, opt-in, under its own profiles. It is not an
automatic move but it is fenced like one (local_lane.serve: `fenced`):
  orchestrator            plan and delegate only: read (Read, Glob, Grep, LS, NotebookRead), plan (TodoWrite,
                          ExitPlanMode, AskUserQuestion), delegate through exactly the cortados-route MCP tools
                          (DELEGATION_TOOLS), and one shell shape: start_task's watch_command (WATCH_COMMAND_RE), with
                          BashOutput / KillShell for that background watcher. No file write, no other shell command, no
                          Agent / Task, no web, no other MCP tool, nothing unknown. start_task may not ask for Chrome
                          or connectors (DELEGATE_REFUSED_INPUTS: [ESCALATE] E13 in the design doc, the stricter option).
  orchestrator-sensitive  the same, for a turn a sensitivity detector marks (E5: the sensitive rule wins): no shell and
                          no delegation (a delegated task leaves the machine), so read and plan only.
Without the client hook (HOOK_HEADER) an orchestrator turn also loses the shell (the watcher): the hook is the client's
half of the check, and no shell is the stricter choice.

An explicit `sub/<local model>` pin is the client's own choice: it never passes here (local_lane.serve's auto_routed
is False for a pin, and a pin carries no orchestrator bind reason).

The project folder (project_dir) is read from the request's `system` prompt only: Claude Code states its working
directory there ("Primary working directory: <path>", older versions "Working directory: <path>"). Only `system` is
read, never messages or tool results, so neither a fetched page nor the model can move the folder. No such line, two
different ones, a relative path, `..`, or a folder that is a filesystem root or a home directory: unknown, and every
file write of the turn is blocked (fail closed).

The client-side boundary hook (docs/BOUNDARY-HOOK.md). The tools run on the CLIENT's machine, which is often not the
provider's, so the provider's realpath (blocked_write) cannot see a symlink that exists only there. Therefore:
  1. every tool call a boundary turn lets through gets an id minted here (MARK_PREFIX + random, Boundary.mark) and a
     LEDGER record: conversation, tool, profile and the project folder the provider assumed. The ledger is bounded
     (LEDGER_MAX, LEDGER_MAX_PER_CONVERSATION) and every record expires (LEDGER_TTL_S).
  2. the client's PreToolUse hook (cortados_router/boundary_hook.py) asks POST /v1/cortados/boundary/check about the
     tool_use id (check_answer), resolves the target with realpath ON THE CLIENT, and denies a write whose real path
     leaves the real project folder or lands on a protected path, and Bash on a sensitive profile. An id with
     MARK_PREFIX fails closed on any error (provider unreachable, record expired or unknown); any other id is not the
     boundary's and passes.
  3. no hook, no edits: a client that runs the hook says so with HOOK_HEADER on every request. A SENSITIVE turn
     without it is read-only: the file-writing tools are not offered and an emitted one is blocked ("write-no-hook").
     A COST turn without it keeps its edits under the provider-side path rules alone (the symlink limit above).
blocked_write's provider-side realpath check stays as defence in depth.

Honest limit: a pattern list cannot recognise every outward shell command (a script that sends, an alias defined
earlier, a variable holding the command name, obfuscation, a Makefile target). On cost routes the boundary removes the
obvious and the declared routes out; Claude Code's own permission prompts remain the final guard for what Bash runs.
The provider's path rules read the path's text and what the provider's own filesystem shows (realpath); the client's
hook is what sees the client's symlinks. A client that sends HOOK_HEADER but whose hook does not run (python3 missing,
hooks disabled by a managed policy) is not detected: the header is the client's word.
Stdlib only."""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("cortados.provider.effect_boundary")

# Profiles. A profile names the tools a turn may declare and call, and how strict the path rules are.
SENSITIVE = "sensitive"
COST = "cost"
ORCHESTRATOR = "orchestrator"                       # a local route pick's main loop: plan and delegate only
ORCHESTRATOR_SENSITIVE = "orchestrator-sensitive"   # the same turn marked sensitive (E5): read and plan only
ORCHESTRATOR_PROFILES = frozenset({ORCHESTRATOR, ORCHESTRATOR_SENSITIVE})
# The bind reasons of a local route pick (routing.choose_local_pick). Not an automatic move, so not in the comment
# above, but fenced the same way (local_lane.serve `fenced`).
PICK_LOCAL_REASON = "route_pick_local"
PICK_LOCAL_SENSITIVE_REASON = "route_pick_local_sensitive"
# WHICH automatic rule bound the conversation to a local lane (routing._moved's `bind_reason`) -> its profile.
BIND_PROFILES: dict[str, str] = {
    "sensitive_flag": SENSITIVE,     # x-cortados-local: sensitive
    "keyword": SENSITIVE,            # the node's keyword list matched the person's words
    "refusal": SENSITIVE,            # a cloud model refused the opening turn
    "prefer": COST,                  # x-cortados-local: prefer: local because it is cheaper (owner decision 2026-11-14)
    "auto_route": COST,              # R0 auto_route: local because it is cheaper, not because it is sensitive
    "index": COST,                   # the published-score index route (routing._move_to_local)
    PICK_LOCAL_REASON: ORCHESTRATOR,                       # a local route pick (E8): the owner's opt-in
    PICK_LOCAL_SENSITIVE_REASON: ORCHESTRATOR_SENSITIVE,   # ... whose turn a sensitivity detector marks (E5)
}
ORCHESTRATOR_REASONS = frozenset({PICK_LOCAL_REASON, PICK_LOCAL_SENSITIVE_REASON})


def fenced(auto_routed: bool, reason: Any) -> bool:
    """Whether a local turn passes the boundary: every automatic move, and every turn of a local route pick (its bind
    reason is an orchestrator one). A plain `sub/` pin does not."""
    return bool(auto_routed) or (isinstance(reason, str) and reason in ORCHESTRATOR_REASONS)


def profile_for(reason: Any) -> str:
    """The profile of a bind reason; an unknown or missing reason is SENSITIVE (the strictest: fail closed)."""
    return BIND_PROFILES.get(reason, SENSITIVE) if isinstance(reason, str) else SENSITIVE


# Tools an automatic-local turn may declare and call. Each acts on this machine only:
#   Read, Glob, Grep, LS, NotebookRead          read local files
#   Edit, MultiEdit, Write, NotebookEdit        write local files: inside the project folder only (PROTECTED_PATHS)
#   TodoWrite, ExitPlanMode                     the client's own task list / plan state, no I/O outside it
#   Bash, BashOutput, KillShell, KillBash       COST profile only: a local shell and its background jobs; Bash's
#                                               command is inspected against the pattern lists (KillBash: the older
#                                               name of KillShell). A shell can do anything: a SENSITIVE turn has none.
# Deliberately NOT listed (so dropped): WebFetch and WebSearch (network), every `mcp__*` tool (connectors: mail, chat,
# drive, calendar, payments, browsers), Task / Agent (a subagent is routed afresh and may land on a cloud model with
# every tool), Skill / SlashCommand (run arbitrary instructions), and any name this list does not know.
BASH_FAMILY = frozenset({"Bash", "BashOutput", "KillShell", "KillBash"})
LOCAL_TOOL_ALLOWLIST = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead",
    "Edit", "MultiEdit", "Write", "NotebookEdit",
    "TodoWrite", "ExitPlanMode",
}) | BASH_FAMILY
# The local orchestrator (module docstring). Exact names: Claude Code names an MCP tool mcp__<server>__<tool>, and the
# router's server is `cortados-route` (.mcp.json, README "Delegating whole tasks"); any other server, or another
# spelling, is not a delegation tool and is dropped.
DELEGATION_SERVER = "cortados-route"
DELEGATION_TOOLS = frozenset("mcp__%s__%s" % (DELEGATION_SERVER, t) for t in (
    "start_task", "route_task", "task_status", "wait_task", "list_tasks", "cancel_task"))
START_TASK_TOOL = "mcp__%s__start_task" % DELEGATION_SERVER
# start_task inputs a local orchestrator may not set (truthy): `chrome` drives the owner's real signed-in browser and
# `connectors` loads mail / chat / drive servers into the child. [ESCALATE] E13 (doc §5): §5 leaves "may a local
# orchestrator start tasks with outward effects" to the owner; refusing these two is the stricter option until he says.
DELEGATE_REFUSED_INPUTS = ("chrome", "connectors")
ORCHESTRATOR_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead",
                                     "TodoWrite", "ExitPlanMode", "AskUserQuestion"})
# The watcher's shell: Bash (only WATCH_COMMAND_RE), and BashOutput / KillShell / KillBash for its background job.
ORCHESTRATOR_SHELL = BASH_FAMILY
PROFILE_TOOLS = {SENSITIVE: LOCAL_TOOL_ALLOWLIST - BASH_FAMILY, COST: LOCAL_TOOL_ALLOWLIST,
                 ORCHESTRATOR: ORCHESTRATOR_READ_TOOLS | DELEGATION_TOOLS | ORCHESTRATOR_SHELL,
                 ORCHESTRATOR_SENSITIVE: ORCHESTRATOR_READ_TOOLS}

# child_tasks.watch_command's shape (the background watcher AGENTS.md has the orchestrator run after every start_task),
# the same shape as the routed-subagents mod's TASK_WATCH_RE (hooks/register.ts) and the same pattern as
# boundary_hook.WATCH_COMMAND_RE (tests/test_local_orchestrator.py keeps the two equal): [env] [CORTADOS_TASKS_DIR=… CORTADOS_EXEC_HOSTS_FILE=…] <a python3> [-B]
# <…/cortados_router/child_tasks.py> --watch <task id>… [--timeout|--interval|--stall <seconds>]…, optionally
# `; echo "exit $?"`. Spaces and tabs only: no newline, other command, pipe, redirect, substitution or variable. The
# only shell a local orchestrator has; it only polls task_status.
_W_PLAIN = r"[A-Za-z0-9_./~+@%:,-]"
_W_TASK_ID = r"t[0-9]{14}-[0-9a-f]{8}"
_W_FLAG = r"--(?:timeout|interval|stall)[ \t]+[0-9]+(?:\.[0-9]+)?"


def _w_path_to(tail: str) -> str:
    return r"(?:'(?:[^']*/)?%s'|(?:%s*/)?%s)" % (tail, _W_PLAIN, tail)


WATCH_COMMAND_RE = re.compile(
    r"[ \t]*(?:env[ \t]+)?"
    + r"(?:CORTADOS_(?:TASKS_DIR|EXEC_HOSTS_FILE)=(?:'[^']*'|%s+)[ \t]+)*" % _W_PLAIN
    + _w_path_to(r"python3(?:\.[0-9]+)?") + r"(?:[ \t]+-B)?[ \t]+"
    + _w_path_to(r"cortados_router/child_tasks\.py") + r"[ \t]+--watch"
    + r"(?:[ \t]+%s)*[ \t]+%s(?:[ \t]+(?:%s|%s))*" % (_W_FLAG, _W_TASK_ID, _W_TASK_ID, _W_FLAG)
    + r"""(?:[ \t]*;[ \t]*echo[ \t]+(?:"exit \$\?"|exit[ \t]+\$\?))?[ \t]*""")


def is_watch_command(command: Any) -> bool:
    """Whether a Bash command is exactly start_task's watch_command shape (WATCH_COMMAND_RE, fullmatch)."""
    return isinstance(command, str) and bool(WATCH_COMMAND_RE.fullmatch(command))
# The allowlisted tools whose `command` input is a shell command line to inspect.
SHELL_TOOLS = frozenset({"Bash"})
# The allowlisted tools that write a file, and the input key naming it.
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

# A local orchestrator's blocked reply (a tool off its profile, any shell command but the watcher, a refused start_task
# input): it plans and delegates only.
ORCHESTRATOR_BLOCKED_MESSAGE = ("cortados: this session's main loop is a local orchestrator (route pick local, the "
                                "orchestrator profile): it reads, plans and delegates only (start_task / route_task), "
                                "so the action it proposed was not sent to your tools. Ask it to delegate the work, or "
                                "switch the main loop to a cloud pick (/model cortados@R3).")
BLOCKED_MESSAGE = ("cortados: the local model proposed an action with an outward effect; run this turn on a cloud "
                   "model (start a new conversation, which is routed again, or pin a cloud model with sub/<model>). "
                   "Nothing was sent to your tools.")

# A Bash command line is cut into simple commands at every place a new command word can start: a separator
# (; & | && || newline), a subshell or command substitution ( ( $( <( ` ), a brace group, and a quote (so
# `bash -c "ssh host"` and `eval 'curl -d ..'` are read too, at the price of reading a quoted message as a command).
# A pipe into a sender (`... | sendmail x`) is therefore a simple command of its own.
_SPLIT = re.compile(r"[;&|(){}\n`\"']|\$\(|<\(")
# Words skipped before the command word: shell keywords and wrappers that run their argument as a command, with their
# flags (and one flag argument: sudo -u x, xargs -n 1), numbers (timeout 30) and VAR=value assignments.
_WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "time", "command", "builtin", "exec", "nice", "xargs", "stdbuf",
                       "caffeinate", "timeout", "then", "do", "else", "eval", "if", "while", "until", "!"})
_ASSIGN = re.compile(r"[A-Za-z_]\w*=")
_NUMBER = re.compile(r"\d+[smhd]?")


def _cmd(names: str, cond: str | None = None, unless: str | None = None) -> tuple:
    """A rule: a command word in `names` (space separated, compared case-insensitively: macOS file names are), and
    optionally `cond` searched in the rest of the same simple command (its arguments, each preceded by a space), and
    `unless` not found there."""
    return frozenset(names.split()), re.compile(cond) if cond else None, re.compile(unless) if unless else None


# (rule id, (command names, condition on the arguments)) pairs, checked against every simple command of a Bash call's
# `command`. Conservative: a false positive costs one refused turn (the person reruns it on a cloud model); a miss
# costs an outward effect. BEST-EFFORT (the honest limit in the module docstring): it is the cost profile's only shell
# guard besides Claude Code's own permission prompts, which remain the last guard.
OUTWARD_BASH_RULES: tuple[tuple[str, tuple], ...] = (
    # curl with a method or a body: -X/--request (any method), -d/--data*, -F/--form*, -T/--upload-file, --json,
    # including a combined short flag (-sSd). A plain GET is not matched (see the honest limit: a URL can carry data).
    ("curl-upload", _cmd("curl", r"\s(?:-[A-Za-z]*[dFTX]|--(?:data[\w-]*|form[\w-]*|upload-file|json|request)\b)")),
    ("wget-upload", _cmd("wget", r"\s--(?:post-data|post-file|body-data|body-file|method)\b")),
    ("ssh", _cmd("ssh scp sftp mosh autossh")),
    ("netcat", _cmd("nc ncat netcat socat telnet ftp lftp")),
    # rsync to or from a host: host:path, host::module or rsync://; a local-to-local rsync passes.
    ("rsync-remote", _cmd("rsync", r":")),
    ("git-push", _cmd("git", r"\s(?:push|send-email|request-pull)\b")),
    # git aliases and config hide a push or a hook (`git config alias.p push`, `git -c alias.p=push p`,
    # `git config core.hooksPath x`): any config write and any -c is refused; reading config (--get, --list) passes.
    ("git-config", _cmd("git", r"\s(?:-c|config)(?:\s|$)",
                        unless=r"\s(?:--get|--get-all|--get-regexp|--list|-l)(?:\s|$)")),
    # gh subcommands that create, change or delete something on GitHub (gh pr view / gh issue list pass).
    ("gh-write", _cmd("gh", r"\s(?:create|edit|merge|comment|close|delete|release|reopen|review|ready|transfer|lock"
                            r"|unlock|fork|rename|archive|unarchive|run|rerun|cancel|set|upload|sync|enable|disable"
                            r"|pin|unpin|develop|add|remove|login|logout)\b")),
    ("gh-api-write", _cmd("gh", r"\sapi\b.*\s(?:-X|--method|-f|-F|--field|--raw-field|--input)(?:\s|=|$)")),
    ("publish", _cmd("npm pnpm yarn bun cargo poetry flit hatch uv pip pip3", r"\s(?:publish|unpublish|upload)\b")),
    ("publish", _cmd("twine", r"\supload\b")),
    ("publish", _cmd("python python3", r"\s-m\s+twine\s+upload\b")),
    ("publish", _cmd("gem docker podman helm", r"\spush\b")),
    # AppleScript drives Mail, Messages and every scriptable app (and `do shell script`): never from a local turn.
    ("osascript", _cmd("osascript")),
    ("open-url-scheme", _cmd("open xdg-open", r"(?i:\b(?:mailto|sms|imessage|facetime|tel|message):)")),
    ("mail", _cmd("sendmail mail mailx mutt msmtp ssmtp")),
    # Work scheduled for later runs outside this turn and outside any later permission prompt.
    ("persistence", _cmd("launchctl crontab at batch")),
    # A symlink turns a write inside the project into a write outside it (the path rules see the link's name).
    ("symlink", _cmd("ln", r"\s(?:-[A-Za-z]*s[A-Za-z]*|--symbolic)(?:\s|$)")),
)
_RULE_NAMES = frozenset().union(*(rule[1][0] for rule in OUTWARD_BASH_RULES))
# A shell or interpreter reading its program from a pipe (`... | base64 -d | sh`, `curl ... | python3`): what runs is
# not on the command line, so it cannot be judged.
_PIPE_RUNNERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "node", "perl", "ruby",
                           "php", "osascript", "source", "."})
# Interpreters whose inline program (or script name) appears on the command line, and the words that mean it talks
# to the network or spawns a sender (python -c "import socket", node -e "require('https')", perl -MIO::Socket).
_INTERPRETERS = frozenset({"python", "python2", "python3", "node", "deno", "bun", "perl", "ruby", "php"})
_NETWORK_WORDS = frozenset({"socket", "urllib", "urllib2", "urllib3", "requests", "httpx", "aiohttp", "smtplib",
                            "ftplib", "telnetlib", "http", "https", "fetch", "net", "dgram", "tls", "websocket",
                            "child_process", "lwp", "smtp", "mail", "sendmail", "paramiko", "webbrowser"})
_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Whole-command text rules (literal alternations: linear). Places that act later, outside this turn and outside the
# project: git hooks, launchd jobs, ssh config and keys, shell startup files, crontab.
OUTWARD_BASH_TEXT_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("protected-path", re.compile(r"(?i)\.git/(?:hooks|config)|launchagents|launchdaemons|\.ssh(?:/|\b)|\.zshrc|"
                                  r"\.zshenv|\.zprofile|\.zlogin|\.bashrc|\.bash_profile|\.bash_login|(?:^|[\s/~])"
                                  r"\.profile\b|crontab")),
    # eval of a command substitution, or `source <(...)`: the command is computed at run time (base64, a variable).
    ("computed-command", re.compile(r"\beval\b[^\n;|&]{0,200}(?:\$\(|`)|(?:\bsource|(?:^|[\s;&|])\.)\s+<\(")),
)


def _segments(command: str):
    """(separator before it, simple command text) for each piece of `command` cut at _SPLIT. Linear."""
    parts = _SPLIT_KEEP.split(command)
    yield "", parts[0]
    for i in range(1, len(parts) - 1, 2):
        yield parts[i], parts[i + 1]


_SPLIT_KEEP = re.compile(r"([;&|(){}\n`\"']|\$\(|<\()")


def _simple_commands(command: str):
    """(separator before it, command word, arguments) of each simple command: the word lower-cased without its
    directory (/usr/sbin/sendmail is sendmail), the arguments as one string, each preceded by a space. Linear."""
    for sep, segment in _segments(command):
        words = segment.split()
        i, wrapped = 0, False
        while i < len(words):
            w = words[i]
            if w in _WRAPPERS:
                wrapped = True
            elif _ASSIGN.match(w) or wrapped and _NUMBER.fullmatch(w):
                pass
            elif wrapped and w.startswith("-"):
                nxt = words[i + 1] if i + 1 < len(words) else ""
                if nxt and not nxt.startswith("-") and nxt.rsplit("/", 1)[-1].lower() not in _RULE_NAMES \
                        and nxt not in _WRAPPERS:
                    i += 1                              # the flag's argument (sudo -u x), unless it is a command
            else:
                break
            i += 1
        if i < len(words):
            yield sep, words[i].rsplit("/", 1)[-1].lower(), "".join(" " + w for w in words[i + 1:])


def tools_for_node(body: dict[str, Any], profile: str = SENSITIVE) -> tuple[dict[str, Any], list[str]]:
    """The request as an automatic-local node may see it under `profile`: `tools` cut to PROFILE_TOOLS[profile], and
    a `tool_choice` that names a dropped tool (or requires a tool when none is left) removed. Returns (body, dropped
    tool names). The caller's dict is not changed. A body without `tools` is returned as is."""
    allowed = PROFILE_TOOLS.get(profile, PROFILE_TOOLS[SENSITIVE])
    tools = body.get("tools")
    if not isinstance(tools, list):
        return body, []
    kept, dropped = [], []
    for t in tools:
        name = t.get("name") if isinstance(t, dict) else None
        # A client tool only (no `type`, or "custom"): a typed server tool (web_search_*, bash_*, computer_*) runs
        # wherever its host runs it, whatever its name, so it is dropped even if the name matches.
        if isinstance(name, str) and name in allowed and t.get("type") in (None, "custom"):
            kept.append(t)
        else:
            dropped.append(name if isinstance(name, str) else "<unnamed>")
    if not dropped:
        return body, []
    out = dict(body, tools=kept)
    choice = out.get("tool_choice")
    if isinstance(choice, dict) and (choice.get("type") == "tool" and choice.get("name") not in
                                     {t["name"] for t in kept} or choice.get("type") == "any" and not kept):
        out.pop("tool_choice")
    return out, dropped


def blocked_bash(command: Any) -> str | None:
    """The rule id an outward shell command matches, or None. A command that is not a string is blocked (fail closed).
    Best-effort (module docstring): Claude Code's permission prompts remain the last guard."""
    if not isinstance(command, str):
        return "bash-malformed"
    interpreter = False
    for sep, word, args in _simple_commands(command):
        if sep == "|" and word in _PIPE_RUNNERS:
            return "pipe-to-shell"
        interpreter = interpreter or word in _INTERPRETERS or word.startswith("python")
        for rule, (names, cond, unless) in OUTWARD_BASH_RULES:
            if word in names and (cond is None or cond.search(args)) and not (unless and unless.search(args)):
                return rule
    for rule, pattern in OUTWARD_BASH_TEXT_RULES:
        if pattern.search(command):
            return rule
    if interpreter and not _NETWORK_WORDS.isdisjoint(w.lower() for w in _WORDS.findall(command)):
        return "interpreter-network"
    return None


# ------------------------------------------------------------------------------------------------- file writes
# Paths a file write of an automatic-local turn may not touch even inside the project folder. Reason: each of these is
# read and ACTED ON later by something other than this turn (git, CI, a hook manager, a build tool, direnv, Claude
# Code itself, the shell, launchd, ssh, cron), so a local model could plant an effect outside the machine (a push in a
# hook, a curl in a CI step, a LaunchAgent) that no tool call of this turn shows. Matched case-insensitively (macOS
# file names are), on the path relative to the project folder.
#   any component        .git (hooks, config: also a submodule's or a worktree's), .husky (git hooks), .claude
#                        (settings, hooks, skills), .ssh (keys, config: a home-directory project)
#   any component pair   .github/workflows (CI), Library/LaunchAgents and Library/LaunchDaemons (launchd jobs)
#   any file name        .gitlab-ci.yml (CI), .pre-commit-config.yaml (hooks), .envrc (direnv runs it on cd),
#                        .mcp.json (MCP servers Claude Code starts), setup.py / setup.cfg (run by pip install), shell
#                        startup files, crontab
#   build files          package.json (npm scripts), pyproject.toml (build backends, scripts), Makefile: refused on
#                        SENSITIVE routes, ALLOWED on COST routes (normal coding targets, owner decision 2026-11-14)
#   the project root     any dotfile or dot-directory directly in the project folder (.envrc, .npmrc, .gitattributes
#                        filters, .tool-versions, ...: read by tools on entry)
# Outside the project folder every write is refused anyway, which covers ~/Library/LaunchAgents, ~/.ssh, ~/.zshrc and
# the system crontab; the names above also catch them when the project folder is an ancestor.
PROTECTED_PATHS: dict[str, frozenset] = {
    "components": frozenset({".git", ".husky", ".claude", ".ssh"}),
    "component_pairs": frozenset({(".github", "workflows"), ("library", "launchagents"), ("library", "launchdaemons")}),
    "names": frozenset({".gitlab-ci.yml", ".pre-commit-config.yaml", ".envrc", ".mcp.json", "setup.py", "setup.cfg",
                        ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".bashrc", ".bash_profile",
                        ".bash_login", ".bash_logout", ".profile", ".kshrc", ".cshrc", ".tcshrc", ".inputrc",
                        "config.fish", "crontab"}),
    "build_files": frozenset({"package.json", "pyproject.toml", "makefile", "gnumakefile"}),
}
# Folders that are never a project folder: a write "inside" them could reach every dotfile of a home directory.
_NOT_PROJECTS = re.compile(r"/|/(?:Users|home)(?:/[^/]+)?|/root|/var/root|/private(?:/var(?:/root)?)?|/tmp"
                           r"|/private/tmp")


def _protected(rel_parts: list[str], profile: str) -> bool:
    parts = [p.casefold() for p in rel_parts]
    if not parts or parts[0].startswith("."):
        return True
    if any(p in PROTECTED_PATHS["components"] for p in parts):
        return True
    if any(pair in PROTECTED_PATHS["component_pairs"] for pair in zip(parts, parts[1:])):
        return True
    name = parts[-1]
    return name in PROTECTED_PATHS["names"] or (name in PROTECTED_PATHS["build_files"] and profile != COST)


def _inside(path: str, folder: str) -> list[str] | None:
    """`path`'s components below `folder` (both normalised absolute), or None when it is not strictly inside."""
    root = folder.rstrip("/") + "/"
    return path[len(root):].split("/") if path.startswith(root) and len(path) > len(root) else None


def blocked_write(tool_input: Any, key: str, project: str | None, profile: str) -> str | None:
    """Why a file write may not reach the client (a rule id), or None. The target must be strictly inside the project
    folder, contain no `..`, start with neither `~` nor a variable, and hit no PROTECTED_PATHS entry; a symlink the
    provider's own filesystem shows leading out of the folder (or onto a protected name) is refused too."""
    if project is None:
        return "write-no-project"
    path = tool_input.get(key) if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return "write-malformed"
    if path.startswith("~") or "$" in path:
        return "write-outside-project"                  # a home or variable path is not resolved here: not inside
    if ".." in path.split("/"):
        return "write-dotdot"
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(project, path))
    rel = _inside(full, project)
    if rel is None:
        return "write-outside-project"
    if _protected(rel, profile):
        return "write-protected"
    try:                                                # symlinks: only what this machine's filesystem shows
        real, real_project = os.path.realpath(full), os.path.realpath(project)
    except (OSError, ValueError):
        return "write-malformed"
    if real != full or real_project != project:
        real_rel = _inside(real, real_project)
        if real_rel is None:
            return "write-symlink"
        if _protected(real_rel, profile):
            return "write-protected"
    return None


# Claude Code's environment block in its system prompt: "Primary working directory: /path" (2.x, as a " - " list item)
# or "Working directory: /path" (older <env> block). Only `system` is read (project_dir).
_CWD_LINE = re.compile(r"^[ \t]*(?:-[ \t]+)?(?:Primary working directory|Working directory)[ \t]*:[ \t]*"
                       r"([^\n]*?)[ \t]*$", re.MULTILINE)


def project_dir(body: dict[str, Any]) -> str | None:
    """The conversation's project folder: the one working directory Claude Code names in the request's `system`
    prompt, normalised. None (every write of the turn is then blocked) when there is none, when two different ones are
    named, or when it is relative, holds `..`, or is a filesystem root, /tmp or a home directory (_NOT_PROJECTS).
    Messages and tool results are never read: neither the model nor a fetched page can move the folder."""
    system = body.get("system") if isinstance(body, dict) else None
    if isinstance(system, str):
        text = system
    elif isinstance(system, list):
        text = "\n".join(b.get("text", "") for b in system
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    else:
        return None
    found = {m.group(1) for m in _CWD_LINE.finditer(text)}
    if len(found) != 1:
        return None
    (raw,) = found
    if not raw.startswith("/") or ".." in raw.split("/") or "\x00" in raw:
        return None
    norm = posixpath.normpath(raw)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return None if _NOT_PROJECTS.fullmatch(norm) else norm


def blocked_delegation(name: str, tool_input: Any) -> str | None:
    """Why a delegation call of a local orchestrator is refused (a rule id), or None: start_task may not set the
    DELEGATE_REFUSED_INPUTS, and its input must be an object (the only one judged by its input)."""
    if name != START_TASK_TOOL:
        return None
    if not isinstance(tool_input, dict):
        return "delegate-malformed"
    for key in DELEGATE_REFUSED_INPUTS:
        if tool_input.get(key):
            return "delegate-" + key
    return None


def blocked_call(name: Any, tool_input: Any, profile: str = SENSITIVE, project: str | None = None) -> str | None:
    """Why a tool call the local model emitted must not reach the client under `profile` (a rule id), or None."""
    if not isinstance(name, str) or name not in PROFILE_TOOLS.get(profile, PROFILE_TOOLS[SENSITIVE]):
        return "tool-not-allowed"
    if profile in ORCHESTRATOR_PROFILES:
        # Plan and delegate only: the one shell shape, the delegation inputs; no write tool is in these profiles.
        if name in SHELL_TOOLS:
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            return None if is_watch_command(command) else "bash-not-watch"
        return blocked_delegation(name, tool_input)
    if name in SHELL_TOOLS:
        if not isinstance(tool_input, dict):
            return "bash-malformed"
        return blocked_bash(tool_input.get("command"))
    if name in WRITE_TOOLS:
        return blocked_write(tool_input, WRITE_TOOLS[name], project, profile)
    return None


# ------------------------------------------------------------------------------------- the client-side hook
# The header a client sends on every request when its PreToolUse boundary hook is installed (session.py's
# custom_headers, the routed-subagents mod's child env). Exactly "1"; anything else is "no hook".
HOOK_HEADER = "x-cortados-boundary-hook"
# Every tool call a boundary turn lets through carries an id starting with this, minted by the provider (Boundary.mark),
# never the model's own. The client hook fails closed on any error for such an id, even when it cannot reach the
# provider; an id without it is not the boundary's and passes. Starts like Anthropic's own ids (toolu_).
MARK_PREFIX = "toolu_cxb_"
_MARK_RE = re.compile(re.escape(MARK_PREFIX) + r"[0-9a-f]{24}")
# The ledger's bounds: a record lives LEDGER_TTL_S (a tool call runs seconds after its reply; an hour covers a permission
# prompt left open), at most LEDGER_MAX_PER_CONVERSATION per conversation and LEDGER_MAX in all (oldest out first).
LEDGER_TTL_S = 3600.0
LEDGER_MAX = 4096
LEDGER_MAX_PER_CONVERSATION = 256
# The write tools the hook checks, and the input key naming the target (WRITE_TOOLS, sent in check_answer).
HOOK_TOOLS = frozenset(WRITE_TOOLS) | SHELL_TOOLS


def is_marked(tool_use_id: Any) -> bool:
    """Whether `tool_use_id` has the shape of an id the boundary minted."""
    return isinstance(tool_use_id, str) and bool(_MARK_RE.fullmatch(tool_use_id))


class BoundaryLedger:
    """The provider's record of every tool call a boundary turn let through: tool_use id -> (conversation, tool,
    profile, project, expiry). Bounded (max_total, max_per_conversation; the oldest goes first) and expiring (ttl_s).
    Thread-safe. Metadata only: never a tool input or a file content."""

    def __init__(self, ttl_s: float = LEDGER_TTL_S, max_total: int = LEDGER_MAX,
                 max_per_conversation: int = LEDGER_MAX_PER_CONVERSATION, clock=time.time):
        self.ttl_s, self.max_total, self.max_per_conversation, self.clock = ttl_s, max_total, max_per_conversation, clock
        self._lock = threading.Lock()
        self._records: "OrderedDict[str, dict[str, Any]]" = OrderedDict()   # insertion order is expiry order
        self._per_conversation: dict[str, list[str]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def _drop(self, tid: str) -> None:
        rec = self._records.pop(tid, None)
        if rec is None:
            return
        ids = self._per_conversation.get(rec["conversation"])
        if ids is not None:
            try:
                ids.remove(tid)
            except ValueError:
                pass
            if not ids:
                del self._per_conversation[rec["conversation"]]

    def _expire(self, now: float) -> None:
        while self._records:
            tid, rec = next(iter(self._records.items()))
            if rec["expires"] > now:
                return
            self._drop(tid)

    def record(self, conversation: Any, tool: Any, profile: str, project: str | None) -> str:
        """Mint a new marked id for one tool call and record it; returns the id."""
        conv = conversation if isinstance(conversation, str) and conversation else "-"
        tid = MARK_PREFIX + secrets.token_hex(12)
        with self._lock:
            now = self.clock()
            self._expire(now)
            ids = self._per_conversation.setdefault(conv, [])
            while len(ids) >= self.max_per_conversation:
                self._drop(ids[0])
                ids = self._per_conversation.setdefault(conv, [])
            while len(self._records) >= self.max_total:
                self._drop(next(iter(self._records)))
            self._records[tid] = {"conversation": conv, "tool": tool if isinstance(tool, str) else None,
                                  "profile": profile, "project": project, "expires": now + self.ttl_s}
            self._per_conversation.setdefault(conv, []).append(tid)
        return tid

    def lookup(self, tool_use_id: Any) -> dict[str, Any] | None:
        """The live record of `tool_use_id` (a copy), or None: unknown, expired or not a marked id."""
        if not is_marked(tool_use_id):
            return None
        with self._lock:
            self._expire(self.clock())
            rec = self._records.get(tool_use_id)
            return dict(rec) if rec else None

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._per_conversation.clear()


# The provider's one ledger (the check endpoint reads it; local_lane's boundary turns write it).
LEDGER = BoundaryLedger()


def profile_tools(profile: str, hook: bool = True) -> frozenset:
    """The tools a turn under `profile` may call: PROFILE_TOOLS (an unknown profile: SENSITIVE's), less the shell for an
    orchestrator turn whose client runs no hook (Boundary)."""
    tools = PROFILE_TOOLS.get(profile, PROFILE_TOOLS[SENSITIVE])
    return tools - BASH_FAMILY if profile in ORCHESTRATOR_PROFILES and not hook else tools


def protected_rules(profile: str) -> dict[str, Any]:
    """PROTECTED_PATHS as the hook applies them under `profile` (the build files only on non-COST profiles)."""
    return {"root_dotfiles": True,
            "components": sorted(PROTECTED_PATHS["components"]),
            "component_pairs": sorted([list(p) for p in PROTECTED_PATHS["component_pairs"]]),
            "names": sorted(PROTECTED_PATHS["names"]),
            "build_files": [] if profile == COST else sorted(PROTECTED_PATHS["build_files"])}


def check_answer(tool_use_id: Any, ledger: BoundaryLedger | None = None) -> dict[str, Any]:
    """POST /v1/cortados/boundary/check's answer for `tool_use_id`. {"known": False, "marked": bool} when there is no
    live record; else the record (tool, profile, project, the file-writing tools and their input keys, the protected
    path rules, bash: allowed or not, expires_in). Metadata only."""
    rec = (ledger or LEDGER).lookup(tool_use_id)
    if rec is None:
        return {"known": False, "marked": is_marked(tool_use_id)}
    profile = rec["profile"]
    orch = profile in ORCHESTRATOR_PROFILES
    # `tools`: the profile's tool names, which the hook checks the call against (any tool: its matcher is `*`).
    # `bash_watch`: Bash only in start_task's watch_command shape (orchestrator); `bash` stays False there, so an older
    # hook that knows only `bash` denies every orchestrator shell call. `delegate_refused`: start_task inputs refused.
    return {"known": True, "marked": True, "tool": rec["tool"], "profile": profile, "project": rec["project"],
            "write_tools": {} if orch else dict(WRITE_TOOLS), "bash": profile == COST,
            "bash_watch": orch and "Bash" in PROFILE_TOOLS[profile],
            "tools": sorted(PROFILE_TOOLS.get(profile, PROFILE_TOOLS[SENSITIVE])),
            "delegate_refused": list(DELEGATE_REFUSED_INPUTS) if orch else [],
            "protected": protected_rules(profile),
            "expires_in": max(0, int(rec["expires"] - (ledger or LEDGER).clock()))}


class Boundary:
    """The boundary of one automatic-local turn: its bind reason, the profile it maps to, the project folder read
    from the request (project_dir), whether the client runs the boundary hook (HOOK_HEADER), and the conversation its
    tool calls are recorded under. local_lane.serve / complete build one per turn (for_turn).

    A SENSITIVE turn from a client without the hook is read-only: the file-writing tools are cut from `tools` and an
    emitted one is blocked ("write-no-hook")."""

    def __init__(self, reason: Any, project: str | None, *, hook: bool = False, conversation: Any = None,
                 ledger: BoundaryLedger | None = None):
        self.reason = reason if isinstance(reason, str) else None
        self.profile = profile_for(reason)
        self.project = project
        self.hook = hook is True
        self.read_only = self.profile == SENSITIVE and not self.hook
        # An orchestrator turn without the hook loses its shell (the watcher): profile_tools.
        self.tools = profile_tools(self.profile, self.hook) - (frozenset(WRITE_TOOLS) if self.read_only else
                                                                frozenset())
        self.conversation = conversation if isinstance(conversation, str) else None
        self.ledger = ledger or LEDGER

    @classmethod
    def for_turn(cls, reason: Any, body: dict[str, Any], *, hook: bool = False, conversation: Any = None,
                 ledger: BoundaryLedger | None = None) -> "Boundary":
        b = cls(reason, project_dir(body), hook=hook, conversation=conversation, ledger=ledger)
        if b.reason not in BIND_PROFILES:
            logger.warning("effect boundary: bind reason %r unknown; the %s profile applies", b.reason, SENSITIVE)
        if b.read_only:
            logger.info("effect boundary: no %s on a %s turn: read-only", HOOK_HEADER, SENSITIVE)
        elif b.profile in ORCHESTRATOR_PROFILES and not b.hook:
            logger.info("effect boundary: no %s on an %s turn: no shell", HOOK_HEADER, b.profile)
        return b

    def tools_for_node(self, body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        out, dropped = tools_for_node(body, self.profile)
        cut = (frozenset(WRITE_TOOLS) if self.read_only else frozenset()) | (PROFILE_TOOLS[self.profile] - self.tools)
        if cut and isinstance(out.get("tools"), list):
            kept = [t for t in out["tools"] if t.get("name") not in cut]
            if len(kept) != len(out["tools"]):
                dropped = dropped + [t["name"] for t in out["tools"] if t.get("name") in cut]
                out = dict(out, tools=kept)
                choice = out.get("tool_choice")
                if isinstance(choice, dict) and (choice.get("type") == "tool" and choice.get("name") in cut
                                                 or choice.get("type") == "any" and not kept):
                    out.pop("tool_choice")
        return out, dropped

    def name_rule(self, name: Any) -> str | None:
        """The rule refusing a tool by its name alone under this boundary, or None."""
        if self.read_only and name in WRITE_TOOLS:
            return "write-no-hook"
        if not isinstance(name, str) or name not in self.tools:
            return "tool-not-allowed"
        return None

    def blocked_call(self, name: Any, tool_input: Any) -> str | None:
        return self.name_rule(name) or blocked_call(name, tool_input, self.profile, self.project)

    def blocked_reply(self, blocks: Any) -> tuple[str, str] | None:
        return blocked_reply(blocks, self)

    def mark(self, name: Any) -> str:
        """A new id for a tool call this boundary let through, recorded in the ledger (the client hook's source)."""
        return self.ledger.record(self.conversation, name, self.profile, self.project)

    def mark_blocks(self, blocks: Any) -> None:
        """Give every tool-call block of a judged, allowed reply a minted id (in place)."""
        for b in blocks if isinstance(blocks, list) else []:
            if isinstance(b, dict) and b.get("type") not in PLAIN_BLOCKS and "id" in b:
                b["id"] = self.mark(b.get("name"))


# Content block types a reply may carry without inspection; any other block (tool_use, server_tool_use, an unknown
# type) is a call and is inspected.
PLAIN_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})


def blocked_block(block: Any, boundary: Boundary | None = None) -> tuple[str, str] | None:
    """(rule id, tool name) when a reply content block must not reach the client, else None. Metadata only.
    No `boundary`: the strictest one (SENSITIVE, no project folder)."""
    if not isinstance(block, dict) or block.get("type") in PLAIN_BLOCKS:
        return None
    name = block.get("name")
    b = boundary or Boundary(None, None)
    rule = b.blocked_call(name, block.get("input"))
    return (rule, name if isinstance(name, str) else "<unnamed>") if rule else None


def blocked_reply(blocks: Any, boundary: Boundary | None = None) -> tuple[str, str] | None:
    """The first blocked call in a reply's content, as (rule id, tool name), or None."""
    for b in blocks if isinstance(blocks, list) else []:
        hit = blocked_block(b, boundary)
        if hit:
            return hit
    return None


def blocked_message(profile: Any) -> str:
    """The text a blocked reply is answered with under `profile`."""
    return ORCHESTRATOR_BLOCKED_MESSAGE if profile in ORCHESTRATOR_PROFILES else BLOCKED_MESSAGE


def log_blocked(rule: str, tool: str, where: str) -> None:
    """The one log line of a block: the rule id and the tool name, never the arguments."""
    logger.warning("effect boundary: blocked a local tool call (%s): rule %s, tool %s", where, rule, tool)


class StreamedCall:
    """One tool-call content block being buffered from a stream: its name, any input given at block start, and the
    input_json_delta fragments. complete_input() is the parsed input (None when it does not parse: blocked_call then
    fails closed for Bash and the file-writing tools; a read-only allowlisted tool needs no input to be judged)."""

    def __init__(self, index: Any, block: dict[str, Any]):
        self.index, self.name = index, block.get("name")
        self.start_input = block.get("input")
        self.fragments: list[str] = []

    def complete_input(self) -> Any:
        if self.fragments:
            try:
                return json.loads("".join(self.fragments))
            except ValueError:
                return None
        return self.start_input


def log_dropped(dropped: list[str]) -> None:
    """The tools cut from an automatic-local request: their names only (a tool name is metadata, never an argument)."""
    if dropped:
        logger.info("effect boundary: %d tool(s) not offered to the local model: %s", len(dropped),
                    ", ".join(sorted(set(dropped))))
