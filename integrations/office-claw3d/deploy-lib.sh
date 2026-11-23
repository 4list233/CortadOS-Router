# Pure checks of deploy-air.sh (sourced; reads only). Tested in deploy.test.mjs with a fake dscl,
# dseditgroup and tailscale JSON: nothing here creates, changes or starts anything.
# Needs ROLE_GID, ROLE_SHELL and ROLE_REALNAME set by the caller.

die() { echo "deploy-air: $*" >&2; exit 1; }

# A user attribute's values, read as a plist and parsed exactly. Not the text form: `dscl . -read
# /Users/U KEY` exits 0 with "No such key: KEY" on stderr for an absent key (an exit-code test reads
# every key as present), prints some keys with a prefix (`dsAttrTypeNative:IsHidden: 1`) and a value
# with spaces on its own indented line (RealName). `dscl -plist` gives a dict of key -> [values], with
# an absent key simply missing (`<dict/>`). Keys match as given or behind a dsAttrType{Standard,Native}:
# prefix. Mode "one" prints the value when there is exactly one (nothing otherwise), "count" the number
# of values (0 when absent). Returns 1 when the record cannot be read or parsed: never "absent".
attr_read() {  # user attribute one|count
  local plist
  plist="$(dscl -plist . -read "/Users/$1" "$2" 2>/dev/null)" || return 1
  printf '%s\n' "$plist" | python3 -I -c '
import plistlib, sys
key, mode = sys.argv[1], sys.argv[2]
try:
    doc = plistlib.loads(sys.stdin.buffer.read())
except Exception:
    sys.exit(1)
if not isinstance(doc, dict):
    sys.exit(1)
names = {key, "dsAttrTypeStandard:" + key, "dsAttrTypeNative:" + key}
vals = []
for k, v in doc.items():
    if k in names:
        vals.extend(v if isinstance(v, list) else [v])
if mode == "count":
    print(len(vals))
elif len(vals) == 1 and isinstance(vals[0], str) and "\n" not in vals[0]:
    print(vals[0])' "$2" "$3"
}
attr() { attr_read "$1" "$2" one; }  # user attribute -> its single value ("" when absent, multi-valued or unreadable)
# The names holding a UID (comma-separated, "" when none), from a SUCCESSFUL `dscl . -list /Users
# UniqueID` only. Captured on macOS 26: exit 0, one "NAME<spaces>UID" line per account, about 130 of
# them (the _ system accounts included); a bad node exits 185 "DS Error: -14009 (eDSUnknownNodeName)".
# Returns 1 when the command fails, prints nothing, or prints any line that is not exactly NAME UID:
# an unread or unparseable directory is never "the UID is free".
uid_holders() {  # uid
  local out
  out="$(dscl . -list /Users UniqueID 2>/dev/null)" || return 1
  printf '%s\n' "$out" | python3 -I -c '
import re, sys
uid = sys.argv[1]
lines = sys.stdin.read().splitlines()
if not lines or not any(l.strip() for l in lines):
    sys.exit(1)
held = set()
for l in lines:
    m = re.fullmatch(r"(\S+)\s+(-?[0-9]+)", l)
    if not m:
        sys.exit(1)
    if m.group(2) == uid:
        held.add(m.group(1))
print(",".join(sorted(held)))' "$1"
}
# "present" | "absent" for /Users/NAME; returns 1 on anything else. Absent ONLY on the exact not-found
# signature captured on macOS 26 (`dscl . -read /Users/nosuchuser_xyz123`): exit 56, stdout empty,
# stderr exactly "<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)". Any other failure (a permission
# error, directory services down, a changed message) is unknown, never "no such account".
account_state() {  # name
  local out err rc=0 errf
  errf="$(mktemp)" || return 1
  out="$(dscl . -read "/Users/$1" RecordName 2>"$errf")" || rc=$?
  err="$(cat "$errf")"; rm -f "$errf"
  if [[ $rc == 0 ]]; then echo present; return 0; fi
  if [[ $rc == 56 && -z "$out" && "$err" == "<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)" ]]; then
    echo absent; return 0
  fi
  return 1
}
# Prints "create" (no such account, confirmed, and the UID free) or "reuse" (validated); anything else
# aborts. "reuse" also adopts an account a stopped --apply left behind, when every attribute matches.
check_role() {  # name uid home
  local name="$1" uid="$2" home="$3" holders authorities state
  holders="$(uid_holders "$uid")" || die "cannot list the accounts' UIDs (dscl . -list /Users UniqueID failed or answered unexpectedly): refusing to plan $name"
  state="$(account_state "$name")" || die "cannot establish whether $name exists (dscl . -read failed with something other than eDSRecordNotFound): refusing to plan it"
  if [[ "$state" == absent ]]; then
    [[ -z "$holders" ]] || die "UID $uid is taken by '$holders' (wanted for $name): pick another UID deliberately"
    echo create; return
  fi
  [[ "$holders" == "$name" ]] || die "$name exists but UID $uid is held by '${holders:-nobody}' (exactly $name must hold it)"
  [[ "$(attr "$name" UniqueID)" == "$uid" ]] || die "$name has UID $(attr "$name" UniqueID), expected $uid"
  [[ "$(attr "$name" PrimaryGroupID)" == "$ROLE_GID" ]] || die "$name: primary group is not $ROLE_GID"
  [[ "$(attr "$name" NFSHomeDirectory)" == "$home" ]] || die "$name: home is not $home"
  [[ "$(attr "$name" UserShell)" == "$ROLE_SHELL" ]] || die "$name: shell is not $ROLE_SHELL (login must stay disabled)"
  [[ "$(attr "$name" Password)" == '*' ]] || die "$name: password is not '*' (login must stay disabled)"
  # Any AuthenticationAuthority value (ShadowHash, Kerberosv5, SecureToken, even an empty one) refuses;
  # an unreadable record refuses too. Only a key that is really absent passes.
  authorities="$(attr_read "$name" AuthenticationAuthority count)" || die "$name: cannot read its AuthenticationAuthority"
  [[ "$authorities" == 0 ]] || die "$name has an AuthenticationAuthority (a password or token login): not a role user"
  [[ "$(attr "$name" IsHidden)" == 1 ]] || die "$name: not hidden"
  [[ "$(attr "$name" RealName)" == "$ROLE_REALNAME" ]] || die "$name: RealName is not '$ROLE_REALNAME'"
  admin_negative "$name" || exit 1
  echo reuse
}

