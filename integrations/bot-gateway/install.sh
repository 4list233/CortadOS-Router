#!/bin/sh
# Run the bot gateway (cortados_router/bot_gateway.py, phase 1: route_task only) on the Air as a launchd LaunchAgent,
# as the Air's normal user, on loopback port 8434 for Tailscale Funnel. Design: docs/GROKBOT-BRIDGE.md; why the provider
# token is passed the way it is: README.md, "Bot gateway on the Air".
#   install.sh token    create ~/.config/cortados/bot-gateway-token (0600, 32 random bytes, base64url) if absent;
#                       the value is never printed
#   install.sh plist    write ~/Library/LaunchAgents/com.cortados.bot-gateway.plist (no secret values in it)
#   install.sh load     (re)load the LaunchAgent
#   install.sh unload   stop and unload it
#   install.sh status   launchctl state, an unauthenticated POST /mcp (expected: 401), the log path and line count
#   install.sh funnel-on|funnel-off   verify and add/remove the owned /mcp handler on reserved public port 443
# Secrets: this script never reads, prints or copies a token value. Of ~/.cortados/provider.env it checks only whether
# a line assigns the variable NAME CORTADOS_PROVIDER_TOKEN (grep -q, no output). At run time launch.py reads that file
# as data (never as shell code) and execs the gateway with an explicit environment.
set -eu

LABEL=com.cortados.bot-gateway
PORT=8434                                        # docs/GROKBOT-BRIDGE.md section 0: the gateway's one port on the Air
# Funnel on 443 because outside bots' egress proxies commonly pass only 443 (TLS to 8443 fails there). Funnel shares
# a port's HTTPS listener with every Serve path on it, so private tailnet-only Serve (e.g. the provider's) must use
# another port; funnel-on/off refuse a 443 listener holding any handler or path that is not exactly ours.
# Tailscale keys the web handler "<host>:443"; the gateway's CORTADOS_BOT_ALLOWED_HOST is then the bare hostname.
FUNNEL_PORT=443
# Earlier installs published on 8443 and recorded "8443 /mcp ..." ownership. funnel-off still recognises that legacy
# record and removes exactly the owned 8443 handler, so an upgrade can withdraw the old public listener;
# funnel-on refuses while it exists (migration order: docs/GROKBOT-BRIDGE.md).
LEGACY_FUNNEL_PORT=8443
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${CORTADOS_BOT_GATEWAY_REPO:-$(cd "$HERE/../.." && pwd)}"   # the checkout this script lives in (~/cortados-router on the Air)
PYBIN="${CORTADOS_BOT_GATEWAY_PYTHON:-/usr/bin/python3}"          # the interpreter the LaunchAgent runs (3.9 on the Air)
: "${HOME:?HOME must be set}"
TOKEN_DIR="$HOME/.config/cortados"
TOKEN_FILE="$TOKEN_DIR/bot-gateway-token"
# Design (b) only: a provider token file the owner creates; used when provider.env does not assign the token.
PROVIDER_TOKEN_FILE="$TOKEN_DIR/bot-gateway-provider-token"
PROVIDER_ENV="$HOME/.cortados/provider.env"
STATE_DIR="$HOME/.cortados"
LOG="$STATE_DIR/bot-gateway.out"
# No --workspace-root since #86: tasks run in the Air's CortadOS workspaces (exec-hosts.json `root`) or any existing
# folder the bot names; the old ~/cortados-bot-workspace is neither created nor passed.
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
FUNNEL_OWNER="$STATE_DIR/bot-gateway-funnel-owner"
DOMAIN="gui/$(id -u)"
ALLOWED_HOST="${CORTADOS_BOT_ALLOWED_HOST:-}"       # optional exact public Host (bare hostname on 443), copied into the plist; never hard-coded

die() { echo "install.sh: $*" >&2; exit 2; }
case "$ALLOWED_HOST" in *[!A-Za-z0-9.:-]* ) die "CORTADOS_BOT_ALLOWED_HOST contains an invalid character" ;; esac

