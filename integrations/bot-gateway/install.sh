#!/bin/sh
# Run the bot gateway (cortados_router/bot_gateway.py, phase 1: route_task only) on the Air as a launchd LaunchAgent,
# as the Air's normal user, on the Air's tailnet address, port 8434. Design: docs/GROKBOT-BRIDGE.md; why the provider
# token is passed the way it is: README.md, "Bot gateway on the Air".
#   install.sh token    create ~/.config/cortados/bot-gateway-token (0600, 32 random bytes, base64url) if absent;
#                       the value is never printed
#   install.sh plist    write ~/Library/LaunchAgents/com.cortados.bot-gateway.plist (no secret values in it)
#   install.sh load     (re)load the LaunchAgent
#   install.sh unload   stop and unload it
#   install.sh status   launchctl state, an unauthenticated POST /mcp (expected: 401), the log path and line count;
#                       prints no token and no log contents
# Secrets: this script never reads, prints or copies a token value. Of ~/.cortados/provider.env it checks only whether
# a line assigns the variable NAME CORTADOS_PROVIDER_TOKEN (grep -q, no output). At run time launch.py reads that file
# as data (never as shell code) and execs the gateway with an explicit environment.
set -eu

LABEL=com.cortados.bot-gateway
PORT=8434                                        # docs/GROKBOT-BRIDGE.md section 0: the gateway's one port on the Air
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
WORKSPACE="$HOME/cortados-bot-workspace"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

die() { echo "install.sh: $*" >&2; exit 2; }

# A file holding a secret must be a regular file (not a symlink), owned by us, mode 0600: the gateway refuses any other
# token file (bot_gateway.read_token_file), so the installer checks the same thing up front. find -prune without -L
# does not follow a symlink, so a link is type l and fails -type f.
secret_ok() {
  [ -n "$(find "$1" -prune -type f -user "$(id -u)" -perm 600 2>/dev/null)" ]
}

# The tailnet IPv4 of this machine, from `tailscale ip -4` at install time (never written into the repo). The macOS
# app bundle's CLI is the fallback when no `tailscale` is on PATH.
tailnet_ip() {
  ts="${TAILSCALE:-}"
  if [ -z "$ts" ]; then
    if command -v tailscale >/dev/null 2>&1; then ts=tailscale
    elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then ts=/Applications/Tailscale.app/Contents/MacOS/Tailscale
    else die "no tailscale CLI found (set TAILSCALE=<path>)"; fi
  fi
  # The CLI's own exit status is checked (no pipe in between to mask it); a failing CLI is never read for an address.
  out="$("$ts" ip -4 2>/dev/null)" || die "tailscale ip -4 failed (is Tailscale up?)"
  # The complete output must be exactly one IPv4 address in 100.64.0.0/10, Tailscale's address range: more lines, a
  # malformed address, 0.0.0.0 (every interface) or any address outside the tailnet would bind beyond the tailnet.
  python3 -I -c '
import ipaddress, sys
s = sys.argv[1]
try:
    ok = s == str(ipaddress.IPv4Address(s)) and ipaddress.IPv4Address(s) in ipaddress.ip_network("100.64.0.0/10")
except ValueError:
    ok = False
sys.exit(0 if ok else 1)' "$out" || die "tailscale ip -4 did not give exactly one tailnet (100.64.0.0/10) IPv4 address"
  printf '%s' "$out"
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
  # the owner creates, passed by path (CORTADOS_PROVIDER_TOKEN_FILE). Only the variable name is checked here.
  if [ -f "$PROVIDER_ENV" ] && grep -Eq '^(export[[:space:]]+)?CORTADOS_PROVIDER_TOKEN=' "$PROVIDER_ENV"; then
    mode=env
  elif secret_ok "$PROVIDER_TOKEN_FILE"; then
    mode=file
  else
    die "no provider token for route_task: $PROVIDER_ENV does not assign CORTADOS_PROVIDER_TOKEN, and there is no 0600 $PROVIDER_TOKEN_FILE. Create that file yourself (umask 077; then write the provider token into it) and rerun"
  fi
  ip="$(tailnet_ip)"
  ( umask 077; mkdir -p "$STATE_DIR" "$WORKSPACE" "$HOME/Library/LaunchAgents" )
  chmod 700 "$WORKSPACE"
  [ -e "$LOG" ] || ( umask 077; : > "$LOG" )
  python3 -I - "$PLIST" "$LABEL" "$mode" "$ip" "$PORT" "$REPO" "$PYBIN" "$TOKEN_FILE" "$WORKSPACE" "$LOG" \
    "$PROVIDER_ENV" "$PROVIDER_TOKEN_FILE" "$STATE_DIR" <<'PY'
import os, plistlib, sys
(plist, label, mode, ip, port, repo, py, token_file, workspace, log,
 provider_env, provider_token_file, state_dir) = sys.argv[1:14]
url = f"http://{ip}:8432"
gateway = [py, "-B", "-m", "cortados_router.bot_gateway", "--host", ip, "--port", port,
           "--token-file", token_file, "--workspace-root", workspace,
           # Required by bot_gateway for any non-loopback --host: it states that a child would run as this user. Phase 1
           # starts no child (route_task only; no --enable-start-task), so nothing runs as the owner on a bot's behalf.
           "--children-run-as-me"]
env = {"CORTADOS_PROVIDER_URL": url, "CORTADOS_HOME": state_dir, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
if mode == "env":
    # launch.py reads provider.env as DATA (never executes it), takes only CORTADOS_PROVIDER_TOKEN, and execs the
    # gateway with exactly HOME, a fixed PATH, CORTADOS_HOME, CORTADOS_PROVIDER_URL (both from this plist) and the token.
    # python3 -I: no PYTHON* variable and no user site directory affect the launcher. Why not sourcing: README.md.
    args = [py, "-I", os.path.join(repo, "integrations", "bot-gateway", "launch.py"),
            "--provider-env", provider_env, "--python", py, "--"] + gateway[4:]
else:
    env["CORTADOS_PROVIDER_TOKEN_FILE"] = provider_token_file
    args = gateway
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
  echo "plist: wrote $PLIST (gateway on $ip:$PORT, provider http://$ip:8432, provider token: $([ "$mode" = env ] && echo "read by launch.py from $PROVIDER_ENV" || echo "file $PROVIDER_TOKEN_FILE"))"
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

cmd_status() {
  echo "== launchctl $DOMAIN/$LABEL"
  if out="$(launchctl print "$DOMAIN/$LABEL" 2>&1)"; then
    printf '%s\n' "$out" | grep -E '^[[:space:]]*(state|pid|last exit code|runs) =' || echo "(loaded; no state lines)"
  else
    echo "not loaded"
  fi
  ip="$(tailnet_ip)"
  # Unauthenticated on purpose: a 401 shows the gateway is up AND checks the bearer token, without using the token.
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST -H 'content-type: application/json' --data '{}' \
          "http://$ip:$PORT/mcp" 2>/dev/null)" || true
  echo "== POST http://$ip:$PORT/mcp without a token: HTTP ${code:-none} (expected 401)"
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
*) echo "usage: $0 token | plist | load | unload | status" >&2; exit 2 ;;
esac
