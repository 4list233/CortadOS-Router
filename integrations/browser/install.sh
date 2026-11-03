#!/bin/sh
# Spike: install a pinned @playwright/mcp into a CortadOS-owned prefix and write a hardened per-task MCP config.
# Design: docs/BROWSER-TOOL.md. Nothing global: no npm -g, browsers under the prefix, never ~/Library/Caches/ms-playwright.
#   install.sh install [--browsers]   npm install into $PREFIX (and, with --browsers, download Playwright's chromium)
#   install.sh config <task_dir> [proxy_url]   write <task_dir>/browser-mcp.json
set -eu
PIN_MCP=0.0.84        # >= 0.0.40 (CVE-2025-9611 DNS rebinding), >= 0.0.55 (file:// blocked)
: "${CORTADOS_HOME:?set CORTADOS_HOME (tests: CORTADOS_HOME=\$(mktemp -d))}"
PREFIX="$CORTADOS_HOME/browser/mcp-$PIN_MCP"
BIN="$PREFIX/node_modules/.bin/playwright-mcp"

case "${1:-}" in
install)
  mkdir -p "$PREFIX"
  # --ignore-scripts: no install-time scripts run; browsers are fetched explicitly below.
  npm install --prefix "$PREFIX" --ignore-scripts --no-fund --no-audit --save-exact "@playwright/mcp@$PIN_MCP"
  # playwright core must be >= 1.55.1 (CVE-2025-59288, curl -k in install scripts).
  node -e 'const v=require(process.argv[1]+"/node_modules/playwright-core/package.json").version.split(/[.-]/).slice(0,3).map(x=>parseInt(x,10));
    if (v[0]<1||(v[0]==1&&(v[1]<55||(v[1]==55&&v[2]<1)))) {console.error("playwright-core "+v.join(".")+" < 1.55.1");process.exit(1)}
    console.log("playwright-core "+v.join("."))' "$PREFIX"
  if [ "${2:-}" = "--browsers" ]; then
    PLAYWRIGHT_BROWSERS_PATH="$PREFIX/browsers" "$PREFIX/node_modules/.bin/playwright" install --only-shell chromium
  else
    echo "browsers not downloaded; run: CORTADOS_HOME=$CORTADOS_HOME $0 install --browsers"
  fi ;;
config)
  TASK="${2:?task_dir}"; PROXY="${3:-}"
  mkdir -p "$TASK/browser/profile" "$TASK/browser/out" "$TASK/browser/home"
  python3 -I - "$BIN" "$PREFIX/browsers" "$TASK" "$PROXY" <<'PY'
import json, sys
bin_, browsers, task, proxy = sys.argv[1:5]
args = ["--headless", "--isolated", "--browser", "chromium",
        "--output-dir", f"{task}/browser/out", "--block-service-workers",
        "--timeout-navigation", "30000", "--timeout-action", "10000",
        # first filter only ("not a security boundary"); the egress proxy is the boundary
        "--blocked-origins", "localhost;127.0.0.1;[::1];0.0.0.0"]
if proxy:
    args += ["--proxy-server", proxy]
cfg = {"mcpServers": {"browser": {"command": bin_, "args": args,
       "env": {"PLAYWRIGHT_BROWSERS_PATH": browsers, "HOME": f"{task}/browser/home"}}}}
open(f"{task}/browser-mcp.json", "w").write(json.dumps(cfg, indent=2) + "\n")
print(f"{task}/browser-mcp.json")
PY
  echo "child flags: --mcp-config $TASK/browser-mcp.json --disallowedTools mcp__browser__browser_evaluate,mcp__browser__browser_run_code,mcp__browser__browser_file_upload,mcp__browser__browser_install" ;;
*) echo "usage: $0 install [--browsers] | config <task_dir> [proxy_url]" >&2; exit 2 ;;
esac
