"""Offline connector manifest validation and pure, fail-closed call policy.

No discovery, execution, credentials, persistence or network access lives here.
Manifest, taint, test targets and approval records must come from the trusted host,
never from model-provided tool arguments. See docs/CONNECTORS.md.
"""
import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

KINDS = frozenset({"claude_ai", "mcp_stdio", "mcp_http"})
CLASSES = frozenset({"read", "write", "outward"})
TIERS = frozenset({"owner-interactive", "owner-unattended", "bot", "outcome-loop"})


class ManifestError(ValueError):
    """A manifest violates the version-1 schema."""


def _fields(value, required, optional, path):
    if not isinstance(value, dict):
        raise ManifestError(f"{path}: expected object")
    if any(not isinstance(key, str) for key in value):
        raise ManifestError(f"{path}: object keys must be strings")
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing or extra:
        raise ManifestError(f"{path}: missing {sorted(missing)}, unknown {sorted(extra)}")


def _name(value, path):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ManifestError(f"{path}: expected nonempty name without surrounding whitespace")


def validate_manifest(manifest):
    """Validate the closed schema; return an independent JSON-compatible copy."""
    _fields(manifest, {"version", "servers"}, set(), "manifest")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ManifestError("manifest.version: expected integer 1")
    if not isinstance(manifest["servers"], list):
        raise ManifestError("manifest.servers: expected array")
    seen = set()
    for index, server in enumerate(manifest["servers"]):
        path = f"servers[{index}]"
        _fields(server, {"name", "kind", "tools"}, {"url"}, path)
        _name(server["name"], path + ".name")
        if server["name"] in seen:
            raise ManifestError(f"{path}.name: duplicate server {server['name']!r}")
        seen.add(server["name"])
        if not isinstance(server["kind"], str) or server["kind"] not in KINDS:
            raise ManifestError(f"{path}.kind: expected one of {sorted(KINDS)}")
        if "url" in server:
            url = server["url"]
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid = (parsed is not None and parsed.scheme in {"http", "https"}
                         and parsed.hostname and not parsed.username and not parsed.password
                         and not parsed.fragment and not parsed.query
                         and not any(c.isspace() for c in url))
                if parsed is not None:
                    parsed.port  # Reject malformed ports too.
            except ValueError:
                valid = False
            if not valid:
                raise ManifestError(f"{path}.url: expected HTTP(S) URL without credentials, query or fragment")
        if server["kind"] == "mcp_http" and "url" not in server:
            raise ManifestError(f"{path}.url: required for mcp_http")
        if not isinstance(server["tools"], list):
            raise ManifestError(f"{path}.tools: expected array")
        tools = set()
        for ti, tool in enumerate(server["tools"]):
            tp = f"{path}.tools[{ti}]"
            _fields(tool, {"name", "class"}, set(), tp)
            _name(tool["name"], tp + ".name")
            if tool["name"] in tools:
                raise ManifestError(f"{tp}.name: duplicate tool {tool['name']!r}")
            tools.add(tool["name"])
            if not isinstance(tool["class"], str) or tool["class"] not in CLASSES:
                raise ManifestError(f"{tp}.class: expected one of {sorted(CLASSES)}")
    return json.loads(json.dumps(manifest))


def suggest_class(tool_name):
    """Name-only suggestion for human manifest authors. NEVER authorization."""
    if not isinstance(tool_name, str):
        return "outward"
    words = set(re.sub(r"([a-z])([A-Z])", r"\1_\2", tool_name).lower().replace("-", "_").split("_"))
    words.update(re.findall(r"[a-z]+", tool_name.lower()))
    if words & {"send", "post", "publish", "delete", "share", "pay", "invite", "deploy", "transfer"}:
        return "outward"
    if words & {"get", "list", "search", "read", "query", "fetch"}:
        return "read"
    if words & {"create", "update", "edit", "write", "upload", "draft"}:
        return "write"
    return "outward"


def _json_value(value):
    # Reject coercions (integer object keys, tuples, NaN) that can alias another call.
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("arguments must contain only finite JSON values with string object keys")


def call_hash(server, tool, args):
    """SHA-256 of canonical UTF-8 JSON [server, tool, args]; object order is irrelevant."""
    _name(server, "server")
    _name(tool, "tool")
    if type(args) is not dict:
        raise ValueError("arguments must be a JSON object")
    _json_value(args)
    payload = json.dumps([server, tool, args], sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunState:
    manifest: dict
    tainted: bool = False
    test_targets: tuple = ()


def mark_external_read(run_state):
    """Host calls after outside email/chat/docs/web is read; taint is monotonic."""
    return replace(run_state, tainted=True)


def decide(caller_tier, server, tool, args, run_state, approvals):
    """Return (allow|ask|deny, reason), without mutating any inputs.

    Approvals: trusted mapping of call_hash -> {"approved": True}. The host
    scopes records to this run and obtains them from an explicit owner action.
    An approval never overrides tier restrictions. No suggested class is used.
    """
    if not isinstance(caller_tier, str) or caller_tier not in TIERS:
        return "deny", "unknown caller tier"
    if not isinstance(run_state, RunState) or type(run_state.tainted) is not bool:
        return "deny", "invalid trusted run state"
    if (not isinstance(run_state.test_targets, (tuple, list))
            or any(not isinstance(s, str) or not s.strip() for s in run_state.test_targets)):
        return "deny", "invalid test-target list"
    try:
        manifest = validate_manifest(run_state.manifest)
        digest = call_hash(server, tool, args)
    except (ValueError, TypeError, RecursionError):
        return "deny", "invalid manifest or call"
    if caller_tier == "outcome-loop" and server not in run_state.test_targets:
        return "deny", "outcome-loop server is not an explicit test target"
    risk = "outward"  # Unknown server/tool must never inherit a name suggestion.
    for entry in manifest["servers"]:
        if entry["name"] == server:
            for declared in entry["tools"]:
                if declared["name"] == tool:
                    risk = declared["class"]
                    break
            break
    if caller_tier == "bot" and risk != "read":
        return "deny", "bot is read-only"
    if caller_tier == "outcome-loop" and risk == "outward":
        return "deny", "outcome-loop cannot approve or perform outward calls"
    if risk != "outward":
        return "allow", f"manifest grants {risk}"
    record = approvals.get(digest) if isinstance(approvals, dict) else None
    if isinstance(record, dict) and record.get("approved") is True:
        return "allow", "explicit approval matches exact server, tool and arguments"
    if run_state.tainted:
        return "ask", "outside-content taint: exact-call owner approval required"
    return "ask", "outward or unknown tool: exact-call owner approval required"
