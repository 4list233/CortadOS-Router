<!-- PURPOSE: Record the task-specific Claude source proof still needed for 006.
     CONTRACT: Draft annex to RT-QUOTA-SOURCES-006; no implementation authority.
     CEILING: Same existing subscription credential, read-only quota, no model call. -->
---
contract_id: RT-QUOTA-SOURCES-006-CLAUDE-DRAFT
owner: Forest
author: /root/quota_repair_contract
status: evidence-incomplete-disabled
implements: [RT-QUOTA-SOURCES-006]
---

# Claude native source annex draft

This separate draft does not change the common/Cursor contract under independent review.
It grants no builder dispatch or Claude enablement. The existing strict refusal remains until
source facts, independent fixtures, D and A are complete. A successful HTTP response alone will
not close the actual-worker source-selection proof.

The proposed initial profile is `claude-2.1.302-teacher-oauth-env-v1`: the existing first-party subscription
credential selected by `CLAUDE_CODE_OAUTH_TOKEN`. It does not select a stored account, refresh a
credential, claim that an API key has subscription quota, or change the actual worker environment.
The first proof target is the existing nightly teacher invocation. Brain and ordinary PTY remain
disabled until separately proven; the table below records their eventual surfaces without enabling them.

## Proven native facts

Evidence below is private in `~/.cortados-builds/quota-verification-repair/`. Source excerpts come
from the installed native binary, not a guessed public API. The launcher `/opt/homebrew/bin/claude`
resolves to `/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, version
`2.1.302`, SHA-256 `5262ce9de86243fed0d21edc39b271555d56780d0112e2b194439fe1c6662d37`.
The release profile must freeze this executable chain and refuse changed or unrecognized versions.

| Evidence | Fact and limitation |
|---|---|
| `claude-source-excerpts.json` | Native `L0()` returns the exact nonempty OAuth environment token first, with `refreshToken: null` and `expiresAt: null`. Subscription type and rate-limit tier in that object come from environment metadata; they are not server entitlement proof. |
| `cli-auth-help.json` | Native status reports logged in, `oauth_token`, and `firstParty`, with subscription type null. This does not disclose the bearer or prove a fresh allowance. |
| `claude-source-excerpts.json` | Native `Ya()`/`wl()` also inspect bare mode, another auth token, API key/helper, file descriptors, profile selection, gateway/provider and host context. Finding an environment OAuth token is insufficient to prove actual model transport uses it. |
| `claude-native-auth-symbols.json`, `claude-provider-owner-exact.json` | Bare mode includes `CLAUDE_CODE_SIMPLE` or `--bare`. Provider selection checks gateway state first, then Bedrock, Foundry, Anthropic AWS, Anthropic Google Cloud, Mantle and Vertex selectors. Only the remaining branch is first party. The matched provider chunk imports are retained to distinguish repeated minified symbol names. |
| `claude-gateway-oauth-base-exact.json` | Gateway choice includes native host credential slots and host policy, not only one environment flag. The OAuth base owner has production/local/staging and custom-OAuth branches; the default URL cannot be assumed when those selectors are active. |
| `claude-source-excerpts.json` | Native usage path is `GET https://api.anthropic.com/api/oauth/usage` for the production first-party profile. The selected OAuth header is `Authorization: Bearer <existing token>` and `anthropic-beta: oauth-2025-04-20`; the usage request adds `Content-Type: application/json`. |
| `claude-source-excerpts.json` | Native `qm()` can refresh OAuth and retry following a 401. Calling that whole owner is incompatible with this read-only proof. The bounded collector must use only its captured read-only request protocol, with no refresh or retry branch. |
| `claude-usage-shape-excerpts.json`, `claude-native-panel.json` | The environment-token scope default is `user:inference`; the captured environment does not advertise `user:profile`. Native `qm()` requires both the subscription gate and profile scope before a live request. Declaring extra scopes would not grant them and is not authorized. |
| `claude-usage-native-schema.json` | Native response/cache schema includes nullable `utilization` and `resets_at` for `five_hour`, `seven_day`, and optional model/application windows. It also has `limits[]` and extra-usage fields. A nullable schema does not authorize filling in missing resets. |
| `claude-window-duration-exact.json` | Native window configuration explicitly associates `five_hour` with 18,000 seconds and `seven_day` with 604,800 seconds. This establishes duration semantics; the server must still supply each actual reset. Diagnostic fake-header reset construction in the same binary is excluded as quota evidence. |

