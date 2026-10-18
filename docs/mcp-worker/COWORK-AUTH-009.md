<!-- PURPOSE: Bound the future authentication package without authorizing execution.
     CONTRACT: RT-COWORK-AUTH-009 v3, subordinate to RT-COWORK-MCP-008.
     DEPENDS: Accepted local core; maintained FastMCP/Authlib/MCP; separate D/A.
     CEILING: Draft design only; no implementation, exams, registration or activation. -->
# Cowork future authentication contract

```yaml
contract_id: RT-COWORK-AUTH-009
contract_version: 3
status: draft
owner: Forest
implements: RT-COWORK-MCP-008 section 10 FUTURE AUTH
depends_on: [RT-COWORK-MCP-008]
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
dispatchable: false
attempt_ledger: inherited, never reset by this subordinate ID
execution_contract: COWORK-AUTH-009-EXECUTION.md
```

[FIXED] This package MUST preserve AC-008-01/02 and D-008-01/02 identities. It MUST NOT claim core, auth, or deployment acceptance. Its two documents are the entire current writable scope. Normative R rules below are proposed obligations for a future approved build, not permission to build. Definitions are [FIXED] unless explicitly marked [ESCALATE]. Contracts have no line cap under this assignment; prospective source responsibilities MUST each remain below 500 physical lines.

## 1. Authority, status and evidence

Governing inputs read: `/Users/velox/cortados/contracts/CONVENTIONS.md`; `CONVENTIONS.md`, `RUBRIC.md`, `ORCHESTRATION.md`, `PLAYBOOK.md`, and `standards/00-ENGINEERING-STANDARD.md` beneath `/Users/velox/cortados/context-staging/research/ai-coding-std-workflow/` (governing revision `ab1b521047dcabc1b610caa157e5dfbd544f589b`). Project-local parent: [008](COWORK-MCP-008.md) §§2–4 and 10, [execution](COWORK-MCP-008-EXECUTION.md), [decisions](COWORK-MCP-008-DECISIONS.md), [root review](COWORK-MCP-008-ROOT-REVIEW.md). The explicit user-selected ID/location override generic CTX/1NN/single-contract-home naming; they do not waive gates.

Evidence root **V** = `/Users/velox/.cortados-builds/cowork-mcp-verification`. Library root **F** = `/Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp`. Installed research root **S** = `V/auth-research-venv/lib/python3.14/site-packages`. These are read-only evidence aliases, not runtime paths or grants. Revision authority is Router commit 5d98d77, “Auth technical closure directions after independent maintained baseline”; earlier 495973c decisions remain history only where consistent with v3. No original repo edits or workers are authorized.

| Evidence | Meaning retained |
|---|---|
| `V/core-v3-D/FULL-D-REVIEW.md`; e055c51 | Core v3 D=21, A not run. No production failure count inferred. |
| `V/core-v4-D/readiness.json`; 65e2dbb | Core v4 D=3; V4-01 composition and V4-02 examples. |
| `V/core-v4-D-closure/readiness.json`; 1637088 | Independent core D=0; parent SHA256 `89314886b6aaf26c7070d7b82efafcc2699d6e6b92e610d94d648c3e793a2f5c`; A=PENDING_INDEPENDENT_EXAMS; production changes=0. |
| Current assignment | Frozen core008 D0/A0 and protected exams are read-only dependencies; core B is running separately. Auth build requires accepted core B, not merely D0/A0. |
| `V/008-v2-technical-review.md` | D-008-01: claims hook runs after parsing/writes and misses transparent refresh. D-008-02: one process does not serialize redemption. Neither defect is erased by 009. |
| `V/provider-preflight.md` | GitHub OAuth App `offline_access` is an available candidate; expiring user-token GitHub App is an alternative. No live account/app decision. |
| `V/auth-flow-qualification/{REPORT.md,qualification.py,concurrency.py}` | 81 measured maintained-method cases: 78 malformed rejects before observed parsing/writes, 3 valid controls. Setup included 162 mock calls; concurrency script 4 including setup. Forced duplicate code and refresh exchanges each succeeded twice. |
| Root-confirmed investigation job `4540efb6-75b9-409b-9e85-3c45f21fa08e` | Root independently reran with socket.connect forbidden: zero connects. This is supplied root evidence, not a run by this author. |

Assignment history remains group `53f0ea39-a61e-4307-9c2c-466c1b4b0bd1`: author jobs `a21a86cf-d936-4be9-af76-ddec5048e479`, `5c236b3c-ffc4-4526-ad03-dbf5da0ac957`, `0cf07890-97dd-401c-86ac-cd8bf976a01d`, `162d277f-c2dd-48e8-b1ed-f663af7caa22`. Root-review feedback records draft acceptance only. This authoring is not a valid production attempt. Missing fixtures/environment are blocked, not failed builds. A second valid failure of the same design still triggers design_error; changing package/model never resets it.

This v3 revision is directed by root commit 5d98d77. The explicit current prohibition on workers overrides the general delegation workflow; no worker was launched. Only the two isolated copied drafts are writable; all original-repository references are read-only. No delegation, network, installs, model calls, credential reads, probes, acceptance tests or commits were performed. Existing concurrent `acceptance/cowork_mcp/` work is outside scope.

## 2. Maintained owners and pins

Observed searches: `rg --files integrations/mcp`, `rg -n 'CoreRuntime|CoreClient|create_test_app|load_core_config' docs/mcp-worker/COWORK-MCP-008.md`, and AST extraction of the pinned library files. The frozen core008 contract and protected exam conventions are the integration reference; concurrently building core source is not inspected or used as authority. Do not treat a contract signature as an implemented import.

| Responsibility | Disposition / sole owner |
|---|---|
| OAuth consent, state, PKCE, code issuance, JWT issuance, refresh transitions | ADOPT FastMCP GitHubProvider → OAuthProxy → MCP handlers / Authlib. No local OAuth engine. |
| Strict resource, temporal, owner/client and safe-error policy | BUILD NEW thin adapters; maintained defaults demonstrably omit these constraints. |
| Credential storage | ADOPT explicit maintained FileTreeStore + Fernet wrapper + provider PydanticAdapters. No second token database. |
| Auth serialization and startup refusal | BUILD NEW outer lifetime owner; maintained per-upstream lock excludes explicit code/refresh races. |
| Tool transport and byte transfer | EXTEND only accepted remote_http composition; one SDK manager and original transfer owner. |
| Core lifecycle, jobs, capacity/quota and native subscription billing | ADOPT 008 and existing broker. Auth never owns a task queue, job retries, quota or model billing; its finite credential waiter gate is separate. |
| stdio and training | ADOPT unchanged. No remote dependency import on ordinary startup. |

FastMCP reference HEAD: `83c9764e0bc2d9d32910b82264c09370da24ab7a`. The qualification report confirms installed github/proxy hashes equal reference. Exact examined hashes:

| Path relative to F, except S entries | SHA256 |
|---|---|
| server/auth/providers/github.py | `13a89114465210ba0fcce39b4ff3e7acb09a699a8a5de0e97223c419f53141ba` |
| server/auth/oauth_proxy/proxy.py | `58f13a58bd31373c13ee166f94bf3726dadd6bb86a6c8da89de3b09a627d26f0` |
| server/auth/oauth_proxy/models.py | `b2f45bc919484355f89ad10bf06d84cf45ebf5ed9af51b932ea0800c665242f8` |
| server/auth/oauth_proxy/consent.py | `2810a5bb3df3f1548b04601621d7b9363b88f711991696974244db91ffc823f3` |
| server/auth/jwt_issuer.py | `9d3bceb3ffb9aa86bf080f7e0a972aeddc7a87e75b483be6af4bf4dc94f7bd9e` |
| server/auth/auth.py | `94405db4b54afb041e41c7ba70ac31781f67f1f769c017d9213238edd0cd8b86` |
| S/mcp/server/auth/handlers/token.py | `cade6452f07cce106038da586493904c5ca82590610b450a3d4137190324a2db` |
| S/authlib/integrations/httpx_client/oauth2_client.py | `24c0c3dd6b05c8daeb9d781ce6ceef6c64107ae25a881076fefb82a5995ae463` |

Resolved research versions: fastmcp-slim=3.4.9, Authlib=1.9.1, mcp=1.30.0, httpx=0.28.1, py-key-value-aio=0.4.6, starlette=1.8.0, cryptography=50.0.2, pydantic=2.14.1, uvicorn=0.54.0. Complete transitive freeze: `V/auth-research-freeze.txt`. This is a research environment, not an approved production lock. A separately reviewed remote extra/lock MUST preserve ordinary installed environments.

### Exact called interfaces

The following are observed signatures, not reconstructed SDK APIs. Types retain their owning imports. `self` is omitted.

| Symbol and source | Signature / use |
|---|---|
| github.py:206 GitHubProvider | Keyword-only `client_id:str, client_secret:str, base_url:AnyHttpUrl\|str, resource_base_url:AnyHttpUrl\|str\|None=None, issuer_url:AnyHttpUrl\|str\|None=None, redirect_path:str\|None=None, required_scopes:list[str]\|None=None, timeout_seconds:int=10, cache_ttl_seconds:int\|None=None, max_cache_size:int\|None=None, allowed_client_redirect_uris:list[str]\|None=None, client_storage:AsyncKeyValue\|None=None, jwt_signing_key:str\|bytes\|None=None, require_authorization_consent:bool\|Literal['remember','external']=True, consent_csp_policy:str\|None=None, forward_resource:bool=True, fallback_refresh_token_expiry_seconds:int\|None=None, fastmcp_access_token_expiry_seconds:int\|None=None, token_expiry_threshold_seconds:int=0, http_client:httpx.AsyncClient\|None=None, enable_cimd:bool=True`. |
| proxy.py:719 | `_create_upstream_oauth_client()->authlib.integrations.httpx_client.AsyncOAuth2Client`; MUST construct the maintained client through §12.1, then register both compliance hooks before returning; the v2 super-only construction is superseded to set supported HTTPX constructor policy. No constructor extra_authorize_params on GitHubProvider. |
| consent.py:277 | `_build_upstream_authorize_url(txn_id:str, transaction:dict[str,Any])->str`; MUST copy transaction, replace copied scopes with identity scopes plus offline_access, then call super. |
| proxy.py:838 | `async register_client(client_info:mcp.shared.auth.OAuthClientInformationFull)->None`; persists public ProxyDCRClient, not confidential client credentials. |
| proxy.py:1027,1075 | `async load_authorization_code(client:OAuthClientInformationFull,authorization_code:str)->AuthorizationCode\|None`; `async exchange_authorization_code(client:OAuthClientInformationFull,authorization_code:AuthorizationCode)->OAuthToken`. |
| proxy.py:1384,1420 | `async load_refresh_token(client:OAuthClientInformationFull,refresh_token:str)->RefreshToken\|None`; `async exchange_refresh_token(client:OAuthClientInformationFull,refresh_token:RefreshToken,scopes:list[str])->OAuthToken`. |
| proxy.py:1797,2008,2146 | `async load_access_token(token:str)->AccessToken\|None`; `get_routes(mcp_path:str\|None=None)->list[Route]`; `async _handle_idp_callback(request:Request)->HTMLResponse\|RedirectResponse`. |
| jwt_issuer.py:218 | `verify_token(token:str,expected_token_use:str='access')->dict[str,Any]`; reuse signature checks, add stricter claim checks. Never call issue_* from adapter. |
| Authlib oauth2_client.py:177–180,195–198 | `register_compliance_hook('access_token_response',hook)` and `register_compliance_hook('refresh_token_response',hook)` run synchronous `hook(httpx.Response)->httpx.Response` before `parse_response_token`. |
| MCP TokenHandler:93–232 | `async handle(request:Request)` authenticates client before code lookup :133 / exchange :189 and refresh lookup :199 / exchange :232. Gate MUST wrap endpoint entry, not exchange methods. |
| auth.py:329 | `get_middleware()->list` uses AuthenticationMiddleware/backend=BearerAuthBackend(verifier), then AuthContextMiddleware. Adapter replaces verifier only. |
| MCP 1.30 transport | `StreamableHTTPSessionManager(app=lowlevel_server,json_response=True,stateless=True)`; lifespan enters `manager.run()`; POST routes to `manager.handle_request(scope,receive,send)`. Adopt accepted 008 interface. |

## 3. Typed adapter boundary and compact examples

All local records are closed, required-field records. Local ingress/config/marker and token-response JSON reject duplicate keys, unknown local record keys, nonfinite values, surrogate text and bool-as-int. Stored maintained records use the explicit §12.2 byte preflight and decoded validation boundary, not a dict-level duplicate-key claim. N/P/ID/Hash/Principal/CoreLimits/Owner are exactly 008 §2; identity remains strings. Epoch times below are UTC seconds, not core millisecond fields. Monotonic time measures waits only.

| Record | Exact fields / constraints |
|---|---|
| AuthConfig | `schema:'cortados.cowork-auth.v1', origin:HTTPSOrigin, issuer:Nonblank, resource:Nonblank, installation_id:ID, github_subject:decimal-string ^[1-9][0-9]*$, downstream_client_id:Nonblank, redirect_uri:'https://claude.ai/api/mcp/auth_callback', enabled:bool, identity_scopes:tuple[], auth_root:absolute-path, upstream_client_id:Nonblank, upstream_secret_ref:Nonblank, signing_key_ref:Nonblank, storage_key_ref:Nonblank, control_bytes:P, gate_wait_ms:P, operation_ms:P, response_bytes:P, max_waiters:P, upstream_body_bytes:P, upstream_chunk_bytes:P, upstream_header_bytes:P, storage_record_bytes:P`. Secret references are opaque operator-resolver names, never values or caller paths. |
| VerifiedClaims | `iss:str=issuer, aud:str=resource, token_use:'access', exp:P, iat:N, client_id:str=downstream_client_id, jti:Nonblank`; optional `nbf:N`. Require exp>now, iat<=now, nbf<=now, iat<exp. Other maintained JWT claims may exist but are not copied. |
| AuthorizationFacts | `principal:Principal, resource:str, expires_at:P`; principal issuer=canonical issuer, subject=verified live GitHub decimal ID, installation_id=config ID, client_id=verified downstream ID, enabled=true. No credential. |
| AuthEvent | `schema:'cortados.cowork-auth-event.v1', event_id:ID, at_ms:N, event:enum[starting,ready,refused,failed,closed], reason:enum[none,invalid_request,invalid_token,owner_disabled,upstream_unavailable,storage_unavailable,operation_interrupted,gate_exhausted]`. No payload/URI/path/credential/exception. |
| AuthFailure | `error:enum[invalid_request,invalid_client,invalid_grant,temporarily_unavailable], error_description:'Authentication request refused'`. Resource 401 uses fixed `invalid_token` challenge instead of this OAuth token endpoint body. |
| AuthRuntime (not serialized) | Single process-owned provider and gate, AuthStorage handle (sole lock/marker owner), guarded identity client, state in enum[starting,ready,stopping,failed,closed]. No token serializer or credential-bearing lifecycle control channel. |

