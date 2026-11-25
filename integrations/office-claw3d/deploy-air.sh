#!/bin/bash
# Install, upgrade or publish the CortadOS Office on the Air: Claw3D Studio + the read-only office
# bridge, each as its own hidden macOS role user under a LaunchDaemon, both bound to loopback.
# Runbook and rationale: integrations/office-claw3d/DEPLOY.md.
#
# NOT RUN BY ITS AUTHOR. Written for issue #77; the owner runs it on the Air when ready.
# Every mode is a dry run (preconditions + the plan, nothing changed) unless given --apply.
#
#   deploy-air.sh [--apply] [--rotate-office-token]   install, or upgrade to this checkout's HEAD
#   deploy-air.sh --publish-tailnet [--apply]         Studio on the tailnet: tailscale serve, HTTPS, tailnet only
#   deploy-air.sh --unpublish-tailnet [--apply]       take that serve down again
#
# Order: every check runs BEFORE the first change, so a refused run leaves the machine as it was.
# Then the new Node, adapter copy, Claw3D build, launchers, plists and any new office token are
# staged beside the running ones. Only after all of them succeed is the release activated, as one
# transaction (tx_run, deploy-lib.sh): links switched, staged files promoted (the replaced ones kept
# as .prev), daemons restarted, health checked. A failure anywhere in it rolls everything back and
# restarts the old daemons; a rotated token's old registration is revoked only after success.
# It never prints a token: secrets go file-to-file under umask 077.
set -euo pipefail

# ---- pins (change deliberately, re-verify the hashes) --------------------------------
NODE_VERSION="v24.22.3"                    # Node LTS "Krypton", 2026-11-10
NODE_TARBALL="node-${NODE_VERSION}-darwin-arm64.tar.gz"
NODE_SHA256="ff6e17d4874b76d272212b908cc020d6e815fba5a6c7eaaa06add725840307fd"  # nodejs.org SHASUMS256.txt
CLAW3D_REPO="https://github.com/iamlukethedev/Claw3D.git"
CLAW3D_COMMIT="51337f7f250459af61a7cf92b7b0015d59faed5c"   # the commit the patches and PROTOCOL.md were checked on
EXPECTED_HOST="${CORTADOS_OFFICE_EXPECTED_HOST:-Forests-MacBook-Air}"

ROOT="/opt/cortados-office"
BRIDGE_USER="_cortados-office"; BRIDGE_UID=480; BRIDGE_HOME="$ROOT/home/bridge"
STUDIO_USER="_cortados-claw3d"; STUDIO_UID=481; STUDIO_HOME="$ROOT/home/studio"
ROLE_SHELL="/usr/bin/false"; ROLE_GID=20; ROLE_REALNAME="CortadOS Office role user"
BRIDGE_PORT=18789                          # Claw3D's default upstream gateway port
# Studio's upstream is the loopback bridge, named by this one host. The URL in Studio's settings file
# (below) and Claw3D's UPSTREAM_ALLOWLIST (compared with `new URL(url).hostname`) are both built from
# it, so they cannot drift. `localhost`: the bridge allows the Origin Studio's proxy derives from it
# (bridge.mjs). The allowlist names a hostname only (Claw3D compares no port); the port is pinned by
# the settings file's URL.
GATEWAY_HOST=localhost
GATEWAY_URL="ws://$GATEWAY_HOST:$BRIDGE_PORT"
STUDIO_PORT=3000
# The tailnet HTTPS port for Studio. The Air already serves 443 (Funnel: /mcp only) and 8444
# (Serve: the provider); --publish-tailnet refuses any port already in `tailscale serve status`.
TAILNET_PORT=8446
PROVIDER_OFFICE_URL="http://127.0.0.1:8432/v1/cortados/office"
# The provider's scoped client (office:read only), in one of two slots: a rotation registers the new
# token in the other slot and revokes the old one only after the new release passed its health check.
CLIENT_SLOTS=(claw3d-office claw3d-office-b)
BRIDGE_LABEL="com.cortados.office-bridge"; STUDIO_LABEL="com.cortados.office-studio"
OFFICE_TOKEN="$BRIDGE_HOME/secrets/office-token"
BRIDGE_SECRET="$BRIDGE_HOME/secrets/bridge-token"
# Only read by launchers before #77 fix round 1 (as CLAW3D_GATEWAY_TOKEN, which Claw3D's proxy ignores): no
# longer created or used; an existing one is left for a rollback to the previous launcher (step 5b).
GATEWAY_SECRET="$STUDIO_HOME/secrets/gateway-token"
ACCESS_TOKEN="$STUDIO_HOME/secrets/studio-access-token"
# Claw3D's state dir, set explicitly in run-studio.sh (OPENCLAW_STATE_DIR) so the path does not hang
# on launchd's HOME. It is the same path Claw3D defaults to for this user (<home>/.openclaw), so state
# an earlier release wrote there is kept. Claw3D 51337f7's gateway proxy reads its upstream ONLY from
# <stateDir>/claw3d/settings.json {gateway:{url,token,adapterType}} (server/studio-settings.js
# loadUpstreamGatewaySettings), never from CLAW3D_GATEWAY_URL/_TOKEN: without this file it falls
# back to ws://localhost:18789 with no token and refuses every connect (studio.gateway_token_missing).
STUDIO_STATE="$STUDIO_HOME/.openclaw"
STUDIO_SETTINGS="$STUDIO_STATE/claw3d/settings.json"
# The health check's first pass must start within this many seconds of the restart (a check that is
# running then finishes on its own timeouts, at most about 21 s), and then hold 10 s later.
HEALTH_DEADLINE_S=60
STAGE="$ROOT/stage"                        # root 0700: launchers, plists and a new token, before activation
ADAPTER_FILES=(adapter.mjs bridge.mjs sprites.mjs claw3d/avatar-profile-hint.patch claw3d/studio-login.patch
  claw3d/studio-login.cjs claw3d/cortados-rooms.patch claw3d/system-fonts.patch claw3d/proxy-auth.patch)

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# Every later step runs from /, once, here (not a `(cd / && ...)` per call): a command run as a role
# user (sudo -u, e.g. as_studio's clone, switch_link, fs_rm, the prune) inherits the caller's cwd,
# and the role users cannot read the owner's home. Run from ~/office-deploy-..., step 4's clone died
# with "Unable to read current working directory: Permission denied" (the Air, 2026-11-23). One `cd`
# also covers any role-user call added later. Every path below is absolute (HERE and REPO are
# resolved above, TMP comes from mktemp -d); $0 is only echoed.
cd /
APPLY=0; MODE=install; ROTATE=0
usage() { echo "usage: $0 [--apply] [--rotate-office-token] | --publish-tailnet [--apply] | --unpublish-tailnet [--apply]" >&2; exit 2; }
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --rotate-office-token) ROTATE=1 ;;
    --publish-tailnet) [[ $MODE == install ]] || usage; MODE=publish ;;
    --unpublish-tailnet) [[ $MODE == install ]] || usage; MODE=unpublish ;;
    *) usage ;;
  esac
