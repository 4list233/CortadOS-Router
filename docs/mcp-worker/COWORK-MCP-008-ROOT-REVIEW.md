<!-- PURPOSE: Record planner evidence and unresolved choices for Cowork008.
     CONTRACT: RT-COWORK-MCP-008 draft; this record does not approve deployment.
     DEPENDS: Independent QA captures and the existing accepted001–007 package. -->
# Cowork008 planner review

Forest authorized OAuth, generated task folders, binary file transfer and existing subscription
workers on 2026-10-17. General tasks do not require a repository. Local scoped commits remain
authorized; no push or alteration of the shared checkout's Git state is authorized.

## Current evidence

- Draft author job `a21a86cf-d936-4be9-af76-ddec5048e479` completed under group
  `53f0ea39-a61e-4307-9c2c-466c1b4b0bd1`. Planner accepted the two requested draft documents,
  not an implementation or dispatch approval. Feedback ID `11947912-9f05-4687-92da-ceb9703fa959`.
- Revision author job `5c236b3c-ffc4-4526-ad03-dbf5da0ac957` completed under the same group with
  exact draft API/scope choices, separate prebuild and Cowork activation gates, and persisted native
  policy identity. Root read all three documents and accepted the draft-only deliverable through
  feedback `3f278224-bfa1-49c5-a3f2-318485c8ba0f`. It is not a dispatch or D/A approval.
- Independent native sandbox capability capture passed 13 assertions on Codex0.163.0. Task
  read/write succeeded; synthetic sibling/home/symlink/tmp read/write and loopback network failed.
  This is shell-subcommand evidence, not proof of actual model tool enforcement. Raw evidence:
  `/Users/velox/.cortados-builds/cowork-mcp-verification/native-sandbox-qa/review.json`.
  SHA-256 `8d7b5b53b04a87d32781b2be1e4ed320c2f9d2e47cb7431bcbdb7b3d246514a9`.
- Ordinary worker and training profiles must remain unchanged. A remote profile uses explicit
  `default_permissions` and permissions tables, omits legacy `-s`/`sandbox_mode`, and needs its own
  persisted identity and actual native execution capture. OS runtime read exceptions must be
  reported; a folder by itself is not read isolation.
- Independent AF_UNIX synthetic-socket denial passed separately; the original thirteen shell checks
  remain unchanged. Evidence: `native-sandbox-qa/unix-socket-result.json`, SHA-256
  `cf060e15be35d0a76c5c0c40687e6e8a99c16019b829a4ed3c7e8d69d84193a2` in the private verification root.

## Actual native execution capture

Root reviewed the independent preparation and ran exactly once:

```sh
/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017/venv/bin/python -B /Users/velox/.cortados-builds/cowork-mcp-verification/native-exec-capture.py --execute --config /Users/velox/.config/cortados-workers/config.json
```

Capture job `97417125-08c6-4df8-b916-5edac704a359` used native0.163.0, existing subscription,
fresh native quota admission and the remote-only profile. Native execution completed and the existing
watchdog proved containment. Raw events independently record successful task read/write and denied
localhost HTTP with a positive host control. Synthetic outside canaries stayed unchanged; no outside
write files appeared. The driver SHA-256 was
`956e914c9031842fdc37b2bac7b149dba85f0ac93cb6aac68a4fbfd0c8bae74e`.

The model-written receipt reports ten file-boundary denials, but the native JSONL omits those tool
events. Do not accept the receipt as independent proof of the ten attempts. That part of actual-exec
acceptance remains inconclusive. No independent native file-read tool was exposed, and no document
runtime was qualified. This evidence is separate from the direct sandbox thirteen-assertion pass.
No failed job was automatically retried. Raw capture is under
`/Users/velox/.cortados-builds/cowork-mcp-verification/native-exec-capture/`.

## Authentication qualification

Tailscale tsidp v0.0.17 at `972d97d49b6e44d2baf4786def7485f55a7b09c1` was examined and a
checksum-verified published binary staged for help-only inspection. It has not been enrolled or
started. The source permits optional/plain PKCE, logs a supplied secret in an error path, and
holds sessions in memory. It is not the selected production provider.

Current candidate: maintained FastMCP3.4.9 OAuthProxy/GitHubProvider, source at
`/Users/velox/.cortados-builds/cowork-mcp-research/fastmcp`, commit
`83c9764e0bc2d9d32910b82264c09370da24ab7a`. Its supported MCP range includes the existing1.30.0.
Remote installation dependencies require an explicit lock; existing installed releases stay intact.

Current [GitHub OAuth documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
supports expiring access tokens and refresh tokens through `offline_access`. That value is not an
ordinary API permission. The proxy only issues a refresh token if the actual upstream response has
one; a real refresh capture is required. No fabricated refresh or one-year fallback is acceptable.

The proposed login uses existing account `4list233`, publicly verified numeric ID `237812476`,
with no repository scopes. Forest's provider choice is pending. No login application or credential
has been created. GitHub authenticates the connector user; Codex/Claude subscriptions remain
separate Studio credentials.

Independent source review requires the thin adapter to enforce the configured numeric subject,
verified proxy OAuth-client identity, exact resource/issuer and mandatory temporal claims. The
provider's returned `AccessToken.client_id` is a GitHub user ID and its token field contains the
upstream secret: neither may be blindly forwarded as planner/task evidence. Only a sanitized local
principal crosses into broker calls. Disable arbitrary redirects/CIMD/public registration unless
the frozen contract explicitly needs them. Keep consent enabled, durable encrypted private auth
state, and exact hosted Claude callback. Exercise secret-sentinel error tests; library logs/error
bodies are not automatically safe. Full QA notes are in the private verification directory.

## File policy question pending

Forest was asked to choose initial100MB/file,1GB/task,10GB service storage, retaining files until
explicit deletion (recommended), or automatic cleanup seven days after completed work. These are
decimal bytes:100000000,1000000000,10000000000. No choice is inferred from elapsed time. Full
capacity refuses new transfers; it does not delete unrelated data. The author draft's automatic
24-hour/7-day retention proposal has not been approved.

## Accumulated baseline

Exact command at source4ed7508 (only draft docs added):

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/baseline-4ed7508
```

Result:785 tests,784 passing,1 failure,0 errors,0 skipped across26 lanes (including two benchmark
lanes). The suite is **not green**. The failure is the historical007 installed-parity test's whole
`labels.jsonl` hash: the separately authorized nightly run is appending training labels. Its other
immutable-file assertions passed. No008 production change exists at this baseline.

QA's preservation plan is to run unchanged007 assertions with explicitly recorded literal path
relocation to an exact frozen historical source/data fixture, preserving original label bytes and
hashes, the real007 installed release, and assertion/AST identity. Separately verify the live
pre-existing label prefix, held-out exam identities, and new rows' final training partition.
Do not mutate or weaken the protected007 exam, or freeze the entire live label file during the
authorized training run. Candidate tests must additionally prove008 ordinary-profile compatibility.

## Release gates

Independent committed local acceptance and D0/A0 precede production dispatch. Actual Cowork
OAuth/binary transfer is an activation-completion gate after implementation and local verification;
requiring that finished connection before implementation would be circular. No public endpoint,
auth service, task store or connector has been installed by this review. The existing private
Studio HTTPS443 route and active nightly remain separate.
