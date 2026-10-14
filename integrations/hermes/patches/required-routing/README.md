# Required model routing

`agent.model_router: cortados-router` makes each Hermes model request pass the
shared selector. The selected provider must have credentials, available quota,
enough context capacity and the capabilities required by the call. Explicit
review constraints remain mandatory. A missing decision blocks the call.

The trained scorer supplies difficulty. If it is unavailable, the Router still
selects from eligible models in the conservative fallback band using live
capacity. Profile model defaults and static fallback chains do not select the
answering model in required mode. The model default is retained only to allow
older host constructors to initialize before routing.

## Request sequence

1. Context assembles the current request from canonical history.
2. Router scores the assignment with the bounded, scoped task projection and
   relevant recall, then checks the assembled messages and tool schemas.
3. A provider/model change uses the host's transactional `switch_model` and
   causes fresh Context assembly and preflight from canonical history.
4. Final payload validation checks the same route. The Context execution guard
   consumes its own permission at dispatch; Router does not consume it.
5. Every subsequent tool-loop call and provider retry repeats this sequence.

Completed tools are preserved, never replayed by reassembly. Routing errors
are local execution failures, not fabricated model responses. Selected routes
and observed model responses are recorded separately. Image fit uses the host
image-token estimate; base64 transport bytes are not counted as text tokens.

The current scorer receives an explicitly labelled `advisory_text_v1`
projection. This does not claim that the promoted adapter understands the
typed Context contract or that an unbound task is authorized. Context owns the
larger overall-plan/dependency projection and task authorization.

## Other entry points

Install these adapters together with the required request helper:

- `integrations/hermes/patches/cron-routing.py`: selects every scheduled agent run and preserves
  its route constraints into subsequent model requests.
- `integrations/hermes/patches/delegation-routing.py`: selects and resolves credentials for every
  native child before constructing any child in a batch.
- `integrations/hermes/patches/auxiliary-routing.py`: routes vision, compression, titles and other
  calls through the host auxiliary API, including async and streaming calls.
- `CortadOS plugins/cortados-pty/model_routing.py`: routes external CLI launches and
  continuation submissions. CLI transports for an admitted Hermes request
  retain that receipt rather than choosing a different model downstream.

The three host adapters inherit `agent.model_router` when their own
`cron.model_router`, `delegation.model_router`, or `auxiliary.model_router` key
is absent. The fleet explicitly sets all four. Explicit null/empty keys retain
upstream host behavior; they are not part of the fleet's required policy.

External Claude/Codex CLI workers control their own internal requests. The
launch and subsequent submissions are routed, but this does not establish
per-request control inside those vendor processes. Vendor-native continuations
retain their provider/model because moving them would lose their transcript.
Context-owned direct consumers and the frozen RC28 canary require their own
coordinated adapters; this installer does not update them.

## Installation

All installers support `--check` to validate their current host anchors without
writing. Install on the intended host only, after coordinated graceful drain:

```sh
python3 integrations/hermes/patches/required-routing.py --hermes-root /path/to/hermes --check
python3 integrations/hermes/patches/cron-routing.py --hermes-root /path/to/hermes --check
python3 integrations/hermes/patches/delegation-routing.py --hermes-root /path/to/hermes --check
python3 integrations/hermes/patches/auxiliary-routing.py --hermes-root /path/to/hermes --check
```

Omit `--check` to apply. Set the four routing keys and clear
`fallback_providers` only after all adapters are installed. Preserve all
Context-owned settings and use `bin/gateway-restart.sh` for the main fleet.
Do not enable the policy in a frozen canary without its rollout owner.

Tests stage the actual host builders/retry loop, normalize native delegation,
exercise auxiliary dispatch, verify profile scope, and use fake providers.
They do not demonstrate live coverage; record a fresh runtime receipt before
claiming that a surface has been verified in production.