done
[[ $ROTATE -eq 0 || $MODE == install ]] || usage

step() { printf '\n== %s\n' "$*"; }
run() {  # print the command; run it only with --apply
  printf '  $ %s\n' "$*"
  if [[ $APPLY -eq 1 ]]; then "$@"; fi
}
# die, attr, uid_holders, account_state, check_role, root_test, launchd_state, serve_port_state: pure
# checks, tested in deploy.test.mjs
. "$HERE/deploy-lib.sh"
owner_of() { stat -f '%Su:%Sg' "$1"; }
mode_of() { stat -f '%Lp' "$1"; }
# A read as root (files under the role users' 0700 homes): 0 true, 1 false; a check sudo or test could
# not answer aborts (root_test, deploy-lib.sh), never reads as "absent".
as_root_test() {
  local a; a="$(root_test "$@")" || die "cannot check 'test $*' as root (sudo or test failed): refusing to guess"
  [[ "$a" == yes ]]
}
# Root never writes inside a directory a role user owns (#77 fix round 2). Such a user can swap any
# path component there for a link at any moment, after every check, so a write as root through it
# could overwrite any file on the machine (shown by the review with settings.json.next planted as a
# link to a root-owned file). Every write there is done AS the owning role user instead: a link it
# planted then reaches only what it could write anyway. Root still reads the staged content from its
# own 0700 STAGE and passes it on stdin.
# role_write: stdin -> DEST, written as USER, 0600 (umask), through a new file mktemp creates
# exclusively beside DEST (never an existing name, never a link), then renamed over DEST; the temp
# file is removed if the copy or the rename fails.
role_write() {  # user dest < content
  sudo -u "$1" /bin/sh -c 'umask 077; t="$(mktemp "$1.next.XXXXXX")" || exit 1
if cat > "$t" && mv -f "$t" "$1"; then exit 0; fi
rm -f "$t"; exit 1' role-write "$2"
}
role_write_from() { role_write "$1" "$3" < "$2"; }        # user source dest (source readable by the caller)
promote_as() { sudo cat "$2" | role_write "$1" "$3"; }    # user staged dest (staged: root's STAGE)

# ---- preconditions common to every mode (they only read) -----------------------------
step "Preconditions ($MODE)"
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "macOS on Apple silicon only"
[[ "$(scutil --get LocalHostName 2>/dev/null || hostname -s)" == "$EXPECTED_HOST" ]] \
  || die "this is not $EXPECTED_HOST (set CORTADOS_OFFICE_EXPECTED_HOST to override deliberately)"
[[ $EUID -ne 0 ]] || die "run as the owner (the provider's user), not root; sudo is used per step"

# ---- the role users: exact, distinct UIDs, and a reused account must be exactly what we create
#      (unpublish skips them: taking a serve down must work whatever state the install is in)
if [[ $MODE != unpublish ]]; then
  [[ "$BRIDGE_UID" != "$STUDIO_UID" && "$BRIDGE_USER" != "$STUDIO_USER" ]] || die "the two roles need distinct names and UIDs"
  BRIDGE_ACCOUNT="$(check_role "$BRIDGE_USER" "$BRIDGE_UID" "$BRIDGE_HOME")"
  STUDIO_ACCOUNT="$(check_role "$STUDIO_USER" "$STUDIO_UID" "$STUDIO_HOME")"
  echo "  ok: host, arch; $BRIDGE_USER ($BRIDGE_UID): $BRIDGE_ACCOUNT; $STUDIO_USER ($STUDIO_UID): $STUDIO_ACCOUNT"
fi

# ======================================================================================
# --publish-tailnet / --unpublish-tailnet: a separate, explicit step on an existing install
# ======================================================================================
if [[ $MODE != install ]]; then
  TS="$(command -v tailscale || true)"
  [[ -n "$TS" ]] || TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  [[ -x "$TS" ]] || die "tailscale CLI not found"
  # What the serve config says about TAILNET_PORT: free | ours | funnel | other. A failed status
  # command aborts: an unread config is never taken for a free port (serve_state, deploy-lib.sh).
  state="$(serve_state "$TS" "$TAILNET_PORT" "http://127.0.0.1:$STUDIO_PORT")" \
    || die "tailscale serve status failed: cannot verify port $TAILNET_PORT, changing nothing (is tailscaled up and logged in?)"
  if [[ $MODE == unpublish ]]; then
    step "Unpublish Studio from the tailnet (https port $TAILNET_PORT)"
    case "$state" in
      free) echo "  nothing served on $TAILNET_PORT"; exit 0 ;;
      ours) run "$TS" serve --https="$TAILNET_PORT" off ;;
      funnel) run "$TS" funnel --https="$TAILNET_PORT" off; run "$TS" serve --https="$TAILNET_PORT" off ;;
      *) die "port $TAILNET_PORT serves something else; not touching it (see tailscale serve status)" ;;
    esac
    exit 0
  fi
  step "Publish Studio on the tailnet (tailscale serve, HTTPS $TAILNET_PORT, tailnet only, never funnel)"
  [[ "$STUDIO_ACCOUNT" == reuse ]] || die "$STUDIO_USER does not exist: install first"
  [[ -f "$ROOT/bin/run-studio.sh" ]] || die "install first (no $ROOT/bin/run-studio.sh)"
  as_root_test -s "$ACCESS_TOKEN" || die "Studio has no access token file: re-run the install first"
  grep -q 'STUDIO_ACCESS_TOKEN' "$ROOT/bin/run-studio.sh" || die "the installed launcher predates the access token: re-run the install"
  grep -q 'UPSTREAM_ALLOWLIST' "$ROOT/bin/run-studio.sh" || die "the installed launcher predates the upstream allowlist (the Office cannot reach its bridge): re-run the install"
  grep -q 'OPENCLAW_STATE_DIR' "$ROOT/bin/run-studio.sh" || die "the installed launcher predates Studio's gateway settings file (the Office cannot reach its bridge): re-run the install"
  as_root_test -s "$STUDIO_SETTINGS" || die "Studio has no gateway settings file ($STUDIO_SETTINGS): re-run the install"
  # The gate must already be on, on loopback, before anything is published: / refuses without the
  # cookie and the login form answers.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$STUDIO_PORT/" || true)"
  [[ "$code" == 401 ]] || die "Studio does not refuse a request without its access cookie (got $code): not publishing"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$STUDIO_PORT/cortados-login" || true)"
  [[ "$code" == 200 ]] || die "Studio's login form does not answer (got $code): is the studio-login patch built in?"
  case "$state" in
    free) ;;
    ours) echo "  already published on $TAILNET_PORT (tailnet only): nothing to do" ;;
    funnel) die "port $TAILNET_PORT is FUNNELED (public): run --unpublish-tailnet and investigate" ;;
    *) die "port $TAILNET_PORT already serves something else: pick another TAILNET_PORT deliberately" ;;
  esac
  [[ $APPLY -eq 1 ]] || echo "  DRY RUN: nothing below is executed. Re-run with --apply."
  if [[ "$state" == free ]]; then
    run "$TS" serve --bg --https="$TAILNET_PORT" "http://127.0.0.1:$STUDIO_PORT"
  fi
  if [[ $APPLY -eq 1 ]]; then
    after="$(serve_state "$TS" "$TAILNET_PORT" "http://127.0.0.1:$STUDIO_PORT")" || after="unreadable"
    if [[ "$after" != ours ]]; then
      "$TS" serve --https="$TAILNET_PORT" off \
        || die "after serve, port $TAILNET_PORT is '$after', not a tailnet-only proxy to Studio, AND taking it down failed: run --unpublish-tailnet / tailscale serve status now"
      die "after serve, port $TAILNET_PORT is '$after', not a tailnet-only proxy to Studio: taken down again"
    fi
    name="$("$TS" status --json | python3 -I -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
    echo "  published (tailnet only). Log in once per device at: https://$name:$TAILNET_PORT/cortados-login"
  fi
  exit 0
