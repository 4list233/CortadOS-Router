"""The outcome loop's provider backend: judge and generate calls go THROUGH THE ROUTER (Forest 2026-11-03).

Each stateless text call is a `cortados` provider /v1/messages request pinned `sub/<model>` (the judge: Claude Fable 5.1;
the generator: a Codex model), so the provider picks the subscription account (claude, claude#b, codex, ...) by
scarcity and refuses on its own spent gates. This is the teacher's (s1/label.py) backend, reused: same token file,
same `x-cortados-reveal` headroom answer, same retry classification.

On by default when CORTADOS_PROVIDER_URL is set; CORTADOS_OUTCOME_BACKEND=local forces the old local CLI path. With
the provider on, the quota door for these calls reads the provider-revealed "% left" of the lane that served the last
call instead of this machine's usage reading (which on the Studio is missing for Codex and lacks Claude's dropped
5-hour panel). The 95 % stop rule is kept as "hold below 5 % left". The last-hour-before-reset rule cannot be applied
here (the reveal carries no reset time); the provider's own gates cover the spent case.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import label
from outcome import quota, store

TOKEN_FILE_DEFAULT = Path.home() / ".config/cortados/provider-token"
# Same bound as the labeler's provider call: a judge prompt with a full ladder can take minutes on Fable.
TIMEOUT_S = 900
# Hold a lane once it reports less than this much left: the loop's 95 % stop rule (quota.STOP_PROJECTED_PCT).
MIN_LEFT_PCT = 100.0 - quota.STOP_PROJECTED_PCT
# A headroom reading older than the quota module's reading age is not trusted to HOLD forever: past it, the next call
# re-learns the figure (the provider still refuses a spent account itself).
MAX_HEADROOM_AGE_S = quota.MAX_READING_AGE_S
MAX_TOKENS = 16000
RUN_ID = f"s1-outcome-{time.strftime('%Y%m%d')}-{os.getpid()}"


class ProviderError(RuntimeError):
    pass


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
    body = {"model": f"sub/{model}", "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": user}]}
    if system:
        body["system"] = system
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
        raise ProviderError("answer truncated (stop_reason max_tokens)")
    return "".join(b.get("text", "") for b in out.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")


def _record(lane: str, model: str, left: float | None, served, spent: bool = False) -> None:
    store.append("provider_headroom", {"ts": time.time(), "lane": lane, "model": model, "left_pct": left,
                                       "served": served, "spent": spent})


def admit(lane: str, model: str | None = None, now: float | None = None) -> tuple[bool, str]:
    """The quota door on the provider backend: hold a lane whose last revealed headroom (fresh) is below MIN_LEFT_PCT,
    was a spent refusal, or was not revealed at all (unknown headroom cannot honour the stop rule; set
    CORTADOS_OUTCOME_ALLOW_UNREVEALED=1 to continue on the provider's own spent gate). No reading yet: admit — the
    first call is how the headroom is learned, and the provider refuses a spent account itself."""
    now = time.time() if now is None else now
    rows = [r for r in store.read("provider_headroom") if r.get("lane") == lane and now - r.get("ts", 0) < MAX_HEADROOM_AGE_S]
    if not rows:
        return True, f"{lane}: no fresh provider headroom reading; the provider gates this call"
    last = rows[-1]
    if last.get("spent"):
        return False, f"{lane}: provider refused the last call as spent ({str(last.get('served'))[:120]})"
    left = last.get("left_pct")
    if left is None:
        if os.environ.get("CORTADOS_OUTCOME_ALLOW_UNREVEALED") == "1":
            return True, f"{lane}: headroom not revealed; continuing on the provider's own gate"
        return False, f"{lane}: provider did not reveal % left for {last.get('served')}; stop rule cannot be honoured"
    if left < MIN_LEFT_PCT:
        return False, f"{lane}: {left:.0f}% left on {last.get('served')} (< {MIN_LEFT_PCT:.0f}%)"
    return True, f"{lane}: {left:.0f}% left on {last.get('served')}"


def status() -> dict[str, str]:
    return {lane: admit(lane)[1] for lane in quota.LANES}


def ask_claude(model: str, system: str, user: str, timeout_s: int = TIMEOUT_S) -> str:
    """agents.ask_claude's signature, through the provider."""
    return ask("claude", model, system, user, timeout_s)


def ask_codex(model: str, prompt: str, timeout_s: int = TIMEOUT_S, effort: str | None = None) -> str:
    """agents.ask_codex's signature, through the provider (the effort hint is the provider's to choose)."""
    return ask("codex", model, "", prompt, timeout_s)
