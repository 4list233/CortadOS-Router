"""The effect boundary of AUTOMATICALLY local-routed turns (docs/LOCAL-RESOURCES.md "Effect boundary").

Owner policy (Forest): a request may reach a local uncensored model automatically (R0 auto_route, the sensitive flag,
prefer, keyword, the refusal fallback, and the published index when it lands) only when the work has no effect outside
the Pro. local_admission scores the request; scoring alone cannot stop a model from escalating beyond what was asked
("draft it, do not send" answered with a send_email call). So every automatic-local turn also passes this boundary:

  1. tools_for_node  the declared `tools` are cut to LOCAL_TOOL_ALLOWLIST before the request reaches the node, so the
                     model cannot even name a tool that reaches outside the machine. Allowlist, not denylist: a tool
                     not listed (any `mcp__*` connector, WebFetch, WebSearch, Task/Agent, an unknown name) is dropped.
  2. blocked_call    every tool call the local model emits is inspected before the reply reaches the client: a tool
                     not on the allowlist (defence in depth) or a Bash command matching OUTWARD_BASH_RULES blocks the
                     whole reply (local_lane.serve / complete; server.py answers it).

An explicit `sub/<local model>` pin is the client's own choice: it never passes here (local_lane.serve's auto_routed
is False for a pin).

Honest limit: a pattern list cannot recognise every outward shell command (a script that sends, an alias, a variable
holding the command name, base64 or other obfuscation, an interpreter one-liner). The boundary removes the obvious
and the declared routes out; Claude Code's own permission prompts remain the final guard for what Bash runs.
Stdlib only."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("cortados.provider.effect_boundary")

# Tools an automatic-local turn may declare and call. Each acts on this machine only:
#   Read, Glob, Grep, LS, NotebookRead          read local files
#   Edit, MultiEdit, Write, NotebookEdit        write local files (in the client's workspace, under its permissions)
#   TodoWrite, ExitPlanMode                     the client's own task list / plan state, no I/O outside it
#   Bash, BashOutput, KillShell, KillBash       a local shell and its background jobs; Bash's command is inspected
#                                               against OUTWARD_BASH_RULES (KillBash: the older name of KillShell)
# Deliberately NOT listed (so dropped): WebFetch and WebSearch (network), every `mcp__*` tool (connectors: mail, chat,
# drive, calendar, payments, browsers), Task / Agent (a subagent is routed afresh and may land on a cloud model with
# every tool), Skill / SlashCommand (run arbitrary instructions), and any name this list does not know.
LOCAL_TOOL_ALLOWLIST = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead",
    "Edit", "MultiEdit", "Write", "NotebookEdit",
    "TodoWrite", "ExitPlanMode",
    "Bash", "BashOutput", "KillShell", "KillBash",
})
# The allowlisted tools whose `command` input is a shell command line to inspect.
SHELL_TOOLS = frozenset({"Bash"})

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


def _cmd(names: str, cond: str | None = None) -> tuple[frozenset, re.Pattern | None]:
    """A rule: a command word in `names` (space separated, compared case-insensitively: macOS file names are), and
    optionally `cond` searched in the rest of the same simple command (its arguments, each preceded by a space)."""
    return frozenset(names.split()), re.compile(cond) if cond else None


# (rule id, (command names, condition on the arguments)) pairs, checked against every simple command of a Bash call's
# `command`. Conservative: a false positive costs one refused turn (the person reruns it on a cloud model); a miss
# costs an outward effect.
OUTWARD_BASH_RULES: tuple[tuple[str, tuple[frozenset, re.Pattern | None]], ...] = (
    # curl with a method or a body: -X/--request (any method), -d/--data*, -F/--form*, -T/--upload-file, --json,
    # including a combined short flag (-sSd). A plain GET is not matched (see the honest limit: a URL can carry data).
    ("curl-upload", _cmd("curl", r"\s(?:-[A-Za-z]*[dFTX]|--(?:data[\w-]*|form[\w-]*|upload-file|json|request)\b)")),
    ("wget-upload", _cmd("wget", r"\s--(?:post-data|post-file|body-data|body-file|method)\b")),
    ("ssh", _cmd("ssh scp sftp mosh autossh")),
    ("netcat", _cmd("nc ncat netcat socat telnet ftp lftp")),
    # rsync to or from a host: host:path, host::module or rsync://; a local-to-local rsync passes.
    ("rsync-remote", _cmd("rsync", r":")),
    ("git-push", _cmd("git", r"\s(?:push|send-email|request-pull)\b")),
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
)
_RULE_NAMES = frozenset().union(*(names for _, (names, _c) in OUTWARD_BASH_RULES))


def _simple_commands(command: str):
    """(command word, arguments) of each simple command: the word lower-cased without its directory
    (/usr/sbin/sendmail is sendmail), the arguments as one string, each preceded by a space. Linear in the input."""
    for segment in _SPLIT.split(command):
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
            yield words[i].rsplit("/", 1)[-1].lower(), "".join(" " + w for w in words[i + 1:])


def tools_for_node(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The request as an automatic-local node may see it: `tools` cut to LOCAL_TOOL_ALLOWLIST, and a `tool_choice`
    that names a dropped tool (or requires a tool when none is left) removed. Returns (body, dropped tool names). The
    caller's dict is not changed. A body without `tools` is returned as is."""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return body, []
    kept, dropped = [], []
    for t in tools:
        name = t.get("name") if isinstance(t, dict) else None
        # A client tool only (no `type`, or "custom"): a typed server tool (web_search_*, bash_*, computer_*) runs
        # wherever its host runs it, whatever its name, so it is dropped even if the name matches.
        if isinstance(name, str) and name in LOCAL_TOOL_ALLOWLIST and t.get("type") in (None, "custom"):
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
    """The rule id an outward shell command matches, or None. A command that is not a string is blocked (fail closed)."""
    if not isinstance(command, str):
        return "bash-malformed"
    for word, args in _simple_commands(command):
        for rule, (names, cond) in OUTWARD_BASH_RULES:
            if word in names and (cond is None or cond.search(args)):
                return rule
    return None