fi

# ======================================================================================
# install / upgrade
# ======================================================================================
[[ -f "$HERE/claw3d/avatar-profile-hint.patch" && -f "$HERE/claw3d/studio-login.patch" \
  && -f "$HERE/claw3d/cortados-rooms.patch" && -f "$HERE/claw3d/system-fonts.patch" \
  && -f "$HERE/claw3d/proxy-auth.patch" && -f "$HERE/bridge.mjs" ]] \
  || die "run from a CortadOS-Router checkout"
xcode-select -p >/dev/null 2>&1 || die "git needs the Xcode command line tools (xcode-select --install)"
# captured first: a failed `git status` prints nothing, which must not read as "clean"
DIRTY="$(git -C "$REPO" status --porcelain -- integrations/office-claw3d)" || die "git status failed: cannot confirm a clean checkout"
[[ -z "$DIRTY" ]] || die "integrations/office-claw3d has uncommitted changes: the install is pinned to a commit"
SRC_SHA="$(git -C "$REPO" rev-parse HEAD)"
BUILD_ID="${CLAW3D_COMMIT:0:12}-${SRC_SHA:0:12}"   # the patches come from this checkout
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$PROVIDER_OFFICE_URL" || true)"
[[ "$code" == 401 || "$code" == 403 ]] || die "provider office endpoint not answering 401 without a token (got $code)"

# ---- the existing install, if any: validated before anything changes ------------------
link_into() {  # path dir: path is absent, or a symlink to an entry of dir
  [[ ! -e "$1" && ! -L "$1" ]] && return 0
  [[ -L "$1" ]] || die "$1 exists and is not a link (unknown layout): move it aside first"
  case "$(readlink "$1")" in "$2"/*) ;; *) die "$1 points outside $2" ;; esac
}
UPGRADE=0
# ROOT's absence (a fresh install) is read as the owner: only where its directory can be searched
[[ -e "$ROOT" || -L "$ROOT" ]] || link_target "$ROOT" >/dev/null \
  || die "cannot tell whether $ROOT exists (${ROOT%/*} cannot be searched)"
if [[ -e "$ROOT" || -L "$ROOT" ]]; then
  UPGRADE=1
  [[ -d "$ROOT" && ! -L "$ROOT" ]] || die "$ROOT is not a plain directory"
  [[ "$(owner_of "$ROOT")" == root:wheel && "$(mode_of "$ROOT")" == 755 ]] || die "$ROOT must be root:wheel 0755"
  for d in home bin adapters stage; do
    [[ ! -e "$ROOT/$d" ]] || [[ "$(owner_of "$ROOT/$d")" == root:wheel && ! -L "$ROOT/$d" ]] || die "$ROOT/$d must be root:wheel"
  done
  # The existence checks below run as the owner: a directory it cannot search would make every entry
  # read as absent, so the ones they look into must be searchable (as the install creates them).
  for d in home bin adapters; do
    [[ ! -e "$ROOT/$d" ]] || [[ "$(mode_of "$ROOT/$d")" == 755 ]] || die "$ROOT/$d must be 0755"
  done
  for f in "$ROOT/bin/run-bridge.sh" "$ROOT/bin/run-studio.sh"; do
    [[ ! -e "$f" ]] || [[ "$(owner_of "$f")" == root:wheel && ! -L "$f" ]] || die "$f must be a root:wheel file"
  done
  link_into "$ROOT/adapter" "$ROOT/adapters"
  link_into "$ROOT/node" "$ROOT"
  if [[ -e "$ROOT/claw3d" ]]; then
    [[ "$(owner_of "$ROOT/claw3d")" == "$STUDIO_USER:staff" && ! -L "$ROOT/claw3d" && "$(mode_of "$ROOT/claw3d")" == 755 ]] \
      || die "$ROOT/claw3d must belong to $STUDIO_USER, 0755"
    [[ ! -e "$ROOT/claw3d/builds" ]] || [[ -d "$ROOT/claw3d/builds" && ! -L "$ROOT/claw3d/builds" && "$(mode_of "$ROOT/claw3d/builds")" == 755 ]] \
      || die "$ROOT/claw3d/builds must be a 0755 directory"
    link_into "$ROOT/claw3d/current" "$ROOT/claw3d/builds"
    for e in "$ROOT/claw3d"/*; do
      case "${e##*/}" in builds|current|current.new|'*') ;; *) die "unexpected $e (unknown layout): move it aside first" ;; esac
    done
  fi
  [[ ! -e "$BRIDGE_HOME" ]] || [[ "$(owner_of "$BRIDGE_HOME")" == "$BRIDGE_USER:staff" && "$(mode_of "$BRIDGE_HOME")" == 700 ]] \
    || die "$BRIDGE_HOME must be $BRIDGE_USER 0700"
  [[ ! -e "$STUDIO_HOME" ]] || [[ "$(owner_of "$STUDIO_HOME")" == "$STUDIO_USER:staff" && "$(mode_of "$STUDIO_HOME")" == 700 ]] \
    || die "$STUDIO_HOME must be $STUDIO_USER 0700"
  if [[ -d "$ROOT/node-$NODE_VERSION" ]]; then
    [[ "$(owner_of "$ROOT/node-$NODE_VERSION")" == root:wheel ]] || die "$ROOT/node-$NODE_VERSION must be root:wheel"
  fi
fi
for label in "$BRIDGE_LABEL" "$STUDIO_LABEL"; do
  p="/Library/LaunchDaemons/$label.plist"
  [[ ! -e "$p" ]] || [[ "$(owner_of "$p")" == root:wheel && "$(mode_of "$p")" == 644 && ! -L "$p" ]] || die "$p must be root:wheel 0644"
done
# 0 loaded, 1 confirmed not loaded; a launchctl answer that is neither aborts (launchd_state, deploy-lib.sh).
loaded() {
  local rc=0; launchd_state "$1" || rc=$?
  [[ $rc == 0 || $rc == 1 ]] || die "cannot tell whether $1 is loaded (launchctl print answered unexpectedly)"
  return $rc
}

