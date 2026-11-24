# The Antigravity (agy) lane (#144)

Status after PR A (2026-11-24): the lane's **quota is read and shown**, honestly. It is **not routed**:
`tables/budgets.json` keeps `ceiling_tokens.antigravity = 0` (the 2026-10-04 containment), so `live.lane_enabled` is
False and `core.resolve`'s fit gate refuses every agy route. The provider serves no agy backend
(`provider/routing.py: SERVED_PROVIDERS` is still `claude-cli`, `codex-cli`).

## What PR A changed

| Piece | Where | What |
|---|---|---|
| Isolation (one owner) | `cortados_router/agy_isolation.py` | Dotenv guard (round 3, below). Environment allowlist for every agy process (`HOME`, `USER`, `LOGNAME`, `TMPDIR`, `LANG`, `LC_*`, fixed `PATH`; `TERM` from the PTY). A `GEMINI_*` / `GOOGLE_*` / `AGY_*` / `CLOUDSDK_*` name in the router's shared env file (`$CORTADOS_HOME/env`) is a hard failure (`forbidden-env`); in the process environment it is dropped. Other vendors' variables are dropped, never fatal (that file serves their lanes too). The binary is a configured absolute path, never searched on PATH. A settings guard reads agy's real config files (never writes them) against **allowlist** schemas (below); a subscription proof reads agy's own `/usage` screen (below). One launch path: `agy_isolation.prepare` runs all of this before every agy process (the probe and `agy models`). |
| Binary | provider `--agy-bin <absolute path>` (or `CORTADOS_AGY_BIN`) | Unset: the lane is **not configured** and never sampled (no PTY opened); the views say "not configured", not FAILED. |
| Honest failures | `scarcity.tui_capture`, `scarcity._antigravity` | `binary-missing` (with `searched`), `exec-failed` (the child could not exec, or exited at once with no output; all lanes now raise on an exec failure instead of returning an empty screen), `not-logged-in`, `first-run-pending` (a first-run screen, or no `antigravity-cli/settings.json` yet), `forbidden-env`, `settings-refused`, `subscription-unproven`. |
| Deny-all | `tui_capture(refuse=...)` | Enter is sent only for the existing workspace-trust prompt. The refusal patterns are checked on every read and before every write, at every stage (boot, after trust, after `/usage`, while paging): a sign-in or first-run screen closes the CLI at once, and the error says truly what had been typed by then ("nothing was typed" only when nothing was). |
| Pager | `scarcity._agy_next_page`, `parse_agy_usage`, `agy_usage_gaps` | agy 1.5.16 shows `/usage` in a scrollable pager ("(1–17 of 30 lines)", ↑/↓, pgdown, ctrl+end Bottom). The probe presses ↓ (navigation only, never text) until every group is read, the footer shows the last line, or a press stops moving the view. Each painted view is parsed on its own (a window counts only under a group header shown above it in the same view) and views are merged per group and window. A reading needs both pools (`gemini`, `claude-gpt`), each with its week and five-hour window, and the pager read to its end; anything less is `partial-reading` ("quota screen read only in part"), an error, never a healthy reading. ↓ rather than pgdown / ctrl+end: stepping one line shows every group that fits in a view whole in some view; a page jump can land with a group's header just above the top. |
| Views | `usage._one_lane` / `live.subscriptions_section` (`state`), `core.subscription_readings` (`state`), `dashboard.html`, the cortados-pulse pane | The lane's state in words: "not configured", "not installed", "could not start", "not logged in", "first-run setup pending", "refused: …". The pane prints it in place of "last sample FAILED". |
| Pools | `scarcity.agy_pool`, `unitcost._pool_of` | By `inventory.antigravity.models`, not a "gemini" substring. Unknown model: `pool_for` raises `UnknownAgyModel`; unit-cost learning charges it to no pool. |
| Tables | `tables/inventory/agy.txt`, `routing-table.json` v23 `inventory.antigravity` (rebased on #143's v21; v22 is the scarcity-v2-api-pacing branch's) | The 27 models agy 1.5.16 lists on the Air (2026-11-24). `gemini` pool: Gemini 4 Neon / Carbon / Argon / Hydrogen (Low … XHigh), the three Orchestrator pairings, Gemini 3.8 Flash (until 2026-11-30). `claude-gpt` pool: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B. R1's `Gemini 3.7 Flash (Medium)` was **removed** (no longer listed; a replacement is a band ruling, below). |
| Inventory script | `bin/refresh-inventory.sh` | Never starts agy itself: it calls `python3 -B -m cortados_router.agy_isolation models` (`run_models` → `prepare`: absolute `CORTADOS_AGY_BIN` only, never PATH; allowlisted env; env-file ban; settings guard; an empty per-run folder) and turns agy 1.5.16's space-padded columns into the tab format `build_tiers.py` reads. Refused or not configured: exit 2, the reason on stderr, `agy.txt` left as it is. |

### Settings guard: the allowlists (`agy_isolation.check_settings`)

Round 2 (2026-11-24): the shapes are agy 1.5.16's **real files on the Air** (key names and types read, values not
printed). agy stores **no auth mode** in its settings; round 1 required `security.auth.selectedType = "oauth-personal"`,
guessed from the Gemini CLI's file on the Pro, and would have refused the owner's correct install. That requirement is
gone; the subscription login is proven from the screen instead (next section).

