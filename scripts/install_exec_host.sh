#!/bin/bash
# Make THIS machine able to run CortadOS start_task child sessions (an execution host, cortados_router/exec_hosts.py).
# Run it on the machine itself, from its cortados-router checkout:
#
#   scripts/install_exec_host.sh <host> [--ssh ALIAS] [--gui|--no-gui] [--provider-url URL] [--claude PATH] [--dry-run]
#
#   <host>          this machine's slug in exec-hosts.json: pro | air | studio
#   --ssh ALIAS     the ssh destination OTHER machines use to reach this one (omit it for the Pro: nothing ssh-es into
#                   the laptop, so its tasks are started on the Pro itself with host "here")
#   --gui/--no-gui  whether chrome / connector children may run here (a desktop session is logged in); default --gui
#   --provider-url  the provider as seen from this machine; default: the content of ~/.config/cortados/provider-url
#   --claude PATH   the claude CLI; default ~/.local/bin/claude, else `claude` on PATH
#   --dry-run       print what would be created and loaded; change nothing (the read-only checks still run)
#
# Idempotent. What it does:
#   1. creates the task root ~/CortadOS/{active,archive,held} (workspaces.py),
#   2. installs the daily archive cleanup as the Air has it (com.cortados.archive-cleanup, 04:30, deletes archive/
#      entries settled more than 3 days ago: child_tasks.py --cleanup-archive), rewriting and reloading the LaunchAgent
#      only when it differs or is not loaded,
#   3. checks, read-only, that the claude CLI is present and logged in (`claude auth status`; only `loggedIn` is read),
#   4. checks that ~/.config/cortados/provider-url and provider-token exist (presence only: never read or printed,
#      except the URL, which is not a secret),
#   5. prints this machine's exec-hosts.json entry and whether ~/.config/cortados/exec-hosts.json already names it.
# Exit: 0 ready, 1 installed but something is left for the owner (listed as TODO lines), 2 usage error.
# Test seams: HOME, CORTADOS_LAUNCHCTL (the launchctl binary), CORTADOS_INSTALL_HOSTNAME (this machine's short name).
set -euo pipefail

LABEL="com.cortados.archive-cleanup"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHCTL="${CORTADOS_LAUNCHCTL:-/bin/launchctl}"
PYTHON="/usr/bin/python3"
CONF_DIR="$HOME/.config/cortados"
ROOT="$HOME/CortadOS"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/.cortados/archive-cleanup.out"
PATH_ENTRY="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

die() { echo "install_exec_host: $*" >&2; exit 2; }
say() { echo "install_exec_host: $*"; }
TODO=0
todo() { echo "TODO: $*"; TODO=1; }

[ $# -ge 1 ] || die "usage: $0 <host> [--ssh ALIAS] [--gui|--no-gui] [--provider-url URL] [--claude PATH] [--dry-run]"
HOST="$1"; shift
[[ "$HOST" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die "host must be a slug like pro, air, studio"
case "$HOST" in here|local) die "'$HOST' is start_task's alias for this machine; name the host (pro, air, studio)" ;; esac
SSH=""; GUI=1; URL=""; CLAUDE=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) SSH="${2:?}"; shift 2 ;;
    --gui) GUI=1; shift ;;
    --no-gui) GUI=0; shift ;;
    --provider-url) URL="${2:?}"; shift 2 ;;
    --claude) CLAUDE="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) die "unknown option $1" ;;
  esac
done
if [ -n "$SSH" ]; then
  [[ "$SSH" =~ ^[A-Za-z0-9_.@-]+$ && "$SSH" != -* ]] || die "--ssh must be an ssh_config alias or user@host"
fi
ME="${CORTADOS_INSTALL_HOSTNAME:-$(hostname -s)}"
[ "$DRY" = 1 ] && say "dry run: nothing is created or loaded"
say "host '$HOST' on this machine ($ME), user $(id -un), checkout $REPO"

# 1. Task root ------------------------------------------------------------------------------------------------------
for sub in active archive held; do
  if [ -d "$ROOT/$sub" ]; then
    say "task root: $ROOT/$sub exists"
  elif [ "$DRY" = 1 ]; then
    say "task root: would create $ROOT/$sub"
  else
    mkdir -p "$ROOT/$sub"
    say "task root: created $ROOT/$sub"
  fi
done

# 2. Daily archive cleanup LaunchAgent (the Air's plist, with this machine's paths; built with plistlib so any path is
# written as it is) ----------------------------------------------------------------------------------------------------
PLIST_XML="$(PY="$PYTHON" SCRIPT="$REPO/cortados_router/child_tasks.py" REPO="$REPO" HOMEDIR="$HOME" LOG="$LOG" \
             LABEL="$LABEL" "$PYTHON" -I -c '
import os, plistlib, sys
e = os.environ
plist = {"Label": e["LABEL"],
         "ProgramArguments": [e["PY"], "-B", e["SCRIPT"], "--cleanup-archive"],
         "WorkingDirectory": e["REPO"],
         "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": e["HOMEDIR"]},
         "StartCalendarInterval": {"Hour": 4, "Minute": 30},
         "RunAtLoad": False,
         "StandardOutPath": e["LOG"], "StandardErrorPath": e["LOG"]}
sys.stdout.write(plistlib.dumps(plist, sort_keys=False).decode())')"
SAME=0
if [ -f "$PLIST" ] && "$PYTHON" -I -c '
import plistlib, sys
a = plistlib.load(open(sys.argv[1], "rb")); b = plistlib.loads(sys.stdin.buffer.read())
sys.exit(0 if a == b else 1)' "$PLIST" <<<"$PLIST_XML" 2>/dev/null; then
  SAME=1
