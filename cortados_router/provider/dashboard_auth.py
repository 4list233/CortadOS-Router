"""Password login for the provider's dashboard: the owner opens /dashboard with a password he sets, not the owner token.

Why: the owner token authorises everything (the model endpoints, heartbeats, every view). Pasting it into a browser
tab puts the provider's master credential into page memory and sessionStorage. A dashboard session is a narrower
credential: it opens only the dashboard's read views (live, usage, tasks, office) and the `local:switch` scope, never
/v1/messages, /v1/responses, heartbeats or anything else. The owner token keeps working exactly as before for every
client; a client that sends the token never needs a cookie.

State, both under $CORTADOS_HOME (paths.state_dir()), both written atomically with mode 0600:
- dashboard-auth.json      {"version": 1, "kdf": "scrypt", "n", "r", "p", "dklen", "salt" (hex), "hash" (hex),
                            "generation" (hex), "set_at"}, or with "kdf": "pbkdf2_sha256" and "iterations" in place
                            of n/r/p (see KDF below). Only the salted hash and its parameters; never the password.
                            `generation` is new on every set-password: a session minted under an older generation is
                            refused, so changing or clearing the password ends every session even if the sessions file
                            could not be cleared.
- dashboard-sessions.json  {"version": 1, "sessions": [{"id_sha256", "csrf", "generation", "created", "expires"}]}.
                            Only the SHA-256 of each cookie value is stored, so a restart keeps the owner logged in and
                            a copy of the file does not log anyone in. `csrf` is the per-session token the page sends
                            back in a header on every state-changing request (it is useless without the cookie).
Both files are re-read when they change on disk (inode, size, mtime), so `set-password`, `clear-password` and
`logout-all` take effect on a running provider without a restart. A file that does not parse fails closed: no session
is valid and the login answers 500 until the file is fixed or cleared (the owner token is unaffected).

KDF: scrypt (hashlib.scrypt, n=2**15, r=8, p=1, 16-byte salt, 32-byte key) wherever the Python has it. The Air's provider
runs on Apple's CommandLineTools Python 3.9, built against LibreSSL, which has NO hashlib.scrypt (checked 2026-11-13);
there set-password records PBKDF2-HMAC-SHA256 at 600,000 iterations (OWASP's 2023 figure; 0.17 s on the Air), says so,
and the record names the KDF, so this is never a silent downgrade. A scrypt record on a Python without scrypt fails
closed (the login answers 500 and the log says to rerun set-password with the provider's own python3).

Login rate limit (LoginThrottle): per client address and globally, decided BEFORE the password is checked, and
verification is serialised (one scrypt at a time: each takes ~32 MiB and tens of ms). Behind Tailscale Serve every
request arrives from loopback, so the client address is Serve's appended last X-Forwarded-For hop, trusted only from a
loopback peer (the bot gateway's rule, bot_gateway.client_address).

CLI, run on the provider host in a real terminal (never prints the password or the hash):
    python3 -B -m cortados_router.provider.dashboard_auth set-password [--stdin]
    python3 -B -m cortados_router.provider.dashboard_auth clear-password
    python3 -B -m cortados_router.provider.dashboard_auth status
    python3 -B -m cortados_router.provider.dashboard_auth logout-all
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("cortados.provider.dashboard_auth")

AUTH_FILE = "dashboard-auth.json"
SESSIONS_FILE = "dashboard-sessions.json"
COOKIE_NAME = "cortados_dash"
CSRF_HEADER = "x-cortados-csrf"

# scrypt at n=2**15, r=8, p=1: 32 MiB and roughly 50-100 ms per check, which makes an offline guess against
# a stolen hash expensive while a login stays instant. Recorded in the file, so a later change still verifies old hashes.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN, SALT_BYTES = 2 ** 15, 8, 1, 32, 16
# OpenSSL needs a little over 128*n*r bytes; hashlib's default maxmem (32 MiB) is exactly 128*n*r and refuses it.
SCRYPT_MAXMEM = 64 * 1024 * 1024
# Parameters accepted from the file: a tampered or corrupt file must not make one login allocate gigabytes.
_N_RANGE, _R_RANGE, _P_RANGE = (2 ** 14, 2 ** 17), (1, 16), (1, 4)
# PBKDF2-HMAC-SHA256 where hashlib has no scrypt (see KDF above). 600,000 is OWASP's 2023 minimum for this hash; the
# range bounds a tampered file both ways (too cheap to matter, or a login that takes minutes of CPU).
PBKDF2_ITERATIONS = 600_000
_ITER_RANGE = (100_000, 10_000_000)


def scrypt_available() -> bool:
    return hasattr(hashlib, "scrypt")
# 12 characters minimum: with scrypt and the throttle below, a 12-character password is out of online and offline
# reach; shorter ones are not. The cap bounds what one login request makes the provider hash.
MIN_PASSWORD_LEN, MAX_PASSWORD_LEN = 12, 1024

# 30 days: the owner opens the dashboard from his own devices; a month between logins is convenience without a
# forever-cookie. Logout or logout-all ends a session sooner.
SESSION_TTL_S = 30 * 24 * 3600
# A handful of browsers and devices; past this the oldest session is dropped at the next login (bounded file).
MAX_SESSIONS = 20
# token_urlsafe(32) is 43 characters; anything longer or outside the alphabet is not one of ours and is not hashed.
_COOKIE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_HEX_RE = re.compile(r"[0-9a-f]+")
MAX_FILE_BYTES = 256 * 1024                 # both files are tiny; a huge one is not ours and is refused unread

# Per address: 5 failures within 15 minutes lock that address out for 15 minutes, doubling on each further lockout up
# to 4 hours (a typo streak costs minutes; a guesser gets a handful of tries per hour). A success resets the address.
IP_FAILURES, IP_WINDOW_S = 5, 15 * 60
IP_LOCKOUT_S, IP_LOCKOUT_MAX_S = 15 * 60, 4 * 3600
# Globally: 30 failures within 15 minutes from any mix of addresses refuse every attempt until the oldest ages out, so
# distributed guessing runs at about two guesses a minute in total. The cost: while the cap holds, the owner cannot log
# in either (the owner token still opens everything). Checked before the password, so a capped login hashes nothing.
GLOBAL_FAILURES, GLOBAL_WINDOW_S = 30, 15 * 60
# Bounds the per-address table under address churn; an address with an active lockout is never evicted for a new one.
MAX_TRACKED_ADDRESSES = 4096
LOOPBACK = ("127.0.0.1", "::1", "localhost")

# Scopes a dashboard session carries, and the exact (method, path) pairs each opens (no prefixes, as clients.SCOPES:
# a prefix would open whatever is added under it later). The owner token is not limited by these.
LIVE_PATH, USAGE_PATH, TASKS_PATH, OFFICE_PATH = ("/v1/cortados/live", "/v1/cortados/usage", "/v1/cortados/tasks",
                                                  "/v1/cortados/office")
# The local-model switch (branch local-switch-remote): named now so the endpoints can admit a session once both merge.
LOCAL_PATH, LOCAL_SWITCH_PATH = "/v1/cortados/local", "/v1/cortados/local/switch"
SESSION_SCOPES: dict[str, frozenset[tuple[str, str]]] = {
    "dashboard:read": frozenset({("GET", LIVE_PATH), ("GET", USAGE_PATH), ("GET", TASKS_PATH), ("GET", OFFICE_PATH)}),
    "local:switch": frozenset({("GET", LOCAL_PATH), ("POST", LOCAL_SWITCH_PATH)}),
}
DEFAULT_SESSION_SCOPES = frozenset(SESSION_SCOPES)
SAFE_METHODS = ("GET", "HEAD")


class AuthFileError(ValueError):
    """A dashboard auth or sessions file is unusable; the message names the problem, never a secret."""


def state_dir() -> Path:
    from cortados_router import paths
    return paths.state_dir()


def auth_path() -> Path:
    return state_dir() / AUTH_FILE


def sessions_path() -> Path:
    return state_dir() / SESSIONS_FILE


# ------------------------------------------------------------------ files
def write_private(p: Path, doc: dict) -> None:
    """Write `doc` to `p` atomically with mode 0600: a unique temp file in the same directory (mkstemp creates 0600),
    fsynced, then renamed over `p`. A reader sees the old file or the new one, never a partial one or a wider mode."""
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def file_lock(p: Path):
    """Exclusive lock on the stable sibling `<name>.lock` (the file itself is replaced, so its inode changes): the CLI and
    the provider never interleave a read-modify-write of the same file."""
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(p.with_name(p.name + ".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_json(p: Path) -> tuple[tuple | None, dict | None]:
    """(stat key, document) or (None, None) when the file does not exist. AuthFileError when it is not a regular file,
    too large, or not a JSON object."""
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(st.st_mode):
        raise AuthFileError(f"{p.name} is not a regular file")
    if st.st_size > MAX_FILE_BYTES:
        raise AuthFileError(f"{p.name} is larger than {MAX_FILE_BYTES} bytes")
    with open(p, "rb") as f:
        raw = f.read(MAX_FILE_BYTES + 1)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise AuthFileError(f"{p.name} is not JSON") from None
    if not isinstance(doc, dict):
        raise AuthFileError(f"{p.name} is not a JSON object")
    return (st.st_ino, st.st_size, st.st_mtime_ns), doc


def _stat_key(p: Path) -> tuple | None:
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


# ------------------------------------------------------------------ password
def hash_password(password: str) -> dict:
    """A new password record (a fresh salt and generation). The password itself is never stored or returned."""
    check_password_shape(password)
    salt = secrets.token_bytes(SALT_BYTES)
    base = {"version": 1, "dklen": SCRYPT_DKLEN, "salt": salt.hex(), "generation": secrets.token_hex(16),
            "set_at": int(time.time())}
    if scrypt_available():
        key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                             maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN)
        return dict(base, kdf="scrypt", n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, hash=key.hex())
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, SCRYPT_DKLEN)
    return dict(base, kdf="pbkdf2_sha256", iterations=PBKDF2_ITERATIONS, hash=key.hex())


def check_password_shape(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_LEN} characters")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"the password must be at most {MAX_PASSWORD_LEN} characters")


def validate_record(doc: dict) -> dict:
    """The password record if every field is in range, else AuthFileError (fail closed: no login with a bad file)."""
    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)
    kdf = doc.get("kdf")
    if doc.get("version") != 1 or kdf not in ("scrypt", "pbkdf2_sha256"):
        raise AuthFileError("dashboard-auth.json is not a version-1 scrypt or pbkdf2_sha256 record")
    if doc.get("dklen") != SCRYPT_DKLEN:
        raise AuthFileError("dashboard-auth.json has a key length out of range")
    if kdf == "scrypt":
        n, r, p = doc.get("n"), doc.get("r"), doc.get("p")
        if not all(is_int(v) for v in (n, r, p)) or not (_N_RANGE[0] <= n <= _N_RANGE[1]) or n & (n - 1) \
                or not (_R_RANGE[0] <= r <= _R_RANGE[1]) or not (_P_RANGE[0] <= p <= _P_RANGE[1]):
            raise AuthFileError("dashboard-auth.json has scrypt parameters out of range")
        if not scrypt_available():
            raise AuthFileError("dashboard-auth.json was hashed with scrypt, which this Python lacks (no "
                                "hashlib.scrypt); rerun set-password with the provider's own python3")
    else:
        it = doc.get("iterations")
        if not is_int(it) or not (_ITER_RANGE[0] <= it <= _ITER_RANGE[1]):
            raise AuthFileError("dashboard-auth.json has pbkdf2 iterations out of range")
    for k, size in (("salt", SALT_BYTES), ("hash", SCRYPT_DKLEN), ("generation", 16)):
        v = doc.get(k)
        if not isinstance(v, str) or len(v) != 2 * size or not _HEX_RE.fullmatch(v):
            raise AuthFileError(f"dashboard-auth.json field {k} is malformed")
    return doc


def verify_password(password: str, record: dict) -> bool:
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LEN:
        return False
    salt = bytes.fromhex(record["salt"])
    if record["kdf"] == "scrypt":
        key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=record["n"], r=record["r"], p=record["p"],
                             maxmem=SCRYPT_MAXMEM, dklen=record["dklen"])
    else:
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, record["iterations"], record["dklen"])
    return hmac.compare_digest(key, bytes.fromhex(record["hash"]))


# ------------------------------------------------------------------ request helpers
def client_address(peer: str, forwarded: str = "") -> str:
    """The login's client address: Tailscale Serve's appended last X-Forwarded-For hop, trusted only from a loopback
    peer (Serve proxies from loopback); any other peer is itself. Same rule as bot_gateway.client_address, kept here so
    the provider does not import the gateway."""
    if peer in LOOPBACK:
        candidate = (forwarded or "").rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return peer


def cookie_values(header: str | None) -> list[str]:
    """Every value of our cookie in a Cookie header (a sibling host could toss a second one: each is tried, so a tossed
    cookie can neither block nor replace the owner's)."""
    out = []
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == COOKIE_NAME:
            v = value.strip().strip('"')
            if _COOKIE_RE.fullmatch(v):
                out.append(v)
    return out[:8]


def session_cookie(value: str, max_age: int = SESSION_TTL_S) -> str:
    """The Set-Cookie value: HttpOnly (no script reads it), Secure (HTTPS only; browsers also accept it on
    http://localhost), SameSite=Strict (never sent on a cross-site request), whole origin."""
    return f"{COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Strict"


def clear_cookie() -> str:
    return f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict"


def same_origin(headers, peer: str) -> bool:
    """True when the request's Origin (else Referer) names this server: its host[:port] equals the Host header, or
    X-Forwarded-Host when the peer is loopback (Tailscale Serve). A browser cannot forge Origin, Referer or Host for a
    cross-site request, so a page on another site fails here even before the CSRF token. No Origin and no Referer: refused."""
    src = headers.get("origin") or headers.get("referer") or ""
    if not src or src == "null":
        return False
    try:
        u = urlsplit(src)
    except ValueError:
        return False
    if u.scheme not in ("https", "http") or not u.netloc:
        return False
    want = {h.strip().lower() for h in [headers.get("host") or ""] if h.strip()}
    if peer in LOOPBACK:
        want |= {h.strip().lower() for h in (headers.get("x-forwarded-host") or "").split(",") if h.strip()}
    return u.netloc.lower() in want


# ------------------------------------------------------------------ throttle
class LoginThrottle:
    """Failed-login limiter, per client address and global; bounded memory, in-memory only (a restart forgets it, which
    an attacker cannot trigger)."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.addresses: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self.global_failures: collections.deque = collections.deque()

    def _trim_global(self, now: float) -> None:
        while self.global_failures and now - self.global_failures[0] >= GLOBAL_WINDOW_S:
            self.global_failures.popleft()

    def blocked(self, address: str) -> tuple[int, str] | None:
        """(seconds until another attempt is allowed, "address" | "global") or None when an attempt may proceed."""
        now = self.clock()
        with self.lock:
            st = self.addresses.get(address)
            if st is not None and st["until"] > now:
                return max(1, int(st["until"] - now + 0.999)), "address"
            self._trim_global(now)
            if len(self.global_failures) >= GLOBAL_FAILURES:
                return max(1, int(self.global_failures[0] + GLOBAL_WINDOW_S - now + 0.999)), "global"
        return None

    def failed(self, address: str) -> int | None:
        """Record one failure; returns the address's failure count in the window, 0 when this failure locked it out,
        None when the address could not be tracked (every slot holds an active lockout; the global cap still counts it)."""
        now = self.clock()
        with self.lock:
            self._trim_global(now)
            self.global_failures.append(now)
            while len(self.global_failures) > GLOBAL_FAILURES:   # only the newest GLOBAL_FAILURES decide the cap
                self.global_failures.popleft()
            st = self.addresses.get(address)
            if st is None:
                if len(self.addresses) >= MAX_TRACKED_ADDRESSES:
                    victim = next((k for k, v in self.addresses.items() if v["until"] <= now), None)
                    if victim is None:
                        return None                 # every slot is locked out: the global cap alone limits this one
                    del self.addresses[victim]
                st = {"failures": collections.deque(), "lockout": 0, "until": 0.0}
                self.addresses[address] = st
            self.addresses.move_to_end(address)
            f = st["failures"]
            while f and now - f[0] >= IP_WINDOW_S:
                f.popleft()
            f.append(now)
            if len(f) >= IP_FAILURES:
                st["lockout"] = min(IP_LOCKOUT_MAX_S, st["lockout"] * 2 or IP_LOCKOUT_S)
                st["until"] = now + st["lockout"]
                f.clear()
                return 0
            return len(f)

    def succeeded(self, address: str) -> None:
        with self.lock:
            self.addresses.pop(address, None)


# ------------------------------------------------------------------ sessions
@dataclass(frozen=True)
class Session:
    id_sha256: str
    csrf: str
    expires: float
    scopes: frozenset = DEFAULT_SESSION_SCOPES

    def allows(self, method: str, path: str) -> bool:
        return any((method, path) in SESSION_SCOPES[s] for s in self.scopes)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_session_entry(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("id_sha256"), str) and len(e["id_sha256"]) == 64
            and bool(_HEX_RE.fullmatch(e["id_sha256"])) and isinstance(e.get("csrf"), str)
            and bool(_COOKIE_RE.fullmatch(e["csrf"])) and isinstance(e.get("generation"), str)
            and isinstance(e.get("expires"), (int, float)) and not isinstance(e.get("expires"), bool))


def _parse_sessions(doc: dict | None) -> list[dict]:
    if doc is None:
        return []
    if doc.get("version") != 1 or not isinstance(doc.get("sessions"), list):
        raise AuthFileError('dashboard-sessions.json must be {"version": 1, "sessions": [...]}')
    if not all(_valid_session_entry(e) for e in doc["sessions"]):
        raise AuthFileError("dashboard-sessions.json holds a malformed session")
    return doc["sessions"]


class DashboardAuth:
    """The provider's view of the two files plus the login throttle. Paths are resolved at call time (state_dir()), so a
    test's throwaway CORTADOS_HOME applies and a new instance over the same directory sees the same sessions."""

    def __init__(self, clock=time.time, monotonic=time.monotonic):
        self.clock = clock
        self.throttle = LoginThrottle(monotonic)
        self._lock = threading.Lock()                # guards the two caches (held for a stat or a small read only)
        self._write_lock = threading.Lock()          # orders this process's session-file rewrites (with file_lock)
        self._login_lock = threading.Lock()          # one login at a time: throttle check, scrypt, record, atomically
        self._auth_cache: tuple | None = None        # (path, stat key, record or None, error or None)
        self._sess_cache: tuple | None = None        # (path, stat key, entries or None, error or None)

    # -- reading
    def record(self) -> dict | None:
        """The current password record, None when no password is set, AuthFileError when the file is unusable."""
        p = auth_path()
        with self._lock:
            key = _stat_key(p)
            c = self._auth_cache
            if c is None or c[0] != p or c[1] != key:
                try:
                    k, doc = _read_json(p)
                    c = (p, k, validate_record(doc) if doc is not None else None, None)
                except (AuthFileError, OSError) as exc:
                    logger.error("dashboard auth: %s unusable: %s", p.name,
                                 exc if isinstance(exc, AuthFileError) else type(exc).__name__)
                    c = (p, key, None, AuthFileError(str(exc) if isinstance(exc, AuthFileError) else type(exc).__name__))
                self._auth_cache = c
        if c[3] is not None:
            raise c[3]
        return c[2]

    def password_set(self) -> bool:
        """True when a password record exists (also when it is unusable: the caller then fails closed, never falls back
        to the token page, which would silently undo the owner's choice)."""
        try:
            return self.record() is not None
        except AuthFileError:
            return True

    def _sessions(self) -> list[dict]:
        p = sessions_path()
        with self._lock:
            key = _stat_key(p)
            c = self._sess_cache
            if c is None or c[0] != p or c[1] != key:
                try:
                    k, doc = _read_json(p)
                    c = (p, k, _parse_sessions(doc), None)
                except (AuthFileError, OSError) as exc:
                    logger.error("dashboard auth: %s unusable: %s", p.name,
                                 exc if isinstance(exc, AuthFileError) else type(exc).__name__)
                    c = (p, key, None, exc)
                self._sess_cache = c
        if c[3] is not None:
            raise AuthFileError("dashboard-sessions.json unusable")
        return c[2]

    def session(self, cookie_header: str | None) -> Session | None:
        """The live session a request's cookie names, or None (no cookie, unknown, expired, minted under an older
        password, no password set, or an unusable file: every doubt is None). Read-only: a GET never writes."""
        values = cookie_values(cookie_header)
        if not values:
            return None
        try:
            rec = self.record()
            if rec is None:
                return None
            entries = self._sessions()
        except AuthFileError:
            return None
        now = self.clock()
        wanted = {_digest(v) for v in values}
        for e in entries:
            if e["id_sha256"] in wanted and e["generation"] == rec["generation"] and e["expires"] > now:
                return Session(e["id_sha256"], e["csrf"], float(e["expires"]))
        return None

    # -- writing
    def _rewrite_sessions(self, change) -> None:
        """Read the sessions file fresh under the file lock, apply change(entries, now) -> entries, drop the expired and
        write it back (0600, atomic). An unusable file holds no valid session, so it is rewritten from empty (logged)."""
        p = sessions_path()
        with self._write_lock, file_lock(p):        # not self._lock: a CLI holding the file lock must not stall views
            try:
                _, doc = _read_json(p)
                entries = _parse_sessions(doc)
            except AuthFileError as exc:
                logger.warning("dashboard auth: replacing an unusable %s: %s", p.name, exc)
                entries = []
            now = self.clock()
            write_private(p, {"version": 1, "sessions": [e for e in change(list(entries), now) if e["expires"] > now]})
            with self._lock:
                self._sess_cache = None

    def login(self, password: str, address: str) -> dict:
        """One login attempt. {"ok": True, "cookie": value, "csrf": token} or {"ok": False, "status": 401 | 404 | 429
        | 500, "retry_after": s, "why": text}. The password is never logged; failures log the address only."""
        with self._login_lock:
            try:
                rec = self.record()
            except AuthFileError:
                return {"ok": False, "status": 500, "why": "dashboard-auth.json is unusable (see provider log)"}
            if rec is None:
                return {"ok": False, "status": 404, "why": "no dashboard password is set"}
            blocked = self.throttle.blocked(address)
            if blocked is not None:
                secs, kind = blocked
                logger.warning("dashboard login refused from %s: %s lockout, %d s left", address, kind, secs)
                return {"ok": False, "status": 429, "retry_after": secs,
                        "why": "too many failed logins" + (" from everywhere" if kind == "global" else "")}
            if not verify_password(password, rec):
                left = self.throttle.failed(address)
                logger.warning("dashboard login failed from %s (%s)", address,
                               "address now locked out" if left == 0 else "address table full, untracked" if left is None
                               else f"{left} of {IP_FAILURES} in the window")
                return {"ok": False, "status": 401, "why": "wrong password"}
            self.throttle.succeeded(address)
            value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

            def add(entries, now):
                entries = [e for e in entries if e["expires"] > now]
                entries.append({"id_sha256": _digest(value), "csrf": csrf, "generation": rec["generation"],
                                "created": int(now), "expires": int(now + SESSION_TTL_S)})
                entries.sort(key=lambda e: e.get("created", 0))
                return entries[-MAX_SESSIONS:]
            try:
                self._rewrite_sessions(add)
            except OSError as exc:
                logger.error("dashboard login: cannot store the session: %s", type(exc).__name__)
                return {"ok": False, "status": 500, "why": "the session could not be stored (see provider log)"}
            logger.info("dashboard login from %s", address)
            return {"ok": True, "cookie": value, "csrf": csrf}

    def logout(self, session: Session) -> None:
        self._rewrite_sessions(lambda entries, now: [e for e in entries if e["id_sha256"] != session.id_sha256])

    def logout_all(self) -> None:
        self._rewrite_sessions(lambda entries, now: [])

    def csrf_ok(self, session: Session, headers, peer: str) -> bool:
        """A state-changing request authorised by the cookie: same origin AND the session's CSRF token in CSRF_HEADER."""
        given = headers.get(CSRF_HEADER) or ""
        return same_origin(headers, peer) and hmac.compare_digest(given.encode(), session.csrf.encode())


# ------------------------------------------------------------------ CLI
def _read_new_password(use_stdin: bool) -> str:
    if use_stdin:
        line = sys.stdin.readline()
        pw = line[:-1] if line.endswith("\n") else line
        check_password_shape(pw)
        return pw
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("set-password needs a real terminal (it prompts twice, without echo); pass --stdin to read "
                         "one line from standard input instead")
    first = getpass.getpass("New dashboard password: ")
    check_password_shape(first)
    if getpass.getpass("Repeat it: ") != first:
        raise ValueError("the two entries differ; nothing changed")
    return first


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage the dashboard password and sessions (never prints a secret).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set-password", help=f"set or change the password (min {MIN_PASSWORD_LEN} characters); "
                                            "ends every session")
    s.add_argument("--stdin", action="store_true", help="read the password as one line from standard input (scripts)")
    sub.add_parser("clear-password", help="remove the password (the dashboard falls back to the token paste) and end "
                                          "every session")
    sub.add_parser("status", help="whether a password is set and how many sessions are live")
    sub.add_parser("logout-all", help="end every dashboard session")
    args = ap.parse_args(argv)
    ap_path, sp_path = auth_path(), sessions_path()
    auth = DashboardAuth()
    try:
        if args.cmd == "set-password":
            rec = hash_password(_read_new_password(args.stdin))
            with file_lock(ap_path):
                write_private(ap_path, rec)
            auth.logout_all()
            how = ("scrypt" if rec["kdf"] == "scrypt" else
                   f"PBKDF2-HMAC-SHA256, {rec['iterations']} iterations: this Python has no hashlib.scrypt")
            print(f"dashboard password set ({ap_path}, {how}); every existing dashboard session has ended")
            return 0
        if args.cmd == "clear-password":
            with file_lock(ap_path):
                try:
                    os.unlink(ap_path)
                    was = True
                except FileNotFoundError:
                    was = False
            auth.logout_all()
            print("dashboard password cleared; every dashboard session has ended" if was
                  else "no dashboard password was set; sessions cleared")
            return 0
        if args.cmd == "logout-all":
            auth.logout_all()
            print("every dashboard session has ended")
            return 0
        # status
        try:
            rec = auth.record()
        except AuthFileError as exc:
            print(f"dashboard password: file unusable ({exc}); run set-password or clear-password")
            return 1
        if rec is None:
            print("dashboard password: not set (the dashboard asks for the provider token)")
            return 0
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("set_at", 0)))
        try:
            now = time.time()
            live = sum(1 for e in auth._sessions() if e["expires"] > now and e["generation"] == rec["generation"])
            sess = f"{live} live session(s)"
        except AuthFileError:
            sess = "sessions file unusable (run logout-all)"
        kdf = (f"scrypt n={rec['n']} r={rec['r']} p={rec['p']}" if rec["kdf"] == "scrypt"
               else f"pbkdf2_sha256 {rec['iterations']} iterations")
        print(f"dashboard password: set {when} ({kdf}); {sess}")
        return 0
    except EOFError:
        print("dashboard_auth: no input; nothing changed", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        # ValueError texts are this module's own (never the password); an OSError is named by type and file only
        msg = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__} on {getattr(exc, 'filename', '?')}"
        print(f"dashboard_auth: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
