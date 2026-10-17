# Quota and routing logic

The evaluation crash was a double-read bug. The cost expression first read the learned tokens-per-percentage-point estimate to check whether it existed, then read it again for division. The second read returned `None`, so Python attempted to divide a missing value by one million. A concurrent partial rewrite of the estimate file can produce this sequence; the historical writer responsible was not captured. Regression tests reproduce both the crash and the case where two different numeric readings silently change ranking. The repaired expression reads once per candidate and uses that same observation.

## Measured allowance and pace

The provider is the authority for allowance usage and reset timing. For each applicable account/pool/window:

- Remaining percentage = `100 - used_percentage`.
- Elapsed fraction = `clamp(1 - seconds_until_reset / window_duration_seconds, 0, 1)`.
- Projected usage at reset = `used_percentage / max(elapsed_fraction, 0.05)`.
- Allowance per hour = `remaining_percentage / max(hours_until_reset, 0.25)`.

The existing small denominators avoid unstable forecasts at the beginning or end of a window. Projection is a simple continuation-of-current-pace estimate, not a prediction of future workload. For example, 76% used / 60.4% elapsed projects to 126% by reset. Capacity that is lightly used and close to resetting becomes more attractive. A pool at 99% or more is treated as spent under the existing conservative threshold. Other scarcity thresholds and pool overflow behavior are unchanged by this repair.

Providers can expose overlapping windows and separate model pools. The router applies the existing pool mapping and roll-up: tightest pressure, with its existing underused-window preference when no window is tight. It does not average unrelated pools together. Cursor-model and other-model allowances are separate, with the existing documented overflow rule. Missing quota must remain unknown. Local compute has resource pressure, not a subscription percentage.

## Learned token estimates and relative selection cost

The existing learner compares successive quota readings with the usage ledger. For accepted intervals of the same pool, its estimate is `sum(recorded tokens) / sum(increase in used percentage points)`. It excludes resets/non-increasing readings, increases over 40 points, gaps over six hours, and intervals without attributed tokens. It uses the existing 14-day history. Estimated remaining tokens are `(100 - latest_used_percentage) * tokens_per_percentage_point`.

These estimates can be rough: ledger coverage, model mix, caching and provider accounting affect them. They are not an official token balance. The percentage reported by the provider remains separate from this learned conversion.

The current relative burn heuristic is `published_blended_price_per_million / (learned_tokens_per_percentage_point / 1_000_000)`. It is normalized across candidates. Unknown learned cost retains the existing price fallback. Selection cost then adds a scarcity penalty, subtracts an underused/expiring-capacity credit, and may subtract an absolute-capacity credit. Lower cost wins among candidates satisfying capability, fit, permissions and availability rules. These are ranking scores, not a dollar charge or an exact forecast of the next job's quota debit.

The current configured score is `weight + 30 * max(scarcity, 0) - 80 * max(-scarcity, 0) - capacity_credit`.
The largest measured burn among candidates is normalized to weight 100. A non-scarce pool may receive
up to 20 additional points of credit in proportion to its estimated remaining tokens. These existing
weights express a preference for using spare paid capacity before reset; they do not override task fit
or the fresh-quota refusal. Neither the coefficients nor the learner were retuned in this repair.

## Freshness policy requested for both systems

The approved 005 policy requires validated quota no more than 30 seconds old in both the MCP gateway and the legacy fleet. The limit applies to the source observation start, using both wall and monotonic clocks; wrapping an old reading in a fresh response does not refresh it. A cached routing decision is not permission to dispatch: the exact provider/model and matching authentication scope must pass the final quota check. Refresh failure, stale/malformed quota or an exhausted applicable pool must prevent that dispatch. A missing learned token-cost estimate is different from missing provider quota.

This boundary covers calls submitted by the gateway/fleet. It cannot independently intercept every internal inference made inside a running native Claude/Codex CLI session. Provider limits still govern those internal calls.

## Inspection command

The existing command owner is:

```sh
/opt/homebrew/bin/python3.14 /Users/velox/cortados/plugins/cortados-router/scarcity.py --force
/opt/homebrew/bin/python3.14 /Users/velox/cortados/plugins/cortados-router/scarcity.py --force --json
```

