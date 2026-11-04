#!/bin/sh
# Install the pinned @playwright/mcp into a CortadOS-owned prefix and write a hardened per-task MCP config.
# Design: docs/BROWSER-TOOL.md. Nothing global: no npm -g, browsers under the prefix, never ~/Library/Caches/ms-playwright.
#   install.sh install [--browsers]            npm ci the committed lockfile into $PREFIX, check versions and tool list
#                                              (with --browsers, also download Playwright's chromium headless shell)
#   install.sh config <task_dir> < proxy_url   write <task_dir>/browser/config.json and <task_dir>/browser-mcp.json (0600);
#                                              the proxy URL (it carries the token) is read from stdin, never argv
#   install.sh tools                           print the installed server's tools/list (same launch as a task)
#   install.sh args                            print its per-tool argument keys (the args-<pin>.json format)
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
# <task>/browser-mcp.json (0600). The token is in the config file, never in any argv, so ps does not show it: the URL
# reaches python on stdin (printf is a shell builtin, no process), so the program itself is passed with -c.
WRITE_CONFIG_PY="$(cat <<'PY'
import json, os, sys
from urllib.parse import urlsplit
wrapper, prefix, task = sys.argv[1:4]
proxy = sys.stdin.readline().strip()
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
# Claude Code talks only to the stdio allowlist filter (B1b, mcp_filter.py, run with python3 -I so no PYTHON* variable
# reaches it), which starts the server through run-mcp.sh (scrubbed env, fixed flags) as its child over pipes. No
# "env" block here, because whatever Claude Code passes would be dropped by env -i anyway.
filt = os.path.join(os.path.dirname(wrapper), "mcp_filter.py")
# The interpreter is recorded as an absolute path (the interpreter running this writer), so the PATH claude runs with cannot swap the
# python that runs the filter.
write(f"{task}/browser-mcp.json", {"mcpServers": {"browser": {"command": sys.executable,
    # --browsers-path: the PLAYWRIGHT_BROWSERS_PATH run-mcp.sh sets; the filter kills by command line only browser
    # executables under it.
    "args": ["-I", filt, "--task", task, "--browsers-path", os.path.join(prefix, "browsers"), "--",
             wrapper, task, prefix]}}})
print(f"{task}/browser-mcp.json")
PY
)"
write_config() {
  mkdir -p "$1/browser/out" "$1/browser/home" "$1/browser/tmp"
  ( umask 077; printf '%s\n' "$2" | python3 -I -c "$WRITE_CONFIG_PY" "$HERE/run-mcp.sh" "$PREFIX" "$1" )
}

# list_tools [names|args]: tools/list of the installed server. names: one name per line, sorted. args: JSON object
# {tool: sorted inputSchema.properties keys}, the per-tool argument-key allowlist mcp_filter.py enforces. Started through run-mcp.sh on a throwaway task with
# a dummy proxy (no page is opened), so the flags and scrubbed env are exactly a task's: the advertised tools depend
# on them (--no-webmcp, --caps). Reads until the id:2 answer, 30 s cap (a slow host is not a mismatch); on failure
# the server's stderr is shown so the cause is visible.
list_tools() {
  mode="${1:-names}"
  t="$(mktemp -d)"
  trap 'rm -rf "$t"' EXIT INT TERM
  write_config "$t" "http://listtoolsdummytoken0@127.0.0.1:9" >/dev/null
  rc=0
  python3 -I - "$HERE/run-mcp.sh" "$t" "$PREFIX" "$mode" <<'PY' || rc=$?
import json, os, signal, subprocess, sys, tempfile, threading
cmd = ["sh"] + sys.argv[1:4]
mode = sys.argv[4]
# stderr to a file (a full pipe would stall the server); own process group so the kill reaches every child.
errf = tempfile.TemporaryFile(mode="w+")
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True, start_new_session=True)
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
         "capabilities": {}, "clientInfo": {"name": "cortados-install", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
p.stdin.write("".join(json.dumps(m) + "\n" for m in msgs)); p.stdin.flush()
out = {}
# A stdio MCP server's stdout carries only JSON-RPC messages, so a line that is not one (or an id:2 answer without a
# tools list) is a broken or unexpected server: fail with the line shown, never skip it and pass on what follows.
def read():
    for line in p.stdout:
        try:
            m = json.loads(line)
            if not isinstance(m, dict):
                raise ValueError("not a JSON-RPC object")
            if m.get("id") == 2:
                tools = m["result"]["tools"]
                out["names"] = sorted(t["name"] for t in tools)
                # A tool without inputSchema.properties takes no arguments: its allowed key set is empty.
                out["args"] = {t["name"]: sorted((t.get("inputSchema") or {}).get("properties") or {}) for t in tools}
                return
        except (ValueError, KeyError, TypeError) as e:
            out["bad"] = f"{e.__class__.__name__}: {e}; line: {line.strip()[:200]!r}"
            return
th = threading.Thread(target=read, daemon=True); th.start(); th.join(30)
if p.poll() is None:   # still running (the normal case: a stdio server waits for more input)
    os.killpg(p.pid, signal.SIGKILL)
p.wait(); errf.seek(0); err = errf.read()
if "bad" in out:
    sys.stderr.write("list_tools: malformed MCP protocol output (" + out["bad"] + "); server stderr:\n" + err)
    sys.exit(1)
if "names" not in out:
    sys.stderr.write("list_tools: no tools/list answer within 30 s; server stderr:\n" + err)
    sys.exit(1)
if mode == "args":
    print(json.dumps(out["args"], indent=1, sort_keys=True))
else:
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
  # Argument-key gate: the filter refuses any tools/call key outside the tool's reviewed set (args-<pin>.json), so a
  # bump that adds a key (a new path or URL argument) fails the install instead of passing silently, same as a name.
  got="$(list_tools args)" || exit 1
  if [ "$got" != "$(cat "$HERE/args-$PIN_MCP.json")" ]; then
    echo "tools/list inputSchema.properties differ from $HERE/args-$PIN_MCP.json:" >&2
    printf '%s\n' "$got" | diff "$HERE/args-$PIN_MCP.json" - >&2 || true
    exit 1
  fi
  echo "tools/list argument keys match args-$PIN_MCP.json"
  if [ "${2:-}" = "--browsers" ]; then
    PLAYWRIGHT_BROWSERS_PATH="$PREFIX/browsers" "$PREFIX/node_modules/.bin/playwright" install --only-shell chromium
  else
    echo "browsers not downloaded; run: $INSTALL_CMD"
  fi ;;
config)
  TASK="${2:?usage: install.sh config <task_dir> < proxy_url}"
  # The proxy URL carries the token, and argv is visible to every local user through ps: it is read from stdin only.
  # A third argument is refused (not ignored) so a caller that still passes it in argv finds out.
  [ $# -eq 2 ] || { echo "config: the proxy URL is read from stdin, never argv: install.sh config <task_dir> < proxy_url" >&2; exit 2; }
  # No proxy, no config: a browser without the egress proxy has no network boundary.
  PROXY=""
  IFS= read -r PROXY || true
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
args)
  # The installed server's per-tool argument keys (the argument-key gate's input), for review of a pin bump.
  [ -x "$BIN" ] || { echo "args: $BIN missing; run: $INSTALL_CMD" >&2; exit 2; }
  list_tools args ;;
*) echo "usage: $0 install [--browsers] | config <task_dir> < proxy_url | tools | args" >&2; exit 2 ;;
esac
