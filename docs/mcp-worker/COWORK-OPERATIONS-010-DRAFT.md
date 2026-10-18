<!-- PURPOSE: Propose the remaining offline operator boundary for Cowork.
     CONTRACT: RT-COWORK-OPERATIONS-010 v1; proposal only.
     DEPENDS: Frozen008 and proposed009v4; independent review and explicit activation.
     CEILING: No source, secret, service, permission or frozen-contract changes. -->
# Cowork operator setup — bounded proposal

```yaml
contract_id: RT-COWORK-OPERATIONS-010
contract_version: 1
status: draft
owner: Forest
orchestrator: root
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
depends_on: [RT-COWORK-MCP-008, RT-COWORK-AUTH-009]
dispatchable: false
execution_proposal: COWORK-OPERATIONS-010-EXECUTION-DRAFT.md
attempt_ledger: inherited; no reset or new source attempt
```

This isolated proposal closes only secret resolution, first offline provisioning, and parameterized service preparation. LOCAL CORE008 has a rejected valid C1–C10 source attempt and an authorized correction in progress. Normative008 remains frozen. Auth009v4 is under independent D; its source has not started. Nothing here accepts either implementation, dispatches a builder, or authorizes activation. The user's explicit two-document scope overrides the general worker workflow; no nested worker is used.

The conventions at `/Users/velox/cortados/contracts/CONVENTIONS.md` and their local standard, RUBRIC, ORCHESTRATION and PLAYBOOK govern future gates. These files are review inputs in the isolated author cwd, not a second canonical contract home. Root decides incorporation and index ownership after review. No self D/A score is assigned.

## 0. Canonical definition registry

Definitions O0–O9 are the sole registry for this extension. They are [FIXED] proposed profiles, conditional on acceptance. R010-* is the sole obligation registry. Inherited types, records, serializers, marker format, six collection names, control frames and auth/core state machines retain 009 D0 and frozen008 definitions; they are referenced rather than redefined. Examples are synthetic operands, never live defaults.

### O0. Evidence names and dependency identity

`R` = `/Users/velox/.cortados-builds/cortados-router-repo`; `V` = `/Users/velox/.cortados-builds/cowork-mcp-verification`; `F` = `/Users/velox/.cortados-builds/cowork-mcp-research/fastmcp/fastmcp_slim/fastmcp`; `S` = `V/auth-research-venv/lib/python3.14/site-packages`.

Reviewed bytes (SHA256, not an assertion of source acceptance):

| Reference | SHA256 |
|---|---|
| R/docs/mcp-worker/COWORK-AUTH-009.md | 5bdd0a9a6d9f0ca9b8383c3e7d9d8adaf2048802e7f63f012e6ce11dcd32da0a |
| R/docs/mcp-worker/COWORK-AUTH-009-EXECUTION.md | e7139a46f855aff9e1ee47afd02601e1524bd227178673cf963b95ef54a1bdfa |
| F/server/auth/oauth_proxy/proxy.py | 58f13a58bd31373c13ee166f94bf3726dadd6bb86a6c8da89de3b09a627d26f0 |
| F/server/auth/oauth_proxy/models.py | b2f45bc919484355f89ad10bf06d84cf45ebf5ed9af51b932ea0800c665242f8 |
| F/server/auth/providers/github.py | 13a89114465210ba0fcce39b4ff3e7acb09a699a8a5de0e97223c419f53141ba |
| S/key_value/aio/stores/base.py | 8f6352bbad37d643a166e00bf06046c1fc7118038e1e80c474f1d1ed22a0736c |
| S/key_value/aio/stores/filetree/store.py | c278eb600d78fe01c1770b2352db910b1737f42b82104278b8bf2013a1bbcdab |
| V/auth-dependency-lock/server-requirements.txt | 6099038ad1842f32dd9e4b54946dd3494cb97027851d71c8a066abc6f784cb88 |
| R/integrations/mcp/config.py | a70778abefc0c1b2563c21b0f5ead6f85885ef0094ed55f5364b8e59730bb778 |
| R/pyproject.toml | f7516262d17c7ddb3f19ba0b1e6e4ac3c99067244c95795a07e16a9b28e3fa7d |

Final-check dependency drift: both supplied009 hashes matched at initial inspection, but a concurrent root v5 precision amendment changed the current parent to `e9b2c0d1c22954da894643b8638ab9e43aa22f2b7e9fef03df2f10087f022174` and execution to `83052dcde6977750ee633d5a6fb7c179e30f466c75181981ed442eaec94d142b`. Its appended ledger reports v4 independent D4/33 and a precision amendment concerning integer notation, HTTP/1 class/version names and header accounting; a full independent rescore remains pending. Those appended statements are observed concurrent evidence, not this author's acceptance or a complete diff review. This010 proposal remains pinned to the supplied reviewed v4 interfaces. O010-01 now explicitly includes root reconciliation against v5 before any integration; no original file was written here.