| File (under `~`) | Allowed | Why |
|---|---|---|
| `.gemini/antigravity-cli/settings.json` | a JSON object with only `trustedWorkspaces` (a list of strings). **Missing: `first-run-pending`** ("first-run setup pending"), not "unsafe": agy writes it on its first run. | The folders the owner trusted. Trust only skips agy's own prompt for a folder; it grants no tool and changes no billing (the probe runs in a fresh per-run folder anyway). |
| `.gemini/config/config.json` | missing, or a JSON object with only `plugins.swarm.enabled` (a boolean) | Swarm is agy's built-in multi-agent plugin (on in the owner's install). It adds no outside tool or billing path, but **one request can fan out to several agents and so multiplies quota use**: PR B must account for it before routing. Any other plugin or key is refused. |
| `.gemini/config/mcp_config.json` | missing, empty (0 bytes or whitespace: agy 1.5.16 leaves it empty), `{}` or `{"mcpServers": {}}` | An MCP server gives agy tools the router never reviewed: any server entry is refused. Other content, and anything unparsable, is refused. |
| `.gemini/settings.json` (the **Gemini CLI's** file; absent on the Air) | absent; or a JSON object whose auth mode (`security.auth.selectedType`, or the older top-level `selectedAuthType`), where set, is `"oauth-personal"`, and with no `mcpServers` | agy may read it. `gemini-api-key` / `api-key`, `vertex-ai`, `compute-default-credentials` / `cloud-shell` / `adc` bill outside the Google AI Pro subscription and are named in the refusal; any other value is refused without being echoed. Its other keys are the Gemini CLI's business. |

Everywhere: an unparsable non-empty file, a non-object and an unknown key are refused (`settings-refused`, fail closed);
`mcpServers`, `tools`, `approvalMode`, `autoAccept`, `yolo`, `security` are refused by name. Values are never echoed
(a value may be a credential or a private path). The env bans (`GEMINI_*`, `GOOGLE_*`, `AGY_*`, `CLOUDSDK_*` in the
router's env file) are unchanged.

### Dotenv guard (`agy_isolation.check_dotenv`, round 3)

The Gemini CLI family loads a `.env` file into its own process environment **after** the router built it, which could
restore what the env allowlist stripped (`GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, ...). Its search: from the
working directory up to `/`, `<dir>/.gemini/.env` then `<dir>/.env`; then `~/.gemini/.env`, `~/.env`. Which of these
agy 1.5.16 reads is not documented, so **every** one that exists is read before any agy start (probe and `agy models`):

- A `GEMINI_*`, `GOOGLE_*`, `AGY_*` or `CLOUDSDK_*` name (any case, with or without `export`): `forbidden-env`.
- An unreadable or unparsable file (a line that is not a comment, a blank or one `NAME=value` / `NAME: value`
  assignment, or a quoted value not closed on its line): `settings-refused`, fail closed.
- Values are never read into a message; the error names the file and the variable names.

The launch folder is fixed: the probe's fresh `p<pid>-<ms>` folder and `agy models`' fresh `agy-models-*` folder are both
made in `$CORTADOS_HOME/usage-probe` (before round 3 `agy models` ran in a system temp folder). `prepare` checks that
parent's walk; the per-run folder itself is checked again just before the start.

### Private text in errors, state and logs (round 3)

An error goes into the lane's record (`scarcity.json`), stderr and the views, so it holds fixed words only: unknown
setting keys are **counted** ("2 unknown setting(s) under 'plugins'"; the path shown holds schema keys only); names in
`forbidden-env` errors are shown only when they look like a variable name (`^[A-Za-z][A-Za-z0-9_.-]{0,40}$`), others
counted; a partial reading names pools (`claude-gpt: no 5-hour window`), not the group title as shown; the "no quota
groups" error gives line and footer counts.

### Subscription proof (`agy_isolation.check_subscription`) [ESCALATE]

With no auth mode in settings, the probe proves the login from what agy itself shows on the `/usage` answer (the
"Models & Quota" box: an "Account:" line and the tier / plan text). Read-only evidence; the account email is matched and
dropped (`scrub_account`) before anything is checked. No error ever carries screen text (round 3: the "no quota
groups" error used to carry the screen's tail, and a private line survived the email scrub); it reports counts only.

- Refused (`subscription-unproven`, "refused: subscription login not proven"): the screen names an API key, Vertex,
  enterprise, pay-as-you-go, Application Default Credentials / ADC, a Cloud project, a billing account or Code Assist
  (even next to a tier), or shows no personal tier.
- Accepted: a personal Google AI tier, `AI Pro` / `Google AI Pro` (and `AI Ultra`, also a personal tier); recorded as
  the reading's `tier`.
- **[ESCALATE] PROVISIONAL:** no captured agy 1.5.16 screen with the tier line is in this repository (both fixtures are
  reconstructed), so `TIER_PATTERN` (`(Google )?AI (Pro|Ultra)`) is a guess at its wording. `TIER_PATTERN_CONFIRMED` is
  `False`, and while it is the lane **refuses every reading** even when the pattern matches, so the agy quota is not
  shown until confirmed. To confirm: the owner sends the `/usage` box's tier / plan line exactly as printed (email
  scrubbed; the tier may stay); pin `TIER_PATTERN` to it, add the paste as a fixture and set the flag `True`.

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

1. The full `/usage` screen in agy 1.5.16, scrubbed, as pasted (the round-1 fixture `tests/fixtures/agy-usage-pager.txt`
   is reconstructed from the description of that screen: two groups, 30 lines, a 17-line view; replace it with the paste): every group header line (e.g. `GEMINI MODELS`), the "Models within this group"
   line, each "Weekly / Five Hour Limit Remaining NN.NN%" line and the "Refreshes in …" / "Quota available" lines, exactly
   as printed, plus which rendering mode was chosen (Alt-Screen or Inline).
2. What a logged-out `agy` prints at start (the sign-in screen), if it can be seen without signing out the real login
   (e.g. on another macOS user); otherwise say so and the pattern stays provisional.
3. The first-run screens' wording (color scheme, rendering mode), if they were noted.
4. ~~`~/.gemini/antigravity-cli/settings.json` key names~~: done 2026-11-24 (round 2 allowlists). Still needed: the
   `/usage` box's tier / plan line exactly as printed, for the subscription proof above.

## Owner steps on the Air (updated 2026-11-24)

agy 1.5.16 is installed at `~/.local/bin/agy` and signed in (Google AI Pro). The installer no longer has
`--skip-aliases`: install with `bash /tmp/agy-install.sh` (`--skip-path` still exists). Remaining:

1. Add `--agy-bin /Users/<you>/.local/bin/agy` (absolute) to the provider's LaunchAgent arguments, then restart it.
   Until then the lane shows "not configured" and agy is never opened.
2. Make sure `~/.cortados/env` holds no `GEMINI_*`, `GOOGLE_*`, `AGY_*` or `CLOUDSDK_*` line (the lane refuses to run).
3. After the next 30-minute sample, the live view / pulse pane shows the Gemini and third-party quota, or the named
   reason it could not ("not logged in", "first-run setup pending", …).
   **Until the tier line is confirmed** (Subscription proof, [ESCALATE]) it shows "refused: subscription login not
   proven": expected, and the cue to send the `/usage` box's tier / plan line.

## Open for the routing PR (B) [ESCALATE]

1. **Quota-only vs served backend.** Serving agy needs a mode with no built-in tools or a verifiable deny-all. The CLI
   reference now shows `--mode plan`, `--sandbox`, `--disable-slash-commands` and `--input-format stream-json`; none is a
   documented "no tools" mode. Options: (i) stay quota-only, (ii) investigate agy's stream-json / remote-control
   protocol first, (iii) accept agy's own permission mode as the boundary (weaker than the Codex rule; needs an explicit ruling).
2. **Lifting the ceiling-0 containment.** Needs the 180,000-byte input guard and exact-session fit accounting, verified,
   and a re-measure of agy's USER_INPUT truncation (~192,000 bytes in 2026-10) on 1.5.16.
3. **Pools (owner decision 2026-11-24, the PR B direction; nothing is routed yet):** each agy quota group is its own pool
   (`gemini`; `claude-gpt`, the Claude and GPT models). A model served through agy sits in the same band as the same model
   reached elsewhere (agy's Claude Opus 5.5 in Opus 5.5's band), and scarcity alone separates the routes: the agy pool's
   own windows against the other lane's.
   **Band placement** of Gemini 4 Neon / Carbon / Argon / Hydrogen (and their effort levels), and what, if anything,
   replaces R1's removed `Gemini 3.7 Flash (Medium)`. Gemini 3.8 Flash ends 2026-11-30. The nightly scoring
   (`tables/benchmarks`) has no Gemini 4 rows yet; vendor numbers are Google's claims.
4. **The third-party pool.** Antigravity offers Claude Opus 5.5 and Sonnet 5.5 (and GPT-OSS 120B) on a quota pool
   **separate from the claude lane's own subscription**: a second source of Opus / Sonnet capacity for the router, like
   Cursor's `other-models`. Whether and when to draw on it (today's rule keeps it secondary) is a ruling.
5. **The Google login on the Air** (login Keychain, usable by anything running as the owner's user) and whether
   automated CLI use of the AI Pro quota fits Google's terms for the account.
6. **Rendering mode** (Alt-Screen vs Inline): the fixture is pinned once the real screen arrives.
7. **Saved conversations**: accept one saved empty conversation per probe, or ask for / wait on a no-save flag.
