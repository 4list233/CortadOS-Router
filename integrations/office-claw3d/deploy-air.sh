#!/bin/bash
# Install the CortadOS Office on the Air: Claw3D Studio + the read-only office bridge,
# each as its own hidden macOS role user, each under a LaunchDaemon, both loopback-only.
# Runbook and rationale: integrations/office-claw3d/DEPLOY.md.
#
# NOT RUN BY ITS AUTHOR. Written for issue #77; the owner runs it on the Air when ready.
# Default is a dry run that only prints the plan. `--apply` performs it (needs sudo).
# It never prints a token: secrets go file-to-file with umask 077, never via argv or echo.
set -euo pipefail

# ---- pins (change deliberately, re-verify the hashes) --------------------------------
NODE_VERSION="v24.22.3"                    # Node LTS "Krypton", 2026-11-10
NODE_TARBALL="node-${NODE_VERSION}-darwin-arm64.tar.gz"
NODE_SHA256="ff6e17d4874b76d272212b908cc020d6e815fba5a6c7eaaa06add725840307fd"  # nodejs.org SHASUMS256.txt
CLAW3D_REPO="https://github.com/iamlukethedev/Claw3D.git"
CLAW3D_COMMIT="51337f7f250459af61a7cf92b7b0015d59faed5c"   # the commit the patch and PROTOCOL.md were checked on
EXPECTED_HOST="${CORTADOS_OFFICE_EXPECTED_HOST:-Forests-MacBook-Air}"

