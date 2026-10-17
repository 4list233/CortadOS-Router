# Independent native profile exam

Owner: `/root/mcp_qa_followup`, continuing the user-designated MCP QA role.
Contract: `docs/mcp-worker/NATIVE-PROFILE-002.md` (RT-MCP-NATIVE-002 v1).
Builder access is read-only. Original `acceptance/mcp_worker` bytes remain unchanged.

Nine cases: eight public-MCP version/provenance checks plus one independently
inspected captured-byte ground-truth check. Prebuild against `d40803f`: 3 pass,
1 failure and 5 errors, zero skipped. The old profile, legacy schema-1 record,
unknown refusal and real-capture ground truth behave as recorded; new-version
refusals and missing pre-exec version drift detection produce the expected red.
The old/new success matrix is one unittest with two subcases; one red subcase
makes that case fail. The unchanged official SDK transport wraps some assertion
failures in exception groups, hence unittest labels those five cases errors.

`profile_codex.py` is a synthetic version-observation wrapper. It delegates every
non-version operation to the original protected native process double; it does
not pretend that its replays establish new native compatibility. Real installed
0.163.0 compatibility is evidenced separately under `fixtures/0.163.0` with the
capture scripts, metadata redaction provenance, original observer defects and
corrections, and twelve real per-model captures. The three models each have
command/final/usage and folder/network denial, grant-proxy, handoff boundary and
cancellation evidence. The actual internal reasoning trace is neither exposed
nor part of this boundary contract.

The corpus retains two corrected observer mistakes without changing native
output: an incorrect expected Luna handoff marker and an incorrect expectation
that an empty MCP resource catalog should instead raise. Source hash provenance
also retains the briefly changed on-disk capture script variant. These are not
failed production builder attempts or silently relabeled provider errors.

Run from repository root:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/native_profile -t . -v
shasum -a 256 -c acceptance/native_profile/SHA256SUMS
```

The native version-probe addition must also pass the original benchmark and
full accumulated suite before final acceptance. No real account credential is
stored in this exam. Captured config/account string values are redacted with
byte-shape provenance; scratch prompts contain synthetic markers only.