Constructor invariants: issuer MUST equal `str(provider.base_url)` after Pydantic normalization, including root slash; resource MUST equal exact origin + `/mcp`. Origin has no userinfo/path/query/fragment. No default live origin, subject, limits, retention, app or client. Local fixtures supply every parameter; synthetic numbers are not deployment policy.

Prospective APIs (new local APIs, not claims that files exist):

| Owner | Signature and effects |
|---|---|
| remote_auth_config | `load_auth_config(raw:bytes)->AuthConfig`; pure validation, no credentials/files opened. |
| remote_auth_provider | `class StrictGitHubProvider(GitHubProvider)` overrides only the two client-factory/authorize hooks in §2; `validate_token_response(response:httpx.Response,*,response_bytes:int,now_s:int)->httpx.Response`; no gate acquisition. |
| remote_auth_claims | `validate_claims(raw:dict[str,object],config:AuthConfig,now_s:int)->VerifiedClaims`; `project_facts(claims:VerifiedClaims,live:AccessToken,config:AuthConfig)->AuthorizationFacts`; pure, no upstream calls. |
| remote_auth_runtime | `AuthRuntime(*,config:AuthConfig,secrets:ResolvedAuthSecrets,dependency_sha256:str,on_failure:Callable[[],None])`; async enter/exit; `async verify_token(token:str)->AccessToken\|None`; `wrap_oauth(endpoint:ASGIApp)->ASGIApp`; owns single gate and operation settlement; delegates storage lifetime exclusively to AuthStorage. ResolvedAuthSecrets is a private in-memory closed record `{upstream_secret:str,signing_key:bytes,storage_key:bytes}`, never JSON/repr/logged. |
| remote_auth_errors | `safe_oauth_app(app:ASGIApp,*,config:AuthConfig)->ASGIApp`; bounded response filter and fixed local event emission; no protocol minting. |
| remote_service | `create_remote_app(*,core_client:CoreClient,principal:Principal,frontend:FrontendConfig,auth:AuthRuntime)->ASGIApp`; calls the accepted single HTTP composition seam; does not construct another tool server. |

Example notation: `ref(E)` expands the named complete example; `replace(E,field,value)` changes one field; `omit(E,field)` removes one. These are documentation operators, never runtime JSON. QA expands them in its own fixture; nested constituents MUST NOT be recopied into multiple documents.

| Example | Valid value / invalid variant |
|---|---|
| E1 upstream success (recorded probe shape) | `{"access_token":"SYNTHETIC-ACCESS","refresh_token":"SYNTHETIC-REFRESH","token_type":"bearer","expires_in":3600,"refresh_token_expires_in":7200}`; invalid `replace(E1,expires_in,true)` or `omit(E1,refresh_token)`. |
| E2 normalized response | E1 with refresh_token_expires_in removed and refresh_expires_in=7200; invalid both aliases unequal. |
| E3 claims at now=1000 | `{"iss":"https://cowork.invalid/","aud":"https://cowork.invalid/mcp","token_use":"access","exp":4600,"iat":1000,"client_id":"synthetic-claude","jti":"synthetic-jti"}`; invalid omit exp, aud array, aud ending slash, nbf=1001 or exp=true. |
| E4 Principal | `{"issuer":"https://cowork.invalid/","subject":"42","installation_id":"11111111-1111-4111-8111-111111111111","client_id":"synthetic-claude","enabled":true}`; invalid subject=42 or an HTTP-supplied owner. |
| E5 AuthorizationFacts | `{principal:ref(E4),resource:'https://cowork.invalid/mcp',expires_at:4600}`; invalid extra upstream_token field. |
| E6 local event | `{"schema":"cortados.cowork-auth-event.v1","event_id":"22222222-2222-4222-8222-222222222222","at_ms":1000000,"event":"refused","reason":"invalid_token"}`; invalid extra exception field. |
| E7 OAuth error | `{"error":"invalid_grant","error_description":"Authentication request refused"}`; invalid provider-supplied description. |
| E8 upstream error input | `{"error":"invalid_grant","error_description":"SYNTHETIC-SECRET-SENTINEL"}`; invalid error array. Both produce no successful token state; sentinel never reaches logs or response. |
| E9 AuthConfig | All fields in §3: schema literal; origin/issuer/resource from E3; installation/client from E4; github_subject='42'; exact hosted redirect; enabled=true; identity_scopes=[]; auth_root='/synthetic/private/auth'; upstream_client_id='synthetic-upstream'; refs='synthetic-secret','synthetic-sign','synthetic-storage'; control_bytes=65536; gate_wait_ms=1000; operation_ms=5000; response_bytes=65536; max_waiters=2; upstream_body_bytes=131072; upstream_chunk_bytes=65536; upstream_header_bytes=167936; storage_record_bytes=1048576. Invalid omitted github_subject, origin with path, shared task/auth root, or signing/storage reference equality. Values are exam-only. |

## 4. Credential lifetime and storage

R1 [FIXED, AC-008-02] The response hook MUST validate every successful response before Authlib parses it, because late validation already allowed persistence and entirely missed transparent refresh (D-008-01). Success requires object JSON, nonblank access_token and refresh_token strings, token_type string case-insensitively equal to bearer, strict integer expires_in and refresh lifetime in 1..9007199254740991. This integer bound is the inherited interoperable P range, not an invented TTL. Either expiry alias is allowed; both must be strict equal integers. Normalize to refresh_expires_in. Optional scope must be a string. Optional expires_at must be removed before parsing so provider-controlled absolute time cannot override validated expires_in. Unknown success fields are discarded; no id_token is retained. Body/status/size/JSON failure raises only a fixed local error. HTTP non-2xx or an error member takes the fixed failure branch, never successful parsing or durable mutation. No fallback lifetime or fabricated refresh is permitted.

R2 [FIXED, AC-008-02] `_create_upstream_oauth_client` MUST install the hook for both response events on the §12.1 maintained client, because callback, explicit refresh and `_try_transparent_refresh` all use that seam. `_extract_upstream_claims` MUST NOT enforce lifetimes. Downstream code redemption performs no upstream fetch; it trusts callback state, so corrupted stored ClientCode must refuse through storage/schema validation rather than synthesize a response.

R3 [FIXED, AC-008-02] AuthRuntime MUST acquire one process-wide async gate before any credential load for callback, authorization/consent, token exchange, or resource verification. It MUST hold the gate through every maintained read/exchange/write/delete and failure settlement. Wrappers must surround returned OAuth ASGI endpoints, before SDK client authentication; resource verification acquires at `AuthRuntime.verify_token` before JWT/store lookup. Inner provider hooks MUST NOT reacquire. Discovery reads no credentials. No public register/revoke endpoint is mounted in this profile. Future registration/restore commands require the exclusive process lock while the service is stopped. This prevents D-008-02's load-before-lock race without rewriting transitions.

R4 [FIXED, AC-008-01/02] No core/task/DB lock may be held while waiting for auth. Resource verification MUST release auth gate before CoreClient dispatch or any transfer bytes. The gate MUST NOT enclose a job lifetime. A bounded gate wait refuses with fixed temporarily_unavailable; a waiter never starts an exchange after its request deadline. Gate wait and operation durations are mandatory measured parameters. No worker/auth automatic retries follow refusal. This prevents a slow GitHub call from becoming a job queue owner.

R5 [FIXED, AC-008-02] A cancelled/timed-out operation after credential access, uncertain upstream effect, storage error or partial mutation MUST transition AuthRuntime to failed before gate release. New protected requests MUST refuse until explicit operator recovery. In-flight operation settlement MUST precede releasing the exclusive root lock. A process crash or unproven settlement MUST never be described as a completed rollback or refresh-family revocation. Root at 5d98d77 narrowed the lifetime-marker guarantee in §8: exact clean-state/manifest restart is supported; observed uncertainty fails the live supervisor and suppresses relaunch. This does not detect all unobservable failed barriers after all-process loss. No reset/re-registration/recovery ritual is authorized here.

Exact maintained FileTree/Fernet/Pydantic construction and guards are §12.2. AuthStorage alone constructs/closes the underlying FileTreeStore, owns its exclusive lock, and seals the lifetime after AuthRuntime proves settlement. There is no provider.close API. Per-call Authlib clients retain maintained `_upstream_oauth_client` finally/aclose ownership; the real identity AsyncClient is entered once and awaited closed by AuthRuntime after active operations settle. No implicit default store or plaintext fallback is allowed.

| Store aspect | Contract |
|---|---|
| Class | Provider auth records are Canonical; loss requires reauthentication, not projection rebuild. Immutable synthetic captures are Evidence. Human reports are Views, never runtime input. No new token projection. |
| Write | OAuthProxy alone owns six PydanticAdapter collections; adapter supplies serialization/lifetime only. FileTree writes are multi-record, not a transaction. Its writer fsyncs each temporary file before rename; directory and multi-record durability require section 8 qualification. |
| Query | Maintained keyed get/put/delete under gate. The seven direct-method call budgets are frozen in EXECUTION §5; wrapper and barrier costs are separately measured after build. No live latency/RSS SLO is inferred. |
| Mutability | Provider rotates/upserts/deletes credentials with its TTLs. Operator disable is a service policy latch; no local token-family mutation. No automatic purge/repair on corruption. |
| Identity | Maintained random txn/code/upstream/JTI keys; refresh metadata keyed by maintained SHA256(token). Owner identity uses existing canonical([issuer,subject,installation_id]); no new owner algorithm. |
| Rebuild | n/a—canonical. Backup only with service stopped and exclusive lock, entire auth directory + separately secured matching keys + config/dependency identity. Restore remains disabled until canary/integrity/old-token refusal requirements pass; stale-backup restore is excluded by section8, not an ordinary restart. |
| Failure | Missing/decrypt-invalid/type-invalid referenced row refuses. Partial writes or uncertain network effect fail the lifetime. Restart cannot presume clean multiwrite state. No automatic retry, rollback, deletion or restore. |

Exact provider records (models.py, inherited defaults retained only inside dependency): OAuthTransaction stores txn_id/client_id/client_redirect_uri/client_state/code_challenge/code_challenge_method/scopes/created_at plus resource/proxy_code_verifier/csrf_token/csrf_expires_at/consent_token nullable fields. ClientCode stores code/client_id/redirect_uri/code_challenge/code_challenge_method/scopes/idp_tokens/expires_at/created_at. UpstreamTokenSet stores upstream_token_id/access_token/refresh_token/refresh_token_expires_at/expires_at/token_type/scope/client_id/created_at/raw_token_data. JTIMapping stores jti/upstream_token_id/created_at. RefreshTokenMetadata stores client_id/scopes/expires_at/created_at. ProxyDCRClient extends OAuthClientInformationFull with allowed_redirect_uri_patterns/client_name/cimd_document/cimd_fetched_at and excluded allow_unregistered_redirect_uris.

Collection names respectively: `mcp-oauth-transactions`, `mcp-authorization-codes`, `mcp-upstream-tokens`, `mcp-jti-mappings`, `mcp-refresh-tokens`, `mcp-oauth-proxy-clients`. These are private secret-bearing library records, not public MCP output or newly owned schemas. Full types are pinned to models.py hash. QA captures actual serialized examples through maintained methods using E1/E3/E4 and pairs each with a missing required key/corrupt ciphertext negative; no handwritten replacement serializer.

Maintained lifetimes are adopted, not locally minted: transaction TTL=900 seconds (proxy.py:994), consent CSRF lifetime/transaction renewal=900 seconds (consent.py:365,406–412,522), authorization-code lifetime=300 seconds (models.py DEFAULT_AUTH_CODE_EXPIRY_SECONDS). Upstream access and refresh expiration derive only from R1's actual durations and the maintained clock. With fastmcp_access_token_expiry_seconds=None, downstream access mirrors the validated upstream duration; JTI TTL follows the corresponding token, upstream-store TTL is max(access,refresh,downstream-access,1), and refresh metadata follows refresh duration (proxy.py:1211,1251–1274,1573–1654). Client registration persists without a locally imposed expiration. Expiry is not proof of secure file erasure. No refresh-family reuse revocation, sliding session life, automatic cleanup schedule or new lifetime default is promised. Q MUST exercise equality boundaries against the pinned provider rather than silently changing its comparisons.

R6 [FIXED, AC-008-02] Auth root MUST be an existing operator-owned private 0700 directory outside task/blob/staging roots, with no symlink ancestors and 0600 regular secret/state files. AuthStorage MUST acquire an exclusive nonblocking no-follow lifetime lock before constructing storage/provider; a second process refuses before reading credentials. Startup validation failure has no listener or core dispatch. Storage/key rotation MUST stop admission; no old-session continuity is promised. Filesystem durability qualification and backup policy are explicit gates, not invented FileTree guarantees.

```mermaid
flowchart LR
  C["Claude sends OAuth or MCP request"] --> P["Policy validates exact request"]
  P --> G["AuthRuntime serializes credentials"]
  G --> M["Maintained OAuthProxy owns protocol"]
  M --> H["Authlib hook validates before parsing"]
  H --> U["GitHub verifies upstream identity"]
  M <--> ST["AuthStorage guards and seals maintained store"]
  ST <--> S["Canonical encrypted auth collections"]
  M --> T["Guarded HTTPX reads before decoder"]
  T --> U
  G --> A["Adapter emits credential-free facts"]
  A --> I["CoreClient dispatches after gate release"]
  I --> D["Canonical core jobs and artifacts remain core-owned"]
  G --> SUP["Supervisor closes admission and awaits core revocation"]
  SUP --> D
  G --> E["Evidence records fixed safe events"]
  E --> V["View reports for human review"]
  classDef canonical fill:#ffe6b3,stroke:#745000
  classDef evidence fill:#d9edff,stroke:#195681
  classDef view fill:#eeeeee,stroke:#555555
  class S,D canonical
  class E evidence
  class V view
```