# 0 only on dseditgroup's exact confirmed negative: exit 67 with stdout "no NAME is NOT a member of
# admin" (captured on macOS 26: a member, also through a nested group, is exit 0 "yes NAME is a member
# of admin"; an unknown user exit 64 with "Unable to find the user record" on stderr). Any other exit
# status or output -- a member, a directory-services failure, an unexpected format -- refuses: a lookup
# that failed never reads as "not an admin".
admin_negative() {  # name
  local out rc=0
  out="$(dseditgroup -o checkmember -m "$1" admin 2>/dev/null)" || rc=$?
  if [[ $rc == 67 && "$out" == "no $1 is NOT a member of admin" ]]; then return 0; fi
  if [[ $rc == 0 && "$out" == "yes $1 is a member of admin" ]]; then
    echo "deploy-air: $1 is an admin" >&2; return 1
  fi
  echo "deploy-air: $1: cannot establish that it is not an admin (dseditgroup exit $rc, unexpected answer)" >&2
  return 1
}

# `test ARGS` run as root (files under the role users' 0700 homes): prints yes | no, and returns 1
# (printing nothing) when sudo or test itself failed. `sudo test` alone cannot tell "false" (1) from
# a refused sudo (also 1), so the answer is the shell's own report: an unanswered check is never "no".
root_test() {  # test-args...
  local out
  out="$(sudo /bin/sh -c 'test "$@"; echo "cortados-test-rc=$?"' sh "$@" 2>/dev/null)" || return 1
  case "$out" in
    cortados-test-rc=0) echo yes ;;
    cortados-test-rc=1) echo no ;;
    *) return 1 ;;
  esac
}

