"""Bot gateway launcher (the LaunchAgent's program; integrations/bot-gateway/install.sh writes the plist that runs it).

Takes the provider token for route_task out of ~/.cortados/provider.env WITHOUT executing that file, then execs the
gateway with an explicit environment. Reason (review r2): sourcing provider.env as shell code lets anything in it
(set -x, traps, command substitution, readonly or exported variables) print the token, pass it to another program or
add variables to the gateway's environment. Here the file is data:

  - accepted lines: blank, `# comment`, and `[export ]NAME=value`, where value is unquoted, 'single-quoted' or
    "double-quoted"; nothing is expanded;
  - `$`, a backtick, a backslash or any other line (a command, a function, a trap, set -x) refuses the whole file;
  - only CORTADOS_PROVIDER_TOKEN is used; it must be non-empty printable ASCII with no spaces, and appear once.

Every config failure exits 78 (EX_CONFIG) with a message that names a line NUMBER at most, never a line's content or
the token. The gateway gets exactly HOME, a fixed PATH, CORTADOS_HOME, CORTADOS_PROVIDER_URL (the three from the plist)
and CORTADOS_PROVIDER_TOKEN; nothing else is inherited. The token goes in the environment only, never in argv.

Run (by launchd): /usr/bin/python3 -I launch.py --provider-env FILE [--python /usr/bin/python3] -- <gateway flags>
Stdlib only.
"""
import argparse
import os
import re
import stat
import sys

EX_CONFIG = 78
TOKEN_NAME = "CORTADOS_PROVIDER_TOKEN"
FIXED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
FROM_PLIST = ("HOME", "CORTADOS_HOME", "CORTADOS_PROVIDER_URL")   # set by the plist (HOME by launchd); not secrets
MAX_BYTES = 64 * 1024            # provider.env is a few lines; a bigger file is not one we wrote
# Flags the gateway must never get from this launcher: phase 1 is route_task only (owner decision 2026-11-03).
FORBIDDEN_FLAGS = ("--enable-start-task",)

LINE = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UNQUOTED = re.compile(r"^[^\s'\"`$\\;&|<>(){}*?\[\]!#~]*$")
SINGLE = re.compile(r"^'([^']*)'$")
DOUBLE = re.compile(r"^\"([^\"\\`$]*)\"$")


class ConfigError(Exception):
    pass


def parse_env(text: str) -> dict:
    """NAME -> value for the accepted line forms; ConfigError (with a line number only) for anything else."""
    out = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
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
            raise ConfigError(f"line {n}: {name} is assigned twice")
        out[name] = v
    return out


def read_provider_env(path: str) -> str:
    """The file's text. It must be a regular file (no symlink) owned by us and not writable by group or others:
    whoever can write it chooses the token the gateway sends."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ConfigError(f"cannot open the provider env file ({type(exc).__name__}; a symlink is refused)") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError("the provider env file is not a regular file")
        if st.st_uid != os.getuid():
            raise ConfigError("the provider env file is not owned by this user")
        if st.st_mode & 0o022:
            raise ConfigError("the provider env file is writable by group or others")
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ConfigError(f"the provider env file is larger than {MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("the provider env file is not UTF-8") from None


def build(argv, environ) -> tuple:
    """(program, argv, env) for os.execve, or ConfigError."""
    ap = argparse.ArgumentParser(prog="launch.py")
    ap.add_argument("--provider-env", required=True)
    ap.add_argument("--python", default="/usr/bin/python3")
    ap.add_argument("gateway_flags", nargs=argparse.REMAINDER)
    o = ap.parse_args(argv)
    flags = o.gateway_flags[1:] if o.gateway_flags[:1] == ["--"] else o.gateway_flags
    for f in flags:
        if f in FORBIDDEN_FLAGS:
            raise ConfigError(f"{f} is not allowed in phase 1")
    tok = parse_env(read_provider_env(o.provider_env)).get(TOKEN_NAME, "")
    if not tok:
        raise ConfigError(f"the provider env file assigns no non-empty {TOKEN_NAME}")
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
    return o.python, [o.python, "-B", "-m", "cortados_router.bot_gateway", *flags], env


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