The corrected dependency lock qualifies imports on CPython3.14.6/macOS arm64 only. `fastmcp-slim[server]==3.4.9`, `Authlib==1.9.1`, and unchanged `mcp==1.30.0` remain the proposed optional remote graph. The plain slim package failed provider import. Release wheel/interpreter/install-tree hashes remain future evidence. Current core source is mutable work in progress, not a pinned accepted dependency.

### O1. Configuration and private layout

New closed immutable local record `OperatorConfig` belongs to `integrations.mcp.remote_auth_config`. Constructor convention and strict JSON rejection are 009 D0.1/D0.3. Every field is required; no environment expansion, discovery or default path.

| Field | Type and predicate |
|---|---|
| schema | literal `cortados.cowork-operations.v1` |
| release_root, interpreter, working_directory | absolute canonical path strings; working_directory equals release_root; interpreter is its qualified venv executable spelling |
| base_config, core_config, auth_config, native_manifest, dependency_lock | absolute paths to immutable regular nonsecret release configuration/evidence files |
| dependency_sha256 | lower-case 64-hex SHA256 of dependency_lock bytes, equal reviewed release digest |
| secret_root | existing operator-owned 0700 absolute directory; disjoint from release, auth, core/task/blob/staging roots |
| secret_files | closed mapping with exactly the three opaque refs from AuthConfig as keys; distinct values are single basenames matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, excluding `.` and `..` |
| upstream_secret_bytes | strict integer 1..9007199254740991; operator-selected byte bound, not an implicit provider limit |
| host | literal `127.0.0.1` |
| port | strict integer 1..65535; distinct from existing private destination8432 and optional S1 port |
| service_mode | enum `foreground`, `launchd-manual` |
| service_label | nonblank ASCII `[A-Za-z0-9][A-Za-z0-9.-]{0,127}`; separate from every existing service |

Nonsecret reference mappings are trusted operator configuration, never MCP input. Distinct references, filenames and decoded signing/storage key bytes are required; no derivation from the upstream secret. Independence also requires operator provenance: software can reject equality but cannot prove entropy or independent generation. Signing file is exactly32 raw bytes; storage file exactly44 canonical Fernet key bytes; upstream file is nonblank UTF-8 bounded by upstream_secret_bytes with no NUL or CR/LF. No trimming or implicit newline stripping. No secret file reads occur in check or the parent launcher.

Recommended layout, wholly parameterized: a new private release root containing a non-editable wheel installation in `venv/`, read-only `config/` and reviewed `requirements.lock`; separate existing `secret_root`; separate existing `auth_root`; separate core roots. Release directories have no group/other access and no write permission after qualification; config/lock files are0400, directories0500, executable files0500 and other package files0400. The venv executable's possible symlink is an explicit qualified exception: pin its final interpreter target/hash and full link chain, do not silently resolve away venv identity. No mutable `current` symlink, editable install, source-cwd import, `PYTHONPATH`, or package mutation during a lifetime. The service uses the pinned absolute interpreter with `-I -B -m integrations.mcp.remote_service`; an actual installed-package subprocess is an exam prerequisite. Read-only filesystem modes are an operational immutability rule, not protection against a malicious same-UID owner or root.

Secret/state paths reject symlink ancestors, nonowner entries, group/other permissions, unexpected ACL grants, hardlinks and special files. File reads use descriptor-relative O_NOFOLLOW opens, regular-file/nlink/uid/mode checks, bounded reads and pre/post fstat identity checks. ACL inspection on the target filesystem is a qualification gate; mode bits alone are insufficient. No secrets are included in release manifests, task artifacts or diagnostics. Secret file hashes are not emitted; 009 marker-internal bindings remain encrypted.

### O2. Proposed callable registry

These are proposed changes to future owners, not observed imports. Python types come from pathlib, typing, collections.abc, httpx and 009 canonical imports; `Callable`, `Awaitable` and `Literal` retain those meanings. All constructors are effect-free. No new utility module is proposed.

