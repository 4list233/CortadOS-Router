"""Execution hosts for child tasks: the machine a `start_task` child really runs on.

Owner decision 2026-11-08 (issues #89, #86): every child task runs on the always-on MacBook Air, whichever machine
asks. The MCP server (subagent_mcp.py) may run on the Pro, the Air itself or another machine; this module tells it
which configured host a task belongs to, whether that host is this very machine (then the child is launched locally,
exactly as before) and, if not, how to reach it: one `ssh` call running the SAME module on the other side
(`python3 cortados_router/child_tasks.py --remote`), JSON request on stdin, JSON reply on stdout.

Configuration: ~/.config/cortados/exec-hosts.json (CORTADOS_EXEC_HOSTS_FILE overrides the path), shaped like
tables/exec-hosts.example.json, which holds the Air entry ready to copy. The file is per machine (it names SSH
destinations and another machine's paths, which this repository never ships as live config: see tables/*.example.json);
without it start_task refuses with the path to create, it never quietly runs the child on the requesting machine.

Stdlib only. Tests replace TRANSPORT; nothing here opens SSH in tests.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
from pathlib import Path

CONFIG_FILE = "~/.config/cortados/exec-hosts.json"
EXAMPLE_FILE = Path(__file__).resolve().parent.parent / "tables" / "exec-hosts.example.json"
REMOTE_SCRIPT = "cortados_router/child_tasks.py"     # relative to the host's `router` checkout
PROTOCOL = 1                        # bumped when the request/reply shape changes; both checkouts must agree
# A host name is a config key, a stub-file value and part of every message, so it is kept to a short lowercase slug.
HOST_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
# The hosts sit on the tailnet: a connection not made in 10 s is a host that is down or unreachable, not a slow one.
SSH_CONNECT_TIMEOUT_S = 10
# start/status/list/cancel are small JSON exchanges (no clone, no wait): the MCP client gives the whole call 60 s
# (child_tasks.CLIENT_REQUEST_TIMEOUT_S), so a remote call that has not answered in 30 s is reported, not waited on.
SSH_RPC_TIMEOUT_S = 30
STDERR_TAIL = 400                   # of ssh's stderr shown in an error: enough for the cause, not a transcript
LOCAL_SENTINEL = "local"            # a host entry with no `ssh` runs children on the machine that runs the server


class HostError(Exception):
    """Configuration or protocol problem; the message is safe to show the caller (no secrets are ever in it)."""


class HostUnreachable(HostError):
    """The host did not answer: ssh could not connect, timed out, or the remote command produced no reply.
    `ambiguous` is True when the request may have reached the host and run there (a timeout once the session was up,
    or a remote command that ran but left no reply): a `start` may then have launched a child nobody heard about.
    False for failures before anything was sent (ssh could not be run, or exited 255 on connect/authentication)."""

    def __init__(self, msg: str, ambiguous: bool = False):
        super().__init__(msg)
        self.ambiguous = ambiguous


def short_hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


class Host:
    """One configured execution host (validated on load so a bad entry fails at start_task with a clear message)."""

    FIELDS = ("ssh", "hostnames", "root", "router", "python", "claude", "path", "gui", "provider_url")

    def __init__(self, name: str, cfg: dict):
        if not isinstance(name, str) or not HOST_NAME_RE.fullmatch(name):
            raise HostError(f"exec host name {name!r} must match {HOST_NAME_RE.pattern} (a short lowercase slug)")
        if not isinstance(cfg, dict):
            raise HostError(f"exec host {name!r}: the entry must be an object")
        unknown = sorted(set(cfg) - set(self.FIELDS) - {"_comment"})
        if unknown:
            raise HostError(f"exec host {name!r}: unknown field(s) {', '.join(unknown)}; known: {', '.join(self.FIELDS)}")
        self.name = name
        self.ssh = self._opt_str(cfg, "ssh")
        hn = cfg.get("hostnames", [])
        if not isinstance(hn, list) or not all(isinstance(h, str) and h.strip() for h in hn):
            raise HostError(f"exec host {name!r}: hostnames must be a list of short host names")
        self.hostnames = [h.strip().split(".")[0].lower() for h in hn]
        self.root = self._opt_path(cfg, "root")
        self.router = self._opt_path(cfg, "router")
        self.python = self._opt_str(cfg, "python") or "python3"
        self.claude = self._opt_path(cfg, "claude")
        self.path = self._opt_str(cfg, "path")
        self.gui = cfg.get("gui", False)
        if not isinstance(self.gui, bool):
            raise HostError(f"exec host {name!r}: gui must be true or false")
        self.provider_url = self._opt_str(cfg, "provider_url")
        if self.provider_url and not self.provider_url.startswith(("http://", "https://")):
            raise HostError(f"exec host {name!r}: provider_url must be an http:// or https:// URL")
        if self.ssh and not self.router:
            # The remote side runs this repository's own code; without its checkout path there is no command to run.
            raise HostError(f"exec host {name!r}: an ssh host needs `router`, the absolute path of its cortados-router "
                            "checkout")
        if self.ssh and (self.ssh.startswith("-") or any(c.isspace() for c in self.ssh)):
            raise HostError(f"exec host {name!r}: ssh must be a destination (an ssh_config alias or user@host), not options")

    @staticmethod
    def _opt_str(cfg, key):
        v = cfg.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise HostError(f"exec host field {key!r} must be a non-empty string")
        return v.strip()

    @classmethod
    def _opt_path(cls, cfg, key):
        v = cls._opt_str(cfg, key)
        if v is not None and not os.path.isabs(v):
            raise HostError(f"exec host field {key!r} must be an absolute path (got {v!r})")
        return v

    def is_local(self) -> bool:
        """This machine: an entry with no ssh destination, or one whose hostnames name this machine."""
        return not self.ssh or short_hostname() in self.hostnames

    def describe(self) -> str:
        return f"'{self.name}'" + (f" (ssh {self.ssh})" if self.ssh else " (this machine)")

    def remote_command(self) -> str:
        """The command ssh runs on the host: this repository's module in --remote mode, from the host's checkout."""
        return f"{shlex.quote(self.python)} -B {shlex.quote(os.path.join(self.router, REMOTE_SCRIPT))} --remote"


class Hosts:
    def __init__(self, default: str, hosts: dict):
        self.default = default
        self.hosts = hosts

    def resolve(self, name=None, env=None) -> Host:
        """The host for a start_task call: `name` if given, else CORTADOS_EXEC_HOST, else the file's default."""
        env = os.environ if env is None else env
        chosen = name if name not in (None, "") else (env.get("CORTADOS_EXEC_HOST") or self.default)
        if not isinstance(chosen, str) or chosen not in self.hosts:
            raise HostError(f"unknown execution host {chosen!r}; configured: {', '.join(sorted(self.hosts))} "
                            f"(see {config_path(env)})")
        return self.hosts[chosen]

    def local_entry(self):
        """The entry naming this machine (by hostname, or the ssh-less entry), or None."""
        me = short_hostname()
        for h in self.hosts.values():
            if h.ssh and me in h.hostnames:
                return h
        for h in self.hosts.values():
            if not h.ssh:
                return h
        return None