def blocked_call(name: Any, tool_input: Any) -> str | None:
    """Why a tool call the local model emitted must not reach the client (a rule id), or None when it may."""
    if not isinstance(name, str) or name not in LOCAL_TOOL_ALLOWLIST:
        return "tool-not-allowed"
    if name in SHELL_TOOLS:
        if not isinstance(tool_input, dict):
            return "bash-malformed"
        return blocked_bash(tool_input.get("command"))
    return None


# Content block types a reply may carry without inspection; any other block (tool_use, server_tool_use, an unknown
# type) is a call and is inspected.
PLAIN_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})


def blocked_block(block: Any) -> tuple[str, str] | None:
    """(rule id, tool name) when a reply content block must not reach the client, else None. Metadata only."""
    if not isinstance(block, dict) or block.get("type") in PLAIN_BLOCKS:
        return None
    name = block.get("name")
    rule = blocked_call(name, block.get("input"))
    return (rule, name if isinstance(name, str) else "<unnamed>") if rule else None


def blocked_reply(blocks: Any) -> tuple[str, str] | None:
    """The first blocked call in a reply's content, as (rule id, tool name), or None."""
    for b in blocks if isinstance(blocks, list) else []:
        hit = blocked_block(b)
        if hit:
            return hit
    return None


def log_blocked(rule: str, tool: str, where: str) -> None:
    """The one log line of a block: the rule id and the tool name, never the arguments."""
    logger.warning("effect boundary: blocked a local tool call (%s): rule %s, tool %s", where, rule, tool)


class StreamedCall:
    """One tool-call content block being buffered from a stream: its name, any input given at block start, and the
    input_json_delta fragments. complete_input() is the parsed input (None when it does not parse: blocked_call then
    fails closed for Bash, and a non-shell allowlisted tool needs no input to be judged)."""

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
