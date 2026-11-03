# Terminal results

Source remained frozen throughout full-final-frozen; all18 hashes rechecked after it.
Interpreter: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python (-B); umask022; TMPDIR=/Users/velox/.cortados-builds/cortados-router-repo/.cowork-core-evidence/attempt-3/tmp.

Full command: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py /Users/velox/.cortados-builds/cortados-router-repo/.cowork-core-evidence/attempt-3/full-final-frozen

Core command: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py' -v

Full raw summary: {"tests": 896, "failures": 6, "errors": 246, "skipped": 0}. Lanes: 28; green lanes: 20. Overall pass: False.
Core:111 methods/3 failure events/89 error events/0skips. Event totals overlap methods. All28 lanes collected; no missing lane is treated as passing. Failed benchmarks have zero methods and remain failed; successful benchmark lanes are measurements, not unit-method counts.

| Lane | Methods | Failure events | Error events | Skips | Exit | Green |
|---|---:|---:|---:|---:|---:|---|
| standalone | 230 | 0 | 3 | 0 | 1 | False |
| test_auxiliary_model_routing | 31 | 0 | 0 | 0 | 0 | True |
| test_cron_dispatch_routing | 2 | 0 | 0 | 0 | 0 | True |
| test_delegation_model_routing | 17 | 0 | 0 | 0 | 0 | True |
| test_required_model_routing | 15 | 0 | 0 | 0 | 0 | True |
| test_required_routing_full_loop | 5 | 0 | 0 | 0 | 0 | True |
| test_router_gateway_admission | 2 | 0 | 0 | 0 | 0 | True |
| test_router_reviews_cortados | 2 | 0 | 0 | 0 | 0 | True |
| acceptance-cowork_mcp | 111 | 3 | 89 | 0 | 1 | False |
| acceptance-fresh_quota | 130 | 0 | 2 | 0 | 1 | False |
| acceptance-mcp_learning | 31 | 0 | 40 | 0 | 1 | False |
| acceptance-mcp_worker | 53 | 3 | 96 | 0 | 1 | False |
| acceptance-native_profile | 9 | 0 | 16 | 0 | 1 | False |
| acceptance-quota_evaluation | 21 | 0 | 0 | 0 | 0 | True |
| acceptance-quota_sources | 83 | 0 | 0 | 0 | 0 | True |
| acceptance-router_followup | 6 | 0 | 0 | 0 | 0 | True |
| acceptance-training_teacher | 61 | 0 | 0 | 0 | 0 | True |
| benchmark | 0 | 0 | 0 | 0 | 1 | False |
| shared-test_router_nightly_runs | 7 | 0 | 0 | 0 | 0 | True |
| shared-test_router_overnight | 9 | 0 | 0 | 0 | 0 | True |
| preserved-pty-delivery | 25 | 0 | 0 | 0 | 0 | True |
| preserved-pty-fd | 2 | 0 | 0 | 0 | 0 | True |
| shared-router-core | 30 | 0 | 0 | 0 | 0 | True |
| migrated-learning | 3 | 0 | 0 | 0 | 0 | True |
| installed-teacher-adapter | 7 | 0 | 0 | 0 | 0 | True |
| historical-007-parity | 4 | 0 | 0 | 0 | 0 | True |
| teacher-performance | 0 | 0 | 0 | 0 | 0 | True |
| core-performance | 0 | 0 | 0 | 0 | 1 | False |

The native sysctl boot-identity call and /bin/ps are denied by the builder sandbox. Ordinary MCP assertion failures report storage_error before intended preflight or a synthetic HTTP fixture failing to become ready; core crash/separate-process assertions follow blocked runtime startup. No protected assertions were weakened. The raw failures remain red pending host rerun.

The preliminary full-suite invocation had an additional training-teacher mode assertion; rerunning with the prescribed umask022 passes that lane. This does not erase the preliminary result. full-terminal predates the final IPC retained-lease change; full-final-frozen is the sole terminal-source accumulated run.

Transfer/performance benchmark-core cannot construct CoreRuntime, so matrix latency, memory scaling, SQL workload and stop-overshoot measurements remain BLOCKED. Historical baseline785 red and historical007 parity remain separately reported by the unchanged driver.

No cortados-workers tool was exposed; this direct builder did not invoke nested workers. No permission escalation or host-process workaround was attempted.

Terminal correction SHA256: 8042de88889a8f35e91f69687a08895ee02f253938f683036f38d62bdc14983f
Cumulative candidate SHA256: 4176f43a551f067834242aeb5361ed2f708e2304059cae07497f7530f744be2c
Manifest SHA256: 585527a2aaca7780d0ca9509b92e466e7d891147d9866d13be01809643fc8967
Protected inputs:3 contracts +641 prior files +18 core files verified unchanged; all18 source files compile and remain below500 physical lines.

Acceptance disposition: BLOCKED here. Root host rerun, complete measurements and independent B remain mandatory.
