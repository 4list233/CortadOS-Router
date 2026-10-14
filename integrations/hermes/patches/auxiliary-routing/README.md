# Required auxiliary model routing

Prepared on 2026-10-07. The installer has passed a read-only check against the
installed Hermes source; the tests run in temporary checkouts. This change has
not been installed into the runtime.

The patch covers public `agent.auxiliary_client.call_llm` and `async_call_llm`,
including sync streaming. It selects through `route_agent_model` at the start
of `_prepare_aux_request`, before legacy task credential resolution, client
auto-detection, or fallback. It retains the host's request shaping, image wire
conversion and result parsing.

`auxiliary.model_router` defaults to `agent.model_router` when absent. An explicit
empty/null auxiliary setting preserves the original host behavior. Each required
call selects a concrete provider/model with assembled messages/tools in
`input_fit`. The scoring projection preserves text and replaces image data with
size markers. The full original media remains in the execution payload.

The selected provider resolves credentials through Hermes and builds its cached
client directly. Old per-call/task endpoints, credentials and API modes cannot
override the new route. Missing clients or changed provider/model pairs fail;
the old auto-detection and fallback client chains are not entered.

## Constraints and admission

The host sends `entrypoint='auxiliary'`, `platform='auxiliary'`, an empty turn ID,
a fresh request ID and the current session/affinity scope. It retains parent
provider/model only as current-route hints. It requires exactly one matching
decision with a provider/model and a usable context window.

Vision tasks and structured image/video content add
`constraints.required_capabilities=['vision']`. The shared Router selector owns
the supported vision-model inventory and refuses unknown capabilities.
`auxiliary.<task>.routing_constraints` supplies trusted configuration constraints.
For `task='review'`, configured provider/model fields also become the explicit
`{kind:'review', owner:'auxiliary.review', ...}` constraint.

Internal benchmark, reviewer or escalation callers may wrap a call in:

```python
from agent.auxiliary_model_routing import trusted_routing

with trusted_routing("benchmark", {"allowed_routes": [[provider, model]]}):
    response = call_llm(task="benchmark", messages=messages)

with trusted_routing("vision-escalation", {"min_band": "R3"}):
    response = await async_call_llm(task="vision", messages=messages)
```

These are private Python scopes, not model-facing arguments. Ordinary provider
pins, task prose and diagnostic-looking strings do not bypass routing. Configured
route restrictions remain enforced when an inner scope supplies constraints.
Callers that need escalation must explicitly bind that trusted purpose; merely
passing a configured escalation model does not establish a lock.

The patch depends on the generic admission helper installed by
`integrations/hermes/patches/required-routing.py`: `agent.agent_model_routing.admitted(receipt)`.
After final payload validation, only the actual provider call runs inside that
context. The PTY boundary can consume the same receipt without selecting again.
Missing admission support fails before a provider request.

## Failure and retry policy

Required mode admits one host provider dispatch per auxiliary invocation. It
disables the same-provider retry loop and the entire recovery ladder. Nested
affordable-budget or streaming-mode retries cannot issue a second provider
request. A new `call_llm` invocation makes a new routing decision. Provider errors
still reach the caller's existing error handling; no task parser is replaced.

Final payload checks reject changed model IDs, route overrides in `extra_body`,
per-call credential headers, oversized model input and the Antigravity byte
limit. Receipts contain no credentials. Call scopes are isolated across async
tasks and nested calls; lazy Relay streams restore their own scope during
iteration and release it afterward.

## Installation and verification

Install the generic request/admission helper as part of the same coordinated
rollout. This installer requires an explicit checkout and performs no restarts
or configuration changes:

```sh
python integrations/hermes/patches/auxiliary-routing.py --hermes-root /path/to/hermes --check
python integrations/hermes/patches/auxiliary-routing.py --hermes-root /path/to/hermes
```

It updates `agent/auxiliary_client.py` and `hermes_cli/plugins.py` and installs
`agent/auxiliary_model_routing.py`. Reapplying is idempotent. Source drift or syntax
errors abort before any writes. The hook registry can already contain the shared
hook from either the delegation or generic request installer.

The focused test suite stages host functions and exercises both public entry
points, real result validation, scoped cached-client resolution,
concurrent async requests, cancellation, delayed streaming, trusted constraints,
payload changes and blocked retries. Provider calls and production plugin
discovery are replaced with test doubles; no live model requests are made.

```sh
PYTHONPATH=$HERMES_ROOT:$HERMES_ROOT/venv/lib/python3.12/site-packages \
PYTHONDONTWRITEBYTECODE=1 \
uv run --no-project --with pytest \
  --python $HERMES_ROOT/venv/bin/python \
  python -B -m pytest -q tests/integration/test_auxiliary_model_routing.py -p no:cacheprovider
```

This is auxiliary-helper coverage. Direct SDK client calls, availability probes,
native Claude launches, Context's separate extraction transport, and model calls
inside external CLI processes are outside this patch. It does not establish
live fleet deployment or complete model-call coverage.
