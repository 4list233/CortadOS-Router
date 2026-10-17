<!-- PURPOSE: Extend captured native compatibility and preserve truthful job provenance.
     CONTRACT: RT-MCP-NATIVE-002 v1 amends RT-MCP-001 v1's exact native-version clause.
     DEPENDS: Original protected exam; independent 0.163.0 native boundary corpus. -->
# Captured native profile extension

```yaml
contract_id: RT-MCP-NATIVE-002
contract_version: 1
owner: Forest
status: approved
implements: [RT-MCP-001]
depends_on: [RT-MCP-BUILD-001]
attempt_ledger: []
```

Forest authorized Claude Code installation using existing subscriptions after full-suite repair.
The installed Codex CLI is now `0.163.0`; the original contract accepted only captured `0.162.1`.
This amendment extends exactly that macOS profile boundary after fresh real captures. It does not
replace the authentication, quota, permissions, lifecycle, output, retry or model-selection contracts.

## Evidence and existing ownership

Search found native version admission at `integrations/mcp/native_metadata.py:17,137`, normal result
reporting at `integrations/mcp/codex.py:12,145`, and failure/recovery reporting at
`integrations/mcp/job_store.py:29`. All three currently hardcode `0.162.1`. Merely allowing `0.163.0`
would make job results falsely report the old version. The broker already owns immutable accepted
job records and the watchdog already owns the single native launch; those responsibilities remain.

Independent agent `/root/native_compatibility` captured the actual installed `0.163.0` with saved
ChatGPT login, the existing production argv/environment, all three allowed models, model-default
reasoning, and scratch-only tools. The frozen corpus is `acceptance/native_profile/fixtures/0.163.0/`.
It records six metadata methods and twelve per-model checks: command/final/usage plus folder/network
denials, granted MCP, 65,536-byte UTF-8 handoff, and SIGINT containment of the separate tool process.
An explicit proxy probe shows one granted tool, denied hidden calls, empty resource/prompt catalogs,
and denied resource reads/prompt gets. No hidden model reasoning content is exposed or claimed.

Two observer defects are retained with corrections: Luna's exact sentinel answer was compared with
the wrong fixed marker; the initial proxy observer expected a catalog error instead of the already
contracted empty catalog. Corrected checks use unchanged native bytes. The transient on-disk capture
script variant is retained and traced by hashes; it changed only the handoff observer assertion.
This is capture-observer repair, not a relaxed production boundary or a model retry.

## Fixed production behavior

- [FIXED] `integrations/mcp/native_profile.py` MUST become the sole owner of the captured version set
  `{"0.162.1", "0.163.0"}` and historical schema-1 default `"0.162.1"`.
- [FIXED] Exact successful native `--version` output MUST select the observed captured profile.
- [FIXED] Any other version, malformed output, nonzero version exit, or version-check timeout MUST
  refuse with `native_version_mismatch` before native task execution.
- [FIXED] The existing closed native environment and `startup_timeout_s` MUST bound version probes.
- [FIXED] NativeMetadata MUST retain the observed version on every successful snapshot refresh.
- [FIXED] The broker MUST persist that observed profile as a nonempty `cli_version` string in each
  newly accepted job record before authorizing START.
- [FIXED] The accepted record's `cli_version` MUST remain immutable for that attempt.
- [FIXED] The broker MUST pass the persisted `cli_version` to its watchdog bootstrap.
- [FIXED] Immediately before creating the native execution process, the watchdog MUST check the
  executable version again against the attempt's persisted value.
- [FIXED] A mismatch, including a change to the other accepted profile, MUST end that attempt with
  `native_version_mismatch` and zero native `exec` calls.
  This prevents an automatic CLI update from silently changing an admitted attempt's execution profile.
- [FIXED] Normal, failed, cancelled, timed-out and recovered results MUST report the attempt's
  persisted profile in `result.worker.cli_version`.
- [FIXED] Recovery MUST NOT substitute the currently installed CLI version for an older attempt.
  Otherwise an upgrade rewrites historical execution evidence that Claude receives for review.
- [FIXED] Existing schema-1 records with no `cli_version` MUST resolve to `0.162.1` through the sole
  profile owner; only that profile could create records in the previously accepted implementation.
