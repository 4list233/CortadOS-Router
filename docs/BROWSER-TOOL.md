# CortadOS browser tool (design, 2026-11-03)

Status: design + spike (`integrations/browser/`), revised 2026-11-03 after an independent review against the real 0.0.84 package. Nothing is wired into `child_tasks.py` yet; see "Build tickets".

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
| 5 | `@playwright/mcp` | `browser_evaluate` runs arbitrary JS in the page; `browser_run_code_unsafe` runs code with `vm.runInContext` in the MCP server process (Node) = code execution on the host. Both on by default in 0.0.84. | by design | Not in the stdio filter's allowlist (fail closed) and in the child's `--disallowedTools`. `browser_run_code_unsafe` is never grantable. |
| 6 | `@playwright/mcp` | `browser_file_upload` and `browser_drop` take local paths: any file the process can read can be posted to a page. | by design | Not allowlisted by default; `browser_upload` grant only, paths limited to the task dir by the filter. |
| 7 | `@playwright/mcp` | WebMCP: `browser_webmcp_list` / page-exposed tools inject tool descriptions authored by the page (prompt injection into the tool list). | by design | `--no-webmcp`; the filter's allowlist would drop such tools anyway. |
| 7a | `@playwright/mcp`, Playwright | Environment overrides the config: every `PLAYWRIGHT_MCP_*` variable (`PROXY_SERVER`, `PROXY_BYPASS`, `CDP_ENDPOINT`, `PORT`, `CONFIG`, `EXECUTABLE_PATH`, `ALLOW_UNRESTRICTED_FILE_ACCESS`, `EXTENSION`, ...) is read above the config file; `PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK` lets loopback skip the proxy. | by design | `run-mcp.sh` starts the server with `env -i` (see "Environment is part of the boundary"). |
| 7b | Chromium | WebRTC and QUIC are UDP and bypass an HTTP proxy. | by design | Launch args `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`, `--disable-quic`; never `--proxy-bypass`. |
| 7c | Chromium | `chrome://` internal pages are reachable by navigation (review finding on 0.0.84). | by design | Noted; the profile is throwaway and no secrets live in it. The filter may refuse `chrome://` URLs in `browser_navigate` (B1b). |
| 8 | `playwright` (npm and Python) | CVE-2025-59288 / GHSA-7mvr-c777-76hp: browser install scripts used `curl -k` (no TLS verification) -> MITM of the installer. | 1.55.1 | Pin playwright >= 1.55.1 (current 1.64.1); install only from our script. |
| 9 | Chromium | Default persistent profile (`--user-data-dir` under the user's cache) keeps cookies across sessions; `--extension` / CDP endpoint modes attach to a real browser. | by design | `--isolated` (in-memory profile, gone when the task ends); never pass `--extension`, `--cdp-endpoint`, `--remote-debugging-port`. |
| 10 | Chromium | Downloads land wherever the browser chooses. | by design | `--output-dir <task>/browser/` and download path inside the task dir. |

Latest published versions on 2026-11-03: `@playwright/mcp` 0.0.84, `playwright` 1.64.1.

Sources: [GHSA-6fg3-hvw7-2fwq](https://db.gcve.eu/vuln/ghsa-6fg3-hvw7-2fwq), [CVE-2025-9611](https://app.opencve.io/cve/CVE-2025-9611),
[VulDB 339758 (Origin header, <= 0.0.39)](https://vuldb.com/vuln/339758), [playwright-mcp v0.0.55 release (file:// blocked)](https://newreleases.io/project/github/microsoft/playwright-mcp/release/v0.0.55),
[CVE-2025-59288 (GitLab advisory)](https://advisories.gitlab.com/npm/playwright/CVE-2025-59288/), [GHSA-7mvr-c777-76hp](https://db.gcve.eu/vuln/ghsa-7mvr-c777-76hp),
Option text is quoted from the pinned install's own `--help`, committed as `integrations/browser/help-0.0.84.txt`
(e.g. `--allowed-origins`: "*does not* serve as a security boundary"); the tool list is
`integrations/browser/tools-0.0.84.txt` (default caps, `--no-webmcp`).

## Options considered

**A. Pinned `@playwright/mcp` over stdio (recommended).** Maintained by Microsoft, accessibility-snapshot tools that
models already use well, headless + isolated + output-dir flags exist. The dangerous tools are removed on the Claude
side with `--disallowedTools` AND server-side by our stdio allowlist filter (B1b), so no fork is needed.
Network boundary is our own egress proxy, because the MCP's origin filters are not a boundary.

**B. Small own MCP over Python playwright.** Tool allowlist enforced in the server itself (strongest), but it is new
code to maintain (snapshot/ref model, waits, tabs), a Python venv dependency, and weaker model ergonomics. Keep it as
the fallback if A's tool names churn or `--disallowedTools` proves insufficient.

Both need the same egress proxy; the proxy is what actually enforces "no private networks".

## Recommended design (option A)

### Install (once per machine, CortadOS-owned prefix)
- Prefix: `$CORTADOS_HOME/browser/<pin>/` (`~/.cortados/browser/` live; tests use `CORTADOS_HOME=$(mktemp -d)`).
- Committed `integrations/browser/package.json` + `package-lock.json` (registry tarballs with integrity hashes:
  `@playwright/mcp` 0.0.84, `playwright` / `playwright-core` 1.65.0-alpha-2026-10-30). Install copies both into the
  prefix and runs `npm ci --ignore-scripts` (exactly the lock, no install scripts).
- Gates, any failure aborts the install: installed `@playwright/mcp` version == `PIN_MCP`; `playwright-core` >= 1.55.1;
  `tools/list` of the installed server (same flags and scrubbed env as a task) == `tools-0.0.84.txt` byte for byte.
- Browsers: `PLAYWRIGHT_BROWSERS_PATH=<prefix>/browsers <prefix>/node_modules/.bin/playwright install --only-shell chromium`
  (headless shell only; nothing global, nothing under `~/Library/Caches/ms-playwright`).
- Script: `integrations/browser/install.sh`. The router package stays stdlib-only and never imports it.

### Per-task launch (stdio, no port)
`install.sh config <task_dir> <proxy_url>` (B3 moves this into `child_tasks._prepare`) refuses with exit 2 unless the
proxy URL is given and the pinned binary is executable (message carries the install command); there is no
boundary-less config. It writes two files and the child gets `--mcp-config <task_dir>/browser-mcp.json`:
```json
{"mcpServers": {"browser": {"command": "<repo>/integrations/browser/run-mcp.sh",
  "args": ["<task_dir>", "<prefix>", "http://<token>@127.0.0.1:<egress_port>"]}}}
```
`<task_dir>/browser/config.json` (Playwright config; only what has no safe CLI flag):
```json
{"browser": {"browserName": "chromium", "isolated": true, "launchOptions": {"headless": true, "chromiumSandbox": true,
  "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]}}}
```
`run-mcp.sh` then runs `cd <task>/browser && exec env -i PATH=<node dir>:/usr/bin:/bin HOME=<task>/browser/home
TMPDIR=<task>/browser/tmp PLAYWRIGHT_BROWSERS_PATH=<prefix>/browsers <prefix>/node_modules/.bin/playwright-mcp
--config <task>/browser/config.json --headless --isolated --sandbox --no-webmcp --browser chromium
--proxy-server <proxy> --blocked-origins <list> --output-dir <task>/browser/out --block-service-workers
--timeout-navigation 30000 --timeout-action 10000`. With B1b the command becomes the stdio filter, which execs the wrapper.
- `--blocked-origins` entries carry ports (`http://localhost:*;https://localhost:*;http://127.0.0.1:*;...;http://[::1]:*;
  ...;http://0.0.0.0:*`); without a port an entry does not match a dev server. First filter only.
- Never: `--port`, `--host`, `--allowed-hosts`, `--extension`, `--cdp-endpoint`, `--executable-path`, `--proxy-bypass`,
  `--no-sandbox`, `--browser chrome` (the installed Google Chrome), `--allow-unrestricted-file-access`.
- Headless always: there is no option that shows a window. No screen takeover is possible.
- Profile: `--isolated` keeps it in memory plus a throwaway dir under `TMPDIR`, which is why the wrapper points `TMPDIR`
  into the task dir; deleted with the task. The owner's Chrome profile and cookies are never read (Playwright's own
  Chromium headless shell, not `/Applications/Google Chrome.app`).

### Environment is part of the boundary
The server reads its options from the environment above its config file, so the environment it inherits from the
child could undo every flag. `run-mcp.sh` uses `env -i` and sets only `PATH`, `HOME`, `TMPDIR`,
`PLAYWRIGHT_BROWSERS_PATH`. Must be absent (they are, by construction; B3 tests assert the wrapper's child env):
`PLAYWRIGHT_MCP_PROXY_SERVER`, `PLAYWRIGHT_MCP_PROXY_BYPASS`, `PLAYWRIGHT_MCP_CDP_ENDPOINT`, `PLAYWRIGHT_MCP_CDP_HEADER`,
`PLAYWRIGHT_MCP_PORT`, `PLAYWRIGHT_MCP_HOST`, `PLAYWRIGHT_MCP_CONFIG`, `PLAYWRIGHT_MCP_EXECUTABLE_PATH`,
`PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS`, `PLAYWRIGHT_MCP_EXTENSION`, every other `PLAYWRIGHT_MCP_*`,
`PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK`, `HTTP(S)_PROXY`/`NO_PROXY`, `NODE_OPTIONS`.

### Tools on / off (two layers)
Source of truth: `integrations/browser/tools-0.0.84.txt`, the pinned server's `tools/list` (25 tools). Install fails if
the installed server's list differs, so a version bump is a reviewed diff, never silent.
1. **Server side, independent of Claude Code permissions (B1b):** `integrations/browser/mcp_filter.py`, a small stdlib
   stdio filter between `claude` and `run-mcp.sh`. It ALLOWLISTS: `tools/list` responses are rewritten to the allowed
   names; a `tools/call` for any other name is answered with a JSON-RPC error and never forwarded; unknown methods
   other than the MCP basics are refused; a malformed message closes the session (fail closed). Default allowlist =
   the pinned list minus `browser_evaluate`, `browser_run_code_unsafe`, `browser_file_upload`, `browser_drop`.
   Grants add back `browser_evaluate` (`browser_js`) and `browser_file_upload` / `browser_drop` (`browser_upload`, with
   every path argument required to resolve under `<task_dir>/browser/in`). `browser_run_code_unsafe` is never
   grantable (host code execution). The filter also counts pages (see Caps).
2. **Claude side:** the child's `--disallowedTools` adds `mcp__browser__browser_evaluate`,
   `mcp__browser__browser_run_code_unsafe`, `mcp__browser__browser_file_upload`, `mcp__browser__browser_drop` (minus
   grants). Defence in depth only; layer 1 is the one that holds if Claude Code permissions are misconfigured.

### Network boundary: per-task egress proxy
A small stdlib HTTP/CONNECT proxy (`integrations/browser/egress_proxy.py`, own process per task, bound to 127.0.0.1 on
an ephemeral port, random token in the proxy URL) that resolves each host itself and refuses connections whose
resolved address is loopback (127/8, ::1), RFC1918 (10/8, 172.16/12, 192.168/16), CGNAT/Tailscale (100.64.0.0/10,
fd7a:115c:a1e0::/48), link-local (169.254/16, fe80::/10), ULA (fc00::/7), 0.0.0.0, multicast. It connects to the IP it
checked (no second resolution), which closes DNS rebinding. Address checks (B2, unit test per form):
`getaddrinfo` the host and refuse if ANY answer is in a denied range (not just the first); normalise IPv4-mapped
`::ffff:a.b.c.d` and NAT64 `64:ff9b::/96` to the embedded IPv4 before checking; parse IPv4 literals the way browsers
and `inet_aton` do (hex `0x7f000001`, octal `0177.0.0.1`, short `127.1`, integer `2130706433`) so they cannot slip past
a dotted-quad regex; then connect to the checked IP. The proxy answers `407 Proxy Authentication Required` with
`Proxy-Authenticate: Basic` to a request without the token, because Chromium only sends proxy credentials after a 407. `browser_allow` on start_task adds explicit exceptions,
e.g. `["http://localhost:5173"]` for a dev server: exact scheme+host+port only. Optional `browser_sites` allowlist turns
the proxy into allow-only. Every refusal is logged to the task's event stream.

### Caps
- Time: the browser is part of the child, so the child's `max_s` applies; additionally the proxy exits and the MCP is
  killed at `browser_max_s` (default 600 s).
- Pages: HTTPS goes through `CONNECT` tunnels, so the proxy cannot see requests or `Sec-Fetch-Dest`; page counting
  lives in the stdio filter, which counts `browser_navigate`, `browser_navigate_back` and new-tab `browser_tabs` calls
  and refuses after `browser_max_pages` (default 50). Page-initiated navigations are not counted; the byte budget
  below bounds them.
- Bytes: the proxy enforces a per-task tunnel byte budget (both directions, default 500 MB) and closes all tunnels and
  refuses new ones when it is spent.
- Downloads: also invisible inside TLS. A watcher polls the size of `<task_dir>/browser/out` (`--output-max-size` as
  a first limit) and kills the MCP process group above 200 MB total; the event records `cap_output`.

### Artifacts back to the caller
Screenshots and saved sessions go to `<task_dir>/browser/out` (0.0.84 has no `--save-trace`). On completion `task_status` / `wait_task` return
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
- Full 0.0.84 `--help` is committed as `integrations/browser/help-0.0.84.txt`. We pass `--config --headless --isolated
  --sandbox --no-webmcp --browser --proxy-server --blocked-origins --output-dir --block-service-workers
  --timeout-navigation --timeout-action`; never `--port --host --allowed-hosts --endpoint --extension --cdp-endpoint
  --cdp-header --executable-path --shared-browser-context --storage-state --no-sandbox --ignore-https-errors
  --init-script --proxy-bypass --allow-unrestricted-file-access`.
- Review round (2026-11-03): `tools/list` of 0.0.84 (`tools-0.0.84.txt`) has `browser_run_code_unsafe` and
  `browser_drop`, and no `browser_run_code` / `browser_install`; the earlier deny list was wrong, hence the allowlist
  filter. Verified locally: `install.sh install` (npm ci of the lock, version gates, tool-list gate) passes on a
  throwaway `CORTADOS_HOME`; `config` exits 2 without a proxy URL or without the binary; the wrapper answers
  `initialize` with `PLAYWRIGHT_MCP_PORT` / `PLAYWRIGHT_MCP_CDP_ENDPOINT` set in the caller (scrubbed).
- There is no `--save-trace` in 0.0.84. Trace/screenshot artifacts come from `--output-dir` (screenshots, `--save-session`
  to be evaluated in B1). `--isolated` keeps the profile in memory, so no `--user-data-dir` is passed.
- Browser download still to do: `CORTADOS_HOME=<home> integrations/browser/install.sh install --browsers`
  (`playwright install --only-shell chromium` into `<prefix>/browsers`, about 100 MB).

## Build tickets
- B1 Pin + install: done in this branch except the browser download. `package.json` + `package-lock.json`, `npm ci
  --ignore-scripts`, version gates, `tools-0.0.84.txt` / `help-0.0.84.txt` as source of truth, `run-mcp.sh`, `config`
  requiring proxy + binary. Test idea (B3 suite): run `install.sh config` with a fake `$BIN` (an executable stub in a
  throwaway prefix) and assert the two JSON files exactly, exit 2 without proxy, exit 2 without `$BIN`; run
  `run-mcp.sh` with a fake `$BIN` that dumps `env` and `argv` and assert only the four variables and the fixed flags.
- B1b Stdio allowlist filter (stdlib, `integrations/browser/mcp_filter.py`): allowlist from `tools-0.0.84.txt` minus
  the default-off tools, grants, path checks for upload/drop, `chrome://` / `file://` refusal in `browser_navigate`,
  page counting, fail closed on anything unparseable. Unit tests with a fake server over pipes: hidden tools absent
  from `tools/list`, `tools/call browser_run_code_unsafe` refused even when every grant is on, page cap.
- B2 Egress proxy (stdlib): CONNECT + HTTP, 407 + token, resolve-once-and-pin, ANY-answer private-net deny, allow
  exceptions, tunnel byte budget, JSON event log. Unit tests with fake resolvers (no network), one per form:
  plain private v4, multi-answer with one private, `::1`, `::ffff:127.0.0.1`, `::ffff:7f00:1`, `64:ff9b::a00:1`,
  `0x7f000001`, `0177.0.0.1`, `127.1`, `2130706433`, `100.64.x` / `fd7a:115c:a1e0::`, link-local, `0.0.0.0`,
  rebinding (second resolution never happens), missing token -> 407.
- B3 child_tasks: `browser` and `browser_*` fields, mutual exclusion with chrome, config writing, `--disallowedTools`,
  proxy + filter lifecycle, output-dir watcher, cleanup of `<task>/browser`, artifacts in status. Tests use the fake
  claude binary and a fake `$BIN`; no real browser.
- B4 Gateway grant check + refusal messages; tests.
- B5 Dashboard columns/event types.
- B6 One manual end-to-end walkthrough on a throwaway `CORTADOS_HOME` (headless, public site + local dev server via
  `browser_allow`); confirm the downloaded build is chrome-for-testing's `chromium-headless-shell` (`--only-shell`),
  that WebRTC leaks no local/UDP candidates and QUIC is off; evidence block per `.claude/skills/verify-router/SKILL.md`.
- B7 Upgrade check: monthly look at GHSA for `@playwright/mcp` and `playwright`; bump pin only after the tool-list and
  `--help` diffs are reviewed and both files are re-committed.
