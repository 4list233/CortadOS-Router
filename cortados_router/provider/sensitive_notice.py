"""The sensitive-cloud notice (#124): a request a sensitivity detector marked, answered in the cloud because the Pro
could not take it, is answered WITH a short notice saying so, and the route's row records it.

Owner decision (Forest, 2026-11-15): a request marked sensitive (the x-cortados-local flag, the node keyword list, the
planned S1 signal, a cloud refusal) normally runs only on the Pro's local model. When the Pro cannot take it, the
request goes to the cloud anyway and the owner is TOLD. "Private data while the Pro is away" follows the same rule.

Owner of three rules:
  vocabulary   the notice names a DETECTOR category and a REASON category from the fixed tables below, and the
               routed model's name. Never a keyword, a matched term or request text; an unknown category is shown as
               the table's fallback label, never as the string it was handed.
  framing      the notice is its own leading text block (prepend): on /v1/messages a separate content block before
               the model's content (JSON and SSE, indices following from the block list), on /v1/responses a separate
               leading `message` output item. tool_use blocks and the stop reason are the model's, untouched.
  history      the delivered form is the only form (#124 round 5): the session's history holds the reply exactly as
               the client received it (notice + model blocks), and history matching is plain equality against it. A
               client that sends an earlier turn in any other form (the notice merged into the text or dropped, e.g. by
               its own compaction) sent a rewritten history, handled like any other (rebuild or 409, same budget and
               terminal). A rebuilt transcript replays the client's text as sent: the backend model may then read the
               notice as part of a past assistant turn, which is harmless and intended (it is true, and it is history).
               Nothing is ever stripped, relabelled or kept per injection.

Escape hatch: OPT_OUT_HEADER: off on the request that would carry the notice suppresses the injected block only; the
route row and the log line are written all the same, marked `opted_out`.

Stdlib only, and no provider imports.
"""
from __future__ import annotations

import re
from typing import Any

# The route key routing.choose sets on a cloud route whose request a detector marked: {"detector": ..., "reason": ...}.
ROUTE_KEY = "sensitive_cloud"
# The detector routing.choose found on a request it moved to a local node (request_sensitivity's verdict, absent when
# every detector cleared it): if the node then cannot take the opening turn, the server's re-route names it.
DETECTOR_KEY = "sensitivity"
# Per-request opt-out: `off` (case-insensitive) suppresses the injected text for callers that must not get it (a
# caller parsing the reply as data). Anything else, or absent: the notice is shown.
OPT_OUT_HEADER = "x-cortados-sensitive-notice"
OPT_OUT_VALUES = ("off",)
SHOWN, OPTED_OUT = "shown", "opted_out"

# Detector category -> its label in the notice. routing.request_sensitivity's verdicts, plus the refusal fallback.
DETECTORS = {
    "flag": "flag",
    "keyword": "keyword",
    "s1": "S1",
    "refusal": "cloud refusal",
    "unknown": "unreadable detector",       # a keyword list or the local config could not be read: doubt counts
}
# Reason category -> its label. Why the Pro did not take the request (routing._why_category, the gate's outcomes).
REASONS = {
    "too_large": "too large",
    "unsupported": "image or unsupported content",
    "unavailable": "unavailable",
    "switched_off": "switched off",
    "on_battery": "on battery",
    "busy": "busy",
    "model_missing": "model not listed",
    "excluded": "model excluded by the client",
    "outward_effect": "outward effect",
    "s1_degraded": "S1 degraded",
    "not_admitted": "not admitted",
    "responses_wire": "Responses wire",
    "not_configured": "no sensitive model configured",
    "config_error": "local config error",
    "gate_token": "gate token mismatch",
    "node_error": "node error",
    "no_answer": "no usable local answer",
    "not_opening_turn": "not the opening turn",         # kept for rows written before refused_continuation
    "refused_continuation": "refusal after the opening turn",
    "tools_ran": "tools already ran",
}
FALLBACK_DETECTOR, FALLBACK_REASON = "unknown", "unavailable"
# A model name as the notice shows it; anything else is "a cloud model".
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}")
UNNAMED_MODEL = "a cloud model"
PREFIX = "⚠ CortadOS: this request was marked sensitive ("
TEMPLATE = (PREFIX + "{detector}) but the Pro couldn't take it ({reason}), so it was answered by {model} in the cloud.")
# A refusal of a turn after the opening one (reason REFUSED_CONTINUATION): the cloud model's refusal is the reply and
# the turn was not moved (only an opening turn is, and a turn with tool effects is never re-run), so the notice says
# exactly that instead of "answered by".
REFUSED_CONTINUATION = "refused_continuation"
REFUSED_PREFIX = "⚠ CortadOS: "
REFUSED_TEMPLATE = (REFUSED_PREFIX + "{model} in the cloud refused this turn (marked sensitive: {detector}). The turn "
                    "was not moved to the Pro ({reason}): only a conversation's opening turn can be moved, so the "
                    "refusal stands.")