# Node: reuse the pinned version when it is there and answers with that version.
NODE_PLAN=install
if [[ -x "$ROOT/node-$NODE_VERSION/bin/node" ]]; then
  [[ "$("$ROOT/node-$NODE_VERSION/bin/node" --version)" == "$NODE_VERSION" ]] || die "$ROOT/node-$NODE_VERSION is not $NODE_VERSION"
  NODE_PLAN=keep
elif [[ -e "$ROOT/node-$NODE_VERSION" ]]; then
  die "$ROOT/node-$NODE_VERSION exists without a working node (a failed earlier run?): remove it first"
fi

# The staged adapter copy and Claw3D build for this commit, when an earlier run left them.
ADAPTER_DIR="$ROOT/adapters/$SRC_SHA"
BUILD="$ROOT/claw3d/builds/$BUILD_ID"
# The prune (step 9) keeps these as the rollback. Empty ONLY when confirmed absent (link_target,
# deploy-lib.sh): a read that failed aborts here, before any change, never "nothing to keep".
CURRENT_BUILD="$(link_target "$ROOT/claw3d/current")" \
  || die "cannot read $ROOT/claw3d/current (not a readable link, or its directory cannot be searched): the prune would not know the rollback build"
PREVIOUS_ADAPTER="$(link_target "$ROOT/adapter")" \
  || die "cannot read $ROOT/adapter (not a readable link, or its directory cannot be searched): the prune would not know the rollback adapter"
if [[ -d "$ADAPTER_DIR" ]]; then
  for f in "${ADAPTER_FILES[@]}"; do cmp -s "$HERE/$f" "$ADAPTER_DIR/${f##*/}" || die "$ADAPTER_DIR differs from $SRC_SHA: investigate"; done
fi
if [[ "$CURRENT_BUILD" == "$BUILD" && ! -f "$BUILD/.cortados-build-ok" ]]; then
  die "$BUILD is current but has no build marker: investigate"
fi

# Studio's state dir and settings file: a link here is an unknown layout, so the run stops before any
# change. This is NOT what keeps root safe (the Studio user can plant a link after this check): root
# never writes there, every write is done as $STUDIO_USER (role_write, fs_* below, #77 fix round 2).
for f in "$STUDIO_STATE" "$STUDIO_STATE/claw3d" "$STUDIO_SETTINGS"; do
  if as_root_test -L "$f"; then die "$f is a symbolic link: refusing to write through it (move it aside first)"; fi
done
# An activation that was killed outright (no rollback ran) leaves the previous files as .prev.
for f in "$ROOT/bin/run-bridge.sh" "$ROOT/bin/run-studio.sh" "/Library/LaunchDaemons/$BRIDGE_LABEL.plist" \
         "/Library/LaunchDaemons/$STUDIO_LABEL.plist" "$OFFICE_TOKEN" "$STUDIO_SETTINGS"; do
  if as_root_test -e "$f.prev"; then
    die "$f.prev exists: an earlier activation was interrupted. It is the previous version: restore it (sudo mv) or delete it, then re-run"
  fi
done

# Secrets: kept when present, made when absent. The provider client is explicit, in one slot.
CLIENTS_LIST="$(clients_cli list)" || die "cannot list the provider's clients"
LIVE_SLOT=""
for s in "${CLIENT_SLOTS[@]}"; do
  line="$(printf '%s\n' "$CLIENTS_LIST" | awk -v n="$s:" '$1 == n')"
  [[ -z "$line" ]] && continue
  [[ "$line" == "$s: office:read" ]] || die "client $s is registered with other scopes ($line): remove it deliberately first"
  [[ -z "$LIVE_SLOT" ]] || die "both $LIVE_SLOT and $s are registered (an interrupted rotation): remove the one the bridge does not use"
  LIVE_SLOT="$s"
done
HAVE_OFFICE=0; HAVE_BRIDGE_SECRET=0; HAVE_ACCESS=0
if [[ $UPGRADE -eq 1 ]]; then
  as_root_test -s "$OFFICE_TOKEN" && HAVE_OFFICE=1
  as_root_test -s "$BRIDGE_SECRET" && HAVE_BRIDGE_SECRET=1
  as_root_test -s "$ACCESS_TOKEN" && HAVE_ACCESS=1
fi
NEW_SLOT=""
if [[ -n "$LIVE_SLOT" ]]; then
  if [[ $ROTATE -eq 1 ]]; then
    TOKEN_PLAN=rotate
    [[ "$LIVE_SLOT" == "${CLIENT_SLOTS[0]}" ]] && NEW_SLOT="${CLIENT_SLOTS[1]}" || NEW_SLOT="${CLIENT_SLOTS[0]}"
  elif [[ $HAVE_OFFICE -eq 1 ]]; then TOKEN_PLAN=keep
  else die "client $LIVE_SLOT is registered but the bridge has no token file: re-run with --rotate-office-token"
  fi
else
  TOKEN_PLAN=mint; NEW_SLOT="${CLIENT_SLOTS[0]}"   # also when a stale (revoked) token file is there: it is replaced
fi

[[ $UPGRADE -eq 1 ]] && kind=upgrade || kind="fresh install"
echo "  ok: checkout $SRC_SHA (clean), provider reachable on loopback; $kind"
echo "  plan: node $NODE_PLAN, build $BUILD_ID, office token $TOKEN_PLAN${NEW_SLOT:+ (as $NEW_SLOT)}, bridge secret $([[ $HAVE_BRIDGE_SECRET -eq 1 ]] && echo keep || echo make)," \
  "studio access token $([[ $HAVE_ACCESS -eq 1 ]] && echo keep || echo make), studio gateway settings rewritten from the bridge secret"
for label in "$BRIDGE_LABEL" "$STUDIO_LABEL"; do
  loaded "$label" && echo "  $label is loaded: it is restarted after the switch (and again with the old release on a rollback)" || true
done
[[ $APPLY -eq 1 ]] || echo "  DRY RUN: nothing below is executed. Re-run with --apply."

