# The Antigravity (agy) lane (#144)

Status after PR A (2026-11-24): the lane's **quota is read and shown**, honestly. It is **not routed**:
`tables/budgets.json` keeps `ceiling_tokens.antigravity = 0` (the 2026-10-04 containment), so `live.lane_enabled` is
False and `core.resolve`'s fit gate refuses every agy route. The provider serves no agy backend
(`provider/routing.py: SERVED_PROVIDERS` is still `claude-cli`, `codex-cli`).

## What PR A changed

| Piece | Where | What |
|---|---|---|
| Isolation (one owner) | `cortados_router/agy_isolation.py` | Dotenv guard (round 3, below). Environment allowlist for every agy process (`HOME`, `USER`, `LOGNAME`, `TMPDIR`, `LANG`, `LC_*`, fixed `PATH`; `TERM` from the PTY). A `GEMINI_*` / `GOOGLE_*` / `AGY_*` / `CLOUDSDK_*` name in the router's shared env file (`$CORTADOS_HOME/env`) is a hard failure (`forbidden-env`); in the process environment it is dropped. Other vendors' variables are dropped, never fatal (that file serves their lanes too). The binary is a configured absolute path, never searched on PATH. A settings guard reads agy's real config files (never writes them) against **allowlist** schemas (below); a subscription proof reads the tier from agy's own start-up banner (below). One launch path: `agy_isolation.prepare` runs all of this before every agy process (the probe and `agy models`). |
| Binary | provider `--agy-bin <absolute path>` (or `CORTADOS_AGY_BIN`) | Unset: the lane is **not configured** and never sampled (no PTY opened); the views say "not configured", not FAILED. |
| Honest failures | `scarcity.tui_capture`, `scarcity._antigravity` | `binary-missing` (with `searched`), `exec-failed` (the child could not exec, or exited at once with no output; all lanes now raise on an exec failure instead of returning an empty screen), `not-logged-in`, `signin_timeout` (agy still `Signing in...` after `AGY_SIGNIN_ALLOWANCE_S`, below), `first-run-pending` (a first-run screen, or no `antigravity-cli/settings.json` yet), `forbidden-env`, `settings-refused`, `subscription-unproven`. |
| Deny-all | `tui_capture(refuse=..., answer_trust=False)` | Before the banner's account line is proven the agy probe types **nothing at all**. The refusal patterns are checked on every read and before every write, at every stage (boot, after `/usage`, while paging): a sign-in, first-run or trust screen closes the CLI at once, and the error says truly what had been typed by then ("nothing was typed" only when nothing was). One exception, checked when the probe is about to act rather than mid-read (`refuse_unsettled`, `AGY_UNSETTLED`): agy 1.5.16 paints `You are currently not signed in.` / `Signing in...` while it loads its keyring token, even when it is signed in (Air, 2026-11-25). That notice stops nothing while agy signs in; at the end of boot and before any keystroke it refuses `not-logged-in` unless an account line came after it. Refusing it mid-read had closed agy before its token refresh finished, so every probe read "not logged in" (#144). **Slow sign-in (2026-11-27).** Under the provider's LaunchAgent on the Air the `Signing in...` spinner outlasted the 12 s boot window (`check: unsettled`, `after_s: 12.78`, `signing_in: 1`, `account_lines: 0`) and a signed-in agy was refused `not-logged-in`. So the end of boot now tells a sign-in **progress** notice from a sign-in **prompt** (`scarcity.agy_signing_in`): progress is the last `not signed in` notice followed by a `Signing in` spinner, with no account line after it, no sign-in rule (`_AGY_SIGN_IN_RULES`: Google URL, `Sign in with Google`, authorization URL/code, `please sign in`, account picker, consent, login-method picker) and no prompt marker (`_AGY_PROMPT_MARKERS`: a URL, `browser`, `code`, `press …` other than a `press ctrl+c` quit hint, `choose`, `select`, `enter`, `[y/n]` / `(y/n)`, a numbered menu line, a line ending in `?`). While only progress shows, `tui_capture(settling=AGY_SETTLING)` keeps reading, typing nothing, until the account line appears (then the probe goes on as before), the screen stops being progress (a prompt is refused mid-read by `refuse` as `not-logged-in`, as before; anything else is judged as before), or `AGY_SIGNIN_ALLOWANCE_S` (60 s from agy's first paint) runs out: then it refuses `signin_timeout` ("sign-in not finished in time (agy still signing in)"), check `settling`, with `after_s` and `allowance_s` in the refusal facts, so the views tell "slow" from "not signed in". A prompt is never waited on. The lock, folder and reaper rules are unchanged, and the claude / codex / cursor probes pass no `settling`. The spinner screen of the tests (`SIGNING_IN` and the fake's `spin_s` repaints) is modelled on the 2026-11-25 capture; no new agy screen was captured for this change. A refusal keeps `refusal` facts on the failed record (`scarcity.json` → `lanes.antigravity.refusal`, from `scarcity.agy_refusal_facts` and `tui_capture`): which check (`refuse` mid-read, `unsettled` or `trust` about to act), seconds since start, whether the banner had been seen, `trust_prompt_seen` (a workspace-trust prompt was painted at any point), the sign-in rule that matched by a fixed name, and the counts and order of notices, `Signing in` spinners and account lines; names and numbers only, never screen text. **Workspace trust (fix round 5).** agy runs in one fixed folder, `$CORTADOS_HOME/agy-probe/workspace`, that the owner trusts in agy once by hand ("One-time owner step" below). A trust prompt there means the folder is not trusted: it is refused mid-read as `workspace_not_trusted` ("refused: probe folder not trusted in agy"), with no keystroke, whether it comes before or after the banner. Rounds 1–4 instead ran agy in a new folder each run and pressed Enter on its trust prompt only while a terminal-visibility check (`_trust_prompt_last`, a model of rows and columns) said it was still on screen; every review found another terminal trick that hid the prompt yet passed (an erase, scrolling it off, wrapping, a DECSTBM scroll region `ESC[1;2r` then a cursor move and newlines, a bare LF in raw mode). A correct check needs a full terminal emulator, so the keystroke was removed instead. The code never writes agy's settings or its trust list. An account picker (`Choose account`, `Select an account`, `Use another account`) and a Google consent screen are sign-in rules of their own (`account-picker`, `consent`) and refuse `not-logged-in` mid-read. So is agy's login-method picker (`login-method-picker`: `Select login method:` / `> 1. Google OAuth` / `2. Use a Google Cloud project`, drawn signed out after the notice and a `Signing in...` spinner, by a bare-CR overwrite; seen on the Air 2026-11-25 from a headless child). The boot window starts when the CLI first paints; its start-up is waited for separately, up to the same `boot_s` (fix round 3: a slow fake start-up under a loaded test run ate the 3 s window and the screen read as empty). The other CLIs (claude, codex, cursor) still answer their own trust prompt with one Enter, as on main, and the read after it gets a fresh `boot_s`, as on main (fix round 6 restored this; rounds 3–5 had given it only what was left of the one boot window). |
| Probe folder | `scarcity.agy_probe_workspace`, `agy_probe_paths` | One fixed folder, `$CORTADOS_HOME/agy-probe/workspace`, under an exclusive `flock` on `$CORTADOS_HOME/agy-probe/lock` so two probes never share it. A probe that cannot take the lock within `AGY_PROBE_LOCK_WAIT_S` (10 s) skips its sample (`probe_busy`, "skipped: another probe holds the probe folder"): no agy is started, the lane keeps its last reading with a `last_skip` note, and the skip is not written to the history as a sample. Before agy starts (fix round 6: every check is bound to an open directory fd, not to a pathname): the path is walked from `/` one component at a time, each directory opened with `O_DIRECTORY|O_NOFOLLOW` relative to the verified fd of its parent and checked with `fstat` on that fd. **Ancestor rule:** every directory above `$CORTADOS_HOME` must be owned by root or the user and not group/other-writable, unless root owns it and it is sticky (`/tmp`, `/private/tmp`: only an entry's owner can rename it); a shared writable non-sticky parent would let another user replace the state dir. A symlink on the way is followed only when root owns it (macOS's own `/var` and `/tmp`); `$CORTADOS_HOME` is owned by the user and not group/other-writable; `agy-probe` and `workspace` are owned by the user with mode 0700 (made 0700 when missing; an existing one with another mode is refused, never changed); failures are `probe_folder_unsafe`. The lock is opened relative to the `agy-probe` fd (`O_NOFOLLOW`, 0600), the emptiness check lists the `workspace` fd, and the child enters the folder with `fchdir` on that fd, so renaming or symlinking anything after the checks cannot move it. Under the lock: **no process is ever killed because its cwd is in the folder.** If any process has its cwd in the folder (`probe_reaper.cwd_pids_in`, lsof; as a non-root user it sees the user's own processes, and no other user can enter the 0700 folder), the sample is skipped (`probe_folder_busy`, "skipped: a process is open in the probe folder"): no agy is started, the last reading stays, and nothing is killed: it may be the owner's own agy. If lsof cannot run, the sample is refused (`probe_folder_unchecked`). The probe's own leftovers are handled only by what the probe started: right after the fork it writes the child's pid, uid and start time (`ps` `lstart`) to `agy-probe/probe-child` (through the dir fd, 0600) and removes it after the child is reaped; the next run kills nothing (fix round 7: a pid checked once and killed later can be reused in between, and `lstart` is to the second): while a process still matches that exact pid AND start time AND uid, the sample is skipped (`probe_leftover_running`, "skipped: a previous probe's agy is still running") and the record stays; otherwise the record is dropped and the probe goes on. Helpers that outlived a dead probe child are not killed either: they keep the folder busy, and samples skip until the owner ends them. The end-of-probe cleanup signals only the child it spawned and that child's own process group (pgid = its pid, from `pty.fork`) while the child is still unreaped, never a scanned list of pids and never by cwd. **A skip is published** (fix round 7): the lane record's `last_skip` holds the reason and `since` (when the current run of skips for that reason began); `scarcity.lane_state` gives its sentence, and `/v1/cortados/usage` gives `state` plus `skip: {reason, since}` (reason one of `probe_busy`, `probe_folder_busy`, `probe_leftover_running`). The retained reading is not a new sample, and once the skips have lasted more than `SKIP_STALE_INTERVALS` (2) sample intervals it is stale. A fresh reading or a failure ends the skip. Then the folder must be empty: what agy itself leaves there is unknown for 1.5.16, so nothing is allowed (`AGY_WORKSPACE_LEFTOVERS` is empty); anything found refuses `probe_folder_not_clean` ("refused: probe folder not empty"; the error gives a count, never the names) and agy is not started. The folder is never emptied by code: the owner looks at it and removes what is there. If a real run shows agy writing a fixed name there, that exact name is added to `AGY_WORKSPACE_LEFTOVERS` with the version and date. |
| Pager | `scarcity._agy_next_page`, `parse_agy_usage`, `agy_usage_gaps` | agy 1.5.16 shows `/usage` in a scrollable pager ("(1–17 of 30 lines)", ↑/↓, pgdown, ctrl+end Bottom). The probe presses ↓ (navigation only, never text) until every group is read, the footer shows the last line, or a press stops moving the view. Each painted view is parsed on its own (a window counts only under a group header shown above it in the same view) and views are merged per group and window. A reading needs both pools (`gemini`, `claude-gpt`), each with its week and five-hour window, and the pager read to its end; anything less is `partial-reading` ("quota screen read only in part"), an error, never a healthy reading. ↓ rather than pgdown / ctrl+end: stepping one line shows every group that fits in a view whole in some view; a page jump can land with a group's header just above the top. |
| Views | `usage._one_lane` / `live.subscriptions_section` (`state`), `core.subscription_readings` (`state`), `dashboard.html`, the cortados-pulse pane | The lane's state in words: "not configured", "not installed", "could not start", "not logged in", "sign-in not finished in time (agy still signing in)", "first-run setup pending", "refused: …". The pane prints it in place of "last sample FAILED". |
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
| `.gemini/antigravity-cli/settings.json` | a JSON object with only `trustedWorkspaces` (a list of strings). **Missing: `first-run-pending`** ("first-run setup pending"), not "unsafe": agy writes it on its first run. | The folders the owner trusted. Trust only skips agy's own prompt for a folder; it grants no tool and changes no billing. The owner trusts the probe's fixed folder here once by hand ("One-time owner step"); the router never writes this file. |
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

The launch folders are fixed: the probe runs in `$CORTADOS_HOME/agy-probe/workspace` (round 5; before it, a fresh
`p<pid>-<ms>` folder in `$CORTADOS_HOME/usage-probe`), and `agy models` in a fresh `agy-models-*` folder in
`$CORTADOS_HOME/usage-probe` (before round 3 it ran in a system temp folder). `prepare` checks the folder's walk; the
probe folder itself is checked again just before the start.

### One-time owner step: trust the probe folder (round 5)

agy shows a workspace-trust prompt the first time it opens a folder, and the probe never answers it. Once, on the
provider host (the machine whose `$CORTADOS_HOME` the sampler uses; `~/.cortados` unless set), as the user the provider
runs as:

```
mkdir -p ~/.cortados/agy-probe/workspace
chmod 700 ~/.cortados/agy-probe ~/.cortados/agy-probe/workspace
cd ~/.cortados/agy-probe/workspace
agy
```

Accept the trust prompt for this folder, then quit agy (`/quit`, or Ctrl+C twice). **Quit agy before samples run:**
while agy (or anything else, a shell included) has this folder as its current directory, every sample is skipped
(`probe_folder_busy`); the probe never kills it. `cd` out of the folder in that shell too. Then check the folder is still empty
(`ls -A ~/.cortados/agy-probe/workspace` prints nothing); if agy left anything there, remove it and tell the router's
maintainer the names, so they can be judged for `AGY_WORKSPACE_LEFTOVERS`. Until this is done every sample refuses
`workspace_not_trusted` and nothing is typed. If `$CORTADOS_HOME` is set, use `$CORTADOS_HOME/agy-probe/workspace`
instead of `~/.cortados/agy-probe/workspace`.

### Private text in errors, state and logs (round 3)

An error goes into the lane's record (`scarcity.json`), stderr and the views, so it holds fixed words only: unknown
setting keys are **counted** ("2 unknown setting(s) under 'plugins'"; the path shown holds schema keys only); names in
`forbidden-env` errors are shown only when they look like a variable name (`^[A-Za-z][A-Za-z0-9_.-]{0,40}$`), others
counted; a partial reading names pools (`claude-gpt: no 5-hour window`), not the group title as shown; the "no quota
groups" error gives line and footer counts.

### Subscription proof (`agy_isolation.check_subscription`)

With no auth mode in settings, the probe proves the login from what agy itself shows. The tier is read from the
**start-up banner** agy prints when it launches, which the probe already sees in the PTY before it types `/usage`
(`scarcity.tui_capture`'s `on_boot`; the banner text is kept in memory for this check only). Its account line is
`<email> (Google AI Pro)`:

```
                  Antigravity CLI 1.5.16
                  user@example.com (Google AI Pro)
                  Gemini 4 Neon (High)
                  ~/.cortados/agy-probe/workspace
```

The `/usage` "Models & Quota" box holds only an `Account: <email>` line, no tier. Fixture:
`tests/fixtures/agy-banner.txt` (email scrubbed). Read-only evidence; the account email is matched and dropped
(`scrub_account`) before anything is checked, and is never stored, logged or echoed. No error ever carries screen text
(round 3: the "no quota groups" error used to carry the screen's tail, and a private line survived the email scrub); it
reports counts only.

- Accepted: the parenthesised personal tier right after the account on the banner, `Google AI Pro` or `Google AI Ultra`
  (`TIER_PATTERN`: `<account> (Google AI (Pro|Ultra))` after the scrub); recorded as the reading's `tier` (`AI Pro` /
  `AI Ultra`).
- Refused (`subscription-unproven`, "refused: subscription login not proven"): the banner or the `/usage` answer names an
  API key, Vertex, enterprise, pay-as-you-go, Application Default Credentials / ADC, a Cloud project, a billing account or
  Code Assist (even next to a tier); or the banner shows no tier, or any other one (`Google AI Plus`, `Free`, …), or a
  tier not on the account line.
- Confirmed against the owner's agy 1.5.16 screens on 2026-11-24 (`TIER_PATTERN_CONFIRMED = True`). If a later agy changes
  the banner, set the flag `False` (the lane refuses again) until the pattern is pinned to the new line.

### Saved conversations
agy saves every interactive session: on start it prints "Resume with -c (or command below): agy --conversation=<uuid>".
The CLI reference (antigravity.google/docs/cli/reference, read 2026-11-24) lists `-c/--continue` and `--conversation`
but **no flag to start without saving**, so each `/usage` probe (every 30 min) may leave one empty saved conversation in
agy's own store. The probe's folder is kept empty (checked before every run); agy's store is not touched by the router. If a later agy adds a
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
4. ~~`~/.gemini/antigravity-cli/settings.json` key names~~: done 2026-11-24 (round 2 allowlists).
5. ~~The tier line~~: done 2026-11-24. It is on the start-up banner, not in the `/usage` box (Subscription proof above).

## Owner steps on the Air (updated 2026-11-24)

agy 1.5.16 is installed at `~/.local/bin/agy` and signed in (Google AI Pro). The installer no longer has
`--skip-aliases`: install with `bash /tmp/agy-install.sh` (`--skip-path` still exists). Remaining:

1. Add `--agy-bin /Users/<you>/.local/bin/agy` (absolute) to the provider's LaunchAgent arguments, then restart it.
   Until then the lane shows "not configured" and agy is never opened.
2. Make sure `~/.cortados/env` holds no `GEMINI_*`, `GOOGLE_*`, `AGY_*` or `CLOUDSDK_*` line (the lane refuses to run).
3. After the next 30-minute sample, the live view / pulse pane shows the Gemini and third-party quota, or the named
   reason it could not ("not logged in", "first-run setup pending", …).
   "refused: subscription login not proven" means the start-up banner showed no `(Google AI Pro)` / `(Google AI Ultra)`
   after the account, or a non-subscription login (Subscription proof above).

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
