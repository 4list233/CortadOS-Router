# PURPOSE: Load the operator's closed worker configuration without reading credentials.
# CONTRACT: RT-MCP-001 section 2.1; no implicit numeric budgets or live state home.
# DEPENDS: schemas validation primitives; standard-library filesystem and JSON support.
"""Startup-only configuration normalization; callers retain exact source bytes."""

import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .schemas import WorkerError, object_fields, positive_integer, string_list, text_value


# Required operator budgets: smaller values reject usable work or race startup;
# larger values retain more jobs/bytes, delay failure detection, or permit stale quota.
# Units and each particular two-sided tradeoff remain in the operator's acceptance record.
LIMITS = (
    "max_concurrent_jobs",  # Jobs: low denies useful parallelism; high raises CPU/memory pressure.
    "max_timeout_s",  # Seconds: low aborts useful tasks; high retains stuck work and claims.
    "termination_grace_s",  # Seconds: low interrupts orderly cleanup; high delays containment.
    "startup_timeout_s",  # Seconds: low rejects slow native reads; high stalls failed admission.
    "max_handoff_bytes",  # UTF-8 bytes: low rejects useful context; high exceeds captured input bounds.
    "max_output_bytes",  # Combined bytes: low rejects useful evidence; high retains excess memory/disk.
    "metadata_max_age_s",  # Seconds: low repeats probes; high admits against old quota observations.
    "process_poll_ms",  # Milliseconds: low increases process scans; high misses short-lived descendants.
)
# UTF-8 bytes: larger packets lack the protected native boundary capture; smaller
# operator limits may conservatively reject packets that the captured profile accepts.
CAPTURED_HANDOFF_BYTES = 65536


def config_bytes(path):
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise WorkerError("invalid_request", "Configuration path must be absolute")
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise WorkerError("invalid_request", "Configuration could not be read") from exc


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise WorkerError("invalid_request", "Configuration contains duplicate object keys")
        value[key] = item
    return value


def _absolute(value, label, *, executable=False, directory=False):
    value = text_value(value, label)
    if not os.path.isabs(value) or "\x00" in value:
        raise WorkerError("invalid_request", f"{label} must be an absolute path")
    # Executable spelling can select an interpreter's virtual environment;
    # resolving its symlink silently changes installed runtime dependencies.
    value = os.path.normpath(value) if executable else os.path.realpath(value)
    if executable and (not os.path.isfile(value) or not os.access(value, os.X_OK)):
        raise WorkerError("invalid_request", f"{label} must name an executable file")
    if directory and not os.path.isdir(value):
        raise WorkerError("invalid_request", f"{label} must name an existing directory")
    return value


def _env_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise WorkerError("invalid_request", "Connector environment references must be variable names")
    return value


def _connector(definition):
    if not isinstance(definition, dict):
        raise WorkerError("invalid_request", "Connector definition must be an object")
    transport = definition.get("transport")
    if transport == "stdio":
        fields = ("transport", "command", "args", "env_vars", "tools")
        object_fields(definition, fields, fields, "stdio connector")
        args = definition["args"]
        if not isinstance(args, list):
            raise WorkerError("invalid_request", "Connector args must be an array")
        result = {"transport": transport, "command": _absolute(definition["command"], "connector command", executable=True),
                  "args": [text_value(arg, "connector argument", blank=True) for arg in args],
                  "env_vars": [_env_name(name) for name in string_list(definition["env_vars"], "env_vars")]}
    elif transport == "http":
        fields = ("transport", "url", "bearer_token_env_var", "tools")
        object_fields(definition, fields, fields, "HTTP connector")
        url = text_value(definition["url"], "connector URL")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise WorkerError("invalid_request", "Connector URL is invalid") from exc
        try:
            local = hostname == "localhost" or ipaddress.ip_address(hostname or "").is_loopback
        except ValueError:
            local = False
        if not parsed.hostname or parsed.username or parsed.password or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            raise WorkerError("invalid_request", "Connector URL must use HTTPS or loopback HTTP")
        bearer = definition["bearer_token_env_var"]
        result = {"transport": transport, "url": url, "bearer_token_env_var": _env_name(bearer) if bearer is not None else None}
    else:
        raise WorkerError("invalid_request", "Unsupported connector transport")
    result["tools"] = string_list(definition["tools"], "connector tools", empty=False)
    return result


def load_config(path, raw=None):
    try:
        config = json.loads((config_bytes(path) if raw is None else raw).decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise WorkerError("invalid_request", "Configuration must be valid UTF-8 JSON") from exc
    required = ("schema", "client_id", "state_dir", "codex_bin", "codex_home", "model_allowlist", "limits")
    object_fields(config, required + ("allow_network", "connector_registry"), required, "configuration")
    if config["schema"] != "cortados.mcp-worker.config.v1":
        raise WorkerError("invalid_request", "Unsupported configuration schema")
    result = {"schema": config["schema"], "client_id": text_value(config["client_id"], "client_id"),
              "state_dir": _absolute(config["state_dir"], "state_dir"),
              "codex_bin": _absolute(config["codex_bin"], "codex_bin", executable=True),
              "codex_home": _absolute(config["codex_home"], "codex_home", directory=True),
              "model_allowlist": string_list(config["model_allowlist"], "model_allowlist", empty=False)}
    live = os.path.realpath(Path.home() / ".cortados")
    state = result["state_dir"]
    if state == live or state.startswith(live + os.sep):
        raise WorkerError("permission_refused", "Worker state must be separate from live CortadOS")
    if os.path.exists(state):
        stat = os.stat(state)
        if not os.path.isdir(state) or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise WorkerError("permission_refused", "Worker state directory must be private to this user")
    limits = object_fields(config["limits"], LIMITS, LIMITS, "limits")
    result["limits"] = {key: positive_integer(limits[key], key) for key in LIMITS}
    if result["limits"]["max_handoff_bytes"] > CAPTURED_HANDOFF_BYTES:
        raise WorkerError("invalid_request", "max_handoff_bytes exceeds the accepted native capture")
    network = config.get("allow_network", False)
    if type(network) is not bool:
        raise WorkerError("invalid_request", "allow_network must be boolean")
    registry = config.get("connector_registry", {})
    if not isinstance(registry, dict):
        raise WorkerError("invalid_request", "connector_registry must be an object")
    result.update(allow_network=network, connector_registry={
        text_value(name, "connector name"): _connector(value) for name, value in registry.items()})
    return result
