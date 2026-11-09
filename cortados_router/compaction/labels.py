"""Automatic keep labels for past tool calls, read off what the session did afterwards (stdlib only).

For each ToolEvent e of a Session:
- needed_result: dropping e's result would have cost something later. True when a later event re-reads the same file
  or re-runs the same normalized command/query (rule "rerun": the agent had to fetch it again), when a later message
  or later tool input quotes a distinctive line (>= QUOTE_MIN chars) of e's result (rule "quote"), or when it uses a
  distinctive identifier (hash, path, id-like token) whose first appearance in the session is e's result (rule
  "identifier"). False when none of that happens before the session ends.
- needed_call: needed_result, or later text names e's input target (path / command / query; rule "target"), or e had
  side effects (rule "side_effect": forgetting that a write or a commit happened misleads the agent).

Events whose issuing message lies in the last `horizon` messages are labelled "unknown": there is not enough future to
say they were never needed, and calling them "not needed" would teach the heads to drop recent work.

Only LATER information decides a label; the S1 dataset (s1/compaction_dataset.py) must never show it to the model.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any, Iterable, Optional

from .events import Session, ToolEvent

QUOTE_MIN = 40          # a 40-char verbatim line is very unlikely to recur by chance; shorter lines ("}", "OK") do
QUOTE_MAX = 200         # compare at most the first 200 chars of a line: long lines are often re-quoted only in part
DEFAULT_HORIZON = 4     # the last 4 messages (about two turns) have too little future to call anything "never needed"

PATH_KEYS = ("file_path", "path", "notebook_path", "filename")
COMMAND_KEYS = ("command", "cmd")
QUERY_KEYS = ("pattern", "query", "url", "q")

# Tools whose call changes something outside the context: the fact that they ran must survive compaction.
SIDE_EFFECT_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch", "str_replace_editor", "create_file",
                     "delete_file", "todowrite"}

# Shell commands are classified by an ALLOWLIST: a command is "no side effect" only when every simple command of a
# fully parsed chain is a listed read-only program whose arguments pass that program's check. Every other program,
# including any unknown one, is a side effect. Reason: the two errors are not symmetric. Keeping a read-only call by
# mistake costs some tokens; labelling a write, an install or a push "read-only" drops the only evidence that it
# happened and poisons the training labels. The previous denylist (`rm`, `git commit`, ...) let `git branch -D`,
# `xargs rm`, `python3 -c`, `curl -d`, `tar xf` and every unknown program through as read-only.
#
# Each entry maps the program name to a checker: args -> True when the call is read-only with those arguments.
# The checker exists where the program can write or execute given the right arguments (find -delete, sed -i,
# sort -o, git branch -D, curl -o); programs that can only ever read take `_any_args`.

PUNCT_SHORT = re.compile(r"^-[^-=]+$")   # a short option or cluster: -n, -la, -n5, -ofile (not --long, not -k=v)


def _cluster(arg: str) -> str:
    """The letters of a short option cluster (`-rnI` -> `rnI`), or '' for anything else. An attached value is
    included on purpose (`-ofile` -> `ofile`): refusing a letter in a value over-refuses, never under-refuses."""
    return arg[1:] if PUNCT_SHORT.match(arg) else ""


def _any_args(args: list[str]) -> bool:
    return True


def _refusing(exact=(), prefixes=(), letters=""):
    """Checker that refuses the listed exact options, option prefixes and short-cluster letters."""
    def check(args: list[str]) -> bool:
        for a in args:
            if a in exact or a.startswith(tuple(prefixes)):
                return False
            if letters and any(ch in letters for ch in _cluster(a)):
                return False
        return True
    return check


# --- git -------------------------------------------------------------------------------------------------------
# Global options: `-c key=value` (sets any config, including core.pager, diff.external and aliases), `--config-env`,
# `--exec-path=DIR` (replaces the git subprograms) and `-p/--paginate` (forces the configured pager even on a pipe)
# make the subcommand run something we cannot see: refused. Aliases are refused by construction, because only the
# named subcommands below pass and git does not let an alias shadow a builtin.
GIT_GLOBAL_WITH_ARG = {"-C", "--git-dir", "--work-tree", "--namespace"}
GIT_GLOBAL_FLAGS = {"--no-pager", "--bare", "--no-replace-objects", "--literal-pathspecs", "--glob-pathspecs",
                    "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks", "--no-lazy-fetch", "--no-advice"}
GIT_GLOBAL_PREFIXES = ("--git-dir=", "--work-tree=", "--namespace=")
# `--output=FILE` writes the diff/log to a file; `--ext-diff` runs the configured external diff program.
GIT_ARGS_REFUSED = ("--output", "--ext-diff")
GIT_BRANCH_LIST = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes", "--verbose",
                   "--color", "--no-color", "--abbrev", "--no-abbrev", "--merged", "--no-merged", "--contains",
                   "--no-contains", "--points-at", "-i", "--ignore-case", "--column", "--no-column", "--format",
                   "--sort"}
GIT_TAG_LIST = {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                "--sort", "--format", "--column", "--no-column", "--color", "-i", "--ignore-case", "--omit-empty"}
GIT_LIST_PREFIXES = ("--color=", "--abbrev=", "--format=", "--sort=", "--column=", "--merged=", "--no-merged=",
                     "--contains=", "--no-contains=", "--points-at=", "-n")
GIT_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch", "--get-color",
                   "--get-colorbool"}
GIT_CONFIG_SCOPE = {"--global", "--local", "--system", "--worktree", "--show-origin", "--show-scope", "-z", "--null",
                    "--name-only", "--includes", "--no-includes", "--bool", "--int", "--path", "--bool-or-int",
                    "--type", "--default", "-f", "--file"}
GIT_CONFIG_PREFIXES = ("--type=", "--default=", "--file=")


def _git_split(args: list[str]) -> Optional[tuple[str, list[str]]]:
    """(subcommand, its arguments) after the global options, or None when a global option is refused or unknown."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in GIT_GLOBAL_WITH_ARG:
            i += 2
        elif a in GIT_GLOBAL_FLAGS or a.startswith(GIT_GLOBAL_PREFIXES):
            i += 1
        elif a == "--version":
            return "version", args[i + 1:]
        elif a.startswith("-"):
            return None             # -c, -p, --paginate, --exec-path, --config-env, --super-prefix, anything new
        else:
            return a, args[i + 1:]
    return None                     # bare `git` prints usage; nothing to allowlist


