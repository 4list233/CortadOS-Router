"""Load and validate the checkpoint policy, `tables/compaction.json` (stdlib only).

Every setting is `{"value": ..., "why": "..."}`; an unknown key, a missing key, a missing reason or a value of the
wrong shape fails loudly with PolicyError. Reason: a silently ignored typo in a threshold would let sessions run into
the client's own lossy auto-compaction.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "tables" / "compaction.json"

META_KEYS = {"status", "version", "why"}
SECTIONS = ("goal", "rules", "decisions", "in_flight", "files", "excerpts")


class PolicyError(ValueError):
    pass


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _dict_of_ints(keys):
    def check(v):
        return isinstance(v, dict) and set(v) == set(keys) and all(_int(x) and x >= 0 for x in v.values())
    return check


SETTINGS = {
    "warn_pct": lambda v: _num(v) and 0 < v < 100,
    "checkpoint_pct": lambda v: _num(v) and 0 < v < 100,
    "force_pct": lambda v: _num(v) and 0 < v < 100,
    "ceiling_200k_class": _dict_of_ints(("max_window_tokens", "checkpoint_tokens")),
    "ceiling_1m_class": _dict_of_ints(("max_window_tokens", "checkpoint_tokens")),
    "velocity_turns": lambda v: _int(v) and v >= 1,
    "idle_boundary_only": lambda v: isinstance(v, bool),
    "self_handoff_deadline_s": lambda v: _int(v) and v > 0,
    "handoff_total_tokens": lambda v: _int(v) and v > 0,
    "section_budgets": _dict_of_ints(SECTIONS),
    "goal_prompts": lambda v: _int(v) and v >= 1,
    "recency_pin_messages": lambda v: _int(v) and v >= 0,
    "recency_bonus": lambda v: _num(v) and v >= 0,
    "tool_result_truncation_chars": lambda v: _int(v) and v > 0,
    "excerpt_message_truncation_chars": lambda v: _int(v) and v > 0,
    "circuit_breaker": _dict_of_ints(("max_restarts", "window_s", "pause_s")),
}


def validate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a flat {setting: value} dict plus "status"/"version"; raise PolicyError on any defect."""
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a JSON object")
    keys = set(raw)
    unknown = keys - META_KEYS - set(SETTINGS)
    missing = (META_KEYS | set(SETTINGS)) - keys
    if unknown:
        raise PolicyError("unknown policy keys: %s" % sorted(unknown))
    if missing:
        raise PolicyError("missing policy keys: %s" % sorted(missing))
    out: Dict[str, Any] = {"status": raw["status"], "version": raw["version"]}
    for name, check in SETTINGS.items():
        entry = raw[name]
        if not isinstance(entry, dict) or set(entry) != {"value", "why"}:
            raise PolicyError("%s must be {\"value\", \"why\"}" % name)
        if not isinstance(entry["why"], str) or not entry["why"].strip():
            raise PolicyError("%s has no reason (\"why\")" % name)
        if not check(entry["value"]):
            raise PolicyError("%s has an invalid value: %r" % (name, entry["value"]))
        out[name] = entry["value"]
    if not out["warn_pct"] < out["checkpoint_pct"] < out["force_pct"]:
        raise PolicyError("thresholds must satisfy warn_pct < checkpoint_pct < force_pct")
    if sum(out["section_budgets"].values()) > out["handoff_total_tokens"]:
        raise PolicyError("section_budgets sum exceeds handoff_total_tokens")
    if out["ceiling_200k_class"]["max_window_tokens"] >= out["ceiling_1m_class"]["max_window_tokens"]:
        raise PolicyError("ceiling_200k_class must cover smaller windows than ceiling_1m_class")
    return out


def load(path: Any = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    with open(p, encoding="utf-8") as fh:
        return validate(json.load(fh))