The source schema also describes structured `/usage` output, but that output can contain cached
data. It is not a separate admission source and must not be treated as fresh just because the
command returned recently. The native one-token `quota_check` path is explicitly excluded: no
model invocation is authorized to discover quota.

## Actual worker source selection still to prove

The implementation must extend the existing `quota_sources.capture_source` / `read_source`
owner and the 006 `prepare_actual` / `check_actual` lifecycle. It must not introduce a second
collector, a status-only admission rule or a caller-provided authority registry.

Capture the immutable effective environment, exact executable, argv and cwd. Keep the original
environment only in private memory; include it and every proven settings generation in the
existing keyed equality material. No token, token digest, account identifier, scope claims or
settings values enter public metadata. Continue using `ActualSource.public_scope` and the closed
public `QuotaCheck`. Recheck generation before/after collection and at final dispatch; lost
private context refuses continuation. A fresh process must recapture and reprove its source.

`--strict-mcp-config` controls MCP loading, not authentication or settings. The teacher and brain
already use `--setting-sources project`; that excludes ordinary user/local settings but does not
disable managed policy. It must not be appended to a different worker merely to simplify proof.
The ordinary Claude PTY has a different argv/settings surface and requires its own proven
projection within this profile, or remains unknown.

The existing task-specific argv surfaces are:

| Caller | Existing shape to bind, without exposing prompt content |
|---|---|
| `train/label.py` teacher | `-p --output-format json --no-session-persistence --max-turns 1 --tools "" --disable-slash-commands --strict-mcp-config --setting-sources project --model <id> --system-prompt <text>`, cwd `~/.cortados/teacher-cwd`, input on stdin. |
| `runner.build_brain_argv` | `-p --verbose --output-format stream-json --no-session-persistence`, tools/max-turns selection, `--disable-slash-commands --strict-mcp-config --setting-sources project --system-prompt <text>`, optional model. The image variant permits Read and three turns; it must not accidentally select a different source. |
| `runner.build_argv` ordinary Claude worker | Permission-mode flags, optional model, append-system-prompt, and mode-specific print/output/session flags; exact accepted grammar and source projection remain to freeze. |

Prompt and session-id operand contents must not enter the child request, evidence or logs. The
private exact argv remains retained for dispatch equality. Unknown, duplicated or source-changing
flags refuse until their semantics are pinned. Model binding follows the common owner, not an
independent Claude routing rule.

`claude-settings-provider-excerpts.json` establishes settings sources and source-affecting flags,
but does not yet prove the full native load order and effective values for these three callers.
The native effective-settings schema orders user, project, local, flag and policy sources from
low to high. It supports settings `env`, `apiKeyHelper`, managed provider/gateway policy and
external policy helpers. Relevant path roots include `CLAUDE_CONFIG_DIR`, native global config,
`.claude/settings.json`, `.claude/settings.local.json`, and OS/remote/explicit managed sources.
Source proof must identify exact applicable paths, missing-path generations and native discovery
rules for each actual cwd. A helper or remotely changing policy that cannot be bound remains
unknown. Merely checking familiar JSON files is insufficient.

Further static evidence narrows the teacher proof:

- `claude-settings-root-discovery.json` pins `qpn`: allowed setting sources always gain flag and
  policy tiers. `Nz`/`Xpn` put project settings at the actual cwd or explicit project-config root;
  local settings may use an ownership-checked canonical Git root plus the legacy cwd file.
- `claude-settings-path-merge-exact.json` and `claude-settings-env-methods.json` pin the actual
  merge/application owner: filtered legacy global-config `env` is applied first, then filtered
  settings `env` in native source order. Capturing only the inherited environment is insufficient.
- `claude-remote-and-credential-gates.json` pins `IDr()`: a nonempty OAuth environment token makes
  `pc()` return false even if an Anthropic profile exists. This exact profile-selection exception
  is proven for this native version; it does not authorize reading or choosing profile credentials.