# Every file in TMP that ever holds a secret, registered here, before any of them is made: cleanup
# wipes them (wipe_rm, deploy-lib.sh) on every exit, a die, a failed step or a signal, not only on
# the success path (#77 fix round 2).
SECRET_TMP=(office-token office-header bridge-token access-token studio-settings.json studio-settings.old)
# A client registered by this run stays only once its release is committed: any other exit revokes it.
PENDING_CLIENT=""
# After the commit, the client the old release used (a rotation): no daemon uses it any more, so an
# exit before step 8 has revoked it revokes it. Never both set: tx_on_commit moves one to the other.
RETIRED_CLIENT=""
TMP=""
CLEANED=0
cleanup() {  # runs once; a signal arriving while it runs is ignored, so it is never cut short
  trap '' INT TERM HUP
  [[ $CLEANED -eq 0 ]] || return 0
  CLEANED=1
  if [[ -n "$PENDING_CLIENT" && $APPLY -eq 1 ]]; then
    clients_cli remove "$PENDING_CLIENT" >&2 \
      || echo "deploy-air: could not revoke $PENDING_CLIENT: remove it with clients remove" >&2
  fi
  if [[ -n "$RETIRED_CLIENT" && $APPLY -eq 1 ]]; then
    clients_cli remove "$RETIRED_CLIENT" >&2 \
      || echo "deploy-air: could not revoke the old $RETIRED_CLIENT (the bridge uses the new one): check clients list" >&2
  fi
  if [[ -n "$TMP" ]]; then
    wipe_rm "${SECRET_TMP[@]/#/$TMP/}" || echo "deploy-air: could not remove every secret from $TMP: remove it now" >&2
    rm -rf "$TMP"
  fi
}
on_exit() { cleanup; }
# Called by tx_run (deploy-lib.sh) at the commit point, with INT/TERM/HUP deferred, so the two moves
# are one step to a signal (#77 fix round 4). From here the new client is the live one: no handler
# may revoke it. The old slot, when rotated, is no daemon's any more: an exit before step 8 revokes it.
tx_on_commit() {
  if [[ -n "$PENDING_CLIENT" && ${TOKEN_PLAN:-} == rotate ]]; then RETIRED_CLIENT="$LIVE_SLOT"; fi
  PENDING_CLIENT=""
}
# Reason (#77 fix round 3): bash does NOT run the EXIT trap when an untrapped SIGTERM/SIGINT/SIGHUP
# kills it (reproduced with /bin/bash: exit 143, nothing wiped), so each signal has its own handler:
# the same cleanup, then exit 128+signal (which also runs EXIT; cleanup runs only once). Installed
# before TMP exists. During activation tx_run (deploy-lib.sh) replaces them with its rollback handlers
# (a trap replacement is atomic: there is no moment without a handler) and puts these back after.
on_signal() { cleanup; exit $((128 + $1)); }
trap on_exit EXIT
trap 'on_signal 2' INT
trap 'on_signal 15' TERM
trap 'on_signal 1' HUP
TMP="$(mktemp -d)"   # 0700, the owner's

# ---- 1. role users (hidden, no shell, no login) --------------------------------------
step "1. Role users $BRIDGE_USER (bridge, holds office:read) and $STUDIO_USER (Claw3D Studio)"
create_user() {  # name uid home plan
  if [[ "$4" == reuse ]]; then echo "  reuse (validated): $1"; return; fi
  run sudo dscl . -create "/Users/$1"
  run sudo dscl . -create "/Users/$1" UniqueID "$2"
  run sudo dscl . -create "/Users/$1" PrimaryGroupID "$ROLE_GID"
  run sudo dscl . -create "/Users/$1" UserShell "$ROLE_SHELL"
  run sudo dscl . -create "/Users/$1" NFSHomeDirectory "$3"
  run sudo dscl . -create "/Users/$1" RealName "$ROLE_REALNAME"
  run sudo dscl . -create "/Users/$1" IsHidden 1
  run sudo dscl . -create "/Users/$1" Password '*'
  if [[ $APPLY -eq 1 ]]; then [[ "$(check_role "$1" "$2" "$3")" == reuse ]] || die "$1 was not created as specified"; fi
}
create_user "$BRIDGE_USER" "$BRIDGE_UID" "$BRIDGE_HOME" "$BRIDGE_ACCOUNT"
create_user "$STUDIO_USER" "$STUDIO_UID" "$STUDIO_HOME" "$STUDIO_ACCOUNT"
run sudo install -d -o root -g wheel -m 0755 "$ROOT" "$ROOT/home" "$ROOT/bin" "$ROOT/adapters"
run sudo rm -rf "$STAGE"
run sudo install -d -o root -g wheel -m 0700 "$STAGE"
# As root only in root-owned $ROOT and $ROOT/home; inside a role user's own directory, as that user
# (install -d as root would chown/chmod through a link the user planted there).
run sudo install -d -o "$BRIDGE_USER" -g staff -m 0700 "$BRIDGE_HOME" "$ROOT/log-bridge"
run sudo install -d -o "$STUDIO_USER" -g staff -m 0700 "$STUDIO_HOME" "$ROOT/log-studio"
run sudo install -d -o "$STUDIO_USER" -g staff -m 0755 "$ROOT/claw3d"
run sudo -u "$BRIDGE_USER" install -d -m 0700 "$BRIDGE_HOME/secrets"
run sudo -u "$STUDIO_USER" install -d -m 0755 "$ROOT/claw3d/builds"
run sudo -u "$STUDIO_USER" install -d -m 0700 "$STUDIO_HOME/secrets" "$STUDIO_STATE" "$STUDIO_STATE/claw3d"

# ---- 2. Node (pinned tarball, hash-checked, private to /opt/cortados-office) ---------
step "2. Node $NODE_VERSION in $ROOT/node (no Homebrew, nothing on the system PATH): $NODE_PLAN"
if [[ $NODE_PLAN == install ]]; then
  run curl -fsSL --proto =https -o "$TMP/$NODE_TARBALL" "https://nodejs.org/dist/$NODE_VERSION/$NODE_TARBALL"
  if [[ $APPLY -eq 1 ]]; then
    [[ "$(shasum -a 256 "$TMP/$NODE_TARBALL" | awk '{print $1}')" == "$NODE_SHA256" ]] || die "Node tarball hash mismatch"
  fi
  run sudo rm -rf "$ROOT/node-$NODE_VERSION.partial"
  run sudo install -d -o root -g wheel -m 0755 "$ROOT/node-$NODE_VERSION.partial"
  run sudo tar -xzf "$TMP/$NODE_TARBALL" -C "$ROOT/node-$NODE_VERSION.partial" --strip-components 1
  run sudo chown -R root:wheel "$ROOT/node-$NODE_VERSION.partial"
  run sudo mv "$ROOT/node-$NODE_VERSION.partial" "$ROOT/node-$NODE_VERSION"
fi
NODE="$ROOT/node/bin/node"; NPM="$ROOT/node-$NODE_VERSION/bin/npm"

# ---- 3. stage the adapter + bridge (root-owned, read-only copy of this commit) --------
step "3. Stage the office adapter and bridge -> $ADAPTER_DIR"
if [[ -d "$ADAPTER_DIR" ]]; then
  echo "  already staged for this commit (checked identical above)"
else
  run sudo rm -rf "$ADAPTER_DIR.partial"
  run sudo install -d -o root -g wheel -m 0755 "$ADAPTER_DIR.partial"
  for f in "${ADAPTER_FILES[@]}"; do run sudo install -o root -g wheel -m 0644 "$HERE/$f" "$ADAPTER_DIR.partial/${f##*/}"; done
  run sudo mv "$ADAPTER_DIR.partial" "$ADAPTER_DIR"
fi

