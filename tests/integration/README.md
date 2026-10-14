# Integration tests

These tests exercise the router against a real host and are skipped, with the reason, when that host is missing.

| module | needs | how to run |
|---|---|---|
| `test_required_model_routing.py`, `test_cron_dispatch_routing.py`, `test_delegation_model_routing.py`, `test_auxiliary_model_routing.py`, `test_router_gateway_admission.py`, `test_required_routing_full_loop.py` | pytest and an importable Hermes (`hermes_cli`); `HERMES_TEST_ROOT` points at the Hermes checkout whose files are staged and patched in a temp dir (default `~/.cortados/hermes/hermes-agent`). `test_required_model_routing.py` also loads CortadOS's `plugins/cortados-pty/model_routing.py` from `CORTADOS_REPO`. | inside the Hermes venv: `HERMES_TEST_ROOT=$HERMES_ROOT python -B -m pytest -q tests/integration -p no:cacheprovider` |
| `test_router_reviews_cortados.py` | the CortadOS tree at `CORTADOS_REPO` (default `~/cortados`): `faces.json`, `plugins/cortados-pty/mailguard.py` | `python3 -B -m unittest tests.integration.test_router_reviews_cortados` |

The installers under `integrations/hermes/patches/` are only ever applied to a staged copy; no test patches a live host.