| Owner | Exact proposed callable/config shape |
|---|---|
| remote_auth_config | `load_operator_config(raw:bytes,*,auth_config:AuthConfig)->OperatorConfig` — pure schema/cross-reference validation |
| remote_service | `validate_operator_setup(*,operator:OperatorConfig,base_raw:bytes,core_raw:bytes,manifest_raw:bytes,auth_config:AuthConfig)->None` — nonsecret release/filesystem/config/dependency/native-manifest validation only; no runtime enter, state mutation, secret read, network or listener |
| remote_service | `resolve_private_secrets(*,operator:OperatorConfig,auth_config:AuthConfig)->ResolvedAuthSecrets` — trusted module-level function; only invoked in spawned auth/provision child after AuthStorage lock acquisition; fixed-safe exceptions |
| remote_auth_storage | Replace `secrets` constructor operand with `resolve_secrets:Callable[[],ResolvedAuthSecrets]`; add `purpose:Literal['restart','initial']='restart'`; retain config/dependency_sha256/on_failure and all existing methods. Add read-only `resolved_secrets:ResolvedAuthSecrets`, accessible only by entered owner while lock held; no serialization. `client_storage` remains inaccessible before the dirty barrier. |
| remote_auth_runtime | Replace AuthRuntime constructor `secrets` operand with `resolve_secrets:Callable[[],ResolvedAuthSecrets]`; runtime always constructs AuthStorage with purpose='restart'; provider receives storage.resolved_secrets only after enter. |
| remote_auth_provider | `make_provider(*,config:AuthConfig,secrets:ResolvedAuthSecrets,client_storage:AsyncKeyValue,identity_client:httpx.AsyncClient,on_failure:Callable[[],None])->StrictGitHubProvider` — single extraction of 009 D0.16 constructor wiring; no routes/enter/network/registration; no second issuer |
| remote_auth_runtime | `async provision_initial(*,config:AuthConfig,dependency_sha256:str,resolve_secrets:Callable[[],ResolvedAuthSecrets])->None` — offline composition using AuthStorage purpose='initial', make_provider and maintained register_client; no AuthRuntime enter, core, listener or token flow |
| remote_supervisor | `run_initial_provision(*,auth_config:AuthConfig,dependency_sha256:str,resolve_secrets:Callable[[],ResolvedAuthSecrets])->None` — top-level synchronous multiprocessing spawn target calling asyncio.run(provision_initial(...)); no CoreRuntime or RemoteSupervisor instance |

RemoteSupervisor and run_frontend signatures/control frames stay exactly 009 D0.14. The parent passes `functools.partial(remote_service.resolve_private_secrets, operator=operator, auth_config=auth)` as their zero-argument callable. Only immutable nonsecret records are pickled. This is an importable top-level function bound with public operands, not a lambda, closure, dynamically imported plugin or pre-resolved secret record. Entry code is guarded by `if __name__ == '__main__'`; child import has no effects. Runtime passes the callable through to storage without invoking it. The provisioning parent likewise spawns exactly once and observes process exit; no pipe carries resolved material. Exit0 means completed initial seal; exit1 is fixed refusal/failure. Parent death or missing successful child completion is uncertainty, not permission to retry.

`finish(settled=True,failed=False)` in initial purpose has a stronger predicate: one verified configured public-client row and no token rows in the other five collections; all six maintained metadata files/directories exact; registration completed once; no failed latch, pending artifact or open writer. The storage owner verifies the raw/decoded persisted shape using 009 trust guards, not a caller-provided success boolean alone. Its first successful seal is generation0 with the initial run_id. No new marker schema or external journal.

### O3. Maintained APIs and dispositions

Owner searches: `rg -n 'secret|keyring|keychain|launchd|LaunchAgent|KeepAlive|register_client|resolve_secrets' R/integrations R/cortados_router R/bin R/pyproject.toml`; `rg --files R/integrations/mcp`; review009 D0.9–D0.21 and EXECUTION B1/B2. No matching persistent operator resolver/service installer exists in those source paths. Quota-observation nonce keys are transient integrity owners, not OAuth secret storage. An attempted R/scripts search found no directory; no nonexistent script is adopted.