# ---- 4. build Claw3D beside the running one (pinned commit + the patches) -------------
# system-fonts.patch drops next/font/google: the build fetches nothing from Google Fonts, whose
# /l/font?kit=..&skey=.. answers break next/font under Turbopack (and webpack), 2026-11-24.
# proxy-auth.patch makes Studio's gateway proxy always send its configured token to the bridge and
# drop the browser's device block and credentials: over HTTPS the stock proxy took the browser's
# signed device block as its auth, sent no token, and the bridge refused (1012 in the Office), 2026-11-25.
step "4. Claw3D $CLAW3D_COMMIT -> $BUILD (build as $STUDIO_USER; switched in only after it succeeds)"
as_studio() { run sudo -u "$STUDIO_USER" -H env HOME="$STUDIO_HOME" PATH="$ROOT/node-$NODE_VERSION/bin:/usr/bin:/bin" \
  NEXT_TELEMETRY_DISABLED=1 npm_config_fund=false npm_config_audit=false "$@"; }
if [[ -f "$BUILD/.cortados-build-ok" ]]; then
  echo "  already built for this commit pair"
else
  # A build dir without the marker is a failed earlier run; never the current one (checked above).
  [[ ! -e "$BUILD" ]] || run sudo -u "$STUDIO_USER" rm -rf "$BUILD"
  as_studio git clone --no-checkout "$CLAW3D_REPO" "$BUILD"
  as_studio git -C "$BUILD" checkout --detach "$CLAW3D_COMMIT"
  for p in avatar-profile-hint.patch studio-login.patch cortados-rooms.patch system-fonts.patch proxy-auth.patch; do
    as_studio git -C "$BUILD" apply --check "$ADAPTER_DIR/$p"
    as_studio git -C "$BUILD" apply "$ADAPTER_DIR/$p"
  done
  as_studio install -m 0644 "$ADAPTER_DIR/studio-login.cjs" "$BUILD/server/cortados-studio-login.js"
  # Lockfile install. Lifecycle scripts are off; if the build then fails for a missing
  # native binary, see DEPLOY.md "If npm run build fails" (an owner decision, not automatic).
  as_studio bash -c "cd '$BUILD' && '$NPM' ci --ignore-scripts && '$NPM' run build && touch .cortados-build-ok"
fi

# ---- 5. secrets (never printed, never on argv) ----------------------------------------
step "5. Secrets: office:read token ($TOKEN_PLAN), bridge secret, Studio access token, Studio gateway settings"
# 5a. the provider's scoped client token, minted as the owner into the free slot, checked against
#     the provider and STAGED for the bridge user. The running bridge keeps its own token, still
#     registered, until the new release is committed (step 8); only then is the old slot revoked.
office_token_ok() {  # token-file: the provider answers 200 to it (the header goes file-to-file, not argv)
  local code
  (umask 077; printf 'x-cortados-token: %s\n' "$(cat "$1")" > "$TMP/office-header")
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H @"$TMP/office-header" "$PROVIDER_OFFICE_URL" || true)"
  wipe_rm "$TMP/office-header"
  [[ "$code" == 200 ]]
}
if [[ $TOKEN_PLAN != keep ]]; then
  run clients_cli add "$NEW_SLOT" --scope office:read --token-file "$TMP/office-token"
  if [[ $APPLY -eq 1 ]]; then
    PENDING_CLIENT="$NEW_SLOT"
    office_token_ok "$TMP/office-token" || die "the provider does not accept the new $NEW_SLOT token: revoked, nothing switched"
  fi
  run sudo install -o "$BRIDGE_USER" -g staff -m 0600 "$TMP/office-token" "$STAGE/office-token"
  run wipe_rm "$TMP/office-token"
fi
# 5b. a local secret shared by the bridge and Studio (NOT the office token: Studio may hand its
#     upstream token to the browser, so it only ever opens this read-only bridge). The bridge checks
#     BRIDGE_SECRET; Studio's copy is the token in its settings file (5d). An older install's
#     $GATEWAY_SECRET is left in place, unused by this release: the previous launcher, which a
#     rollback restores, still reads it at start.
new_secret() { if [[ $APPLY -eq 1 ]]; then (umask 077; openssl rand -hex 32 > "$1"); fi; }
if [[ $HAVE_BRIDGE_SECRET -eq 0 ]]; then
  new_secret "$TMP/bridge-token"
  run role_write_from "$BRIDGE_USER" "$TMP/bridge-token" "$BRIDGE_SECRET"   # its own secrets dir: as the user
  run wipe_rm "$TMP/bridge-token"
fi
# 5c. Studio's own access token: its access gate is always on, loopback included, so publishing
#     on the tailnet never exposes an open Studio. The owner reads it once (DEPLOY.md "Viewing it").
if [[ $HAVE_ACCESS -eq 0 ]]; then
  new_secret "$TMP/access-token"
  run role_write_from "$STUDIO_USER" "$TMP/access-token" "$ACCESS_TOKEN"
  run wipe_rm "$TMP/access-token"
fi
# 5d. Studio's gateway settings, the only place Claw3D's proxy takes its upstream from (see
#     STUDIO_SETTINGS above): url = GATEWAY_URL, token = the bridge secret, adapterType openclaw.
#     Rewritten on every run from BRIDGE_SECRET (idempotent; a rotated secret follows), keeping the
#     installed file's other keys (Studio's own UI state). STAGED here and installed by the
#     transaction (step 7), so a rollback puts the previous file back. The token goes file -> pipe ->
#     studio-settings.py's stdin -> a 0600 file in the owner's 0700 TMP: never argv, never printed.
#     Each file is read as its own role user, not root (no privilege for a link swapped in later).
#     Exit codes, not die, inside: the caller dies once, with the reason. Both TMP files are wiped
#     whenever stage_studio_settings returns, failed or not (and by cleanup if the run is killed or signalled).
build_studio_settings() {  # 0 built and staged; 1 failed (reason on stderr). Leaves its TMP files.
  local ex
  (umask 077; : > "$TMP/studio-settings.old") || return 1
  ex="$(root_test -e "$STUDIO_SETTINGS")" || { echo "cannot check whether $STUDIO_SETTINGS exists" >&2; return 1; }
  if [[ "$ex" == yes ]]; then
    sudo -u "$STUDIO_USER" cat "$STUDIO_SETTINGS" > "$TMP/studio-settings.old" \
      || { echo "cannot read $STUDIO_SETTINGS as $STUDIO_USER" >&2; return 1; }
  fi
  (set -o pipefail; umask 077; sudo -u "$BRIDGE_USER" cat "$BRIDGE_SECRET" \
    | python3 -I "$HERE/studio-settings.py" --url "$GATEWAY_URL" --existing "$TMP/studio-settings.old" \
    > "$TMP/studio-settings.json") || { echo "cannot build the settings from $BRIDGE_SECRET" >&2; return 1; }
  sudo install -o "$STUDIO_USER" -g staff -m 0600 "$TMP/studio-settings.json" "$STAGE/studio-settings.json" \
    || { echo "cannot stage $STAGE/studio-settings.json" >&2; return 1; }
}
stage_studio_settings() {  # 0 staged; 1 failed (reason on stderr). No token-bearing TMP file outlives it.
  local rc=0
  build_studio_settings || rc=$?
  wipe_rm "$TMP/studio-settings.json" "$TMP/studio-settings.old" || { echo "cannot remove the settings from $TMP" >&2; rc=1; }
  return $rc
}
printf '  stage %s (%s, 0600: gateway %s + the bridge secret) -> %s\n' "$STAGE/studio-settings.json" "$STUDIO_USER" "$GATEWAY_URL" "$STUDIO_SETTINGS"
if [[ $APPLY -eq 1 ]]; then stage_studio_settings || die "cannot stage Studio's gateway settings (see above): nothing switched"; fi