def _git_branch(rest: list[str]) -> bool:
    listing = "--list" in rest
    for a in rest:
        if a in GIT_BRANCH_LIST or a.startswith(GIT_LIST_PREFIXES):
            continue
        if a.startswith("-") or not listing:
            return False            # -D/-d/-m/-c/-u/--set-upstream-to..., or a positional that creates a branch
    return True


def _git_tag(rest: list[str]) -> bool:
    if not rest:
        return True
    listing = "-l" in rest or "--list" in rest
    for a in rest:
        if a in GIT_TAG_LIST or a.startswith(GIT_LIST_PREFIXES):
            continue
        if a.startswith("-") or not listing:
            return False            # -d/-a/-s/-f/-m..., or a positional that creates a tag
    return True


def _git_config(rest: list[str]) -> bool:
    positionals = [a for a in rest if not a.startswith("-")]
    reading = bool(GIT_CONFIG_READ.intersection(rest)) or (positionals and positionals[0] in {"get", "list"})
    if not reading:
        return False                # `git config key value` writes; `git config key` alone is not worth modelling
    return all(a in GIT_CONFIG_READ or a in GIT_CONFIG_SCOPE or a.startswith(GIT_CONFIG_PREFIXES) or
               not a.startswith("-") for a in rest)


def _git_remote(rest: list[str]) -> bool:
    positionals = [a for a in rest if not a.startswith("-")]
    flags = [a for a in rest if a.startswith("-")]
    if not positionals:
        return all(a in {"-v", "--verbose"} for a in flags)
    return positionals[0] in {"show", "get-url"} and all(a in {"-v", "--verbose", "-n", "--push", "--all"} for a in flags)


def _first_positional_in(allowed: set):
    def check(rest: list[str]) -> bool:
        return bool(rest) and rest[0] in allowed
    return check


# A repository-configured textconv driver can run on diff/log/show. This is an accepted limitation:
# the impact is a training label, and textconv requires prior configuration changes in the repository.
GIT_READ_ONLY = {
    "status": _any_args, "log": _any_args, "show": _any_args, "diff": _any_args, "blame": _any_args,
    "shortlog": _any_args, "rev-parse": _any_args, "rev-list": _any_args, "ls-files": _any_args,
    "ls-tree": _any_args, "cat-file": _any_args, "describe": _any_args, "name-rev": _any_args,
    "merge-base": _any_args, "show-ref": _any_args, "for-each-ref": _any_args, "diff-tree": _any_args,
    "diff-index": _any_args, "diff-files": _any_args, "count-objects": _any_args, "check-ignore": _any_args,
    "var": _any_args, "ls-remote": _any_args, "version": _any_args,
    "grep": _refusing(prefixes=("--open-files-in-pager",), letters="O"),      # opens the matches in the pager/editor
    "branch": _git_branch,
    "tag": _git_tag,
    "config": _git_config,
    "remote": _git_remote,
    "stash": _first_positional_in({"list", "show"}),
    "worktree": _first_positional_in({"list"}),
    "reflog": lambda rest: not rest or rest[0] in {"show", "exists"},  # other forms can mutate refs
}


def _git(args: list[str]) -> bool:
    split = _git_split(args)
    if split is None:
        return False
    sub, rest = split
    check = GIT_READ_ONLY.get(sub.casefold())
    if check is None or any(a.startswith(GIT_ARGS_REFUSED) for a in rest):
        return False
    return check(rest)


