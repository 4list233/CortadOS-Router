"""The effect boundary of AUTOMATICALLY local-routed turns (docs/LOCAL-RESOURCES.md "Effect boundary").

Owner policy (Forest): a request may reach a local uncensored model automatically (R0 auto_route, the sensitive flag,
prefer, keyword, the refusal fallback, and the published index when it lands) only when the work has no effect outside
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

Profiles by WHY the turn is local (owner decision, Forest 2026-11-14, "split by reason"): the route carries its
bind reason (routing._moved's `bind_reason`, kept by every continuation of the conversation), BIND_PROFILES maps it:
  sensitive  the sensitive flag, prefer, the keyword list, the refusal fallback: no shell at all (the Bash family is
             not offered and an emitted Bash call is blocked), file writes inside the project only, and every
             PROTECTED_PATHS entry refused (package.json / pyproject.toml / Makefile included).
  cost       R0 auto_route and (branch local-published-index) the published-score index route: Bash stays, inspected
             against the pattern lists, which are BEST-EFFORT (see the honest limit below); the same path rules, with
             package.json / pyproject.toml / Makefile allowed (normal coding targets).
An unknown or missing reason gets the strictest profile (sensitive): fail closed.

An explicit `sub/<local model>` pin is the client's own choice: it never passes here (local_lane.serve's auto_routed
is False for a pin).

The project folder (project_dir) is read from the request's `system` prompt only: Claude Code states its working
directory there ("Primary working directory: <path>", older versions "Working directory: <path>"). Only `system` is
read, never messages or tool results, so neither a fetched page nor the model can move the folder. No such line, two
different ones, a relative path, `..`, or a folder that is a filesystem root or a home directory: unknown, and every
file write of the turn is blocked (fail closed).

Honest limit: a pattern list cannot recognise every outward shell command (a script that sends, an alias defined
earlier, a variable holding the command name, obfuscation, a Makefile target). On cost routes the boundary removes the
obvious and the declared routes out; Claude Code's own permission prompts remain the final guard for what Bash runs.
The path rules read the path's text; a symlink is seen only when it exists on the provider's own machine (realpath).
Stdlib only."""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
from typing import Any

logger = logging.getLogger("cortados.provider.effect_boundary")

# Profiles. A profile names the tools a turn may declare and call, and how strict the path rules are.
SENSITIVE = "sensitive"
COST = "cost"
# WHICH automatic rule bound the conversation to a local lane (routing._moved's `bind_reason`) -> its profile. The
# published-score index route (branch local-published-index) adds one line here: "index": COST.
BIND_PROFILES: dict[str, str] = {
    "sensitive_flag": SENSITIVE,     # x-cortados-local: sensitive
    "prefer": SENSITIVE,             # x-cortados-local: prefer
    "keyword": SENSITIVE,            # the node's keyword list matched the person's words
    "refusal": SENSITIVE,            # a cloud model refused the opening turn
    "auto_route": COST,              # R0 auto_route: local because it is cheaper, not because it is sensitive
}


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
PROFILE_TOOLS = {SENSITIVE: LOCAL_TOOL_ALLOWLIST - BASH_FAMILY, COST: LOCAL_TOOL_ALLOWLIST}
# The allowlisted tools whose `command` input is a shell command line to inspect.
SHELL_TOOLS = frozenset({"Bash"})
# The allowlisted tools that write a file, and the input key naming it.
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

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


def blocked_call(name: Any, tool_input: Any, profile: str = SENSITIVE, project: str | None = None) -> str | None:
    """Why a tool call the local model emitted must not reach the client under `profile` (a rule id), or None."""
    if not isinstance(name, str) or name not in PROFILE_TOOLS.get(profile, PROFILE_TOOLS[SENSITIVE]):
        return "tool-not-allowed"
    if name in SHELL_TOOLS:
        if not isinstance(tool_input, dict):
            return "bash-malformed"
        return blocked_bash(tool_input.get("command"))
    if name in WRITE_TOOLS:
        return blocked_write(tool_input, WRITE_TOOLS[name], project, profile)
    return None


class Boundary:
    """The boundary of one automatic-local turn: its bind reason, the profile it maps to, and the project folder read
    from the request (project_dir). local_lane.serve / complete build one per turn (for_turn)."""

    def __init__(self, reason: Any, project: str | None):
        self.reason = reason if isinstance(reason, str) else None
        self.profile = profile_for(reason)
        self.project = project
        self.tools = PROFILE_TOOLS[self.profile]

    @classmethod
    def for_turn(cls, reason: Any, body: dict[str, Any]) -> "Boundary":
        b = cls(reason, project_dir(body))
        if b.reason not in BIND_PROFILES:
            logger.warning("effect boundary: bind reason %r unknown; the %s profile applies", b.reason, SENSITIVE)
        return b

    def tools_for_node(self, body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        return tools_for_node(body, self.profile)

    def blocked_call(self, name: Any, tool_input: Any) -> str | None:
        return blocked_call(name, tool_input, self.profile, self.project)

    def blocked_reply(self, blocks: Any) -> tuple[str, str] | None:
        return blocked_reply(blocks, self)


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
