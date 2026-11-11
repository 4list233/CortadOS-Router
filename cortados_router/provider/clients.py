"""Scoped per-client tokens for the provider: a client holds a token that opens only the endpoints its scopes name.

The owner token (CORTADOS_PROVIDER_TOKEN) is untouched by this module: it is checked first, exactly as before, and keeps
every endpoint. A client token is a second, narrower credential for a reader that must not route or spend (the Claw3D
office adapter today; the bot gateway later, see shared-connections step 9). A leak of a client's host then exposes what
its scopes allow and nothing more.

Clients live in a JSON file (CORTADOS_PROVIDER_CLIENTS, default ~/.config/cortados/clients.json):
    {"version": 1, "clients": [{"name": "claw3d", "token_sha256": "<64 hex>", "scopes": ["office:read"]}]}
A `live:heartbeat` client is bound to ONE host: it may post heartbeats for that host only (403 for any other), so a leaked
heartbeat token can neither speak for another machine nor push real hosts off the board. The host is the entry's
`"host"` field ([a-z][a-z0-9-]{0,31}, the exec-host slug), else the client's own name when that is a valid host name
(a client named "studio" posts for "studio"); a heartbeat client with neither is refused like any bad entry. `host` on a
client without live:heartbeat is refused too (it would mean nothing). The owner token is never bound.
Only the SHA-256 of each token is stored, so the file is not a secret by itself; the token lives in the client's own
0600 token file. The file is re-read when its size or mtime changes, so adding or revoking a client needs no restart.
A file that does not parse, or names an unknown scope, loads NO clients (fail closed) and says why in the log: a typo
must never widen access, and a half-loaded file would be a silent partial fallback.

CLI (never prints a token):
    python3 -B -m cortados_router.provider.clients add <name> --scope office:read --token-file <path>
    python3 -B -m cortados_router.provider.clients add studio-s1 --scope live:heartbeat --host studio --token-file <path>
    python3 -B -m cortados_router.provider.clients remove <name>
    python3 -B -m cortados_router.provider.clients list
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from cortados_router.compat import getenv

logger = logging.getLogger(__name__)

DEFAULT_CLIENTS_FILE = "~/.config/cortados/clients.json"
CLIENTS_ENV = "CORTADOS_PROVIDER_CLIENTS"
OFFICE_PATH = "/v1/cortados/office"
LIVE_PATH = "/v1/cortados/live"                       # provider/live.py LIVE_PATH (a test keeps the two equal)
LIVE_HEARTBEAT_PATH = "/v1/cortados/live/heartbeat"   # provider/live.py HEARTBEAT_PATH
# Scope -> the (method, path) pairs it opens. Exact paths only (no prefixes): a prefix would open whatever is added
# under it later. A new reader gets a new scope here, reviewed, never a wildcard.
SCOPES: dict[str, frozenset[tuple[str, str]]] = {
    "office:read": frozenset({("GET", OFFICE_PATH)}),
    # the live snapshot (dashboard, pulse pane): read-only metadata, like the office view
    "live:read": frozenset({("GET", LIVE_PATH)}),
    # write-only: a host posting its job states (the Studio's S1 jobs) without holding the owner token, which routes
    "live:heartbeat": frozenset({("POST", LIVE_HEARTBEAT_PATH)}),
}
# A client name lands in log lines: short and plain, so it can never carry a token or a path.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_HOST_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")        # live._HOST_RE / exec_hosts.HOST_NAME_RE
HEARTBEAT_SCOPE = "live:heartbeat"
# Few clients exist (one today, two planned); a file listing more is not a reviewed list.
MAX_CLIENTS = 32
# 32 entries need under 8 KiB in the CLI's format; 64 KiB leaves ample formatting headroom while bounding auth reads.
MAX_CLIENTS_BYTES = 64 * 1024
# Same floor as the owner-token tooling (child_tasks.MIN_TOKEN_LEN is 8); minted tokens are 32 bytes of randomness.
MIN_TOKEN_LEN = 16


class ClientsError(ValueError):
    """The clients file is unusable; the message names the problem, never a token or hash."""


@dataclass(frozen=True)
class Client:
    name: str
    scopes: frozenset[str]
    host: str | None = None                         # the one host a live:heartbeat client may post for (see above)

    def allows(self, method: str, path: str) -> bool:
        return any((method, path) in SCOPES[s] for s in self.scopes)


def clients_path() -> Path:
    return Path(os.path.expanduser(getenv(CLIENTS_ENV) or DEFAULT_CLIENTS_FILE))


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse(raw: str) -> list[tuple[str, Client]]:
    """(token_sha256, Client) for every entry, or ClientsError for any bad entry (the whole file is refused)."""
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ClientsError("the clients file is not JSON") from None
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("clients"), list):
        raise ClientsError('the clients file must be {"version": 1, "clients": [...]}')
    entries = doc["clients"]
    if len(entries) > MAX_CLIENTS:
        raise ClientsError(f"the clients file lists more than {MAX_CLIENTS} clients")
    out: list[tuple[str, Client]] = []
    names, digests = set(), set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ClientsError(f"client #{i} is not an object")
        name, digest, scopes = e.get("name"), e.get("token_sha256"), e.get("scopes")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ClientsError(f"client #{i} has no valid name ([a-z0-9][a-z0-9_-]{{0,31}})")
        if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
            raise ClientsError(f"client {name}: token_sha256 is not 64 lowercase hex characters")
        if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
            raise ClientsError(f"client {name}: scopes must be a non-empty list of names")
        unknown = sorted(set(scopes) - set(SCOPES))
        if unknown:
            # Refused, not skipped: a scope this provider does not know is a typo or a newer file, and guessing would
            # either drop access silently or grant something unreviewed.
            raise ClientsError(f"client {name}: unknown scope(s) {', '.join(unknown)} (known: {', '.join(sorted(SCOPES))})")
        host = e.get("host")
        if HEARTBEAT_SCOPE in scopes:
            if host is None and _HOST_RE.fullmatch(name):
                host = name
            if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
                raise ClientsError(f"client {name}: a {HEARTBEAT_SCOPE} client needs a host ([a-z][a-z0-9-]{{0,31}}), "
                                   "in its \"host\" field or as its name")
        elif host is not None:
            raise ClientsError(f"client {name}: \"host\" applies only to a {HEARTBEAT_SCOPE} client")
        if name in names or digest in digests:
            raise ClientsError(f"client {name}: duplicate name or token")
        names.add(name)
        digests.add(digest)
        out.append((digest, Client(name=name, scopes=frozenset(scopes), host=host)))
    return out


def _read_clients(p: Path) -> str:
    """Bound both the initial size and the read: a file can grow after stat (fail closed in either case)."""
    if p.stat().st_size > MAX_CLIENTS_BYTES:
        raise ClientsError("the clients file exceeds the 64 KiB limit")
    with p.open("rb") as f:
        raw = f.read(MAX_CLIENTS_BYTES + 1)
    if len(raw) > MAX_CLIENTS_BYTES:
        raise ClientsError("the clients file exceeds the 64 KiB limit")
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise ClientsError("the clients file is not UTF-8") from None


class ClientTokens:
    """The loaded clients, re-read when the file changes. `match(token)` -> the Client the token belongs to, or None.
    A missing file means no clients (the ordinary state of a provider without readers); a bad file means no clients
    AND an error log line on every change of the file, never a partial load."""

    def __init__(self, path: Path | None = None, owner_token: str | None = None):
        self._path = path
        self._owner_digest = token_digest(owner_token) if owner_token else None
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._entries: list[tuple[str, Client]] = []
        self.error: str | None = None

    @property
    def path(self) -> Path:
        return self._path or clients_path()

    def _refresh_locked(self) -> None:
        p = self.path
        try:
            st = p.stat()
            sig = (str(p), st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = (str(p), None)
        except OSError as exc:
            sig = (str(p), "stat", type(exc).__name__)
        if sig == self._sig:
            return
        self._sig = sig
        self._entries, self.error = [], None
        if sig[1] is None:
            return
        try:
            entries = parse(_read_clients(p))
            if self._owner_digest and any(d == self._owner_digest for d, _ in entries):
                # A client entry holding the owner token would make the owner a scoped client on paper; refuse the file.
                raise ClientsError("a client's token is the owner token")
        except (OSError, ClientsError) as exc:
            self.error = str(exc) if isinstance(exc, ClientsError) else f"cannot read the clients file ({type(exc).__name__})"
            logger.error("provider clients: %s; NO scoped client token is accepted until it is fixed", self.error)
            return
        self._entries = entries
        logger.info("provider clients: %d loaded (%s)", len(entries), ", ".join(c.name for _, c in entries) or "none")
        bindings = [f"{c.name} -> {c.host}" for _, c in entries if c.host]
        if bindings:
            # Which host each heartbeat client may post for (names only, never a token or digest): a heartbeat refused
            # with 403, or a host card that never fills, is read against this line.
            logger.info("provider clients: heartbeat bindings: %s", ", ".join(bindings))

    def match(self, token: str) -> Client | None:
        if not token or len(token) < MIN_TOKEN_LEN:
            return None
        digest = token_digest(token)
        with self._lock:
            self._refresh_locked()
            entries = list(self._entries)
        found = None
        for d, c in entries:                        # every entry compared, constant time per entry
            if hmac.compare_digest(d, digest):
                found = c
        return found


# ------------------------------------------------------------------ CLI
def _load_doc(p: Path) -> dict:
    if not p.exists():
        return {"version": 1, "clients": []}
    raw = _read_clients(p)
    parse(raw)                                      # refuse to edit a file the provider would refuse
    return json.loads(raw)


def _write_private(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage the provider's scoped client tokens (never prints a token).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="mint a token into --token-file (0600, must not exist) and register its hash")
    a.add_argument("name")
    a.add_argument("--scope", action="append", required=True, choices=sorted(SCOPES))
    a.add_argument("--token-file", required=True)
    a.add_argument("--host", help=f"the host a {HEARTBEAT_SCOPE} client posts for (default: its name)")
    r = sub.add_parser("remove", help="revoke a client")
    r.add_argument("name")
    sub.add_parser("list", help="client names and scopes")
    args = ap.parse_args(argv)
    p = clients_path()
    try:
        # Lock the stable sibling, not clients.json: replacement changes that file's inode.
        # Keep load, candidate validation and replacement in one critical section to preserve revocations.
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p.with_name(p.name + ".lock")), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _locked_main(args, p)
    except (OSError, ClientsError) as exc:
        print(f"clients: {p}: {exc}", file=sys.stderr)
        return 1


def _locked_main(args, p: Path) -> int:
    doc = _load_doc(p)
    if args.cmd == "list":
        for e in doc["clients"]:
            print(f"{e['name']}: {', '.join(e['scopes'])}" + (f" (host {e['host']})" if e.get("host") else ""))
        return 0
    if args.cmd == "remove":
        kept = [e for e in doc["clients"] if e["name"] != args.name]
        if len(kept) == len(doc["clients"]):
            print(f"clients: no client named {args.name}", file=sys.stderr)
            return 1
        doc["clients"] = kept
        candidate = json.dumps(doc, indent=1) + "\n"
        parse(candidate)
        _write_private(p, candidate)
        print(f"clients: removed {args.name}")
        return 0
    if not _NAME_RE.fullmatch(args.name) or any(e["name"] == args.name for e in doc["clients"]):
        print(f"clients: {args.name!r} is not a valid new client name", file=sys.stderr)
        return 1
    if len(doc["clients"]) >= MAX_CLIENTS:
        raise ClientsError(f"cannot add more than {MAX_CLIENTS} clients")
    token = secrets.token_urlsafe(32)
    entry = {"name": args.name, "token_sha256": token_digest(token), "scopes": sorted(set(args.scope))}
    if args.host is not None:
        entry["host"] = args.host
    doc["clients"].append(entry)
    candidate = json.dumps(doc, indent=1) + "\n"
    parse(candidate)                                # reject the full candidate BEFORE creating the token file
    tf = Path(os.path.expanduser(args.token_file))
    tf.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(tf), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never overwrite a live token file
    except FileExistsError:
        print(f"clients: {tf} already exists; remove it first", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    _write_private(p, candidate)
    print(f"clients: added {args.name} ({', '.join(sorted(set(args.scope)))}); token written to {tf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