def config_path(env=None) -> str:
    env = os.environ if env is None else env
    p = env.get("CORTADOS_EXEC_HOSTS_FILE") or CONFIG_FILE
    if p.startswith("~/"):
        p = os.path.join(env.get("HOME") or os.path.expanduser("~"), p[2:])
    return p


def load(path=None, env=None) -> Hosts:
    path = path or config_path(env)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise HostError(f"no execution hosts are configured: create {path} (copy {EXAMPLE_FILE}, which holds the Air "
                        "entry) or set CORTADOS_EXEC_HOSTS_FILE. Child tasks run on the configured host, never "
                        "silently on this machine.") from None
    except (OSError, ValueError, UnicodeError) as exc:
        raise HostError(f"cannot read the execution hosts file {path}: {type(exc).__name__}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("hosts"), dict) or not raw["hosts"]:
        raise HostError(f"{path}: expected {{\"default\": <name>, \"hosts\": {{<name>: {{...}}}}}} with at least one host")
    hosts = {name: Host(name, cfg) for name, cfg in raw["hosts"].items()}
    default = raw.get("default")
    if default not in hosts:
        raise HostError(f"{path}: `default` must name one of the hosts ({', '.join(sorted(hosts))}), got {default!r}")
    return Hosts(default, hosts)


def ssh_argv(host: Host) -> list:
    """BatchMode: never prompt (no one is there); StrictHostKeyChecking=yes: an unknown or changed key is a refusal, not
    a prompt, as cortados_router.local_resources does for its sampling hosts."""
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
            "-o", "StrictHostKeyChecking=yes", "--", host.ssh, host.remote_command()]