```mermaid
stateDiagram-v2
  [*] --> Starting: validate config and exclusive ownership
  Starting --> Ready: verified clean store then durable dirty marker
  Starting --> Failed: dirty missing corrupt or mismatched marker
  Ready --> Ready: serialized operation fully settles
  Ready --> Failed: partial write or uncertain cancellation
  Ready --> Stopping: stop admission and drain
  Stopping --> Closed: flush store then durable clean marker then release lock
  Stopping --> Failed: uncertain settlement or flush failure
  Failed --> Closed: retain evidence and stop
  Closed --> [*]
  note right of Failed
    Refuse credentials; never repair or retry automatically.
    Operator recovery is a separate approved action.
  end note
```

```mermaid
sequenceDiagram
  participant C as Claude request
  participant A as AuthRuntime gate
  participant P as Maintained provider
  participant S as Encrypted canonical store
  participant B as Existing CoreClient and broker
  C->>A: bearer or token exchange
  A->>A: acquire before credential lookup
  A->>P: maintained endpoint or load_access_token
  P->>S: reads then multiple writes and deletes
  Note over P,S: Serialized, not transactionally atomic
  P-->>A: result or failure
  A->>A: sanitize and settle; release gate
  A->>B: verified MCP call only
  B-->>C: existing core result
  Note over A,B: No auth lock across broker work; no token over IPC
```

## 5. Identity, ingress and safe errors

R7 [FIXED, AC-008-01] AuthRuntime MUST first verify the proxy JWT signature with maintained JWTIssuer, then enforce VerifiedClaims, then call maintained load_access_token under the same gate. It MUST compare live `AccessToken.claims['sub']` to the configured decimal GitHub subject. GitHub verifier's returned client_id is the GitHub ID, not downstream client identity (github.py:150–166); the adapter MUST use JWT client_id for the latter. Missing/live foreign subject denies, even with a valid signature. Raw GitHub/PAT/ID/refresh tokens are never alternate resource authenticators.

R8 [FIXED, AC-008-01] The SDK-facing AccessToken MUST be newly projected with token equal to the incoming proxy bearer, client_id from verified claims, scopes=[], expires_at=proxy exp, resource=exact resource, subject=live subject and claims containing only canonical iss/sub/installation_id. It MUST NOT copy the upstream token or user JSON. AuthorizationFacts alone cross the trusted composition boundary; core receives its existing Principal, not any bearer. Owner rollover retains issuer/subject/installation; client change requires stopped-service reconfiguration/rebind, never a caller-selected owner.

R9 [FIXED, AC-008-01/02] Construction MUST use required_scopes=[], cache_ttl_seconds=None, consent=True, forward_resource=False, enable_cimd=False, exact callback allowlist and explicit encrypted store. get_routes('/mcp') MUST run once before JWT verification. Registration enabled MUST be set false before routes are made; `/register` MUST be absent. The synthetic upstream-ID client MUST be denied by the distinct downstream client allowlist. offline_access is an upstream authorize hint only, not an API scope requirement or a repository grant. `/user/repos` is a maintained verification call, not a request for repo permission. A live failure with minimal permissions is an activation blocker, not permission to broaden scopes.

Exact remaining constructor operands: base_url=origin, resource_base_url=origin, issuer_url=origin, redirect_path='/auth/callback', client_id=upstream_client_id, client_secret=resolved upstream secret, jwt_signing_key=resolved signing bytes, client_storage=explicit encrypted wrapper, allowed_client_redirect_uris=[config.redirect_uri], require_authorization_consent=True, fastmcp_access_token_expiry_seconds=None, token_expiry_threshold_seconds=0. `provider.client_registration_options.enabled=False` precedes get_routes. No fallback expiry is deliberately selected; R1 makes missing duration unreachable as success. Pass the runtime-owned guarded real identity_client as http_client; v2's None/fresh-client clause is superseded. fallback_refresh_token_expiry_seconds=None, required_scopes=[], cache_ttl_seconds=None and enable_cimd=False are explicit. Provider timeout_seconds=10 is an unused maintained fresh-client default when http_client is supplied; actual client timeouts follow §12.1 and must be remeasured.

R10 [FIXED, AC-008-02] Ingress MUST reject duplicate authorize/token fields before maintained parsing. Authorize requires exactly one resource equal byte-for-byte to config.resource, response_type=code, the configured client_id and redirect_uri, code_challenge_method=S256 and 43-character base64url SHA256 challenge. Token code grant requires exact client/resource/redirect and verifier; refresh grant requires exact client/resource and refresh_token. Maintained handlers MUST still verify client binding, code, redirect, verifier and scopes. No URL normalization may broaden policy; `/mcp/`, changed port/query/fragment and array audiences deny. The adapter MUST bound form/body bytes using configured control_bytes before parsing. Root at 495973c approved the section 9 correction to advertise none and S256 only.

R11 [FIXED, AC-008-01] All `/mcp` methods MUST authenticate before method dispatch, including initialization/listing. After auth, accepted core POST-only behavior remains; other methods return the core 405. No second `/mcp` route or FastMCP tool server is allowed. Discovery/OAuth protocol routes remain public within their exact method table. Transfers use existing core ticket capability, not an added OAuth bearer requirement; the ticket's Authorization header cannot be interpreted as a proxy JWT. Public origin/Host/Origin policy and trusted ingress configuration MUST refuse unknown origins; forwarded identity headers confer no authority.

R12 [FIXED, AC-008-01/02] Dependency logs from fastmcp.server.auth, mcp.server.auth, authlib, httpx, httpcore and the entire key_value namespace MUST be dropped before handlers, including exception/traceback text; ASGI/ingress access logs MUST omit query/body/Authorization/Cookie. Only AuthEvent is emitted locally once per transition/refusal. Fixed-safe response filtering MUST cover callback HTML errors, token JSON errors, OAuth error redirects and uncaught failures before bytes are sent. Redirect error replacement may preserve state only after maintained validated redirect binding; no untrusted Location is echoed. Success protocol fields remain unchanged. Error HTML becomes fixed text; token errors use E7; resource denial uses SDK 401 with build_resource_metadata_url(resource) and no token detail. Response buffering is limited by response_bytes; overflow refuses without leaking partial bytes. Sentinel captures across logs, stderr, headers, body and exception paths are required because debug-off does not remove error-level leaks at proxy.py:1100,1479,2328–2331.

R13 [FIXED, AC-008-01/02] Owner disable MUST refuse new authenticated admission and invoke accepted CoreRuntime.disable_owner(owner_id) to revoke tickets/close streams. Accepted jobs retain their original lifetime and operator cancellation path. Expiry, auth failure, frontend loss and reconnect MUST NOT cancel, duplicate or retry a job. GitHub authenticates connector owner only; native execution continues through existing subscription and fresh quota admission without API-key fallback.

## 6. Accepted-core integration amendment (O2 resolved at 495973c)

[FIXED] Root approved the distinct oauth_service validator/trusted constructor and one guarded HTTP composition. These are future signatures, not implemented imports. Root must amend accepted 008 with these exact semantics before B2; this isolated copy cannot change the core contract.

| Sole owner | Exact prospective signature / behavior |
|---|---|
| remote_config | load_service_core_config(raw:bytes,base:BaseConfig,*,principal:Principal)->CoreConfig |
| remote_runtime | CoreRuntime(*,base_raw:bytes,core_raw:bytes,manifest_raw:bytes,trusted_service_principal:Principal\|None=None); existing async enter/exit, attach and disable_owner signatures remain unchanged |
| remote_http | compose_http_app(*,core_client:CoreClient,principal:Principal,config:FrontendConfig,mcp_guard:Callable[[ASGIApp],ASGIApp],extra_routes:tuple[Route,...])->ASGIApp |

The service loader accepts exactly the existing core raw schema, except mode must equal oauth_service; principal must equal the trusted argument in all fields and installation_id must equal principal.installation_id. It replaces only synthetic principal/origin restrictions. Common validation remains single-owned: limits, retention, root separation, base.state_dir equality, manifest/native policy and snapshot digest are unchanged. No translation into local_test bytes is allowed. load_core_config continues to accept only local_test with its synthetic restrictions. CoreRuntime's constructor remains effect-free: async enter selects the original loader when trusted_service_principal is None, otherwise the service loader; mismatched mode refuses before lock/DB. Errors retain the parent policy_unavailable mapping.

The trusted launcher constructs Principal from validated AuthConfig (issuer, github_subject, installation_id, downstream_client_id, enabled), compares it against raw core principal and requires core.transfer_origin = auth.origin before either runtime has effects. R7 verifies the same identity live on every authenticated request. HTTP fields never construct the trusted principal. FrontendConfig, PolicySnapshot and config_digest remain core-owned and mode remains excluded from the snapshot as in 008; mode is not an alternate permission.

compose_http_app is the sole lowlevel server, manager, descriptor and transfer composition owner. It adopts one already-bound CoreClient exactly once. create_test_app delegates with identity guard and empty routes, preserving every local_test byte/route behavior. create_remote_app delegates with section 9 routes and the MCP guard. Guard wraps the complete /mcp method dispatcher, including 405; it never wraps /transfers. Extra route paths must be unique and cannot collide with /mcp or /transfers namespaces. OAuth query validation belongs to its route before the core's no-query rejection; core paths retain that rejection. No catch-all bypass or second server is allowed.

remote_supervisor preserves inherited socketpair process construction: broker enters CoreRuntime, attaches server endpoint; frontend constructs CoreClient and awaits bind; then app construction. AuthRuntime enters before app admission; app lifespan enters the one manager.run(), then awaits parent admit before admitting requests. Shutdown stops admission, drains auth, exits manager, closes frontend CoreClient and finally exits AuthRuntime. Failed start unwinds acquired owners in reverse order, never marks auth clean after an uncertain operation. Frontend loss does not invoke broker shutdown or cancel accepted jobs. remote_supervisor alone owns CoreRuntime shutdown; §12.3 freezes its private lifecycle channel.

CoreRuntime.disable_owner(owner_id) remains the only ticket/stream disable owner. Auth marks disabled before the trusted supervisor awaits that call; no new public tool, transfer route or CoreClient opcode is added. The private §12.3 lifecycle channel carries this instruction; core IPC remains unchanged.

## 7. Stable acceptance identity and root decisions

Historical source `git show d4c99db:docs/mcp-worker/COWORK-MCP-008-EXECUTION.md` rows 008-01/02 is retained verbatim below under their stable AC aliases. Current parent v4 names AC-008-01/02 but has no replacement canonical full rows. These are historical wording, not a claim the old gate split is still sufficient:

| Stable ID | Historical local/prebuild text | Historical activation text |
|---|---|---|
| AC-008-01 | Real loopback ASGI discovery/401 challenge, all MCP methods auth, exact port/path/resource; PAT/ID/refresh/wrong issuer/audience/expiry denied | Actual hosted discovery/callback |
| AC-008-02 | Maintained-provider local fixtures: S256 only, exact redirect, duplicate/resource rejection, no positive cache, bad-secret logs, encrypted restart/unknown-key fail closed | Real GitHub App identity/refresh/rotation; two parallel refreshes and crash windows; client deadlines |

R14 [FIXED, AC-008-01/02] Independent QA MUST retain those identities and defect evidence. Root at 495973c ratified the supplemental LOCAL race/crash/hook requirements R1–R6; moving them out of activation is the D-008-02 correction, not a new AC or erased failure. Actual provider may be OAuth App or GitHub App after Forest's choice. The historical “Real GitHub App” wording is not a mandate to register one now.

Every normative R maps to the same stable AC in its label. Execution defines protected exams, resource measurements and deployment gates. No authored example or probe is independent acceptance. Mermaid rendering and complete document schema checks remain QA gates; no renderer was installed or invoked here.

| Stable decision | Resolution / remaining gate |
|---|---|
| O1 | Unanswered Forest choice: deployed subject/provider/app/client and file/retention policy. Synthetic examples are not defaults. |
| O2 | Approved section 6 seam; root must amend accepted core and transfer shared-file ownership before B2. |
| O3 | Approved durable lifetime marker; section 8 fixes protocol. Independent kill-point/filesystem qualification and separately authorized recovery remain gates. |
| O4 | Approved none/S256 metadata projection in section 9; live Claude compatibility remains unqualified. |
| O5 | Historical rows retained verbatim and R14 supplemental local tests ratified. |
| O6 | Strict projection approved; section 10 fixes semantics. Finite fixture bounds and direct-method call budgets are fixed in EXECUTION §5; actual candidate overhead review gates B, and deployment numbers remain unset. |
| O7 | Optional isolated remote extra/resolved lock approved for future preparation; lock review, ordinary parity and existing-index update remain gates. No install authorized here. |

No row grants production dispatch, provider registration, credential resolution, listeners or live defaults. Section12 closes the authorized technical interfaces; independent validation and the separate activation dependencies remain gates.

## 8. Durable lifetime protocol (O3)

[FIXED] remote_auth_storage owns one lifetime lock, one encrypted marker, stored-data trust and durability barriers, not token transitions. All following paths are beneath the operator-provisioned auth_root: auth.lock (0600 stable regular inode, never replace/unlink), lifetime.fernet (0600), lifetime.pending (0600 temporary replacement), and credentials/ (0700). FileTreeStore data_directory and both sanitization strategies use auth_root/credentials, with auto_create=False for that existing root. Its collection children remain library-owned; because auto_create=False also prevents missing collection directories, offline provisioning must prepare all six maintained collections before sealing, not just the client collection. This is a future storage-layout amendment to retained 008 section 10, not migration of any live namespace.

Only AuthStorage writes the marker. Provider alone writes/deletes token collections. No cull/cleanup scheduler, repair, token index, journal of token transitions or extra token database is introduced. Marker is Canonical control state; a manifest digest is an integrity projection, never sufficient authority to rebuild credentials.

Closed AuthMarker fields, all required:
schema literal cortados.cowork-auth-lifetime.v1; state enum[clean,dirty]; generation:N; run_id:ID; config_sha256:Hash; signing_key_sha256:Hash; upstream_secret_sha256:Hash; dependency_sha256:Hash; store_sha256:Hash|null.
clean requires non-null store_sha256; dirty requires null. Config digest is SHA256 of UTF-8 task_evidence.canonical of all AuthConfig serialized fields (including root, references, enabled and budgets). Signing digest uses actual resolved signing bytes; upstream digest uses actual UTF-8 secret bytes. These digests are INSIDE Fernet ciphertext, never logs. Storage-key identity is proven by successful Fernet decryption; changing any actual key/config/dependency identity refuses. dependency_sha256 is SHA256 of reviewed lock file bytes supplied by trusted launcher, not an incidental pip freeze. Generation increments once on startup; overflow refuses without mutation; run_id is fresh UUID4 per lifetime. It is diagnostic ordering, not an external anti-rollback counter.

