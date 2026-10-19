# PURPOSE: Expose host-neutral explicit or S1 selection for one scoped worker task.
# CONTRACT: RT-MCP-001 sections 3 and 4; advice never launches or decomposes work.
# DEPENDS: Existing core scoring/resolution/logging, shared worker_admission, schemas.
"""Preserve exact parent strings and invoke the established Router owners."""

import hashlib
import json
import sqlite3
import uuid

from integrations.mcp.schemas import WorkerError, validate
from . import core, worker_admission


from .task_evidence import (MAX_SCORER_CHARS, handoff, missing_fields,
                            provenance, scoring_text)


def _score(text):
    try:
        decision = core.decide(text)
        if not isinstance(decision, dict) or decision.get("degraded") or not decision.get("answers"):
            raise WorkerError("scorer_unavailable", "S1 could not score the requested work")
        answers = decision["answers"]
        schema = core.SCHEMA
        if not isinstance(answers, dict) or set(answers) != set(schema["required"]):
            raise ValueError("Incomplete scorer answer")
        for key, field in schema["properties"].items():
            value = answers[key]
            if field["type"] == "integer" and (type(value) is not int or not field["minimum"] <= value <= field["maximum"]):
                raise ValueError("Invalid scorer integer")
            if field["type"] == "boolean" and type(value) is not bool:
                raise ValueError("Invalid scorer boolean")
            if "enum" in field and value not in field["enum"]:
                raise ValueError("Invalid scorer category")
        if answers["needs_clarification"]:
            raise WorkerError("needs_clarification", "S1 requires clarification before execution")
        return decision
    except WorkerError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OSError) as exc:
        raise WorkerError("scorer_unavailable", "S1 returned an invalid scoring observation") from exc


def _resolve(decision, arguments, admission, packet):
    resolved = core.resolve(decision, execution=True,
        allowed_routes=tuple(("codex-cli", model) for model in admission["models"]),
        minimum_band=arguments["constraints"].get("min_band"), resources=admission["resources"],
        input_fit={"assembled": True, "known": True, "input_bytes": len(packet.encode("utf-8")),
                   "input_tokens": core.turn_tokens(len(packet)), "transport": "codex-cli"})
    chosen = resolved.get("chosen")
    if not chosen or chosen.get("provider") != "codex-cli" or chosen.get("model") not in admission["models"]:
        raise WorkerError("model_unavailable", "No admitted native model satisfies the Router selection policy")
    return resolved


def _log(route_id, arguments, decision, resolved, config):
    try:
        core.log_route(route_id, {"platform": "mcp:" + config["client_id"], "unit_kind": "interactive",
                                 "request": arguments["task"], "history_msgs": 0,
                                 "context": json.dumps(provenance(arguments), ensure_ascii=False)},
                       decision, resolved)
    except (OSError, sqlite3.Error) as exc:
        raise WorkerError("storage_error", "The isolated Router observation could not be recorded") from exc


def route_task(arguments, config, metadata, *, handoff_packet: str | None = None):
    tool = "delegate" if any(key in arguments for key in ("cwd", "request_id", "timeout_s")) else "route"
    arguments = validate(tool, arguments, config)
    if "task_evidence" in arguments and missing_fields(arguments):
        raise WorkerError("needs_clarification", "Task evidence is incomplete; call context_requirements and answer its questions before resubmitting")
    packet = handoff(arguments) if handoff_packet is None else handoff_packet
    worker_bytes = len(packet.encode("utf-8"))
    if worker_bytes > config["limits"]["max_handoff_bytes"]:
        raise WorkerError("worker_context_too_large", "Worker handoff exceeds the configured UTF-8 byte bound")
    mode = arguments["selection"]["mode"]
    scoring = scoring_text(arguments)
    if scoring is not None and len(scoring) > MAX_SCORER_CHARS:
        raise WorkerError("scoring_context_too_large", "S1 scoring input exceeds its 6000-character bound")
    admission = worker_admission.admit(arguments, config, metadata)
    if arguments["permissions"]["mcp_servers"]:
        from integrations.mcp.grant_proxy import preflight
        preflight(config, arguments["permissions"])
    if mode == "s1":
        decision = _score(scoring)
        admission = worker_admission.admit(arguments, config, metadata)
        resolved = _resolve(decision, arguments, admission, packet)
    else:
        admission = worker_admission.admit(arguments, config, metadata)
        decision = {"engine": "explicit", "engine_ms": 0, "degraded": False}
        resolved = {"band": None, "D": None, "subagents": 0, "verifier": False, "skipped": [],
                    "hard_rules": ["selection_mode:explicit"],
                    "chosen": {"lane": "codex", "provider": "codex-cli", "model": arguments["selection"]["model"]}}
    # Recheck after any scorer/resolver wait. Refreshed resources rerun the same
    # resolver with the existing score; admission is not a prior route's token.
    latest = worker_admission.admit(arguments, config, metadata)
    if mode == "s1" and latest["snapshot"]["sampled_at_ms"] != admission["snapshot"]["sampled_at_ms"]:
        resolved = _resolve(decision, arguments, latest, packet)
        final = worker_admission.admit(arguments, config, metadata)
        if final["snapshot"]["sampled_at_ms"] != latest["snapshot"]["sampled_at_ms"]:
            raise WorkerError("quota_unknown", "Quota changed during the final Router resolution")
        latest = final
    chosen = resolved["chosen"]
    if chosen["model"] not in latest["models"]:
        raise WorkerError("model_unavailable", "The selected model is no longer admitted")
    route_id = str(uuid.uuid4())
    _log(route_id, arguments, decision, resolved, config)
    return {"route_id": route_id, "selection_mode": mode,
            "chosen": {"lane": "codex", "provider": "codex-cli", "model": chosen["model"], "account_ref": "native-default"},
            "band": resolved["band"], "D": resolved["D"],
            "suggested_subagents": resolved["subagents"] if mode == "s1" else None,
            "skipped": [{"provider": "codex-cli", "model": item["model"], "reason": item["why"]}
                        for item in resolved["skipped"]],
            "context": {"worker_bytes": worker_bytes, "scorer_chars": len(scoring) if scoring is not None else 0,
                        "scorer_digest": hashlib.sha256(scoring.encode("utf-8")).hexdigest() if scoring is not None else None},
            "quota": {"status": "fresh", "source": "codex-app-server", "sampled_at_ms": latest["snapshot"]["sampled_at_ms"]},
            "warnings": []}