def run_ssh(argv: list, stdin: bytes, timeout_s: float):
    """(returncode, stdout, stderr) of one ssh invocation; the TRANSPORT tests replace."""
    p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout_s)
    return p.returncode, p.stdout, p.stderr


TRANSPORT = run_ssh


def _tail(b: bytes) -> str:
    return b.decode("utf-8", "replace").strip()[-STDERR_TAIL:]


def call(host: Host, op: str, args: dict, timeout_s: float = SSH_RPC_TIMEOUT_S, transport=None):
    """Run one child-task operation on `host` and return its result. HostUnreachable when the host does not answer;
    HostError for a protocol or remote-side error (the remote's TaskError text). Nothing is retried elsewhere."""
    if host.is_local():
        raise HostError(f"exec host {host.describe()} is this machine; nothing to call over ssh")
    transport = transport or TRANSPORT
    req = json.dumps({"v": PROTOCOL, "op": op, "host": host.name, "args": args}).encode("utf-8")
    try:
        rc, out, err = transport(ssh_argv(host), req, timeout_s)
    except subprocess.TimeoutExpired:
        # Ambiguous: ssh gives up connecting after SSH_CONNECT_TIMEOUT_S (exit 255, below), well inside timeout_s, so a
        # timeout here means the session was up and the request was sent; the host may have acted on it.
        raise HostUnreachable(f"execution host {host.describe()} did not answer {op} within {timeout_s:g} s",
                              ambiguous=True) from None
    except OSError as exc:
        raise HostUnreachable(f"cannot run ssh for execution host {host.describe()}: {type(exc).__name__}: {exc}") from None
    if rc == 255:                                       # ssh's own exit status for a connection/authentication failure
        raise HostUnreachable(f"execution host {host.describe()} is unreachable: {_tail(err) or 'ssh exited 255'}")
    text = out.decode("utf-8", "replace").strip()
    line = text.rsplit("\n", 1)[-1] if text else ""    # the reply is the last line (a login banner may precede it)
    try:
        reply = json.loads(line) if line else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        # Ambiguous: the remote command ran (ssh did not exit 255) and may have acted before failing to reply.
        raise HostUnreachable(f"execution host {host.describe()} returned no reply to {op} (exit {rc}): "
                              f"{_tail(err) or _tail(out) or 'no output'}", ambiguous=True)
    if reply.get("v") != PROTOCOL:
        raise HostError(f"execution host {host.describe()} runs a cortados-router checkout speaking protocol "
                        f"{reply.get('v')!r}, this one speaks {PROTOCOL}: update the checkout there ({host.router})")
    if not reply.get("ok"):
        raise HostError(str(reply.get("error") or f"{op} failed on {host.describe()} without a reason"))
    return reply.get("result")