# --- sed -------------------------------------------------------------------------------------------------------
# sed is read-only only without in-place editing (-i, -I on BSD, --in-place, any cluster holding i/I), without a
# script file (-f: its contents are unseen), and when every script parses under a small sed grammar in which the
# `w`/`W` (write file), `e` (execute) commands and the `w`/`e` flags of `s` do not occur. Scripts are the -e values,
# or the first positional when there is no -e; the remaining positionals are input files.
SED_FLAGS = set("nErsuz")                       # single-letter options that take no argument
SED_LONG_FLAGS = {"--quiet", "--silent", "--regexp-extended", "--null-data", "--separate", "--unbuffered",
                  "--posix", "--debug", "--sandbox", "--follow-symlinks"}
SED_SIMPLE = set("dDgGhHnNpPxz=F")              # commands without an argument
SED_TO_EOL = set("aic")                         # text commands: the rest of the line is text ("a\" continues)
SED_TO_SEP = set(":btTvlLqQrR")                 # label / branch / number / file-name arguments to `;` or newline


def _sed_scripts(args: list[str]) -> Optional[list[str]]:
    scripts: list[str] = []
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positionals.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name == "--expression":
                if eq:
                    scripts.append(value)
                else:
                    if i + 1 >= len(args):
                        return None
                    scripts.append(args[i + 1])
                    i += 1
            elif a in SED_LONG_FLAGS or name in {"--line-length"}:
                pass
            else:
                return None         # --in-place, --file, anything unknown
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            j = 0
            while j < len(letters):
                ch = letters[j]
                if ch == "e":
                    rest = letters[j + 1:]
                    if rest:
                        scripts.append(rest)
                    else:
                        if i + 1 >= len(args):
                            return None
                        scripts.append(args[i + 1])
                        i += 1
                    break
                if ch == "l":
                    break           # -l N (line length): the rest of the cluster or the next argument is a number
                if ch not in SED_FLAGS:
                    return None     # i, I, f, and anything unknown
                j += 1
        else:
            positionals.append(a)
        i += 1
    if not scripts:
        if not positionals:
            return None
        scripts.append(positionals[0])
    return scripts