# 0 loaded, 1 confirmed not loaded, 2 unknown, for a system LaunchDaemon. Not loaded ONLY on the
# answer captured on macOS 26 for an unknown label: exit 113 with stderr holding
# `Could not find service "LABEL" in domain for system`. Any other failure is unknown, never "stopped".
launchd_state() {  # label
  local err rc=0
  err="$(launchctl print "system/$1" 2>&1 >/dev/null)" || rc=$?
  [[ $rc == 0 ]] && return 0
  [[ $rc == 113 && "$err" == *"Could not find service \"$1\" in domain for system"* ]] && return 1
  return 2
}

serve_port_state() {  # port target < `tailscale serve status --json`: free | ours | funnel | other
  python3 -I -c '
import json, sys
port, target = sys.argv[1], sys.argv[2]
try:
    doc = json.load(sys.stdin) or {}
except ValueError:
    print("other"); sys.exit()
tcp = doc.get("TCP") or {}
funnel = [k for k, v in (doc.get("AllowFunnel") or {}).items() if v and k.rsplit(":", 1)[-1] == port]
web = {k: v for k, v in (doc.get("Web") or {}).items() if k.rsplit(":", 1)[-1] == port}
if funnel:
    print("funnel")
elif port not in tcp and not web:
    print("free")
elif len(web) == 1 and (tcp.get(port) or {}).get("HTTPS") and \
        set((list(web.values())[0].get("Handlers") or {})) == {"/"} and \
        (list(web.values())[0]["Handlers"]["/"] or {}).get("Proxy") == target:
    print("ours")
else:
    print("other")' "$1" "$2"
}

# The serve config as JSON, from a SUCCESSFUL `tailscale serve status --json` only. A failed command
# (tailscaled down, not logged in, a CLI error) returns 1 and prints nothing: an unverified config is
# never read as "nothing served". Empty output from a successful command is an empty config.
serve_status_json() {  # tailscale-binary
  local out
  out="$("$1" serve status --json 2>/dev/null)" || return 1
  [[ -n "${out//[[:space:]]/}" ]] || out='{}'
  printf '%s\n' "$out"
}
serve_state() {  # tailscale-binary port target: free | ours | funnel | other; 1 when the status cannot be read
  local json
  json="$(serve_status_json "$1")" || return 1
  printf '%s\n' "$json" | serve_port_state "$2" "$3"
}