The Hermes tool is `subscription_usage` with `refresh: true`; `/router usage refresh` provides its chat command. Refresh actively reads provider usage and updates the existing usage cache/history; it does not request a model completion.

The corrected Codex source uses native structured quota metadata, including its actual window duration and reset epoch. The captured old TUI parser misidentified a preceding session UUID as a model and dropped the date after `AM`: it reported October 18 instead of October 22. This could reverse a cautious recommendation into an expiring-capacity recommendation. The TUI parser is retained for diagnostics, handles supported dates correctly and leaves invalid dates unknown. Historical captures are evidence of that bug, not current balances.

## Configured source and dispatch limits

A reading is bound to the configured CLI executable, effective home/config/auth-store selection and user ID. No tokens or account identities are copied into the scope hash. This proves matching configured sources within the freshness limit; it is not a permanent account-identity guarantee. Alternate API/auth selectors refuse unless that source can be verified.

The strict MCP Codex profile is supported. Legacy Hermes-native OAuth, legacy Codex invocations without the captured strict profile, and local executable routes without endpoint proof remain unknown and refuse. Planning preflight does not authorize a worker: actual constructed argv/environment and continuations are checked at dispatch. These limitations are explicit in [FRESH-QUOTA-005.md](FRESH-QUOTA-005.md).

## October 17 checkpoint recovery

The unchanged evaluator completed successfully against frozen inputs. Candidate and incumbent were graded on the same 610 labeled held-out requests (323 validation requests used for candidate calibration). The new adapter was rejected by the existing promotion gates:

| Metric | Candidate | Regraded incumbent | Gate |
|---|---:|---:|---|
| Routing-band accuracy | 64.1% | 64.9% | Fail |
| Under-routing | 13.8% | 6.6% | Fail |
| Outward-action recall | 92.9% | 88.1% | Pass |

The report was published to `train/runs/2026-10-17-0130/report.json` without changing `train/current.json`. The October 16 model remains active. Nightly scheduling remains 01:30 America/Toronto, with unchanged training and promotion gates. This recovery did not retrain or relabel the saved checkpoint.

## Latest verified operating observation

At 14:23 EDT on October 17, the diagnostic refresh reported Claude weekly 76% used / 24% left /
71.1% elapsed (projected 107%, cautious), and Codex weekly 47% used / 53% left / 35.1% elapsed
(projected 134%, avoid). Codex resets October 22 at 03:22 EDT; Claude resets October 19 at 15:00 EDT.
Cursor's displayed 100%/33% pool usage and Antigravity's 0% were retained from 13:53 EDT, so they
are not fresh admission evidence. These are timestamped observations, not permanent balances.

A separate strict installed-source check at 14:24 EDT admitted Codex. Claude/Antigravity metadata
was unavailable, Cursor process containment could not be proven, and the local transport endpoint
was unproven. These routes refused even where diagnostic status displayed a percentage. Existing
status stance text describes the scarcity heuristic; final admission overrides it when evidence is
unknown. Nightly training stays scheduled, but its teacher will pause if its quota cannot be verified.


## Source-repair observation, October 17 at 17:05 EDT

The common/Cursor repair is now installed after 695 scoped tests and independent D0/A0/B0.
Cursor uses the native billing-cycle reader rather than its interactive display. At 16:59 EDT it
reported 100% used in the Auto/Grok pool and 33.2222% used / 66.7778% left in the other-model pool,
with 22.4% of the cycle elapsed. The existing applicable overflow rule allows admission; it does not
authorize on-demand charges. Reset is November 10 at 17:32:35 EST. All balances are timestamped.

Codex strict MCP source was available. Claude actual brain/teacher and Antigravity sources still
refused as unknown. Claude's normal usage read again returned 429 at 17:00:54 EDT with a one-hour
Retry-After; no further read is eligible before 18:00:54 EDT. Native managed-policy source binding
also remains unresolved, independently of that response. Antigravity lacks a proven authenticated
quota-only source with complete reset data. Cached UI readings never override these refusals.

The 01:30 nightly remains enabled and can use 4,071 existing eligible labeled tasks; new Claude
teacher labels remain blocked. No new model was promoted. See
[FRESH-QUOTA-SOURCES-BUILD-REPORT.md](FRESH-QUOTA-SOURCES-BUILD-REPORT.md) for exact evidence and limits.
