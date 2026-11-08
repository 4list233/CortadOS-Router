"""Deterministic shadow pruning rules (phase 1): a KeepDecision for every past tool event of a session.

SHADOW ONLY: decisions are appended to $CORTADOS_HOME/compaction/decisions.jsonl with the chars/tokens they WOULD have
saved; nothing here edits what any backend sees. Thresholds live in tables/compaction-prune.json, each with its why.

Rules, first match wins:
  pinned          events of the first message and of the last K messages: keep call and result
  error           an error result is kept (the model must remember what failed)
  side_effect     Edit/Write/..., non-read-only Bash, MCP tools that send/post/create: keep call (and result unless
                  a later rule says the result is a re-runnable read, which a side effect never is)
  superseded      a re-runnable read later repeated with the same path / command: keep the call, drop the result
  small           result under N chars: keep (dropping it saves nothing)
  not_rerunnable  a tool whose result cannot be fetched again: keep
  rerunnable_read Read/Grep/Glob/LS/WebFetch/WebSearch/read-only Bash outside the pin window: drop, keep a head of H
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from cortados_router.compaction import store
from cortados_router.compaction.events import KeepDecision, Session, ToolEvent

logger = logging.getLogger(__name__)

TABLE_PATH = Path(__file__).resolve().parents[2] / "tables" / "compaction-prune.json"
_CHAIN = re.compile(r"[;&|<>`]|\$\(")      # chains, pipes, redirections, substitutions: not obviously read-only


@lru_cache(maxsize=4)
def load_table(path: str = str(TABLE_PATH)) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v["value"] for k, v in raw.items() if isinstance(v, dict) and "value" in v}


def _bash_command(ev: ToolEvent) -> Optional[str]:
    if ev.name in ("Bash", "shell", "exec_command", "local_shell"):
        cmd = ev.input.get("command", ev.input.get("cmd"))
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return str(cmd or "").strip()
    return None


def readonly_bash(cmd: str, table: dict[str, Any]) -> bool:
    if not cmd or _CHAIN.search(cmd):
        return False
    return any(cmd == p or cmd.startswith(p + " ") for p in table["readonly_bash_prefixes"])


def is_side_effect(ev: ToolEvent, table: dict[str, Any]) -> bool:
    if ev.name in table["side_effect_tools"]:
        return True
    cmd = _bash_command(ev)
    if cmd is not None:
        return not readonly_bash(cmd, table)
    if ev.name.startswith("mcp__"):
        words = re.split(r"[_\-]+", ev.name.split("__")[-1].lower())
        return any(w in table["mcp_side_effect_words"] for w in words)
    return False


def is_rerunnable(ev: ToolEvent, table: dict[str, Any]) -> bool:
    if ev.name in table["rerunnable_tools"]:
        return True
    cmd = _bash_command(ev)
    return cmd is not None and readonly_bash(cmd, table)


def read_key(ev: ToolEvent) -> str:
    """What a later identical read would repeat: the path for file reads, the command for shell, else the input."""
    cmd = _bash_command(ev)
    if cmd is not None:
        return "cmd:" + cmd
    for k in ("file_path", "path", "notebook_path", "url"):
        if k in ev.input and ev.name in ("Read", "NotebookRead", "LS", "WebFetch"):
            return f"path:{ev.input[k]}"
    return ev.name + ":" + json.dumps(ev.input, sort_keys=True, default=str)


def decide(session: Session, *, upto_message: Optional[int] = None,
           table: Optional[dict[str, Any]] = None) -> list[tuple[KeepDecision, int]]:
    """(decision, chars it would save) for every tool event issued at or before message `upto_message` (default:
    the whole session)."""
    t = table or load_table()
    n_msgs = len(session.messages) if upto_message is None else upto_message + 1
    events = [e for e in session.events if e.message_index < n_msgs]
    pin_from = max(0, n_msgs - int(t["pin_last_messages"]))
    head = int(t["dropped_head_chars"])
    later: dict[str, int] = {}
    for e in events:
        if is_rerunnable(e, t):
            later[read_key(e)] = e.index                 # last index of each read key
    out = []
    for e in events:
        def mk(keep_call: bool, keep_result: bool, rule: str, reason: str) -> tuple[KeepDecision, int]:
            saved = 0 if keep_result else max(0, e.result_chars - head)
            return (KeepDecision(session=session.session, event_index=e.index, keep_call=keep_call,
                                 keep_result=keep_result, decided_by="rule:" + rule, reason=reason), saved)
        if e.message_index == 0 or e.message_index >= pin_from:
            out.append(mk(True, True, "pinned", f"in the first message or the last {t['pin_last_messages']} messages"))
        elif e.is_error:
            out.append(mk(True, True, "error", "error results are kept"))
        elif is_side_effect(e, t):
            out.append(mk(True, True, "side_effect", f"{e.name} changed state"))
        elif is_rerunnable(e, t) and later.get(read_key(e), e.index) > e.index:
            out.append(mk(True, False, "superseded", "a later call repeats this read"))
        elif e.result_chars < int(t["small_result_chars"]):
            out.append(mk(True, True, "small", f"result under {t['small_result_chars']} chars"))
        elif not is_rerunnable(e, t):
            out.append(mk(True, True, "not_rerunnable", f"{e.name} cannot be re-run for the same result"))
        else:
            out.append(mk(True, False, "rerunnable_read", f"re-runnable read outside the pin window; keep a {head}-char head"))
    return out


def decisions_path() -> Path:
    return store.root() / "decisions.jsonl"


def shadow_decide(session_path, *, now: Optional[float] = None) -> list[KeepDecision]:
    """Decide for every past tool event of the stored session and append the decisions (0600) with the chars and
    estimated tokens they WOULD have saved. Returns the decisions."""
    sess = store.read_session(session_path)
    t = load_table()
    pairs = decide(sess, table=t)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    cpt = float(t["chars_per_token"])
    lines = "".join(json.dumps({**dataclasses.asdict(d), "ts": ts, "saved_chars": saved,
                                "saved_tokens_est": int(saved / cpt), "mode": "shadow"}) + "\n"
                    for d, saved in pairs)
    if lines:
        store.ensure_dir(store.root())
        store.append_line(decisions_path(), lines)
    return [d for d, _ in pairs]


def shadow_decide_async(conversation_id: str) -> Optional[threading.Thread]:
    """Run shadow_decide for a closed conversation in a background thread, off the request path. No-op when the
    store is off or the conversation left no file. A failure is logged, never raised into the caller."""
    if not store.enabled():
        return None
    path = store.session_path(conversation_id)
    if not path.exists():
        return None

    def run():
        try:
            shadow_decide(path)
        except Exception as exc:   # background shadow work: report it, never break the provider
            logger.warning("compaction shadow decisions for %s failed: %s: %s", path.name, type(exc).__name__, exc)

    th = threading.Thread(target=run, daemon=True, name="cortados-compaction-shadow")
    th.start()
    return th