# A file holding a secret must be a regular file (not a symlink), owned by us, mode 0600: the gateway refuses any other
# token file (bot_gateway.read_token_file), so the installer checks the same thing up front. find -prune without -L
# does not follow a symlink, so a link is type l and fails -type f.
secret_ok() {
  [ -n "$(find "$1" -prune -type f -user "$(id -u)" -perm 600 2>/dev/null)" ]
}

tailscale_cli() {
  if [ -n "${TAILSCALE:-}" ]; then printf '%s' "$TAILSCALE"
  elif command -v tailscale >/dev/null 2>&1; then printf '%s' tailscale
  elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
    printf '%s' /Applications/Tailscale.app/Contents/MacOS/Tailscale
  else die "no tailscale CLI found (set TAILSCALE=<path>)"; fi
}

# The gateway's provider URL. Default: loopback, because the gateway runs on the same host as the provider and the
# provider listens on 127.0.0.1:8432 only (other machines reach it through a tailnet-only Tailscale Serve; the Tailscale
# app cannot proxy to the host's own tailnet IP, so the provider no longer binds that address).
# CORTADOS_BOT_PROVIDER_URL overrides it: an http(s) URL with a host, no credentials, no query or fragment, so no
# secret is written into the plist and the value is never shell-interpreted.
provider_url() {
  python3 -I -c '
import sys, urllib.parse
u = sys.argv[1]
try:
    p = urllib.parse.urlsplit(u)
    p.port  # raises ValueError on a malformed or out-of-range port
    # The scheme text must be exactly lowercase http/https: urlsplit lowercases it, but launch.py
    # (resolve_provider_url) checks the literal prefix, so "HTTP://" would pass here and crash-loop the gateway (78).
    # Any literal "?" or "#" is refused: a bare trailing one gives an empty query/fragment that urlsplit hides.
    ok = (u.startswith(("http://", "https://")) and p.scheme in ("http", "https") and bool(p.hostname)
          and "@" not in p.netloc and "?" not in u and "#" not in u
          and u.isprintable() and not any(c.isspace() for c in u))
except ValueError:
    ok = False
sys.exit(0 if ok else 1)' "$1" || die "CORTADOS_BOT_PROVIDER_URL must be an http(s) URL with a host and no credentials, query or fragment"
  printf '%s' "$1"
}

cmd_token() {
  ( umask 077; mkdir -p "$TOKEN_DIR" )
  chmod 700 "$TOKEN_DIR"
  if [ -e "$TOKEN_FILE" ] || [ -L "$TOKEN_FILE" ]; then
    secret_ok "$TOKEN_FILE" || die "$TOKEN_FILE exists but is not a regular 0600 file owned by $(id -un); fix it (chmod 600) or remove it, then rerun"
    echo "token: $TOKEN_FILE already exists; left unchanged"
  else
    # 32 random bytes, base64url without padding (43 characters). Written to a temp file in the same directory and
    # moved into place, so the gateway never sees a half-written token. Nothing here goes to stdout or stderr.
    tmp="$(umask 077; mktemp "$TOKEN_DIR/.bot-gateway-token.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT INT TERM
    head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' > "$tmp"
    [ "$(wc -c < "$tmp" | tr -d ' ')" -ge 43 ] || die "token generation produced too few bytes"
    chmod 600 "$tmp"
    mv "$tmp" "$TOKEN_FILE"
    trap - EXIT INT TERM
    echo "token: created $TOKEN_FILE (0600)"
  fi
  echo "To view it yourself (for Grok Bot's secret field; never paste it into a chat):"
  echo "  cat $TOKEN_FILE"
}

