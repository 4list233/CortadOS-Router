#!/bin/bash
# Install the live view's heartbeat sender (cortados_router/live_heartbeat.py) on THIS machine as a LaunchAgent that posts
# every 60 s. Run it on the host itself, from this checkout:
#
#   scripts/install_live_heartbeat.sh <host> [--provider-url URL] [--labels a,b,...] [--s1-data DIR] [--no-s1]
#                                     [--orchestrator] [--force] [--dry-run]
#
#   <host>          the host's slug on the live view: air | studio | pro (the name its token is bound to)
#   --provider-url  where the provider answers from this machine; default: ~/.config/cortados/provider-url if present,
#                   else http://127.0.0.1:8432 (the provider host itself)
#   --labels        launchd labels to report; default: every loaded com.cortados.* job here (the sender's own excluded)
#   --s1-data       the S1 pipeline's data dir (default ~/cortados/train/data); the s1 block is on by default for studio
#   --no-s1 / --s1  turn the s1 block off / on whatever the host
#   --orchestrator  also report this machine's main Claude Code sessions' own tokens (the live view's Work split;
#                   cortados_router/orchestrator_usage.py reads ~/.claude/projects, child-task folders under ~/CortadOS
#                   excluded). Turn it on where the owner runs his orchestrating sessions; off by default.
#   --force         overwrite an existing ~/.config/cortados/heartbeat.json (else it is kept as it is)
#   --dry-run       print what would be written and run; change nothing
#
# Needs, before it is run: the host-bound token in ~/.config/cortados/live-heartbeat-token (0600), minted on the PROVIDER
# host with  python3 -B -m cortados_router.provider.clients add <host>-heartbeat --scope live:heartbeat --host <host> \
#            --token-file <path>  and copied here (scp, then chmod 600). The plist holds no secret.
# Writes: ~/.config/cortados/heartbeat.json, ~/Library/LaunchAgents/com.cortados.live-heartbeat.plist; loads it with
# launchctl bootstrap gui/<uid> (booting out a previous copy first). Logs to ~/.cortados/live-heartbeat.out.
# Test seams: CORTADOS_LAUNCHCTL (the launchctl binary), HOME.
set -euo pipefail

LABEL="com.cortados.live-heartbeat"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHCTL="${CORTADOS_LAUNCHCTL:-/bin/launchctl}"
PYTHON="${CORTADOS_HEARTBEAT_PYTHON:-/usr/bin/python3}"
CONF_DIR="$HOME/.config/cortados"
CONFIG="$CONF_DIR/heartbeat.json"
TOKEN_FILE="$CONF_DIR/live-heartbeat-token"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/.cortados/live-heartbeat.out"

die() { echo "install_live_heartbeat: $*" >&2; exit 2; }

[ $# -ge 1 ] || die "usage: $0 <host> [--provider-url URL] [--labels a,b] [--s1-data DIR] [--no-s1|--s1] [--orchestrator] [--force] [--dry-run]"
HOST="$1"; shift
[[ "$HOST" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die "host must be a slug like air, studio, pro"
URL=""; LABELS=""; S1_DATA="~/cortados/train/data"; FORCE=0; DRY=0; ORCH=0
S1=0; [ "$HOST" = "studio" ] && S1=1
while [ $# -gt 0 ]; do
  case "$1" in
    --provider-url) URL="${2:?}"; shift 2 ;;
    --labels) LABELS="${2:?}"; shift 2 ;;
    --s1-data) S1_DATA="${2:?}"; S1=1; shift 2 ;;
    --no-s1) S1=0; shift ;;
    --s1) S1=1; shift ;;
    --orchestrator) ORCH=1; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) die "unknown option $1" ;;
  esac