Store manifest algorithm: under exclusive ownership, traverse credentials without following links; reject nonregular files, symlinks and hardlinked regular files. Entries are arrays [relative_POSIX_path,kind,bytes,sha256], kind file or directory; directory entries use bytes=0 and sha256=null. Include all descendants, sorted by UTF-8 path bytes, exclude no metadata/temp files; the root itself is excluded. SHA256 of canonical entries is store_sha256. Detect path/inode/size/mtime change between read and post-read stat and refuse. Paths/hashes are private and never events. A leftover library temporary file is an unexplained artifact and refuses clean sealing rather than being silently removed. The manifest is computed, not persisted as another credential store.

Atomic marker replacement: create lifetime.pending with O_CREAT|O_EXCL|O_NOFOLLOW, mode0600; encrypt canonical AuthMarker using maintained Fernet(storage_key); write complete ciphertext, flush userspace, fsync file; os.replace pending onto lifetime.fernet on same filesystem; fsync auth_root directory. No admission/provider construction until the dirty replacement's directory barrier succeeds. A pending file on startup causes refusal even if another marker is clean. Never delete it automatically. The qualified target profile uses fsync on files/directories and, on Darwin, fcntl F_FULLFSYNC on regular files after fsync; directory fsync must succeed. Q pins OS/filesystem/mount and validates support before B acceptance. Unsupported required barriers refuse startup; even successful calls are qualified platform evidence, not a universal power-loss guarantee.

Startup order:

1. Pure config/core/lock identity preflight; no listener. Open/validate existing private root and acquire nonblocking exclusive no-follow auth.lock before marker, secret-store or provider access. Failures release only owned handles and preserve bytes.
2. Validate the already supplied in-memory secrets; their resolution belongs to the trusted external provisioning boundary before enter, never path discovery inside AuthRuntime. Verify marker ciphertext/schema/bindings and absence of pending artifacts. Require clean and matching manifest before constructing FileTreeStore/provider. Dirty/corrupt/unknown-key/missing marker on populated credentials refuses without a write.
3. Atomically persist dirty marker (generation+1, new run_id, null store hash) and complete barriers. Only then construct maintained store/provider, register hook closures, set registration disabled, call get_routes once and verify the pre-provisioned downstream public-client record under the gate. Constructor/startup failure after this point leaves dirty. Fresh empty roots have no implicit initialization: offline provisioning must create the initial clean generation0 marker after registration under the same protocol. Future Q synthetic provisioning uses maintained register_client and is sufficient for local exams; production provisioning remains a separate activation dependency.
4. Mark AuthRuntime ready after auth validation so create_remote_app can consume it. Service admission remains closed until core bind and ASGI manager lifespan readiness also hold. Marker remains dirty throughout the entire admitted lifetime, including idle periods; it is never cleaned between requests.

Clean close order:

1. Stop all OAuth/MCP admission, refuse queued waiters, wait for the active gate owner and all maintained child operations/clients to settle. R5 uncertainty permanently latches failed for this lifetime; it cannot be cleared by later success.
2. With exclusive gate and AuthStorage's root lock and no possible writer remaining, validate referenced records through maintained schemas while still dirty, close maintained resources, then flush/fsync every surviving credential file, then each credentials directory deepest-first, then credentials root and auth_root. Directory barriers account for deletions as well as renames. Compute the manifest after those barriers; any error before clean replacement leaves dirty. Observed errors at/after clean replacement follow step4.
3. Persist clean marker with same generation/run_id and current manifest using the exact atomic-replace/barrier sequence. Only a ready-to-stopping, certainly settled lifetime may do this. Release gate then root lock. No code may touch credentials after clean sealing.
4. On failure, retain all remaining bytes, pending files and fixed failure evidence; notify the parent with §12.3 failure. Do not attempt a second dirty overwrite to conceal an uncertain clean rename. The live supervisor remains failed with admission closed and no automatic relaunch; explicit operator review is required. It must not claim successful drain or clean-close acknowledgment. Only proven process/operation death permits release of owned handles; no later callback can write after lock release.

Supported restart validates the exact clean marker, configuration/key/dependency bindings, complete current manifest, strict record/layout checks and absence of pending artifacts under the exclusive lock on the qualified filesystem. It does **not** prove the previous process observed the last barrier succeeding. After all-process loss, an unobservable failed final barrier may leave the same bytes as a successful close; a complete stale restored image can do the same. Both histories are outside this local detection guarantee. No external fence, silent rollback, reauthentication, deletion, new namespace or recovery is authorized.

| Kill/fault point | Allowed later observation / result |
|---|---|
| Before dirty replacement; no credential operation started | Previous exact clean image may restart after full validation; a leftover pending file refuses. |
| Dirty rename visible but directory barrier incomplete | Clean old image or dirty/new/pending image can survive all-process loss. Admit only a fully validated clean image; no credential operation was allowed yet. A live observer of barrier error fails supervisor. |
| After completed dirty barrier, during callback/code/refresh multiwrite, before clean replacement | Dirty/pending/inconsistent image refuses; preserve bytes. Test each maintained await boundary. No crash atomicity or upstream rollback. |
| During credential-file/directory barriers or manifest computation | No clean seal has been attempted; dirty/pending refuses. |
| Clean pending written, before rename | Pending refuses, even with an old marker. |
| Clean rename visible, final directory barrier not observed successful | Clean/current manifest may validate after all-process loss, or dirty/pending/inconsistent state may refuse. A surviving supervisor that observed uncertainty MUST remain failed and prohibit relaunch. QA must allow both disk outcomes and must not assert universal detection. |
| After completed clean barriers, before lock release | Clean image may restart once OS releases lock and full verification passes. |
| Whole stale image restored | Outside supported restart/recovery; local matching hashes cannot detect rollback. Never claim otherwise. |

Q preserves original fault-injected bytes, barrier results and supervisor traces. Process-kill tests qualify ordering and these allowed outcomes only; they are not power-loss experiments.

## 9. HTTP integration and public-client discovery (O4)

remote_service owns route adaptation and metadata projection; remote_auth_runtime owns endpoint gates; remote_auth_errors owns bounded error filtering; remote_http owns dispatch/lifespan. Maintained endpoints still own state, consent, PKCE, tokens and errors before filtering.

| Exact path | Admitted methods / handling |
|---|---|
| /.well-known/oauth-authorization-server | GET, HEAD, OPTIONS; corrected maintained metadata, no credentials |
| /.well-known/oauth-protected-resource/mcp | GET, HEAD, OPTIONS; maintained exact resource metadata |
| /authorize | GET, HEAD, POST; ingress field validation then outer gate then maintained handler |
| /token | POST, OPTIONS; OPTIONS does not enter credential gate; POST validates then gates before client authentication |
| /auth/callback | GET, HEAD; maintained callback under gate; HEAD executes GET semantics but sends no body, as Starlette Route GET implies HEAD |
| /consent | GET, HEAD, POST; gate before any maintained cookie/transaction lookup |
| /mcp | all methods authenticate; then core POST/405 behavior |
| /transfers/<canonical UUID4> | original core GET/PUT capability behavior; no JWT middleware |

Only these paths mount. Capacity reservation in §10 precedes retained ingress decoding on every credential route; the table's validation→gate order means validation before maintained credential execution, not an uncounted pre-gate body buffer. Unknown paths return core fixed404; disallowed methods fixed405 with allowlist Allow; no redirects for slash variants. GET routes' implicit HEAD is explicit here, not accidentally a credential bypass. Discovery HEAD suppresses body; OPTIONS uses maintained CORS preflight only, with no credential lookup; provider routes not listed do not mount. Outer exact Host/Origin enforcement precedes route dispatch; forwarded identity headers never establish authority. Host must equal config.origin authority; Origin, when present, must equal config.origin or the exact hosted callback origin https://claude.ai. No wildcard or forwarded-header identity; trusted reverse-proxy/server limit wiring remains an activation dependency.

Metadata construction uses mcp.server.auth.routes.build_metadata(base_url, service_documentation_url, client_registration_options, revocation_options), then changes only token_endpoint_auth_methods_supported to ["none"], code_challenge_methods_supported to ["S256"], client_id_metadata_document_supported to False, and leaves registration_endpoint/revocation_endpoint absent (None excluded in serialization). issuer, authorization_endpoint, token_endpoint and grant/response types retain maintained construction. MetadataHandler and cors_middleware serve that value. Protected-resource metadata remains maintained and resource equals origin+"/mcp"; its authorization_servers uses canonical issuer. R9 requires both registration and revocation options disabled before get_routes. The adapter asserts actual returned paths/methods against this table; source drift refuses construction. No custom token issuance, discovery engine or dynamic client fetch.

Pre-provisioned client must have configured downstream client_id, client_secret=None, token_endpoint_auth_method="none", exactly one hosted redirect, grant_types=["authorization_code","refresh_token"], response_types=["code"], scope="" with no requested API scopes, and allow_unregistered_redirect_uris=False. No HTTP Basic, client_secret, client_assertion or alternate client ID accepted. Maintained register_client is used for future isolated synthetic Q provisioning and separately specified operator offline provisioning; startup does not register or overwrite it. Live Claude inability to use the pre-provisioned public profile returns O4, never enables DCR.

Form/query decoder preserves key multiplicity before parsing; duplicate keys in any source or merged query/body refuse. UTF-8/form decoding errors refuse. Required fields are R10. Authorize additionally permits optional state and scope (empty only); token authorization_code permits grant_type, client_id, resource, code, redirect_uri, code_verifier; refresh permits grant_type, client_id, resource, refresh_token, optional empty scope. Unknown fields refuse. Code verifier is 43..128 ASCII characters from ALPHA/DIGIT/-._~; challenge is 43 base64url characters and decodes to exactly32 bytes. Callback/consent retain maintained state/CSRF/cookie schemas; adapter bounds bytes and rejects duplicate inputs but does not reimplement those schemas. Authorization query is permitted only on protocol routes; transfer/MCP query policy remains core-owned.

## 10. Strict projection and bounded operation settlement (O6)

validate_token_response is synchronous and pure except reading the provided response. The registered hook is a closure binding response_bytes and sampling UTC now_s once per response (time.time, truncated to integer seconds; no new local-date helper). It rejects non-2xx status, over-limit response content bytes, invalid UTF-8, duplicate keys at any JSON object level, nonfinite values, surrogate strings, nonobject root, any error member, or R1 invalid fields before Authlib parsing. An unconsumed response is refused; this hook never starts I/O. It is downstream of the §12.1 network guard. Success returns a NEW httpx.Response with the same 2xx status, JSON content containing only access_token, refresh_token, token_type="Bearer", expires_in, refresh_expires_in and scope if supplied. It sets application/json and computed content length; it does not retain upstream headers/cookies/error text. No expires_at, id_token, alias or unknown key survives. Scope, when present, is string (empty allowed). Neither input mapping nor original response is mutated. Failure raises AuthPolicyError("invalid_request") with fixed message only; dependency boundaries translate it to section3 AuthFailure. Lifetime addition must remain finite and within the dependent time representation: values whose now+duration exceeds P refuse, not wrap.

R1's zero parse/token-write requirement applies to the malformed response path before exchange mutations; it does not promise zero prior transaction reads, setup writes or an upstream request that already happened. Once upstream exchange has begun, malformed success or transport ambiguity latches R5 failed even when local writes are zero. Ordinary invalid_grant before mutation can return refusal without failing runtime. If the adapter cannot prove no unsettled effect, it fails the lifetime. Provider-swallowed exceptions must not bypass this latch: the runtime-owned hook closure catches the pure validator's fixed exception and sets a request-local failure signal on rejection; no second gate is acquired.

response_bytes bounds outgoing safe OAuth buffering and semantic hook inspection; upstream_body_bytes separately bounds every actual OAuth and identity response before eager read. The §12.1 guard is mandatory. Neither bound applies to core transfers. Intended successful protocol token/code/state fields are not diagnostic leaks.

Gate policy: one active credential operation and at most max_waiters queued reservations, all strict positive configured integers. Before reading/retaining an OAuth body or making decoded state, atomically either reserve the active slot or increment queued count if below cap; otherwise refuse immediately HTTP503/E7 temporarily_unavailable without consuming a body. Bound method/path/query/header-derived retained auth inputs by control_bytes; bounded body accumulation checks each ASGI chunk before copying and rejects413 at control_bytes+1. Server connections, parser headers and already allocated ASGI chunks have separate server/ingress limits and are not bounded by this queue claim. Admission memory excludes those server-owned buffers and must report them separately.

Waiters are FIFO, carry a monotonic deadline from reservation, and do no credential reads. A queue slot covers body reading/validation too; invalid ingress, cancellation, disconnect, timeout or lifetime failure removes it exactly once in finally. Transfer to active atomically decrements queued/increments active; active<=1 and queued<=max_waiters always. A timed-out/cancelled entry cannot later be selected. No await between checking capacity and reserving; no unbounded secondary task/waiter list. Gate timeout/exhaustion uses fixed503 temporarily_unavailable, including resource routes; bad bearer uses401. A reservation rejected before credential access does not dirty operation state or fail an otherwise healthy lifetime.

operation_ms runs from active acquisition through semantic validation, maintained transitions and safe-output completion. Timeout/disconnect after credential access or uncertain effect latches failed, closes admission, notifies supervisor and cancels cooperatively; shield settlement before releasing the gate/storage lock. The same operation_ms is the additional settlement watchdog interval; if still unsettled, supervisor terminates the frontend and awaits process death without claiming clean close. Request deadlines bound responses, not upstream rollback. Pending waiters fail once and are removed. No retry, token-family repair or subsequent operation follows. Provider-swallowed transport/storage exceptions still latch via on_failure before propagation.

All deployment values remain mandatory and unset. EXECUTION §5 fixes synthetic cap matrices, source floors and method budgets before A; A checks reproducible exams and expected-red candidate boundaries. Candidate overhead/resource comparison occurs after implementation and before B acceptance. Missing candidate measurements cannot be demanded before candidate code exists, and cannot be waived at B. Mock timings never establish live 10s/30s deadline sufficiency.


## 11. Closed constructors, imports and examples

