#!/bin/sh
# Launch the pinned playwright-mcp for one CortadOS child task with a scrubbed environment.
# Design: docs/BROWSER-TOOL.md ("Environment is part of the boundary").
#   run-mcp.sh <task_dir> <prefix> <proxy_url>
# Reason for env -i: playwright-mcp reads every PLAYWRIGHT_MCP_* variable (PROXY_SERVER, PROXY_BYPASS, CDP_ENDPOINT,
# PORT, CONFIG, EXECUTABLE_PATH, ALLOW_UNRESTRICTED_FILE_ACCESS, EXTENSION, ...) ABOVE its config file, and Playwright
# honours PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK. Any of them inherited from the child's environment
# would silently remove the proxy, attach a real browser or open file://. So nothing is inherited; every value the
# server sees is set here; options are the fixed flags below plus the config file written by install.sh config.
set -eu
USAGE="usage: run-mcp.sh <task_dir> <prefix> <proxy_url>"
TASK="${1:?$USAGE}"
PREFIX="${2:?$USAGE}"
# The per-task egress proxy is the network boundary; without it there is no browser (no boundary-less launch).
PROXY="${3:?$USAGE (the per-task egress proxy is required)}"
BIN="$PREFIX/node_modules/.bin/playwright-mcp"
CFG="$TASK/browser/config.json"
# Fail closed: no binary or no config means no browser, never a default-configured one.
[ -x "$BIN" ] || { echo "run-mcp: $BIN missing; run: CORTADOS_HOME=<home> integrations/browser/install.sh install --browsers" >&2; exit 2; }
[ -f "$CFG" ] || { echo "run-mcp: $CFG missing; run install.sh config first" >&2; exit 2; }
NODE="$(command -v node)" || { echo "run-mcp: node not on PATH" >&2; exit 2; }
NODE_DIR="$(dirname "$NODE")"
mkdir -p "$TASK/browser/home" "$TASK/browser/tmp" "$TASK/browser/out"
# cwd inside the task: relative paths the server resolves land in the task dir, not the repo.
cd "$TASK/browser"
# TMPDIR: --isolated keeps its throwaway profile under the OS temp dir, so it is pointed into the task and removed
# with it. Only the flags below are passed; port/host/extension/cdp/executable-path/proxy-bypass are never set.
# --blocked-origins carries ports: an entry without one would not match a dev server on :5173. It is a first filter
# only ("not a security boundary"); the proxy refuses the same ranges by resolved address.
BLOCKED="http://localhost:*;https://localhost:*;http://127.0.0.1:*;https://127.0.0.1:*;http://[::1]:*;https://[::1]:*;http://0.0.0.0:*;https://0.0.0.0:*"
exec env -i \
  PATH="$NODE_DIR:/usr/bin:/bin" \
  HOME="$TASK/browser/home" \
  TMPDIR="$TASK/browser/tmp" \
  PLAYWRIGHT_BROWSERS_PATH="$PREFIX/browsers" \
  "$BIN" --config "$CFG" --headless --isolated --sandbox --no-webmcp --browser chromium \
    --proxy-server "$PROXY" --blocked-origins "$BLOCKED" \
    --output-dir "$TASK/browser/out" --block-service-workers \
    --timeout-navigation 30000 --timeout-action 10000
