"""Read native config provenance; retain no configuration or credential values."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import time

ROOT = Path(__file__).resolve().parent
REMOVED_ENV = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
ARGV = ["/opt/homebrew/bin/codex", "app-server", "--listen", "stdio://",
        "--disable", "hooks", "--disable", "plugins", "--disable", "apps", "--disable", "multi_agent"]
# Seconds: local metadata should return promptly; shorter can misclassify network startup,
# longer would retain an unresponsive native process needlessly. No retry or worker turn.
TIMEOUT_SECONDS = 30
DISALLOWED = {"mdm", "enterpriseManaged", "legacyManagedConfigTomlFromFile", "legacyManagedConfigTomlFromMdm"}
env = {key: value for key, value in os.environ.items() if key not in REMOVED_ENV}
process = subprocess.Popen(ARGV, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
selector = selectors.DefaultSelector()
selector.register(process.stdout, selectors.EVENT_READ)
selector.register(process.stderr, selectors.EVENT_READ)
pending = b""
stderr_bytes = 0
wire_provenance = []
redacted_frames = []
dynamic_aliases = {}
DYNAMIC_MAP_FIELDS = {"mcp_servers", "projects", "model_providers", "profiles", "plugins", "apps", "headers", "http_headers", "env_http_headers", "env", "marketplaces"}
SOURCE_TYPES = {"packagedDefaults", "mdm", "system", "enterpriseManaged", "user", "project", "sessionFlags", "legacyManagedConfigTomlFromFile", "legacyManagedConfigTomlFromMdm"}


def alias(value):
    if value not in dynamic_aliases:
        dynamic_aliases[value] = f"redacted_key_{len(dynamic_aliases) + 1}"
    return dynamic_aliases[value]


def redact(value, path=(), dynamic=False):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            private_key = dynamic or "/" in key or "\\" in key
            safe_key = alias(key) if private_key else key
            map_values = key in DYNAMIC_MAP_FIELDS or key == "origins" or (key == "tools" and "mcp_servers" in path)
            result[safe_key] = redact(item, path + (key,), map_values)
        return result
    if isinstance(value, list):
        return [redact(item, path + ("[]",)) for item in value]
    if isinstance(value, str):
        if len(path) >= 2 and path[-2:] == ("name", "type") and value in SOURCE_TYPES:
            return value
        return "redacted_string"
    return value


def retain_redacted_frame(method, raw_line, message):
    sanitized = (json.dumps(redact(message), separators=(",", ":")) + "\n").encode()
    redacted_frames.append(sanitized)
    name = "config-read.redacted.jsonl" if method == "config/read" else "requirements-read.redacted.jsonl"
    (ROOT / name).write_bytes(sanitized)
    wire_provenance.append({"method": method, "raw_frame_sha256": hashlib.sha256(raw_line + b"\n").hexdigest(),
                            "redacted_frame_sha256": hashlib.sha256(sanitized).hexdigest(), "file": name})


def call(number, method, params):
    global pending, stderr_bytes
    request = {"id": number, "method": method, "params": params}
    process.stdin.write((json.dumps(request) + "\n").encode())
    process.stdin.flush()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            message = json.loads(line)
            if message.get("id") == number:
                if method in {"config/read", "configRequirements/read"}:
                    retain_redacted_frame(method, line, message)
                if "error" in message:
                    raise RuntimeError(f"native RPC error code {message['error'].get('code')}")
                return message.get("result")
            if "id" in message and "method" in message:
                response = {"id": message["id"], "error": {"code": -32601, "message": "Metadata only"}}
                process.stdin.write((json.dumps(response) + "\n").encode())
                process.stdin.flush()
        for key, _ in selector.select(max(0, deadline - time.monotonic())):
            block = os.read(key.fd, 65536)
            if not block:
                selector.unregister(key.fileobj)
                if key.fileobj is process.stdout:
                    raise RuntimeError("native metadata process ended")
            elif key.fileobj is process.stderr:
                stderr_bytes += len(block)
            else:
                pending += block
    raise TimeoutError("native metadata deadline")


def summarize_layer(layer):
    source = layer.get("name", {})
    kind = source.get("type", "unknown")
    config = layer.get("config")
    present = isinstance(config, dict) and bool(config)
    servers = config.get("mcp_servers", {}) if isinstance(config, dict) else {}
    return {"source_type": kind, "version": layer.get("version"),
            "has_config_entries": present, "disabled": bool(layer.get("disabledReason")),
            "mcp_server_name_count": len(servers) if isinstance(servers, dict) else None,
            "disallowed_authority_layer_present": kind in DISALLOWED or (kind == "system" and present)}


started = time.monotonic()
outcome = {}
try:
    call(1, "initialize", {"clientInfo": {"name": "cortados-policy-capture", "version": "0.1.0"},
                          "capabilities": {"experimentalApi": True}})
    process.stdin.write(b'{"method":"initialized","params":{}}\n')
    process.stdin.flush()
    response = call(2, "config/read", {"includeLayers": True, "cwd": str(ROOT)})
    layers = [summarize_layer(layer) for layer in response.get("layers", [])]
    effective_servers = response.get("config", {}).get("mcp_servers", {})
    outcome = {"layers": layers,
               "any_disallowed_authority_layer": any(layer["disallowed_authority_layer_present"] for layer in layers),
               "effective_mcp_server_name_count": len(effective_servers) if isinstance(effective_servers, dict) else None}
    del effective_servers, response
    requirements = call(3, "configRequirements/read", {})
    required = requirements.get("requirements")
    outcome["requirements_present"] = required is not None
    outcome["non_null_requirement_fields"] = sorted(key for key, value in required.items() if value is not None) if isinstance(required, dict) else []
    del required, requirements
finally:
    process.stdin.close()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=3)
    outcome.update({"elapsed_seconds": time.monotonic() - started,
                    "native_exit_code": process.returncode, "stderr_byte_count": stderr_bytes,
                    "cli_version": "0.162.1", "argv": ARGV,
                    "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "raw_configuration_retained": False, "worker_turn_started": False,
                    "ignore_user_config_available_on_app_server": False})
    (ROOT / "policy-metadata.json").write_text(json.dumps(outcome, indent=2) + "\n")
    wire = b"".join(redacted_frames)
    (ROOT / "policy-wire.redacted.jsonl").write_bytes(wire)
    provenance = {"frames": wire_provenance, "wire_sha256": hashlib.sha256(wire).hexdigest(),
                  "raw_frames_memory_only": True, "alias_count": len(dynamic_aliases),
                  "procedure": "All string scalar values redacted except ConfigLayerSource name.type discriminators; dynamic map keys and origins keys consistently aliased; null/boolean/number values and all field/list/map structure retained. Native frame line hashes computed before redaction; raw frames never persisted."}
    (ROOT / "wire-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(outcome, indent=2))
    print(json.dumps(provenance, indent=2))
