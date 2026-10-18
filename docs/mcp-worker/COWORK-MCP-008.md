<!-- PURPOSE: Specify the remote Cowork boundary without authorizing implementation or activation.
     CONTRACT: RT-COWORK-MCP-008 v3, draft; same assignment and independent gates.
     DEPENDS: RT-MCP-001–007; maintained FastMCP 3.4.9 auth and MCP 1.30.0.
     CEILING: Contract authoring only. No production, exams, credentials, commits or live changes. -->
# Cowork remote MCP implementation contract

```yaml
contract_id: RT-COWORK-MCP-008
contract_version: 3
status: draft
owner: Forest
author_role: contract author
implements: [CLAUDE-APP-CONNECTOR-PLAN.md]
depends_on: [RT-MCP-001]
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
dispatchable: false
independent_review: pending designated QA D then A
attempt_ledger: inherited; see DECISIONS evidence ledger (never reset)
```

[FIXED] v3 defines one independently gateable **LOCAL CORE** package: closed schemas, mandatory
configuration, synthetic-principal composition, task ownership/folders, immutable artifacts, raw binary
transfer, durable replay and existing broker/native seams. OAuth, general tasks without repositories,
automatic folders, binary transfer and existing subscription workers remain authorized scope.
Actual authentication adapter and public service are a separate **FUTURE AUTH / ACTIVATION** package.
No listener, remote CLI entrypoint, credential lookup, auth registration or automatic cleanup belongs
to LOCAL CORE. A Python-constructed ASGI test application is not a deployable delivery.
All required operator values MUST be supplied explicitly; absent/unknown values MUST refuse.
Synthetic explicit_delete fixtures MUST NOT select Forest's live retention policy.
Independent D then A and root approval are still pending; this revision declares neither pass.
See [decisions](COWORK-MCP-008-DECISIONS.md) and [execution packages](COWORK-MCP-008-EXECUTION.md).

## 1. Authority and source evidence

Read `/Users/velox/cortados/AGENTS.md`, `contracts/CONVENTIONS.md`, the D-0001/D-0084 rules in
`/Users/velox/cortados/docs/DECISIONS.md`, and pinned standards `CONVENTIONS.md`, `RUBRIC.md`,
`ORCHESTRATION.md`, `PLAYBOOK.md`, `standards/00-ENGINEERING-STANDARD.md` under
`/Users/velox/cortados/context-staging/research/ai-coding-std-workflow` (overlay pin
`ab1b521047dcabc1b610caa157e5dfbd544f589b`). No global ruling is added here.
Historical v2 source HEAD: `4ed7508fa15acf66ece3f100510728b76f1966ce`; v1 drafts were
untracked then. Current v3 source: `d4c99dbfb065813e61cdaed6c4a86e9edce73ca6`; reviewed v2 parent
SHA-256: `1bfb6e6e2a162415c53811187a85ca9a22099a4189c6d73c67e9c23e0e0661d8`.
No worker MCP is exposed now. Historical fallback delegation failed before starting; this revision
makes no fallback model call.
The author inspected local sources directly. No provider/model call or exam was run by this author. Concurrent root/QA evidence is attributed below, not counted as this author's verification.

| Source | Adopt/extend; invariant |
|---|---|
| `integrations/mcp/server.py::tool_schema,serve` | Adopt lowlevel SDK server and result encoding; extract registration only if needed. Stdio input/output schemas stay unchanged. |
| `schemas.py::validate,canonical` | Adopt local validation and canonical encoder; remote validator rejects paths and supplies trusted cwd only inside broker. |
| `broker.py::dispatch,admit_launch,recover,run_worker` | Extend dispatch/atomic acceptance seams. Existing queue, fresh launch admission, watchdog and interruption rules remain the sole owners. |
| `job_store.py::accept,replay,get,feedback` | Extend same SQLite transaction owner. Current get(job_id) is unscoped: remote operations MUST use owner/task predicates. No HTTP-owned database writer. |
| `codex.py::native_arguments,native_environment`; `worker.py::Watchdog` | Extend with explicit persisted remote policy branch. Ordinary argv/environment behavior remains byte-for-byte. |
| `native_profile.py`, `native_process.py` | Adopt captured version and termination proofs; independently capture remote profile identity, never reinterpret local capture. |
| `cortados_router/task_evidence.py`, `route_task.py`, `worker_admission.py` | Adopt evidence projection, S1/explicit routing, native subscription and strict fresh quota. No API billing fallback, second router or grant inheritance. |
| `pyproject.toml` | Keep base mcp==1.30.0. Optional remote = ["fastmcp-slim[server]==3.4.9"] only; record transitive lock in independent packaging evidence. |

FastMCP checkout `/Users/velox/.cortados-builds/cowork-mcp-research/fastmcp`, tag v3.4.9,
HEAD `83c9764e0bc2d9d32910b82264c09370da24ab7a`. Paths below are relative to
`fastmcp_slim/fastmcp/`. Its package metadata's server extra includes authlib>=1.6.11,
joserfc>=1.1.0, py-key-value-aio[filetree,keyring,memory]>=0.4.4,<0.5.0, uvicorn>=0.35,
and the mcp extra (mcp>=1.24,<2; starlette>=1.0.1). This is compatible in declared range with
MCP 1.30, not proof that the resolved environment passes. No FastMCP tasks extra or second task queue.

Root-supplied official Claude auth evidence: <https://claude.com/docs/connectors/building/authentication>:
401 challenge with resource metadata; exact resource; S256; hosted callback
`https://claude.ai/api/mcp/auth_callback`; user dialog can supply client ID/secret; normal calls have
10-second and refresh 30-second client deadlines. These are inherited observations, not a fresh live
check. Existing private 443→8432 route must remain intact; public 8443 is an unactivated candidate.

## 2. FUTURE AUTH — blocked candidate composition (AC-008-01–04)

[ESCALATE: future package only] LOCAL CORE MUST NOT implement this section. Retained candidates
are source evidence, not an approved adapter. D-008-01/02 remain future-auth blockers. Independent QA
MUST qualify the preparse hook and complete lock boundary before future-auth D/A review.

Replace tsidp as candidate: its inspected PKCE/logging gaps remain disqualifying. Do not fork an OAuth
server or implement code/refresh issuance locally. Use GitHubProvider (maintained OAuthProxy),
SDK 1.30 `StreamableHTTPSessionManager(app=lowlevel_server, json_response=True, stateless=True)`
and Starlette routes. Transport sessions never own job lifetime. Run one ASGI process; no multiworker
refresh/store races. Listener loopback only, trusted ingress Host/Origin allowlists; forwarded identity
headers never authenticate. Authentication covers every /mcp method, including initialization/listing.
Discovery/OAuth endpoints have protocol-required public access; transfer endpoints use §5 tickets.

Construction contract (symbols, not a runnable deployment configuration):

1. `GitHubProvider(client_id=operator_client_id, client_secret=resolved_secret,
   base_url=PUBLIC_ORIGIN, resource_base_url=PUBLIC_ORIGIN, issuer_url=PUBLIC_ORIGIN,
   redirect_path="/auth/callback", required_scopes=[], cache_ttl_seconds=None,
   allowed_client_redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
   client_storage=encrypted_store, jwt_signing_key=stable_signing_key,
   require_authorization_consent=True, forward_resource=False, enable_cimd=False)`.
   PUBLIC_ORIGIN has scheme/host/port and no path/query/fragment; resource is exactly origin + /mcp.
   PUBLIC_ISSUER is str(provider.base_url), including Pydantic's root trailing slash; pin this exact
   value in metadata, JWT checks and owner identity. Do not compare issuer to a slashless origin.
   Empty scopes are explicitly supported (github.py constructor tests is not None); never use its
   default broad user or request repo. If qualified registration requires read:user, root records
   that minimal change. GitHub App permissions differ from OAuth scopes; empty OAuth scope neither
   grants repository permissions nor proves GitHub App compatibility.
2. Explicit encrypted persistent client_storage: `FileTreeStore(data_directory=AUTH_ROOT,
   key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(AUTH_ROOT),
   collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(AUTH_ROOT))`, wrapped by
   `FernetEncryptionWrapper(key_value=store, fernet=Fernet(stable_storage_key),
   raise_on_decryption_error=True)`. Use maintained key-value classes imported by proxy.py:35–43.
   Operator provisions stable independent secrets and private 0700 directory outside task roots;
   never derive secrets from caller input. Startup storage/key failure refuses auth. Default proxy
   filetree exists (proxy.py:514–542) but treats decryption failure as cache miss; explicit store makes
   loss observable. Backup clients, transactions, codes, upstream tokens, JTI and refresh mappings
   consistently with keys; restore gate verifies canary records before serving. No memory store.
3. Call `provider.get_routes(mcp_path="/mcp")` once before serving; it initializes resource/JWT issuer.
   Mount returned maintained OAuth/discovery routes at origin. Build the equivalent of
   provider.get_middleware() with the policy adapter as BearerAuthBackend verifier, followed by
   AuthContextMiddleware; require authenticated context at /mcp before manager.handle_request.
   Run manager.run() in ASGI lifespan. Preserve SDK protected-resource 401 challenge using
   build_resource_metadata_url(resource). Do not mount a second FastMCP tool server.
4. Adapter delegates signature verification to `provider.jwt_issuer.verify_token(bearer)` and live
   token lookup/verification to `provider.load_access_token(bearer)`. Require explicit token_use=access,
   integer exp > now, integer iat <= now (bool rejected), valid optional nbf <= now, exact
   iss=PUBLIC_ISSUER and scalar exact
   aud=PUBLIC_ORIGIN + "/mcp", nonempty downstream client_id and jti. JWTIssuer source checks
   signature/issuer/audience/use but permits missing exp and default use; adapter tightens claims,
   it does not mint tokens. Reject raw GitHub/PAT/ID/refresh tokens.
   Preserve downstream client identity from verified JWT, not GitHub's returned AccessToken.client_id
   (numeric GitHub ID). Require live verified claims.sub equals operator-pinned decimal GitHub user ID,
   not mutable login. Return copied SDK access token with downstream client ID, resource, proxy expiry
   and canonical owner claims; never expose upstream token/user JSON to tools.
   Owner is (proxy_issuer, github_numeric_id, installation_id); downstream client must be registered
   and enabled by local installation policy. One Forest principal only; client rollover cannot change owner.
5. Thin ingress policy checks /authorize parameters before maintained handler: exactly one resource
   equal to canonical resource, exactly one S256 method and valid challenge. Reject missing/plain/
   duplicate parameters. /token code and refresh exchanges require exact resource, without
   normalization of path/port/query/trailing slash. Maintained handlers still verify code, client,
   redirect and verifier. proxy.py:935–949 normalizes some URLs, so its check alone is insufficient.
   Bound forms with control_bytes. Callback allowlist comparison is literal, at registration and
   authorization. Disable CIMD to avoid arbitrary metadata fetch. Candidate registration profile:
   pre-registered public PKCE. Set provider.client_registration_options.enabled=False before get_routes
   and do not mount /register. Root provisions a distinct downstream client through maintained
   register_client with exact hosted redirect and token_endpoint_auth_method=none; this method stores
   a public ProxyDCRClient and discards any supplied secret. Never give Claude the upstream GitHub secret.
   Adapter allows only the recorded downstream ID at /authorize, /token and resource verification,
   so the provider's synthetic upstream-ID client cannot bypass policy. Root's registration choice
   remains pending; if actual Cowork requires confidential credentials or DCR, amend and qualify this
   profile rather than claiming register_client preserves a confidential client. No public DCR default.
