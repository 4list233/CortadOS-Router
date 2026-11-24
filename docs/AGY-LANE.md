# The Antigravity (agy) lane (#144)

Status after PR A (2026-11-24): the lane's **quota is read and shown**, honestly. It is **not routed**:
`tables/budgets.json` keeps `ceiling_tokens.antigravity = 0` (the 2026-10-04 containment), so `live.lane_enabled` is
False and `core.resolve`'s fit gate refuses every agy route. The provider serves no agy backend
(`provider/routing.py: SERVED_PROVIDERS` is still `claude-cli`, `codex-cli`).

## What PR A changed

| Piece | Where | What |
|---|---|---|
| Isolation (one owner) | `cortados_router/agy_isolation.py` | Environment allowlist for every agy process (`HOME`, `USER`, `LOGNAME`, `TMPDIR`, `LANG`, `LC_*`, fixed `PATH`; `TERM` from the PTY). A `GEMINI_*` / `GOOGLE_*` / `AGY_*` / `CLOUDSDK_*` name in the router's shared env file (`$CORTADOS_HOME/env`) is a hard failure (`forbidden-env`); in the process environment it is dropped. Other vendors' variables are dropped, never fatal (that file serves their lanes too). The binary is a configured absolute path, never searched on PATH. A settings guard reads `~/.gemini/antigravity-cli/settings.json` (never writes it) and refuses MCP servers, `always-proceed` / `proceed-in-sandbox`, an API key or ADC; an unreadable file is refused (fail closed). |
| Binary | provider `--agy-bin <absolute path>` (or `CORTADOS_AGY_BIN`) | Unset: the lane is **not configured** and never sampled (no PTY opened); the views say "not configured", not FAILED. |
| Honest failures | `scarcity.tui_capture`, `scarcity._antigravity` | `binary-missing` (with `searched`), `exec-failed` (the child could not exec, or exited at once with no output; all lanes now raise on an exec failure instead of returning an empty screen), `not-logged-in`, `first-run-pending`, `forbidden-env`, `settings-refused`. |
| Deny-all | `tui_capture(refuse=...)` | Enter is sent only for the existing workspace-trust prompt. A sign-in or first-run screen (before or after trust) closes the CLI at once with nothing typed. |
| Views | `usage._one_lane` / `live.subscriptions_section` (`state`), `core.subscription_readings` (`state`), `dashboard.html`, the cortados-pulse pane | The lane's state in words: "not configured", "not installed", "could not start", "not logged in", "first-run setup pending", "refused: …". The pane prints it in place of "last sample FAILED". |
| Pools | `scarcity.agy_pool`, `unitcost._pool_of` | By `inventory.antigravity.models`, not a "gemini" substring. Unknown model: `pool_for` raises `UnknownAgyModel`; unit-cost learning charges it to no pool. |
| Tables | `tables/inventory/agy.txt`, `routing-table.json` v21 `inventory.antigravity` | The 27 models agy 1.5.16 lists on the Air (2026-11-24). `gemini` pool: Gemini 4 Neon / Carbon / Argon / Hydrogen (Low … XHigh), the three Orchestrator pairings, Gemini 3.8 Flash (until 2026-11-30). `claude-gpt` pool: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B. R1's `Gemini 3.7 Flash (Medium)` was **removed** (no longer listed; a replacement is a band ruling, below). |
| Inventory script | `bin/refresh-inventory.sh` | Runs only `$CORTADOS_AGY_BIN`, on the same allowlisted env; turns agy 1.5.16's space-padded columns into the tab format `build_tiers.py` reads. |

### Saved conversations
agy saves every interactive session: on start it prints "Resume with -c (or command below): agy --conversation=<uuid>".
The CLI reference (antigravity.google/docs/cli/reference, read 2026-11-24) lists `-c/--continue` and `--conversation`
but **no flag to start without saving**, so each `/usage` probe (every 30 min) may leave one empty saved conversation in
agy's own store. The probe's per-run folder is removed; agy's store is not touched by the router. If a later agy adds a
no-save flag, add it to the probe's argv in `scarcity._antigravity`.