cmd_plist() {
  secret_ok "$TOKEN_FILE" || die "no usable $TOKEN_FILE: run 'install.sh token' first"
  [ -f "$REPO/cortados_router/bot_gateway.py" ] || die "$REPO is not a CortadOS checkout (no cortados_router/bot_gateway.py)"
  # Provider token for route_task. (a) preferred: provider.env assigns CORTADOS_PROVIDER_TOKEN, and the LaunchAgent's
  # launcher (launch.py) reads it as data at start, so the value is in neither the plist nor any command line. (b) otherwise: a 0600 file
  # the owner creates, read by launch.py (--provider-token-file). Only the variable name is checked here, with the
  # same line grammar launch.py accepts (leading spaces/tabs, optional export), so the two never pick differently.
  if [ -f "$PROVIDER_ENV" ] && grep -Eq '^[[:blank:]]*(export[[:blank:]]+)?CORTADOS_PROVIDER_TOKEN=' "$PROVIDER_ENV"; then
    mode=env
  elif secret_ok "$PROVIDER_TOKEN_FILE"; then
    mode=file
  else
    die "no provider token for route_task: $PROVIDER_ENV does not assign CORTADOS_PROVIDER_TOKEN, and there is no 0600 $PROVIDER_TOKEN_FILE. Create that file yourself (umask 077; then write the provider token into it) and rerun"
  fi
  url="$(provider_url "${CORTADOS_BOT_PROVIDER_URL:-http://127.0.0.1:8432}")"
  ( umask 077; mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents" )
  [ -e "$LOG" ] || ( umask 077; : > "$LOG" )
  python3 -I - "$PLIST" "$LABEL" "$mode" "$url" "$PORT" "$REPO" "$PYBIN" "$TOKEN_FILE" "$LOG" \
    "$PROVIDER_ENV" "$PROVIDER_TOKEN_FILE" "$STATE_DIR" "$ALLOWED_HOST" <<'PY'
import os, plistlib, sys
(plist, label, mode, url, port, repo, py, token_file, log,
 provider_env, provider_token_file, state_dir, allowed_host) = sys.argv[1:14]
gateway = [py, "-B", "-m", "cortados_router.bot_gateway", "--host", "127.0.0.1", "--port", port,
           "--token-file", token_file]
env = {"CORTADOS_PROVIDER_URL": url, "CORTADOS_HOME": state_dir, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
if allowed_host:
    env["CORTADOS_BOT_ALLOWED_HOST"] = allowed_host
# Both designs go through launch.py (python3 -I: no PYTHON* variable and no user site directory affect it). It reads
# the one chosen credential source as data, never executes it, and execs the gateway with exactly HOME, a fixed PATH,
# CORTADOS_HOME, CORTADOS_PROVIDER_URL (both from this plist) and the token: launchd's own environment (PYTHONPATH,
# proxies, an ambient CORTADOS_PROVIDER_TOKEN) never reaches the gateway. Why not sourcing provider.env: README.md.
source = ["--provider-env", provider_env] if mode == "env" else ["--provider-token-file", provider_token_file]
args = ([py, "-I", os.path.join(repo, "integrations", "bot-gateway", "launch.py")] + source
        + ["--python", py, "--"] + gateway[4:])
doc = {"Label": label, "ProgramArguments": args, "EnvironmentVariables": env, "WorkingDirectory": repo,
       "RunAtLoad": True,
       # Restart after a crash, and keep retrying while Tailscale is not up yet (the bind fails until it is).
       "KeepAlive": True, "ThrottleInterval": 15,
       "StandardOutPath": log, "StandardErrorPath": log,
       "Umask": 0o077}                       # the usage log and counters the gateway writes stay owner-only
tmp = plist + ".tmp"
with open(tmp, "wb") as f:
    plistlib.dump(doc, f)
os.chmod(tmp, 0o644)
os.replace(tmp, plist)
PY
  if command -v plutil >/dev/null 2>&1; then plutil -lint "$PLIST" >/dev/null || die "plutil rejected $PLIST"; fi
  echo "plist: wrote $PLIST (gateway on 127.0.0.1:$PORT, provider $url, provider token: $([ "$mode" = env ] && echo "read by launch.py from $PROVIDER_ENV" || echo "file $PROVIDER_TOKEN_FILE"))"
  echo "next: $HERE/install.sh load"
}

cmd_load() {
  [ -f "$PLIST" ] || die "no $PLIST: run 'install.sh plist' first"
  secret_ok "$TOKEN_FILE" || die "no usable $TOKEN_FILE: run 'install.sh token' first"
  # bootout first, so a rewritten plist takes effect (bootstrap refuses a label that is already loaded).
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "loaded $LABEL; check: $HERE/install.sh status"
}

cmd_unload() {
  if launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; then echo "unloaded $LABEL"; else echo "$LABEL was not loaded"; fi
}

funnel_state() {
  ts="$1"; port="${2:-$FUNNEL_PORT}"
  out="$("$ts" funnel status --json 2>/dev/null)" || die "tailscale funnel status failed"
  # Refuse anything except our single exact handler on the reserved port. This treats unknown schema/configuration as
  # conflict rather than risking exposure or deletion; other ports are deliberately ignored.
  python3 -I -c '
import json, sys
try:
    cfg=json.loads(sys.argv[1]); port=sys.argv[2]; target=sys.argv[3]
    if not isinstance(cfg, dict) or set(cfg) - {"Web", "AllowFunnel", "TCP"}:
        raise ValueError("unknown configuration")
    web=cfg.get("Web", {}); allow=cfg.get("AllowFunnel", {}); tcp=cfg.get("TCP", {})
    if not all(isinstance(v, dict) for v in (web, allow, tcp)):
        raise ValueError("invalid configuration")
    keys=[k for k in set(web)|set(allow) if k.rsplit(":",1)[-1] == port]
    if not keys and port not in tcp:
        print("empty"); raise SystemExit
    ours=(len(keys)==1 and web.get(keys[0]) == {"Handlers":{"/mcp":{"Proxy":target}}}
          and allow.get(keys[0]) is True and tcp.get(port) == {"HTTPS":True}
          and tcp[port]["HTTPS"] is True)
    print("ours" if ours else "conflict")
except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
    print("conflict")' "$out" "$port" "http://127.0.0.1:$PORT/mcp"
}

# Serialize intent creation, mutation and recovery: otherwise an off command could remove a pending
# record while an on command is about to activate Funnel. A crash-stale lock requires manual inspection.
funnel_lock() {
  ( umask 077; mkdir -p "$STATE_DIR" )
  mkdir "$FUNNEL_OWNER.lock" 2>/dev/null || die "Funnel operation locked; inspect $FUNNEL_OWNER.lock"
  trap 'rmdir "$FUNNEL_OWNER.lock"' EXIT
  trap 'exit 2' INT TERM
}

# The pending record is already ownership, not a disposable temporary file. Persist it exclusively
# before any public mutation, and replace it atomically only after exact configuration verification.
# A failed finalization leaves the durable pending record available to funnel-off.
funnel_record() {
  python3 -I - "$FUNNEL_OWNER" "$1" "${2:-$FUNNEL_PORT} /mcp http://127.0.0.1:$PORT/mcp" <<'PY'
import os, stat, sys, tempfile
path, action, expected = sys.argv[1:]
pending = "pending " + expected + "\n"
final = expected + "\n"
directory = os.path.dirname(path)
dfd = os.open(directory, os.O_RDONLY)
tmp = None
try:
    if action == "create":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pending)
            f.flush()
            os.fsync(f.fileno())
        os.fsync(dfd)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise ValueError("unsafe ownership record")
            record = f.read()
        if record not in (pending, final):
            raise ValueError("ownership record mismatch")
        if action == "finalize":
            fd, tmp = tempfile.mkstemp(prefix=".bot-gateway-funnel-", dir=directory)
            with os.fdopen(fd, "w") as f:
                f.write(final)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            tmp = None
            os.fsync(dfd)
        elif action == "remove":
            os.unlink(path)
            os.fsync(dfd)
        elif action == "check":
            print("pending" if record == pending else "final")
        else:
            raise ValueError("unknown record action")
except (OSError, ValueError) as e:
    print("Funnel ownership record: " + str(e), file=sys.stderr)
    sys.exit(1)
finally:
    if tmp is not None:
        os.unlink(tmp)
    os.close(dfd)
PY
}

cmd_funnel_on() {
  ts="$(tailscale_cli)"
  funnel_lock
  if funnel_record check "$LEGACY_FUNNEL_PORT" >/dev/null 2>&1; then
    die "legacy Funnel ownership record for port $LEGACY_FUNNEL_PORT found; run '$0 funnel-off' first to remove the old listener"
  fi
  [ "$(funnel_state "$ts")" = empty ] || die "reserved Funnel port $FUNNEL_PORT already has configuration"
  funnel_record create || die "cannot persist exclusive Funnel ownership intent; nothing activated"
  "$ts" funnel --bg --https="$FUNNEL_PORT" --set-path /mcp "http://127.0.0.1:$PORT/mcp"
  [ "$(funnel_state "$ts")" = ours ] || die "Funnel did not install the exact reserved-port handler; ownership intent retained"
  funnel_record finalize || die "cannot finalize Funnel ownership; durable intent retained for funnel-off"
}

cmd_funnel_off() {
  ts="$(tailscale_cli)"
  funnel_lock
  # The current 443 record first; otherwise a legacy 8443 record from an earlier install, handled identically.
  if record="$(funnel_record check "$FUNNEL_PORT" 2>/dev/null)"; then
    port="$FUNNEL_PORT"
  elif record="$(funnel_record check "$LEGACY_FUNNEL_PORT" 2>/dev/null)"; then
    port="$LEGACY_FUNNEL_PORT"
  else
    funnel_record check "$FUNNEL_PORT" >/dev/null || true
    die "no valid Funnel ownership marker; refusing removal"
  fi
  state="$(funnel_state "$ts" "$port")"
  # A failed activation can leave only its intent. Never issue a Tailscale mutation in that case.
  if [ "$state" = empty ] && [ "$record" = pending ]; then
    funnel_record remove "$port"
    return
  fi
  [ "$state" = ours ] || die "reserved Funnel configuration on port $port is not exactly ours; refusing removal"
  "$ts" funnel --https="$port" --set-path /mcp off
  [ "$(funnel_state "$ts" "$port")" = empty ] || die "reserved Funnel configuration remains after removal"
  funnel_record remove "$port"
}

cmd_status() {
  echo "== launchctl $DOMAIN/$LABEL"
  if out="$(launchctl print "$DOMAIN/$LABEL" 2>&1)"; then
    printf '%s\n' "$out" | grep -E '^[[:space:]]*(state|pid|last exit code|runs) =' || echo "(loaded; no state lines)"
  else
    echo "not loaded"
  fi
  # Read the installed allowlist (not the caller's environment), so status probes the configuration launchd runs.
  probe_host=""
  if [ -f "$PLIST" ]; then
    probe_host="$(python3 -I -c 'import plistlib,sys; print(plistlib.load(open(sys.argv[1],"rb")).get("EnvironmentVariables",{}).get("CORTADOS_BOT_ALLOWED_HOST",""))' "$PLIST")" || die "cannot read allowed host from plist"
  fi
  # Unauthenticated on purpose: a 401 shows the loopback gateway is up and checks auth without using the token.
  if [ -n "$probe_host" ]; then
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST -H 'content-type: application/json' \
            -H "Host: $probe_host" --data '{}' "http://127.0.0.1:$PORT/mcp" 2>/dev/null)" || true
  else
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST -H 'content-type: application/json' \
            --data '{}' "http://127.0.0.1:$PORT/mcp" 2>/dev/null)" || true
  fi
  echo "== POST http://127.0.0.1:$PORT/mcp without a token: HTTP ${code:-none} (expected 401)"
  # Log contents are never printed: the access log records request paths, and a client that put its token in the
  # path would have it echoed here. The owner reads the file himself.
  if [ -f "$LOG" ]; then echo "== log: $LOG ($(wc -l < "$LOG" | tr -d ' ') lines; not shown)"; else echo "== log: $LOG (none yet)"; fi
  [ "${code:-}" = 401 ]
}

case "${1:-}" in
token) cmd_token ;;
plist) cmd_plist ;;
load) cmd_load ;;
unload) cmd_unload ;;
status) cmd_status ;;
funnel-on) cmd_funnel_on ;;
funnel-off) cmd_funnel_off ;;
*) echo "usage: $0 token | plist | load | unload | status | funnel-on | funnel-off" >&2; exit 2 ;;
esac
