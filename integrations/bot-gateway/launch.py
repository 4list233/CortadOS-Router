"""Bot gateway launcher (the LaunchAgent's program; integrations/bot-gateway/install.sh writes the plist that runs it).

Takes the provider token for route_task from ONE credential source, either ~/.cortados/provider.env (design a) or an
owner-made 0600 token file (design b), WITHOUT executing anything, then execs the gateway with an explicit environment
(python -I -S -B, through a fixed bootstrap that adds only this checkout to sys.path).
Both designs go through here (review r3), so the gateway never inherits launchd's environment (PYTHONPATH, proxies, an
ambient CORTADOS_PROVIDER_TOKEN that would override the file). Reason (review r2): sourcing provider.env as shell code lets anything in it
(set -x, traps, command substitution, readonly or exported variables) print the token, pass it to another program or
add variables to the gateway's environment. Here the file is data:

  - lines are split on LF only (a CRLF ending is accepted), and only spaces and tabs are trimmed;
  - accepted lines: blank, `# comment`, and `[export ]NAME=value`, where value is unquoted, 'single-quoted' or
    "double-quoted"; nothing is expanded;
  - a control character (NUL, U+001C, a lone CR, ...), a Unicode line or paragraph separator, `$`, a backtick, a
    backslash, a repeated name or any other line (a command, a function, a trap, set -x) refuses the whole file;
  - only CORTADOS_PROVIDER_TOKEN is used; it must be non-empty printable ASCII with no spaces.
The token file (design b) holds the token alone, optionally followed by one LF or CRLF; nothing else is trimmed.
Gateway flags are checked against the phase-1 allowlist (exact spellings, separate values), so no abbreviation the
gateway's own argparse would expand (e.g. --enable-start-t) can get through.

Every config failure exits 78 (EX_CONFIG) with a message that names a line NUMBER at most, never a line's content or
the token. The gateway gets exactly HOME, a fixed PATH, CORTADOS_HOME, CORTADOS_PROVIDER_URL (the three from the plist)
and CORTADOS_PROVIDER_TOKEN; nothing else is inherited. The token goes in the environment only, never in argv.

Run (by launchd): /usr/bin/python3 -I launch.py (--provider-env FILE | --provider-token-file FILE)
                  [--python /usr/bin/python3] -- <gateway flags>
Stdlib only.
"""
import argparse
import os
import re
import stat
import sys
import unicodedata

EX_CONFIG = 78
TOKEN_NAME = "CORTADOS_PROVIDER_TOKEN"
FIXED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
FROM_PLIST = ("HOME", "CORTADOS_HOME", "CORTADOS_PROVIDER_URL")   # set by the plist (HOME by launchd); not secrets
MAX_BYTES = 64 * 1024            # provider.env is a few lines; a bigger file is not one we wrote
# Phase-1 gateway flags, exact spellings: flag -> takes a value. Anything else (notably --enable-start-task or any
# prefix of it, which the gateway's argparse would expand) is refused: phase 1 is route_task only (owner decision
# 2026-11-03). --children-run-as-me is required by the gateway for a tailnet --host; with no start_task it starts no child.
# The checkout this launcher lives in (integrations/bot-gateway/launch.py -> the repo root): the only sys.path entry the
# gateway child gets besides the standard library.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# -I: isolated (no PYTHON* variables, no user site, no cwd on sys.path); -S: no site module at all (no sitecustomize);
# -B: no .pyc writes into the checkout.
CHILD_OPTS = ("-I", "-S", "-B")
ALLOWED_FLAGS = {"--host": True, "--port": True, "--token-file": True, "--workspace-root": True,
                 "--children-run-as-me": False}

LINE = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UNQUOTED = re.compile(r"^[^\s'\"`$\\;&|<>(){}*?\[\]!#~]*$")
SINGLE = re.compile(r"^'([^']*)'$")
DOUBLE = re.compile(r"^\"([^\"\\`$]*)\"$")


class ConfigError(Exception):
    pass


def _bad_char(line: str) -> bool:
    """A control character (category Cc, tab excepted: NUL, U+001C, a stray CR, DEL, C1) or a Unicode line/paragraph
    separator. Refused, never normalized: str.splitlines would cut a line at U+001C and str.strip would eat U+00A0, so
    the token the gateway sends would silently differ from the file."""
    return any((unicodedata.category(c) in ("Cc", "Zl", "Zp")) and c != "\t" for c in line)


def parse_env(text: str) -> dict:
    """NAME -> value for the accepted line forms; ConfigError (with a line number only, never a name or value) for
    anything else."""
    out = {}
    for n, raw in enumerate(text.split("\n"), 1):
        if raw.endswith("\r"):
            raw = raw[:-1]                          # CRLF accepted; a CR anywhere else is a control character
        if _bad_char(raw):
            raise ConfigError(f"line {n}: control character")
        line = raw.strip(" \t")
        if not line or line.startswith("#"):
            continue
        if "$" in line or "`" in line or "\\" in line:
            raise ConfigError(f"line {n}: expansion or escape syntax is not accepted")
        m = LINE.match(line)
        if not m:
            raise ConfigError(f"line {n}: not a NAME=value assignment")
        name, v = m.group(1), m.group(2)
        for rx in (SINGLE, DOUBLE):
            q = rx.match(v)
            if q:
                v = q.group(1)
                break
        else:
            if not UNQUOTED.match(v):
                raise ConfigError(f"line {n}: unquoted value with shell syntax")
        if name in out:
            raise ConfigError(f"line {n}: duplicate assignment")
        out[name] = v
    return out