def _sed_skip_regex(s: str, i: int, delim: str) -> int:
    """Index just past the closing delimiter of a regex/field that starts at s[i] (after the opening delimiter).
    Backslash escapes the next character; a bracket expression is skipped whole because the delimiter is literal
    inside it. Returns -1 when the field never closes."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == delim:
            return i + 1
        if c == "[":
            j = i + 1
            if j < n and s[j] == "^":
                j += 1
            if j < n and s[j] == "]":
                j += 1
            close = s.find("]", j)
            if close < 0:
                return -1
            i = close + 1
            continue
        i += 1
    return -1


def _sed_skip_address(s: str, i: int) -> int:
    """Index after one address (number[~step], $, /re/IM, \\cREc) starting at s[i]; i itself when none."""
    n = len(s)
    if i >= n:
        return i
    c = s[i]
    if c.isdigit():
        while i < n and (s[i].isdigit() or s[i] == "~"):
            i += 1
        return i
    if c == "$":
        return i + 1
    if c == "+" or c == "~":        # addr2 forms `+N`, `~N`
        i += 1
        while i < n and s[i].isdigit():
            i += 1
        return i
    if c in "/\\":
        if c == "\\":
            i += 1
            if i >= n:
                return -1
        delim = s[i]
        i = _sed_skip_regex(s, i + 1, delim)
        if i < 0:
            return -1
        while i < n and s[i] in "IM":
            i += 1
        return i
    return i


def _sed_script_read_only(s: str) -> bool:
    n = len(s)
    i = 0
    while i < n:
        while i < n and s[i] in " \t\n;":
            i += 1
        if i >= n:
            break
        if s[i] == "#":
            end = s.find("\n", i)
            i = n if end < 0 else end
            continue
        if s[i] == "}":
            i += 1
            continue
        j = _sed_skip_address(s, i)
        if j < 0:
            return False
        if j > i and j < n and s[j] == ",":
            j = _sed_skip_address(s, j + 1)
            if j < 0:
                return False
        i = j
        while i < n and s[i] in " \t!":
            i += 1
        if i >= n:
            return False            # an address without a command is a syntax error: not provably read-only
        c = s[i]
        i += 1
        if c == "{":
            continue
        if c in "wWe":
            return False
        if c in SED_SIMPLE:
            pass
        elif c in SED_TO_EOL:
            while True:
                end = s.find("\n", i)
                line = s[i:] if end < 0 else s[i:end]
                i = n if end < 0 else end + 1
                if not line.endswith("\\") or end < 0:
                    break
            continue
        elif c in SED_TO_SEP:
            while i < n and s[i] not in ";\n":
                i += 1
            continue
        elif c in "sy":
            if i >= n:
                return False
            delim = s[i]
            if delim in "\n\\":
                return False
            i = _sed_skip_regex(s, i + 1, delim)
            if i < 0:
                return False
            i = _sed_skip_regex(s, i, delim)
            if i < 0:
                return False
            if c == "s":
                while i < n and s[i] in "gpImM0123456789":
                    i += 1
                if i < n and s[i] in "we":
                    return False
        else:
            return False            # unknown command letter
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] not in ";\n}#":
            return False            # trailing garbage after a command (`p w file` would be rejected by sed; so are we)
    return True


def _sed(args: list[str]) -> bool:
    scripts = _sed_scripts(args)
    return scripts is not None and all(_sed_script_read_only(s) for s in scripts)


# --- curl / gh -------------------------------------------------------------------------------------------------
# curl is read-only only as a GET/HEAD fetch to stdout. Any option that sends a body (-d -F -T --json), writes a
# file (-o -O -J -c -D -w/--write-out --trace* --libcurl --etag-save), reads an unseen config (-K/-q are not enough) or changes
# the method is refused; so is every option not listed, because curl has hundreds and some send mail or FTP
# commands. Positionals are URLs.
CURL_FLAGS = set("sSLIikfgvG46N#")
CURL_WITH_ARG = set("HAumxebrzEyYX")           # the value is the rest of the cluster or the next argument
CURL_LONG_FLAGS = {"--silent", "--show-error", "--location", "--head", "--include", "--insecure", "--fail",
                   "--fail-with-body", "--globoff", "--verbose", "--get", "--compressed", "--http1.1", "--http2",
                   "--http3", "--http0.9", "--ipv4", "--ipv6", "--no-buffer", "--no-progress-meter", "--progress-bar",
                   "--tlsv1.2", "--tlsv1.3", "--path-as-is", "--no-keepalive", "--tcp-nodelay", "--tr-encoding",
                   "--raw", "--basic", "--digest", "--ntlm", "--negotiate", "--anyauth", "--disable", "--retry-connrefused"}
CURL_LONG_WITH_ARG = {"--header", "--user-agent", "--user", "--max-time", "--connect-timeout",
                      "--proxy", "--referer", "--cookie", "--range", "--time-cond", "--retry", "--retry-delay",
                      "--retry-max-time", "--url", "--resolve", "--max-redirs", "--cacert", "--capath", "--cert",
                      "--key", "--proto", "--unix-socket", "--abstract-unix-socket", "--speed-limit", "--speed-time",
                      "--limit-rate", "--oauth2-bearer", "--doh-url", "--interface", "--local-port",
                      "--keepalive-time", "--request"}
READ_METHODS = {"GET", "HEAD"}


def _curl(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return True             # only URLs follow
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in CURL_LONG_FLAGS:
                pass
            elif name in CURL_LONG_WITH_ARG:
                if not eq:
                    if i + 1 >= len(args):
                        return False
                    value = args[i + 1]
                    i += 1
                if name == "--request" and value.upper() not in READ_METHODS:
                    return False
            else:
                return False
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            j = 0
            while j < len(letters):
                ch = letters[j]
                if ch in CURL_WITH_ARG:
                    value = letters[j + 1:]
                    if not value:
                        if i + 1 >= len(args):
                            return False
                        value = args[i + 1]
                        i += 1
                    if ch == "X" and value.upper() not in READ_METHODS:
                        return False
                    break
                if ch not in CURL_FLAGS:
                    return False    # -d -F -T -o -O -J -c -D -K -q and anything unknown
                j += 1
        i += 1
    return True


# gh: named read subcommands only; `-w/--web` opens a browser; `gh api` must stay a GET without a body.
GH_READ = {("pr", "view"), ("pr", "list"), ("pr", "status"), ("pr", "diff"), ("pr", "checks"), ("issue", "view"),
           ("issue", "list"), ("issue", "status"), ("repo", "view"), ("run", "list"), ("run", "view"),
           ("release", "list"), ("release", "view"), ("auth", "status"), ("search", "issues"), ("search", "prs"),
           ("search", "repos"), ("search", "code")}
GH_API_REFUSED = ("-f", "-F", "--field", "--raw-field", "--input", "--method", "-X")


def _gh(args: list[str]) -> bool:
    if any(a in {"-w", "--web"} for a in args):
        return False
    if len(args) >= 2 and (args[0], args[1]) in GH_READ:
        return True
    if args and args[0] == "api":
        rest = args[1:]
        for k, a in enumerate(rest):
            if a.startswith(GH_API_REFUSED):
                if a in {"-X", "--method"} and k + 1 < len(rest) and rest[k + 1].upper() in READ_METHODS:
                    continue
                name, eq, value = a.partition("=")
                if eq and name in {"-X", "--method"} and value.upper() in READ_METHODS:
                    continue
                return False
        return True
    return args[:1] == ["--version"]


# --- the allowlist ---------------------------------------------------------------------------------------------
# Interpreters never pass with code (`python3 -c`, `node -e`, `perl -pi -e`, `awk 'system()'`); only a version query.
INTERPRETERS = {"python", "python2", "python3", "node", "ruby", "perl", "php", "java", "go", "cargo", "rustc",
                "deno", "bun", "lua", "awk", "gawk", "mawk", "nawk"}
VERSION_ONLY = ({"--version"}, {"-V"}, {"-v"}, {"version"}, {"-version"})
PACKAGE_READS = {"pip": {"list", "show", "freeze", "check", "--version", "-V"},
                 "pip3": {"list", "show", "freeze", "check", "--version", "-V"},
                 "npm": {"ls", "list", "ll", "la", "view", "info", "show", "outdated", "why", "explain",
                         "--version", "-v"},
                 "brew": {"list", "ls", "info", "outdated", "deps", "search", "--version", "-v"}}
# `date` sets the clock with -s/--set or a bare positional (root); only format and display options are read-only.
DATE_FLAGS = {"-u", "--utc", "--universal", "-R", "--rfc-2822", "--rfc-email", "-j"}
DATE_PREFIXES = ("+", "-I", "--iso-8601", "--rfc-3339")
HOSTNAME_FLAGS = {"-s", "-f", "-d", "-i", "-I", "-a", "-A", "-y", "--short", "--fqdn", "--long", "--domain",
                  "--ip-address", "--all-ip-addresses", "--alias", "--all-fqdns"}


def _version_only(args: list[str]) -> bool:
    return any(set(args) == v and len(args) == 1 for v in VERSION_ONLY)


def _package_reads(prog: str):
    allowed = PACKAGE_READS[prog]
    return lambda args: bool(args) and args[0] in allowed


READ_ONLY_PROGRAMS = {
    # reads of files, directories and system state: cannot write or execute whatever the arguments
    **{p: _any_args for p in (
        "cat", "ls", "head", "tail", "wc", "pwd", "echo", "printf", "true", "false", ":", "test", "stat",
        "du", "df", "whoami", "id", "uname", "nproc", "uptime", "ps", "lsof", "which",
        "whereis", "type", "basename", "dirname", "realpath", "readlink", "grep", "egrep", "fgrep", "cut", "tr",
        "column", "nl", "rev", "comm", "diff", "cmp", "md5", "md5sum", "shasum", "sha1sum", "sha256sum", "jq",
        "seq", "sleep", "printenv", "od", "hexdump", "strings", "sw_vers", "cd")},
    # `cd` changes only the shell's working directory (the next command's paths show it); it writes nothing.
    "file": _refusing(exact=("--compile",), letters="C"),         # -C compiles a magic file into the cwd
    "tree": _refusing(prefixes=("--output",), letters="o"),      # -o FILE writes the listing to a file
    "hostname": lambda args: all(a in HOSTNAME_FLAGS for a in args),   # a positional or -F FILE sets the hostname
    "arch": lambda args: not args,                               # macOS `arch -x86_64 CMD` runs CMD
    "find": _refusing(exact=("-delete",), prefixes=("-exec", "-ok", "-fprint", "-fls")),
    "fd": _refusing(exact=("-x", "-X"), prefixes=("--exec",), letters="xX"),
    "rg": _refusing(prefixes=("--pre",)),                 # --pre runs a preprocessor command on every file
    "sort": _refusing(prefixes=("--output",), letters="o"),   # -o FILE writes the result to a file
    "uniq": lambda args: len([a for a in args if not a.startswith("-")]) <= 1,   # the second positional is an output
    "date": lambda args: all(a in DATE_FLAGS or a.startswith(DATE_PREFIXES) for a in args),
    "sed": _sed,
    "git": _git,
    "curl": _curl,
    "gh": _gh,
    **{p: _version_only for p in INTERPRETERS},
    **{p: _package_reads(p) for p in PACKAGE_READS},
}

# Characters shlex emits as punctuation tokens. "\n" is among them (and removed from the whitespace set) so an
# unquoted newline survives as a command boundary: as whitespace it vanished and `git status\ngit -C /repo commit`
# read as one `git status` with extra arguments.
PUNCTUATION = "();<>|&\n"
# Only explicitly supported punctuation operators are accepted below; all others are conservative.
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
# `>&` writes a file (`echo hi >&out.txt`) unless its target is a descriptor: digits (`2>&1`, `>&2`) or `-` (close).
DUP_REDIRECT = ">&"
# Wrappers whose arguments run the real program; each maps to the wrapper options that take a separate argument,
# skipped together with their value so `sudo -u user rm x` or `env -u HOME rm x` still reach `rm`.
WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U"},
    "env": {"-u", "-C", "-S", "-P", "--unset", "--chdir", "--split-string"},
    "exec": {"-a"},
    "time": set(),
    "command": set(),
    "nohup": set(),
    "nice": {"-n", "--adjustment"},
}
# `timeout DURATION cmd`: the first non-option argument is the duration, not the program.
WRAPPERS_WITH_POSITIONAL = {"timeout": {"-s", "-k", "--signal", "--kill-after"}}
# Unknown wrapper options may consume the real executable (env -S was one example). Only
# these no-argument flags and the separate-argument options above are safely modelled.
WRAPPER_FLAGS = {
    "sudo": {"-E", "-H", "-n", "-S", "-b", "-k", "-K", "-v"},
    "env": {"-i", "--ignore-environment", "-0", "--null"},
    "exec": {"-c", "-l"},
    "time": {"-p"},
    "command": {"-p", "-v", "-V"},
    "nohup": set(),
    "nice": set(),
    "timeout": {"--preserve-status", "--foreground", "-v", "--verbose"},
}
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
# Assignments that change WHICH program runs or make a read-only program execute something: `PATH=/tmp/x ls` runs
# /tmp/x/ls, `HOME=/tmp/x git diff` reads a planted .gitconfig (diff.external), `PAGER='touch m' git -p log`.
UNSAFE_ASSIGNMENTS = {"PATH", "HOME", "IFS", "ENV", "BASH_ENV", "CDPATH", "SHELL", "ZDOTDIR", "PAGER", "GIT_PAGER",
                      "EDITOR", "VISUAL", "GIT_EDITOR", "GIT_EXEC_PATH", "GIT_SSH", "GIT_SSH_COMMAND",
                      "GIT_EXTERNAL_DIFF", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                      "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "XDG_CONFIG_HOME", "CURL_HOME", "PERL5OPT",
                      "PYTHONSTARTUP", "NODE_OPTIONS", "LESSOPEN", "LESSCLOSE"}
UNSAFE_ASSIGNMENT_PREFIXES = ("LD_", "DYLD_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _unwrap(seg: list[str]) -> Optional[list[str]]:
    """Strip VAR=value assignments and wrapper programs (with their options) until the real program is first.
    Iterative on purpose: `env FOO=1 git commit` carries the assignment AFTER the wrapper, and `sudo env X=1 cmd`
    nests wrappers. Every iteration drops at least one token, so the loop ends. None means "not modelled"."""
    i = 0
    while i < len(seg):
        t = seg[i]
        m = ASSIGNMENT.match(t)
        if m:
            var = m.group(1)
            if var in UNSAFE_ASSIGNMENTS or var.startswith(UNSAFE_ASSIGNMENT_PREFIXES):
                return None
            i += 1
            continue
        # Paths outside trusted system directories must not inherit an allowlisted basename,
        # even for wrappers: ./env or ./cat may be arbitrary repository executables.
        if "/" in t and not (os.path.isabs(t) and os.path.normpath(t).startswith(
                ("/bin/", "/usr/bin/", "/opt/homebrew/bin/"))):
            return None
        name = os.path.basename(t).casefold()
        if name not in WRAPPERS and name not in WRAPPERS_WITH_POSITIONAL:
            break
        with_arg = WRAPPERS[name] if name in WRAPPERS else WRAPPERS_WITH_POSITIONAL[name]
        i += 1
        while i < len(seg) and seg[i].startswith("-"):
            if seg[i] == "--":
                i += 1
                break
            option = seg[i]
            if name == "env" and option in {"-S", "--split-string"}:
                return None  # env has a separate expansion language, not ordinary wrapper arguments
            if name == "command" and option in {"-v", "-V"}:
                return []    # `command -v prog` only describes prog, it never runs it
            if option in with_arg:
                i += 2
            elif option in WRAPPER_FLAGS[name]:
                i += 1
            else:
                return None  # attached/grouped/unknown options cannot safely be skipped
        if name in WRAPPERS_WITH_POSITIONAL:
            i += 1                  # the positional (timeout's DURATION)
    return seg[i:]


def _segment_has_side_effect(seg: list[str]) -> bool:
    """True unless the simple command is an allowlisted read-only program with arguments its checker accepts.
    Shell keywords, builtins that evaluate code, interpreters, executors (xargs, find -exec) and every unknown
    program are side effects by falling through. The program name is matched case-insensitively because on a
    case-insensitive filesystem `LS` runs ls; a name that then matches nothing is a side effect anyway."""
    seg = _unwrap(seg)
    if seg is None:
        return True
    if not seg:
        return False                # only assignments/wrapper options: nothing ran
    prog, args = os.path.basename(seg[0]).casefold(), seg[1:]
    check = READ_ONLY_PROGRAMS.get(prog)
    return check is None or not check(args)


def _shell_subset(cmd: str) -> tuple[str, bool]:
    """Strip comments without losing newlines; reject syntax shlex does not model.

    This is a lexical guard, not a shell parser. Expansions, compound constructs, continuations and
    nontrivial redirects must never acquire a read-only label just because shlex accepts them.
    Quotes/escapes are tracked here because shlex discards that information. Two word-level decisions
    that need adjacency are also made here:
    - an unquoted all-digit word immediately followed by `<` or `>` is an IO number (`2>&1`, `0</dev/null`) and
      is dropped, so it never looks like an argument; a digit word followed by a space (`timeout 5 </dev/null rm x`)
      is an ordinary argument and stays;
    - a quoted or escaped word made only of operator characters (`'<'`, `\\;`) is reported as unsupported, because
      shlex would hand it back as a bare operator and `git -C '<' commit` would read as a redirect.
    """
    out: list[str] = []
    quote = ""
    word_start = True
    word_begin = 0              # index in `out` where the current word began
    word_quoted = False         # the current word held a quote or an escape
    content: list[str] = []     # the literal characters of the current word, as shlex would yield them

    def quoted_operator() -> bool:
        return word_quoted and bool(content) and all(ch in PUNCTUATION for ch in content)

    i = 0
    while i < len(cmd):
        c = cmd[i]
        if c == "#" and not quote and word_start:
            end = cmd.find("\n", i)
            i = len(cmd) if end < 0 else end
            continue
        if c == "\\" and quote != "'":
            if i + 1 >= len(cmd) or cmd[i + 1] == "\n":
                return "".join(out), False
            out.extend(cmd[i:i + 2])
            content.append(cmd[i + 1])
            word_quoted = True
            i += 2
            word_start = False
            continue
        if c in "'\"" and (not quote or quote == c):
            quote = "" if quote else c
            word_quoted = True
            out.append(c)
            word_start = False
            i += 1
            continue
        if quote != "'" and c in "$`":
            return "".join(out), False
        if not quote and c in "{}*?[]~":
            return "".join(out), False
        if not quote and c == "=" and word_start:
            return "".join(out), False      # zsh `=prog` expands to the program's path
        if not quote and c in " \t\r\n;|&()<>":
            if c in "<>" and content and not word_quoted and all(ch.isdigit() for ch in content):
                del out[word_begin:]        # IO number: part of the redirect, not an argument
            elif quoted_operator():
                return "".join(out), False
            out.append(c)
            content = []
            word_quoted = False
            word_start = True
            word_begin = len(out)
            i += 1
            continue
        out.append(c)
        content.append(c)
        word_start = False
        i += 1
    if quote or quoted_operator():
        return "".join(out), False
    return "".join(out), True


def _tokens(cmd: str) -> tuple[list[str], bool]:
    cmd, supported = _shell_subset(cmd)
    if not supported:
        return [], False
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=PUNCTUATION)
    lex.whitespace_split = True
    lex.whitespace = " \t\r"
    lex.commenters = ""  # comments were removed above, retaining their terminating newline
    try:
        # Split punctuation runs so a redirect next to a newline cannot erase the command boundary.
        tokens = []
        for t in lex:
            if "\n" in t and all(c in PUNCTUATION for c in t):
                tokens.extend(re.findall(r"[^\n]+|\n", t))
            else:
                tokens.append(t)
        return tokens, True
    except ValueError:
        return [], False


def _tokens_mutate(tokens: list[str]) -> bool:
    seg: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t and all(c in PUNCTUATION for c in t):
            if t in WRITE_REDIRECTS or t in {DUP_REDIRECT, "<", "<&"}:
                if i + 1 >= len(tokens):
                    return True  # incomplete syntax cannot prove read-only
                target = tokens[i + 1]
                if not target or all(c in PUNCTUATION for c in target):
                    return True
                if t in WRITE_REDIRECTS and target != "/dev/null":
                    return True
                if t == ">&" and not (target.isdigit() or target in {"-", "/dev/null"}):
                    return True
                if t == "<&" and not (target.isdigit() or target == "-"):
                    return True
                i += 2
                continue
            if t not in {";", "&&", "||", "|", "&", "|&", "\n"}:
                return True  # heredocs, here strings, groups and unknown operators are unmodelled
            if _segment_has_side_effect(seg):
                return True
            seg = []
        else:
            seg.append(t)
        i += 1
    return _segment_has_side_effect(seg)


def command_has_side_effect(cmd: str) -> bool:
    """True means known mutation, an unlisted program, OR unmodelled shell syntax (there is no uncertain label).

    Only a completely parsed supported command whose every simple command is an allowlisted read-only program
    may return False. A regex fallback cannot establish safety for a shell program and previously turned parser
    gaps into incorrect training labels.
    """
    tokens, complete = _tokens(cmd)
    return not complete or _tokens_mutate(tokens)


# Distinctive identifiers: git-like hashes, uuids, slash paths, long id-like tokens mixing letters and digits.
IDENT_PATTERNS = (
    re.compile(r"\b[0-9a-f]{7,40}\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"(?:~|\.{0,2})?/(?:[\w.-]+/)+[\w.-]+"),
    re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{10,}\b"),
)


def _first(d: dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def normalize_command(cmd: str) -> str:
    return " ".join(cmd.split())


def target(e: ToolEvent) -> Optional[tuple[str, str]]:
    """(kind, normalized value) of what the call acted on, or None."""
    inp = e.input if isinstance(e.input, dict) else {}
    p = _first(inp, PATH_KEYS)
    if p:
        return ("path", p)
    c = _first(inp, COMMAND_KEYS)
    if c:
        return ("command", normalize_command(c))
    q = _first(inp, QUERY_KEYS)
    if q:
        return ("query", f"{e.name.lower()}:{normalize_command(q)}")
    return None


def has_side_effect(e: ToolEvent) -> bool:
    if e.name.lower() in SIDE_EFFECT_TOOLS:
        return True
    inp = e.input if isinstance(e.input, dict) else {}
    for k in COMMAND_KEYS:
        v = inp.get(k)
        if v is not None and not isinstance(v, str):
            # {"command": ["bash", "-lc", "git push"]}: an argv list or any other shape is a command we did not
            # parse, so it cannot be proven read-only (it was silently read-only before).
            return True
    c = _first(inp, COMMAND_KEYS)
    return bool(c and command_has_side_effect(c))


def _strings(v: Any) -> Iterable[str]:
    """Every string leaf of a tool input, decoded. Searching the JSON dump instead would hide reuse behind escapes:
    an Edit old_string quoting a line with `"` or `\\` appears as `\\"` / `\\\\` in JSON and no longer matches."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k in sorted(v, key=str):
            yield from _strings(v[k])
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)
    elif v is not None and not isinstance(v, bool):
        yield str(v)


