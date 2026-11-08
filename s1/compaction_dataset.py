"""Compaction keep-head SFT data: labelled tool events → mlx-lm chat-format files (same {"messages": [...]} shape as
build_sft.py). Two questions per event, one example each:
  call   — "Does it still matter that this call happened?"                            (label needed_call)
  result — "Must this result stay verbatim, because re-running it would not do?"     (label needed_result)

The prompt is a compact state built ONLY from what existed when the call returned (no label leakage):
  goal    the last GOAL_PROMPTS user prompts up to the issuing message, each cut to GOAL_CHARS
  call    tool name + JSON input, cut to CALL_CHARS
  result  result head RESULT_HEAD chars + tail RESULT_TAIL chars (the middle elided, size stated), plus error/interrupt
  window  the WINDOW messages up to and including the issuing message, each cut to WINDOW_CHARS
Sizes keep a prompt near 2.5k chars (~700 tokens), well inside the routing trainer's --max-seq-length 1536.

Splits are BY SESSION (sha1 of seed + session id): events of one session are strongly correlated, so an event-level
split would leak a session's habits into test. "unknown" labels (too little future) are never written.

Class balance: kept results are the minority, and dropping a needed result is the expensive mistake, so in TRAIN only
the minority class of each head is repeated up to parity, capped at MAX_REPS (more repetition overfits a few sessions).
valid and test keep the natural mix so calibration and eval measure the real distribution.

CLI: python3 s1/compaction_dataset.py --labels labels.jsonl --sessions sessions.jsonl --out DIR
(sessions.jsonl: one Session per line as written by session_to_dict). Writes train/valid/test.jsonl, test-rows.jsonl
(event metadata + labels + prompt for eval) and stats.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cortados_router.compaction.events import Message, Session, ToolEvent  # noqa: E402

GOAL_PROMPTS, GOAL_CHARS = 3, 300
CALL_CHARS = 600
RESULT_HEAD, RESULT_TAIL = 800, 400
WINDOW, WINDOW_CHARS = 4, 250
SEED = "compaction-v1"
SPLIT = (0.8, 0.1, 0.1)   # train / valid / test by session
MAX_REPS = 4

SYSTEM = ("You decide what an agent's context keeps when it is compacted. You see the task goal, one past tool call "
          "with its result, and the messages around it. Answer with exactly one word: yes or no.")
QUESTIONS = {
    "call": "Does it still matter that this call happened?",
    "result": "Must this result stay verbatim, because re-running it would not do?",
}
LABEL_KEY = {"call": "needed_call", "result": "needed_result"}


def cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def head_tail(text: str) -> str:
    if len(text) <= RESULT_HEAD + RESULT_TAIL:
        return text
    return f"{text[:RESULT_HEAD]}\n[… {len(text) - RESULT_HEAD - RESULT_TAIL} chars elided …]\n{text[-RESULT_TAIL:]}"


def state_text(session: Session, event: ToolEvent) -> str:
    """Compact decision-time state for one event. Reads nothing after the issuing message."""
    upto = [m for m in sorted(session.messages, key=lambda m: m.index) if m.index <= event.message_index]
    goals = [m for m in upto if m.role == "user" and m.text.strip()][-GOAL_PROMPTS:]
    window = upto[-WINDOW:]
    try:
        call = json.dumps(event.input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        call = str(event.input)
    flags = []
    if event.is_error:
        flags.append("error")
    if event.interrupted:
        flags.append("interrupted, no result")
    lines = ["GOAL (latest user prompts):"] + [f"- {cut(m.text.strip(), GOAL_CHARS)}" for m in goals]
    lines += ["", f"CALL: {event.name} {cut(call, CALL_CHARS)}",
              f"RESULT ({event.result_chars} chars{'; ' + ', '.join(flags) if flags else ''}):",
              head_tail(event.result_text), "", "NEARBY MESSAGES:"]
    lines += [f"[{m.role}] {cut(m.text.strip(), WINDOW_CHARS)}" for m in window]
    return "\n".join(lines)


def example(state: str, head: str, answer: bool | None = None) -> dict[str, Any]:
    m = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"{state}\n\nQUESTION: {QUESTIONS[head]}"}]
    if answer is not None:
        m.append({"role": "assistant", "content": "yes" if answer else "no"})
    return {"messages": m}


def split_of(session_id: str, seed: str = SEED) -> str:
    x = int(hashlib.sha1((seed + "\0" + session_id).encode()).hexdigest(), 16) % 1000 / 1000
    return "train" if x < SPLIT[0] else "valid" if x < SPLIT[0] + SPLIT[1] else "test"


def build(sessions: Iterable[Session], labels: Iterable[dict[str, Any]], *, seed: str = SEED) -> dict[str, Any]:
    by_session = {s.session: s for s in sessions}
    out: dict[str, Any] = {"train": [], "valid": [], "test": [], "test_rows": []}
    stats: dict[str, Any] = {"skipped_unknown": 0, "skipped_no_session": 0, "sessions": Counter(),
                             "events": Counter(), "positives": {h: Counter() for h in QUESTIONS}}
    train_by_head: dict[str, list] = {h: [] for h in QUESTIONS}
    seen_sessions: dict[str, set] = {"train": set(), "valid": set(), "test": set()}
    for row in labels:
        if row.get("status") != "labelled":
            stats["skipped_unknown"] += 1
            continue
        s = by_session.get(row["session"])
        ev = next((e for e in s.events if e.index == row["event_index"]), None) if s else None
        if ev is None:
            stats["skipped_no_session"] += 1
            continue
        sp = split_of(s.session, seed)
        seen_sessions[sp].add(s.session)
        stats["events"][sp] += 1
        st = state_text(s, ev)
        for head in QUESTIONS:
            y = bool(row[LABEL_KEY[head]])
            stats["positives"][head][sp] += int(y)
            if sp == "train":
                train_by_head[head].append((y, example(st, head, y)))
            else:
                out[sp].append(example(st, head, y))
        if sp == "test":
            out["test_rows"].append({"session": s.session, "event_index": ev.index, "result_chars": ev.result_chars,
                                     "input_chars": len(json.dumps(ev.input, sort_keys=True, default=str)),
                                     "needed_call": bool(row["needed_call"]),
                                     "needed_result": bool(row["needed_result"]), "state": st})
    stats["reps"] = {}
    for head, items in train_by_head.items():
        pos = sum(1 for y, _ in items if y)
        neg = len(items) - pos
        minority = pos <= neg
        small, big = (pos, neg) if minority else (neg, pos)
        reps = min(MAX_REPS, max(1, round(big / small))) if small else 1
        stats["reps"][head] = {"class": "yes" if minority else "no", "reps": reps}
        for y, ex in items:
            out["train"].extend([ex] * (reps if y == minority else 1))
    stats["sessions"] = {k: len(v) for k, v in seen_sessions.items()}
    stats["events"] = dict(stats["events"])
    stats["positives"] = {h: dict(c) for h, c in stats["positives"].items()}
    stats["examples"] = {k: len(out[k]) for k in ("train", "valid", "test")}
    out["stats"] = stats
    return out


def session_to_dict(s: Session) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(s)


def session_from_dict(d: dict[str, Any]) -> Session:
    msgs = tuple(Message(**{**m, "tool_event_indexes": tuple(m.get("tool_event_indexes", ()))}) for m in d["messages"])
    return Session(session=d["session"], source=d["source"], messages=msgs,
                   events=tuple(ToolEvent(**e) for e in d["events"]))


def write(out: dict[str, Any], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, key in (("train.jsonl", "train"), ("valid.jsonl", "valid"), ("test.jsonl", "test"),
                      ("test-rows.jsonl", "test_rows")):
        with open(dest / name, "w", encoding="utf-8") as f:
            for r in out[key]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (dest / "stats.json").write_text(json.dumps(out["stats"], indent=2, sort_keys=True) + "\n")


def _read_jsonl(p: Path):
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--sessions", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", default=SEED)
    a = ap.parse_args(argv)
    out = build([session_from_dict(d) for d in _read_jsonl(a.sessions)], _read_jsonl(a.labels), seed=a.seed)
    write(out, a.out)
    print(json.dumps(out["stats"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