| Concern | Disposition and source-qualified reason |
|---|---|
| Base configuration | Adopt R/integrations/mcp/config.py:136 load_config; credential-free closed configuration, existing native environment/permissions owners remain unchanged. Its connector environment-token references are not suitable for the no-secret-environment requirement. |
| Authentication store | Extend proposed009 remote_auth_storage; sole lock/marker/barrier/trust owner already specified. Creating an operator marker writer would duplicate its invariant. |
| Serialization/collections | Adopt S/key_value/aio/stores/base.py:175 `async setup_collection(self,*,collection:str)->None`; FileTreeStore inherits it. S/.../filetree/store.py:533 prepares directories and :353 DiskCollectionInfo.create_or_get_info writes maintained metadata. No handwritten metadata/credential JSON. |
| Registration | Adopt F/.../proxy.py:838 `async register_client(self,client_info:OAuthClientInformationFull)->None`; :780 `async get_client(self,client_id:str)->OAuthClientInformationFull|None`. Registration uses `_client_store.put`, not an insert-only transaction, so the empty-state predicate belongs before it. |
| Provider construction | Extend proposed remote_auth_provider by extracting existing009 wiring into make_provider; both runtime and initial composition reuse it. F/.../proxy.py:485–548 defaults derive keys/store paths and tolerate decrypt misses; explicit independent keys/store prevent those unwanted defaults. |
| Launch/CLI | Extend proposed remote_service/remote_supervisor; one live RemoteSupervisor, no new broker or daemon. R/INSTALLATION.md:121 reports no launchd job installed; ordinary stdio release is separate. |
| Resolver | Build-new bounded function inside proposed remote_service; stdlib filesystem reads reuse existing configuration conventions, not a new credential DB or source module. |
| Packaging | Adopt non-editable release pattern from INSTALLATION and009 optional remote extra/lock; no new dependency or ordinary-package change in010. |

Resolver options: (a) environment variables violate the boundary and are rejected; (b) a parent-read pipe violates child-only resolution and is rejected; (c) macOS Keychain could avoid plaintext secret files but no reviewed project owner/API, noninteractive access qualification or approved entitlement exists, so return it to root if desired rather than invoking `security` or introducing a dependency; (d) private operator-provisioned separate files allow a bounded stdlib implementation with no subprocess or new credential service and are recommended, subject to Forest's permission/backup decision. The function does not generate, migrate, rotate or search for credentials. Python cannot promise zeroization of immutable strings/bytes, privileged-process memory secrecy, or exclusion of a malicious same-UID reader. Disable core dumps for this process before resolution; inherited crash-report/debug policy remains a deployment qualification.

### O4. Initial-only protocol and state

Initial admissible root: an existing empty0700 auth_root containing only an existing zero-length0600 regular single-link `auth.lock`, provisioned by the operator. No credentials directory, marker or pending file exists. Requiring the stable lock in advance avoids racing first lock creation and makes refusal byte-preserving. Startup also requires the existing lock. No command here creates the root or lock. File locking is nonblocking exclusive on that same inode; descriptor/entry identity is rechecked under lock.

State enum for initial invocation: `preflight`, `locked`, `resolved`, `dirty_initial`, `registered`, `sealed`, `refused`, `uncertain`. This is an invocation lifecycle, not a persisted credential entity. Refused has no initial mutation; uncertain preserves whatever exists and requires operator review.

Ordered algorithm:

1. Nonsecret O1 validation and release/config/key-reference binding; set safe logging before dependency construction; no listener/core/native/provider calls. Spawn the provision child once.
2. AuthStorage validates existing auth_root/auth.lock and acquires the lock; rechecks exact initial-empty predicate. Any extra object, even an empty credentials directory or a matching sealed marker, refuses without read/overwrite of its contents. Existing uncertainty never becomes an idempotent success.
3. AuthStorage invokes the bound resolver while locked. A missing/invalid/unavailable file releases owned descriptors and refuses without a marker or directory write. No fallback value or prompt. Three independent values are validated with009 constraints.
4. AuthStorage writes dirty generation0, new UUID4 run_id, exact config/signing/upstream/dependency bindings and null store_sha256 through009 marker primitive. Complete file fsync, Darwin F_FULLFSYNC and auth_root directory fsync precede any credential construction. Any failure enters uncertain, preserving pending/marker bytes.
5. AuthStorage creates credentials0700 once. Only in initial purpose it constructs maintained FileTreeStore with auto_create=True, explicit data/metadata directory and both FileTreeV1 strategies, under umask077. It awaits `setup_collection(collection=name)` for each of the six009 names in canonical order. That API is internal to AuthStorage, not exposed through provider guards. No dummy token put/delete. Verify exact six metadata/directories and permissions; auto_create=False remains mandatory for every ordinary restart. No background cull is started.
6. Offline composition creates the bounded identity client without making a request, then make_provider with explicit storage/secrets. It disables registration options for route safety; no get_routes, ASGI app or routes are needed. Invoke maintained register_client exactly once with009 E19 public profile: configured distinct downstream ID, configured hosted redirect, none authentication, authorization_code/refresh_token grants, code response, empty scope and all optional metadata null. No upstream secret is a downstream field. Read back with maintained get_client and strict stored preflight; the upstream-ID fallback cannot qualify as a stored row. Validate five other collections contain no rows.
7. Await identity-client close and maintained store settlement; there is no provider.close API. AuthStorage.finish performs009 complete file/directory barriers, manifest computation, then its exact atomic encrypted clean replacement with generation0/same run_id. Only after the final barrier succeeds can lock release and exit0 occur. No credential access follows the seal.

