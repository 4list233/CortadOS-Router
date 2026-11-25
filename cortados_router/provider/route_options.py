"""GET /v1/cortados/route-options?band=R3 (owner token or a dashboard session): the models a route pick can name, per
band, with each account and lane that serves them and how much of its quota is left, from CACHED readings only
(docs/ORCHESTRATOR-ROUTE-PICKER.md §2, step 4). The terminal launcher's picker (cortados_router/session.py) and the
dashboard's Live tab read it.

Never opens a vendor CLI: quota comes from core.lane_health(refresh=False) (the scarcity cache; a stale reading is
"unread", never a number), the API credit lane from api_credits.state (its ledger file), the local section from the
nodes' gates (the same call GET /v1/cortados/local makes, passed in by the server). Metadata only: model ids, display
names, indexes, lane and account names, states, percentages, reset times. Never a config path, a login, a token, a
prompt or any request-derived text. Stdlib only.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from cortados_router.provider import local_lane, routing, usage

logger = logging.getLogger("cortados.provider.route_options")

PATH = "/v1/cortados/route-options"
VERSION = 1
CLOUD_BANDS = ("R0", "R1", "R2", "R3", "R4")
BANDS = CLOUD_BANDS + ("local",)
# Shown on every local row until step 8 builds the `orchestrator` boundary profile (E8, owner 2026-11-24): a local
# pick is parsed and validated, and refused by the provider with routing.LOCAL_PICKS_REFUSED.
LOCAL_NOTE = "local orchestrator: coming in the next step (plan-and-delegate profile)"
# The lanes a model may live on that this endpoint never runs (routing.SERVED_PROVIDERS): said once, by count, so
# "where is Sonnet 5.5?" has an answer without listing every reach row.
NOT_SERVED_WHY = ("only on lanes this endpoint never runs (Cursor, Antigravity, local rows): the provider serves "
                  "claude-cli and codex-cli")


class BandError(ValueError):
    """?band= is missing or not one of BANDS (a 400)."""


def parse_band(query: dict[str, list[str]]) -> str:
    got = query.get("band")
    if not got or len(got) != 1:
        raise BandError("band must be given once: one of " + ", ".join(BANDS))
    b = got[0].strip()
    b = b.upper() if b.upper() in CLOUD_BANDS else b.casefold()
    if b not in BANDS:
        raise BandError("band must be one of " + ", ".join(BANDS))
    return b


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _route_state(h: dict[str, Any]) -> dict[str, Any]:
    """One route's quota state from core.candidate_health: ok / spent / refusing / unread, the tightest window's
    percent left and reset time. A reading for another login or a stale one (no tightest window) is `unread`."""
    tight = h.get("tightest") if isinstance(h.get("tightest"), dict) else {}
    used = _num(tight.get("used_pct"))
    why = str(h.get("why") or "")
    if why.startswith("stale cross-account"):
        state = "unread"
    elif not h.get("ok", True):
        state = "spent" if used is not None else "refusing"
    else:
        state = "ok" if used is not None else "unread"
    out = {"state": state, "left_pct": None, "window": None, "resets_at": None}
    if used is not None and state != "unread":
        out["left_pct"] = round(max(0.0, 100.0 - used), 1)
        out["window"] = str(tight.get("window"))[:40] if tight.get("window") else None
        epoch = _num(tight.get("reset_epoch"))
        out["resets_at"] = (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch
                            else (str(tight.get("resets_at"))[:40] if tight.get("resets_at") else None))
    return out


def _shown_id(acct: dict[str, Any] | None, index: int) -> str:
    """The account's id as this view may show it: the configured id when it passes the usage view's string rule
    (usage._label: no '@', '/', address, …) and is short, else its letter by position (A, B, …). Reason: an account id
    is free text in accounts.json and may be a login email (`review@example.invalid`); no email leaves this view."""
    a = (acct or {}).get("account")
    if isinstance(a, str) and a and len(a) <= 24 and "#" not in a and usage._label(a) == a:
        return a
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def _account_label(acct: dict[str, Any] | None, index: int, many: bool) -> str | None:
    if not acct or not many:
        return None
    a = _shown_id(acct, index)
    return a.upper() if len(a) <= 2 else a


def _shown_key(key: str | None, acct: dict[str, Any] | None, index: int) -> str | None:
    """A provider or lane key (`claude-cli#<id>`, `claude#<id>`) with its account suffix as _shown_id shows it."""
    if not isinstance(key, str) or "#" not in key:
        return key
    return key.split("#", 1)[0] + "#" + _shown_id(acct, index)


def cloud_section(core, band: str, *, now: float | None = None) -> dict[str, Any]:
    """The cloud models for `band`: every served model whose general index clears the band's reference (R0: every
    served model), grouped by model, each with its routes (every account and lane) and their cached state."""
    now = time.time() if now is None else now
    ms = core.model_scores() or {}
    refs = ms.get("reference_index") or {}
    ref = {} if band == "R0" else dict(refs.get(band) or {})
    ref_general = 0.0 if band == "R0" else _num(ref.get("general"))
    if ref_general is None:
        raise ValueError(f"model scores carry no {band} reference index")
    sel = (core.table().get("selection") or {})
    face = _num(sel.get("interactive_min_general_index"))
    served = routing.served_routes(core)                  # the provider's own allowlist (claude-cli, codex-cli)
    accounts = core._vendor_accounts("claude", "claude", "claude-cli")
    acct_of = {a["provider"]: (i, a) for i, a in enumerate(accounts)}
    health = core.lane_health(refresh=False)               # cached readings only: never opens a vendor CLI
    from cortados_router import api_credits
    try:
        api = api_credits.state(now)
    except Exception as exc:                               # api_credits.state never raises; belt and braces
        logger.warning("route options: api credit state unreadable: %s", type(exc).__name__)
        api = {"on": False}
    rows = [x for m in ms.get("models") or [] for x in m.get("reach") or []]
    rows += [x for b in core.table()["candidates"].values() for x in b]
    row_of: dict[tuple[str, str], dict[str, Any]] = {}
    for x in rows:
        row_of.setdefault((routing.CLONED_PROVIDERS.get(x["provider"], x["provider"]), x["model"]), x)
    by_key: dict[str, list[tuple[str, str]]] = {}
    for p, m in served:
        by_key.setdefault(routing.model_key(m), []).append((p, m))
    models, below, not_served = [], [], []
    for fam in ms.get("models") or []:
        idx = fam.get("index") or {}
        general = _num(idx.get("general"))
        pairs = sorted({pm for r in fam.get("reach") or [] for pm in by_key.get(routing.model_key(r.get("model")), [])})
        if not pairs:
            not_served.append(str(fam.get("family"))[:64])
            continue
        ids = sorted({m for _, m in pairs})
        mid = ids[0]
        if general is None or general < ref_general:
            below.append({"id": mid, "name": routing.readable_model_name(mid), "general": general})
            continue
        routes = []
        for p, m in pairs:
            base = p.split("#", 1)[0]
            i, acct = acct_of.get(p, (0, None)) if base == "claude-cli" else (0, None)
            c = routing._pair_lane(core, p, m, row_of, {k: a for k, (_, a) in acct_of.items()})
            st = _route_state(core.candidate_health(c, health)) if c else {"state": "unread", "left_pct": None,
                                                                           "window": None, "resets_at": None}
            routes.append({"provider": _shown_key(p, acct, i), "lane": _shown_key((c or {}).get("lane"), acct, i),
                           "account": _account_label(acct, i, len(accounts) > 1),
                           "reserve": bool((acct or {}).get("reserve")), **st})
        if any(p.split("#", 1)[0] == "claude-cli" for p, _ in pairs) and api.get("on"):
            # E4: a selected Claude model may run on the API credit lane when the lane would be offered anyway
            routes.append({"provider": routing.API_PROVIDER, "lane": routing.API_PROVIDER, "account": None,
                           "reserve": False, "state": "ok", "left_pct": None, "window": None, "resets_at": None,
                           "left_usd": round(float(api.get("headroom_usd") or 0.0), 2)})
        short = sorted(u for u, v in ref.items() if u != "general" and _num(v) is not None
                       and _num(idx.get(u)) is not None and _num(idx.get(u)) < _num(v))
        models.append({"id": mid, "ids": ids, "name": routing.readable_model_name(mid),
                       "index": {k: _num(v) for k, v in idx.items() if _num(v) is not None},
                       "ceiling": bool(fam.get("ceiling")), "short_of": short,
                       "below_face_floor": face is not None and general < face, "routes": routes})
    models.sort(key=lambda x: (-(x["index"].get("general") or 0.0), x["id"]))
    below.sort(key=lambda x: (-(x["general"] or 0.0), x["id"]))
    return {"reference": {"general": ref_general, **{k: _num(v) for k, v in ref.items() if k != "general"}},
            "face_floor": face, "models": models,
            # served models under the band's reference: a pick may still name one (it escalates when a turn needs
            # more, E3), so the launcher accepts them by name; they are not offered as the band's own list
            "below_band": below,
            "not_served": {"count": len(not_served), "models": sorted(not_served), "why": NOT_SERVED_WHY},
            "api_credit": {"on": bool(api.get("on"))}}


def _local_state(row: dict[str, Any] | None) -> str:
    if row is None:
        return "not reported"
    if not row.get("reachable"):
        return "node unreachable"
    if row.get("switch") == "off":
        return "switch off"
    if row.get("power") == "battery":
        return "on battery"
    if row.get("allowed") is False:
        return "not allowed now"
    return "available" if row.get("allowed") is True else "unknown"


def local_section(core, local_view: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
    """Every local pin (local_lane.pins) with its node, alias, tag, context length, published proxy index and the node's
    state as its gate reports it (the GET /v1/cortados/local rows). A clashing or broken local config is said as such
    (node named, never the file path), never guessed around."""
    out: dict[str, Any] = {"status": "ok", "error": None, "note": LOCAL_NOTE, "enabled": False, "models": []}
    try:
        pins = routing.local_pins(core)
    except local_lane.LocalConfigError as exc:
        logger.warning("route options: local config: %s", exc)
        out.update(status="misconfigured", error=f"local node {getattr(exc, 'node', None) or '?'} is misconfigured on "
                                                 "the provider (see provider log)")
        return out
    if not pins:
        out["status"] = "none configured"
        return out
    rows: dict[str, dict[str, Any]] = {}
    if local_view is not None:
        try:
            rows = {r.get("node"): r for r in (local_view() or {}).get("nodes") or [] if isinstance(r, dict)}
        except Exception as exc:                          # the nodes' state is said unknown, the list still shown
            logger.warning("route options: local view failed: %s", type(exc).__name__)
            out.update(status="error", error="local node state unavailable (see provider log)")
    proxies = {}
    for lm in ((core.model_scores() or {}).get("local_models") or []):
        for r in lm.get("reach") or []:
            proxies.setdefault(str(r.get("model")).casefold(), {})[lm.get("thinking") or "off"] = (
                _num((lm.get("index") or {}).get("general")))
    for node, m in pins:
        st = rows.get(node["node"])
        loaded = (st or {}).get("models_loaded") or []
        out["models"].append({
            "id": local_lane.pin_id(m), "tag": m["id"], "name": m.get("name"), "node": node["node"],
            "node_label": node.get("label"), "context_length": m.get("context_length"),
            "index": proxies.get(m["id"].casefold()) or proxies.get((m.get("alias") or "").casefold()),
            "state": _local_state(st), "loaded": m["id"] in loaded})
    return out


def collect(core, band: str, *, local_view: Callable[[], dict[str, Any]] | None = None,
            now: float | None = None) -> dict[str, Any]:
    """The view for `band` (parse_band's value): a cloud band carries its models, `local` the local section. Kept apart
    on purpose: the local section asks every node's gate (up to local_switch.GATE_TIMEOUT_S each), and the launcher
    waits only 2 s for a cloud band's list. A failure of a section is that section's error, never an empty success."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"version": VERSION, "band": band, "as_of": round(now, 3),
                           "readings": "cached only (no vendor CLI opened)", "errors": []}
    if band == "local":
        out["local"] = local_section(core, local_view)
        return out
    try:
        out["cloud"] = cloud_section(core, band, now=now)
    except Exception as exc:
        logger.warning("route options: cloud section failed: %s", type(exc).__name__)
        out["cloud"] = None
        out["errors"].append({"source": "cloud", "error": type(exc).__name__})
    return out
