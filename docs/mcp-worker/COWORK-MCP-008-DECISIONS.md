<!-- PURPOSE: Record remaining policy/evidence gates without author rulings.
     CONTRACT: RT-COWORK-MCP-008 v2 draft; same assignment, no self-approval.
     DEPENDS: Parent source inspection and independent root/QA evidence.
     CEILING: No global decisions, production, tests, registration or activation edits. -->
# Cowork 008 decision and gate record

Contract: RT-COWORK-MCP-008 v2. Group: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1.
Status: **draft; dispatchable=false**. Existing broad OAuth/tasks/file-transfer scope is authorized;
do not ask for it again. This document records technical proposals and honest unresolved evidence,
not Forest rulings. No provider registration/file-policy approval arrived in recorded docs during
this authoring run. [Root review](COWORK-MCP-008-ROOT-REVIEW.md) and independent provider-preflight.md /
auth-composition-result.json arrived during revision and were read. They supply source/synthetic
evidence, NOT D/A or product approval. Root/contract owner must record any later approval explicitly.

| ID | Current disposition | Owner / evidence needed | Gate location |
|---|---|---|---|
| O1 maintained auth | Candidate changed from tsidp to FastMCP3.4.9 GitHubProvider/OAuthProxy. Pinned composition and narrow resource/identity policy adapter specified in parent §2. No bespoke OAuth server. | Root selects/records GitHub registration and downstream client; independent QA qualifies actual supported flow, refresh, concurrency/restart, safe logs and identity-only permissions. | Registration choice and dependency review before package approval; live OAuth success before activation, not before deterministic task fixtures. |
| O2 native isolation | Concrete remote-only cowork-task-v1 profile proposed; persisted policy digest/CLI version required at job acceptance/bootstrap. Ordinary argv byte-for-byte unchanged. Shell capability review 13/13 passes only its bounded shell lane. | QA actual Codex exec model/tool capture: positive task work; denied credential/broker/sibling/tmp/symlink/network; native auth/quota and termination still work. :minimal runtime surface must be reported. | Local native acceptance before enabling remote execution; NOT a prerequisite to schema/store implementation. |
| O3 binary transport | Technical protocol fixed: raw PUT/GET with header-only capability, immutable content identity, no URL fetching; exact requests/results/errors/recovery in parent. | QA local binary hash/transaction exams; root actual Cowork header egress and download materialization. No query-ticket fallback or silent companion. | Deterministic local build/acceptance first, actual Cowork journey later. |
| O4 technical contract | RESOLVED AS DRAFT DESIGN: closed schemas, result projection, one owner, additive same-transaction task/job binding, canonical replay, recovery, source scopes and remote-policy identity specified. No dependency on live Cowork connection for local builder start after gates. | Independent D then A still required; author resolution is not approval. Any real source seam contradiction returns to root, never private builder change. | Technical freeze proposal ready for review; not dispatchable. |
| O5 size/retention | Pending user policy, already requested by root: 100000000 bytes/file, 1000000000/task, 10000000000/installation (decimal). Recommend keep until explicit delete; optional seven-day cleanup only if recorded approved. No 24-hour idle sweep. | Root records Forest response, backup/payload versus prompt/history distinction; QA pins technical budgets and measures disk-growth overshoot/headroom. | Policy record before approval; no unapproved cleanup or implicit startup defaults. |

## O1 facts and qualification boundary

Pinned FastMCP HEAD: 83c9764e0bc2d9d32910b82264c09370da24ab7a, tag v3.4.9.
Optional dependency proposal fastmcp-slim[server]==3.4.9; base MCP remains exactly 1.30.0.
Its metadata supports mcp>=1.24,<2; this is dependency-range evidence, not a successful install exam.