Failure before step4 preserves original namespace bytes. Failure at/after step4 leaves all partial bytes and the failed latch; no rollback/delete/re-registration. An observed failure during the final clean rename/barrier returns failure even if clean-looking bytes remain. A second initial invocation refuses on those bytes. Normal restart is not a recovery path for an observed uncertain close. A surviving supervisor never relaunches it. After total process loss, exact matching clean bytes cannot prove the previous barrier was observed, nor detect a full stale-image restore;009's narrowed restart guarantee and excluded rollback/power-loss claims remain unchanged.

### O5. Ordinary restart and service lifecycle

`check` performs only O1 nonsecret checks, including original base loader, proposed service-core loader, principal/origin/root equality, exact release dependency lock and manifest provenance. It is not an auth-readiness probe. `serve` delegates the accepted009 lifetime to exactly one RemoteSupervisor. Child AuthStorage locks first, resolves secrets, validates clean marker/config/key/lock binding and full manifest/layout before dirty generation+1. Startup never calls setup_collection to repair missing metadata, register_client, writes missing records, changes keys or overwrites a failed image. Existing maintained lazy setup is entered only after exact layout validation, so missing metadata cannot silently be recreated.

HTTP admission still follows core bind, ready, admit. Auth-failure stops admission, revokes through the existing parent CoreRuntime owner and settles or kills/joins the child according to009; accepted jobs are not retried/cancelled merely by frontend loss. SIGTERM/interactive stop invokes the same supervisor shutdown and close proof. Child failure does not mean parent CoreRuntime has been shut down. No external manager imposes a short kill timeout over accepted-job shutdown; operator-reviewed forced termination remains a separate action.

Recommended first launch is foreground. Optional `launchd-manual` is a reviewed plist projection, not a new scheduler: exact ProgramArguments are pinned interpreter, `-I`, `-B`, `-m`, module, `serve`, and nonsecret config path flags; WorkingDirectory is release_root; Umask63 decimal (077 octal); KeepAlive=false; RunAtLoad=false; no StartInterval/StartCalendarInterval/WatchPaths/QueueDirectories/Sockets/LaunchEvents; no EnvironmentVariables carrying secrets; no shell wrapper/reloader. Service label and service-manager choice remain explicit pending choices. The plist is never installed/generated by this draft. No automatic boot/login start or failure respawn is permitted. A manual restart after uncertain failure requires an operator recovery disposition, not merely `kickstart`. External watchdogs are excluded. Any launchd exit/kill policy that cannot preserve009 settlement is a blocking compatibility result, not authority to shorten shutdown.

The private443→127.0.0.1:8432 route, ordinary installed MCP and training retain their owners. Proposed public8443/mcp stays inactive. No service check opens a port or changes Tailscale. A future public-ingress design must qualify server header/body/connection bounds alongside009 auth queue bounds; these are separate allocations, not covered by auth body size alone.

### O6. CLI and result profile

Proposed extension of009 CLI (no commands executed here):

```text
python -I -B -m integrations.mcp.remote_service check --operator-config ABS --auth-config ABS --core-config ABS --base-config ABS --native-manifest ABS
python -I -B -m integrations.mcp.remote_service provision-initial --operator-config ABS --auth-config ABS --core-config ABS --base-config ABS --native-manifest ABS
python -I -B -m integrations.mcp.remote_service serve --operator-config ABS --auth-config ABS --core-config ABS --base-config ABS --native-manifest ABS --host 127.0.0.1 --port PORT
```

Each explicit path/host/port equals OperatorConfig; disagreement refuses rather than choosing precedence. No `--secret`, `--force`, `--reset`, `--repair`, `--overwrite`, arbitrary callable path or recovery flag. `check` and provisioning print only fixed lines `Operator setup validated` and `Initial provisioning completed`, respectively, on success; each returns0. Failure returns1 and fixed `Operator operation refused`, without traceback/cause/path/ref/credential. serve emits only009 AuthEvent and inherited safe core events. Auth errors, raw dependency LogRecords, stdout/stderr and process-control output are covered by sentinel tests. `check` success makes no secret-readiness assertion. There is no JSON report or new runtime store to maintain.

### O7. Store aspects and inherited limits

