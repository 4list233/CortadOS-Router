# PURPOSE: Own shared model, account, quota and permission admission for MCP work.
# CONTRACT: RT-MCP-001 sections 4 and 5; both explicit and S1 use the same gates.
# DEPENDS: NativeMetadata observations, native scarcity adapter, immutable resources.
"""Admission facts are observations, never durable authorization tokens."""

import time

from integrations.mcp.schemas import WorkerError
from .scarcity_codex_native import project
from .selection_resources import SelectionResources


# Only these exact models have the protected command/final/usage and handoff
# captures for both accepted native profiles (0.162.1 and 0.163.0). Inventory alone is insufficient.
CAPTURED_MODELS = frozenset(("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"))


def validate_snapshot(snapshot, *, admission=True):
    account_response = snapshot.get("account")
    if not isinstance(account_response, dict):
        raise WorkerError("account_unavailable", "Native account metadata is unavailable")
    account = account_response.get("account")
    if not isinstance(account, dict):
        raise WorkerError("account_unavailable", "Native account is not logged in")
    if account.get("type") == "apiKey":
        raise WorkerError("subscription_required", "A native ChatGPT subscription login is required")
    if account.get("type") != "chatgpt" or account_response.get("requiresOpenaiAuth") is not True:
        raise WorkerError("account_unavailable", "Native subscription account is unavailable")
    return project(snapshot.get("rate_limits"), admission=admission)


def permissions(arguments, config):
    requested = arguments["permissions"]
    if requested["network"] and not config["allow_network"]:
        raise WorkerError("permission_refused", "Network access is not enabled by the operator")
    registry = config["connector_registry"]
    for grant in requested["mcp_servers"]:
        definition = registry.get(grant["name"])
        if definition is None or not set(grant["tools"]).issubset(definition["tools"]):
            raise WorkerError("permission_refused", "A requested connector or tool is not permitted")


def admit(arguments, config, metadata):
    """Reevaluate current policy and refresh expired native snapshots on every call."""
    if "timeout_s" in arguments and arguments["timeout_s"] > config["limits"]["max_timeout_s"]:
        raise WorkerError("invalid_request", "timeout_s exceeds the operator maximum")
    permissions(arguments, config)
    snapshot = metadata.snapshot()
    from .quota_admission import _age, _maximum, _number
    sampled = snapshot.get("sampled_at_ms")
    row = {"observed_at": sampled / 1000 if _number(sampled) else None,
           "observed_monotonic": snapshot.get("sampled_monotonic")}
    if _age(row, time.time(), time.monotonic(), _maximum(config)) is None:
        raise WorkerError("quota_unknown", "Native quota observation is stale or invalid")
    record = validate_snapshot(snapshot)
    if any(w["reset_epoch"] > row["observed_at"] + w["window_s"] for w in record["windows"]):
        raise WorkerError("quota_unknown", "Native quota reset exceeds its observed window")
    models = set(config["model_allowlist"]) & CAPTURED_MODELS & set(snapshot["models"])
    constraints = arguments["constraints"]
    if "allowed_models" in constraints:
        models &= set(constraints["allowed_models"])
    if arguments["selection"]["mode"] == "explicit":
        models &= {arguments["selection"]["model"]}
    if not models:
        raise WorkerError("model_unavailable", "No captured native model satisfies the requested constraints")
    return {"snapshot": snapshot, "models": tuple(sorted(models)),
            "resources": SelectionResources.from_quota(record)}
