"""The outcome loop's provider backend: judge and generate calls go THROUGH THE ROUTER (Forest 2026-11-03).

Each stateless text call is a `cortados` provider /v1/messages request pinned `sub/<model>` (the judge: Claude Fable 5.1;
the generator: a Codex model), so the provider picks the subscription account (claude, claude#b, codex, ...) by
scarcity and refuses on its own spent gates. This is the teacher's (s1/label.py) backend, reused: same token file,
same `x-cortados-reveal` headroom answer, same retry classification.

On by default when CORTADOS_PROVIDER_URL is set; CORTADOS_OUTCOME_BACKEND=local forces the old local CLI path. With
the provider on, the quota door (`admit`) reads the provider's authenticated GET /resources: the cached usage reading
of every account the provider can route to (claude, claude#b, codex, ...), window by window (core.subscription_readings).
A lane is admitted only if one of its accounts has a FRESH reading (< quota.MAX_READING_AGE_S old, last try not
failed) whose weekly windows for the model (general + the model's own pool) are all >= 5 % left (the 95 % stop rule)
and none resets within the hour (STOP_HOURS_BEFORE_RESET). /resources unreachable or unreadable: hold (fail closed).
The per-call reveal ("· N% left", never sent for the codex lane) and a spent refusal are a supplementary signal: they
open a hold that only a /resources reading sampled AFTER it, showing the lane above the floor, clears; a hold never
ages into an admit. Known limit: the provider picks the account; the door admits when any account of the lane is above
the floor, and the provider's ranking prefers the least scarce one.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import common
import label
from outcome import quota, store

TOKEN_FILE_DEFAULT = Path.home() / ".config/cortados/provider-token"
# Same bound as the labeler's provider call: a judge prompt with a full ladder can take minutes on Fable.
TIMEOUT_S = 900
# Hold a lane once it reports less than this much left: the loop's 95 % stop rule (quota.STOP_PROJECTED_PCT).
MIN_LEFT_PCT = 100.0 - quota.STOP_PROJECTED_PCT
# GET /resources is re-read at most once a minute: the door runs before every band and batch, while the readings
# behind it change only on the sampler's ~20-minute cycle, so a minute's cache hides nothing and spares the provider.
RESOURCES_TTL_S = 60
# Output cap per judge/generate answer. The Messages API requires one; 16k tokens covers a judge verdict (a few hundred
# tokens) and a full generate batch of BATCH specs with small fixtures (~1-2k each) with room to spare. A generate batch
# that still hits it is split, not retried identically (generate.py); a single seed that hits it is refused.
MAX_TOKENS = 16000
# Head of every user message this loop sends through the provider: keeps its router rows out of S1 training data.
TAG_NOTE = f"\n\nThe user message starts with a bookkeeping tag line ({common.OUTCOME_MARKER} …); ignore it."
RUN_ID = f"s1-outcome-{time.strftime('%Y%m%d')}-{os.getpid()}"


class ProviderError(RuntimeError):
    pass


class TruncatedError(ProviderError):
    """The answer hit MAX_TOKENS: the same request would be truncated again."""


def tag(run_id: str = RUN_ID) -> str:
    return f"{common.OUTCOME_MARKER} run={run_id}"


def enabled() -> bool:
    return bool((os.environ.get("CORTADOS_PROVIDER_URL") or "").strip()) and \
        os.environ.get("CORTADOS_OUTCOME_BACKEND", "provider") != "local"


def config() -> tuple[str, str]:
    url = (os.environ.get("CORTADOS_PROVIDER_URL") or "").strip().rstrip("/")
    if not url:
        raise ProviderError("CORTADOS_PROVIDER_URL is not set")
    token = (os.environ.get("CORTADOS_PROVIDER_TOKEN") or "").strip()
    if not token:
        tf = Path(os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or TOKEN_FILE_DEFAULT).expanduser()
        try:
            token = tf.read_text().strip()
        except OSError as exc:
            raise ProviderError(f"provider token file {tf} unreadable: {type(exc).__name__}")
        if not token:
            raise ProviderError(f"provider token file {tf} is empty")
    return url, token


def lane_of(served_model: str | None, requested_lane: str) -> str:
    m = (served_model or "").lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith(("gpt", "codex")):
        return "codex"
    return requested_lane


def ask(lane: str, model: str, system: str, user: str, timeout_s: int = TIMEOUT_S, urlopen=urllib.request.urlopen) -> str:
    """One stateless call through the provider pinned to `sub/<model>`; records the revealed headroom of the lane that
    served it. Raises ProviderError on any non-200 or unreachable provider (the caller retries next pass)."""
    url, token = config()
    body = {"model": f"sub/{model}", "max_tokens": MAX_TOKENS, "system": (system or "") + TAG_NOTE,
            "messages": [{"role": "user", "content": f"{tag()}\n{user}"}]}
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": token,
               "x-cortados-run-id": RUN_ID, "x-cortados-reveal": "1"}
    req = urllib.request.Request(url + "/v1/messages", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout_s) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        msg = label._error_message(exc.read())
        if exc.code == 429 or label.QUOTA_RE.search(msg or ""):
            _record(lane, model, None, f"provider refused: HTTP {exc.code}: {msg}", spent=True)
        raise ProviderError(f"HTTP {exc.code}: {msg}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
        raise ProviderError(f"provider unreachable or bad answer: {type(exc).__name__}: {str(exc)[:160]}")
    served = out.get("model")
    left = label.left_pct(served)
    _record(lane_of(served, lane), model, left, served)
    if out.get("stop_reason") == "max_tokens":
        raise TruncatedError("answer truncated (stop_reason max_tokens)")
    return "".join(b.get("text", "") for b in out.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")


def _record(lane: str, model: str, left: float | None, served, spent: bool = False) -> None:
    store.append("provider_headroom", {"ts": time.time(), "lane": lane, "model": model, "left_pct": left,
                                       "served": served, "spent": spent})


_resources_cache: dict = {"at": 0.0, "snap": None, "local_at": 0.0}


def resources(now: float | None = None, urlopen=urllib.request.urlopen) -> tuple[dict | None, str]:
    """(snapshot, error) of the provider's GET /resources, cached RESOURCES_TTL_S. Any failure is an error, never data."""
    now = time.time() if now is None else now
    c = _resources_cache
    if c["snap"] is not None and 0 <= now - c["at"] < RESOURCES_TTL_S:
        return c["snap"], ""
    try:
        url, token = config()
        req = urllib.request.Request(url + "/resources", headers={"x-api-key": token, "authorization": f"Bearer {token}"})
        with urlopen(req, timeout=30) as r:
            snap = json.loads(r.read().decode("utf-8"))
        if not isinstance(snap, dict) or not isinstance(snap.get("readings"), dict) or "error" in snap["readings"]:
            raise ValueError("no per-window readings in /resources (provider older than this loop, or its reading failed)")
    except (ProviderError, urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
        return None, f"provider /resources unreadable: {type(exc).__name__}: {str(exc)[:160]}"
    c.update(at=now, snap=snap, local_at=now)
    return snap, ""


def _pool_of(model: str | None) -> str:
    return "fable" if "fable" in (model or "").lower() else "all"


def _account_verdict(reading: dict, model: str | None, age_s: float) -> tuple[bool, str]:
    if reading.get("failed"):
        return False, "last usage try failed"
    if not isinstance(reading.get("fetched_at"), (int, float)) or not 0 <= age_s < quota.MAX_READING_AGE_S:
        return False, "no fresh usage reading"
    weekly = [w for w in reading.get("windows") or [] if str(w.get("window") or "").startswith("week") and not w.get("stale")]
    general = [w for w in weekly if (w.get("pool") or "all") == "all"]
    if not general:
        return False, "no current weekly window"
    pool = _pool_of(model)
    own = [w for w in weekly if w.get("pool") == pool] if pool != "all" else []
    # The model's own pool panel is sometimes missing (Fable's per-model panel down): the general weekly windows still
    # bound it, as the loop's local door does since 3c32cd3 (owner decision); the reason line says so.
    relevant, note = general + own, ("" if pool == "all" or own else f" ({pool} panel missing: general weekly only)")
    worst = None
    for w in relevant:
        left = w.get("headroom_pct")
        left = (100.0 - float(w["used_pct"])) if left is None and isinstance(w.get("used_pct"), (int, float)) else left
        if not isinstance(left, (int, float)):
            return False, f"{w.get('window')}: no usage figure"
        if left < MIN_LEFT_PCT:
            return False, f"{w.get('window')}: {left:.0f}% left (< {MIN_LEFT_PCT:.0f}%)"
        reset = w.get("reset_epoch")
        if isinstance(reset, (int, float)) and (reset - (reading["fetched_at"] + age_s)) < quota.STOP_HOURS_BEFORE_RESET * 3600:
            return False, f"{w.get('window')}: resets within {quota.STOP_HOURS_BEFORE_RESET:.0f} h"
        worst = left if worst is None else min(worst, left)
    return True, f"{worst:.0f}% weekly left{note}"


def admit(lane: str, model: str | None = None, now: float | None = None, urlopen=urllib.request.urlopen) -> tuple[bool, str]:
    """The quota door on the provider backend (see the module docstring). Fails closed on every unknown."""
    now = time.time() if now is None else now
    snap, err = resources(now, urlopen)
    if snap is None:
        why = f"{lane}: held, {err}"
        print(f"  · quota door: {why}", flush=True)
        return False, why
    gen = snap.get("generated_at") if isinstance(snap.get("generated_at"), (int, float)) else None
    if gen is None:
        return False, f"{lane}: held, /resources has no generated_at"
    local_at = _resources_cache["local_at"] or now
    holds = [r for r in store.read("provider_headroom") if r.get("lane") == lane and
             (r.get("spent") or (isinstance(r.get("left_pct"), (int, float)) and r["left_pct"] < MIN_LEFT_PCT))]
    hold_ts = max((float(r.get("ts", 0)) for r in holds), default=None)
    reasons = []
    for acct, reading in sorted(snap["readings"].items()):
        if acct.split("#", 1)[0] != lane or not isinstance(reading, dict):
            continue
        age = gen - float(reading.get("fetched_at") or 0)
        ok, why = _account_verdict(reading, model, age)
        # The reading's sample time on THIS machine's clock (provider clock skew cancels: age is provider-relative).
        sampled_local = local_at - age
        if ok and hold_ts is not None and sampled_local <= hold_ts:
            ok, why = False, "held since a low/spent reveal; awaiting a usage reading sampled after it"
        reasons.append(f"{acct} {why}")
        if ok:
            return True, f"{lane}: {acct} {why}"
    if not reasons:
        reasons.append("no account of this lane in /resources")
    return False, f"{lane}: held: " + "; ".join(reasons)


def status() -> dict[str, str]:
    return {lane: admit(lane)[1] for lane in quota.LANES}


def ask_claude(model: str, system: str, user: str, timeout_s: int = TIMEOUT_S) -> str:
    """agents.ask_claude's signature, through the provider."""
    return ask("claude", model, system, user, timeout_s)


def ask_codex(model: str, prompt: str, timeout_s: int = TIMEOUT_S, effort: str | None = None) -> str:
    """agents.ask_codex's signature, through the provider (the effort hint is the provider's to choose)."""
    return ask("codex", model, "", prompt, timeout_s)