6. No positive auth cache. GitHubTokenVerifier calls /user AND /user/repos (github.py:100–145) for scopes;
   the latter call is a qualification concern, not repository permission requested by this contract.
   It must work with identity-only permissions. Upstream outage refuses; no cached-validity fallback.
   Owner disable denies new MCP/ticket/stream calls and revokes tickets. Accepted jobs keep existing
   lifetime, with local operator cancel available. Token expiry/disconnect never retries/cancels jobs.
   Logs omit tokens, secrets, query credentials, bodies and provider error text. QA source review
   identifies error-level raw code/exception logging and reflected provider errors at proxy.py:1100,
   1479,2328–2331. Debug-off is insufficient. remote_auth must install dependency-log suppression
   for auth/http client namespaces (including exception tracebacks), emit only allowlisted local
   event codes, and replace OAuth error bodies/descriptions with fixed safe messages before send.
   Callback HTML 4xx/5xx, token JSON error_description and error redirects all need this boundary;
   successful protocol fields remain unchanged. Secret-sentinel tests are mandatory. If interception
   misses a provider-controlled error path, auth activation is BLOCKED; no claim defaults are safe.

**Updated root/QA evidence (received during revision):** [root review](COWORK-MCP-008-ROOT-REVIEW.md)
and private provider-preflight.md report current official GitHub OAuth App documentation supports
expiring access/refresh via offline_access:
<https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps>.
This supersedes the initial categorical no-refresh assumption. The source fact remains:
proxy.py:1125,1233–1268 emits downstream refresh ONLY when actual upstream response supplies it;
:1462 refuses otherwise. Never synthesize refresh or accept the implicit one-year lifetime fallback.
GitHub App expiring user authorization remains an alternative, not a required migration.

Minimal source-supported OAuth App composition: subclass GitHubProvider only in remote_auth.py;
override _build_upstream_authorize_url(txn_id,transaction), copy the transaction mapping, set its
scopes to the reviewed identity scopes plus offline_access, and return super's method on the copy.
consent.py:277–311 builds the entire state/PKCE/redirect URL; do not duplicate that protocol.
Keep required_scopes=[] (or separately justified read:user) in the verifier: offline_access is not an
API scope and must NOT be required in X-OAuth-Scopes. Never modify persisted downstream scopes for
this hint. The pinned inherited hook is an explicit adapter seam with regression capture, not an
upstream constructor argument (GitHubProvider does not expose extra_authorize_params).
Root still must choose/register the application and QA capture actual expiry field names and token
rotation. If upstream lacks finite positive token lifetimes or refresh_token, activation MUST refuse.
**D-008-01:** _extract_upstream_claims MUST NOT enforce lifetimes: initial/explicit refresh
parsing/storage precede it; transparent refresh never calls it. Candidate seam: override
_create_upstream_oauth_client, call super, register Authlib access_token_response and
refresh_token_response compliance hooks before parsing. QA MUST qualify all three paths.
Successful responses MUST require nonempty access/refresh tokens, reviewed bearer type and finite
positive typed durations (bool forbidden). Normalize refresh_token_expires_in to refresh_expires_in;
reject conflicting aliases. Valid upstream OAuth errors need a separate fixed-safe-error branch.
No invented TTL or durable update on invalid response is permissible.
**D-008-02:** candidate process-wide async auth gate MUST span callback/token credential loading,
exchange, store/delete and resource verification that may transparently refresh. Acquire once at the
outer request boundary before credential load; internal provider calls MUST NOT reacquire it.
No task/DB lock may be held while acquiring auth gate; release before broker dispatch.
Locking exchange_refresh_token alone is too late. QA MUST qualify exact maintained handler seams,
parallel redemption, transparent/explicit races, interruption and restart in future-auth local A,
before auth build dispatch. Crash loss MUST fail closed into reauthentication; no claimed atomic
refresh-family reuse detection. These candidate seams still require independent QA qualification.
No claim this refresh path has been exercised. Proxy rotates downstream refresh metadata (:1642–1654), but
crash/concurrent reuse is not proven atomic by filetree writes; qualify one-process serialization and
fail-closed loss behavior. No replacement refresh engine. GitHubProvider lacks a dedicated refresh
timeout argument; qualify inherited HTTP calls against Claude's 30-second deadline. No claim of
immediate upstream session revocation or refresh-family reuse revocation without evidence.
These gaps can block auth activation without blocking deterministic task work.

## 3. Closed remote wire contract (AC-008-05–08)

Only tools in the operation table are registered remotely. No caller cwd, paths, account home, policy
selector, native overrides or grants. Control JSON is UTF-8, no duplicate keys, NaN or surrogates;
serialized request/response ≤ operator control_bytes (proposed 65536, aligned to captured handoff).
No truncation: oversized result returns output_limit while canonical result remains local.
UUIDs are lowercase RFC4122 v4; reject other spellings before hashing. Strings retain exact bytes:
no trimming/Unicode normalization. Nonblank predicates supplement schema. Filename MUST be a nonblank
UTF-8 leaf of 1–255 encoded bytes; it MUST NOT equal dot or dot-dot or contain slash, backslash,
U+0000–001F, U+007F–009F or surrogates. Storage uses opaque IDs. The 255-byte ceiling bounds portable
filesystem leaf names; smaller rejects ordinary names and larger may fail workspace materialization.
Unicode and punctuation MUST be preserved as metadata rather than silently renamed. Media type is
descriptive only. These are AC-008-05,10,12 requirements.

