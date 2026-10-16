# PURPOSE: Build the captured native invocation and interpret closed execution evidence.
# CONTRACT: RT-MCP-001 sections 2.1, 3.1 and 5; native completion is not planner acceptance.
# DEPENDS: stdlib, schemas.WorkerError; native harness owns execution and authentication.
import json
import os
from pathlib import Path
import sys

from .schemas import WorkerError

CLI_VERSION = "0.162.1"
ENVIRONMENT_NAMES = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                     "SSL_CERT_FILE", "SSL_CERT_DIR")
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")
DISABLED_FEATURES = ("hooks", "plugins", "apps", "multi_agent", "browser_use", "browser_use_external",
                     "browser_use_full_cdp_access", "computer_use", "image_generation", "skill_search")


def native_environment(config):
    """The sole native environment owner excludes all ambient provider/connector overrides."""
    environment = {name: os.environ[name] for name in ENVIRONMENT_NAMES if name in os.environ}
    environment["CODEX_HOME"] = config["codex_home"]
    return environment


def toml_value(value):
    """Encode whole-table overrides, including paths containing TOML punctuation."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(toml_value(key) + "=" + toml_value(item) for key, item in value.items()) + "}"
    raise TypeError("Unsupported native TOML value")


def native_arguments(config, arguments, route, final_path, proxy_socket=None):
    servers = {}
    for grant in arguments["permissions"]["mcp_servers"]:
        if proxy_socket is None:
            raise WorkerError("connector_unavailable", "Granted connector has no private proxy channel")
        servers[grant["name"]] = {
            "command": sys.executable,
            "args": ["-B", str(Path(__file__).with_name("grant_proxy.py")), "--socket", str(proxy_socket), "--server", grant["name"]],
            "required": True, "enabled_tools": grant["tools"],
            "tools": {tool: {"approval_mode": "approve"} for tool in grant["tools"]},
        }
    settings = {
        "approval_policy": "never", "web_search": "disabled",
        "projects": {arguments["cwd"]: {"trust_level": "untrusted"}},
        "sandbox_workspace_write": {"exclude_slash_tmp": True, "exclude_tmpdir_env_var": True,
                                    "writable_roots": [], "network_access": arguments["permissions"]["network"]},
        "mcp_servers": servers,
    }
    argv = [config["codex_bin"], "exec", "--strict-config", "--ignore-user-config", "--ignore-rules",
            "--ephemeral", "--skip-git-repo-check", "-C", arguments["cwd"], "-s", "workspace-write",
            "-m", route["chosen"]["model"]]
    for key, value in settings.items():
        argv.extend(["-c", key + "=" + toml_value(value)])
    for feature in DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    return argv + ["--json", "--color", "never", "-o", str(final_path), "-"]


class EventReader:
    """Validate JSONL incrementally while preserving native bytes in separate artifacts."""
    def __init__(self):
        self.pending = b""
        self.completions = []
        self.last_message = None
        self.final_message = None
        self.failed = False
        self.warnings = []

    def feed(self, chunk):
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            self._line(line)

    def finish(self):
        if self.pending:
            self._line(self.pending)
            self.pending = b""

    def _line(self, line):
        if not line.strip():
            return
        try:
            event = json.loads(line.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError) as exc:
            raise WorkerError("native_protocol_error", "Native output is not UTF-8 JSONL") from exc
        if not isinstance(event, dict):
            raise WorkerError("native_protocol_error", "Native output event is not an object")
        kind = event.get("type")
        if kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self.last_message = item["text"]
            elif item.get("type") == "error":
                self.warnings.append({"code": "native_warning", "message": str(item.get("message", "Native warning item"))})
        elif kind == "turn.completed":
            self.completions.append(event)
            self.final_message = self.last_message
        elif kind == "turn.failed":
            self.failed = True

    def completion(self, returncode, final_path):
        if returncode != 0 or self.failed:
            raise WorkerError("native_failed", "Native worker did not complete successfully")
        self.finish()
        if len(self.completions) != 1 or not self.final_message or not self.final_message.strip():
            raise WorkerError("native_protocol_error", "Native completion evidence is missing or conflicting")
        try:
            final = Path(final_path).read_bytes()
        except OSError as exc:
            raise WorkerError("native_protocol_error", "Native final-message artifact is missing") from exc
        if final != self.final_message.encode("utf-8"):
            raise WorkerError("native_protocol_error", "Native final-message artifact differs from terminal message")
        raw = self.completions[0].get("usage")
        raw = raw if isinstance(raw, dict) else {}
        usage = {key: value if type(value := raw.get(key)) is int and value >= 0 else None for key in USAGE_FIELDS}
        if any(value is None for value in usage.values()):
            self.warnings.append({"code": "usage_unavailable", "message": "One or more native token counts are unavailable"})
        return self.final_message, usage


def terminal_result(route, job_id, artifacts, *, summary=None, usage=None, warnings=None, error=None):
    """Closed service result; failed/interrupted native prose never becomes a summary."""
    return {
        "summary": summary if error is None else None,
        "worker": {"provider": "codex-cli", "requested_model": route["chosen"]["model"],
                   "reported_model": None, "cli_version": CLI_VERSION, "account_ref": "native-default"},
        "usage": usage if error is None and usage is not None else {key: None for key in USAGE_FIELDS},
        "artifacts": {key: f"jobs/{job_id}/{name}" if name else None for key, name in artifacts.items()},
        "changed_paths": None, "verification": "planner_review_required", "warnings": warnings or [],
        "error": {"code": error.code, "message": error.message} if error else None,
    }