ROOT="/opt/cortados-office"
BRIDGE_USER="_cortados-office"; BRIDGE_UID=480
STUDIO_USER="_cortados-claw3d"; STUDIO_UID=481
BRIDGE_PORT=18789                          # Claw3D's default upstream gateway port
STUDIO_PORT=3000
PROVIDER_OFFICE_URL="http://127.0.0.1:8432/v1/cortados/office"
CLIENT_NAME="claw3d-office"                # the provider's scoped client (office:read only)

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
[[ $# -gt 1 || ( $# -eq 1 && $APPLY -eq 0 ) ]] && { echo "usage: $0 [--apply]" >&2; exit 2; }

step() { printf '\n== %s\n' "$*"; }
run() {  # print the command; run it only with --apply
  printf '  $ %s\n' "$*"
  if [[ $APPLY -eq 1 ]]; then "$@"; fi
}
die() { echo "deploy-air: $*" >&2; exit 1; }

# ---- preconditions (checked in both modes; they only read) ---------------------------
step "Preconditions"
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "macOS on Apple silicon only"
[[ "$(scutil --get LocalHostName 2>/dev/null || hostname -s)" == "$EXPECTED_HOST" ]] \
  || die "this is not $EXPECTED_HOST (set CORTADOS_OFFICE_EXPECTED_HOST to override deliberately)"
[[ $EUID -ne 0 ]] || die "run as the owner (the provider's user), not root; sudo is used per step"
[[ -f "$HERE/claw3d/avatar-profile-hint.patch" && -f "$HERE/bridge.mjs" ]] || die "run from a CortadOS-Router checkout"
xcode-select -p >/dev/null 2>&1 || die "git needs the Xcode command line tools (xcode-select --install)"
for u in "$BRIDGE_UID" "$STUDIO_UID"; do
  owner="$(dscl . -search /Users UniqueID "$u" 2>/dev/null | awk 'NR==1{print $1}')"
  [[ -z "$owner" || "$owner" == "$BRIDGE_USER" || "$owner" == "$STUDIO_USER" ]] || die "UID $u is taken by $owner"
done
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$PROVIDER_OFFICE_URL" || true)"
[[ "$code" == 401 || "$code" == 403 ]] || die "provider office endpoint not answering 401 without a token (got $code)"
echo "  ok: host, arch, checkout, UIDs, provider reachable on loopback"
[[ $APPLY -eq 1 ]] || echo "  DRY RUN: nothing below is executed. Re-run with --apply."

# ---- 1. role users (hidden, no shell, no login) --------------------------------------
step "1. Role users $BRIDGE_USER (bridge, holds office:read) and $STUDIO_USER (Claw3D Studio)"
create_user() {  # name uid home
  if dscl . -read "/Users/$1" >/dev/null 2>&1; then echo "  exists: $1"; return; fi
  run sudo dscl . -create "/Users/$1"
  run sudo dscl . -create "/Users/$1" UniqueID "$2"
  run sudo dscl . -create "/Users/$1" PrimaryGroupID 20
  run sudo dscl . -create "/Users/$1" UserShell /usr/bin/false
  run sudo dscl . -create "/Users/$1" NFSHomeDirectory "$3"
  run sudo dscl . -create "/Users/$1" RealName "CortadOS Office role user"
  run sudo dscl . -create "/Users/$1" IsHidden 1
  run sudo dscl . -create "/Users/$1" Password '*'
}
create_user "$BRIDGE_USER" "$BRIDGE_UID" "$ROOT/home/bridge"
create_user "$STUDIO_USER" "$STUDIO_UID" "$ROOT/home/studio"
run sudo install -d -o root -g wheel -m 0755 "$ROOT" "$ROOT/home" "$ROOT/bin" "$ROOT/adapter"
run sudo install -d -o "$BRIDGE_USER" -g staff -m 0700 "$ROOT/home/bridge" "$ROOT/home/bridge/secrets" "$ROOT/log-bridge"
run sudo install -d -o "$STUDIO_USER" -g staff -m 0700 "$ROOT/home/studio" "$ROOT/home/studio/secrets" "$ROOT/log-studio"
run sudo install -d -o "$STUDIO_USER" -g staff -m 0755 "$ROOT/claw3d"

# ---- 2. Node (pinned tarball, hash-checked, private to /opt/cortados-office) ---------
step "2. Node $NODE_VERSION in $ROOT/node (no Homebrew, nothing on the system PATH)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
run curl -fsSL --proto =https -o "$TMP/$NODE_TARBALL" "https://nodejs.org/dist/$NODE_VERSION/$NODE_TARBALL"
if [[ $APPLY -eq 1 ]]; then
  [[ "$(shasum -a 256 "$TMP/$NODE_TARBALL" | awk '{print $1}')" == "$NODE_SHA256" ]] || die "Node tarball hash mismatch"
fi
run sudo install -d -o root -g wheel -m 0755 "$ROOT/node-$NODE_VERSION"
run sudo tar -xzf "$TMP/$NODE_TARBALL" -C "$ROOT/node-$NODE_VERSION" --strip-components 1
run sudo chown -R root:wheel "$ROOT/node-$NODE_VERSION"
run sudo ln -sfn "$ROOT/node-$NODE_VERSION" "$ROOT/node"
NODE="$ROOT/node/bin/node"; NPM="$ROOT/node/bin/npm"

# ---- 3. the adapter + bridge (root-owned, read-only copy of this checkout) -----------
step "3. Office adapter and bridge -> $ROOT/adapter (pinned to this checkout's HEAD)"
echo "  source commit: $(git -C "$REPO" rev-parse HEAD)"
for f in adapter.mjs bridge.mjs sprites.mjs; do run sudo install -o root -g wheel -m 0644 "$HERE/$f" "$ROOT/adapter/$f"; done

# ---- 4. Claw3D (pinned commit + the avatar-profile patch; npm confined to $ROOT/claw3d)
step "4. Claw3D $CLAW3D_COMMIT -> $ROOT/claw3d (build as $STUDIO_USER)"
as_studio() { run sudo -u "$STUDIO_USER" -H env HOME="$ROOT/home/studio" PATH="$ROOT/node/bin:/usr/bin:/bin" \
  NEXT_TELEMETRY_DISABLED=1 npm_config_fund=false npm_config_audit=false "$@"; }
as_studio git clone --no-checkout "$CLAW3D_REPO" "$ROOT/claw3d/src"
as_studio git -C "$ROOT/claw3d/src" checkout --detach "$CLAW3D_COMMIT"
run sudo install -o root -g wheel -m 0644 "$HERE/claw3d/avatar-profile-hint.patch" "$ROOT/adapter/avatar-profile-hint.patch"
as_studio git -C "$ROOT/claw3d/src" apply --check "$ROOT/adapter/avatar-profile-hint.patch"
as_studio git -C "$ROOT/claw3d/src" apply "$ROOT/adapter/avatar-profile-hint.patch"
# Lockfile install. Lifecycle scripts are off; if the build then fails for a missing
# native binary, see DEPLOY.md "If npm run build fails" (an owner decision, not automatic).
as_studio bash -c "cd '$ROOT/claw3d/src' && '$NPM' ci --ignore-scripts && '$NPM' run build"

# ---- 5. secrets (never printed, never on argv) ----------------------------------------
step "5. Secrets: the office:read token (bridge only) and the bridge secret (bridge + Studio)"
# 5a. mint the provider's scoped client token as the owner, then hand it to the bridge user
run env PYTHONPATH="$REPO" python3 -B -m cortados_router.provider.clients add "$CLIENT_NAME" --scope office:read --token-file "$TMP/office-token"
run sudo install -o "$BRIDGE_USER" -g staff -m 0600 "$TMP/office-token" "$ROOT/home/bridge/secrets/office-token"
run rm -P "$TMP/office-token"
# 5b. a fresh local secret shared by the bridge and Studio (NOT the office token: Studio may
#     hand its upstream token to the browser, so it only ever opens this read-only bridge)
if [[ $APPLY -eq 1 ]]; then
  (umask 077; openssl rand -hex 32 > "$TMP/bridge-token")
fi
run sudo install -o "$BRIDGE_USER" -g staff -m 0600 "$TMP/bridge-token" "$ROOT/home/bridge/secrets/bridge-token"
run sudo install -o "$STUDIO_USER" -g staff -m 0600 "$TMP/bridge-token" "$ROOT/home/studio/secrets/gateway-token"
run rm -P "$TMP/bridge-token"

# ---- 6. launchers + LaunchDaemons -------------------------------------------------------
step "6. Launchers and LaunchDaemons (system domain, UserName = role user, RunAtLoad, KeepAlive)"
write_root_file() {  # dest mode <<content
  local content; content="$(cat)"
  printf '  write %s (%s)\n' "$1" "$2"
  if [[ $APPLY -eq 1 ]]; then printf '%s\n' "$content" | sudo tee "$1" >/dev/null; sudo chown root:wheel "$1"; sudo chmod "$2" "$1"; fi
}
write_root_file "$ROOT/bin/run-bridge.sh" 0755 <<EOF
#!/bin/bash
# Runs as $BRIDGE_USER. Token paths only; the bridge reads the files itself.
export CORTADOS_OFFICE_URL="$PROVIDER_OFFICE_URL"
export CORTADOS_OFFICE_TOKEN_FILE="$ROOT/home/bridge/secrets/office-token"
export CORTADOS_OFFICE_BRIDGE_TOKEN_FILE="$ROOT/home/bridge/secrets/bridge-token"
export CORTADOS_OFFICE_BRIDGE_HOST=127.0.0.1
export CORTADOS_OFFICE_BRIDGE_PORT=$BRIDGE_PORT
export CORTADOS_OFFICE_POLL_SECONDS=5
exec "$NODE" "$ROOT/adapter/bridge.mjs"
EOF
write_root_file "$ROOT/bin/run-studio.sh" 0755 <<EOF
#!/bin/bash
# Runs as $STUDIO_USER. Loopback only; no STUDIO_ACCESS_TOKEN because it is never exposed.
set -euo pipefail
export HOST=127.0.0.1 PORT=$STUDIO_PORT NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
export CLAW3D_GATEWAY_URL="ws://localhost:$BRIDGE_PORT"
export CLAW3D_GATEWAY_ADAPTER_TYPE=openclaw
CLAW3D_GATEWAY_TOKEN="\$(cat "$ROOT/home/studio/secrets/gateway-token")"
export CLAW3D_GATEWAY_TOKEN
cd "$ROOT/claw3d/src"
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
plist com.cortados.office-bridge "$BRIDGE_USER" "$ROOT/bin/run-bridge.sh" "$ROOT/log-bridge" \
  | write_root_file /Library/LaunchDaemons/com.cortados.office-bridge.plist 0644
plist com.cortados.office-studio "$STUDIO_USER" "$ROOT/bin/run-studio.sh" "$ROOT/log-studio" \
  | write_root_file /Library/LaunchDaemons/com.cortados.office-studio.plist 0644
run sudo launchctl bootstrap system /Library/LaunchDaemons/com.cortados.office-bridge.plist
run sudo launchctl bootstrap system /Library/LaunchDaemons/com.cortados.office-studio.plist

# ---- 7. checks --------------------------------------------------------------------------
step "7. Checks"
if [[ $APPLY -eq 1 ]]; then
  sleep 15
  sudo lsof -nP -iTCP:$BRIDGE_PORT -sTCP:LISTEN | awk 'NR>1{print "  bridge listens on " $9}'
  sudo lsof -nP -iTCP:$STUDIO_PORT -sTCP:LISTEN | awk 'NR>1{print "  studio listens on " $9}'
  if sudo lsof -nP -iTCP:$BRIDGE_PORT -iTCP:$STUDIO_PORT -sTCP:LISTEN | awk 'NR>1{print $9}' | grep -vqE '^(127\.0\.0\.1|\[::1\]):'; then
    die "a listener is not loopback-only: stop both daemons and investigate"
  fi
  curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$STUDIO_PORT/" && echo "  Studio answers on 127.0.0.1:$STUDIO_PORT"
  tail -n 5 "$ROOT/log-bridge/err.log" 2>/dev/null | sed 's/^/  bridge: /' || true
fi
echo
echo "Open it from the Air: http://127.0.0.1:$STUDIO_PORT  (from another machine: ssh -L $STUDIO_PORT:127.0.0.1:$STUDIO_PORT air)"
