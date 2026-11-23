# Pure checks of deploy-air.sh (sourced; reads only). Tested in deploy.test.mjs with a fake dscl,
# dseditgroup and tailscale JSON: nothing here creates, changes or starts anything.
# Needs ROLE_GID and ROLE_SHELL set by the caller.

die() { echo "deploy-air: $*" >&2; exit 1; }

attr() {  # user attribute -> its single value ("" when absent or multi-valued)
  dscl . -read "/Users/$1" "$2" 2>/dev/null | awk -v k="$2:" '$1 == k && NF == 2 { print $2; exit }'
}
uid_holders() { dscl . -list /Users UniqueID | awk -v u="$1" '$2 == u { print $1 }' | sort -u | paste -s -d, -; }
# Prints "create" (no such account, UID free) or "reuse" (validated); anything else aborts.
check_role() {  # name uid home
  local name="$1" uid="$2" home="$3" holders
  holders="$(uid_holders "$uid")"
  if ! dscl . -read "/Users/$name" >/dev/null 2>&1; then
    [[ -z "$holders" ]] || die "UID $uid is taken by '$holders' (wanted for $name): pick another UID deliberately"
    echo create; return
  fi
  [[ "$holders" == "$name" ]] || die "$name exists but UID $uid is held by '${holders:-nobody}' (exactly $name must hold it)"
  [[ "$(attr "$name" UniqueID)" == "$uid" ]] || die "$name has UID $(attr "$name" UniqueID), expected $uid"
  [[ "$(attr "$name" PrimaryGroupID)" == "$ROLE_GID" ]] || die "$name: primary group is not $ROLE_GID"
  [[ "$(attr "$name" NFSHomeDirectory)" == "$home" ]] || die "$name: home is not $home"
  [[ "$(attr "$name" UserShell)" == "$ROLE_SHELL" ]] || die "$name: shell is not $ROLE_SHELL (login must stay disabled)"
  [[ "$(attr "$name" Password)" == '*' ]] || die "$name: password is not '*' (login must stay disabled)"
  if dscl . -read "/Users/$name" AuthenticationAuthority >/dev/null 2>&1; then
    die "$name has an AuthenticationAuthority (a password or token login): not a role user"
  fi
  [[ "$(attr "$name" IsHidden)" == 1 ]] || die "$name: not hidden"
  if dseditgroup -o checkmember -m "$name" admin >/dev/null 2>&1; then die "$name is an admin"; fi
  echo reuse
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
#   fs_readlink LINK      the link's target, or nothing      fs_exists PATH
#   fs_link TARGET LINK USER   atomic (rename over)           fs_rm PATH USER
#   fs_backup DEST        copy DEST to DEST.prev, owner/mode kept
#   fs_promote STAGED DEST     put STAGED at DEST by rename   fs_restore DEST   DEST.prev back over DEST
#   daemon_loaded LABEL   daemon_start LABEL   daemon_stop LABEL (returns 1 if it does not stop)
#   health_check          0 when the new release answers as it must
# bash 3.2 (macOS /bin/bash) with set -u: an empty array is expanded as ${a[@]+"${a[@]}"}.
TX_UNDO=(); TX_WAS_LOADED=(); TX_PREV=()
tx_undo() { TX_UNDO+=("$(printf '%q ' "$@")"); }
tx_begin() {
  TX_UNDO=(); TX_WAS_LOADED=(); TX_PREV=()
  local l
  for l in ${TX_LABELS[@]+"${TX_LABELS[@]}"}; do
    if daemon_loaded "$l"; then TX_WAS_LOADED+=("$l"); fi
  done
}
tx_link() {  # target link user
  local prev; prev="$(fs_readlink "$2")" || prev=""
  if [[ -n "$prev" ]]; then tx_undo fs_link "$prev" "$2" "$3"; else tx_undo fs_rm "$2" "$3"; fi
  fs_link "$1" "$2" "$3"
}
tx_file() {  # staged dest
  if fs_exists "$2"; then
    fs_backup "$2" || return 1
    TX_PREV+=("$2.prev"); tx_undo fs_restore "$2"
  else
    tx_undo fs_rm "$2" ""
  fi
  fs_promote "$1" "$2"
}
tx_restart() {  # label
  if daemon_loaded "$1"; then daemon_stop "$1" || return 1; fi
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
  local rc=0 i l
  echo "deploy-air: ROLLBACK: restoring the previous release" >&2
  for l in ${TX_LABELS[@]+"${TX_LABELS[@]}"}; do
    if daemon_loaded "$l"; then daemon_stop "$l" || { echo "deploy-air: rollback: $l did not stop" >&2; rc=1; }; fi
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
  tx_begin
  trap 'tx_rollback; exit 1' INT TERM HUP
  if tx_activate; then
    trap - INT TERM HUP
    tx_commit; return 0
  fi
  trap - INT TERM HUP
  if tx_rollback; then return 1; fi
  return 2
}
