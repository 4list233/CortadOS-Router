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
  history      the delivered form is canonical (#124 round 4): the session's history holds the reply exactly as the
               client received it (notice + model blocks), so a client echoing it matches exactly. The backend never
               saw the notice, so each injection records its accepted client forms as exact fingerprints (Forms:
               kept, merged into the next text, dropped), keyed by the model-only fingerprint in the lineage's
               Provenance, never by assistant index. Comparisons go through same_reply / same_history (explicit,
               directional); a rebuilt session remaps the client's history by verified matches (canonical). Nothing
               is ever stripped: a rebuilt transcript keeps a kept notice and labels it (labelled); model-authored
               text that looks like a notice is always preserved.

Escape hatch: OPT_OUT_HEADER: off on the request that would carry the notice suppresses the injected block only; the
route row and the log line are written all the same, marked `opted_out`.

Stdlib only, and no provider imports: anthropic_wire imports this module.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

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
    """Whether `text_` is SHAPED like a notice. Tests and the verify scenario only: nothing is ever removed or relabelled
    by shape (#124 round 4); the provider only recognises a notice by the exact forms it recorded (Provenance)."""
    return isinstance(text_, str) and _NOTICE_RE.fullmatch(text_.strip()) is not None


# Forms of one reply with an injected notice (#124 round 4). The delivered form is canonical: the session's history
# holds the fingerprint of the reply exactly as the client received it (notice block + model blocks). The backend never
# saw the notice, so a client may send the turn back in any of three forms, each recorded at injection time as an exact
# fingerprint: (a) the notice kept as its own block (= delivered), (b) merged into the model's first text with one of
# MERGE_SEPARATORS between them, (c) dropped (= the fingerprint of the model's own blocks, the record's key).
MERGE_SEPARATORS = ("\n\n", "\n", " ")
# What a rebuilt backend's transcript puts before a notice the client kept (form a or b), so the model knows the text
# that follows is the provider's: the client's text itself is never removed or changed.
REBUILD_LABEL = "[CortadOS provider notice: added to this reply by the endpoint, not written by the assistant]\n"


class Forms(NamedTuple):
    """One injection's accepted client forms (exact fingerprints, anthropic_wire.notice_forms builds it)."""
    model: str                  # (c) the model's own blocks: the record's content key
    delivered: str              # (a) the reply as sent: the session's history entry
    accepted: frozenset         # (a), (b) and (c)
    notice: str                 # the exact injected text (a rebuilt transcript labels it; nothing removes it)


class Provenance:
    """The injections of one conversation lineage, keyed by content (the model-only fingerprint), never by assistant
    index, so a compaction or rewrite that shifts indices cannot lose them. Copied to a rebuilt session with its
    history; memory-only, like that history (a restarted endpoint holds neither: the safe terminal no_writer)."""

    def __init__(self, records: Any = ()):
        self._by_model: dict[str, list[Forms]] = {}
        self._by_delivered: dict[str, Forms] = {}
        self._by_form: dict[str, list[Forms]] = {}
        for r in records:
            self.add(r)

    def add(self, rec: Forms) -> None:
        if rec in self._by_model.get(rec.model, ()):
            return
        self._by_model.setdefault(rec.model, []).append(rec)
        self._by_delivered.setdefault(rec.delivered, rec)
        for f in rec.accepted:
            self._by_form.setdefault(f, []).append(rec)

    def records(self) -> list[Forms]:
        return [r for rs in self._by_model.values() for r in rs]

    def copy(self) -> "Provenance":
        return Provenance(self.records())

    def __len__(self) -> int:
        return len(self._by_delivered)

    def delivered(self, fp: Any) -> Forms | None:
        """The record whose delivered form (a session history entry) is `fp`, else None."""
        return self._by_delivered.get(fp) if isinstance(fp, str) else None

    def accepting(self, fp: Any) -> list[Forms]:
        """Every record that accepts `fp` as one of its forms."""
        return list(self._by_form.get(fp, ())) if isinstance(fp, str) else []


def same_reply(prov: Provenance | None, mine: Any, theirs: Any) -> bool:
    """Whether the client's entry `theirs` is the session's entry `mine`: equal, or `mine` is the delivered form of a
    recorded injection and `theirs` one of its accepted forms. Directional (the session's side first), explicit, and
    plain str equality underneath: nothing about a fingerprint's own == is overloaded."""
    if mine == theirs:
        return True
    rec = prov.delivered(mine) if prov is not None else None
    return rec is not None and theirs in rec.accepted


def same_history(prov: Provenance | None, mine: Any, theirs: Any) -> bool:
    """same_reply over two whole histories (lists of fingerprints, the session's first)."""
    mine, theirs = list(mine or []), list(theirs or [])
    return len(mine) == len(theirs) and all(same_reply(prov, m, t) for m, t in zip(mine, theirs))


def holds(prov: Provenance | None, mine: Any, theirs: Any) -> bool:
    """Whether some entry of the session's history `mine` is the client's entry `theirs` (same_reply)."""
    return any(same_reply(prov, m, theirs) for m in (mine or []))


def canonical(prov: Provenance | None, theirs: Any) -> list:
    """A client's history as a rebuilt session holds it: each entry that is a verified accepted form of exactly one
    delivered reply (every record accepting it agrees) is remapped to that delivered form, so later continuations in any
    form keep matching; every other entry is kept as sent."""
    out = []
    for t in list(theirs or []):
        if prov is not None and prov.delivered(t) is None:
            ds = {r.delivered for r in prov.accepting(t)}
            if len(ds) == 1:
                t = next(iter(ds))
        out.append(t)
    return out


def labelled(blocks: list, rec: Forms | None) -> list:
    """An assistant message's blocks for a rebuilt transcript. `rec`: the injection whose kept or merged form this
    message verifiably is (its fingerprint matched; None otherwise). Then the first text block, if it starts with that
    exact notice, gets REBUILD_LABEL in front; nothing is ever removed, and text that only looks like a notice (no
    verified record, or not leading) is the model's and stays exactly as it is."""
    if rec is None:
        return list(blocks)
    out = list(blocks)
    for i, b in enumerate(out):
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.lstrip().startswith(rec.notice):
                out[i] = {**b, "text": REBUILD_LABEL + t}
            break                                       # the first text block only
    return out


def prepend(blocks: list, notice: dict[str, Any] | None) -> list:
    return [notice, *blocks] if notice else list(blocks)