- `claude-refresh-env-token-branches.json` and `claude-refresh-owner-exact.json` pin `NWs` and
  `q0`: a local user-supplied env token with no remote-session ID or Unix socket is retained on
  401 instead of adopting a stored credential. `X0` returns the env-token owner directly and
  normal refresh returns `no_refresh_token`. An SDK refresh callback precedes the no-switch
  branch, so the plain-CLI callback absence still requires a startup-path proof.

Sanitized `claude-actual-settings-presence.json` and `claude-actual-secondary-presence.json` capture
presence/types only. The teacher/brain/repo project and local files are absent. The current user
settings have env/model fields but none of the examined auth/provider selectors; global config
has no env field. Managed file/drop-ins and both native macOS MDM plist paths are absent.
The remote policy cache exists with no env/auth/helper fields. The listed competing provider,
auth, SDK, remote-session and proxy selectors are absent. These observations do not prove all
unlisted selectors harmless and are not permission to bypass later policy changes.

**Unresolved teacher source boundary:** `claude-remote-eligibility-callers.json` shows that a token
with null locally reported subscription type remains eligible for remote policy. Native ordinary
CLI preAction launches `r3t()` independently of `--setting-sources project`
(`claude-remote-startup-path.json`). `claude-remote-filter-callers.json` shows that unverified cached
policy loses transport/auth environment controls, but a verified live payload bypasses that filter.
Consequently, a clean cached policy fingerprint does not yet prove that the native worker's later
startup fetch cannot select a different transport source. This is a concrete automatic-selection
gap. Do not silently disable remote policy, add host-managed auth flags, or treat its current empty
cache as an enduring absence. A positive quota payload will leave this gap open until the actual
native startup selection is proven within the existing dispatch boundary.

### Supported session-control option, not implementation authority

Static native schemas and handlers in `claude-session-status-protocol.json` and
`claude-session-status-handlers.json` expose two relevant controls. `get_settings` returns
effective settings, raw settings by source, applied model/effort and settings errors; its handler
is at native binary offset 249048823. It does not attest the final filtered process environment.
`get_status`, handled at offset 248997577, constructs status rows using the actual session's
credentials handle. Its formatted account/provider rows do not attest equality with a particular
bearer. Neither control presently closes the source-binding gap.

A separately reviewable design could investigate collecting authenticated source evidence from
the already-started native session before submitting its prompt. That would require a stream-JSON
control-session lifecycle and an exact credential-equality attestation that these captured schemas
do not yet provide. It changes the current teacher transport and the existing zero-start-on-refusal
boundary, so it is not an implicit 006 caller migration. The proposal must preserve managed policy,
prove that no model call occurs before admission, and undergo independent contract/exam review
before implementation. No supported fixed-credential control has been identified that preserves
the current teacher invocation and managed-policy semantics. Do not enable this profile by disabling
policy, adding an unproven host override, or using a probe-only environment change.