# ---- 6. launchers + LaunchDaemons, staged (they only name stable paths) -----------------
step "6. Stage launchers and LaunchDaemons (system domain, UserName = role user, RunAtLoad, KeepAlive)"
write_root_file() {  # staged-name mode <<content: written to a private temp file, then installed into $STAGE
  local tmp; tmp="$TMP/$1"
  cat > "$tmp"
  printf '  stage %s (%s)\n' "$STAGE/$1" "$2"
  if [[ $APPLY -eq 1 ]]; then sudo install -o root -g wheel -m "$2" "$tmp" "$STAGE/$1"; fi
}
write_root_file run-bridge.sh 0755 <<EOF
#!/bin/bash
# Runs as $BRIDGE_USER. Token paths only; the bridge reads the files itself.
export CORTADOS_OFFICE_URL="$PROVIDER_OFFICE_URL"
export CORTADOS_OFFICE_TOKEN_FILE="$OFFICE_TOKEN"
export CORTADOS_OFFICE_BRIDGE_TOKEN_FILE="$BRIDGE_SECRET"
export CORTADOS_OFFICE_BRIDGE_HOST=127.0.0.1
export CORTADOS_OFFICE_BRIDGE_PORT=$BRIDGE_PORT
export CORTADOS_OFFICE_POLL_SECONDS=5
exec "$NODE" "$ROOT/adapter/bridge.mjs"
EOF
write_root_file run-studio.sh 0755 <<EOF
#!/bin/bash
# Runs as $STUDIO_USER. Loopback only. STUDIO_ACCESS_TOKEN is always set: every browser, on the
# Air or through tailscale serve, logs in at /cortados-login (claw3d/studio-login.cjs).
set -euo pipefail
export HOST=127.0.0.1 PORT=$STUDIO_PORT NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
# Claw3D's state dir: its gateway proxy takes the upstream URL and token ONLY from
# \$OPENCLAW_STATE_DIR/claw3d/settings.json (deploy-air.sh writes it: $GATEWAY_URL + the bridge
# secret). No CLAW3D_GATEWAY_URL/_TOKEN here: Claw3D 51337f7's proxy ignores them (#77).
export OPENCLAW_STATE_DIR="$STUDIO_STATE"
# Claw3D 51337f7 (server/gateway-proxy.js isUpstreamAllowed) refuses EVERY upstream when NODE_ENV is
# production and this is empty ("studio.gateway_url_blocked": the Office stays "CONNECTING", #77).
# The settings URL's hostname, nothing else. Claw3D compares hostnames only, so this allows any port
# on localhost; the settings file pins the bridge's port.
export UPSTREAM_ALLOWLIST="$GATEWAY_HOST"
STUDIO_ACCESS_TOKEN="\$(cat "$ACCESS_TOKEN")"
export STUDIO_ACCESS_TOKEN
cd "$ROOT/claw3d/current"
exec "$NODE" server/index.js
EOF
plist() {  # label user script logdir
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>UserName</key><string>$2</string>
  <key>ProgramArguments</key><array><string>$3</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$4/out.log</string>
  <key>StandardErrorPath</key><string>$4/err.log</string>
</dict></plist>
EOF
}
plist "$BRIDGE_LABEL" "$BRIDGE_USER" "$ROOT/bin/run-bridge.sh" "$ROOT/log-bridge" \
  | write_root_file "$BRIDGE_LABEL.plist" 0644
plist "$STUDIO_LABEL" "$STUDIO_USER" "$ROOT/bin/run-studio.sh" "$ROOT/log-studio" \
  | write_root_file "$STUDIO_LABEL.plist" 0644