def _input_text(e: ToolEvent) -> str:
    return "\n".join(_strings(e.input))


def distinctive_lines(text: str) -> list[str]:
    out, seen = [], set()
    for line in text.splitlines():
        s = line.strip()[:QUOTE_MAX]
        if len(s) >= QUOTE_MIN and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def identifiers(text: str) -> set[str]:
    found: set[str] = set()
    for pat in IDENT_PATTERNS:
        found.update(m.group(0) for m in pat.finditer(text))
    return {t for t in found if len(t) >= 7}


def label_session(session: Session, *, horizon: int = DEFAULT_HORIZON) -> list[dict[str, Any]]:
    """One label row per tool event, in event order."""
    msgs = sorted(session.messages, key=lambda m: m.index)
    events = sorted(session.events, key=lambda e: e.index)
    last_msg = msgs[-1].index if msgs else -1
    cutoff = last_msg - horizon          # events issued by a message after this index have too little future
    rows = []
    for e in events:
        later_msgs = [m for m in msgs if m.index > e.message_index]
        later_events = [x for x in events if x.index > e.index]
        row: dict[str, Any] = {
            "session": session.session, "source": session.source, "event_index": e.index,
            "message_index": e.message_index, "tool": e.name, "result_chars": e.result_chars,
            "horizon": {"messages": len(later_msgs), "events": len(later_events), "min_future_messages": horizon},
        }
        if e.message_index > cutoff:
            row.update(status="unknown", needed_call=None, needed_result=None,
                       evidence=[{"rule": "horizon", "detail": f"issued within the last {horizon} messages"}])
            rows.append(row)
            continue
        evidence = _result_evidence(e, events, msgs, later_msgs, later_events)
        needed_result = bool(evidence)
        call_ev = list(evidence)
        if has_side_effect(e):
            call_ev.append({"rule": "side_effect", "detail": e.name})
        tgt = target(e)
        if tgt and not evidence:
            hit = _mentions(tgt[1] if tgt[0] != "query" else tgt[1].split(":", 1)[1], later_msgs, later_events)
            if hit:
                call_ev.append({"rule": "target", **hit})
        row.update(status="labelled", needed_result=needed_result, needed_call=bool(call_ev), evidence=call_ev)
        rows.append(row)
    return rows


