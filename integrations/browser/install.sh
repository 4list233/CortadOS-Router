#!/bin/sh
# Install the pinned @playwright/mcp into a CortadOS-owned prefix and write a hardened per-task MCP config.
# Design: docs/BROWSER-TOOL.md. Nothing global: no npm -g, browsers under the prefix, never ~/Library/Caches/ms-playwright.
#   install.sh install [--browsers]            npm ci the committed lockfile into $PREFIX, check versions and tool list
#                                              (with --browsers, also download Playwright's chromium headless shell)
#   install.sh config <task_dir> <proxy_url>   write <task_dir>/browser/config.json and <task_dir>/browser-mcp.json
set -eu
PIN_MCP=0.0.84        # >= 0.0.40 (CVE-2025-9611 DNS rebinding), >= 0.0.55 (file:// blocked); tools-0.0.84.txt pins its tools
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${CORTADOS_HOME:?set CORTADOS_HOME (tests: CORTADOS_HOME=\$(mktemp -d))}"
PREFIX="$CORTADOS_HOME/browser/mcp-$PIN_MCP"
BIN="$PREFIX/node_modules/.bin/playwright-mcp"
INSTALL_CMD="CORTADOS_HOME=$CORTADOS_HOME $HERE/install.sh install --browsers"

# tools/list of the installed server, one name per line, sorted. Same flags and scrubbed env as run-mcp.sh, because
# the advertised tools depend on them (--no-webmcp, --caps).
list_tools() {
  t="$(mktemp -d)"
  { printf '%s\n' \
      '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cortados-install","version":"0"}}}' \
      '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
      '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 3; } |
    env -i PATH="$(dirname "$(command -v node)"):/usr/bin:/bin" HOME="$t" TMPDIR="$t" \
      "$BIN" --headless --isolated --sandbox --no-webmcp --browser chromium 2>/dev/null |
    python3 -I -c 'import sys, json
for line in sys.stdin:
    m = json.loads(line)
    if m.get("id") == 2:
        print("\n".join(sorted(t["name"] for t in m["result"]["tools"])))'
  rm -rf "$t"
}

case "${1:-}" in
install)
  mkdir -p "$PREFIX"
  cp "$HERE/package.json" "$HERE/package-lock.json" "$PREFIX/"
  # npm ci: exactly the committed lockfile (versions + integrity hashes), fails if package.json and lock disagree.
  # --ignore-scripts: no install-time scripts run; browsers are fetched explicitly below.
  npm ci --prefix "$PREFIX" --ignore-scripts --no-fund --no-audit
  # Version gates. @playwright/mcp must be exactly the pin (the tool list and flags below were reviewed for it);
  # playwright-core must be >= 1.55.1 (CVE-2025-59288, curl -k in install scripts).
  node -e 'const p=process.argv[1], pin=process.argv[2];
    const mcp=require(p+"/node_modules/@playwright/mcp/package.json").version;
    if (mcp!==pin) {console.error("@playwright/mcp "+mcp+" != pin "+pin);process.exit(1)}
    const v=require(p+"/node_modules/playwright-core/package.json").version.split(/[.-]/).slice(0,3).map(x=>parseInt(x,10));
    if (v[0]<1||(v[0]==1&&(v[1]<55||(v[1]==55&&v[2]<1)))) {console.error("playwright-core "+v.join(".")+" < 1.55.1");process.exit(1)}
    console.log("@playwright/mcp "+mcp+", playwright-core "+v.join("."))' "$PREFIX" "$PIN_MCP"
  # Tool list gate: the reviewed list is the source of truth for the stdio filter's allowlist. Any added, renamed or
  # removed tool fails the install so a bump is reviewed, not silently trusted.
  got="$(list_tools)"
  if [ "$got" != "$(cat "$HERE/tools-$PIN_MCP.txt")" ]; then
    echo "tools/list differs from $HERE/tools-$PIN_MCP.txt:" >&2
    printf '%s\n' "$got" | diff "$HERE/tools-$PIN_MCP.txt" - >&2 || true
    exit 1
  fi
  echo "tools/list matches tools-$PIN_MCP.txt"
  if [ "${2:-}" = "--browsers" ]; then
    PLAYWRIGHT_BROWSERS_PATH="$PREFIX/browsers" "$PREFIX/node_modules/.bin/playwright" install --only-shell chromium
  else
    echo "browsers not downloaded; run: $INSTALL_CMD"
  fi ;;
config)
  TASK="${2:?usage: install.sh config <task_dir> <proxy_url>}"
  # No proxy, no config: a browser without the egress proxy has no network boundary.
  PROXY="${3:-}"
  [ -n "$PROXY" ] || { echo "config: proxy_url is required (the per-task egress proxy is the network boundary)" >&2; exit 2; }
  [ -x "$BIN" ] || { echo "config: $BIN missing; run: $INSTALL_CMD" >&2; exit 2; }
  mkdir -p "$TASK/browser/out" "$TASK/browser/home" "$TASK/browser/tmp"
  python3 -I - "$HERE/run-mcp.sh" "$PREFIX" "$TASK" "$PROXY" <<'PY'
import json, sys
wrapper, prefix, task, proxy = sys.argv[1:5]
# Playwright config file: only what has no safe CLI flag. WebRTC and QUIC (UDP) do not go through the HTTP proxy,
# so they are turned off in Chromium itself; the sandbox is requested explicitly as well as by --sandbox.
pw = {"browser": {"browserName": "chromium", "isolated": True, "launchOptions": {
          "headless": True, "chromiumSandbox": True,
          "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]}}}
with open(f"{task}/browser/config.json", "w") as f:
    f.write(json.dumps(pw, indent=2) + "\n")
# The MCP server is always started through run-mcp.sh (scrubbed env, fixed flags); no "env" block here, because
# whatever Claude Code passes would be dropped by env -i anyway.
cfg = {"mcpServers": {"browser": {"command": wrapper, "args": [task, prefix, proxy]}}}
with open(f"{task}/browser-mcp.json", "w") as f:
    f.write(json.dumps(cfg, indent=2) + "\n")
print(f"{task}/browser-mcp.json")
PY
  echo "child flags: --mcp-config $TASK/browser-mcp.json --disallowedTools mcp__browser__browser_evaluate,mcp__browser__browser_run_code_unsafe,mcp__browser__browser_file_upload,mcp__browser__browser_drop"
  echo "(second layer, not a substitute: the stdio allowlist filter of ticket B1b)" ;;
*) echo "usage: $0 install [--browsers] | config <task_dir> <proxy_url>" >&2; exit 2 ;;
esac
