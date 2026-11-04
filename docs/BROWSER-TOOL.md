# CortadOS browser tool (design, 2026-11-03)

Status: design + spike (`integrations/browser/`), revised 2026-11-03 after two independent review rounds against the real 0.0.84 package. Nothing is wired into `child_tasks.py` yet; see "Build tickets".

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
| 7c | `@playwright/mcp`, Chromium | `checkUrlAllowed` blocks only `file:`; `javascript:`, `data:`, `blob:`, `chrome://` navigations pass, and a `javascript:` / `data:` navigation runs script in the page exactly as the denied `browser_evaluate` would. | by design | The B1b filter admits only `http:`, `https:` and the exact URL `about:blank` in `browser_navigate` and in `browser_tabs` new-tab URLs; every other scheme is refused (allowlist, not a deny list). |
| 7d | `@playwright/mcp` | Without `--ignore-roots` the server asks the client for its workspace roots (`roots/list`) and its file checks then allow reads and writes anywhere under them: the child's repo, `.env` included, for upload/drop, screenshots and `filename=` outputs. | by design | `--ignore-roots` in `run-mcp.sh` and in the install gate's listing (tool list unchanged by it), so the workspace is the cwd `<task>/browser`; the B1b filter also never forwards `roots/list`. |
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
  `tools/list` of the installed server == `tools-0.0.84.txt` byte for byte. The listing starts the server through
  `run-mcp.sh` on a throwaway task with a dummy proxy (no page is opened), so flags and env are exactly a task's; it
  reads until the `id:2` answer with a 30 s cap and prints the server's stderr when there is none. A stdout line that
  is not a JSON-RPC object, or an `id:2` answer without a tools list, fails the listing at once with the line and the
  server's stderr (a stdio server's stdout carries only protocol messages; garbage is never skipped)
  (`install.sh tools` shows the list on its own); and each tool's `inputSchema.properties` keys == `args-0.0.84.json`
  (`install.sh args`), the per-tool argument-key allowlist the filter enforces.
- Browsers: `PLAYWRIGHT_BROWSERS_PATH=<prefix>/browsers <prefix>/node_modules/.bin/playwright install --only-shell chromium`
  (headless shell only; nothing global, nothing under `~/Library/Caches/ms-playwright`).
- Script: `integrations/browser/install.sh`. The router package stays stdlib-only and never imports it.