# ---- 7. activate, as one transaction: everything staged above succeeded ---------------
step "7. Activate: switch the links, install the staged files, restart, health check (rollback on any failure)"
switch_link() {  # target link [user]: replace `link` atomically (rename over it)
  local who=(sudo); [[ -n "${3:-}" ]] && who=(sudo -u "$3")
  run "${who[@]}" ln -sfn "$1" "$2.new" && run "${who[@]}" mv -fh "$2.new" "$2"
}
# The primitives tx_run (deploy-lib.sh) drives. Each returns a code, never exits.
fs_readlink() { link_target "$1"; }  # the target; nothing only when confirmed absent; 1 when unreadable
fs_exists() {  # 0 exists, 1 absent, 2 unknown (sudo or test failed): never exits inside the transaction
  case "$(root_test -e "$1" -o -L "$1")" in yes) return 0 ;; no) return 1 ;; *) return 2 ;; esac
}
fs_link() { switch_link "$1" "$2" "$3"; }
# USER set (a role user's directory): every write AS that user, never root (role_write, above). Empty:
# root, only for root-owned directories ($ROOT/bin, /Library/LaunchDaemons).
fs_rm() { if [[ -n "${2:-}" ]]; then run sudo -u "$2" rm -f "$1"; else run sudo rm -f "$1"; fi; }
fs_backup() {  # dest user: a .prev left there (planted after the preflight) is replaced, never written through
  if [[ -n "${2:-}" ]]; then run sudo -u "$2" /bin/sh -c 'rm -f "$1.prev" && cp -p "$1" "$1.prev"' fs-backup "$1"
  else run sudo cp -p "$1" "$1.prev"; fi
}
fs_promote() {  # staged dest user
  if [[ -n "${3:-}" ]]; then run promote_as "$3" "$1" "$2"
  else run sudo cp -p "$1" "$2.next" && run sudo mv -f "$2.next" "$2"; fi
}
fs_restore() { if [[ -n "${2:-}" ]]; then run sudo -u "$2" mv -f "$1.prev" "$1"; else run sudo mv -f "$1.prev" "$1"; fi; }
daemon_loaded() { launchd_state "$1"; }   # 0 / 1 / 2 unknown: returns, never exits (tx_*)
daemon_start() { run sudo launchctl bootstrap system "/Library/LaunchDaemons/$1.plist"; }
daemon_stop() {
  run sudo launchctl bootout "system/$1" || true      # a daemon that exits meanwhile is not an error
  [[ $APPLY -eq 1 ]] || return 0
  local st
  for _ in $(seq 1 20); do
    st=0; launchd_state "$1" || st=$?
    case $st in
      1) return 0 ;;
      0) sleep 0.5 ;;
      *) echo "deploy-air: cannot tell whether $1 stopped" >&2; return 1 ;;
    esac
  done
  echo "deploy-air: $1 did not stop" >&2; return 1
}
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || true; }
office_healthy() {  # prints why not, on failure
  local l
  for l in "$BRIDGE_LABEL" "$STUDIO_LABEL"; do
    # read as root and captured whole, then matched (launchd_running, deploy-lib.sh): the owner cannot
    # see the system domain, and piping into an early-exiting grep could SIGPIPE launchctl
    launchd_running "$l" || { echo "$l is not running"; return 1; }
  done
  [[ "$(http_code "http://127.0.0.1:$BRIDGE_PORT/")" == 426 ]] || { echo "the bridge does not answer on 127.0.0.1:$BRIDGE_PORT"; return 1; }
  [[ "$(http_code "http://127.0.0.1:$STUDIO_PORT/")" == 401 ]] || { echo "Studio does not refuse a request without its access cookie"; return 1; }
  [[ "$(http_code "http://127.0.0.1:$STUDIO_PORT/cortados-login")" == 200 ]] || { echo "the login form does not answer"; return 1; }
  # End to end, as the browser does: through Studio's own proxy (/api/gateway/ws, behind its access
  # gate) an OpenClaw `connect`, which the proxy forwards with its gateway token; only the bridge's
  # ok:true hello passes (gateway-check.mjs). It also sends a connect shaped like the browser's over
  # HTTPS (a signed device block, no token), which only a proxy with proxy-auth.patch gets through. Reason: on 2026-11-24 every check above passed while
  # the proxy refused its upstream (studio.gateway_url_blocked) and the Office showed 0 agents.
  # The access token goes file -> pipe -> stdin: never printed, never on a command line. Read as
  # $STUDIO_USER, not root: the path is in Studio's home and the probe sends what it reads to Studio's
  # port, so as root a link planted there would hand Studio any root-readable file (#77 fix round 2).
  # Fails closed: a refused sudo (pipefail), a refusal, a close or the 5 s deadline all fail.
  local gw
  gw="$(sudo -u "$STUDIO_USER" cat "$ACCESS_TOKEN" 2>/dev/null | "$NODE" "$HERE/gateway-check.mjs" --port "$STUDIO_PORT" --timeout-ms 5000 2>/dev/null)" \
    && [[ "$gw" == ok ]] || { echo "Studio's gateway proxy does not reach the bridge (${gw:-no answer})"; return 1; }
  # 0 only on a successful lsof listing loopback listeners on both ports (deploy-lib.sh): a refused
  # sudo or an unreadable answer fails the health check, so the transaction rolls back
  listeners_loopback_only "$BRIDGE_PORT" "$STUDIO_PORT" || return 1
}
# Bounded by an overall deadline, not a try count (reason: 30 tries of a check that can take ~21 s
# each, the probe's 5 s and three 5 s curls, could run ~210 s with no output). No new attempt starts
# after HEALTH_DEADLINE_S; one already running finishes on its own timeouts. A pass is confirmed by a
# second full check 10 s later. Worst case: ~60 + 21 + 10 + 21 s, about 2 minutes.
health_check() {
  [[ $APPLY -eq 1 ]] || { echo "  (dry run) health check: both daemons running, bridge 426, Studio 401 on /, 200 on /cortados-login, a connect through Studio's proxy answered by the bridge, loopback only"; return 0; }
  local why="not checked" deadline=$((SECONDS + HEALTH_DEADLINE_S))
  while (( SECONDS < deadline )); do
    if why="$(office_healthy)"; then
      sleep 10
      why="$(office_healthy)" && { echo "  healthy: both daemons running on loopback, Studio gated (401), login form 200, gateway connect ok"; return 0; }
    fi
    sleep 2
  done
  echo "deploy-air: health check (deadline ${HEALTH_DEADLINE_S} s): $why" >&2; return 1
}
TX_LINKS=("$ROOT/node-$NODE_VERSION|$ROOT/node|" "$ADAPTER_DIR|$ROOT/adapter|" "$BUILD|$ROOT/claw3d/current|$STUDIO_USER")
TX_FILES=("$STAGE/run-bridge.sh|$ROOT/bin/run-bridge.sh" "$STAGE/run-studio.sh|$ROOT/bin/run-studio.sh"
  "$STAGE/$BRIDGE_LABEL.plist|/Library/LaunchDaemons/$BRIDGE_LABEL.plist"
  "$STAGE/$STUDIO_LABEL.plist|/Library/LaunchDaemons/$STUDIO_LABEL.plist")
# The third field: the role user owning the destination's directory, who does every write there
# (fs_*, #77 fix round 2). The launchers and plists live in root-owned directories: root.
[[ $TOKEN_PLAN == keep ]] || TX_FILES+=("$STAGE/office-token|$OFFICE_TOKEN|$BRIDGE_USER")
TX_FILES+=("$STAGE/studio-settings.json|$STUDIO_SETTINGS|$STUDIO_USER")   # always: rewritten every run (5d)
TX_LABELS=("$BRIDGE_LABEL" "$STUDIO_LABEL")
tx_rc=0; tx_run || tx_rc=$?
case $tx_rc in
  0) ;;
  1) die "activation failed and was rolled back: the previous release runs again${PENDING_CLIENT:+ ($PENDING_CLIENT is revoked)}" ;;
  *) die "activation failed AND the rollback did not complete (see above): stop both daemons and investigate" ;;
esac

# ---- 8. commit: the new release is healthy, so the old credential goes --------------------
step "8. Commit"
if [[ $TOKEN_PLAN == rotate ]]; then
  run clients_cli remove "$LIVE_SLOT" \
    || echo "deploy-air: could not revoke $LIVE_SLOT: remove it with clients remove (the bridge uses $NEW_SLOT)" >&2
fi
RETIRED_CLIENT=""   # PENDING_CLIENT was cleared at the commit point (tx_on_commit)
run sudo rm -rf "$STAGE"
if [[ $APPLY -eq 1 ]]; then tail -n 5 "$ROOT/log-bridge/err.log" 2>/dev/null | sed 's/^/  bridge: /' || true; fi

# ---- 9. prune: keep the new and the previous Claw3D build / adapter copy (one rollback) --
step "9. Prune older builds (kept: current and previous)"
for b in "$ROOT/claw3d/builds"/*; do
  [[ -e "$b" && "$b" != "$BUILD" && "$b" != "$CURRENT_BUILD" ]] && run sudo -u "$STUDIO_USER" rm -rf "$b"
done
for a in "$ROOT/adapters"/*; do
  [[ -e "$a" && "$a" != "$ADAPTER_DIR" && "$a" != "$PREVIOUS_ADAPTER" ]] && run sudo rm -rf "$a"
done
true

echo
echo "Open it on the Air: http://127.0.0.1:$STUDIO_PORT/cortados-login (the access token: DEPLOY.md \"Viewing it\")."
echo "Other devices: $0 --publish-tailnet (a separate step)."
