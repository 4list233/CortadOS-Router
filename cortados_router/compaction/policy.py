"""Load and validate the checkpoint policy, `tables/compaction.json` (stdlib only).

Every setting is `{"value": ..., "why": "..."}`; an unknown key, a missing key, a missing reason or a value of the
wrong shape fails loudly with PolicyError. Reason: a silently ignored typo in a threshold would let sessions run into
the client's own lossy auto-compaction. Values and their sources: docs/COMPACTION-THRESHOLDS.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "tables" / "compaction.json"

META_KEYS = {"status", "version", "why"}
SECTIONS = ("goal", "recency_pin", "rules", "decisions", "in_flight", "files", "excerpts")


class PolicyError(ValueError):
    pass


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _dict_of(keys, pred):
    def check(v):
        return isinstance(v, dict) and set(v) == set(keys) and all(pred(x) for x in v.values())
    return check


_nonneg_int = lambda x: _int(x) and x >= 0  # noqa: E731
_threshold = _dict_of(("pct", "cap_tokens"), lambda x: _num(x) and x > 0)

SETTINGS = {
    "warn": _threshold,
    "checkpoint": _threshold,
    "force": _threshold,
    "price_cliff_tokens": lambda v: isinstance(v, dict) and all(isinstance(k, str) and _int(x) and x > 0
                                                                 for k, x in v.items()),
    "price_cliff_margin_tokens": _nonneg_int,
    "velocity": _dict_of(("lookback_turns", "projection_turns", "single_turn_pct_of_window"),
                         lambda x: _num(x) and x > 0),
    "idle_cache_ttl_s": lambda v: isinstance(v, dict) and v and all(_int(x) and x > 0 for x in v.values()),
    "self_handoff_deadline_s": _dict_of(("after_tool_end", "force"), lambda x: _int(x) and x > 0),
    "time_based_timer": lambda v: isinstance(v, bool),
    "handoff_total_tokens": lambda v: _int(v) and v > 0,
    "section_budgets": _dict_of(SECTIONS, _nonneg_int),
    "goal_prompts": lambda v: _int(v) and v >= 1,
    "recency_pin": _dict_of(("user_turns", "tool_calls"), _nonneg_int),
    "files_max": lambda v: _int(v) and v >= 1,
    "recency_window_messages": _nonneg_int,
    "recency_bonus": lambda v: _num(v) and v >= 0,
    "chars_per_token": lambda v: isinstance(v, dict) and "unknown" in v and all(_num(x) and x > 0
                                                                                 for x in v.values()),
    "excerpt_tool_result_chars": _dict_of(("head", "tail"), _nonneg_int),
    "excerpt_message_truncation_chars": lambda v: _int(v) and v > 0,
    "provider_tool_result_cap_tokens": lambda v: _int(v) and v > 0,
    "circuit_breaker": _dict_of(("max_forced_restarts", "window_s", "fresh_open_max_pct_of_checkpoint"),
                                lambda x: _num(x) and x > 0),
    "pruning": _dict_of(("start_cap_tokens", "start_pct", "keep_last_results", "min_freed_tokens"),
                        lambda x: _num(x) and x >= 0),
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
    for part in ("pct", "cap_tokens"):
        if not out["warn"][part] < out["checkpoint"][part] < out["force"][part]:
            raise PolicyError("thresholds must satisfy warn.%s < checkpoint.%s < force.%s" % (part, part, part))
    if out["force"]["pct"] >= 100:
        raise PolicyError("force.pct must be below 100")
    # Section budgets are caps that may sum above the total (the total trims); a single section above the total
    # could never fit and is a typo.
    if max(out["section_budgets"].values()) > out["handoff_total_tokens"]:
        raise PolicyError("a section budget exceeds handoff_total_tokens")
    return out


def thresholds(pol: Dict[str, Any], window_tokens: float, model: str = "") -> Dict[str, float]:
    """Token thresholds for a window: min(pct of W, cap); force also <= price cliff - margin for a listed model."""
    out = {name: min(pol[name]["pct"] * window_tokens / 100.0, pol[name]["cap_tokens"])
           for name in ("warn", "checkpoint", "force")}
    cliff = pol["price_cliff_tokens"].get(model) if model else None
    if cliff is not None:
        out["force"] = min(out["force"], cliff - pol["price_cliff_margin_tokens"])
        out["checkpoint"] = min(out["checkpoint"], out["force"])
        out["warn"] = min(out["warn"], out["checkpoint"])
    return out


def load(path: Any = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    with open(p, encoding="utf-8") as fh:
        return validate(json.load(fh))
