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
