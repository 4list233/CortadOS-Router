<!-- PURPOSE: Record independent candidate verification and the exact acceptance boundary.
     CONTRACT: RT-MCP-001 / RT-MCP-BUILD-001; baseline failures cannot become a full-suite pass.
     DEPENDS: Pinned candidate, independently owned exam, retained command and native evidence. -->
# MCP worker build verification

**Implementation verified; overall acceptance remains FAIL solely because of four documented
pre-existing integration failures.** No new implementation failure, error or skip was found. No live
Claude configuration, CortadOS runtime, installed plugin, login, launchd job or model weights were
changed. Nothing was pushed. This report is not deployment approval.

Source candidate: `c0588b727270bd63e0af94ecd5c6bd761fa348dc`, branch `plan/subagent-mcp`, isolated
checkout `/Users/velox/.cortados-builds/cortados-router-repo`. The checkout was clean when the independent
run began. Temporary wheel build directories were removed afterward; the wheel remains in scratch.
This report and evidence are a subsequent documentation-only commit, with identical production bytes.

## Delivered behavior

Claude supplies a task and chosen context, then reviews the result. The service exposes five stdio MCP
tools and uses the native Codex harness with its saved ChatGPT subscription. Exact-model selection
needs no S1. Optional S1 scores the brief and feeds the existing capability/scarcity resolver. Both
modes require current included-usage observations. No API fallback or other provider runner is shipped.

The broker owns transactional identity, workspace/capacity reservations and immutable terminal jobs.
A separate watchdog starts only after the durable START handshake and contains observed process
identities on cancellation or broker loss. Per-job proxies expose only explicitly granted MCP tools,
including stdio and authenticated HTTP upstreams. Connectors do not inherit Claude's in-memory sessions.

Existing core/scarcity owners are reused. The only existing production edit is core's optional resource
seam, net one line; old callers retain their default paths. All 19 new source/config/test files have
PURPOSE/CONTRACT/DEPENDS headers, parse, and stay below the 500-line soft limit (largest: 336 lines).
The exact 20-file builder scope is hashed in the retained source manifest. No existing test was changed.

## Independent verification

| Lane | Cases | Pass | Fail | Error | Skip |
|---|---:|---:|---:|---:|---:|
| Standalone: 147 existing + 34 new component regressions | 181 | 181 | 0 | 0 | 0 |
| Seven Hermes/CortadOS integration modules | 74 | 70 | 4 | 0 | 0 |
| Protected MCP black-box acceptance | 53 | 53 | 0 | 0 | 0 |
| **Maintained unique total** | **308** | **304** | **4** | **0** | **0** |

All **87 new worker cases pass**. All maintained suites were inventoried, including the package and
integration locations. There are no missing/skipped suites. The benchmark, installed wheel and six
real-native checks are additional evidence, not extra unit-test counts. The independent protected run
took 70.202 seconds; standalone framework time was 9.957 seconds.

The four failures exactly reproduce [the prebuild baseline](QA-BASELINE.md): shared `faces.json`
review-rule deployment; missing standalone `bin/route-crons.py`; auxiliary installer fixture already
patched; delegation installer fixture already patched. They remain failures. No donor code, test
weakening or live-state change was used to conceal them.

Exact root orchestration command (expanded per-suite argv and environment are in `suites/report.json`):

```sh
/private/tmp/cortados-mcp-sdk-env/bin/python -B /private/tmp/mcp_root_full_suite.py /private/tmp/mcp-root-verification-c0588b7
```

Protected command:

```sh
/private/tmp/cortados-mcp-sdk-env/bin/python -B -m unittest discover -s acceptance/mcp_worker -t . -v
/private/tmp/cortados-mcp-sdk-env/bin/python -B acceptance/mcp_worker/benchmark.py --output /private/tmp/mcp-root-verification-c0588b7/performance.json
```

Source tests use isolated Python 3.14.6/MCP 1.30.0. Integration tests use the pre-existing Python 3.12.13
Hermes environment described in the baseline. Each lane gets fresh non-live state. The runner retains
nonzero lane exits; its orchestration process completing does not mean the accumulated suite passed.

## Installed package and real subscription checks

Built using the declared `setuptools==80.9.0` backend, installed into a fresh temporary environment,
then run with a working directory outside the source checkout and without source PYTHONPATH:

```sh
/Users/velox/.local/bin/uv build --wheel --cache-dir /private/tmp/cortados-mcp-uv-cache --out-dir /private/tmp/cortados-mcp-wheel-c0588b7 --python /opt/homebrew/opt/python@3.14/bin/python3.14
/Users/velox/.local/bin/uv pip install --python /private/tmp/cortados-mcp-installed-check/bin/python --cache-dir /private/tmp/cortados-mcp-uv-cache /private/tmp/cortados-mcp-wheel-c0588b7/cortados_router-1.0.0-py3-none-any.whl
/private/tmp/cortados-mcp-sdk-env/bin/python -B /private/tmp/mcp_root_native_check.py --installed --python /private/tmp/cortados-mcp-installed-check/bin/python --directory /private/tmp/mcp-root-native-c0588b7
```

Wheel SHA-256: `f7b47a28948a06a9a8a83c4464682a2cad8b7453f2266acc9aabdb09d81a933f`.
All 46 shipped source/data entries match checkout bytes; all five routing bands load from installed
tables. Runtime dependency is exactly `mcp==1.30.0`. The packaged service ran native CLI 0.162.1 on macOS.