Maintained primitives exist: GitHubProvider constructor, OAuthProxy routes/JWTIssuer/token swap,
encrypted persistent FileTreeStore wrapper, code exchange and refresh rotation. Parent names precise
adapter seams for exact resource, required exp/use claims, numeric GitHub subject and downstream
client preservation. Scope defaults must be overridden: required_scopes=[] is supported, broad user
default is not accepted. read:user only if independently justified; never repo permission.
Verifier calls /user/repos for scope headers; identity-only GitHub App behavior remains a qualification
question. Raw GitHub tokens/PATs are not accepted MCP bearers.

Updated recorded root/QA evidence supersedes the initial no-refresh assumption: current GitHub OAuth
App docs support expiring tokens via offline_access. Parent specifies the inherited authorize-URL
hook to add that hint without requiring it as an API scope. OAuthProxy still does not synthesize
refresh: actual upstream fields, finite lifetimes, rotation and restart need qualification.
GitHub App user authorization remains an alternative, not a mandatory migration. No credentials/app have been created by this author.
Persistent storage is not proof of atomic refresh rotation: interruption between multiple writes,
parallel refresh and reauthentication behavior need independent tests. No cache or handwritten refresh
fallback closes that gap. GitHubProvider timeout_seconds concerns API verification; source inspection
does not establish every inherited token exchange meets Claude's 30-second refresh deadline.

tsidp is rejected as current candidate based on recorded optional/plain PKCE and supplied-secret
logging gaps. This is not permission to patch tsidp, deploy a proxy workaround or broaden credentials.
If GitHubProvider qualification fails, root records factual blockers and separately reviewed maintained
alternative; task/file schemas and ownership remain independent of provider choice.

## O5 policy choices versus technical guard proposals

File policy candidates above replace v1 MiB/GiB values. Effective limits are copied into each task's
canonical record and returned by API. No automatic policy change on replay or restart.
The 100-artifact, 300-second ticket, 65536-byte control/chunk, one upload/download per task, four stream
installation, 30-second idle/600-second total bounds are explicit proposals with two-sided reasons in
parent §6. They must be pinned by root/QA, not treated as performance measurements.

Stored accounting includes staging, input copies and mutable workspaces. Byte transfer reservations
can refuse exactly at their boundary. Worker filesystem growth monitoring is NOT a hard disk quota;
QA measures overshoot and root approves headroom/poll budgets or selects a qualified storage-quota
mechanism if strict physical ceilings are required. That distinction is an explicit resource gate,
not a hidden weakening of the promised task budget. No new OS component is authorized here.

Explicit task deletion does not remove append-only job/feedback/prompt evidence. Request tombstones
remain indefinitely in v1; no backup/history pruning inferred from payload retention. If full-history
erasure or backup TTL is required, root must freeze a separate policy before claiming it exists.

## Required recorded evidence, in order

1. Root provider-registration decision and file-policy response; dependency/source pins and package
   scopes reviewed. No need to repeat approval for general OAuth/tasks/files.
2. Designated independent QA authors/commits protected local fixtures and D=0 then A=0; root records
   approval for bounded implementation. Current three docs do not contain that approval.
3. One builder implements local deterministic seams; QA verifies all accumulated 001–007 suites and
   new fixtures, raw baseline/performance evidence, actual native exec profile and Rubric B.
4. Root handles qualified provider registration/activation with exact live scope and rollback, keeping
   private 443→8432. Cowork control calls plus PDF/DOCX/XLSX/non-UTF8 source and materialized output
   hash evidence complete the end-to-end gate. HTTP success or shell sandbox capture alone cannot pass it.

Current author evidence: local source/document reads and contract self-check only; no production/test
files, exams, provider calls, native canary, installation, credentials, commits or live routes changed.

Recorded supplements: root review keeps provider/file choices pending; independent provider-preflight.md
and auth-composition-result.json are source/synthetic evidence, not approval. Native prepare-review.json
has model_executed=false and native_exec_strict_parser_proven=false. Current baseline is 784/785 passing,
one historical 007 parity failure, not green; execution package records preservation requirements.
Candidate downstream client profile now explicitly disables public DCR/CIMD and uses maintained
public-PKCE pre-registration. Root must confirm actual Cowork registration UI compatibility; never
supply the GitHub upstream secret as a downstream secret.
