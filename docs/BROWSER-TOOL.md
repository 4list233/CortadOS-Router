# CortadOS browser tool (design, 2026-11-03)

Status: design + spike (`integrations/browser/`). Nothing is wired into `child_tasks.py` yet; see "Build tickets".

## Why
Routed child tasks (`cortados_router/child_tasks.py`, headless `claude -p`) need browser walkthroughs and UI testing.
- Claude in Chrome from headless sessions is unreliable ("Browser extension is not connected", issue #48), and
  `chrome: true` (login mode) runs unrouted on the owner's real, signed-in Chrome.
- Playwright driving the owner's real Chrome took over the screen. Not acceptable.
- The main session must not do browser work itself.

Forest's decision: CortadOS gets its own browser tool. Playwright is allowed, with care about its current vulnerabilities.

## Known security issues (researched 2026-11-03)

| # | Component | Issue | Fixed in | Mitigation here |
|---|---|---|---|---|
| 1 | `@playwright/mcp` | CVE-2025-9611 / GHSA-6fg3-hvw7-2fwq: HTTP/SSE transport did not validate `Origin`, so a malicious page could reach a locally running server via DNS rebinding and call its tools. | 0.0.40 | stdio only, never `--port`; pin >= 0.0.84. |
| 2 | `@playwright/mcp` | `--allowed-hosts` (Host-header check for the HTTP transport) defaults to the bound host; `*` disables it. | n/a (config) | Irrelevant with stdio; never pass `--port`/`--host`/`--allowed-hosts`. |
| 3 | `@playwright/mcp` | `--allowed-origins` / `--blocked-origins` are documented as "not a security boundary" and do not apply to redirects. | n/a (by design) | Use them only as a first filter; the real network boundary is the per-task egress proxy (below). |
| 4 | `@playwright/mcp` | `file://` navigation / API requests. Release v0.0.55 restricts navigation and API requests to `http:`, `https:`, `about:`, `data:`; `PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS` re-opens it. | 0.0.55 | Pin >= 0.0.84; never set the unrestricted-file-access env/flag unless the task explicitly grants `file://`. |
| 5 | `@playwright/mcp` | `browser_evaluate` / `browser_run_code` run arbitrary JS (page context; `run_code` in the Playwright server context = Node, i.e. code execution on the host). | by design | Off by default via the child's `--disallowedTools`. |
| 6 | `@playwright/mcp` | `browser_file_upload` can read any local file the process can read and post it to a page. | by design | Off by default; when granted, roots limited to the task dir. |
| 7 | `@playwright/mcp` | `browser_install` downloads and installs browsers at agent request. | by design | Off (tool disallowed); browsers are installed once by our install script. |
| 8 | `playwright` (npm and Python) | CVE-2025-59288 / GHSA-7mvr-c777-76hp: browser install scripts used `curl -k` (no TLS verification) -> MITM of the installer. | 1.55.1 | Pin playwright >= 1.55.1 (current 1.64.1); install only from our script. |
| 9 | Chromium | Default persistent profile (`--user-data-dir` under the user's cache) keeps cookies across sessions; `--extension` / CDP endpoint modes attach to a real browser. | by design | `--isolated` (in-memory profile, gone when the task ends); never pass `--extension`, `--cdp-endpoint`, `--remote-debugging-port`. |
| 10 | Chromium | Downloads land wherever the browser chooses. | by design | `--output-dir <task>/browser/` and download path inside the task dir. |

Latest published versions on 2026-11-03: `@playwright/mcp` 0.0.84, `playwright` 1.64.1.

Sources: [GHSA-6fg3-hvw7-2fwq](https://db.gcve.eu/vuln/ghsa-6fg3-hvw7-2fwq), [CVE-2025-9611](https://app.opencve.io/cve/CVE-2025-9611),
[VulDB 339758 (Origin header, <= 0.0.39)](https://vuldb.com/vuln/339758), [playwright-mcp v0.0.55 release (file:// blocked)](https://newreleases.io/project/github/microsoft/playwright-mcp/release/v0.0.55),
[CVE-2025-59288 (GitLab advisory)](https://advisories.gitlab.com/npm/playwright/CVE-2025-59288/), [GHSA-7mvr-c777-76hp](https://db.gcve.eu/vuln/ghsa-7mvr-c777-76hp),
[playwright-mcp README options mirror (allowed-origins "not a security boundary", --isolated, --allowed-hosts)](https://github.com/Coder-RL/playwright-mcp).
Third-party mirrors were used for option text; confirm against `npx @playwright/mcp@0.0.84 --help` of the pinned install (ticket B1).

## Options considered

**A. Pinned `@playwright/mcp` over stdio (recommended).** Maintained by Microsoft, accessibility-snapshot tools that
models already use well, headless + isolated + output-dir flags exist. The dangerous tools are removed on the Claude
side with `--disallowedTools` (child_tasks already builds that list for chrome children), so no fork is needed.
Network boundary is our own egress proxy, because the MCP's origin filters are not a boundary.

**B. Small own MCP over Python playwright.** Tool allowlist enforced in the server itself (strongest), but it is new
code to maintain (snapshot/ref model, waits, tabs), a Python venv dependency, and weaker model ergonomics. Keep it as
the fallback if A's tool names churn or `--disallowedTools` proves insufficient.

Both need the same egress proxy; the proxy is what actually enforces "no private networks".

## Recommended design (option A)

### Install (once per machine, CortadOS-owned prefix)
- Prefix: `$CORTADOS_HOME/browser/<pin>/` (`~/.cortados/browser/` live; tests use `CORTADOS_HOME=$(mktemp -d)`).
- `npm install --prefix <prefix> --ignore-scripts --no-save @playwright/mcp@0.0.84` (exact pin, lockfile committed
  in `integrations/browser/`), then `PLAYWRIGHT_BROWSERS_PATH=<prefix>/browsers <prefix>/node_modules/.bin/playwright install chromium`
  (headless shell only; nothing global, nothing under `~/Library/Caches/ms-playwright`).
- Script: `integrations/browser/install.sh` (spike). The router package stays stdlib-only and never imports it.

### Per-task launch (stdio, no port)
`child_tasks._prepare` writes `<task_dir>/browser-mcp.json` and appends `--mcp-config <file>` exactly as connectors are
passed today:
```json
{"mcpServers": {"browser": {"command": "<prefix>/node_modules/.bin/playwright-mcp",
  "args": ["--headless", "--isolated", "--browser", "chromium",
           "--output-dir", "<task_dir>/browser/out",
           "--proxy-server", "http://127.0.0.1:<egress_port>",
           "--blocked-origins", "<private-net hosts>", "--block-service-workers",
           "--timeout-navigation", "30000", "--timeout-action", "10000"],
  "env": {"PLAYWRIGHT_BROWSERS_PATH": "<prefix>/browsers", "HOME": "<task_dir>/browser/home"}}}}
```
- Never: `--port`, `--host`, `--allowed-hosts`, `--extension`, `--cdp-endpoint`, `--executable-path` to the owner's
  Chrome, `--browser chrome` (which uses the installed Google Chrome), `PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS`.
- Headless always: there is no option that shows a window. No screen takeover is possible.
- Profile: isolated, under the task dir, deleted when the task ends. The owner's Chrome profile and cookies are never
  read (the browser binary is Playwright's own Chromium, not `/Applications/Google Chrome.app`).

### Tools on / off
Child `--disallowedTools` adds, by default: `mcp__browser__browser_evaluate`, `mcp__browser__browser_run_code`,
`mcp__browser__browser_file_upload`, `mcp__browser__browser_install`. Grants (`browser_js: true`, `browser_upload: true`)
remove the corresponding entries. Anything not in the pinned version's known tool list is disallowed (fail closed on
upgrade: ticket B1 snapshots the tool list).

### Network boundary: per-task egress proxy
A small stdlib HTTP/CONNECT proxy (`integrations/browser/egress_proxy.py`, own process per task, bound to 127.0.0.1 on
an ephemeral port, random token in the proxy URL) that resolves each host itself and refuses connections whose
resolved address is loopback (127/8, ::1), RFC1918 (10/8, 172.16/12, 192.168/16), CGNAT/Tailscale (100.64.0.0/10,
fd7a:115c:a1e0::/48), link-local (169.254/16, fe80::/10), ULA (fc00::/7), 0.0.0.0, multicast. It connects to the IP it
checked (no second resolution), which closes DNS rebinding. `browser_allow` on start_task adds explicit exceptions,
e.g. `["http://localhost:5173"]` for a dev server: exact scheme+host+port only. Optional `browser_sites` allowlist turns
the proxy into allow-only. Every refusal is logged to the task's event stream.

### Caps
- Time: the browser is part of the child, so the child's `max_s` applies; additionally the proxy exits and the MCP is
  killed at `browser_max_s` (default 600 s).
- Pages: the proxy counts top-level navigations (`Sec-Fetch-Dest: document`) and refuses after `browser_max_pages`
  (default 50); downloads capped at 50 MB each / 200 MB total in `<task_dir>/browser/out/downloads`.

### Artifacts back to the caller
`--save-trace` + screenshots go to `<task_dir>/browser/out`. On completion `task_status` / `wait_task` return
`browser: {artifacts: [paths], trace: <path>, pages: N, refused: [hosts]}`; the final screenshot path is appended to the
result text so the orchestrator can `Read` it.

### Opt-in (start_task)
- `browser: true` (default false). Mutually exclusive with `chrome: true` -> `TaskError("browser and chrome are
  mutually exclusive: chrome drives the owner's signed-in Chrome, browser a throwaway headless one")`.
- Unlike chrome, a browser child stays ROUTED (provider lane, S1 decides the model); the browser is just an MCP server.
  S1 task text should mention browsing so outward floors apply (a browser can post forms = outward effect).
- Extra fields, all optional, validated strictly: `browser_allow` (list of origins incl. private dev servers),
  `browser_sites` (allow-only list), `browser_js`, `browser_upload`, `browser_file` (allow `file://` under cwd only),
  `browser_max_pages`, `browser_max_s`.
- If the pinned install is missing -> refuse with the install command; never fall back to Chrome or a global Playwright.

### Bot gateway
Gateway-originated tasks (Slack/Telegram bots) carry `browser: true` only if the gateway's grant table lists the
requester for `browser`; otherwise refuse with `browser not granted for this requester`. `browser_allow` with private
addresses, `browser_js`, `browser_upload`, `browser_file` need a separate `browser_private` / `browser_unsafe` grant.
Refusals are logged, never silently downgraded.

### Dashboard records
Per task, from `child-tasks.jsonl`: `browser: true`, pinned versions (`@playwright/mcp`, chromium build), grants used,
pages visited (count + origins), refused hosts, downloads (name, size), artifact paths, browser wall time, exit reason
(done / cap_pages / cap_time / killed). No page content or cookies are recorded.

## Spike findings (2026-11-03, temp CORTADOS_HOME, no browsers downloaded)
- `npm install --ignore-scripts @playwright/mcp@0.0.84` = 3 packages, under 1 s; it pulls a `playwright-core`
  1.65 alpha (>= 1.55.1, so CVE-2025-59288 is fixed).
- 0.0.84 `--help` has: `--headless --isolated --browser --output-dir --output-max-size --proxy-server --proxy-bypass
  --blocked-origins --allowed-origins --block-service-workers --timeout-action --timeout-navigation --idle-timeout
  --caps --file-paths --allow-unrestricted-file-access --save-session`, and the ones we must never pass: `--port --host
  --allowed-hosts --endpoint --extension --cdp-endpoint --cdp-header --executable-path --shared-browser-context
  --storage-state --no-sandbox --ignore-https-errors --init-script`.
- There is no `--save-trace` in 0.0.84. Trace/screenshot artifacts come from `--output-dir` (screenshots, `--save-session`
  to be evaluated in B1). `--isolated` keeps the profile in memory, so no `--user-data-dir` is passed.
- Browser download still to do: `CORTADOS_HOME=<home> integrations/browser/install.sh install --browsers`
  (`playwright install --only-shell chromium` into `<prefix>/browsers`, about 100 MB).

## Build tickets
- B1 Pin + install: `integrations/browser/install.sh` + committed `package.json`/`package-lock.json`; record the pinned
  `--help` output and tool list into `integrations/browser/tools-0.0.84.txt`; refuse versions < 0.0.84 / playwright < 1.55.1.
- B2 Egress proxy (stdlib): CONNECT + HTTP, resolve-once-and-pin, private-net deny, allow exceptions, page/download caps,
  JSON event log. Unit tests with fake resolvers (no network).
- B3 child_tasks: `browser` and `browser_*` fields, mutual exclusion with chrome, config writing, `--disallowedTools`,
  proxy lifecycle, cleanup of the profile dir, artifacts in status. Tests use the fake claude binary; no real browser.
- B4 Gateway grant check + refusal messages; tests.
- B5 Dashboard columns/event types.
- B6 One manual end-to-end walkthrough on a throwaway `CORTADOS_HOME` (headless, public site + local dev server via
  `browser_allow`), evidence block per `.claude/skills/verify-router/SKILL.md`.
- B7 Upgrade check: monthly look at GHSA for `@playwright/mcp` and `playwright`; bump pin only after the tool-list diff.
