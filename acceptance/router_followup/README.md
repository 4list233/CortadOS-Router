# Independent baseline repair exam

Owner: `/root/mcp_qa_followup`, continuing the user-designated MCP QA role.
Contract: `docs/mcp-worker/FOLLOWUP.md` (RT-MCP-FOLLOWUP-001 v1).
Builders may read but must not edit this directory.

The six new cases complement all unchanged original tests. Prebuild: 2 pass,
2 failures, 2 errors, zero skips. Missing sampler and stale roster are the expected
red cases. The two original installer modules independently pass 31 + 17 cases
against the frozen pristine source inputs; their assertions are unchanged.

`hermes_pristine/` contains four upstream source files, not a runnable host.
Set `HERMES_TEST_ROOT` to this directory only for the auxiliary and delegation
integration modules. Runtime `PYTHONPATH` stays pointed at the isolated complete
Hermes host; the full-loop lane also uses that complete host as its input root.
`HERMES-SOURCE.json` pins Git revision, blob identities, SHA-256 and byte counts.
`HERMES-LICENSE.txt` preserves the source license. These large captured files are
fixtures; do not refactor them to satisfy newly authored source limits.

`review-policy.json` contains only the seven authorized replacement roster fields,
not a snapshot of private account identifiers or runtime configuration. The complete
before-image for the shared roster stays private outside this repository. Root must
independently confirm that no other JSON field changed.

Run from repository root:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/router_followup -t . -v
shasum -a 256 -c acceptance/router_followup/SHA256SUMS
```

The source fixture fixes are setup repairs, not evidence of new provider capability.
Native version extension and Claude installation require their separate gates.