def _mentions(needle: str, later_msgs, later_events) -> Optional[dict[str, Any]]:
    for m in later_msgs:
        if needle in m.text:
            return {"later": "message", "index": m.index}
    for x in later_events:
        if needle in _input_text(x):
            return {"later": "event", "index": x.index}
    return None


def _result_evidence(e: ToolEvent, events, msgs, later_msgs, later_events) -> list[dict[str, Any]]:
    if e.interrupted or not e.result_text:
        return []                          # no result was ever in the context, so none can be needed
    ev: list[dict[str, Any]] = []
    tgt = target(e)
    if tgt:
        for x in later_events:
            # a later write to the same file is not a re-read; a re-run of a side-effect command is a new action
            if target(x) == tgt and not has_side_effect(x):
                ev.append({"rule": "rerun", "later": "event", "index": x.index, "detail": f"{tgt[0]} {tgt[1][:120]}"})
                break
    later_texts = [("message", m.index, m.text) for m in later_msgs] + \
                  [("event", x.index, _input_text(x)) for x in later_events]
    for line in distinctive_lines(e.result_text):
        hit = next(((k, i) for k, i, t in later_texts if line in t), None)
        if hit:
            ev.append({"rule": "quote", "later": hit[0], "index": hit[1], "detail": line[:80]})
            break
    # identifiers first seen in e's result: not in any earlier message, earlier event input/result, or e's own input
    before = [m.text for m in msgs if m.index <= e.message_index] + \
             [_input_text(x) + "\n" + x.result_text for x in events if x.index < e.index] + [_input_text(e)]
    before_text = "\n".join(before)
    fresh = sorted(t for t in identifiers(e.result_text) if t not in before_text)
    for tok in fresh:
        hit = next(((k, i) for k, i, t in later_texts if tok in t), None)
        if hit:
            ev.append({"rule": "identifier", "later": hit[0], "index": hit[1], "detail": tok[:80]})
            break
    return ev


def write_jsonl(rows: Iterable[dict[str, Any]], path) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
            n += 1
    return n