# What is_notice recognises (tests and the verify scenario only): either template with any short parenthesised labels
# and any model token, so a notice written under an older vocabulary still matches. Anchored at the start; the model token holds no whitespace.
_MODEL_TOKEN = r"(?:\S{1,120}|" + re.escape(UNNAMED_MODEL) + r")"
_NOTICE_RE = re.compile(
    r"(?:" + re.escape(PREFIX) + r"[^()\n]{1,60}\) but the Pro couldn't take it \([^()\n]{1,80}\), so it "
    r"was answered by " + _MODEL_TOKEN + r" in the cloud\."
    r"|" + re.escape(REFUSED_PREFIX) + _MODEL_TOKEN + r" in the cloud refused this turn \(marked sensitive: "
    r"[^()\n]{1,60}\)\. The turn was not moved to the Pro \([^()\n]{1,80}\): only a conversation's opening turn "
    r"can be moved, so the refusal stands\.)")


def detector(value: Any) -> str:
    return value if isinstance(value, str) and value in DETECTORS else FALLBACK_DETECTOR


def reason(value: Any) -> str:
    return value if isinstance(value, str) and value in REASONS else FALLBACK_REASON


def model_label(model: Any) -> str:
    return model if isinstance(model, str) and _MODEL_RE.fullmatch(model) else UNNAMED_MODEL


def text(det: Any, why: Any, model: Any) -> str:
    """The notice's wording: fixed labels only. A refused continuation says the refusal stands and the turn stayed."""
    if reason(why) == REFUSED_CONTINUATION:
        return REFUSED_TEMPLATE.format(detector=DETECTORS[detector(det)], reason=REASONS[REFUSED_CONTINUATION],
                                       model=model_label(model))
    return TEMPLATE.format(detector=DETECTORS[detector(det)], reason=REASONS[reason(why)], model=model_label(model))


def block(det: Any, why: Any, model: Any) -> dict[str, Any]:
    return {"type": "text", "text": text(det, why, model)}


def mark(det: Any, why: Any) -> dict[str, str]:
    """The ROUTE_KEY value: categories only."""
    return {"detector": detector(det), "reason": reason(why)}


def opted_out(headers: Any) -> bool:
    try:
        raw = headers.get(OPT_OUT_HEADER) if headers is not None else None
    except Exception:
        return False
    return isinstance(raw, str) and raw.strip().lower() in OPT_OUT_VALUES


def record(mark_: Any, notice: str) -> dict[str, str]:
    """The route row's `sensitive_cloud` value: detector, reason, and whether the notice was shown or opted out."""
    m = mark_ if isinstance(mark_, dict) else {}
    return {"detector": detector(m.get("detector")), "reason": reason(m.get("reason")),
            "notice": notice if notice in (SHOWN, OPTED_OUT) else SHOWN}


def valid_record(value: Any) -> dict[str, str] | None:
    """A stored record re-validated for publishing (tasks / live views): the fixed vocabulary, else None."""
    if not isinstance(value, dict):
        return None
    det, why, notice = value.get("detector"), value.get("reason"), value.get("notice")
    if det not in DETECTORS or why not in REASONS or notice not in (SHOWN, OPTED_OUT):
        return None
    return {"detector": det, "reason": why, "notice": notice}


def is_notice(text_: Any) -> bool:
    """Whether `text_` is SHAPED like a notice. Tests and the verify scenario only: the provider never removes,
    relabels or matches anything by shape (#124 round 5)."""
    return isinstance(text_, str) and _NOTICE_RE.fullmatch(text_.strip()) is not None


def prepend(blocks: list, notice: dict[str, Any] | None) -> list:
    return [notice, *blocks] if notice else list(blocks)