## Still provisional: the screens

The `/usage` parser and the refusal patterns are written against the format the 2026-10 probe parsed and Google's docs,
not a captured agy 1.5.16 screen. Fixture: `tests/fixtures/agy-usage-provisional.txt`. To confirm them, the owner sends
(scrubbed of the account email; the plan name may stay):

1. The full `/usage` screen in agy 1.5.16: every group header line (e.g. `GEMINI MODELS`), the "Models within this group"
   line, each "Weekly / Five Hour Limit Remaining NN.NN%" line and the "Refreshes in …" / "Quota available" lines, exactly
   as printed, plus which rendering mode was chosen (Alt-Screen or Inline).
2. What a logged-out `agy` prints at start (the sign-in screen), if it can be seen without signing out the real login
   (e.g. on another macOS user); otherwise say so and the pattern stays provisional.
3. The first-run screens' wording (color scheme, rendering mode), if they were noted.
4. `~/.gemini/antigravity-cli/settings.json` key names (values may be redacted), so the settings guard can match exact
   keys instead of key families.

## Owner steps on the Air (updated 2026-11-24)

agy 1.5.16 is installed at `~/.local/bin/agy` and signed in (Google AI Pro). The installer no longer has
`--skip-aliases`: install with `bash /tmp/agy-install.sh` (`--skip-path` still exists). Remaining:

1. Add `--agy-bin /Users/<you>/.local/bin/agy` (absolute) to the provider's LaunchAgent arguments, then restart it.
   Until then the lane shows "not configured" and agy is never opened.
2. Make sure `~/.cortados/env` holds no `GEMINI_*`, `GOOGLE_*`, `AGY_*` or `CLOUDSDK_*` line (the lane refuses to run).
3. After the next 30-minute sample, the live view / pulse pane shows the Gemini and third-party quota, or the named
   reason it could not ("not logged in", "first-run setup pending", …).

## Open for the routing PR (B) [ESCALATE]

1. **Quota-only vs served backend.** Serving agy needs a mode with no built-in tools or a verifiable deny-all. The CLI
   reference now shows `--mode plan`, `--sandbox`, `--disable-slash-commands` and `--input-format stream-json`; none is a
   documented "no tools" mode. Options: (i) stay quota-only, (ii) investigate agy's stream-json / remote-control
   protocol first, (iii) accept agy's own permission mode as the boundary (weaker than the Codex rule; needs an explicit ruling).
2. **Lifting the ceiling-0 containment.** Needs the 180,000-byte input guard and exact-session fit accounting, verified,
   and a re-measure of agy's USER_INPUT truncation (~192,000 bytes in 2026-10) on 1.5.16.
3. **Band placement** of Gemini 4 Neon / Carbon / Argon / Hydrogen (and their effort levels), and what, if anything,
   replaces R1's removed `Gemini 3.7 Flash (Medium)`. Gemini 3.8 Flash ends 2026-11-30. The nightly scoring
   (`tables/benchmarks`) has no Gemini 4 rows yet; vendor numbers are Google's claims.
4. **The third-party pool.** Antigravity offers Claude Opus 5.5 and Sonnet 5.5 (and GPT-OSS 120B) on a quota pool
   **separate from the claude lane's own subscription**: a second source of Opus / Sonnet capacity for the router, like
   Cursor's `other-models`. Whether and when to draw on it (today's rule keeps it secondary) is a ruling.
5. **The Google login on the Air** (login Keychain, usable by anything running as the owner's user) and whether
   automated CLI use of the AI Pro quota fits Google's terms for the account.
6. **Rendering mode** (Alt-Screen vs Inline): the fixture is pinned once the real screen arrives.
7. **Saved conversations**: accept one saved empty conversation per probe, or ask for / wait on a no-save flag.