fi
LOADED=0
"$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && LOADED=1
if [ "$SAME" = 1 ] && [ "$LOADED" = 1 ]; then
  say "archive cleanup: $LABEL is installed and loaded (unchanged)"
elif [ "$DRY" = 1 ]; then
  say "archive cleanup: would write $PLIST and load it ($LAUNCHCTL bootstrap gui/$(id -u)):"
  echo "$PLIST_XML"
else
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.cortados"
  if [ "$SAME" = 0 ]; then
    printf '%s\n' "$PLIST_XML" > "$PLIST.tmp" && mv "$PLIST.tmp" "$PLIST"
    chmod 644 "$PLIST"
    say "archive cleanup: wrote $PLIST"
  fi
  "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST"
  say "archive cleanup: loaded $LABEL (daily 04:30, archive/ entries older than 3 days; log $LOG)"
fi

# 3. claude CLI (read-only) -------------------------------------------------------------------------------------------
if [ -z "$CLAUDE" ]; then
  if [ -x "$HOME/.local/bin/claude" ]; then CLAUDE="$HOME/.local/bin/claude"; else CLAUDE="$(command -v claude || true)"; fi
fi
if [ -z "$CLAUDE" ] || [ ! -x "$CLAUDE" ]; then
  todo "install the claude CLI (curl -fsSL https://claude.ai/install.sh | bash), then run 'claude' once and log in"
  CLAUDE="${CLAUDE:-$HOME/.local/bin/claude}"
else
  # A provider env in this shell would make claude report that login instead of the machine's own: cleared here.
  LOGGED="$(env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN \
            "$CLAUDE" auth status --json 2>/dev/null | "$PYTHON" -I -c '
import json, sys
try:
    print("yes" if json.load(sys.stdin).get("loggedIn") is True else "no")
except Exception:
    print("unknown")' || echo unknown)"
  case "$LOGGED" in
    yes) say "claude CLI: $CLAUDE, logged in" ;;
    no) todo "log the claude CLI in on this machine: run '$CLAUDE' and complete /login (children need its login)" ;;
    *) todo "could not read '$CLAUDE auth status'; run '$CLAUDE auth status' by hand and log in if needed" ;;
  esac
fi

# 4. Provider files (presence only) -----------------------------------------------------------------------------------
if [ -s "$CONF_DIR/provider-token" ]; then
  say "provider token: $CONF_DIR/provider-token present"
else
  todo "copy the provider token to $CONF_DIR/provider-token (chmod 600)"
fi
if [ -s "$CONF_DIR/provider-url" ]; then
  say "provider url: $CONF_DIR/provider-url present"
  [ -n "$URL" ] || URL="$(head -n 1 "$CONF_DIR/provider-url" | tr -d '[:space:]')"
else
  todo "write the provider URL as seen from this machine to $CONF_DIR/provider-url (a 'here' task started on this machine reads it)"
fi
if [ -n "$URL" ] && ! [[ "$URL" =~ ^https?:// ]]; then
  die "provider url must be an http:// or https:// URL"
fi

# 5. This machine's exec-hosts entry ----------------------------------------------------------------------------------
ENTRY="$(HOST="$HOST" SSH="$SSH" ME="$ME" ROOT="$ROOT" REPO="$REPO" PY="$PYTHON" CLAUDE="$CLAUDE" P="$PATH_ENTRY" \
         GUI="$GUI" URL="$URL" "$PYTHON" -I -c '
import json, os
e = os.environ
entry = {}
if e["SSH"]:
    entry["ssh"] = e["SSH"]
entry.update({"hostnames": [e["ME"]], "root": e["ROOT"], "router": e["REPO"], "python": e["PY"],
              "claude": e["CLAUDE"], "path": e["P"], "gui": e["GUI"] == "1"})
if e["URL"]:
    entry["provider_url"] = e["URL"]
print(json.dumps({e["HOST"]: entry}, indent=2))')"
say "this machine's entry for ~/.config/cortados/exec-hosts.json (\"hosts\"; the same file on every machine,"
say "template: tables/exec-hosts.example.json):"
echo "$ENTRY"
HOSTS_FILE="${CORTADOS_EXEC_HOSTS_FILE:-$CONF_DIR/exec-hosts.json}"
if [ ! -f "$HOSTS_FILE" ]; then
  todo "create $HOSTS_FILE from tables/exec-hosts.example.json (with the entry above)"
else
  FOUND="$(cd "$REPO" && ME="$ME" "$PYTHON" -B -c '
import os, sys
from cortados_router import exec_hosts as eh
eh.short_hostname = lambda: os.environ["ME"].split(".")[0].lower()
try:
    h = eh.load(sys.argv[1]).local_entry()
except eh.HostError as exc:
    print("error: " + str(exc)); sys.exit(0)
print(h.name if h is not None and os.environ["ME"].split(".")[0].lower() in h.hostnames else "none")' "$HOSTS_FILE")"
  case "$FOUND" in
    "$HOST") say "exec hosts: $HOSTS_FILE names this machine as '$HOST'" ;;
    none) todo "add the entry above to $HOSTS_FILE: no entry names $ME in hostnames, so host \"here\" is refused here" ;;
    error:*) todo "fix $HOSTS_FILE: ${FOUND#error: }" ;;
    *) todo "$HOSTS_FILE names this machine as '$FOUND', not '$HOST'" ;;
  esac
fi

if [ "$TODO" = 0 ]; then say "ready: this machine can run start_task children"; fi
exit "$TODO"