Local records use the 008 mechanical convention Name(**fields), validate once, deep-copy nested mappings/sequences, expose immutable named attributes and retain no caller aliases. No record accepts positional arguments. AuthConfig/ResolvedAuthSecrets live in remote_auth_config; AuthMarker lives in remote_auth_storage; VerifiedClaims/AuthorizationFacts in remote_auth_claims; AuthEvent/AuthFailure/AuthPolicyError in remote_auth_errors. Runtime is an effect-free object constructor, not a serializable record. Unknown/missing local record keys reject; optional VerifiedClaims.nbf is the local absent-field exception. Maintained persisted/token records have their explicitly listed optional/nullable fields. Raw JWTs are inputs to projection and may contain other maintained claims; VerifiedClaims output is closed. Library-owned records below keep their actual Pydantic defaults and are not converted into local closed schemas.

AuthPolicyError(code:str) accepts only invalid_request, invalid_token, owner_disabled, upstream_unavailable, storage_unavailable, operation_interrupted, gate_exhausted. Its message is always "Authentication request refused", no cause/traceback in public output. Pure validation raises this fixed error; runtime boundaries map it to AuthFailure or fixed 401. Gate admission exhaustion is HTTP503 temporarily_unavailable; invalid client is HTTP401 invalid_client; invalid grant/request is HTTP400; resource invalidity is HTTP401 with fixed challenge. Unexpected exceptions map to503 temporarily_unavailable and fail the lifetime if effects may have occurred. Constructors never resolve references or perform I/O.

Additional exact callable registry (all prospective; no implicit imports):

- remote_auth_runtime.AuthRuntime(*,config:AuthConfig,secrets:ResolvedAuthSecrets,dependency_sha256:str,on_failure:Callable[[],None]); async __aenter__()->AuthRuntime; async __aexit__(exc_type:type[BaseException]|None,exc:BaseException|None,tb:TracebackType|None)->None; async verify_token(token:str)->AccessToken|None; wrap_oauth(endpoint:ASGIApp)->ASGIApp. Constructor validates in-memory types only. enter delegates §8 to AuthStorage; exit proves drain and delegates seal or failed preservation. Runtime never writes a marker. wrap_oauth returns an ASGI callable and acquires only at request entry, not wrapper creation.
- remote_auth_runtime.wrap_mcp(endpoint:ASGIApp)->ASGIApp is an AuthRuntime method. It composes AuthenticationMiddleware(backend=BearerAuthBackend(self)) then AuthContextMiddleware, requires authenticated context, and checks projected Principal equality with construction principal before calling endpoint. AuthenticationMiddleware is outermost. It does not acquire another gate after verify_token. Invalid tokens return None to maintained backend; fixed outage failure is handled by the guard. No secret enters scope beyond the maintained authentication object needed for this request. AuthRuntime's on_failure synchronously invokes the child supervisor's once-only admission latch/Event; one existing control coroutine sends failure(auth). It does not create a task per failure or retain exception/token text. Transport and storage invoke runtime's latch, which propagates even if a provider catches the error.
- remote_service.create_remote_app(*,core_client:CoreClient,principal:Principal,frontend:FrontendConfig,auth:AuthRuntime)->ASGIApp requires entered/ready auth and bound client; validates equality then calls the one composition owner. It does not enter auth a second time. remote_service.project_metadata(metadata:OAuthMetadata)->OAuthMetadata is a pure copy/update as section9, never a protocol issuer.
- remote_auth_errors.safe_oauth_app(app:ASGIApp,*,config:AuthConfig)->ASGIApp buffers only OAuth/discovery responses; emit_auth_event(event:AuthEvent)->None emits canonical fixed event once. safe_oauth_app wraps the gated endpoint so no exception bytes precede filtering.
- Existing section3 validate_claims/project_facts/load_auth_config signatures remain exact. Pure project_facts rejects missing claims, wrong live sub or disabled config; it never contacts GitHub. StrictGitHubProvider inherits the maintained keyword-only constructor with the additional mandatory local keywords config:AuthConfig and runtime-owned on_response_failure:Callable[[],None]; these are consumed locally, never passed to super. Its two override signatures stay exactly section2; closures bind local operands, including config.response_bytes; §12.1 owns real client construction.

| Types / operations | Canonical import owner |
|---|---|
| ASGIApp; Scope/Receive/Send for returned callables | starlette.types; callable signature async (scope:Scope,receive:Receive,send:Send)->None |
| Callable; TracebackType | collections.abc; types |
| Route; Request; HTMLResponse/RedirectResponse | starlette.routing; starlette.requests; starlette.responses |
| AuthenticationMiddleware | starlette.middleware.authentication |
| BearerAuthBackend; AuthContextMiddleware | mcp.server.auth.middleware.bearer_auth; mcp.server.auth.middleware.auth_context |
| SDK AccessToken/AuthorizationCode/RefreshToken | mcp.server.auth.provider; projected AccessToken uses this base, not a new token schema |
| OAuthClientInformationFull/OAuthToken/OAuthMetadata | mcp.shared.auth |
| build_metadata/cors_middleware/build_resource_metadata_url | mcp.server.auth.routes |
| MetadataHandler | mcp.server.auth.handlers.metadata |
| ClientRegistrationOptions/RevocationOptions | mcp.server.auth.settings |
| GitHubProvider | fastmcp.server.auth.providers.github |
| AsyncOAuth2Client | authlib.integrations.httpx_client |
| HTTP response/client | httpx.Response; httpx.AsyncClient |
| FileTreeStore and FileTreeV1*SanitizationStrategy | key_value.aio.stores.filetree |
| FernetEncryptionWrapper; Fernet | key_value.aio.wrappers.encryption; cryptography.fernet |
| PydanticAdapter; AsyncKeyValue | key_value.aio.adapters.pydantic; key_value.aio.protocols |
| AnyHttpUrl | pydantic |
| Principal/CoreConfig/FrontendConfig/CoreLimits/RemoteError | future integrations.mcp.remote_schemas, as 008 |
| BaseConfig | validated dict returned by integrations.mcp.config.load_config(None,raw=base_raw); BaseConfig is a semantic alias, not an importable Config class |
| CoreClient/CoreRuntime | future integrations.mcp.remote_ipc / remote_runtime |
| canonical | cortados_router.task_evidence.canonical, never a duplicate JSON canonicalizer |
| StreamableHTTPSessionManager; Server | mcp.server.streamable_http_manager; mcp.server.lowlevel |
| CallToolResult/TextContent | mcp.types; sole transport owner is remote_http |

Examples below are synthetic schema/constructor examples, not credentials, live evidence or independently accepted fixtures. ref/replace/omit are the exact section3 document operators. Required fields never rely on an ellipsis. For library-owned records, these are complete constructor inputs including explicit nullable/default fields; Pydantic's internal excluded fields are identified. Invalid examples are semantic adapter refusals where the library is intentionally permissive; QA must not claim library validation alone enforces policy.