| Aspect | Extension disposition |
|---|---|
| Class | Auth credentials/marker remain canonical009 state; private secret files are operator canonical inputs; release/config are immutable evidence for a particular installation; human measurements are views. |
| Write | Only AuthStorage writes marker/layout; maintained provider/Pydantic/FileTree write client and metadata. This proposal has no secret writer. |
| Query | Three bounded child-only file reads per lifetime; one maintained registration put and readback get for initial flow; six setup_collection calls. File/stat/decrypt/barrier costs measured separately. No invented latency SLO. |
| Mutability | Initial empty namespace only; runtime follows009. Changing any key/config/dependency requires a separately reviewed maintenance path, not initial flow. |
| Identity | Existing owner/collection/key identities, lock inode and009 generation/run_id retained; no new row IDs or token DB. |
| Rebuild | Canonical inputs are not rebuildable. Backup custody, matching-key escrow, restore and deletion remain unapproved; no automatic backup copy is made. |
| Failure | Refuse/preserve unknown state; no cleanup, migration, retry, repair or universal crash-atomicity guarantee. |

### O8. Illustrative valid and invalid flows

Synthetic valid OperatorConfig shape (all paths are fictional fixture paths):

```json
{"schema":"cortados.cowork-operations.v1","release_root":"/fixture/release-a","interpreter":"/fixture/release-a/venv/bin/python","working_directory":"/fixture/release-a","base_config":"/fixture/release-a/config/base.json","core_config":"/fixture/release-a/config/core.json","auth_config":"/fixture/release-a/config/auth.json","native_manifest":"/fixture/release-a/config/native.json","dependency_lock":"/fixture/release-a/requirements.lock","dependency_sha256":"6099038ad1842f32dd9e4b54946dd3494cb97027851d71c8a066abc6f784cb88","secret_root":"/fixture/private-inputs","secret_files":{"fixture-upstream":"upstream.bin","fixture-signing":"signing.bin","fixture-storage":"storage.bin"},"upstream_secret_bytes":4096,"host":"127.0.0.1","port":18443,"service_mode":"foreground","service_label":"invalid.example.cowork-fixture"}
```

The referenced synthetic AuthConfig uses those three refs, a synthetic decimal GitHub subject, distinct upstream/downstream IDs, and all009 required limits. A fixture constructor supplies full008/009 records; the above is an OperatorConfig example, not a complete deploy configuration. Its4096/18443 values are exam operands only. Invalid variants: port=true; an extra field `upstream_secret`; `secret_files` value `../key`; equal signing/storage filenames; altered dependency digest; relative config path; auth_root inside secret_root. Each refuses before secret read or state effects.

Valid provisioning trace: lock-only root → lock → resolve three fixture values → dirty0 barrier → six maintained collection setups → register once/read once → settle → credential barriers/manifest → clean0 barrier → exit0. An immediate repeated initial command returns1 and preserves all bytes. Normal serve then follows clean0→dirty1→clean1 on proven close; next restart becomes dirty2. Existing registration bytes remain unchanged across those restarts.

Invalid provisioning trace: credentials directory with missing marker → refuse before resolver/registration, preserve tree. Invalid restart: dirty1 or clean1 with wrong signing/storage/upstream value, config or dependency digest → no registration/listener/admission; preserve existing files. Key rotation is not equivalent to a new empty namespace.

Resolver success is an in-memory ResolvedAuthSecrets with009 exact types; its representation is redacted. Synthetic tests create fixture values only under separately authorized exam roots; none are embedded here. Invalid resolution includes32-byte storage key rather than44-byte canonical Fernet encoding, 33-byte signing file, invalid UTF-8 upstream value, aliased inode, file swap during bounded read or unavailable root. Failures expose only the fixed result profile.

### O9. Explicit009 amendment packet — not applied

Amend D0.10 and D0.13 with O2's replacement constructor operands and initial-purpose extension; amend D0.14's child sentence so resolution occurs through AuthStorage after lock, rather than before AuthRuntime enter. Update D0.18 to require the pre-existing lock and define initial dirty0/clean0 as O4, with the initial-only auto_create=True exception. Add O2 make_provider extraction to the existing provider owner and O2 initial composition/entry to runtime/supervisor. Add O6 mandatory operator-config CLI operand and provision-initial subcommand to009 EXECUTION scope only after independent review. Preserve normal FileTree auto_create=False and unchanged marker/control schemas. Add O1 config type to the existing config owner; no new module.

