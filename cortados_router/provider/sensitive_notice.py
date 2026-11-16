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
  history      provenance, never shape (#124 round 3): the session records each notice it injects (Injected: its
               exact text, at its turn, block index 0) in its own history, whose entry is the fingerprint of the
               model's own blocks. A client that sends the reply back as received, with the notice merged into the
               next text block, or without it, matches that entry; a rebuilt backend's transcript leaves out exactly
               that injected text (without_injected). Text the model wrote that only looks like a notice is never
               removed, and fingerprints are taken over everything the client sent (anthropic_wire.client_fingerprint).

Escape hatch: OPT_OUT_HEADER: off on the request that would carry the notice suppresses the injected block only; the
route row and the log line are written all the same, marked `opted_out`.

Stdlib only, and no provider imports: anthropic_wire imports this module.
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
# What split_leading recognises as a candidate: either template with any short parenthesised labels and any model token, so a notice
# written under an older vocabulary still matches. Anchored at the start; the model token holds no whitespace.
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
    """Whether `text_` is SHAPED like a notice. Shape alone never removes anything: only an injection the session
    recorded (Injected) is ever taken out of a fingerprint or a rebuilt transcript."""
    return isinstance(text_, str) and _NOTICE_RE.fullmatch(text_.strip()) is not None


def split_leading(joined: str) -> tuple[str, str] | None:
    """(notice-shaped prefix, the rest) of an assistant message's joined text, or None. A CANDIDATE only: a client's
    Fingerprint carries it, and only a session's Injected entry with exactly that text, at that turn, accepts it."""
    s = joined.lstrip() if isinstance(joined, str) else ""
    m = _NOTICE_RE.match(s)
    return (m.group(0), s[m.end():]) if m else None


class Fingerprint(str):
    """One assistant message of a client's history, as anthropic_wire.client_fingerprint gives it: the str value is
    the fingerprint of the message exactly as sent (nothing removed), and `leading` is (notice-shaped prefix,
    fingerprint of the message without it) when its text starts with one, else None. Equality is the str's: only a
    session's Injected entry looks at `leading`."""
    leading: tuple[str, str] | None = None           # (str subclasses take no __slots__)

    def __new__(cls, value: str, leading: tuple[str, str] | None = None):
        o = super().__new__(cls, value)
        o.leading = leading
        return o


class Injected(Fingerprint):
    """Provenance (#124 round 3): a session's history entry for a reply this endpoint sent with an injected notice.
    The str value is the fingerprint of the model's own blocks; `notice` is the exact injected text, and `index` its
    block index in that reply (always 0: the notice leads). It is recorded in the session's own history, at the turn
    it was injected in, so it stays with that turn through every copy of the history.

    It equals a client's entry for the same turn when the client sent the reply without the notice (the str value) or
    with a leading prefix that is exactly the injected text, as its own block or merged into the next text
    (Fingerprint.leading). Nothing else is accepted: model-authored text that only looks like a notice stays in the
    fingerprint. A subclass of Fingerprint, so Python asks it first whichever side of == it is on. Its hash is the
    str's, so an Injected must never be a set member or dict key next to client fingerprints (history lists are
    compared with ==, never hashed)."""
    notice: str = ""
    index: int = 0

    def __new__(cls, value: str, notice: str, index: int = 0):
        o = super().__new__(cls, value)
        o.notice, o.index = notice, index
        return o

    def accepts(self, other: Any) -> bool:
        if not isinstance(other, str):
            return False
        if str.__eq__(self, other):
            return True
        lead = getattr(other, "leading", None)
        return bool(lead) and self.index == 0 and lead[0] == self.notice and str.__eq__(self, lead[1])

    def __eq__(self, other: Any):
        if not isinstance(other, str):
            return NotImplemented
        if isinstance(other, Injected):
            return str.__eq__(self, other)
        return self.accepts(other)

    def __ne__(self, other: Any):
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    __hash__ = str.__hash__


def injected(history: Any) -> dict[int, str]:
    """{assistant message index: exact injected notice text} of a session's history (the Injected entries)."""
    return {i: h.notice for i, h in enumerate(history or []) if isinstance(h, Injected)}


def carry(old: Any, new: list) -> list:
    """A rebuilt session's history (`new`, the client's entries) keeping the old session's Injected entries where the
    client's entry at the same index matches one, so the provenance survives a rebuild."""
    old = list(old or [])
    return [o if i < len(old) and isinstance(o := old[i], Injected) and o == c else c for i, c in enumerate(new)]


def without_injected(blocks: list, notice: str | None) -> list:
    """An assistant message's blocks without the notice this endpoint injected into it (`notice`, its exact text; None:
    nothing was injected, nothing is removed). Only the leading text (the first text block, with no text before it)
    is touched: removed when it is exactly the notice, else stripped of a prefix that is exactly the notice (a client
    that merged it into the model's text). Text that only looks like a notice, anywhere, is the model's and stays."""
    if not notice:
        return list(blocks)
    out = list(blocks)
    for i, b in enumerate(out):
        if not (isinstance(b, dict) and b.get("type") == "text"):
            continue
        t = b.get("text")
        if isinstance(t, str):
            if t.strip() == notice:
                del out[i]
            elif t.lstrip().startswith(notice):
                out[i] = {**b, "text": t.lstrip()[len(notice):].lstrip()}
        break                                           # the first text block only
    return out


def prepend(blocks: list, notice: dict[str, Any] | None) -> list:
    return [notice, *blocks] if notice else list(blocks)
