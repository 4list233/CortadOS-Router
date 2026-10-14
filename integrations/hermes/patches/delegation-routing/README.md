# Native delegation routing

Prepared on 2026-10-07. This patch has been checked against the installed Hermes
source and tested in temporary checkouts. It has **not been deployed**.

`delegation.model_router` selects the required plugin. When the key is absent,
the adapter reads `agent.model_router`. An explicit null/empty delegation value
keeps the upstream behavior. Router owns fleet configuration and the shared
`route_agent_model` selector.

The patch normalizes and validates every task, asks the router for each route,
and resolves all selected credentials before creating transcripts or children.
A missing, failed, duplicate or incomplete decision blocks the batch. Old
delegation endpoint, key, ACP command, request overrides and fallback chains
are excluded from the new credential configuration. Each child receives its
selected runtime and a credential-free route receipt; mixed batches have no
single model in their call-wide metadata.

The host calls `route_agent_model` with `entrypoint='native-delegation'`, the
child goal/context, parent session scope, `platform='subagent'`, parent provider
and model, task index/count, and trusted constraints. It uses a unique
`api_request_id` and an empty `turn_id`: a child must not overwrite its parent's
pending route. The parent turn remains in `task_context.parent_turn_id`.

`start_review` explicitly supplies constraints with `kind='review'`,
`owner='auxiliary.review'`, and its configured provider/model. These restrict
the shared selector; the review still routes. Other internal callers can pass
verified `allowed_routes` constraints, including a diagnostic target. Ordinary
tool arguments and prompt phrases cannot provide these constraints or bypass
selection. Constraints and receipts survive on
`child._cortados_route_constraints` and `child._cortados_route_receipt` for the
generic request gate. This patch does not itself change models during a loop.

## Check and apply

The installer requires an explicit checkout. `--check` validates source anchors
and Python syntax without writing:

```sh
python integrations/hermes/patches/delegation-routing.py --hermes-root /path/to/hermes --check
python integrations/hermes/patches/delegation-routing.py --hermes-root /path/to/hermes
```

It updates `tools/delegate_tool.py`, `agent/review_engine.py`, and
`hermes_cli/plugins.py`, then installs `tools/delegate_model_routing.py`.
Reapplying is idempotent. It accepts a hook already registered by the generic
request installer. Changed source anchors abort before the first write.
It performs no configuration changes or restarts.

## Verification

The 17 tests in `tests/integration/test_delegation_model_routing.py` stage the current host
sources under pytest's temporary directory. They execute the patched host
functions with real task normalization, runtime helpers and hook dispatch;
constructors, transcripts, credentials and execution are stubbed. A separate
case exercises the real credential resolver against a scoped fake backend.
There are no model calls or production plugin discovery.

The installed Hermes venv lacks pytest. The verified invocation uses an
ephemeral uv environment and the existing host dependencies:

```sh
PYTHONPATH=$HERMES_ROOT:$HERMES_ROOT/venv/lib/python3.12/site-packages \
PYTHONDONTWRITEBYTECODE=1 \
uv run --no-project --with pytest \
  --python $HERMES_ROOT/venv/bin/python \
  python -B -m pytest -q tests/integration/test_delegation_model_routing.py -p no:cacheprovider
```

## Remaining host entry points

These findings describe the installed Hermes checkout at inspection time.
Router owns the broader request gate in `integrations/hermes/patches/required-routing.py`.

| Entry | Host path | Remaining consideration |
| --- | --- | --- |
| CLI startup | `hermes_cli/cli_agent_setup_mixin.py:538` | Constructs `AIAgent` before a turn. A stale initial provider can fail credential resolution before a request gate runs. |
| Interactive CLI | `hermes_cli/cli_chat_turn_mixin.py:322` | Calls the existing agent's `run_conversation`; needs the shared request gate. |
| CLI query/quiet | `cli.py:4065`, `cli.py:4140`, `cli.py:4157` | Alternate `run_conversation` entry paths also need the shared gate. |
| Kanban worker | `hermes_cli/kanban_db_dispatch.py:2398` | `_worker_argv` launches `--cli --accept-hooks`, optional `-m`/`--provider`, then `chat -q 'work kanban task <id>'`; inherits both CLI issues. |
| In-place switch | `agent/agent_runtime_helpers.py:2148` | `switch_model` rebuilds runtime/client/compressor and billing state. `_finish_switch` preserves unrelated fallback entries, so the request gate must clear static fallback policy explicitly. |

Preconstruction routing knows the goal/context, not the complete assembled
system prompt, image payload and tool schemas. The generic request gate must
validate the final request fit and enforce the retained review constraints.
These tests establish the native host boundary, not live fleet coverage.