Reason for amendment:009 currently requires a lock before secret-store access in D0.18 yet resolves secrets before AuthRuntime enter in D0.14, and its storage constructor receives already-resolved secrets. A real resolver makes that ordering conflict material. Passing an unresolved callable to the same lock owner closes it without a second lock owner. The initial-only mode is absent from009; it cannot be inferred as implementation latitude. Changes to strict <500-source-line budgets or source ownership require root disposition, not splitting into an unapproved utility file.

Until root adopts and re-reviews these exact amendments,010 cannot be built against009. If009 D changes interfaces meanwhile, report the dependency diff and re-review010 under the same group/attempt history.

## 1. Atomic obligation and acceptance registry

Each row is one requirement with its reason and test oracle. All are [FIXED] within this proposal. AC commands and protected fixture ownership are in the execution draft; AC sentences are copied there verbatim.

| ID | Requirement | Reason | AC |
|---|---|---|---|
| R010-01 | Work MUST remain within the two-document author scope. | Root retains source/activation authority while008/009 work proceeds. | AC010-01 |
| R010-02 | Future integration MUST wait for accepted dependency interfaces and exclusive owner transfer. | Concurrent edits could invalidate root's review evidence. | AC010-01 |
| R010-03 | Operator ingress MUST validate O1/O6 before runtime effects. | Forest needs refusal rather than accidental default deployment. | AC010-02 |
| R010-04 | Secret resolution MUST occur only in a spawned child under the AuthStorage lock. | Parent/process transports would expose operator secrets and bypass exclusion. | AC010-03 |
| R010-05 | Resolver I/O MUST enforce O1's bounded private-file predicates. | Symlink/coercion/unbounded reads could disclose unrelated files to the service. | AC010-03 |
| R010-06 | Diagnostics and transports MUST exclude resolved secret material. | Forest cannot retract credentials from logs or task history. | AC010-03 |
| R010-07 | Initial provisioning MUST refuse every root outside O4's lock-only predicate. | Maintained register_client overwrites; existing identity would otherwise be replaced. | AC010-04 |
| R010-08 | AuthStorage MUST remain the sole lock/marker/barrier owner. | A second writer could certify incomplete provider state. | AC010-05 |
| R010-09 | Initial layout creation MUST use the six maintained setup_collection calls. | Missing collections would make later auto_create=False operations fail. | AC010-05 |
| R010-10 | Client provisioning MUST use the single maintained register_client path. | Handwritten credentials could drift from maintained serializer/profile semantics. | AC010-05 |
| R010-11 | Initial sealing MUST enforce O2's persisted predicate and O4 ordering. | Forest needs a qualified generation0 image before ordinary startup. | AC010-06 |
| R010-12 | Failure MUST preserve O4's refused/uncertain state without automatic recovery. | Reattempting registration would erase evidence of partial mutation. | AC010-06 |
| R010-13 | Ordinary startup MUST enforce O5 without repair or registration. | An apparently successful reboot must not silently change identity/storage. | AC010-07 |
| R010-14 | Live lifetime ownership MUST remain with the single009 RemoteSupervisor. | A second broker/retry owner could duplicate jobs or lose close proof. | AC010-08 |
| R010-15 | Service-manager configuration MUST enforce O5's manual-only profile. | Automatic relaunch after auth failure bypasses operator review. | AC010-08 |
| R010-16 | Release preparation MUST preserve the ordinary installation, training and private route. | Forest's existing workflows must not be displaced by this connector. | AC010-09 |
| R010-17 | Qualification MUST measure O7 costs through actual candidate owners. | Mock timings cannot establish candidate ordering or resource behavior. | AC010-10 |
| R010-18 | Activation MUST wait for every pending release choice and real-client gate. | Local synthetic success does not prove Cowork usability or permission approval. | AC010-11 |

| AC | Exact acceptance sentence |
|---|---|
| AC010-01 | The reviewed diff contains only authorized files and preserves dependency hashes, ownership gates and attempt history. |
| AC010-02 | Invalid or mismatched operator configuration refuses before secret reads, state writes, runtime entry or network activity. |
| AC010-03 | Spawn traces show one child-only resolution after lock acquisition and zero secret sentinels in parent memory captures, argv, environment, IPC, diagnostics or task files. |
| AC010-04 | Every noninitial, corrupt or uncertain root is refused with unchanged bytes and zero registration calls. |
| AC010-05 | Owner traces show one storage lock/marker owner, six maintained collection setups, one maintained registration and no replacement serializer or store. |
| AC010-06 | Fault traces prove the initial seal barriers and preserve every partial state without retry or a false successful-close report. |
| AC010-07 | Qualified restart retains exact identity/config/dependency binding and performs zero registration or repair. |
| AC010-08 | One supervisor controls admission and close, while every auth/close failure suppresses automatic relaunch. |
| AC010-09 | Installed-package validation preserves ordinary MCP, training and the existing private route without activating the candidate endpoint. |
| AC010-10 | Actual candidate measurements report secret-read, storage, registration, barrier and launch overhead separately with raw counters and uncertainty. |
| AC010-11 | Root's release record distinguishes every answered choice and actual provider/Claude/file/native gate from synthetic local evidence. |