| Real installed-service probe | Observed outcome | End-to-end seconds |
|---|---|---:|
| Explicit `gpt-6-luna` | Native command wrote the exact marker file; accepted final marker | 6.939 |
| Explicit `gpt-6.1-sol` | Same file and terminal-evidence check | 6.535 |
| Explicit `gpt-6-astra` | Same file and terminal-evidence check | 9.853 |
| S1 | Real scorer returned R1/D=2; resolver chose `gpt-6.1-sol`; exact marker file and final | 8.510 |
| Granted MCP + filesystem boundary | `visible_marker` reached upstream once; inside write succeeded; outside write denied and absent | 11.787 |
| Cancellation | Real native sleep child observed, job became cancelled, child gone, summary null | See timestamps in events |

Every route reports fresh native quota observations. Explicit routes have null S1 difficulty/band.
Native responses do not report the actual model field, so `reported_model` remains null; the requested
CLI model is recorded separately. No measured cost saving or task-quality guarantee is inferred.
The temporary broker was stopped and its test processes were absent after verification.

Real-probe settings retain the acceptance profile's 2-job capacity, 65,536-byte handoff, 1,048,576-byte
output and 20 ms process sampling. Timeout maximum was 90 s to include a deliberately sleeping child;
startup was 20 s for real native metadata/connector setup; termination grace was 5 s for orderly native
tool interruption; metadata freshness was 30 s to avoid redundant account reads across the smoke run.
Lower bounds could interrupt legitimate setup; higher values delay stall/termination detection or permit
older quota data. These are measured test settings, not automatic production defaults.

## Efficiency

Four workloads use the protected reference plus public MCP polling. Every polling read returned identical
terminal evidence and started no metadata or native work. The measured regression threshold is
420.958375 ms p99, derived in the protected exam; it is not a service-level promise.

| Workload | Direct native-double reference median ms | Candidate end-to-end ms | Added orchestration ms | Public query p99 ms |
|---|---:|---:|---:|---:|
| Small | 75.789 | 507.387 | 431.599 | 1.608 |
| Typical | 67.304 | 409.080 | 341.776 | 1.668 |
| 65,536-byte boundary | 69.068 | 578.856 | 509.788 | 3.197 |
| Malformed terminal | 67.846 | 374.040 | 306.193 | 1.666 |

These synthetic times measure broker/admission/process/evidence overhead, not provider latency or model
quality. The adverse case returned a failed job, not a fast fabricated success. All raw samples remain.

## Gate and execution ledger

D/A were approved before production dispatch in `4c41a3f5b13185b70c3e5c097a093c8ae20fd1be`.
Independent QA repaired one exam observation in `1fc76cc29bb5f49f8d3e8b6701ae586a059fc06d`:
macOS framework Python injects a variable after launch, so the permission test now records the native
exec environment before Python starts. The allowlist is unchanged; injected-variable controls still
fail it. Original failing evidence and the reviewer-owned amendment are retained under acceptance/.
No production workaround or protected-test edit was made by a builder.

Parent contract SHA-256: `a7934cdb9a78e18dc18e70eaf8973052308cec696776e2b9c55ac88768b9a740`.
Protected manifest SHA-256: `a889f2a26aeaf983b27cf245d7ae1df4ddaade51b93ef613dfda663ae5e2f25b`;
all 167 protected files were rechecked unchanged.

| Rubric B area | Independent disposition |
|---|---|
| B1 required AC commands | Worker ACs, installation/native seam and performance satisfied; AC-12 accumulated suite remains FAIL due solely to baseline four. |
| B2 ground truth | Returned IDs, immutable results, files, observed denied write, process exit, SQLite backup/recovery, raw logs and hashes retained. |
| B3–B4 scope/contract protection | Exact 20 builder paths; root documentation separate; parent/execution bytes unchanged and protected exam owned by QA. |
| B5 regressions | No new regression. 217 legacy cases still pass; same four still fail. This is not a green full suite. |
| B6–B8 invariants | Source reviewed for transaction/unique index ownership, queued/START ordering, monotonic deadline, creation identity, strict receipt, closed grant proxy, structured events and actual stopped backup/restore. Named tests independently pass. |
| B9–B11 disclosure/structure | Limitations below; headers/source sizes checked. Standard-library SQLite/Unix sockets/libproc chosen for single-writer durability and captured-host process identity; SDK owns MCP wire behavior. |

Ledger: `RT-MCP-BUILD-001 v1 · /root/mcp_builder · source c0588b7 · root independent B · 0 new builder defects · overall acceptance FAIL · outcome blocked on existing QA-04 baseline failures`.
Those unchanged failures do not increment a failed-builder-attempt counter or imply a design error.
Overall Rubric B is **not PASS** while that required gate fails. The independent exam owner confirmed
this distinction. No second implementation attempt or deployment is claimed.

## Limits and handoff

This version is Codex-only and pinned to the captured native profile. API/local/Cursor/Claude worker
adapters, live Claude installation, shared in-memory connectors, automatic decomposition, automatic
retries/worktrees and retention pruning are outside this build. S1's scoring interface allows 6,000
Unicode characters; larger worker context travels separately. The native harness owns subsequent
repository/tool context. Its responses always require planner review.

Process sampling cannot prove containment of deliberately unobserved daemon escapes. Detected lost
ownership or invalid termination evidence retains the workspace claim. Policy/quota snapshots are not
atomic against external account/admin changes. No implicit grant, forced policy bypass or paid fallback
is used. The next owner can follow [USAGE.md](USAGE.md); production setup requires choosing workload
limits and explicitly configuring compatible connector endpoints.

Raw verification evidence is in [evidence/c0588b7](evidence/c0588b7/summary.json), with SHA256SUMS.
Private runtime SQLite/config remain in the named scratch directories; no account or real connector credentials are copied into
the repository. The connector proof uses a clearly labeled synthetic test value. The shared `/Users/velox/cortados` checkout was not edited by this implementation.
