# MCP worker baseline QA

Owner: independent MCP QA/exam agent `/root/mcp_qa`.
Recorded: 2026-10-15 America/Toronto (raw runner timestamps are UTC).
Candidate baseline: `plan/subagent-mcp` at `39df4854de213313b929caa4c8230c57e65457d7`.
The repository was clean before collection and test execution.

**Full accumulated baseline: FAIL — 217 passed, 4 failed, 0 errors, 0 skipped unique cases across 221 cases.**
This is a baseline record, not acceptance of the new MCP worker service.
Existing failures are not charged to the new builder; they still prevent a full-suite PASS.

## Inventory and accounting

Inventory used `rg --files -uu` across the whole standalone repository, followed by an AST scan of
all Python files for `test_*` functions. All 91 tracked files were hashed before new work.
No maintained tests were found outside `tests/`: `cortados_router/`, `s1/`, `integrations/`,
`tables/` and `bin/` have no package-local test modules. There was no `acceptance/`, CI workflow,
`pyproject.toml`, `package.json`, pytest configuration or test-registration manifest at the baseline.
S1's maintained test coverage is included in the top-level context, nightly-run and overnight modules;
no model-quality evaluation was invoked and no model weights were loaded.

| Lane | Modules | Unique cases | Pass | Fail | Error | Skip |
|---|---:|---:|---:|---:|---:|---:|
| Standalone unittest modules | 11 | 147 | 147 | 0 | 0 | 0 |
| Hermes/CortadOS integration modules | 7 | 74 | 70 | 4 | 0 | 0 |
| Combined, deduplicated | 18 | 221 | 217 | 4 | 0 | 0 |

The README unittest discovery command reports 155: 148 pass, 1 fail and 6 skipped module-import
placeholders. It executes the 147 standalone cases plus the two CortadOS review cases and emits one
skip for each unavailable pytest/Hermes module. Those placeholders are not six real integration
cases. All seven integration modules were subsequently executed individually in the existing isolated
Hermes environment, producing the 74 real integration cases above. The two review cases are counted
once in the combined total. No expected failures, unexpected passes, retries or timed-out cases occurred.

| Integration module | Pass | Fail | Error | Skip |
|---|---:|---:|---:|---:|
| `test_auxiliary_model_routing.py` | 30 | 1 | 0 | 0 |
| `test_cron_dispatch_routing.py` | 1 | 1 | 0 | 0 |
| `test_delegation_model_routing.py` | 16 | 1 | 0 | 0 |
| `test_required_model_routing.py` | 15 | 0 | 0 | 0 |
| `test_required_routing_full_loop.py` | 5 | 0 | 0 | 0 |
| `test_router_gateway_admission.py` | 2 | 0 | 0 | 0 |
| `test_router_reviews_cortados.py` | 1 | 1 | 0 | 0 |

## Existing failures

1. `test_router_reviews_cortados.CortadOSReviewTests.test_profile_instructions_are_opt_in_and_dynamic`:
   the shared CortadOS `faces.json` still contains the global `before it is called done` review rule.
   This is the deployment-pending failure already named by the standalone README. Owner: CortadOS Router/fleet.
2. `test_cron_dispatch_routing.test_each_fire_routes_with_current_capacity_and_profile_without_repinning`:
   the exported integration test imports `bin/route-crons.py` from this standalone repository, where it
   does not exist. The routing assertions run first and pass; the later outcome-sampler import fails.
   Owner: standalone export/integration contract. No donor file was copied in to conceal the missing seam.
3. `test_auxiliary_model_routing.test_installer_dry_run_idempotence_and_drift`:
   installer check returns one pending file instead of the expected three because the available Hermes
   source snapshot already contains two of the patches. Owner: integration fixture/environment.
4. `test_delegation_model_routing.test_installer_check_idempotence_and_existing_generic_hook`:
   installer check returns one pending file instead of the expected four because the available Hermes
   source snapshot already contains three of the patches. Owner: integration fixture/environment.

The last two are setup compatibility failures, not evidence that the new service regressed routing.
They remain failures in the raw reports. No test was changed, disabled or relabelled.

## Environments and exact commands

