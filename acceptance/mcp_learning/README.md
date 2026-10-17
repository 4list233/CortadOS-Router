# Independent MCP learning exam

Owner: `/root/mcp_qa_followup`, Forest's designated independent MCP exam owner.
Contract author: `/root/mcp_learning_contract`.
Contract: `docs/mcp-worker/LEARNING-CONTEXT.md`, RT-MCP-LEARNING-003 v1.
Production baseline: `6a64b77853ca990a5e641918098c55e0976858f3`.
The builder has read-only access to this directory and all older protected exams.

31 cases: seven public context/projection checks, six public feedback/storage checks,
seven collection/partition checks, six actual teacher/SFT/evaluation entrypoint checks,
two independently checked ground-truth captures, and three staged activation checks.
`test_activation.py` deliberately fails with BLOCKED messages until the separately
delegated activation builder supplies guarded staged sources and the pinned installed
release. This stage is not an empty or skipped green lane.

Run from repository root:

```sh
CORTADOS_MCP_ACTIVATION_ROOT=/Users/velox/.cortados-builds/mcp-learning-activation /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/mcp_learning -t . -v
shasum -a 256 -c acceptance/mcp_learning/SHA256SUMS
shasum -a 256 -c acceptance/mcp_worker/SHA256SUMS
```

The complete prebuild run contains 31 cases: four pass, eight fail and nineteen
error, zero skip. Three failures are the unimplemented second-stage activation
package. Missing tools/owners and unimplemented training changes explain the other
red results. The official SDK wraps assertions made inside its session in exception
groups, which unittest reports as errors. These are prebuild design evidence, not
failed builder attempts. The four passing cases are legacy canonical/replay behavior,
exact existing evaluation input, native tokenizer capture integrity and worked-example
integrity. `evidence/prebuild.log` preserves the raw report.

`evidence/draft-prebuild.log` preserves the initial 25-case author run. One exam
defect was corrected before freezing: SQLite jobs.arguments stores canonical bytes
without request_id, while record.arguments retains the complete normalized request.
The final exam now checks both representations and their independent hash. The later
SFT assertion compares parsed message content rather than escaped JSON text; its
purpose and expected context preservation did not change. No production code was
edited to obtain these fixture self-checks.

The only original protected change is AC01's exact catalog assertion and its truthful
method name: exact five becomes exact seven because this contract adds two requested
tools. The original bytes remain in `fixtures/original-protocol.py.txt`, SHA-256
`ebe1f95b9652033035ea23c4e88019056854d1fe5f62e0ddff08b5c3e5c9f437`.
Every other assertion in that file remains identical. The original corpus manifest
changes only its test_protocol.py entry. There is no compatibility flag to hide tools.

Observed integration evidence is under `fixtures/source-shapes/`: live legacy jobs,
routes, request rows and teacher label rows. The capture script uses read-only SQLite
transactions and replaces all free-text strings; keys, types, arrays and nulls remain
unchanged. These samples prove source layouts, not private task meaning. The tokenizer
captures use the actual installed MLX ChatDataset and TokenizerWrapper with synthetic
student messages and answers. Full sequences contain 1,536 and 1,537 tokens, including
69 supervised answer tokens and the template's BOS/EOS behavior. Neither weights nor
mlx.core were loaded for those captures.

The pipeline boundary doubles never invoke Claude, read account secrets or load model
weights. Actual candidate entrypoints are imported with private DATA and captured
teacher/tokenizer/scorer calls. Enabled activation runs remove cached Router modules
and bind the pinned installed site-packages before loading dependencies; the resolved
learning_data.__file__ must match that release. This prevents a checkout import from
masquerading as installed-release proof. Root separately records the old Bash inode
and inherited environment experiment, live hash checks, read-only real-source smoke,
installed wheel identity, and actual post-activation process identities.

Independent D/A review is zero unresolved defects for dispatch, subject to the hashes
in this committed package. Review closed: legacy optional-default identity drift;
unknown versus empty context; assembled Unicode/UTF-8 limits; feedback ownership and
transactionality; late feedback revision identity; immutable teacher provenance;
future-outcome leakage; group partition conflicts; stale same-count evaluation bars;
real source location/WAL visibility; atomic requests-before-projection publication;
one installed split owner; exact next-run activation branches; and rejected optimization
evidence. Four Mermaid diagrams render successfully with retained commands/results.

This D/A result permits implementation only. B remains pending production candidate,
full accumulated suite, performance/query evidence, installed-wheel checks and staged
activation. Root's inventory must retain the original 331 cases, add these 31 and all
builder regressions, and run the actual shared 7 nightly/9 overnight cases. The full
shared CortadOS suite is outside this standalone package and is not claimed green.