## 2. Lifecycle views

```mermaid
flowchart LR
  E[Immutable release evidence and nonsecret config] --> P[Parent validates and spawns once]
  P --> C[Child acquires AuthStorage lock]
  K[Canonical operator secret files] --> C
  C --> A[AuthStorage resolves and validates]
  A --> M[Canonical encrypted marker]
  A --> G[Maintained provider registers client offline]
  G --> T[Canonical six maintained collections]
  T --> A
  A --> V[Human view of fixed status]
  classDef canonical fill:#f9d5d3
  classDef evidence fill:#d3e5fa
  classDef view fill:#eeeeee
  class K,M,T canonical
  class E evidence
  class V view
```

```mermaid
stateDiagram-v2
  [*] --> preflight
  preflight --> locked: validated existing root and lock
  preflight --> refused: no effects
  locked --> resolved: exact empty predicate and child reads
  locked --> refused: preserve bytes
  resolved --> dirty_initial: initial marker barrier succeeds
  resolved --> refused: failure before first mutation
  resolved --> uncertain: marker write or barrier uncertainty
  dirty_initial --> registered: maintained layout and registration
  dirty_initial --> uncertain: partial failure
  registered --> sealed: settlement and all seal barriers
  registered --> uncertain: failed close or barrier
  refused --> [*]
  uncertain --> [*]: preserve for operator review
  sealed --> [*]: release lock and exit success
```

```mermaid
sequenceDiagram
  participant P as Parent validates public operands
  participant C as Spawned child composes auth
  participant S as AuthStorage owns lock and barriers
  participant L as Maintained provider owns serialization
  P->>C: Nonsecret config and bound top-level callable
  C->>S: Enter initial purpose
  S->>S: Lock then resolve then dirty0 barrier
  S->>L: Six maintained collection setups
  C->>L: Await register_client then readback
  C->>S: Settled finish after client close
  S->>S: Validate and flush files/directories then clean0 barrier
  S-->>C: Release only after proven settlement
  C-->>P: Exit status only
```

## 3. Open decisions returned to root

| ID | Pending decision/gate | Blocks |
|---|---|---|
| O010-01 [ESCALATE] | Accept/review009 amendments, accepted008 correction,009 D/A/B and exclusive source ownership. No amendment is applied here. | Dispatch/integration |
| O010-02 [ESCALATE] | Forest's GitHub app/identity and downstream client choice. Proposed4list233 / numeric subject237812476 with no repo scopes is unanswered, not a default. | Live configuration/provisioning |
| O010-03 [ESCALATE] | Forest's file/task/total limits and retention. Decimal100MB/file,1GB/task,10GB total and explicit-delete versus7-day retention are proposals only. | Live core configuration |
| O010-04 [ESCALATE] | Private-file resolver approval, separately provisioned independent keys/root/lock, upstream_secret_bytes, ACL/coredump/backup custody and service account. Keychain alternative needs a new qualified proposal. | Secret access/provisioning |
| O010-05 [ESCALATE] | Exact immutable release/interpreter/roots, optional launchd-manual versus foreground, label, loopback port and measured server/ingress budgets. | Launch |
| O010-06 [ESCALATE] | Qualified target filesystem barriers, failure ordering, clean restart and separate operator-reviewed recovery/rotation/restore/reset/deletion policy. Initial flow does not grant those operations. | Provisioning/release; recovery remains excluded |
| O010-07 [ESCALATE] | Actual provider finite access/refresh lifetimes without fabricated TTL and actual Claude none/S256/pre-provisioned-public-client compatibility. No DCR fallback. | Public activation |
| O010-08 [ESCALATE] | Actual Cowork binary upload/download/hash materialization and native all-tool confinement, including file/read/edit/document runtimes. Synthetic shell proof is insufficient. | Completion claim |
| O010-09 [ESCALATE] | Explicit final activation approval after root presents reviewed local results and exact networking/service changes preserving private443. | Any live app, listener, route or service changes |

Air Claude Cowork custom connector URL+OAuth remains the selected experience. Studio native Codex subscription remains the worker backend; S1 is optional. No SSH/companion substitution, API-key fallback, or automatic file/connector/history inheritance follows from this proposal.
