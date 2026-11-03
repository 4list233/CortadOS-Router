#!/bin/sh
# Install the pinned @playwright/mcp into a CortadOS-owned prefix and write a hardened per-task MCP config.
# Design: docs/BROWSER-TOOL.md. Nothing global: no npm -g, browsers under the prefix, never ~/Library/Caches/ms-playwright.
#   install.sh install [--browsers]            npm ci the committed lockfile into $PREFIX, check versions and tool list
#                                              (with --browsers, also download Playwright's chromium headless shell)
#   install.sh config <task_dir> <proxy_url>   write <task_dir>/browser/config.json and <task_dir>/browser-mcp.json (0600)
#   install.sh tools                           print the installed server's tools/list (same launch as a task)
set -eu
PIN_MCP=0.0.84        # >= 0.0.40 (CVE-2025-9611 DNS rebinding), >= 0.0.55 (file:// blocked); tools-0.0.84.txt pins its tools
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${CORTADOS_HOME:?set CORTADOS_HOME (tests: CORTADOS_HOME=\$(mktemp -d))}"
PREFIX="$CORTADOS_HOME/browser/mcp-$PIN_MCP"
BIN="$PREFIX/node_modules/.bin/playwright-mcp"
INSTALL_CMD="CORTADOS_HOME=$CORTADOS_HOME $HERE/install.sh install --browsers"

# Proxy URL shape: the per-task egress proxy is always ours, on loopback, with a token. Anything else (another host,
# https, no token, a path, a bypass list) is refused rather than passed to the browser.
check_proxy() {
  printf '%s' "$1" | grep -Eq '^http://[A-Za-z0-9_-]{16,}@127\.0\.0\.1:[0-9]{1,5}$' ||
    { echo "config: proxy_url must be http://<token>@127.0.0.1:<port> (token >= 16 of [A-Za-z0-9_-])" >&2; exit 2; }
}

# write_config <task_dir> <proxy_url>: <task>/browser/config.json (0600, carries the proxy token) and
# <task>/browser-mcp.json (0600). The token is in the config file, not argv, so ps does not show it.
write_config() {
  mkdir -p "$1/browser/out" "$1/browser/home" "$1/browser/tmp"
  ( umask 077; python3 -I - "$HERE/run-mcp.sh" "$PREFIX" "$1" "$2" <<'PY'
import json, os, sys
from urllib.parse import urlsplit
wrapper, prefix, task, proxy = sys.argv[1:5]
u = urlsplit(proxy)
px = {"server": f"http://{u.hostname}:{u.port}", "username": u.username, "password": ""}
# Playwright config file: only what has no safe CLI flag. The proxy is set for launch and for the context (--isolated
# creates a fresh context); never a bypass list. WebRTC and QUIC (UDP) do not go through the HTTP proxy, so they are
# turned off in Chromium itself; the sandbox is requested explicitly as well as by --sandbox.
pw = {"browser": {"browserName": "chromium", "isolated": True,
          "launchOptions": {"headless": True, "chromiumSandbox": True, "proxy": px,
              "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]},
          "contextOptions": {"proxy": px}}}
def write(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj, indent=2) + "\n")
    os.chmod(path, 0o600)
write(f"{task}/browser/config.json", pw)
# The MCP server is always started through run-mcp.sh (scrubbed env, fixed flags); no "env" block here, because
# whatever Claude Code passes would be dropped by env -i anyway.
write(f"{task}/browser-mcp.json", {"mcpServers": {"browser": {"command": wrapper, "args": [task, prefix]}}})
print(f"{task}/browser-mcp.json")
PY
  )
}

# tools/list of the installed server, one name per line, sorted. Started through run-mcp.sh on a throwaway task with
# a dummy proxy (no page is opened), so the flags and scrubbed env are exactly a task's: the advertised tools depend
# on them (--no-webmcp, --caps). Reads until the id:2 answer, 30 s cap (a slow host is not a mismatch); on failure
# the server's stderr is shown so the cause is visible.
list_tools() {
  t="$(mktemp -d)"
  trap 'rm -rf "$t"' EXIT INT TERM
  write_config "$t" "http://listtoolsdummytoken0@127.0.0.1:9" >/dev/null
  rc=0
  python3 -I - "$HERE/run-mcp.sh" "$t" "$PREFIX" <<'PY' || rc=$?
import json, os, signal, subprocess, sys, tempfile, threading
cmd = ["sh"] + sys.argv[1:4]
# stderr to a file (a full pipe would stall the server); own process group so the kill reaches every child.
errf = tempfile.TemporaryFile(mode="w+")
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True, start_new_session=True)
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
         "capabilities": {}, "clientInfo": {"name": "cortados-install", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
p.stdin.write("".join(json.dumps(m) + "\n" for m in msgs)); p.stdin.flush()
out = {}
def read():
    for line in p.stdout:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == 2:
            out["names"] = sorted(t["name"] for t in m["result"]["tools"])
            return
th = threading.Thread(target=read, daemon=True); th.start(); th.join(30)
if p.poll() is None:   # still running (the normal case: a stdio server waits for more input)
    os.killpg(p.pid, signal.SIGKILL)
p.wait(); errf.seek(0); err = errf.read()
if "names" not in out:
    sys.stderr.write("list_tools: no tools/list answer within 30 s; server stderr:\n" + err)
    sys.exit(1)
print("\n".join(out["names"]))
PY
  rm -rf "$t"; trap - EXIT INT TERM
  return $rc
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
  got="$(list_tools)" || exit 1
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
  check_proxy "$PROXY"
  [ -x "$BIN" ] || { echo "config: $BIN missing; run: $INSTALL_CMD" >&2; exit 2; }
  write_config "$TASK" "$PROXY"
  echo "child flags: --mcp-config $TASK/browser-mcp.json --disallowedTools Bash,mcp__browser__browser_evaluate,mcp__browser__browser_run_code_unsafe,mcp__browser__browser_file_upload,mcp__browser__browser_drop"
  echo "(Bash: the proxy bounds the browser, not a shell. MCP denials are the second layer behind the B1b stdio filter)" ;;
tools)
  # The installed server's tool list (the install gate's input), for review of a pin bump.
  [ -x "$BIN" ] || { echo "tools: $BIN missing; run: $INSTALL_CMD" >&2; exit 2; }
  list_tools ;;
*) echo "usage: $0 install [--browsers] | config <task_dir> <proxy_url> | tools" >&2; exit 2 ;;
esac
