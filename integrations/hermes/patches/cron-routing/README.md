# Cron dispatch routing

After a Hermes update, run `python3 integrations/hermes/patches/cron-routing.py`. The patch installs
the generic `route_cron_model` hook after prompt preparation and before provider
resolution. It refuses unfamiliar upstream call sites. Validate with Hermes's
`scripts/run_tests.sh tests/integration/test_cron_dispatch_routing.py`.

The fleet seed and existing profiles set `cron.model_router: cortados-router`.
The Router plugin scores each prepared job prompt, checks live capacity and
credential availability, and picks an eligible provider/model. Missing
credentials cause another router choice. If there is no usable route, the job
fails with a configuration error rather than escaping to a saved static fallback.
Script-only jobs require no model and retain their existing early exit.

Routing affects only the in-memory occurrence; saved prompts, schedules, delivery
and model pins are preserved. The selected route and routing session are written
to each run's output header. `bin/route-crons.py` collects those outcomes and
steers auxiliary calls; it no longer edits job model pins. Historical reports
without model evidence are recorded as unknown, not attributed to today's pin.

Coordinate rollout with Context and use `bin/gateway-restart.sh` to load the
patched host and plugin. Retain the private pre-change config backup in
`~/.cortados/backups/cron-router-20261007-100653`. Reverting the `cron.model_router`
key restores Hermes's normal saved/default model behavior; removing the key does
not require undoing unrelated shared code.

Native `openai-codex` uses a separate Hermes OAuth grant. Add it at the fleet root
with `hermes auth add openai-codex --type oauth --label cortados-fleet --no-browser`
and complete the browser approval. Profiles inherit the grant under the gateway's
root `HERMES_HOME`. Do not copy a rotating refresh token from the Codex app.