def read_secret_file(path: str, what: str, forbidden_bits: int) -> str:
    """The file's text. It must be a regular file (no symlink) owned by us, with none of forbidden_bits set: whoever
    can write it chooses the token the gateway sends (and a token file must not be readable by others either)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ConfigError(f"cannot open the {what} ({type(exc).__name__}; a symlink is refused)") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"the {what} is not a regular file")
        if st.st_uid != os.getuid():
            raise ConfigError(f"the {what} is not owned by this user")
        if st.st_mode & forbidden_bits:
            raise ConfigError(f"the {what} mode is {oct(st.st_mode & 0o777)}; bits {oct(forbidden_bits)} must be clear")
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ConfigError(f"the {what} is larger than {MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"the {what} is not UTF-8") from None


def token_from_file(text: str) -> str:
    """Design (b): the whole file is the token, with at most one trailing LF or CRLF. Nothing else is trimmed."""
    for end in ("\r\n", "\n"):
        if text.endswith(end):
            return text[: -len(end)]
    return text


def check_flags(flags) -> list:
    """The gateway flags, if every one is an exact phase-1 spelling with its value as a separate argument."""
    i = 0
    while i < len(flags):
        f = flags[i]
        if f not in ALLOWED_FLAGS:
            raise ConfigError(f"gateway argument {i + 1} is not an allowed phase-1 flag")
        if ALLOWED_FLAGS[f]:
            if i + 1 >= len(flags) or flags[i + 1].startswith("-") or not flags[i + 1]:
                raise ConfigError(f"gateway argument {i + 1} needs a value")
            i += 1
        i += 1
    return list(flags)


def build(argv, environ) -> tuple:
    """(program, argv, env) for os.execve, or ConfigError."""
    ap = argparse.ArgumentParser(prog="launch.py", allow_abbrev=False)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--provider-env")
    src.add_argument("--provider-token-file")
    ap.add_argument("--python", default="/usr/bin/python3")
    ap.add_argument("gateway_flags", nargs=argparse.REMAINDER)
    o = ap.parse_args(argv)
    flags = check_flags(o.gateway_flags[1:] if o.gateway_flags[:1] == ["--"] else o.gateway_flags)
    if o.provider_env:
        tok = parse_env(read_secret_file(o.provider_env, "provider env file", 0o022)).get(TOKEN_NAME, "")
    else:
        tok = token_from_file(read_secret_file(o.provider_token_file, "provider token file", 0o077))
    if not tok:
        raise ConfigError(f"no non-empty {TOKEN_NAME} in the credential source")
    if not (tok.isascii() and tok.isprintable()) or " " in tok:
        raise ConfigError(f"{TOKEN_NAME} is not printable ASCII without spaces")
    env = {"PATH": FIXED_PATH, TOKEN_NAME: tok}
    for k in FROM_PLIST:
        v = environ.get(k, "")
        if not v:
            raise ConfigError(f"{k} is not set (the plist sets it)")
        env[k] = v
    if not env["CORTADOS_PROVIDER_URL"].startswith("http://"):
        raise ConfigError("CORTADOS_PROVIDER_URL is not an http:// URL")
    return o.python, [o.python, *CHILD_OPTS, "-c", bootstrap(), *flags], env


def bootstrap() -> str:
    """The child's -c program: put this checkout first on sys.path, then run the gateway as __main__ (runpy keeps
    sys.argv[1:], the flags, for its argparse). Reason (review r4): with -B alone Python still imports sitecustomize /
    usercustomize from the user site under HOME, code that would see the token and could edit sys.argv; -I -S turn off
    the user site, PYTHON* variables, the cwd entry and site itself, so the explicit path below is the only addition."""
    return ("import sys; sys.path.insert(0, %r); import runpy; "
            "runpy.run_module('cortados_router.bot_gateway', run_name='__main__', alter_sys=True)" % REPO)


def main(argv=None, environ=None, execve=os.execve) -> int:
    try:
        prog, args, env = build(sys.argv[1:] if argv is None else argv, os.environ if environ is None else environ)
    except ConfigError as exc:
        sys.stderr.write(f"bot-gateway launch: refusing to start: {exc}\n")
        return EX_CONFIG
    try:
        execve(prog, args, env)
    except OSError as exc:
        sys.stderr.write(f"bot-gateway launch: cannot exec the gateway ({type(exc).__name__})\n")
        return EX_CONFIG
    return 0                     # only reached with a fake execve (tests)


if __name__ == "__main__":
    sys.exit(main())