The following normative JSON Schema 2020-12 defines requests and results. Resolve each named $defs
against this document. All objects are closed; optional fields are those omitted from required.
Each operation result is oneOf(its success, Failure). MCP emits matching structuredContent and JSON
text, with isError = !ok. Semantic conditions after the schema also apply. Stdio is unchanged.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:cortados:RT-COWORK-MCP-008:v3",
  "$defs": {
    "ID": {"type":"string","pattern":"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "Hash": {"type":"string","pattern":"^[0-9a-f]{64}$"},
    "Name": {"type":"string","minLength":1,"maxLength":255},
    "Media": {"type":"string","maxLength":127,"pattern":"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"},
    "Selection": {"oneOf":[{"type":"object","properties":{"mode":{"const":"explicit"},"model":{"type":"string","minLength":1}},"required":["mode","model"],"additionalProperties":false},{"type":"object","properties":{"mode":{"const":"s1"},"routing_brief":{"type":"string"}},"required":["mode"],"additionalProperties":false}]},
    "Constraints": {"type":"object","properties":{"allowed_models":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true},"min_band":{"enum":["R0","R1","R2","R3","R4"]}},"required":[],"additionalProperties":false},
    "Permissions": {"type":"object","properties":{"network":{"const":false},"mcp_servers":{"type":"array","maxItems":0}},"required":[],"additionalProperties":false},
    "Reference": {"type":"object","properties":{"source":{"type":"string","minLength":1},"text":{"type":"string","minLength":1}},"required":["source","text"],"additionalProperties":false},
    "Evidence": {"type":"object","properties":{"schema":{"const":"cortados.mcp-task-evidence.v1"},"task_group_id":{"$ref":"#/$defs/ID"},"components":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true},"dependencies":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true},"constraints":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true},"uncertainties":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true},"verification":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100,"uniqueItems":true},"references":{"type":"array","items":{"$ref":"#/$defs/Reference"},"maxItems":100}},"required":["schema","task_group_id"],"additionalProperties":false},
    "Create": {"type":"object","properties":{"request_id":{"$ref":"#/$defs/ID"},"task_group_id":{"$ref":"#/$defs/ID"},"title":{"type":"string","maxLength":200}},"required":["request_id","task_group_id"],"additionalProperties":false},
    "TaskKey": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"}},"required":["task_id"],"additionalProperties":false},
    "Delete": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"request_id":{"$ref":"#/$defs/ID"}},"required":["task_id","request_id"],"additionalProperties":false},
    "Context": {"type":"object","properties":{"task":{"type":"string","minLength":1},"context":{"type":"string"},"selection":{"$ref":"#/$defs/Selection"},"task_evidence":{"$ref":"#/$defs/Evidence"}},"required":["task","selection"],"additionalProperties":false},
    "Route": {"type":"object","properties":{"task":{"type":"string","minLength":1},"context":{"type":"string"},"selection":{"$ref":"#/$defs/Selection"},"task_evidence":{"$ref":"#/$defs/Evidence"},"task_id":{"$ref":"#/$defs/ID"},"constraints":{"$ref":"#/$defs/Constraints"},"permissions":{"$ref":"#/$defs/Permissions"},"account_ref":{"const":"native-default"}},"required":["task","selection","task_evidence","task_id"],"additionalProperties":false},
    "Delegate": {"type":"object","properties":{"task":{"type":"string","minLength":1},"context":{"type":"string"},"selection":{"$ref":"#/$defs/Selection"},"task_evidence":{"$ref":"#/$defs/Evidence"},"task_id":{"$ref":"#/$defs/ID"},"constraints":{"$ref":"#/$defs/Constraints"},"permissions":{"$ref":"#/$defs/Permissions"},"account_ref":{"const":"native-default"},"request_id":{"$ref":"#/$defs/ID"},"timeout_s":{"type":"integer","minimum":1},"input_artifact_ids":{"type":"array","items":{"$ref":"#/$defs/ID"},"maxItems":100,"uniqueItems":true}},"required":["task","selection","task_evidence","task_id","request_id","timeout_s","input_artifact_ids"],"additionalProperties":false},
    "JobKey": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"job_id":{"$ref":"#/$defs/ID"}},"required":["task_id","job_id"],"additionalProperties":false},
    "Check": {"type":"object","properties":{"kind":{"enum":["command","artifact","review"]},"reference":{"type":"string","minLength":1},"claim":{"enum":["passed","failed","unknown"]}},"required":["kind","reference","claim"],"additionalProperties":false},
    "Feedback": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"job_id":{"$ref":"#/$defs/ID"},"feedback_id":{"$ref":"#/$defs/ID"},"verdict":{"enum":["accepted","rejected","inconclusive"]},"summary":{"type":"string","minLength":1},"checks":{"type":"array","items":{"$ref":"#/$defs/Check"},"maxItems":100}},"required":["task_id","job_id","feedback_id","verdict","summary","checks"],"additionalProperties":false},
    "Upload": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"request_id":{"$ref":"#/$defs/ID"},"artifact_id":{"anyOf":[{"$ref":"#/$defs/ID"},{"type":"null"}]},"filename":{"$ref":"#/$defs/Name"},"bytes":{"type":"integer","minimum":0},"sha256":{"$ref":"#/$defs/Hash"},"media_type":{"$ref":"#/$defs/Media"}},"required":["task_id","request_id","artifact_id","filename","bytes","sha256","media_type"],"additionalProperties":false},
    "Download": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"request_id":{"$ref":"#/$defs/ID"},"artifact_id":{"$ref":"#/$defs/ID"}},"required":["task_id","request_id","artifact_id"],"additionalProperties":false},
    "Error": {"type":"object","properties":{"code":{"enum":["invalid_request","idempotency_conflict","model_unavailable","account_unavailable","subscription_required","quota_unknown","quota_spent","scorer_unavailable","scoring_context_too_large","worker_context_too_large","needs_clarification","permission_refused","connector_unavailable","workspace_busy","capacity_exhausted","job_not_found","storage_error","native_version_mismatch","native_protocol_error","native_failed","output_limit","timeout","cancel_requested","broker_interrupted","containment_failed","job_not_terminal","task_not_found","artifact_not_found","task_deleted","task_quarantined","upload_incomplete","transfer_busy","ticket_invalid","ticket_expired","ticket_consumed","length_mismatch","hash_mismatch","artifact_corrupt","policy_unavailable","publication_failed"]},"message":{"type":"string","minLength":1}},"required":["code","message"],"additionalProperties":false},
    "Failure": {"type":"object","properties":{"ok":{"const":false},"error":{"$ref":"#/$defs/Error"}},"required":["ok","error"],"additionalProperties":false},
    "Limits": {"type":"object","properties":{"file_bytes":{"type":"integer","minimum":1},"task_bytes":{"type":"integer","minimum":1},"installation_bytes":{"type":"integer","minimum":1},"artifacts_per_task":{"type":"integer","minimum":1,"maximum":100},"ticket_ttl_s":{"type":"integer","minimum":1},"control_bytes":{"type":"integer","minimum":1},"retention":{"const":"explicit_delete"}},"required":["file_bytes","task_bytes","installation_bytes","artifacts_per_task","ticket_ttl_s","control_bytes","retention"],"additionalProperties":false},
    "Artifact": {"type":"object","properties":{"artifact_id":{"$ref":"#/$defs/ID"},"filename":{"$ref":"#/$defs/Name"},"media_type":{"$ref":"#/$defs/Media"},"bytes":{"type":"integer","minimum":0},"sha256":{"$ref":"#/$defs/Hash"},"origin":{"enum":["input","output"]},"job_id":{"anyOf":[{"$ref":"#/$defs/ID"},{"type":"null"}]},"state":{"enum":["reserved","uploading","sealed","quarantined"]},"partial":{"type":"boolean"},"created_at_ms":{"type":"integer","minimum":0}},"required":["artifact_id","filename","media_type","bytes","sha256","origin","job_id","state","partial","created_at_ms"],"additionalProperties":false},
    "Task": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"task_group_id":{"$ref":"#/$defs/ID"},"title":{"type":"string"},"state":{"enum":["creating","ready","busy","quarantined","deleting","deleted"]},"created_at_ms":{"type":"integer","minimum":0},"expires_at_ms":{"type":"null"},"active_job_id":{"anyOf":[{"$ref":"#/$defs/ID"},{"type":"null"}]},"artifacts":{"type":"array","items":{"$ref":"#/$defs/Artifact"},"maxItems":100},"limits":{"$ref":"#/$defs/Limits"},"cleanup_error":{"anyOf":[{"$ref":"#/$defs/Error"},{"type":"null"}]}},"required":["task_id","task_group_id","title","state","created_at_ms","expires_at_ms","active_job_id","artifacts","limits","cleanup_error"],"additionalProperties":false},
    "TaskSuccess": {"type":"object","properties":{"ok":{"const":true},"task":{"$ref":"#/$defs/Task"}},"required":["ok","task"],"additionalProperties":false},
    "Policy": {"type":"object","properties":{"id":{"const":"cowork-task-v1"},"sha256":{"$ref":"#/$defs/Hash"},"native_version":{"const":"0.163.0"}},"required":["id","sha256","native_version"],"additionalProperties":false},
    "Job": {"type":"object","properties":{"task_id":{"$ref":"#/$defs/ID"},"job_id":{"$ref":"#/$defs/ID"},"route_id":{"$ref":"#/$defs/ID"},"state":{"enum":["queued","running","cancelling","succeeded","failed","cancelled","timed_out","interrupted"]},"created_at_ms":{"type":"integer","minimum":0},"started_at_ms":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]},"finished_at_ms":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]},"elapsed_ms":{"type":"integer","minimum":0},"progress":{"type":"string"},"reason":{"anyOf":[{"enum":["invalid_request","idempotency_conflict","model_unavailable","account_unavailable","subscription_required","quota_unknown","quota_spent","scorer_unavailable","scoring_context_too_large","worker_context_too_large","needs_clarification","permission_refused","connector_unavailable","workspace_busy","capacity_exhausted","job_not_found","storage_error","native_version_mismatch","native_protocol_error","native_failed","output_limit","timeout","cancel_requested","broker_interrupted","containment_failed","job_not_terminal","task_not_found","artifact_not_found","task_deleted","task_quarantined","upload_incomplete","transfer_busy","ticket_invalid","ticket_expired","ticket_consumed","length_mismatch","hash_mismatch","artifact_corrupt","policy_unavailable","publication_failed"]},{"type":"null"}]},"execution_policy":{"$ref":"#/$defs/Policy"},"publication":{"enum":["pending","complete","failed","blocked"]},"publication_error":{"anyOf":[{"$ref":"#/$defs/Error"},{"type":"null"}]}},"required":["task_id","job_id","route_id","state","created_at_ms","started_at_ms","finished_at_ms","elapsed_ms","progress","reason","execution_policy","publication","publication_error"],"additionalProperties":false},
    "JobSuccess": {"type":"object","properties":{"ok":{"const":true},"job":{"$ref":"#/$defs/Job"}},"required":["ok","job"],"additionalProperties":false},
    "Worker": {"type":"object","properties":{"provider":{"const":"codex-cli"},"requested_model":{"type":"string","minLength":1},"reported_model":{"anyOf":[{"type":"string"},{"type":"null"}]},"cli_version":{"type":"string","minLength":1},"account_ref":{"const":"native-default"}},"required":["provider","requested_model","reported_model","cli_version","account_ref"],"additionalProperties":false},
    "Usage": {"type":"object","properties":{"input_tokens":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]},"cached_input_tokens":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]},"cache_write_input_tokens":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]},"output_tokens":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]},"reasoning_output_tokens":{"anyOf":[{"type":"integer","minimum":0},{"type":"null"}]}},"required":["input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens","reasoning_output_tokens"],"additionalProperties":false},
    "Warning": {"type":"object","properties":{"code":{"type":"string","minLength":1},"message":{"type":"string"}},"required":["code","message"],"additionalProperties":false},
    "Result": {"type":"object","properties":{"summary":{"anyOf":[{"type":"string"},{"type":"null"}]},"worker":{"$ref":"#/$defs/Worker"},"usage":{"$ref":"#/$defs/Usage"},"artifacts":{"type":"array","items":{"$ref":"#/$defs/Artifact"},"maxItems":100},"changed_paths":{"const":null},"verification":{"const":"planner_review_required"},"warnings":{"type":"array","items":{"$ref":"#/$defs/Warning"},"maxItems":100},"error":{"anyOf":[{"$ref":"#/$defs/Error"},{"type":"null"}]}},"required":["summary","worker","usage","artifacts","changed_paths","verification","warnings","error"],"additionalProperties":false},
    "ResultSuccess": {"type":"object","properties":{"ok":{"const":true},"job":{"$ref":"#/$defs/Job"},"result":{"anyOf":[{"$ref":"#/$defs/Result"},{"type":"null"}]}},"required":["ok","job","result"],"additionalProperties":false},
    "FeedbackSuccess": {"type":"object","properties":{"ok":{"const":true},"feedback":{"type":"object","properties":{"job_id":{"$ref":"#/$defs/ID"},"feedback_id":{"$ref":"#/$defs/ID"},"seq":{"type":"integer","minimum":1},"created_at_ms":{"type":"integer","minimum":0},"verdict":{"enum":["accepted","rejected","inconclusive"]},"verification_status":{"const":"planner_asserted"}},"required":["job_id","feedback_id","seq","created_at_ms","verdict","verification_status"],"additionalProperties":false}},"required":["ok","feedback"],"additionalProperties":false},
    "ContextSuccess": {"type":"object","properties":{"ok":{"const":true},"context_requirements":{"type":"object","properties":{"schema":{"const":"cortados.mcp-context-requirements.v1"},"status":{"enum":["over_budget","needs_context","ready"]},"missing_fields":{"type":"array","items":{"type":"string","minLength":1},"maxItems":100},"questions":{"type":"array","items":{"type":"object","properties":{"field":{"type":"string","minLength":1},"prompt":{"type":"string","minLength":1}},"required":["field","prompt"],"additionalProperties":false},"maxItems":100},"scorer_chars":{"type":"integer","minimum":0},"worker_bytes":{"type":"integer","minimum":0},"scorer_limit_chars":{"type":"integer","minimum":0},"worker_limit_bytes":{"type":"integer","minimum":0},"scorer_digest":{"anyOf":[{"$ref":"#/$defs/Hash"},{"type":"null"}]}},"required":["schema","status","missing_fields","questions","scorer_chars","worker_bytes","scorer_limit_chars","worker_limit_bytes","scorer_digest"],"additionalProperties":false}},"required":["ok","context_requirements"],"additionalProperties":false},
    "RouteSuccess": {"type":"object","properties":{"ok":{"const":true},"route":{"type":"object","properties":{"route_id":{"$ref":"#/$defs/ID"},"selection_mode":{"enum":["explicit","s1"]},"chosen":{"type":"object","properties":{"lane":{"const":"codex"},"provider":{"const":"codex-cli"},"model":{"type":"string","minLength":1},"account_ref":{"const":"native-default"}},"required":["lane","provider","model","account_ref"],"additionalProperties":false},"context":{"type":"object","properties":{"worker_bytes":{"type":"integer","minimum":0},"scorer_chars":{"type":"integer","minimum":0},"scorer_digest":{"anyOf":[{"$ref":"#/$defs/Hash"},{"type":"null"}]}},"required":["worker_bytes","scorer_chars","scorer_digest"],"additionalProperties":false},"quota":{"type":"object","properties":{"status":{"const":"fresh"},"source":{"const":"codex-app-server"},"sampled_at_ms":{"type":"integer","minimum":0}},"required":["status","source","sampled_at_ms"],"additionalProperties":false}},"required":["route_id","selection_mode","chosen","context","quota"],"additionalProperties":false}},"required":["ok","route"],"additionalProperties":false},
    "Ticket": {"type":"object","properties":{"ticket_id":{"$ref":"#/$defs/ID"},"artifact_id":{"$ref":"#/$defs/ID"},"method":{"enum":["PUT","GET"]},"url":{"type":"string","format":"uri"},"authorization":{"type":"string","minLength":1},"expires_at_ms":{"type":"integer","minimum":0},"bytes":{"type":"integer","minimum":0},"sha256":{"$ref":"#/$defs/Hash"},"media_type":{"$ref":"#/$defs/Media"}},"required":["ticket_id","artifact_id","method","url","authorization","expires_at_ms","bytes","sha256","media_type"],"additionalProperties":false},
    "TicketSuccess": {"type":"object","properties":{"ok":{"const":true},"ticket":{"$ref":"#/$defs/Ticket"}},"required":["ok","ticket"],"additionalProperties":false},
    "Receipt": {"type":"object","properties":{"ok":{"const":true},"task_id":{"$ref":"#/$defs/ID"},"artifact":{"$ref":"#/$defs/Artifact"}},"required":["ok","task_id","artifact"],"additionalProperties":false}
  }
}
```

| Remote tool | Request → success | Mandatory semantics |
|---|---|---|
| task_create | Create → TaskSuccess | Idempotent generated folder; title defaults ""; immutable group and effective limits. |
| task_get | TaskKey → TaskSuccess | Owner-scoped snapshot including pending upload intents; deleted tombstone returns empty artifacts. |
| task_delete | Delete → TaskSuccess | Busy/uncertain refuses; otherwise commit deleting and return current lifecycle; never implicit cancel. |
| context_requirements | Context → ContextSuccess | Existing structural questions/budgets, no task needed, no inference. |
| route | Route → RouteSuccess | Require owned ready task and matching evidence group. Project listed fields from existing route; recommendation never starts work. |
| delegate | Delegate → JobSuccess | Require same group, complete evidence, sealed input list; atomically reserve one existing native job. |
| job_status / job_cancel | JobKey → JobSuccess | Ownership check precedes broker lookup; cancel preserves existing idempotent stop-intent semantics (no request_id). |
| job_result | JobKey → ResultSuccess | result=null until terminal; no private path fields; publication may still be pending/failed. |
| job_feedback | Feedback → FeedbackSuccess | Existing feedback_id replay/append-only semantics; accepted needs nonempty all-passed checks, rejected needs failed check. |
| file_upload_ticket | Upload → TicketSuccess | artifact_id=null creates intent; existing ID renews same unsealed content intent with a new request_id. |
| file_download_ticket | Download → TicketSuccess | Only sealed immutable artifacts; idempotent ticket issuance, read retries until expiry. |

Route/delegate require task_evidence (schema required above); all five evidence arrays must exist and
verification must be nonempty for dispatch. References are data, never URL fetch requests or permission.
Use existing context_requirements before route/delegate; broker verifies structural completeness,
not a fabricated durable proof that a prior conversation called that tool. min_band only in S1.
Defaults: context="", constraints={}, permissions={network:false,mcp_servers:[]},
account_ref="native-default". Explicit network:true or any grant refuses permission_refused
before routing; unknown fields refuse invalid_request. timeout_s ≤ existing max_timeout_s.
Models remain existing supported native catalog; schema does not invent another model list.
All list bounds are 100 (control-size guard); server may reject smaller control/handoff byte budgets.
Input ID order is significant and preserved. No sorting of user arrays.

Remote route is an explicit safe projection: route_id, selection_mode, chosen, context and quota only.
The full existing routing decision remains canonical locally; do not alter Router return types.
Result summary and warning message remain native untrusted text, not permission or verified success.
Private structured paths/events/stderr are never exported; user-generated prose may mention paths,
so do not promise textual redaction of arbitrary prose or mutate the exact native final message.
Only output descriptors are downloadable; no broker evidence artifacts are silently converted.

Error payload is exactly Failure, with a safe nonempty operator message and no traceback, path,
provider body or secret. MCP tool errors use isError, not HTTP 500. Unknown/foreign tasks return
task_not_found, artifacts artifact_not_found, jobs job_not_found; identical messages and no existence
leak. Owner disable is HTTP 403. Missing/invalid MCP bearer is HTTP 401 with resource_metadata challenge.
Invalid protocol JSON uses SDK JSON-RPC errors, while a valid tool invocation with bad fields returns
invalid_request. Ingress too-large control body is 413. Domain codes from 001 remain unchanged locally;
new remote codes live in remote schema/error owner, not a new Router refusal algorithm.

### Concrete wire examples

These UUIDs are illustrative closed-schema fixtures, not credentials or Cowork captures.

```json
{"request_id":"10000000-0000-4000-8000-000000000001","task_group_id":"53f0ea39-a61e-4307-9c2c-466c1b4b0bd1","title":"Quarterly workbook"}
```

```json
{"task_id":"20000000-0000-4000-8000-000000000001","request_id":"10000000-0000-4000-8000-000000000002","artifact_id":null,"filename":"empty.bin","bytes":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","media_type":"application/octet-stream"}
```

```json
{"task_id":"20000000-0000-4000-8000-000000000001","request_id":"10000000-0000-4000-8000-000000000003","task":"Inspect the input and write outputs/report.txt.","selection":{"mode":"s1"},"timeout_s":120,"input_artifact_ids":["30000000-0000-4000-8000-000000000001"],"task_evidence":{"schema":"cortados.mcp-task-evidence.v1","task_group_id":"53f0ea39-a61e-4307-9c2c-466c1b4b0bd1","components":["Binary input"],"dependencies":[],"constraints":["No network"],"uncertainties":[],"verification":["Review report contents and output hash"]}}
```

```json
{"ok":false,"error":{"code":"idempotency_conflict","message":"Request identity has different arguments"}}
```

### Canonical identity and transactions

Broker is sole metadata writer in its isolated remote state namespace. HTTP gets a trusted installation
principal from Python construction in LOCAL CORE; FUTURE AUTH must supply it from verified auth.
Private IPC MUST bind that principal; tool JSON and identity headers MUST NOT supply it.
Broker accepts only its configured installation/owner tuple, validates every remote command again,
and uses indexed (owner_id, task_id), (owner_id, task_id, artifact_id),
(owner_id, task_id, job_id) lookups. Remote namespace cannot read local stdio jobs even with their UUIDs.
Private IPC is not exposed publicly. HTTP does not mutate shared global config per request.

Extend the existing jobs.db additively with canonical tables:
- remote_tasks: primary (owner_id,task_id), immutable group, title, state, effective limits/policy
  revision, timestamps, active_job_id, cleanup_error and generated-root identity.
- remote_operations: unique (owner_id,operation,request_id), argument hash, intent phase,
  object IDs, durable nonsecret receipt and any bounded recovery error. Tombstones MUST remain preserved in this local core; live semantics remain an activation gate.
- remote_job_bindings: unique job_id and unique active task reservation; owner/task FK, input manifest,
  execution-policy identity. It participates in the SAME transaction as jobs.accept, never an after-write.
- remote_artifacts: primary (owner_id,task_id,artifact_id), declared and observed bytes/hash, origin/job,
  state, blob identity, partial flag; no content-hash dedup across owners/tasks.
- remote_tickets: unique ticket_id, operation link, capability hash/key version, direction, artifact
  identity, bytes/hash, expiry, state, stream lease. No bearer plaintext at rest.
- remote_publications: unique job_id, ordered bounded output manifest and per-object publication phase.

JobStore owns BEGIN IMMEDIATE/COMMIT/ROLLBACK; task_store uses that connection inside owner-supplied
transactions, never its own SQLite connection/commit. Foreign keys and indexes are mandatory.
Migration is transactional, version checked, integrity checked, backup reviewed by QA, and must open
both new and prior stdio databases without changing old result/argument hashes. No concurrent migration.

Replay key: owner + operation + request_id, except existing feedback_id and cancel-by-job semantics.
Canonical argument hash uses schemas.canonical after documented defaults (excluding request_id only),
includes task_id, group, full input order and content intent. It excludes live tokens, derived cwd and
current config. Policy identity is frozen on first acceptance; replay compares original caller args,
then returns original job regardless of later config/policy change; it never launches again.
Lookup committed intent/replay BEFORE scoring/admission. A changed hash returns idempotency_conflict.
Uncommitted validation/admission refusals claim no request_id and start no process. Disk/integrity
failure after intent commit leaves a recoverable intent; it cannot be mistaken for never accepted.
Do not auto-resubmit after an uncertain response. Inspect task_get/job_status with the original IDs.

Delegate holds existing admission serialization and task operation lock, checks replay, task ready,
no active streams/uploads, exact group, sealed input ownership/hash, limits and policy availability,
then invokes existing route/admission. In one SQLite transaction recheck task version, accept job,
bind immutable input manifest and execution policy, mark task busy and store response identity.
Only after commit prepare the private run workspace and bootstrap the existing watchdog.
No route result is permission to skip fresh admit_launch just before START. Failure preparing inputs
finishes the accepted job failed without model launch; never creates a second attempt.
A task lock must cover filesystem preparations; no database transaction is held across network scoring.

Task create: commit creating intent and generated task ID; mkdir relative to trusted root fd, fsync,
commit ready. Replays may return creating until recovery completes, always same ID.
Lifecycle: creating→ready; ready→busy on atomic acceptance; busy→ready only after termination AND
publication finished or visibly failed; containment uncertainty→quarantined.
Ready→deleting→deleted; creating may delete after proof no writer. No remote unquarantine command.
Root can reconcile proof under a separately authorized operator procedure. No automatic job replay.

## 4. Native execution policy (AC-008-09)

Persist execution_policy={id:"cowork-task-v1", sha256:<canonical profile manifest digest>,
native_version:"0.163.0"} at acceptance, in job record, remote binding and watchdog bootstrap.
Manifest includes native binary SHA-256, exact profile tables/argv construction, feature disables,
environment policy and capture references. Digest covers template plus version; resolved workspace
identity is stored alongside it. Never recompute accepted policy from mutable startup config.
Unknown/missing/mismatched remote policy refuses START with policy_unavailable/native_version_mismatch.
Existing stdio records without policy use their legacy path only; missing remote policy NEVER defaults
to local workspace-write. Restart recovery is existing interruption/containment recovery, not relaunch.

Leave ordinary native_arguments output byte-for-byte (including flag order and -s workspace-write).
Remote-only branch uses the same strict config, ignore-user-config/rules, ephemeral, no-repository
requirement, disabled features, model, final-message/event controls and no MCP servers. Use -c
default_permissions="cowork-task" and complete permissions table, omit -s and sandbox_mode and legacy
sandbox_workspace_write. Native exec has no -P. Required profile:
```toml
default_permissions = "cowork-task"
[permissions.cowork-task]
extends = ":workspace"
[permissions.cowork-task.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"
[permissions.cowork-task.network]
enabled = false
```
Workspace comes only from broker-generated -C. Each attempt receives a clean generated workspace
beneath its task, containing only chosen inputs and outputs/; old attempts and broker blobs are outside
that workspace. Immutable inputs are copied, never hardlinked, as inputs/<artifact_id>/<filename>.
The agent can modify its input copy, but cannot change canonical input bytes. Do not copy credentials.
Service-authenticated native harness still uses its existing credential owner; tools must not read it.
Ordinary native_environment behavior stays unchanged; remote environment changes need explicit capture.
Task/context strings remain byte-exact in existing handoff; append a separate untrusted manifest
section with only relative input paths and output directory. Include that addition in handoff budget
and persisted manifest; it must never inject policy instructions or fetched content into original text.

Read evidence:
`/Users/velox/.cortados-builds/cowork-mcp-verification/native-sandbox-qa/review.json`,
`codex-home/config.toml`, capture.json and its scripts. Native binary SHA-256
`8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31`.
Review reports 13/13 shell sandbox assertions, task read/write positives, sibling/home/symlink/tmp/network
negatives and positive unsandboxed network control. It explicitly does NOT prove exec model/tool
execution. :minimal permits some runtime paths (/usr/bin and /etc observed); no claim of an exhaustive
runtime allowlist. Actual Codex exec canary with positive task tool use and negative host credential,
broker/sibling/tmp/symlink/network attempts remains REQUIRED native acceptance before remote execution
is enabled. Shell proof allows precise implementation/capture preparation, not acceptance laundering.

## 5. Raw byte transfer and output publication (AC-008-10–14)

Tickets are capabilities carried ONLY in Authorization: Bearer <ticket>, never URL/query/fragment.
Ticket.url is exact configured HTTPS origin + /transfers/<ticket_id>; no arbitrary URL fetch,
redirect, multipart, base64, parsing, archive extraction or macro execution. Authorization value in
Ticket includes the Bearer prefix. Token is base64url HMAC-SHA256 using a dedicated persisted random
ticket key over a versioned, length-delimited canonical encoding of ticket_id, owner, operation,
artifact, direction, bytes, hash and expiry. Store only digest/key version and metadata; reproduce the
same token on issuance replay without storing plaintext. Key changes revoke outstanding tickets.
A 32-byte HMAC is not a substitute for OAuth: minting requires authenticated owner/capability.
PUT and GET verify hash in constant time, current owner enabled, task not deleting/deleted/quarantined,
canonical artifact binding, expiry and method. Bad/foreign credentials all return ticket_invalid.

Upload intent is atomic: reserve declared bytes + staging/copy cost, artifact slot and unique object
identity before returning ticket. artifact_id=null with identical request replays same intent/ticket;
new request with existing unsealed artifact renews only matching name/type/length/hash. Revoke old
ticket, require no writer and remove proven inactive staging first. Different content conflicts;
sealed artifact cannot renew (ticket_consumed). Ticket replay after expiry returns same expired ticket,
never extends expiry. Caller obtains a new request_id to renew a pending intent.
Zero bytes is legal. task_get exposes reserved/uploading/sealed state for lost-ACK recovery.

PUT: Content-Type application/octet-stream, exact Content-Length required; reject Transfer-Encoding,
Content-Encoding and Range. Body length must equal declared bytes; max check BEFORE reading; stream
in bounded chunks, increment hash, refuse early EOF/extra bytes/hash mismatch. Claim ticket exclusively
in broker transaction (issued→claimed); repeat concurrent PUT returns transfer_busy.
HTTP byte mover MUST send bounded raw chunks over private stream protocol §8.
Broker file_transfer owns the staging descriptor and writes; HTTP owns no file descriptor or metadata.
After fsync and exact verification, persist publication intent, rename without replacing an existing
blob, fsync containing directory, commit sealed receipt and consumed ticket. Return HTTP 201 Receipt;
no JSON bytes in MCP. A consumed PUT returns 409 ticket_consumed (even same body); task_get recovers
receipt. A disconnect aborts staging after writer termination proof; new ticket required. A failed or
interrupted PUT never exposes partial bytes or releases reservation while writer may still run.

GET: immutable sealed object only, full body HTTP 200; no Range (416 invalid_request), no redirects.
Headers: Content-Type application/octet-stream, Content-Length exact bytes, ETag quoted SHA-256,
X-Content-Type-Options nosniff, Cache-Control no-store. Content-Disposition MUST use attachment with
ASCII fallback filename="artifact-<artifact_id>" and filename*=UTF-8'' followed by percent-encoded
UTF-8 filename bytes (urllib.parse.quote with safe=""). This adopts [RFC6266](https://www.rfc-editor.org/rfc/rfc6266)
and [RFC8187](https://www.rfc-editor.org/rfc/rfc8187) parameter encoding; arbitrary Unicode MUST NOT enter
raw headers. The descriptor retains the original validated filename. Media type remains descriptive.
Full retries allowed
until expiry; expiry prevents new streams, does not truncate a stream already validly opened.
Delete/owner disable actively abort open streams; bytes already sent cannot be recalled.
Recheck descriptor fstat identity/size and stored hash before first serving after restart; mutation/
missing blob quarantines and returns artifact_corrupt. Never serve agent-mutable files directly.

Transfer errors use Failure JSON and HTTP: 400 invalid_request/length_mismatch/hash_mismatch;
401 ticket_invalid/ticket_expired; 403 permission_refused; 404 artifact_not_found;
409 transfer_busy/ticket_consumed/task_deleted/task_quarantined/upload_incomplete;
413 output_limit; 507 capacity_exhausted; 503 storage_error/artifact_corrupt/policy_unavailable.
Do not echo token/URI/body. Unsupported methods return 405. Authentication precedes object-existence
checks; malformed ticket and foreign object must not leak identity. Upload/read total and idle deadlines
are operator budgets, enforced even for slow peers; read timeout is timeout (408) before headers, or
connection abort after headers, with safe broker audit event.

Path safety: anchor every open/mkdir/rename/unlink to trusted directory descriptors; O_NOFOLLOW on
every component, regular-file fstat, link count=1, same device and recorded inode identity.
Reject absolute/traversal/symlink/hardlink/device/socket/FIFO targets. A check-then-unconstrained-open is
not sufficient. Output scan is bounded by artifact count, byte budget and depth; v1 exports only regular
leaf files immediately in outputs/ with valid Name, no nested directories. Invalid entry fails the
whole publication visibly; do not silently omit files. No .codex, native logs or arbitrary cwd export.
Each task's blob/staging directories are outside worker-visible workspace.

After proven termination, snapshot outputs into private staging, hash copied bytes, persist one ordered
publication manifest, fsync/rename each immutable object, then atomically publish all descriptors.
No partial manifest exposure. Reconcile crash by journal/hash/inode; never re-read a changed workspace
as though it were the original snapshot. On failure publication=failed with publication_error;
job state remains its real native outcome. On unproven termination publication=blocked, task quarantined.
Failed/cancelled/timed_out/interrupted job output descriptors have partial=true, verification remains
planner_review_required. Later attempts cannot overwrite prior outputs. Accepted feedback is a planner
assertion, not independent QA. Empty output set is complete, not an error.

## 6. Recovery, retention and budgets (AC-008-15–16)

| Store / class | Sole owner | Recovery / deletion rule |
|---|---|---|
| jobs.db + remote tables / canonical | Broker JobStore transaction owner | SQLite integrity/version/FK checks; ownership never reconstructed from directory names. Corruption fails namespace closed. |
| Auth clients/tokens / canonical | Maintained OAuthProxy encrypted store | Stable keys; no task access; restart/rotation qualification outstanding, no claimed transactional refresh guarantee. |
| Input/output immutable blobs / evidence | Descriptor-bound byte publisher under broker intents | Preserve exact raw bytes; missing/hash mismatch quarantines; no regeneration. |
| Run workspace / mutable execution state | Existing native attempt within remote policy | Preserve on containment uncertainty; never resume model attempt after restart. |
| Staging / disposable projection | One leased byte mover | Discard only after proving writer stopped; reconcile rename intent before freeing capacity. |
| Tickets / ephemeral canonical authority | Broker | Durable consumed/revoked state; revoke all issued/claimed tickets on restart before accepting core calls (a future listener remains blocked). |
| Responses/manifests / projections | Remote result adapter | Rebuild only from authorized canonical records, never filesystem discovery or raw event paths. |

Recovery sequence: acquire existing exclusive broker ownership; migrate/check DB; revoke outstanding
tickets; establish termination proof for streams/worker processes; apply existing job interruption rules;
reconcile creating/upload/publication intents using exact IDs/hash/recorded filesystem identity;
resume deleting tasks; only then admit new calls. On uncertain same-boot process ownership keep active
reservation and quarantine. No automatic model/API retry. Missing post-rename commit may finalize the
record only when committed intent and blob agree; unjournaled objects are quarantined, not imported.
Deleting task revokes tickets and closes streams before unlink; deleting remains visible with
cleanup_error until every owned payload is gone. Tombstone preserves task/group/create-operation identity.
Repeated delete returns current tombstone, never recreates. Unknown object remains not found.

Delete refuses workspace_busy for active job, upload, publication or uncertain containment. Download
streams may be cancelled by delete, then await close before unlink. Retain append-only job/feedback and
operation tombstones under existing policy; payload deletion is not prompt/history erasure. Remove
workspace inputs/outputs, staging and blobs only. No automatic backup purge or hidden history policy.
[FIXED] LOCAL CORE supplies explicit task_delete only; expires_at_ms MUST be null.
There MUST be no scheduler, age sweep or automatic cleanup implementation. Recovery of committed
explicit delete or failed staging is not retention cleanup. Synthetic retention=explicit_delete is
a fixture declaration only. Live retention MUST be frozen before activation, including payload/
history/tombstone/backup semantics; a different policy requires an amended reviewed package.
No seven-day implementation is authorized even conditionally in this build.

Root policy candidates use DECIMAL bytes: 100000000/file, 1000000000/task, 10000000000/installation.
These replace v1's MiB/GiB proposals; no approval recorded here. Proposed technical guards: 100 artifact
slots/task, 300-second ticket TTL, 65536-byte control bodies, 65536-byte stream chunks, one upload and
one download stream/task, four streams/installation, 30-second idle and 600-second total transfer
deadline. No values are implicit runtime defaults: startup requires a recorded effective policy.
Small byte/count/time limits reject useful work or slow clients; large values consume disk/memory and
extend capability exposure. Stream chunks bound working memory; lower increases syscall overhead,
higher increases per-stream RAM. Concurrency caps bound descriptors and memory; smaller queues block
useful work, larger caps create resource contention. Capacity exhaustion refuses, never auto-deletes.

Accounting includes physical canonical blobs, staging, input copies and retained workspaces, not merely
published manifest sizes; reservations must account for simultaneous copy/rename phases. Reconcile counts
with journal, no double credit on retries. Metadata/auth/history space is separate and needs operator disk
headroom. Native-generated workspace bytes cannot be hard-capped by these transfer checks alone.
Monitor task/installation usage with watchdog and stop on overrun; refuse further admission/publication,
preserve containment proof. This is bounded detection, not a proven hard OS disk quota. QA must measure
overshoot and root approve disk headroom/poll budget before activation; if strict physical byte ceilings
are required, a qualified OS storage quota is a blocker, not an invented guarantee. No builder may
quietly exclude workspace bytes from the advertised accounting.

Backups must carry DB + blob manifest consistently; restore into disabled namespace, verify hashes,
ownership, policy identities and no surviving processes, revoke tickets, then explicitly enable.
Auth backup/restore has separate maintained-provider qualification. Storage loss never widens access.

## 7. Gate boundaries and authoring result

Stable requirements AC-008-01–16 retain identities; execution document maps each to independent scenarios.
Local deterministic API/filesystem/transaction exams and captured native acceptance are separate from
public activation and real Cowork UI/byte delivery. A Cowork capture is NOT required to write the local
builder's protected fixtures. LOCAL CORE requires independent D then A, committed protected local exams and root package
approval before implementation dispatch. Provider-registration and deployed limits/retention are
activation gates, not prerequisites for this parameterized synthetic-only package. No pass is claimed.
Source evidence here supports implementation design, not provider qualification, sandbox exec acceptance,
performance compliance or successful file delivery in Cowork.

### Recorded review evidence supplement (2026-10-17)

Read concurrent [root review](COWORK-MCP-008-ROOT-REVIEW.md) and private
/Users/velox/.cortados-builds/cowork-mcp-verification/provider-preflight.md,
auth-composition-result.json and native-exec-capture/prepare-review.json.
Synthetic auth research used FastMCP-slim3.4.9 / MCP1.30.0 / Starlette1.8.0 / authlib1.9.1 /
joserfc1.7.5 / py-key-value-aio0.4.6, zero network calls. It proved its synthetic protected ASGI
route, not production MCP manager or real GitHub refresh. It observed missing exp and future nbf
accepted by dependency verifier; parent adapter rejects both. Fresh provider/same store retained
synthetic validity, and plaintext upstream sentinel was absent from sampled storage.
Actual native exec parser/tool capture is still false/pending in prepare-review.json.

Root reports public numeric GitHub ID 237812476 for 4list233; provider/allowlist approval still pending.
Do not silently set this as an enabled production principal. Root/QA documentation updates outside
these three author files were observed concurrently and were not modified/reverted by this author.

Inherited v2 source SHA-256 anchors (re-pin candidate diff before dispatch):
```text
server.py           8f99ff57287edc993c2126a439533cef3877ecfa8451cc7dc2c9e247dcbdd87e
schemas.py          bd1cdfc0e84a18b960a3efe9a60f06c389e5cf93e3b165e9a8fe38980b22594a
broker.py           afccb68597afa01083761a555ced00c0621ec32e19352d0dec0f5fe9e2a23697
job_store.py        07fb691f9916c47da9c42e460e0a0d7631c764bda4c1d5a90f3882b0103d3312
codex.py            34f5ce9ca1838399fbd995ef124b29a40292084cd4bda4d4b43fc702c47c02d1
worker.py           bf596478aed116db05e39919132f81c8fd3399f5933a30e46f96c476b127a2e0
worker_admission.py a75e41a93ae01fa43f0439475b3f28a0d45436a64354e3e893ad3cf79e36bc10
```
First six paths are integrations/mcp; last is cortados_router. No claim of test execution follows.

## 8. LOCAL CORE exact construction and IPC contract

[FIXED; AC-008-03–16] This section owns the new internal interfaces. It replaces v2's undefined
descriptor-ID handoff. All requirements in §3–6 apply to LOCAL CORE except actual auth/listener
construction, which belongs exclusively to §2. Stdio and training MUST retain existing behavior.

### Mandatory closed configuration (AC-008-03,05,09,16)

remote_config.load_core_config(raw: bytes, base: validated existing config) -> CoreConfig MUST reject
duplicates, unknown fields, null, booleans-as-integers, missing fields and unsupported enum values.
Startup errors MUST be WorkerError(policy_unavailable, "Local core configuration is unavailable");
there MUST be no partial startup. The following typed record is the normative closed schema;
EVERY listed key is required. No other key is legal. Nested records are also closed.

| Record | Exact fields and types |
|---|---|
| CoreConfig | schema="cortados.cowork-core.v1"; mode="local_test"; installation_id:ID; principal:Principal; roots:Roots; limits:CoreLimits; retention:Retention; execution_policy:Policy (§3); transfer_origin:HTTPSOrigin; ticket_key_version:nonblank ASCII string; ticket_key:32 raw bytes supplied through trusted Python construction |
| Principal | issuer:nonempty string; subject:nonempty string; installation_id:ID; client_id:nonempty string; enabled:boolean |
| Roots | state_dir:absolute path; tasks_dir:absolute path; blobs_dir:absolute path; staging_dir:absolute path |
| CoreLimits | file_bytes,task_bytes,installation_bytes,artifacts_per_task,ticket_ttl_s,control_bytes,chunk_bytes,uploads_per_task,downloads_per_task,streams_per_installation,transfer_idle_ms,transfer_total_ms,ipc_idle_ms,workspace_poll_ms,disk_headroom_bytes:positive integers |
| Retention | payloads="explicit_delete"; history="preserve"; tombstones="preserve"; backups="external_not_managed"; automatic_cleanup=false |

The raw configuration MUST encode ticket_key as exactly 64 lower-case hexadecimal characters;
the loader MUST decode it to 32 bytes and MUST NOT log it. Test fixtures MUST use a synthetic key.
Limits MUST be <= 2^53-1; control_bytes MUST also be <= 2^32-1 (four-byte frame-header length).
artifacts_per_task MUST be <=100 (wire structural ceiling).
file_bytes <= task_bytes <= installation_bytes; chunk_bytes <= control_bytes;
uploads_per_task and downloads_per_task <= streams_per_installation;
transfer_idle_ms <= transfer_total_ms and ipc_idle_ms <= transfer_total_ms.
All budget dimensions are integer bytes, counts or explicitly named milliseconds/seconds.
No value from §6's historical proposals is a default. Existing base config still supplies its
validated native job count/timeout/handoff limits; core MUST NOT introduce alternative defaults.

HTTPSOrigin MUST be a scheme/host/optional port with no userinfo/path/query/fragment.
Local tests MUST explicitly supply https://cowork.invalid; ASGITransport performs no DNS/listen.
Principal issuer MUST be urn:cortados:synthetic and subject MUST start synthetic: in mode local_test.
Owner identity MUST be canonical([issuer,subject,installation_id]); client_id MUST match separately.
Disabled principal MUST refuse new control/ticket/stream operations with permission_refused.
Principal installation_id MUST match CoreConfig. There is no inferred GitHub identity.
Roots MUST be distinct, private 0700 sibling directories beneath one authorized isolated test tree;
no symlink ancestors, shared stdio state, user home or installed runtime is legal.
base.state_dir and existing_broker.config.state_dir MUST both equal roots.state_dir after canonical
path validation. Any mismatch MUST refuse policy_unavailable before migration or job acceptance.
Core config digest MUST hash canonical configuration excluding raw ticket_key but including its SHA256.
Config policy/limit snapshot MUST be frozen on task creation; lower current installation capacity
MUST prevent new reservations, never modify prior receipts or silently relax an accepted policy.
Task Limits wire projection MUST contain the existing seven §3 keys, including retention=explicit_delete.
Remaining technical limits MUST stay in the persisted snapshot, not appear as extra wire fields.

Valid fixture example (all values are synthetic, not recommendations):
```json
{"schema":"cortados.cowork-core.v1","mode":"local_test","installation_id":"40000000-0000-4000-8000-000000000001","principal":{"issuer":"urn:cortados:synthetic","subject":"synthetic:forest","installation_id":"40000000-0000-4000-8000-000000000001","client_id":"synthetic-client","enabled":true},"roots":{"state_dir":"/authorized-fixture/state","tasks_dir":"/authorized-fixture/tasks","blobs_dir":"/authorized-fixture/blobs","staging_dir":"/authorized-fixture/staging"},"limits":{"file_bytes":1024,"task_bytes":8192,"installation_bytes":32768,"artifacts_per_task":10,"ticket_ttl_s":30,"control_bytes":8192,"chunk_bytes":1024,"uploads_per_task":1,"downloads_per_task":1,"streams_per_installation":2,"transfer_idle_ms":1000,"transfer_total_ms":5000,"ipc_idle_ms":1000,"workspace_poll_ms":100,"disk_headroom_bytes":4096},"retention":{"payloads":"explicit_delete","history":"preserve","tombstones":"preserve","backups":"external_not_managed","automatic_cleanup":false},"execution_policy":{"id":"cowork-task-v1","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","native_version":"0.163.0"},"transfer_origin":"https://cowork.invalid","ticket_key_version":"fixture-1","ticket_key":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
```
QA MUST substitute its authorized temporary paths and captured synthetic policy manifest.
Deleting chunk_bytes, setting retention to seven_days, adding public_enabled, or using a real subject
MUST fail before creating state. Explicit syntactic fixture values MUST NOT waive filesystem/digest checks.

### Module interfaces and ownership (AC-008-04–16)

All calls below MUST use typed immutable records, never dictionaries with undocumented fields.
WorkerError uses §3 Failure at transport boundaries; trusted internal calls raise that same error.
Source owners and exact writable paths are in EXECUTION; no new router, queue or storage writer.

| Owner | Required callable / effect |
|---|---|
| remote_schemas | validate_remote(tool:str,args:object,config:CoreConfig)->normalized arguments; remote_tool_schema(tool)->closed schema; project_remote_result(record)->§3 success or Failure. Reuse schemas.canonical and task_evidence validators, never copy route/quota decisions. |
| remote_dispatch | CoreBroker(existing_broker,config); async dispatch(principal:Principal,tool:str,args:object)->§3 result. MUST check owner/client before task lookup and scope all job reads. Existing admission_lock precedes per-task lock, then DB transaction; no inverse order. |
| task_store | migrate(db); create_intent(tx,principal,args,snapshot); get_owned(tx,owner,task_id); accept_binding(tx,job,owner,task_id,inputs,policy); operation(tx,owner,op,request_id,hash); transition(tx,owner,task_id,expected_version,next_state). Each returns canonical typed rows; tx MUST be JobStore.db in its current transaction. |
| JobStore | accept(arguments,route,cli_version=...,remote_binding=None)->(record,created). Optional binding MUST be trusted typed data from CoreBroker, never local wire input. Job insert, task reservation, operation response and binding MUST commit together. replay precedes route. No nested BEGIN, separate writer or after-commit association. Existing local accept behavior remains identical. |
| task_workspace | prepare(task_row,job_binding)->WorkspaceIdentity; remove(task_row)->DeleteProgress; reconcile(intent)->RecoveryResult. Paths MUST derive from generated UUIDs and trusted root fds. Identity is closed {device:int,inode:int,relative_name:str}; no public path output. |
| artifact_store | open_stage(intent)->broker-owned fd; seal(fd,intent)->BlobIdentity; open_blob(row)->broker-owned fd; snapshot(binding)->ordered manifest. BlobIdentity is closed {device:int,inode:int,relative_name:str,bytes:int,sha256:Hash}. MUST close on every error. |
| transfer_tickets | issue(tx,principal,args,direction)->TicketSuccess; claim(tx,capability,direction,connection_id)->Lease; revoke(tx,task_id)->lease IDs. Capability never enters logs or job evidence. |
| file_transfer | async serve_stream(connection,core)->None; put_chunk(lease,offset,bytes)->offset; finish_put(lease)->Receipt; read_chunk(lease,offset,max_bytes)->bytes; close(lease,reason)->None. Sole stream descriptor owner in broker process. |
| remote_http | create_test_app(*,core_client,principal:Principal,config:CoreConfig)->ASGI app. Explicit trusted constructor only; no global app, main, environment factory, listening socket or auth bypass flag. Tool registry uses remote_schemas and existing lowlevel SDK manager; PUT/GET translate to private stream protocol. |
| remote_native | build_binding(task,inputs,policy)->immutable binding; verify_binding(binding,manifest)->None; native_arguments(binding,base_args)->argv. MUST reuse existing codex TOML encoder/features and native version/termination owners. |
| broker / worker / codex | Broker.run_worker MUST send persisted remote binding in bootstrap only for remote jobs; Watchdog MUST validate before START and use remote argv branch. Missing/mismatched binding MUST refuse; ordinary bootstrap/argv stay unchanged. |

remote_http MUST reject principal/owner/client identity in HTTP headers, query, JSON-RPC parameters or
tool fields. Tests vary principals by constructing separate clients/apps, never a network-selected
principal. There MUST be no public OAuth/discovery endpoints in this app. Synthetic authorization
proves ownership enforcement, not authentication. No code path in LOCAL CORE may register real auth.

### Private IPC: bytes move, descriptors do not (AC-008-06,10–16)

[FIXED] Reuse broker process and its exclusive state ownership. Existing broker.rpc/connection use
newline JSON; they MUST stay byte-compatible for stdio. CoreBroker MUST provide a separately framed
private connection on a Python-created AF_UNIX SOCK_STREAM socketpair passed at trusted construction.
No socket pathname/listener is installed by LOCAL CORE. Parent MUST close unused socket ends after
process creation; each endpoint belongs to one process. QA MUST exercise separate processes as well
as in-process ASGI. CoreBroker MUST NOT use asyncio StreamReader and recvmsg on the same descriptor.
There is NO SCM_RIGHTS transfer, descriptor number/ID serialization or frontend open-path RPC.

Each frame MUST be: four-byte big-endian JSON header length, header UTF-8 bytes, then exactly the
declared raw body bytes. Header length MUST be 1..control_bytes; body length MUST be 0..chunk_bytes.
Reject oversized lengths before allocation, truncated frame, duplicate keys and unknown fields.
Header schema: closed {v:1,seq:nonnegative integer,op:enum below,body_bytes:nonnegative integer,
args:operation-specific object}. Responses MUST echo v,seq,op and contain closed
{v,seq,op,body_bytes,result}; result is §3 Failure or the exact success below.
A connection MUST permit one request in flight; seq starts 0 and increases by one.
All control bodies MUST be empty. Only put_chunk requests and read_chunk successes carry raw bytes.
No base64, JSON byte arrays or full-file buffering. max_bytes and offsets are integer bytes >=0.
Every args shape is closed, with every listed field required.

| op | args | success result |
|---|---|---|
| bind | config_digest:Hash, principal:Principal | {ok:true,connection_id:ID} |
| tool | tool:§3 operation name, arguments:its request | matching §3 success |
| open_put | ticket_id:ID, capability:string, content_length:integer>=0 | {ok:true,lease_id:ID,offset:0} |
| put_chunk | lease_id:ID, offset:integer>=0 | {ok:true,offset:integer>=0} |
| finish_put | lease_id:ID | §3 Receipt success |
| open_get | ticket_id:ID, capability:string | {ok:true,lease_id:ID,artifact:Artifact} |
| read_chunk | lease_id:ID, offset:integer>=0, max_bytes:integer>=1 | {ok:true,offset:integer>=0,eof:boolean} |
| close | lease_id:ID, reason:enum[complete,disconnect,timeout,abort] | {ok:true} |

bind MUST be first, once, body_bytes=0; config digest/principal MUST exactly match trusted construction.
The private bind identity is never accepted from HTTP/tool fields. Socket possession is an internal
trust boundary, not an auth adapter. Unknown op/seq/shape MUST return invalid_request then close.
Failed bind MUST return permission_refused then close. Foreign/unknown lease MUST return ticket_invalid.
put_chunk offset MUST equal current offset; nonzero bodies only; offset mismatch MUST abort lease
with length_mismatch. read_chunk max_bytes MUST be <=chunk_bytes. Broker MUST return at most that many
bytes, next offset and eof=true exactly at artifact length. Empty GET returns zero bytes/eof=true.
finish_put MUST require exact length/hash and exclusive lease, then use §5 publication transaction.
Only complete GET may close with complete; an incomplete PUT close always revokes ticket and aborts.

Lease is broker-only closed {lease_id,connection_id,owner_id,task_id,artifact_id,ticket_id,direction,
offset,deadline_ms,idle_deadline_ms,state}; IDs use ID, owner_id is canonical owner string,
direction enum[put,get], state enum[open,closing,closed], offset nonnegative, deadlines UTC milliseconds.
Live timers MUST use monotonic deadlines derived once at open; durable UTC deadlines are recovery data.
Broker MUST retain descriptor and hash state in memory keyed by lease_id. No descriptor may survive
in SQLite; a restarted broker MUST revoke every lease/ticket and never recreate authority from IDs.
Every RPC lease check MUST bind connection + owner + ticket + artifact + direction.

Frontend disconnect MUST close broker descriptor before marking lease closed/freeing reservation.
Broker crash closes its own FDs by process death; recovery MUST reconcile staging before freeing bytes.
Frontend MUST detect EOF/timeout, stop HTTP streaming and close its endpoint. Lost PUT completion ACK
MUST resolve via task_get sealed receipt, never blind retry; GET may mint/reopen according to §5.
Delete/disable MUST revoke lease before any further read/write; file_transfer MUST check revocation
between chunks. Active chunk syscall may finish; delete MUST await its completion and descriptor close
before unlink. Kernel-buffered bytes already sent cannot be recalled. No unproven writer is swept.
Bounds MUST include socket backpressure and HTTP awaits; memory O(chunk_bytes * active streams).
Disk writes/hash work MUST complete before next chunk ACK; no detached writes outlive a closed lease.

Example header (body contains four raw bytes, including NUL; never JSON-encoded):
```json
{"v":1,"seq":2,"op":"put_chunk","body_bytes":4,"args":{"lease_id":"50000000-0000-4000-8000-000000000001","offset":0}}
```
Success header:
```json
{"v":1,"seq":2,"op":"put_chunk","body_bytes":0,"result":{"ok":true,"offset":4}}
```
A header with fd_id or offset=-1 MUST fail invalid_request. A valid header from another connection
using the same lease MUST fail ticket_invalid. QA MUST assert no unrelated descriptor was touched.

### Additive DB layout and recovery (AC-008-06–08,13,15–16)

[FIXED] Existing jobs.db user_version=1 MUST remain 1; no silent schema-version bump breaks stdio.
JobStore MUST invoke task_store.migrate only for explicit core construction. Transactionally create
remote_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1)).
Unknown existing remote version MUST refuse storage_error. Existing jobs and job_feedback definitions,
record hashes and append-only triggers MUST remain unchanged. Missing tables in an already-versioned
remote namespace MUST be corruption, not silently recreated. Foreign keys MUST be enabled first.
Names below extend §3 table identities; fields are mandatory NOT NULL unless marked nullable.
TEXT JSON MUST use canonical encoding and schema validation before write and after read.
INTEGER boolean fields MUST have CHECK IN(0,1); integer counters/times MUST be >=0.

| Table | Exact columns besides keys specified in §3 |
|---|---|
| remote_tasks | owner_id TEXT,task_id TEXT,group_id TEXT,title TEXT,state TEXT,policy_json TEXT,created_at_ms INTEGER,updated_at_ms INTEGER,version INTEGER,active_job_id TEXT nullable,root_identity_json TEXT nullable,cleanup_error_json TEXT nullable |
| remote_operations | owner_id TEXT,operation TEXT,request_id TEXT,argument_hash TEXT,phase TEXT,object_ids_json TEXT,receipt_json TEXT nullable,error_json TEXT nullable,created_at_ms INTEGER |
| remote_job_bindings | job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),owner_id TEXT,task_id TEXT,input_manifest_json TEXT,execution_policy_json TEXT,workspace_identity_json TEXT nullable,active INTEGER |
| remote_artifacts | owner_id TEXT,task_id TEXT,artifact_id TEXT,filename TEXT,media_type TEXT,declared_bytes INTEGER,declared_hash TEXT,observed_bytes INTEGER nullable,observed_hash TEXT nullable,origin TEXT,job_id TEXT nullable,state TEXT,blob_identity_json TEXT nullable,partial INTEGER,created_at_ms INTEGER |
| remote_tickets | ticket_id TEXT PRIMARY KEY,owner_id TEXT,task_id TEXT,artifact_id TEXT,operation TEXT,request_id TEXT,capability_hash TEXT,key_version TEXT,direction TEXT,bytes INTEGER,sha256 TEXT,expires_at_ms INTEGER,state TEXT,lease_id TEXT nullable,connection_id TEXT nullable |
| remote_publications | job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),owner_id TEXT,task_id TEXT,manifest_json TEXT,state TEXT,error_json TEXT nullable |
| remote_reservations | reservation_id TEXT PRIMARY KEY,owner_id TEXT,task_id TEXT,operation TEXT,request_id TEXT,kind TEXT,bytes INTEGER,state TEXT |

Task/artifact composite PKs and operation unique key are §3's exact keys. Composite owner/task FKs
MUST connect bindings/artifacts/tickets/publications/reservations to remote_tasks.
Tickets MUST FK owner/task/artifact and owner/operation/request to the matching rows.
A partial unique index on remote_job_bindings(owner_id,task_id) WHERE active=1 MUST enforce one active job.
Indexes MUST cover artifacts(owner_id,task_id), operations(owner_id,operation,request_id),
tickets(owner_id,task_id,state), reservations(owner_id,task_id,state).
Closed states: task/artifact use §3; operation phase=[intent,committed,failed];
ticket state=[issued,claimed,consumed,revoked]; publication state=[pending,complete,failed,blocked];
reservation kind=[upload,workspace_copy,output_snapshot], state=[held,materialized,released].
Input manifest is ordered Artifact identities plus immutable BlobIdentity; output manifest is ordered
Artifact + BlobIdentity + phase enum[staged,renamed,sealed]. Object IDs JSON is closed
{task_id:ID,artifact_ids:ID[],job_id:ID|null,ticket_id:ID|null}. Error JSON is §3 Error.
Root identity uses WorkspaceIdentity. policy_json contains complete config snapshot minus ticket_key,
plus its hash and config digest. Receipt JSON MUST match operation's §3 success exactly except TicketSuccess: stored ticket MUST omit
authorization, which transfer_tickets reconstructs from committed metadata and the configured key
only at projection. No plaintext capability may be stored in any receipt. Key mismatch MUST return
ticket_invalid, never reconstruct with a different key.
Migration MUST NOT create a remote table inside an ordinary stdio-only open.

Reservations MUST count remaining promised writes plus measured physical bytes, not both for the
same extent. Rename on same filesystem transfers accounting, never doubles it; input/output copies
reserve additional physical bytes before copying. Refuse cross-device rename at config/open time.
All owned task files, staging, blobs and retained workspaces MUST count toward task/installation use.
A publication MUST fail visibly if its entire snapshot cannot reserve capacity; no partial manifest.
Native overrun MUST stop through existing watchdog with capacity_exhausted; quota is observed/polled,
not a guaranteed hard physical ceiling. disk_headroom_bytes is mandatory and independently measured.
Recovery MUST hold admissions closed until every held reservation is reconciled against its intent,
known descriptors/process death and file identity. Missing proof MUST quarantine, not release.
Rollback of acceptance MUST leave no job/binding/active task/operation partial rows.
Committed job + missing workspace MUST finish same job failed without launch; never reaccept it.
No inferred metadata import from orphan directories. Corrupt snapshots MUST fail closed.

### Reviewable outcome cases (AC-008-05–16)

Success shape definitions in §3 remain canonical. The worked examples below define output shapes;
QA MUST independently freeze matching concrete fixtures before A, using existing owner projections.
Required core ground truth includes the following; no self-reported worker receipt is sufficient:

| Case | Required observable result |
|---|---|
| same create request twice | same task_id, one remote_tasks row; changed title returns §3 idempotency_conflict |
| sealed empty upload | §3 Receipt bytes=0 and SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; GET Content-Length=0 |
| delete ready task | TaskSuccess state=deleted, expires_at_ms=null, artifacts=[]; payload files gone, operation/job/feedback evidence retained |
| same accepted delegate after lost ACK | same JobSuccess job_id, zero additional scorer/native invocations |
| foreign task/job/artifact | same Failure shape/code/message as unknown identity, no path/owner in output |
| native succeeds but invalid output entry | JobSuccess state=succeeded, publication=failed, publication_error code=publication_failed; no new visible descriptors |
| recovery cannot prove writer dead | task quarantined, reservation held, Failure task_quarantined on new work |
| missing operator limit | policy_unavailable before folders/DB/socket creation; no fallback value |

```mermaid
flowchart LR
  T["Trusted Python constructs synthetic principal"] --> H["ASGI test app checks closed requests"]
  H --> I["Private framed socket carries raw chunks"]
  I --> B["Existing broker enforces ownership and admission"]
  B --> D["JobStore commits task and job together"]
  B --> F["Broker owns descriptors and immutable publication"]
  B --> W["Existing watchdog checks persisted policy before START"]
```

```mermaid
stateDiagram-v2
  [*] --> creating: commit task intent
  creating --> ready: fsync folder and commit identity
  ready --> busy: atomically bind accepted job
  busy --> ready: prove termination and finish publication
  busy --> quarantined: containment uncertain
  ready --> deleting: commit explicit delete and revoke streams
  deleting --> deleted: close descriptors and unlink payloads
  deleting --> deleting: recover recorded delete intent
  deleted --> [*]
```

## 9. v3 evidence and gate disposition

Private report read: /Users/velox/.cortados-builds/cowork-mcp-verification/008-v2-technical-review.md,
reviewer quota_evaluation_qa, reviewed d4c99db and parent hash in §1. Its D=NOT READY (two confirmed auth
defects) and A=BLOCKED/not run remain historical facts; this revision does not turn them into passes.
It permits mandatory parameterized local enforcement in principle, subject to amended independent D/A.
Historical 007 four assertions passed against frozen source/data; current baseline remains 784/785.
The original ten receipt-only denials remain unverified in that original capture. A new bounded
capture has since received independent adjudication, recorded below. Authlib preparse hooks and gate
boundary remain candidates requiring qualification.
No production/exam/test/model/provider/service/credential/live/commit action was taken for this draft.

### Latest independent native evidence received during v3 authoring

Read native-exec-capture-v2/independent-adjudication.json under the private verification root.
Job ba4c3e14-c1f7-47fb-93b6-d703a98a6db7 has thirteen raw shell outcomes: two task positives,
ten synthetic filesystem denials and loopback denial. QA disposition:
BOUNDED_NATIVE_SHELL_EVIDENCE_VALID_WITH_RECORDED_DEVIATION.
strict_exact_command_pass remains false: native command added one unused literal dot script argument.
QA checked immutable script semantics; this is not a relaxation of future exact-command assertions.
Adjudication SHA256 4e3e350867f48ad50dab64a5cbea5a39af38b780ddc0250accb58e6d8ebc9444;
raw events SHA256 262b41aed6988fa954284093377de928e593a1286c062b046a13dffb56617010;
preserved failed root comparison SHA256 aa0f29a7e58e525a9bfb8190be2c63e8e017875f58bf96d591a49da1745fef88.
This closes missing shell-negative evidence only. Actual credentials/broker socket, model file-read
API, all real TMPDIR variants, document runtimes and Cowork delivery were not exercised.
The first capture is not rewritten. No full native/activation/D/A pass follows.
Root separately records current contract-only revision job 0cf07890-97dd-401c-86ac-cd8bf976a01d
under the unchanged group. This author did not start another worker or model call.

### Canonical success examples (AC-008-05)

These are synthetic shape examples, not runtime receipts. UUIDs, timestamps and policy digests are
illustrative. QA MUST bind real synthetic fixture hashes/keys and existing catalog model at execution.
For each object below, adding owner_id/cwd or changing ok to a string MUST fail invalid_request.
Job/result paths MUST remain absent. Ticket authorization shown is a noncredential placeholder;
issuing tests MUST replace it with the computed capability before any transfer.

TaskSuccess:
```json
{"ok":true,"task":{"task_id":"20000000-0000-4000-8000-000000000001","task_group_id":"53f0ea39-a61e-4307-9c2c-466c1b4b0bd1","title":"Synthetic workbook","state":"ready","created_at_ms":1000,"expires_at_ms":null,"active_job_id":null,"artifacts":[{"artifact_id":"30000000-0000-4000-8000-000000000001","filename":"empty.bin","media_type":"application/octet-stream","bytes":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","origin":"input","job_id":null,"state":"sealed","partial":false,"created_at_ms":1000}],"limits":{"file_bytes":1024,"task_bytes":8192,"installation_bytes":32768,"artifacts_per_task":10,"ticket_ttl_s":30,"control_bytes":8192,"retention":"explicit_delete"},"cleanup_error":null}}
```

JobSuccess:
```json
{"ok":true,"job":{"task_id":"20000000-0000-4000-8000-000000000001","job_id":"60000000-0000-4000-8000-000000000001","route_id":"70000000-0000-4000-8000-000000000001","state":"succeeded","created_at_ms":1000,"started_at_ms":1001,"finished_at_ms":1002,"elapsed_ms":1,"progress":"Worker completed","reason":null,"execution_policy":{"id":"cowork-task-v1","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","native_version":"0.163.0"},"publication":"complete","publication_error":null}}
```

ResultSuccess:
```json
{"ok":true,"job":{"task_id":"20000000-0000-4000-8000-000000000001","job_id":"60000000-0000-4000-8000-000000000001","route_id":"70000000-0000-4000-8000-000000000001","state":"succeeded","created_at_ms":1000,"started_at_ms":1001,"finished_at_ms":1002,"elapsed_ms":1,"progress":"Worker completed","reason":null,"execution_policy":{"id":"cowork-task-v1","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","native_version":"0.163.0"},"publication":"complete","publication_error":null},"result":{"summary":"Synthetic output completed.","worker":{"provider":"codex-cli","requested_model":"gpt-6-sol","reported_model":null,"cli_version":"0.163.0","account_ref":"native-default"},"usage":{"input_tokens":null,"cached_input_tokens":null,"cache_write_input_tokens":null,"output_tokens":null,"reasoning_output_tokens":null},"artifacts":[],"changed_paths":null,"verification":"planner_review_required","warnings":[],"error":null}}
```

FeedbackSuccess:
```json
{"ok":true,"feedback":{"job_id":"60000000-0000-4000-8000-000000000001","feedback_id":"80000000-0000-4000-8000-000000000001","seq":1,"created_at_ms":1003,"verdict":"inconclusive","verification_status":"planner_asserted"}}
```

ContextSuccess:
```json
{"ok":true,"context_requirements":{"schema":"cortados.mcp-context-requirements.v1","status":"ready","missing_fields":[],"questions":[],"scorer_chars":100,"worker_bytes":120,"scorer_limit_chars":2048,"worker_limit_bytes":4096,"scorer_digest":null}}
```

RouteSuccess:
```json
{"ok":true,"route":{"route_id":"70000000-0000-4000-8000-000000000001","selection_mode":"explicit","chosen":{"lane":"codex","provider":"codex-cli","model":"gpt-6-sol","account_ref":"native-default"},"context":{"worker_bytes":120,"scorer_chars":100,"scorer_digest":null},"quota":{"status":"fresh","source":"codex-app-server","sampled_at_ms":1000}}}
```

TicketSuccess:
```json
{"ok":true,"ticket":{"ticket_id":"90000000-0000-4000-8000-000000000001","artifact_id":"30000000-0000-4000-8000-000000000001","method":"GET","url":"https://cowork.invalid/transfers/90000000-0000-4000-8000-000000000001","authorization":"Bearer SYNTHETIC-NOT-A-CREDENTIAL","expires_at_ms":31000,"bytes":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","media_type":"application/octet-stream"}}
```

Receipt:
```json
{"ok":true,"task_id":"20000000-0000-4000-8000-000000000001","artifact":{"artifact_id":"30000000-0000-4000-8000-000000000001","filename":"empty.bin","media_type":"application/octet-stream","bytes":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","origin":"input","job_id":null,"state":"sealed","partial":false,"created_at_ms":1000}}
```

ResultSuccess before termination MUST use result=null with the same accepted job identity and its
current queued/running state. Invalid examples: Artifact bytes=-1; Ticket method=POST; Usage tokens
as a numeric string; Feedback verification_status=independently_verified. Each MUST be rejected.

### Remaining typed seam closure (AC-008-07–09,13,15–16)

[FIXED] RemoteBinding MUST be closed:
{owner_id:canonical owner string,task_id:ID,job_id:ID,task_group_id:ID,
input_manifest:ordered Artifact+BlobIdentity records,execution_policy:Policy,
workspace_identity:WorkspaceIdentity|null,config_digest:Hash,policy_snapshot:CoreConfig without key}.
Before watchdog bootstrap workspace_identity MUST be nonnull and match the generated cwd inode.
The bootstrap MUST add only remote_binding:WorkerBinding to the existing trusted remote-job payload.
WorkerBinding is the closed projection {owner_id,task_id,job_id,task_group_id,input_manifest,
execution_policy,workspace_identity,config_digest,limits}, with types inherited from RemoteBinding
and CoreLimits. It MUST omit policy_snapshot, ticket material and private roots.
No caller field may populate remote_binding. codex.native_arguments and Watchdog MUST branch solely
on this trusted persisted field; remote job classification MUST also be in persisted JobStore record.
A remote job whose binding is missing MUST fail; absence on an ordinary job retains the old branch.
Persisted policy snapshot MUST include the ticket key hash rather than key bytes; the bootstrap
projection MUST contain only the WorkerBinding fields enumerated above. Native process MUST receive no broker
socket endpoint or inherited root/staging/blob descriptor (close_fds and explicit pass_fds only).

DeleteProgress MUST be closed {state:enum[deleting,deleted],cleanup_error:Error|null}.
RecoveryResult MUST be closed {state:enum[ready,quarantined,deleting,deleted],
error:Error|null}; job interruption/result remains owned by existing Broker.recover/JobStore.
Migration MUST fail before either result on schema/integrity error.
task_store mutations MUST return the corresponding canonical row defined in §8, except accept_binding
which returns the persisted RemoteBinding. Duplicate operation MUST return its original row without
mutation. State CAS mismatch MUST raise workspace_busy. An unknown/foreign lookup MUST raise the
matching §3 not-found code. Task versions MUST increment by one per state-changing transaction.

Every config constraint and internal shape MUST be enforced by its named owner, not just by tests.
Pre-acceptance validation MUST occur before any route/model call; deterministic tests MUST inject
captured route/quota/native dependencies at existing owners and assert invocation counts.
No test-only principal field, auth environment switch or provider bypass may reach a deployable CLI.
No public service is a delivery artifact of this package.