### Per-task launch (stdio, no port)
`install.sh config <task_dir> < proxy_url` (B3 moves this into `child_tasks._prepare`) reads the proxy URL from stdin,
never argv (a third argument is refused with exit 2), and refuses with exit 2 unless the proxy URL is given, has the exact shape `http://<token>@127.0.0.1:<port>` (token of at least 16 `[A-Za-z0-9_-]`; no
other host, no https, no password, no path or bypass), and the pinned binary is executable (message carries the
install command); there is no boundary-less config. It writes two files, both 0600 (inside the 0700 task dir), and
the child gets `--mcp-config <task_dir>/browser-mcp.json`:
```json
{"mcpServers": {"browser": {"command": "<absolute python3>", "args": ["-I", "<repo>/integrations/browser/mcp_filter.py",
  "--task", "<task_dir>", "--browsers-path", "<prefix>/browsers", "--", "<repo>/integrations/browser/run-mcp.sh",
  "<task_dir>", "<prefix>"]}}}
```
`<task_dir>/browser/config.json` (Playwright config; only what has no safe CLI flag, plus the proxy):
```json
{"browser": {"browserName": "chromium", "isolated": true,
  "launchOptions": {"headless": true, "chromiumSandbox": true,
    "proxy": {"server": "http://127.0.0.1:<egress_port>", "username": "<token>", "password": ""},
    "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]},
  "contextOptions": {"proxy": {"server": "http://127.0.0.1:<egress_port>", "username": "<token>", "password": ""}}}}
```
The proxy token lives only in this file, never in argv (`ps` shows argv to every local user) and never in
`browser-mcp.json`. That holds end to end: the caller pipes the URL to `install.sh config` on stdin, the shape check
reads it through a pipe from the `printf` builtin, and the config writer gets it on stdin with its program passed as
`python3 -I -c` (tests record every external command's argv and assert the token is absent). `run-mcp.sh` refuses with exit 2 unless both proxies are present, point at `127.0.0.1`, carry a
username and have no `bypass`; then it runs `cd <task>/browser && exec env -i PATH=<node dir>:/usr/bin:/bin
HOME=<task>/browser/home TMPDIR=<task>/browser/tmp PLAYWRIGHT_BROWSERS_PATH=<prefix>/browsers
<prefix>/node_modules/.bin/playwright-mcp --config <task>/browser/config.json --headless --isolated --sandbox
--no-webmcp --ignore-roots --browser chromium --blocked-origins <list> --output-dir <task>/browser/out
--block-service-workers --timeout-navigation 30000 --timeout-action 10000`. With B1b (built) the command is the stdio
filter, which starts the wrapper as its child over pipes. B6 confirms on the real build that the config-file proxy is what Chromium uses (a
page fetch shows up in the proxy log; with the proxy stopped, navigation fails).
- `--ignore-roots`: the workspace (the server's file boundary) is the cwd `<task>/browser`, never the client's roots
  (item 7d). B1b's `<task>/browser/in` path check for upload/drop is then defence in depth, not the only line.
- `--timeout-navigation 30000`: half the 60 s default, so a hung page fails a tool call well inside `browser_max_s`.
  `--timeout-action 10000`: twice the 5 s default, because a cold headless shell on a loaded host misses 5 s on
  ordinary clicks; still short enough that a stuck action returns an error instead of stalling the child.
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
   names; a `tools/call` for any other name is answered with a JSON-RPC error and never forwarded; URL arguments of
   `browser_navigate` and of `browser_tabs` new-tab calls must be `http:` / `https:` or exactly `about:blank`, every
   other scheme (`javascript:`, `data:`, `blob:`, `chrome:`, `file:`, `view-source:`, ...) is refused, and so is a URL
   that does not parse. Fail-closed cases, each with a unit test:
   - client->server methods allowed: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`,
     `notifications/cancelled`; anything else gets a JSON-RPC error (requests) or is dropped (notifications);
   - a top-level JSON array (batch), invalid JSON, a non-object message or a line over the size cap closes the session;
   - `tools/call` with missing `params`, non-object `params`, non-string `name` or non-object `arguments` is refused;
   - `tools/call` with an argument key outside the tool's set in `args-0.0.84.json` is refused (-32005), `filename` is
     refused on every tool (-32004: the server auto-names outputs under `--output-dir`), and upload/drop `paths` must be
     absolute and resolve under `<task>/browser/in` (-32004);
   - closing (bad input, stdin EOF, SIGTERM/SIGHUP) sends SIGTERM to the server's group, waits `EXIT_GRACE_S`, sends
     SIGKILL, then kills every surviving descendant found by walking `ps` from the server pid (Playwright launches
     Chromium detached, in its own group), every descendant recorded by a walk repeated every 2 s while running, and
     every process of this user that is this task's browser: its kernel-reported executable (libproc `proc_pidpath`,
     never argv[0], which a process can forge) is a Playwright Chromium / Chrome for Testing / headless-shell binary
     under `--browsers-path` (the `PLAYWRIGHT_BROWSERS_PATH` run-mcp.sh sets, written by `install.sh config`) and its
     kernel argv (`KERN_PROCARGS2`) has a whole `--user-data-dir=<path>` entry whose realpath is inside
     `<task>/browser/tmp` (TMPDIR, so `--isolated` puts the profile there). That still finds a browser reparented
     after the server exited on its own, and never a shell or other program that merely mentions the task dir; where
     the kernel identity is unavailable (not macOS, call failed) nothing is killed by command line, and the failure is
     reported on stderr once per filter process (a pid that is gone, ESRCH/EINVAL, is the normal race and silent).
     The argv buffer is parsed exactly (argv[0] at the 8-byte-aligned offset after the exec path, then exactly argc
     NUL-terminated entries, never the environment that follows); an unterminated entry, an empty argv[0], non-NUL
     padding or an out-of-range argc makes the process "not ours" (empty entries after argv[0] are kept).
     Recorded pids carry their `ps` start time (`lstart`), re-checked right before every signal, so a pid recycled by
     an unrelated process is skipped. The descendant walk starts only from the server pid with the start time recorded
     at spawn, so a recycled server pid's children are never adopted; the server's group is signalled only while the
     server is still an unreaped child, never after (its former members are signalled one by one, each verified). One
     lock owns the server process: every poll/wait goes through it and the "unreaped? then signal the group" step runs
     under it, so no other thread can reap the server in between. A failing `ps` (error or nonzero exit, in the
     process listing and in each start-time check) is reported on stderr with its message, and means nothing is
     signalled for that pid.
     SIGTERM/SIGHUP handlers only write a byte to a self-pipe; a coordinator thread runs the close, so a signal
     landing while the main thread holds the filter's state lock cannot deadlock;
   - server->client requests (`roots/list`, `sampling/*`, `elicitation/*`, any other) are answered by the filter with a
     JSON-RPC error and never forwarded to Claude Code; server notifications other than progress/log/`list_changed`
     are dropped, and a `tools/list_changed` makes the filter re-list and re-apply the allowlist. Default allowlist =
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
- The proxy bounds the browser, not the child. A child with a shell (`Bash`) could `curl` a private host or start
  `<prefix>/node_modules/.bin/playwright-mcp` itself without the proxy, so "no boundary-less launch" holds for the MCP
  path only. Therefore a `browser: true` child carries no shell tool by default: B3 adds `Bash` (and any other
  command-running tool the child has) to its `--disallowedTools`. A shell is given back only when the requester is
  separately granted it; gateway browser tasks never get it without a `shell` grant in the B4 table.

### Bot gateway
Gateway-originated tasks (Slack/Telegram bots) carry `browser: true` only if the gateway's grant table lists the
requester for `browser`; otherwise refuse with `browser not granted for this requester`. Gateway browser tasks always
run with `Bash` in `--disallowedTools` unless the requester also holds a `shell` grant (see Opt-in). `browser_allow` with private
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
  --sandbox --no-webmcp --ignore-roots --browser --blocked-origins --output-dir --block-service-workers
  --timeout-navigation --timeout-action` (the proxy comes from the config file, not `--proxy-server`); never `--port --host --allowed-hosts --endpoint --extension --cdp-endpoint
  --cdp-header --executable-path --shared-browser-context --storage-state --no-sandbox --ignore-https-errors
  --init-script --proxy-bypass --allow-unrestricted-file-access`.
- Review round (2026-11-03): `tools/list` of 0.0.84 (`tools-0.0.84.txt`) has `browser_run_code_unsafe` and
  `browser_drop`, and no `browser_run_code` / `browser_install`; the earlier deny list was wrong, hence the allowlist
  filter. Verified locally: `install.sh install` (npm ci of the lock, version gates, tool-list gate) passes on a
  throwaway `CORTADOS_HOME`; `config` exits 2 without a proxy URL or without the binary; the wrapper answers
  `initialize` with `PLAYWRIGHT_MCP_PORT` / `PLAYWRIGHT_MCP_CDP_ENDPOINT` set in the caller (scrubbed).
- Review round 2: tool list unchanged by `--ignore-roots` and by `clientInfo.name`; the claims above are now pinned by
  `tests/test_browser_tool.py` (stub server, stdlib, part of the full suite).
- There is no `--save-trace` in 0.0.84. Trace/screenshot artifacts come from `--output-dir` (screenshots, `--save-session`
  to be evaluated in B1). `--isolated` keeps the profile in memory, so no `--user-data-dir` is passed.
- Browser download still to do: `CORTADOS_HOME=<home> integrations/browser/install.sh install --browsers`
  (`playwright install --only-shell chromium` into `<prefix>/browsers`, about 100 MB).

## Build tickets
- B1 Pin + install: done in this branch except the browser download. `package.json` + `package-lock.json`, `npm ci
  --ignore-scripts`, version gates, `tools-0.0.84.txt` / `help-0.0.84.txt` as source of truth, `run-mcp.sh`, `config`
  requiring proxy + binary. Tests: `tests/test_browser_tool.py` with a stub `$BIN` in a throwaway prefix: the two
  JSON files byte-exact and 0600, token absent from `browser-mcp.json` and argv; exit 2 without proxy, with a
  wrong-shaped proxy, without `$BIN`, without config, and for a config whose proxy is missing, has a bypass or points
  off loopback; a poisoned caller env reaches the stub as exactly `PATH HOME TMPDIR PLAYWRIGHT_BROWSERS_PATH` with
  the fixed argv (incl. `--ignore-roots`) and cwd `<task>/browser`; `install.sh tools` lists through the wrapper and
  shows the server's stderr when it fails.
- B1b Stdio allowlist filter (stdlib, `integrations/browser/mcp_filter.py`): allowlist from `tools-0.0.84.txt` minus
  the default-off tools, grants, path checks for upload/drop, URL scheme allowlist (`http:`, `https:`, `about:blank`)
  in `browser_navigate` and `browser_tabs` new-tab, page counting, the fail-closed cases listed under "Tools on / off".
  Unit tests with a fake server over pipes: hidden tools absent from `tools/list`, `tools/call browser_run_code_unsafe`
  refused even when every grant is on, page cap; one refusal test per scheme (`javascript:alert(1)`, `JavaScript:`
  mixed case, leading whitespace, `data:text/html,...`, `blob:`, `chrome://settings`, `file:///etc/passwd`,
  `view-source:http://x`, unparseable) and admission of `http`, `https`, `about:blank` (but not `about:srcdoc`); one
  test per fail-closed case (batch array, bad JSON, non-object, oversize line, bad `params` / `name` / `arguments`,
  disallowed method, `roots/list` and `sampling/createMessage` from the server answered with an error and not seen by
  the client).
- B2 Egress proxy (stdlib): CONNECT + HTTP, 407 + token, resolve-once-and-pin, ANY-answer private-net deny, allow
  exceptions, tunnel byte budget, JSON event log. Unit tests with fake resolvers (no network), one per form:
  plain private v4, multi-answer with one private, `::1`, `::ffff:127.0.0.1`, `::ffff:7f00:1`, `64:ff9b::a00:1`,
  `0x7f000001`, `0177.0.0.1`, `127.1`, `2130706433`, `100.64.x` / `fd7a:115c:a1e0::`, link-local, `0.0.0.0`,
  rebinding (second resolution never happens), missing token -> 407.
- B3 child_tasks: `browser` and `browser_*` fields, mutual exclusion with chrome, config writing, `--disallowedTools`
  (browser denials plus `Bash` unless a shell grant; test that a `browser: true` child's argv carries it),
  proxy + filter lifecycle, output-dir watcher, cleanup of `<task>/browser`, artifacts in status. Tests use the fake
  claude binary and a fake `$BIN`; no real browser.
- B4 Gateway grant check + refusal messages; tests.
- B5 Dashboard columns/event types.
- B6 One manual end-to-end walkthrough on a throwaway `CORTADOS_HOME` (headless, public site + local dev server via
  `browser_allow`); confirm the downloaded build is chrome-for-testing's `chromium-headless-shell` (`--only-shell`),
  that WebRTC leaks no local/UDP candidates and QUIC is off; evidence block per `.claude/skills/verify-router/SKILL.md`.
- B7 Upgrade check: monthly look at GHSA for `@playwright/mcp` and `playwright`; bump pin only after the tool-list and
  `--help` diffs are reviewed and both files are re-committed.