# ---- activation transaction (deploy-air.sh steps 7-8) ---------------------------------------------
# Everything is staged before it runs; this switches the links, promotes the staged files (launchers,
# plists, a new office token), restarts the daemons and runs the health check. Any failure, or INT/
# TERM/HUP, rolls back: both daemons stopped, every switch undone in reverse (each replaced file comes
# back from its `.prev` copy, each replaced link to its old target, each new one removed), then the
# daemons that were loaded before are bootstrapped again. Only tx_commit, after the health check,
# drops the `.prev` copies. Steps return codes and never exit, so a failure always reaches the rollback.
#
# The caller sets TX_LINKS ("target|link|user", user may be empty), TX_FILES ("staged|dest"),
# TX_LABELS (daemons, in start order) and defines the primitives:
#   fs_readlink LINK      the link's target; nothing (0) when absent; 1 when it cannot be read
#   fs_exists PATH        0 exists, 1 confirmed absent, anything else unknown (fails the step)
#   fs_link TARGET LINK USER   atomic (rename over)           fs_rm PATH USER
#   fs_backup DEST        copy DEST to DEST.prev, owner/mode kept
#   fs_promote STAGED DEST     put STAGED at DEST by rename   fs_restore DEST   DEST.prev back over DEST
#   daemon_loaded LABEL   0 loaded, 1 confirmed not loaded, anything else unknown
#   daemon_start LABEL    daemon_stop LABEL (returns 1 if it does not stop)
#   health_check          0 when the new release answers as it must
# bash 3.2 (macOS /bin/bash) with set -u: an empty array is expanded as ${a[@]+"${a[@]}"}.
TX_UNDO=(); TX_WAS_LOADED=(); TX_PREV=()
tx_undo() { TX_UNDO+=("$(printf '%q ' "$@")"); }
tx_begin() {  # 1 when a daemon's state cannot be read (nothing changed yet)
  TX_UNDO=(); TX_WAS_LOADED=(); TX_PREV=()
  local l rc
  for l in ${TX_LABELS[@]+"${TX_LABELS[@]}"}; do
    rc=0; daemon_loaded "$l" || rc=$?
    case $rc in
      0) TX_WAS_LOADED+=("$l") ;;
      1) ;;
      *) echo "deploy-air: cannot tell whether $l is loaded" >&2; return 1 ;;
    esac
  done
}
tx_link() {  # target link user
  local prev; prev="$(fs_readlink "$2")" || return 1   # unreadable is not "absent": the undo would delete it
  if [[ -n "$prev" ]]; then tx_undo fs_link "$prev" "$2" "$3"; else tx_undo fs_rm "$2" "$3"; fi
  fs_link "$1" "$2" "$3"
}
tx_file() {  # staged dest
  local e=0; fs_exists "$2" || e=$?
  if [[ $e == 0 ]]; then
    fs_backup "$2" || return 1
    TX_PREV+=("$2.prev"); tx_undo fs_restore "$2"
  elif [[ $e == 1 ]]; then
    tx_undo fs_rm "$2" ""
  else
    echo "deploy-air: cannot tell whether $2 exists" >&2; return 1
  fi
  fs_promote "$1" "$2"
}
tx_restart() {  # label
  local rc=0; daemon_loaded "$1" || rc=$?
  case $rc in
    0) daemon_stop "$1" || return 1 ;;
    1) ;;
    *) echo "deploy-air: cannot tell whether $1 is loaded" >&2; return 1 ;;
  esac
  daemon_start "$1"
}
tx_activate() {
  local e t l u
  for e in ${TX_LINKS[@]+"${TX_LINKS[@]}"}; do
    IFS='|' read -r t l u <<<"$e"
    tx_link "$t" "$l" "$u" || { echo "deploy-air: switching $l failed" >&2; return 1; }
  done
  for e in ${TX_FILES[@]+"${TX_FILES[@]}"}; do
    IFS='|' read -r t l <<<"$e"
    tx_file "$t" "$l" || { echo "deploy-air: installing $l failed" >&2; return 1; }
  done
  for l in ${TX_LABELS[@]+"${TX_LABELS[@]}"}; do
    tx_restart "$l" || { echo "deploy-air: (re)starting $l failed" >&2; return 1; }
  done
  health_check || { echo "deploy-air: the new release failed its health check" >&2; return 1; }
}
tx_rollback() {  # 0 when every undo step and restart succeeded
  local rc=0 i l s
  echo "deploy-air: ROLLBACK: restoring the previous release" >&2
  for l in ${TX_LABELS[@]+"${TX_LABELS[@]}"}; do
    s=0; daemon_loaded "$l" || s=$?
    case $s in
      0) daemon_stop "$l" || { echo "deploy-air: rollback: $l did not stop" >&2; rc=1; } ;;
      1) ;;
      *) echo "deploy-air: rollback: cannot tell whether $l is loaded; trying to stop it" >&2
         daemon_stop "$l" || { echo "deploy-air: rollback: $l did not stop" >&2; rc=1; } ;;
    esac
  done
  for ((i = ${#TX_UNDO[@]} - 1; i >= 0; i--)); do
    eval "${TX_UNDO[i]}" || { echo "deploy-air: rollback step failed: ${TX_UNDO[i]}" >&2; rc=1; }
  done
  TX_UNDO=()
  for l in ${TX_WAS_LOADED[@]+"${TX_WAS_LOADED[@]}"}; do
    daemon_start "$l" || { echo "deploy-air: rollback: $l did not start again" >&2; rc=1; }
  done
  return $rc
}
tx_commit() {
  local p
  for p in ${TX_PREV[@]+"${TX_PREV[@]}"}; do fs_rm "$p" "" || echo "deploy-air: could not remove $p (harmless)" >&2; done
  TX_UNDO=(); TX_PREV=()
}
# 0: activated and committed. 1: rolled back cleanly. 2: the rollback itself failed (investigate).
tx_run() {
  tx_begin || { echo "deploy-air: nothing was changed" >&2; return 1; }
  trap 'tx_rollback; exit 1' INT TERM HUP
  if tx_activate; then
    trap - INT TERM HUP
    tx_commit; return 0
  fi
  trap - INT TERM HUP
  if tx_rollback; then return 1; fi
  return 2
}