- [FIXED] The SQLite schema version MUST remain 1; the added provenance field lives in the existing
  JSON record and requires no destructive migration, table rewrite, or identity change.
- [FIXED] The worker result envelope MUST retain its existing keys, types and nullability.
- [FIXED] The original three-model allowlist and all original protected tests MUST remain unchanged.

For a successful new-profile job, the existing worker object is, for example:

```json
{"provider":"codex-cli","requested_model":"gpt-6-luna","reported_model":null,"cli_version":"0.163.0","account_ref":"native-default"}
```

A `codex-cli 0.164.0` observation is an invalid example even if its tool shapes appear similar.
On a pre-exec mismatch no native worker ran; the failed result identifies the admitted profile and
its `native_version_mismatch` error rather than fabricating execution with the changed binary.

## Exact implementation scope

The builder MUST modify only these paths:

- `integrations/mcp/native_profile.py` (new; existing parent directory).
- `integrations/mcp/native_metadata.py` (extend version observation, adopt new owner).
- `integrations/mcp/codex.py` (remove version literal, report supplied attempt version).
- `integrations/mcp/job_store.py` (persist version and adopt historical-record owner).
- `integrations/mcp/broker.py` (bind observed admission version and pass immutable provenance).
- `integrations/mcp/worker.py` (enforce pre-exec version and pass result provenance).
- `cortados_router/worker_admission.py` (comment describing both captured profiles only).
- `tests/test_mcp_worker.py` (builder-owned implementation regressions; preserve existing cases).

Disposition: extend NativeMetadata and broker/watchdog/store responsibilities; consolidate the three
duplicated version facts into the one profile owner. Retain all public MCP APIs, request identity,
database format and original compatible profile. No replacement model scorer or quota owner is added.
All original and new `acceptance/`, contract, fixture and review files are read-only to the builder.
All other source paths, live state, installation files, global decisions and native account files are
excluded. Source-size limits and required headers/reasons remain those of the approved build package.
Root owns final documentation, actual installation, source inventory and independent verification.

## Acceptance and dispatch gate

| ID | Required ground truth | Independent command |
|---|---|---|
| AC-N01 | Both exact captured versions succeed and report themselves; a refreshed snapshot may select the newly observed captured profile for a new job. | Native-profile unittest command. |
| AC-N02 | Failed, cancelled and timed-out 0.163.0 jobs report 0.163.0 with existing terminal/error semantics. | Native-profile unittest command. |
| AC-N03 | Interrupted 0.163.0 job still reports 0.163.0 after restart under 0.162.1; old schema-1 record reports 0.162.1; replay performs no new exec. | Native-profile unittest command. |
| AC-N04 | Unknown version refuses before acceptance; captured-version drift between snapshot and START gives a failed job and zero native exec. | Native-profile unittest command. |
| AC-N05 | All model/kind captures retain hashed real native bytes, terminal/usage agreement, exact handoff sentinels, permission denials and observed child exit. | Native-profile fixture-ground-truth case and capture manifest. |
| AC-N06 | Original 53-case exam and complete accumulated suites pass; current installed wheel executes outside source checkout and reports 0.163.0. | Root's full-inventory runner and real installed-package smoke probe. |

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest \
  discover -s acceptance/native_profile -t . -v
shasum -a 256 -c acceptance/native_profile/SHA256SUMS
```

The independent exam adds eight public-MCP version/provenance cases and one captured-byte ground-truth
case. The original native double remains unchanged; a small exam wrapper sequences only `--version`
fault inputs and delegates all other mechanics to that owner. Real compatibility comes from the new
native captures, not from relabeling old wire fixtures in the synthetic orchestration tests.

Independent reviewer: `/root/mcp_qa_followup`. Root reviewed the exact source scope and public tests.
**D = 0. A = 0 at the commit containing this amendment, captures and protected manifest.** The two
original observer mistakes and their evidence corrections are disclosed; no unresolved behavior or
capture gap remains. The unchanged parent diagrams still describe the flow, with a version observation
inside admission and immediately before the existing watchdog's native spawn. No new store/lifecycle
or wire field is added. Existing benchmark obligations apply to the extra native version probe;
candidate overhead must be measured, not assumed negligible.

Dispatch requires that commit. Root independently runs B; the package does not self-certify a build.