E9 complete AuthConfig (replaces the shorthand row's expansion):

E9 (full value).

```json
{
  "schema": "cortados.cowork-auth.v1",
  "origin": "https://cowork.invalid",
  "issuer": "https://cowork.invalid/",
  "resource": "https://cowork.invalid/mcp",
  "installation_id": "11111111-1111-4111-8111-111111111111",
  "github_subject": "42",
  "downstream_client_id": "synthetic-claude",
  "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
  "enabled": true,
  "identity_scopes": [],
  "auth_root": "/synthetic/private/auth",
  "upstream_client_id": "synthetic-upstream",
  "upstream_secret_ref": "synthetic-secret",
  "signing_key_ref": "synthetic-sign",
  "storage_key_ref": "synthetic-storage",
  "control_bytes": 65536,
  "gate_wait_ms": 1000,
  "operation_ms": 5000,
  "response_bytes": 65536,
  "max_waiters": 2,
  "upstream_body_bytes": 131072,
  "upstream_chunk_bytes": 65536,
  "upstream_header_bytes": 167936,
  "storage_record_bytes": 1048576
}
```

Invalid: omit(E9,github_subject); replace(E9,enabled,1); add unknown field. Cross-root disjointness is launcher validation, since this record alone does not contain core roots.

E5 (full expansion; same E5 identity).

```json
{
  "principal": {
    "issuer": "https://cowork.invalid/",
    "subject": "42",
    "installation_id": "11111111-1111-4111-8111-111111111111",
    "client_id": "synthetic-claude",
    "enabled": true
  },
  "resource": "https://cowork.invalid/mcp",
  "expires_at": 4600
}
```

Invalid: add upstream_token; expires_at=true; principal.client_id different from E9.

E10 ResolvedAuthSecrets in-memory constructor: ResolvedAuthSecrets(upstream_secret='SYNTHETIC-SECRET', signing_key=b's'*32, storage_key=base64.urlsafe_b64encode(b'e'*32)). All fields required; signing_key exactly32 bytes; storage_key is44-byte canonical Fernet URL-safe base64 encoding of32 bytes; upstream_secret nonblank UTF-8. Distinct key material and references required. Invalid: signing_key=b's', storage_key=b'not-fernet', extra field or empty upstream_secret. No JSON serializer; repr/str redact every field. The base64 import is stdlib base64. These bytes are public synthetic examples only.

E11 AuthMarker plaintext before Fernet. This is a valid structural empty-store seal with exact E9/E10 digests and lock bytes UTF-8 'synthetic-lock-v1' followed by LF; it is NOT a provisioned ready service (client is absent).

```json
{
  "schema": "cortados.cowork-auth-lifetime.v1",
  "state": "clean",
  "generation": 0,
  "run_id": "33333333-3333-4333-8333-333333333333",
  "config_sha256": "11b71c3ccef78d966ff3ab029a05a70f29691d56272ed2ae90a24c5ef65c5dbb",
  "signing_key_sha256": "8fd6a6a78f5857d7ba1cfe9033bffeec86da2ba6a4bd60a6f833209d9d3e390d",
  "upstream_secret_sha256": "51325b6df2b8f087f99b463606e6794596f8589d3301539abee173985d1d4d77",
  "dependency_sha256": "294fdee877b7699ed86363d72fb742593c91641d568e1668b306bfc00f3c9652",
  "store_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
}
```

Invalid: state=dirty with non-null store_sha256; omitted dependency_sha256; generation=true; decrypt under different key. A valid dirty variant is replace(replace(replace(E11,state,'dirty'),generation,1),store_sha256,null); use a new UUID4 run_id in a real startup.

E12 AuthRuntime constructor: AuthRuntime(config=AuthConfig(**ref(E9)),secrets=ref(E10),dependency_sha256=ref(E11).dependency_sha256). Valid construction has state starting and no open fd/provider/gate operation; it is not ready. Invalid: raw mapping in secrets, unknown argument, reused/entered runtime or invalid dependency hash. Entering E11's empty fixture refuses missing registered client and preserves dirty; only independently provisioned fixtures reach ready. This is a lifecycle example, not serialized state.

E13 maintained OAuthTransaction constructor input at now=1000.

```json
{
  "txn_id": "synthetic-txn",
  "client_id": "synthetic-claude",
  "client_redirect_uri": "https://claude.ai/api/mcp/auth_callback",
  "client_state": "synthetic-state",
  "code_challenge": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "code_challenge_method": "S256",
  "scopes": [],
  "created_at": 1000.0,
  "resource": "https://cowork.invalid/mcp",
  "proxy_code_verifier": null,
  "csrf_token": "synthetic-csrf",
  "csrf_expires_at": 1900.0,
  "consent_token": null
}
```

Invalid: omit txn_id; adapter rejects code_challenge_method='plain'.

E14 maintained ClientCode constructor input.

```json
{
  "code": "synthetic-code",
  "client_id": "synthetic-claude",
  "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
  "code_challenge": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "code_challenge_method": "S256",
  "scopes": [],
  "idp_tokens": {
    "access_token": "SYNTHETIC-ACCESS",
    "refresh_token": "SYNTHETIC-REFRESH",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_expires_in": 7200
  },
  "expires_at": 1300.0,
  "created_at": 1000.0
}
```

Invalid: omit idp_tokens; adapter rejects idp_tokens.expires_in=true before trusting restored code.

E15 maintained UpstreamTokenSet constructor input.

```json
{
  "upstream_token_id": "synthetic-upstream-id",
  "access_token": "SYNTHETIC-ACCESS",
  "refresh_token": "SYNTHETIC-REFRESH",
  "refresh_token_expires_at": 8200.0,
  "expires_at": 4600.0,
  "token_type": "Bearer",
  "scope": "",
  "client_id": "synthetic-claude",
  "created_at": 1000.0,
  "raw_token_data": {
    "access_token": "SYNTHETIC-ACCESS",
    "refresh_token": "SYNTHETIC-REFRESH",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_expires_in": 7200
  }
}
```

Invalid: omit access_token or wrong client binding. Null refresh_token/refresh_token_expires_at is structurally valid maintained data; it cannot authorize a finite-refresh operation without the required token/expiry. Do not relabel a valid nullable field as storage corruption.

E16 maintained JTIMapping constructor input.

```json
{
  "jti": "synthetic-jti",
  "upstream_token_id": "synthetic-upstream-id",
  "created_at": 1000.0
}
```

Invalid: omit jti; referenced upstream row missing refuses authentication.

E17 maintained RefreshTokenMetadata constructor input; key is maintained SHA256 of the synthetic refresh JWT.

```json
{
  "client_id": "synthetic-claude",
  "scopes": [],
  "expires_at": 8200,
  "created_at": 1000.0
}
```

Invalid: omit client_id or expires_at=true. Null expires_at is structurally valid maintained data but is not acceptable evidence of a finite refresh lifetime. Created_at remains maintained float epoch, not local P.

E18 maintained ProxyDCRClient constructor input. allow_unregistered_redirect_uris is intentionally excluded by maintained serialization, not a missing persisted field.

```json
{
  "redirect_uris": [
    "https://claude.ai/api/mcp/auth_callback"
  ],
  "token_endpoint_auth_method": "none",
  "grant_types": [
    "authorization_code",
    "refresh_token"
  ],
  "response_types": [
    "code"
  ],
  "scope": "",
  "client_name": null,
  "client_uri": null,
  "logo_uri": null,
  "contacts": null,
  "tos_uri": null,
  "policy_uri": null,
  "jwks_uri": null,
  "jwks": null,
  "software_id": null,
  "software_version": null,
  "client_id": "synthetic-claude",
  "client_secret": null,
  "client_id_issued_at": null,
  "client_secret_expires_at": null,
  "issuer": null,
  "allowed_redirect_uri_patterns": [
    "https://claude.ai/api/mcp/auth_callback"
  ],
  "cimd_document": null,
  "cimd_fetched_at": null,
  "allow_unregistered_redirect_uris": false
}
```

Invalid: client_secret='SYNTHETIC-SECRET', foreign client_id, wildcard redirect or cimd_document non-null refuse this profile. Removing required redirect_uris is a model error.

E19 OAuthClientInformationFull offline provisioning input is E18 with exactly allowed_redirect_uri_patterns, cimd_document, cimd_fetched_at and allow_unregistered_redirect_uris removed. All remaining fields are explicit. Invalid: client_id=null, redirect_uris omitted or any secret/client authentication method other than none. Future isolated Q provisioning calls await provider.register_client(OAuthClientInformationFull(**E19)); this grants no operator provisioning or execution in this turn.

E20 projected SDK AccessToken input after E3/E4 verification.

```json
{
  "token": "SYNTHETIC-PROXY-BEARER",
  "client_id": "synthetic-claude",
  "scopes": [],
  "expires_at": 4600,
  "resource": "https://cowork.invalid/mcp",
  "subject": "42",
  "claims": {
    "iss": "https://cowork.invalid/",
    "sub": "42",
    "installation_id": "11111111-1111-4111-8111-111111111111"
  }
}
```

Invalid: token='SYNTHETIC-ACCESS' or client_id='42' is forbidden projection. This constructor example is not itself a valid signed bearer.

E21 normalized success is ref(E2) with token_type='Bearer'; invalid output includes expires_at, id_token or refresh_token_expires_in. E22 AuthFailure is ref(E7), invalid error='provider_exception'. E23 AuthEvent is ref(E6), invalid reason='secret text'; event starting/ready/closed requires reason=none, failed/refused requires a non-none listed reason. E24 VerifiedClaims is ref(E3), optional nbf=1000 is valid, nbf=1001 invalid at now1000. E4 remains the exact Principal example owned by 008; no new principal schema.

The six library examples specify shapes, not freshly captured maintained outputs. QA must compare actual serialization (including Authlib-added expires_at inside raw_token_data, which is derived after strict projection) and TTL envelopes from pinned methods before A. Do not invent a replacement serializer or assume model coercions provide the stricter adapter policy. Pydantic model validation cannot itself prove ciphertext integrity, reference existence, client binding or an ordered clean close.


### Additional dependency record examples

These complete constructor inputs finish the callable-record inventory; maintained issuance remains the only producer of real OAuth tokens. Invalid variants are adapter refusals unless specified as model errors. No test is claimed.

E25 SDK AuthorizationCode.

```json
{"code":"synthetic-code","scopes":[],"expires_at":1300.0,"client_id":"synthetic-claude","code_challenge":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","redirect_uri":"https://claude.ai/api/mcp/auth_callback","redirect_uri_provided_explicitly":true,"resource":"https://cowork.invalid/mcp","subject":null}
```

Invalid: omit code (model error); wrong resource/client or expired code refuses maintained exchange. Null subject is a maintained intermediate and never replaces live identity verification.

E26 SDK RefreshToken.

```json
{"token":"SYNTHETIC-REFRESH-JWT","client_id":"synthetic-claude","scopes":[],"expires_at":8200,"resource":"https://cowork.invalid/mcp","subject":null}
```

Invalid: omit token (model error); expired/foreign refresh refuses. Placeholder is not a signed token.

E27 maintained OAuthToken.

```json
{"access_token":"SYNTHETIC-ACCESS-JWT","token_type":"Bearer","expires_in":3600,"scope":"","refresh_token":"SYNTHETIC-REFRESH-JWT"}
```

Invalid: missing access_token (model error); missing refresh or finite access duration violates this profile even if library model accepts it.

E28 corrected OAuthMetadata; None fields are omitted on the wire by maintained handler.

```json
{"issuer":"https://cowork.invalid/","authorization_endpoint":"https://cowork.invalid/authorize","token_endpoint":"https://cowork.invalid/token","registration_endpoint":null,"scopes_supported":[],"response_types_supported":["code"],"response_modes_supported":null,"grant_types_supported":["authorization_code","refresh_token"],"token_endpoint_auth_methods_supported":["none"],"token_endpoint_auth_signing_alg_values_supported":null,"service_documentation":null,"ui_locales_supported":null,"op_policy_uri":null,"op_tos_uri":null,"revocation_endpoint":null,"revocation_endpoint_auth_methods_supported":null,"revocation_endpoint_auth_signing_alg_values_supported":null,"introspection_endpoint":null,"introspection_endpoint_auth_methods_supported":null,"introspection_endpoint_auth_signing_alg_values_supported":null,"code_challenge_methods_supported":["S256"],"client_id_metadata_document_supported":false}
```

Invalid: client_secret_basic method, plain PKCE, non-null registration_endpoint or CIMD=true violates O4.

E29 ProtectedResourceMetadata.

```json
{"resource":"https://cowork.invalid/mcp","authorization_servers":["https://cowork.invalid/"],"jwks_uri":null,"scopes_supported":[],"bearer_methods_supported":["header"],"resource_signing_alg_values_supported":null,"resource_name":null,"resource_documentation":null,"resource_policy_uri":null,"resource_tos_uri":null,"tls_client_certificate_bound_access_tokens":null,"authorization_details_types_supported":null,"dpop_signing_alg_values_supported":null,"dpop_bound_access_tokens_required":null}
```

Invalid: resource ending /mcp/ or foreign authorization server violates exact binding.

E30 manifest entries (tuple representation; not token records).

```json
[["empty","directory",0,null],["empty/sample","file",0,"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"]]
```

Invalid: duplicate/unsorted relative path, ../ escape, absolute path, directory hash non-null or observed symlink. This is a structural traversal example, not E11's empty-root digest.

## 12. Frozen v3 technical boundaries

These are proposed build obligations under 5d98d77, not alternatives or implementation authority. B1 adds exactly transport and storage to its five auth files; B2 adds exactly supervisor to its listed integration/package files. Every source remains <500 physical lines. No current008/exam edits.

### 12.1 remote_auth_transport — one upstream allocation guard

Exact new APIs (all constructors effect-free; types from httpx/collections.abc):

- `make_oauth_client(*,client_id:str,client_secret:str|None,token_endpoint_auth_method:str|None,config:AuthConfig,on_failure:Callable[[],None])->AsyncOAuth2Client`.
- `make_identity_client(*,config:AuthConfig,on_failure:Callable[[],None])->httpx.AsyncClient`.
- `install_guards(client:httpx.AsyncClient,*,kind:Literal["oauth","identity"],config:AuthConfig,on_failure:Callable[[],None])->None`; called once before first send; appends async request/response hooks, refuses a non-fresh client. Literal imports typing.
- `deadline_scope(deadline:float)->ContextManager[None]` binds a finite monotonic absolute operation deadline in a ContextVar and restores its token in finally; imports contextlib/contextvars/time. Runtime enters it exactly once after gate acquisition and retains it through settlement. Request hook outside that scope refuses; no implicit default. E9 at monotonic100.0 binds deadline105.0; at105.0 a new send refuses before transport.
- `GuardedStream(stream:httpx.AsyncByteStream,*,body_bytes:int,chunk_bytes:int,expected_length:int|None,on_failure:Callable[[],None])`; `__aiter__()->AsyncIterator[bytes]`, `async aclose()->None`. Single consumption, idempotent close, no background reader.

Both factories construct real maintained clients with `trust_env=False, follow_redirects=False, http1=True, http2=False, limits=httpx.Limits(max_connections=1,max_keepalive_connections=0), timeout=httpx.Timeout(config.operation_ms/1000), headers={"Accept-Encoding":"identity"}` and a `http.cookiejar.CookieJar` whose CookiePolicy.set_ok and return_ok always return False (other CookiePolicy methods inherit DefaultCookiePolicy). Pass the deny-all jar as cookies=jar. No proxy/mount/custom SSL environment is used; verify=True and maintained CA trust remain. Disable keepalive so the shared verifier does not add cross-operation connection state. The OAuth factory passes the exact three upstream arguments from the provider's existing factory fields unchanged; it leaves Authlib scope/token/refresh/authentication defaults unchanged. This replaces only the v2 call-to-super construction, using the expressly overridable proxy.py:719 seam and Authlib's supported HTTPX kwargs. It does not replace fetch_token, refresh_token, request signing, parsing or issuance. OAuth clients close through the maintained contextmanager finally. The identity client is made/entered once during AuthRuntime enter, passed as GitHubProvider(http_client=identity_client), reused sequentially under the same gate, and awaited aclose after operations settle on every exit/failed enter. No http_client=None fallback. If a close is uncertain the lifetime fails; do not clean-seal.

Request hook permits exactly POST https://github.com/login/oauth/access_token for oauth, or GET https://api.github.com/user and GET https://api.github.com/user/repos for identity. Require canonical HTTPS host/path, effective port443, no explicit alternate port, userinfo, query or fragment; compare raw URL representation to those literals. Reject all other requests before sending. Browser authorize URL https://github.com/login/oauth/authorize remains maintained redirect construction, not an HTTP-client allowlist exception. Force Accept-Encoding: identity and remove Cookie on every request; the deny-all jar prevents response cookie retention before hooks. Authorization and token form encoding remain Authlib/GitHub-owned. No retry, redirect following, pagination, repo-scope expansion or environment proxy. Request timeout extension is set to the remaining operation deadline (connect/read/write/pool all finite); outer operation timer still bounds a sequence of calls.

Async response hook runs at HTTPX _client.py:1696–1703 before send:1637 aread. Refuse consumed/closed responses, missing AsyncByteStream or synthetic preloaded content in production. Reject any 3xx, any Content-Encoding other than absent or single identity, invalid/conflicting Content-Length, negative/nondecimal length or length > upstream_body_bytes. Identical repeated Content-Length values may already be consolidated by pinned h11._headers.normalize_and_validate; accept that maintained normalization, never claim raw duplicate detection at the response hook. Count the returned raw header name/value bytes plus separators; reject above upstream_header_bytes and reject unexpected protocol version. Then replace response.stream with GuardedStream before any body read/decoder. Latch on_failure and close on header error. Guard counts len(raw_chunk) before yielding; if chunk exceeds upstream_chunk_bytes or cumulative bytes exceed upstream_body_bytes, latch on_failure, close underlying stream in shielded finally, and raise fixed AuthPolicyError(upstream_unavailable) without yielding that chunk. Count actual bytes when length is absent/lying; at EOF require equality when Content-Length was supplied. Empty chunks do not increase counters; operation deadline still applies. Cancellation/BaseException latches failure and awaits close; no swallowed close errors. All OAuth token paths and both identity calls traverse this same owner. Successful identity /user additionally requires strict object JSON and id type exactly int, positive and <=P before maintained conversion to str; bool/string IDs cannot acquire authority. For identity only, after installing GuardedStream the async response hook awaits response.aread() through that guard, then strict-decodes UTF-8/duplicates/nonfinite/surrogates and validates /user before returning. /user/repos may be any well-formed JSON on a successful response; no repository data is projected. Non-200 identity errors stay bounded and return maintained refusal (do not require an id on them). OAuth response hook does not read; HTTPX then eagerly reads through the same guard before Authlib compliance parsing.

Source floor, not a deployment recommendation: httpcore1.0.9 HTTP11Connection.READ_NUM_BYTES=65536; its AnyIO network backend passes max_bytes to receive. HTTP11Connection.MAX_INCOMPLETE_EVENT_SIZE=102400 is h11's incomplete-event bound, **not** an exact complete-header cap. h11 0.16.0 checks NEED_DATA after parse; at most one further 65536-byte read can be resident before that check, giving conservative 167936 bytes of parser input for one header event. A complete header may exceed102400; response-hook rejection happens after that bounded parsing. HTTP/2 is disabled. Pin those sources and AnyIO4.15.1; changed backend/protocol/source refuses qualification. Require upstream_chunk_bytes>=65536 and upstream_header_bytes>=167936; stricter requested source caps are unsupported, not enforced by slicing. Body cap may be smaller than a network chunk: the fixed source read is still retained transiently before rejection. A test transport producing a huge chunk must be rejected, but that negative test is not evidence of production allocation bounds. Memory includes h11 copies, TLS/OS/client allocations and HTTPX's final join (bounded body may be copied); measure these separately. No total-process RSS bound follows from body length alone.

E31 exact new-field fixture: E9 is valid; omit max_waiters, bool body limit, chunk_bytes=65535, header_bytes=167935 all refuse construction. A 131073-byte identity body against E9 refuses before decoder; gzip with a small Content-Length refuses before decoding. Source-backed synthetic stream exams must exercise actual pinned HTTP11Connection with a bounded fake AsyncNetworkStream, observing read(max_bytes=65536), fragmented/complete/oversized headers, absent/lying length, compression, cancellation and close—not just a mock that returns an already-buffered httpx.Response.

### 12.2 remote_auth_storage — one storage trust and durability owner

Exact new APIs:

- `AuthStorage(*,config:AuthConfig,secrets:ResolvedAuthSecrets,dependency_sha256:str,on_failure:Callable[[],None])`; `async __aenter__()->AuthStorage`; immutable `client_storage:AsyncKeyValue` available only after dirty barrier; `async finish(*,settled:bool,failed:bool)->None`; `async __aexit__(exc_type,exc,tb)->None` refuses implicit clean sealing unless finish succeeded. finish is once-only/idempotent after terminal success; any failure permanently prevents clean sealing. Runtime supplies truthful settlement and invokes no writes itself.
- `validate_stored(collection:str,value:dict[str,object],*,config:AuthConfig)->None` and `validate_envelope(value:dict[str,object],*,limit_bytes:int)->None` are pure, fixed-error validators.
- `async preflight_entry(*,collection:str,key:str)->None` is private to the storage handle: validates metadata/path and bounded raw bytes as below before a maintained method can coerce, expire or delete them.
- `EnvelopeGuard(key_value:AsyncKeyValue,*,owner:AuthStorage)` and `DecodedGuard(key_value:AsyncKeyValue,*,owner:AuthStorage)` retain that single owner, perform no constructor I/O and expose the maintained signatures `async get(key:str,*,collection:str|None=None)->dict|None`, `get_many(keys:Sequence[str],*,collection:str|None=None)->list[dict|None]`, `ttl(key:str,*,collection:str|None=None)->tuple[dict|None,float|None]`, `ttl_many(keys:Sequence[str],*,collection:str|None=None)->list[tuple[dict|None,float|None]]`, `put(key:str,value:Mapping[str,Any],*,collection:str|None=None,ttl:SupportsFloat|None=None)->None`, `put_many(keys:Sequence[str],values:Sequence[Mapping[str,Any]],*,collection:str|None=None,ttl:SupportsFloat|None=None)->None`, `delete(key:str,*,collection:str|None=None)->bool`, `delete_many(keys:Sequence[str],*,collection:str|None=None)->int`. All are async, including abbreviated names. Exactly six named collections; None/unknown collection refuses. Bulk inputs must have matching lengths and bounded total input <=storage_record_bytes; no unbounded materialization. Any other store entrypoint is unavailable to the provider; added dependency use requires an amendment.

Construction (outer to inner) is provider-owned PydanticAdapters → DecodedGuard → maintained FernetEncryptionWrapper(raise_on_decryption_error=True,fernet=Fernet(storage_key)) → EnvelopeGuard → maintained FileTreeStore. FileTree uses data_directory=auth_root+"/credentials", metadata_directory equal to that directory, both maintained FileTreeV1 sanitization strategies on the same directory, auto_create=False, default BasicSerializationAdapter. No replacement serializer, database, token index or monkeypatch. The guards forward writes to those owners; they do not generate token records. EnvelopeGuard rejects plaintext dicts **before** Fernet sees them, including get/ttl/bulk paths. Exact envelope keys are __encrypted_data__ (nonempty ASCII standard base64 string, validate=True and roundtrip-canonical, length<=storage_record_bytes) and __encryption_version__ (type exactly int, value1). No missing/extra key, bool version, empty payload or wrong encryption version. Fernet's own URL-safe token is the bytes inside this outer standard base64; do not confuse the two encodings.

All methods validate collection, key and path first. Before reads/deletes/overwrites, preflight any existing entry (including expired records), so malformed present data cannot become an innocent miss through TTL culling. A genuinely missing entry remains None/False; a missing referenced row subsequently refuses authentication. Reads validate envelopes then decoded records before provider Pydantic. Writes validate decoded dict and finite TTL, then Fernet generates the envelope, then EnvelopeGuard validates it and requires the maintained JSON-encoded envelope size plus512 bytes of fixed FileTree version/date/key-syntax allowance <=storage_record_bytes before the maintained writer runs. This allowance bounds the selected version1/aware-ISO-date metadata shape; it is a serialization safety margin, not a deployment record-size default. Bound decoded input to storage_record_bytes before encryption as well. Bulk prevalidates every element before first write; partial I/O failure still fails the lifetime, not rollback. ttl/ttl_many must validate the value as strictly as get and preserve the maintained remaining TTL result, including None. TTL input is None or finite positive int/float excluding bool, passed unchanged; no local rounding/new expiry. Unknown/malformed present data latches on_failure and raises fixed storage_unavailable, never logs raw bytes or returns None. Wrapper preflight costs are separate from the seven maintained call budgets.

Byte-level duplicate rejection is explicit preflight, not a claim about FileTree's ordinary json.loads. Under the lifetime lock and the private/no-concurrent-writer assumption, preflight opens each touched regular file with O_NOFOLLOW, checks uid/mode/nlink=1, reads at most storage_record_bytes+1, rejects excess before unbounded library read, checks pre/post fstat identity and parses strict UTF-8 with object_pairs_hook rejecting duplicates at every depth, parse_constant rejection and recursive surrogate validation. It validates the entire FileTree JSON entry and envelope, uses the same maintained Fernet instance to decrypt the bounded ciphertext, then strict-decodes/validates the resulting JSON bytes before the library subsequently reads/decrypts those unchanged bytes normally. This deliberate duplicate read/decrypt is a trust preflight, never an alternate serializer or returned token source. No await permits another provider operation between preflight and maintained call because the outer gate owns all operations. A hostile concurrent same-UID writer is **outside** the guarantee; path-based FileTree I/O cannot exclude one. No dict wrapper alone proves byte uniqueness.

FileTree entry shape from BasicSerializationAdapter is exactly version=1, value=the encrypted envelope, created_at=ISO datetime string; expires_at is an ISO datetime string when TTL is present and is **absent**, not null, for no TTL. Dates must parse as aware finite timestamps. No extra fields in this selected writer profile (key/collection are not emitted by DiskCollection.put_entry). E32 structural entry at synthetic epoch1000: {"version":1,"value":{"__encrypted_data__":"<standard-base64-of-maintained-Fernet-token>","__encryption_version__":1},"created_at":"1970-01-01T00:16:40+00:00","expires_at":"1970-01-01T00:31:40+00:00"}. The angle-bracket string is explicitly a construction notation, not valid ciphertext: the executable fixture derives it with base64.b64encode(Fernet(E10.storage_key).encrypt(maintained_serialized_record_bytes)).decode("ascii"), preserving maintained serialization. Mutations version=true, plaintext value, duplicate raw value key, expires_at=null or ciphertext flip refuse.

At startup/seal enumerate all six collection directories and their six *-info.json metadata files using maintained sanitizers. Each metadata file must have exactly version=1, collection=the exact logical name, directory=the exact canonical child directory, created_at=aware ISO datetime. No extra/missing metadata, foreign child, link, temporary artifact or path outside root. Preflight metadata bytes before FileTree._load_collection_infos/any from_file call; metadata remains immutable for the lifetime. On each entry preflight recheck metadata identity/size/hash against that validated snapshot before path-based library access. All files bounded by storage_record_bytes; scan/hash incrementally and keep only manifest entry metadata, not all token contents. Full namespace integrity scan at startup and clean seal includes expired entries before maintained TTL logic. Missing metadata never triggers implicit library recreation. Initial Q fixture setup creates directories/metadata through maintained setup, registers E19, then seals generation0 under isolated exclusive ownership; runtime never provisions.

Strict decoded collection table: all listed persisted keys are required including nullable defaults, unknown keys refuse; str means exact str, timestamps mean finite int/float excluding bool (0<=value<=P), integers mean exact int. Recursive mappings/lists reject nonfinite values, surrogates and non-string keys. Type validation happens before Pydantic; keep its serializer and defaults for creation only.

| Collection / maintained model | Exact decoded field rules |
|---|---|
| mcp-oauth-transactions / OAuthTransaction | E13 exact keys. txn_id/client_id/client_redirect_uri/client_state/code_challenge_method:str; code_challenge:str\|null; scopes:list[str]; created_at:timestamp; resource/proxy_code_verifier/csrf_token/consent_token:str\|null; csrf_expires_at:timestamp\|null. Configured client/redirect, S256, scopes=[] and resource=config.resource for this profile; nullable protocol intermediates stay nullable. |
| mcp-authorization-codes / ClientCode | E14 exact keys. code/client_id/redirect_uri/code_challenge_method:str; code_challenge:str\|null; scopes:list[str]; idp_tokens:TokenData; expires_at/created_at:timestamp. Exact client/redirect/S256/empty scopes; null challenge cannot satisfy redemption even though storage shape allows it. |
| mcp-upstream-tokens / UpstreamTokenSet | E15 exact keys. upstream_token_id/access_token/token_type/scope/client_id:str; refresh_token:str\|null; refresh_token_expires_at:timestamp\|null; expires_at/created_at:timestamp; raw_token_data:TokenData or empty dict (maintained default). Match configured client. Empty/null maintained optional state is structurally valid; finite-refresh authorization separately requires nonempty refresh and finite expiry. |
| mcp-jti-mappings / JTIMapping | E16 exact keys. jti/upstream_token_id:str; created_at:timestamp. Key must equal jti; referenced upstream row must exist for authorization. |
| mcp-refresh-tokens / RefreshTokenMetadata | E17 exact keys. client_id:str; scopes:list[str] (empty here); expires_at:strict integer\|null; created_at:timestamp. Exact client. Null expiry cannot authorize finite-refresh policy. Key remains maintained token SHA256. |
| mcp-oauth-proxy-clients / ProxyDCRClient | E18 exact keys **except excluded allow_unregistered_redirect_uris is absent on disk**. client_id=config downstream; redirect_uris and allowed_redirect_uri_patterns=[config.redirect_uri]; token_endpoint_auth_method="none"; grant_types=["authorization_code","refresh_token"]; response_types=["code"]; scope=""; all remaining E18 fields null, including client_name, all URI/contact/JWK/software/secret/issuer/CIMD/time fields. get_client restores the excluded false default. No unknown fields or coercion. |

TokenData is maintained post-Authlib serialized token mapping: exact required access_token, refresh_token (nonblank str), token_type (Bearer case-insensitive str), expires_in and refresh_expires_in (strict positive integers<=P); optional scope:str and expires_at:strict positive integer<=P. The latter is **derived by Authlib**, allowed only in stored data, and must equal its maintained int(clock)+expires_in arithmetic in capture oracles; ingress removes supplied expires_at before parsing. No alias/id_token/other keys. Do not require absent scope to become a key; maintained output may omit it. Raw-token timestamps remain finite and compatible with maintained derived values, never coerced strings. E14/E15 show valid constructor shapes; stored captures ordinarily add expires_at. Captured synthetic rows, not handwritten JSON, are the QA ground truth for serialization and TTL.

Retain §4 exact TTL ownership: transaction900, CSRF900, code300 seconds; maintained derived upstream/JTI/refresh durations; registration no TTL. Storage shape validation is distinct from request expiry/finite-lifetime authorization: a nullable field is not corruption, an expired well-formed record is not an everlasting grant. Expired deletion follows maintained semantics only after successful preflight; malformed rows are preserved. Suppress the full key_value logger namespace at record creation/dispatch boundary, including key_value.aio.adapters.pydantic and descendants, before any handler/propagation; parent logger filters alone do not filter child records. Install suppression before constructing dependencies, restore process logging ownership only after close, and capture raw LogRecords plus stdout/stderr in exams. Errors contain no cause-chain text; only AuthEvent reaches diagnostics.

### 12.3 remote_supervisor — trusted parent and frontend lifetime

Exact new parent API: `RemoteSupervisor(*,base_raw:bytes,core_raw:bytes,manifest_raw:bytes,auth_config:AuthConfig,dependency_sha256:str,host:Literal["127.0.0.1"],port:int,resolve_secrets:Callable[[],ResolvedAuthSecrets])`; `async __aenter__()->RemoteSupervisor`; `async disable()->None`; `async shutdown()->None`; `async __aexit__(exc_type,exc,tb)->None`. Construction is effect-free, validates the trusted Principal/config equality in §6, and owns no second Broker. Enter owns the single CoreRuntime and creates one spawned frontend via multiprocessing spawn, the existing core socketpair and a separate inherited AF_UNIX SOCK_STREAM lifecycle socketpair. Parent CoreRuntime owns the server/core endpoint; supervisor owns only its control endpoint; child owns the frontend/core endpoint and its control endpoint; both close all unused endpoint copies immediately. Actual socket objects are transferred with the existing reduction convention. No named socket/path/public listener for control; no credentials/jobs/tasks over it. Secret resolution remains an explicit activation dependency in frontend startup, with Q supplying the explicit synthetic resolver callable.

remote_service remains CLI/config and app composition; it delegates parent lifetime to RemoteSupervisor and child HTTP/auth lifetime to `async run_frontend(*,core_socket:socket.socket,control_socket:socket.socket,frontend:FrontendConfig,auth_config:AuthConfig,dependency_sha256:str,host:Literal["127.0.0.1"],port:int,resolve_secrets:Callable[[],ResolvedAuthSecrets])->None` in remote_supervisor. This is a trusted spawn entry coroutine, not lifecycle control data. Port is strict int1..65535, host exactly127.0.0.1; no defaults. The spawn-safe resolver is a trusted top-level callable, invoked only in the child before AuthRuntime enter; no secret bytes enter control messages. Q supplies a top-level synthetic resolver. Real resolver implementation/provisioning remains an explicit activation dependency. dependency_sha256 is the reviewed lock byte digest and is supplied unchanged to AuthRuntime. The single HTTP server admits only after admit, with one worker and no reload/access logs; no alternate application instance. The frontend app adopts/closes CoreClient once, enters AuthRuntime then the existing manager, and owns server admission. The parent is the only CoreRuntime shutdown owner. §6 core amendments are proposed only after accepted008 B and exclusive scope transfer; frozen current008 and exams are not modified here.

Control wire is unsigned4-byte big-endian length followed by strict canonical UTF-8 JSON, no raw body/trailing payload. Length1..256 checked **before** allocating; schema is one of the exact messages below, all keys required, no extras, duplicates, bool-as-int, whitespace variants or arbitrary reason text. One lifetime per pair, no sequence/owner/request identifiers and no retry; state ordering disambiguates messages. This is a private finite state machine, not a broker request protocol.

| Exact wire JSON | Sender / when |
|---|---|
| {"event":"ready","v":1} | Child once: CoreClient bound, AuthRuntime ready and manager entered, HTTP admission still closed. |
| {"event":"admit","v":1} | Parent once after ready and parent core ready. Child opens admission only on receipt; parent never infers readiness from spawn. |
| {"event":"disable","v":1} | Parent once from ready/admitted state for trusted disable or shutdown; closes new parent-side orchestration admission immediately. |
| {"event":"closed","v":1} | Child once after disable: all new HTTP/OAuth/MCP/transfer admission closed, queued auth refused; active auth may still be settling. |
| {"event":"failure","reason":"auth","v":1} | Child on latched auth/transport/storage/settlement failure, after synchronously closing admission. |
| {"event":"failure","reason":"startup","v":1} | Child failed before ready; no admission occurred. |
| {"event":"revoked","v":1} | Parent only after closed or failure, and **after** awaiting core.disable_owner(construction_bound_owner_id), including ticket revocation/stream descriptor closure. |
| {"clean":true,"event":"drained","v":1} | Child after revoked, active auth settled, manager/client closed and successful AuthStorage clean seal; child then closes control and exits. |
| {"clean":false,"event":"drained","v":1} | Child after revoked with proven operation/process-side settlement and failed/dirty preserved storage; no clean claim, then close/exit. |

E33 normal sequence ready→admit→disable→closed→revoked→drained(clean=true)→EOF. E34 auth failure ready→admit→failure(auth)→revoked→drained(clean=false)→EOF. An active operation finishing cannot reopen admission or send ready again. A late closed racing a failure is accepted once as redundant closure evidence, never a second revoke; duplicate ready/admit/revoked/drained or messages from wrong sender/state are protocol failure. Simultaneous trusted disable and child failure coalesce into one disable_owner call and one revoked reply. No failure reason may include credential, path, exception or job ID.

Parent disable() returns successfully only after revoked was sent and drained(clean=true) received plus child exit; core revocation completion is the minimum meaning of acknowledgment, not merely a signal. Failed/unclean drain raises a fixed failure, while tickets already revoked remain revoked and accepted jobs remain. Repeated disable after completed disable is idempotent; after failure it returns the same fixed refusal without relaunch. After revoked, any new storage-close failure sends drained(clean=false), not another failure/revoked cycle. Child startup failure also causes parent disable_owner before cleanup. Do not clean-seal an auth-failed lifetime even if it later settles.

Idle control sockets have no read timeout while the service is healthy; absence of an expected message is not failure. All startup ready/admit, partial-frame reads/writes, disable→closed, core revoke wait and revoked→drained waits have separate monotonic intervals bounded by mandatory config.operation_ms; no byte trickle resets a frame deadline. On interval expiry, close admission and fail parent; terminate/join child with an additional operation_ms grace then kill/join if needed. Failure to prove death is a retained failed supervisor with locks/core still owned, no relaunch. Do not cancel an in-progress core.disable_owner and report success: shield it to finish; if the deadline is exceeded, report failure while continuing closure, never claim revoked early. Q measures this uncertain/settlement state separately.

Unexpected child EOF/death before drained: parent shuts down the runtime-owned server socket with shutdown(SHUT_RDWR), lets runtime close its adopted endpoint, and awaits the frozen attach result's Session.closed future; it retains CoreRuntime and accepted jobs. Ordinary unexpected death alone does not revoke every owner ticket as an explicit auth-failure signal would; it closes this endpoint/leases. If parent already observed auth failure/disable, it completes disable_owner even if the child then dies. The live supervisor latches failed/no-auto-relaunch either way. Child sees unexpected parent EOF: immediately closes all HTTP admission, refuses waiters, settles or fails auth, closes CoreClient and exits; no reconnection or standalone service. Parent loss cannot be promised to preserve jobs beyond core's own recovery contract. Explicit operator restart is a new authorized startup, subject to §8 validation and any surviving failure latch—not automatic repair or bypass.

shutdown() first runs the disable/drain sequence where possible, then invokes the existing CoreRuntime exit/shutdown exactly once; this explicit parent shutdown has core's own accepted-job shutdown semantics. A frontend exit/disable/auth failure never calls it implicitly. There is no public disable opcode, HTTP owner argument, duplicate broker, job queue, token protocol or accepted-job cancellation in lifecycle messages.

### 12.4 Remaining gates

The authorized technical choices are fixed in v3. Independent D may find defects; this author assigns no D/A score. Local acceptance still requires committed protected exams, accepted core B before integration, source-qualified transport/storage/control fault lanes, reviewed dependency lock and candidate resource comparison. No missing live identity is a reason to block synthetic D/A.

Activation separately requires Forest's identity/app/client and numeric deployment/file/retention policy, qualified filesystem and provisioning/secret resolver/service-manager configuration, exact ingress/server limits, real provider and Claude OAuth/deadline/file-materialization/native-tool evidence. Empty identity scopes only; read:user requires a later explicit source-qualified amendment. Operator recovery remains unimplemented and unauthorized. All-process-loss/stale-image histories in §8 are excluded guarantees, not errors that QA may relabel solved.

## 13. Revision source ledger and read-only verification

The table below retains the v2 author's historical source ledger; it does not assert fresh v3 reads of every historical item. V3 reads the two supplied drafts (input hashes a1cf29d5e7f4c28b9a81c5da1cad5c84d5eb29512bfd6c30bbc33ab683d8a34a and 6d004e6e9b4d1a3506c622ab241102ea55a451d365c9fc03dfeb15178fb54fd5), root5d98d77 directions, independent baseline/closure, frozen core contract/exam conventions and pinned dependency source. No current core implementation is read as normative authority. Only document syntax/example/hash checks are performed; no tests, imports of candidate source, providers, installs, secrets/live state, workers or commits.

| Historical v2 evidence source | Previously recorded SHA256 |
|---|---|
| /Users/velox/cortados/contracts/CONVENTIONS.md | 0e09b19a664befb03073d9160765340debea72bec6ed6b64344afcb50260afe6 |
| /Users/velox/cortados/context-staging/research/ai-coding-std-workflow/CONVENTIONS.md | 2d8a2c18fbce516b4749d77a5e5771e31751ee7c17160cc0c9afcdbffa2b2132 |
| /Users/velox/cortados/context-staging/research/ai-coding-std-workflow/RUBRIC.md | cccae8d0e13997e4a639d846a0f771ef15ed1017bcf0b4def24963298a9cf404 |
| /Users/velox/cortados/context-staging/research/ai-coding-std-workflow/ORCHESTRATION.md | 077ba776ba250d4af88455f59b7bdfb42f34e734f4389090eec9f6f1466a5673 |
| /Users/velox/cortados/context-staging/research/ai-coding-std-workflow/PLAYBOOK.md | 0dbbb7436bdbc72ed190be199d62a59c07d71a4baa9ed64021aedc2dc6781664 |
| /Users/velox/cortados/context-staging/research/ai-coding-std-workflow/standards/00-ENGINEERING-STANDARD.md | d55295920cd6492759611e9177c52e4f08e151da88355b8f78ff1cf4e6d79b94 |
| /Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp/server/auth/providers/github.py | 13a89114465210ba0fcce39b4ff3e7acb09a699a8a5de0e97223c419f53141ba |
| /Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp/server/auth/oauth_proxy/proxy.py | 58f13a58bd31373c13ee166f94bf3726dadd6bb86a6c8da89de3b09a627d26f0 |
| /Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp/server/auth/oauth_proxy/models.py | b2f45bc919484355f89ad10bf06d84cf45ebf5ed9af51b932ea0800c665242f8 |
| /Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp/server/auth/oauth_proxy/consent.py | 2810a5bb3df3f1548b04601621d7b9363b88f711991696974244db91ffc823f3 |
| /Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp/server/auth/auth.py | 94405db4b54afb041e41c7ba70ac31781f67f1f769c017d9213238edd0cd8b86 |
| /Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp/server/auth/jwt_issuer.py | 9d3bceb3ffb9aa86bf080f7e0a972aeddc7a87e75b483be6af4bf4dc94f7bd9e |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/authlib/integrations/httpx_client/oauth2_client.py | 24c0c3dd6b05c8daeb9d781ce6ceef6c64107ae25a881076fefb82a5995ae463 |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/mcp/server/auth/handlers/token.py | cade6452f07cce106038da586493904c5ca82590610b450a3d4137190324a2db |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/mcp/server/auth/provider.py | 73916a156a98f2bd851b450b31dee2084026871efb7b69e8d149c771269fc2da |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/mcp/server/auth/routes.py | 5a5c5cde95acdbf33c540ae0115e901205282162890fa98941e13f42272bbdd2 |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/mcp/server/auth/middleware/bearer_auth.py | bc83a65fcbc83269310e108611e105ee93c53eecea9085f858233dfbfccbb95f |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/mcp/shared/auth.py | c7237afdd49a6e47a79e0059c6a47a97bf38ff98a0504907a357a9ed4720c396 |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/lib/python3.14/site-packages/key_value/aio/stores/filetree/store.py | c278eb600d78fe01c1770b2352db910b1737f42b82104278b8bf2013a1bbcdab |
| /Users/velox/.cortados-builds/cortados-router-repo/docs/mcp-worker/COWORK-MCP-008.md | 5b9527ac2e71f8ef6bd345031a9dbfe68f0b743d6b004bf7e8db853b923cfc1a |
| /Users/velox/.cortados-builds/cortados-router-repo/docs/mcp-worker/COWORK-MCP-008-EXECUTION.md | 92760ded3f365f7e92074d7b2dd248660ea4b6a6435549acb57978a41f663096 |
| /Users/velox/.cortados-builds/cortados-router-repo/integrations/mcp/config.py | a70778abefc0c1b2563c21b0f5ead6f85885ef0094ed55f5364b8e59730bb778 |
| /Users/velox/.cortados-builds/cortados-router-repo/cortados_router/task_evidence.py | f25082f9d4065a57cf244e5705709ebbf16b5afcb7a510feb6744d7eab8be788 |
| /Users/velox/.cortados-builds/cortados-router-repo/pyproject.toml | f7516262d17c7ddb3f19ba0b1e6e4ac3c99067244c95795a07e16a9b28e3fa7d |
| /Users/velox/.cortados-builds/cowork-mcp-verification/auth-flow-qualification/REPORT.md | 375fd5424df242b8a3e78a003ed675029c4cac6e6eaf020a74890b700befe9fe |
| 495973c:docs/mcp-worker/COWORK-MCP-008-ROOT-REVIEW.md | 88495028f797c33566d0e9a6c4f45dd1c4f1b6700dc31bfcd043879f126dbc8a |
| 495973c:docs/mcp-worker/COWORK-AUTH-009.md | 6e13773a7075d1c0cf75b8cb39ba6f859e14320bcd04dc908b80b68c86f26ecf |
| 495973c:docs/mcp-worker/COWORK-AUTH-009-EXECUTION.md | 254b4238570989288f68ca89222b04bb390ffe77bad841e6ddf83f4dcb0185bb |

Governing standard checkout HEAD was independently read as ab1b521047dcabc1b610caa157e5dfbd544f589b, matching the required pin.

Source anchor implications: proxy.py:719 client factory feeds all three token paths; Authlib:177–180/195–198 hooks precede parsing; proxy.py:838 register_client forces none; auth.py:740–800 composes SDK token/protected metadata routes; routes.py:169/176 defaults confidential methods but S256; filetree/store.py:127–173 fsyncs temp file and renames without parent directory barrier. The latter is why section8 adds qualified lifetime barriers and does not claim provider transactions. Detailed source signatures in sections2/11 and immutable hashes, rather than line numbers alone, define the review target.

Additional final source reads: authlib/oauth2/client.py SHA256 87ff9d5d5b06a1b54b82849f1e27fc4649966bb943dd69fdea8e92cf8dc6a4dc; key_value/aio/stores/base.py SHA256 8f6352bbad37d643a166e00bf06046c1fc7118038e1e80c474f1d1ed22a0736c; key_value/aio/wrappers/base.py SHA256 fc042d46d3394540468f0c04c9387b777e3e6edabe71582614081d98b1c5c890.

### V3 closure source ledger

All paths below are read-only evidence. S and V are the aliases in §1. SHA256 pins include the source-level read/parser limits and maintained plaintext fallback.

| Source | SHA256 |
|---|---|
| V/auth-baseline/OWNER-CLOSURE.md | 9728dde230af719cbecd6c3d4c16affd9387bafb6d89caa39aa88f78f069fc2d |
| V/auth-baseline/README.md | 904d4b556ececf64df46350d9c8fb4fe7f7dbd60f666fdeb4d8d3c10e494faf7 |
| V/auth-baseline/run-3/report.json | 9612c2037882a3775483918cc8776833bc8ae4f7f2e926a89588dc75338b91f2 |
| V/auth-baseline/manifest.json | f27d143b6b314b4231bd69881b84668a98fe9cb78853e6dfcdff02ac9514f8a2 |
| S/httpx/_client.py | c43f941baefe58c91e96d00039e1868fe719d91453026d7db1647194563bff8d |
| S/httpx/_models.py | e3ffc6bb2bf580bc6e6428708a6f247d036220e709f5a72b785392babbd97e6b |
| S/httpcore/_async/http11.py | faa33d6d5ecf8d2405e6fc6cdfbf9e5173881706c88cf7196cd962b97f53b41c |
| S/httpcore/_backends/anyio.py | c7c3e01215d10bc6d5aac77393f609c7c63a77d4ee6f4e82b944b09db9adaa86 |
| S/h11/_connection.py | 93d61155fea4a19a9bb6d056dfac5259a26959d6706bec50554f401c4a3d0ee2 |
| S/h11/_headers.py | 3fb0fe9413711f074b64f2e29a6630acf1bef59923125bef25925d6408123fee |
| S/authlib/integrations/httpx_client/utils.py | acae89ef0766dd13f9945b5a543fab44a537f2bbd7f283d43ef33b26a4e9d0c9 |
| S/authlib/oauth2/rfc6749/wrappers.py | 4e0cac44f62561c05a324ff4bd52b6e7f5ee41b5ede616fcb608b4b7467b487a |
| S/key_value/aio/wrappers/encryption/base.py | 84a2c8b39e7dbbe90c75521939d575c1a1a4192bc725d1431b844403b848582f |
| S/key_value/aio/wrappers/encryption/fernet.py | d4dd8a48308586abb1bbcd1d1d8f7fea979f0369606d393c6f3f8a7b2ccca18f |
| S/key_value/aio/stores/filetree/store.py | c278eb600d78fe01c1770b2352db910b1737f42b82104278b8bf2013a1bbcdab |
| S/key_value/aio/stores/base.py | 8f6352bbad37d643a166e00bf06046c1fc7118038e1e80c474f1d1ed22a0736c |
| S/key_value/aio/_utils/serialization.py | 114c280484bf625866cba7c87f024ec9f8fb12dd1e0b08928d98c4ff6307284e |
| S/key_value/aio/_utils/managed_entry.py | 6393025e0503bfdbcb9cc9f574453d64ba9a7cb1ff6089056539069cea138740 |
| S/key_value/aio/_utils/time_to_live.py | 03a6945166962967cf8f0364c9b3d1ae4088e2daa7c77e219b92e2363b2b9c7f |
| S/key_value/aio/adapters/pydantic/base.py | 9e1d7819708599ba934f7536799a4d8c7d3b06b4d998ff068f499c6bc8ebeb9f |
| Router/acceptance/cowork_mcp/README.md | a23cb3f5bb8f0100bfad98370f7ec8a06b19878e6bd779ab4e7fea539c2edea7 |
| Router/acceptance/cowork_mcp/MANIFEST.json | e0680bf5687e46b339fcea469663a17595c5bad08e1897149b2af45f2aaf2852 |
| Router5d98d77:docs/mcp-worker/COWORK-MCP-008-ROOT-REVIEW.md | 832862f2aeefc2a96fee49c4d5973e3a79080068d8bd6c7555d7d648680f778a |