Standalone: `/opt/homebrew/opt/python@3.14/bin/python3.14`, Python 3.14.6; Node v26.5.0 available
for fake vendor CLI process cleanup. CWD is this repository root.

```sh
python3 -B -m unittest discover -v -s tests -t .
```

Environment: `PYTHONDONTWRITEBYTECODE=1`, fresh temporary `CORTADOS_HOME`, `CORTADOS_HERMES_HOME`
and `HERMES_HOME`; `HERMES_IGNORE_USER_CONFIG=1`, `HERMES_TELEMETRY_ENABLED=false`,
`CORTADOS_LIVE_WRITE=0`, `WANGCAI_LIVE_WRITE=0`. Exact scratch paths and exit 1 are recorded in
`acceptance/mcp_worker/evidence/baseline/unittest-baseline.json`. Test framework duration 8.041 s;
parent command wall time 14.764 s. The process/PTY tests ran with sandbox escalation for `ps`/PTY access.

Integration: existing `/Users/velox/.cortados-builds/target-test-env/venv/bin/python`, Python 3.12.13,
pytest 8.4.2, pytest-asyncio 1.4.0, SQLite 3.53.1, macOS 27.0.1 arm64.
For each of the seven modules, in a fresh process and fresh temporary runtime directories:

```sh
/Users/velox/.cortados-builds/target-test-env/venv/bin/python -B -m pytest -q \
  /Users/velox/.cortados-builds/cortados-router-repo/tests/integration/<module>.py \
  -p no:cacheprovider \
  --junitxml=/private/tmp/cortados-router-mcp-qa-baseline/<module>.xml
```

Every fully expanded command is preserved in `integration-baseline.json`.
The process environment retains only host `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, then sets:

```text
PYTHONDONTWRITEBYTECODE=1
PYTHONPATH=/Users/velox/.cortados-builds/cortados-router-repo:/Users/velox/.cortados-builds/target-test-env/hermes-agent
HERMES_TEST_ROOT=/Users/velox/.cortados-builds/target-test-env/hermes-agent
CORTADOS_REPO=/Users/velox/cortados
CORTADOS_HOME=<fresh scratch>/state
CORTADOS_HERMES_HOME=<fresh scratch>/hermes
HERMES_HOME=<fresh scratch>/hermes
HERMES_IGNORE_USER_CONFIG=1
HERMES_TELEMETRY_ENABLED=false
CORTADOS_LIVE_WRITE=0
WANGCAI_LIVE_WRITE=0
PYTHONHASHSEED=0
TZ=UTC
LANG=C.UTF-8
```

The integration source is an existing isolated Hermes copy based on `9796235822b89e08597a402dad045b5b4464e474`
plus captured local edits. `external-files.json` hashes all 2,020 Python source/external dependency files
used to identify that snapshot, the CortadOS `faces.json`/PTY files, and the three hardcoded host source
files read by `test_required_model_routing`. The latter module does not use `HERMES_TEST_ROOT` for those
three reads; its source defaults to `~/.cortados/hermes/hermes-agent`. It copies those files into test
temporary directories before installer mutation. No existing host source or runtime configuration was edited.

## Evidence and next gate

All raw logs, JUnit reports, commands, environment information, discovery results and source manifests
are retained in `acceptance/mcp_worker/evidence/baseline/`; `SHA256SUMS` fixes their exact bytes.
`inventory.json` lists all 18 discovered modules with baseline hashes and pre-parametrization counts.
`repository-files.json` hashes all 91 baseline tracked files (sha256
`8c50cedf75a415077763dd16dd2234f8e802d5353e0041fed0270218ddad9e71`).
`external-files.json` sha256 is `68794b033edc3c3639f333d8546dc561718fca5dbf84944f1a5445f16a5e2314`.

Candidate verification must rerun all 18 modules plus the independently frozen MCP acceptance exam.
Every baseline failure must remain visible separately. A source/test removal, new skip or count reduction
requires investigation rather than a narrower discovery command. Provider-format readiness additionally
requires the per-model/per-output-kind real captures required by D-0092. This offline baseline supplies
neither model-quality evidence nor permission to deploy or alter live configuration.
