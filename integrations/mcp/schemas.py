# PURPOSE: Validate and canonicalize the closed MCP worker request/event contract.
# CONTRACT: RT-MCP-001 sections 2, 3 and 6; invalid input never claims identity.
# DEPENDS: Python standard library only; admission policy lives in worker_admission.
"""Closed request types and transport-independent typed refusals."""

import json
import os
import sys
import time
import uuid


ERROR_CODES = frozenset("invalid_request idempotency_conflict model_unavailable account_unavailable subscription_required quota_unknown quota_spent scorer_unavailable scoring_context_too_large worker_context_too_large needs_clarification permission_refused connector_unavailable workspace_busy capacity_exhausted job_not_found storage_error native_version_mismatch native_protocol_error native_failed output_limit timeout cancel_requested broker_interrupted containment_failed job_not_terminal".split())
JOB_STATES = frozenset("queued running cancelling succeeded failed cancelled timed_out interrupted".split())
TOOLS = ("route", "delegate", "job_status", "job_result", "job_cancel", "context_requirements", "job_feedback")


class WorkerError(Exception):
    """A public refusal carries safe operator text, never native error output."""

    def __init__(self, code, message):
        if code not in ERROR_CODES:
            raise ValueError("Unknown worker error code")
        self.code, self.message = code, str(message)
        super().__init__(self.message)

    def payload(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def object_fields(value, allowed, required=(), label="object"):
    if not isinstance(value, dict):
        raise WorkerError("invalid_request", f"{label} must be an object")
    if set(value) - set(allowed) or set(required) - set(value):
        raise WorkerError("invalid_request", f"{label} has missing or unknown fields")
    return value


def text_value(value, label, *, blank=False):
    if not isinstance(value, str) or (not blank and not value.strip()):
        raise WorkerError("invalid_request", f"{label} must be a {'string' if blank else 'nonblank string'}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkerError("invalid_request", f"{label} must be valid UTF-8") from exc
    return value


def string_list(value, label, *, empty=True):
    if not isinstance(value, list) or (not empty and not value):
        raise WorkerError("invalid_request", f"{label} must be an array")
    result = [text_value(item, label) for item in value]
    if len(set(result)) != len(result):
        raise WorkerError("invalid_request", f"{label} contains duplicate values")
    return result


def positive_integer(value, label):
    if type(value) is not int or value <= 0:
        raise WorkerError("invalid_request", f"{label} must be a positive integer")
    return value


def uuid4(value, label):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
        if parsed is None or parsed.version != 4 or str(parsed) != value.lower():
            raise ValueError("Not a UUIDv4 string")
    except (ValueError, AttributeError) as exc:
        raise WorkerError("invalid_request", f"{label} must be a UUIDv4 string") from exc
    return value


def _selection(value):
    object_fields(value, ("mode", "model", "routing_brief"), ("mode",), "selection")
    if value["mode"] == "explicit":
        object_fields(value, ("mode", "model"), ("mode", "model"), "explicit selection")
        return {"mode": "explicit", "model": text_value(value["model"], "model")}
    if value["mode"] == "s1":
        object_fields(value, ("mode", "routing_brief"), ("mode",), "S1 selection")
        result = {"mode": "s1"}
        if "routing_brief" in value:
            result["routing_brief"] = text_value(value["routing_brief"], "routing_brief", blank=True)
        return result
    raise WorkerError("invalid_request", "selection.mode must be explicit or s1")


def _permissions(value):
    object_fields(value, ("network", "mcp_servers"), label="permissions")
    network = value.get("network", False)
    if type(network) is not bool:
        raise WorkerError("invalid_request", "permissions.network must be boolean")
    grants = value.get("mcp_servers", [])
    if not isinstance(grants, list):
        raise WorkerError("invalid_request", "mcp_servers must be an array")
    result = []
    for grant in grants:
        object_fields(grant, ("name", "tools"), ("name", "tools"), "connector grant")
        result.append({"name": text_value(grant["name"], "connector name"),
                       "tools": string_list(grant["tools"], "granted tools", empty=False)})
    if len({grant["name"] for grant in result}) != len(result):
        raise WorkerError("invalid_request", "Duplicate connector grants")
    return {"network": network, "mcp_servers": result}


def validate_feedback(arguments, config):
    object_fields(arguments, ('job_id', 'feedback_id', 'verdict', 'summary', 'checks'),
                  ('job_id', 'feedback_id', 'verdict', 'summary', 'checks'), 'job_feedback')
    result = {key: uuid4(arguments[key], key) for key in ('job_id', 'feedback_id')}
    verdict = arguments['verdict']
    if verdict not in ('accepted', 'rejected', 'inconclusive'):
        raise WorkerError('invalid_request', 'Invalid planner verdict')
    summary = text_value(arguments['summary'], 'summary')
    if not isinstance(arguments['checks'], list):
        raise WorkerError('invalid_request', 'checks must be an array')
    checks = []
    for check in arguments['checks']:
        object_fields(check, ('kind', 'reference', 'claim'), ('kind', 'reference', 'claim'), 'check')
        if check['kind'] not in ('command', 'artifact', 'review') or check['claim'] not in ('passed', 'failed', 'unknown'):
            raise WorkerError('invalid_request', 'Invalid check kind or claim')
        checks.append(dict(check, reference=text_value(check['reference'], 'reference')))
    claims = [check['claim'] for check in checks]
    if (verdict == 'accepted' and (not claims or any(claim != 'passed' for claim in claims))
            or verdict == 'rejected' and 'failed' not in claims):
        raise WorkerError('invalid_request', 'Verdict requires supporting planner checks')
    result.update(verdict=verdict, summary=summary, checks=checks)
    # Complete UTF-8 feedback bytes share the established private handoff budget:
    # smaller rejects useful checks, larger permits unbounded persistence/RPC.
    if len(canonical(result)) > config['limits']['max_handoff_bytes']:
        raise WorkerError('invalid_request', 'Planner feedback exceeds configured byte bound')
    return result


def validate(tool, arguments, config):
    """Insert only documented defaults; retain all task and array/string bytes."""
    if tool not in TOOLS:
        raise WorkerError("invalid_request", "Unknown worker tool")
    if tool == "job_feedback":
        return validate_feedback(arguments, config)
    if tool.startswith("job_"):
        object_fields(arguments, ("job_id",), ("job_id",), tool)
        return {"job_id": uuid4(arguments["job_id"], "job_id")}
    required = ("task", "selection")
    allowed = required + (("context", "task_evidence") if tool == "context_requirements" else
                          ("context", "constraints", "permissions", "account_ref", "task_evidence"))
    if tool == "delegate":
        required += ("cwd", "request_id", "timeout_s")
        allowed += ("cwd", "request_id", "timeout_s")
    object_fields(arguments, allowed, required, tool)
    selection = _selection(arguments["selection"])
    constraints = object_fields(arguments.get("constraints", {}), ("allowed_models", "min_band"), label="constraints")
    normalized_constraints = {}
    if "allowed_models" in constraints:
        normalized_constraints["allowed_models"] = string_list(constraints["allowed_models"], "allowed_models")
    if "min_band" in constraints:
        if selection["mode"] == "explicit" or constraints["min_band"] not in ("R0", "R1", "R2", "R3", "R4"):
            raise WorkerError("invalid_request", "min_band requires S1 selection and an R0-R4 band")
        normalized_constraints["min_band"] = constraints["min_band"]
    if arguments.get("account_ref", "native-default") != "native-default":
        raise WorkerError("invalid_request", "account_ref must be native-default")
    result = {"task": text_value(arguments["task"], "task"),
              "context": text_value(arguments.get("context", ""), "context", blank=True),
              "selection": selection, "constraints": normalized_constraints,
              "permissions": _permissions(arguments.get("permissions", {})), "account_ref": "native-default"}
    if "task_evidence" in arguments:
        from cortados_router.task_evidence import validate_evidence
        result["task_evidence"] = validate_evidence(arguments["task_evidence"])
    if tool == "delegate":
        cwd = text_value(arguments["cwd"], "cwd")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise WorkerError("invalid_request", "cwd must be an absolute existing directory")
        timeout = positive_integer(arguments["timeout_s"], "timeout_s")
        result.update(cwd=os.path.realpath(cwd), timeout_s=timeout,
                      request_id=uuid4(arguments["request_id"], "request_id"))
    return result


def canonical(arguments):
    """Identity excludes request_id only; input must first pass validate()."""
    return json.dumps({key: value for key, value in arguments.items() if key != "request_id"},
                      ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def emit_event(config, event, request_id=None, route_id=None, job_id=None,
               from_state=None, state=None, reason=None):
    if event not in ("refusal", "state_transition") or reason not in ERROR_CODES | {None}:
        raise ValueError("Invalid structured event")
    if from_state not in JOB_STATES | {None} or state not in JOB_STATES | {None}:
        raise ValueError("Invalid structured event state")
    # Invalid request IDs are unavailable observations, never untrusted log text.
    ids = {}
    for name, value in (("request_id", request_id), ("route_id", route_id), ("job_id", job_id)):
        try:
            ids[name] = uuid4(value, name) if value is not None else None
        except WorkerError:
            ids[name] = None
    row = {"schema": "cortados.mcp-worker.event.v1", "at_ms": max(0, time.time_ns() // 1_000_000),
           "event": event, "client_id": config.get("client_id", ""), **ids,
           "from_state": from_state, "state": state, "reason": reason}
    print(json.dumps(row, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)