The initial profile must conservatively refuse competing auth/provider/endpoint selections until
the native owner proves them absent or ignored. Concrete selectors already established include
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, API-key/token descriptors, active profile selection,
`apiKeyHelper`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_UNIX_SOCKET`, custom/local/staging OAuth bases,
third-party provider flags and host-managed gateway state. This list is evidence, not a complete
allowlist. Additional source-affecting environment/settings controls must be discovered before
readiness. Do not strip selectors only from the probe.

## Bounded read and lifetime proposal

Once actual selection is proven, the existing isolated observation child performs one bounded
read using the retained bearer and captured native headers. It uses the existing startup/output/
termination bounds and identity-aware process owner. It does not run an interactive Claude TUI,
refresh, retry, follow redirects, consult a persisted quota cache, edit settings or trust, or
fall back to another credential. Standard verified TLS remains required. Native transport proxy,
certificate and endpoint semantics must match or the profile refuses; they are not silently
replaced with the diagnostic script's defaults.

Only a successful live response from this request can supply facts. Parent probe-start clocks
remain authoritative; reading completion cannot restamp data. Cache-age indicators or a response
known to come from a fallback are not fresh source evidence. The successful response's cache
headers and native request provenance must be frozen with its fixture before admission is enabled.

The native env-token object has no expiry or refresh credential. This does not prove a token
never expires, nor permit a fabricated JWT expiry. Proposed profile-specific lifetime is the
existing at-most-30-second observation lifetime, with token/settings equality rechecked at the
final boundary and a proven native no-switch path. This requires a reviewed extension of 006's
private proof interpretation: null `valid_until_epoch` is currently permitted only for Codex.
Do not silently use the Cursor JWT deadline rule or widen null handling for arbitrary profiles.
If native failure can switch to a stored account or API key, this proposal cannot authorize it;
that branch must be excluded by captured source facts before freeze.

The proposed pure parser seam is `quota_sources.claude_record(payload, *, observed_at)`. It will
reuse existing scarcity window/pool arithmetic and fixed malformed-source error
`WorkerError("quota_unknown", "Native quota data is invalid")`. Finite nonboolean percentages,
explicit future timezone-aware resets and positive native durations remain required. Exact
payload scale, model-window applicability, nullable/untouched behavior and pool mapping must be
frozen from a real successful payload plus native interpretation. Fields for extra usage,
overage, credits or enabled billing never authorize API spending. Missing required geometry is
unknown, including a zero-utilization window whose reset is absent. No `now + duration` fallback.

## Captured failure and cooldown

Two bounded diagnostic reads returned 429; neither produced quota authorization. The second
started at `2026-10-17T20:06:46Z`, returned native error type `rate_limit_error` and
`Retry-After: 3240`. Its response cache-control was private/no-store/no-cache/max-age=0.
The next normal diagnostic read is ineligible before **2026-10-17T21:00:46Z**.
`claude-native-usage-second.json` preserves only safe status/header/error fields; no arbitrary
body or credential was retained. No rate-limit limit/remaining/reset headers were recorded.

`claude-usage-cache-excerpts.json` shows the native 60-second snapshot and persisted fallback
up to one hour old. Both exceed the 30-second admission limit. The fast native TUI results
therefore establish display behavior only; their process containment was also unproven.
Native status plus cached usage cannot substitute for a successful read under this profile.

A 401, 403, 429, unavailable response or malformed payload invalidates prior authorization.
Honor Retry-After under the existing process-local observation owner and return typed unknown;
no retry loop, alternate endpoint or account, and no cached-success fallback. Process-local
coalescing cannot guarantee that independent fleet processes avoid upstream limits. This annex
does not add a central broker or promise continuous availability after one successful request.

## Independent readiness work

Before any Claude implementation dispatch, freeze the exact source manifest and these exams:

1. Real successful response from the same existing bearer, sanitized only for secrets and
   identifiers; captured status, request/header provenance, source clocks and actual reset fields.
2. Native selection parity for the initial teacher profile, including settings discovery,
   managed/host sources, endpoint/header behavior and no account/API fallback after an auth error.
   Brain and ordinary PTY remain unknown until their own selection parity is independently proven.
3. Scope/token/env/file-generation/cwd/argv changes; interpreter-origin attacks; no secret
   persistence; pre-read/post-read/final-boundary races; lost continuation context.
4. Independent response fixtures for all required and model-specific windows, nullable/zero/
   malformed/expired cases, percentage scale and exact reset parsing; extra usage never grants
   spending permission.
5. 429 Retry-After, 401/403, failed refresh and native cached-display refusal; source calls and
   model calls counted separately, with zero model calls and zero credential/settings writes.
6. Existing 006 lifecycle/clock/cache/containment exams, all 005 assertions, and installed caller
   coverage accumulated without weakening. Positive source evidence does not bypass D/A/B.

Remaining work is concrete source evidence, not a request for blanket account or billing approval.
If the existing credential cannot expose fresh complete subscription quota, report that limitation
and keep it unknown; do not invent authority to make nightly run.


## Subsequent bounded operator observation

After the prior cooldown, the single normal read at 2026-10-17 21:00:54 UTC again returned
429 / rate_limit_error with Retry-After 3600 and private/no-store/no-cache response headers.
No model call, refresh, retry or alternate source was used. Sanitized evidence is
`claude-native-usage-after-cooldown.json` in the same private evidence directory. The next read is
ineligible before 2026-10-17 22:00:54 UTC. This does not resolve the disabled source-binding proposal.