done
if [ -n "$URL" ]; then
  [[ "$URL" =~ ^https?:// ]] || die "--provider-url must be an http:// or https:// URL"
fi

if [ -z "$LABELS" ]; then
  # every loaded com.cortados.* job of this user (third column of launchctl list), the sender itself excluded
  LABELS="$("$LAUNCHCTL" list 2>/dev/null | awk '{print $3}' | grep -E '^com\.cortados\.' | grep -vx "$LABEL" \
            | sort -u | head -n 14 | paste -sd, - || true)"
fi

# The token must already be here: never minted or printed by this script.
if [ "$DRY" = 0 ]; then
  [ -f "$TOKEN_FILE" ] || die "no token at $TOKEN_FILE (mint it on the provider host: see the header of this script)"
  mode="$(stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null || stat -c '%a' "$TOKEN_FILE")"
  [ "$mode" = "600" ] || die "$TOKEN_FILE must be mode 600 (chmod 600 it)"
fi

# One isolated Python builds both files: the config as JSON and the plist with plistlib, so a path holding &, |, < or
# any other XML or sed special is written as it is (the old sed substitution of a template corrupted such a plist).
GENERATED="$(HOST="$HOST" URL="$URL" LABELS="$LABELS" S1="$S1" S1_DATA="$S1_DATA" ORCH="$ORCH" LABEL="$LABEL" PY="$PYTHON" \
             CONFIG="$CONFIG" REPO="$REPO" LOG="$LOG" "$PYTHON" -I -c '
import json, os, plistlib
e = os.environ
cfg = {"version": 1, "host": e["HOST"], "token_file": "~/.config/cortados/live-heartbeat-token",
       "ttl_s": 180, "jobs": [l for l in e["LABELS"].split(",") if l], "hardware": True, "disk": True}
if e["URL"]:
    cfg["provider_url"] = e["URL"]
if e["S1"] == "1":
    cfg["s1"] = {"data_dir": e["S1_DATA"]}
if e["ORCH"] == "1":
    # the default task root (workspaces.py); the reader also skips any <x>/{active,archive,held}/<task id> folder
    cfg["orchestrator"] = {"task_roots": ["~/CortadOS"]}
# The heartbeat sender of the live view (cortados_router/live_heartbeat.py), one post every 60 s. No secret in it: the
# sender reads its token from the 0600 file its config names.
plist = {"Label": e["LABEL"],
         "ProgramArguments": [e["PY"], "-B", "-m", "cortados_router.live_heartbeat", "--config", e["CONFIG"]],
         "WorkingDirectory": e["REPO"], "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
         "StartInterval": 60, "RunAtLoad": True, "ProcessType": "Background", "LowPriorityIO": True,
         "StandardOutPath": e["LOG"], "StandardErrorPath": e["LOG"]}
print(json.dumps(cfg, indent=1))
print("#--PLIST--")
print(plistlib.dumps(plist, sort_keys=False).decode(), end="")')"
CONFIG_JSON="${GENERATED%%$'\n'#--PLIST--*}"
PLIST_XML="${GENERATED#*#--PLIST--$'\n'}"

if [ "$DRY" = 1 ]; then
  echo "# $CONFIG$( [ -f "$CONFIG" ] && [ "$FORCE" = 0 ] && echo ' (exists: kept)')"
  echo "$CONFIG_JSON"
  echo "# $PLIST"
  echo "$PLIST_XML"
  echo "# then: $LAUNCHCTL bootout gui/$(id -u)/$LABEL; $LAUNCHCTL bootstrap gui/$(id -u) $PLIST"
  exit 0
fi

[ -d "$CONF_DIR" ] || mkdir -p -m 700 "$CONF_DIR"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.cortados"
if [ -f "$CONFIG" ] && [ "$FORCE" = 0 ]; then
  echo "install_live_heartbeat: keeping the existing $CONFIG (--force to rewrite it)"
else
  umask 077
  printf '%s\n' "$CONFIG_JSON" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
  echo "install_live_heartbeat: wrote $CONFIG"
fi
# The config, the provider URL and the token file must pass before anything is scheduled (exit 2 names the problem).
(cd "$REPO" && "$PYTHON" -B -m cortados_router.live_heartbeat --config "$CONFIG" --check >/dev/null) \
  || die "the sender refused $CONFIG; nothing was loaded"
printf '%s\n' "$PLIST_XML" > "$PLIST.tmp" && mv "$PLIST.tmp" "$PLIST"
chmod 644 "$PLIST"
"$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
"$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST"
echo "install_live_heartbeat: loaded $LABEL for host $HOST (log: $LOG)"
