"""Deterministic handoff document from a parsed Session (phase 0; stdlib only, no model calls).

`build_handoff(session, policy) -> (markdown, manifest)`. Sections, in order, each under its own token budget from
`tables/compaction.json` (values and sources: docs/COMPACTION-THRESHOLDS.md):
  Goal (latest user prompt verbatim), Recency pin (earlier recent user turns verbatim + the last tool calls raw),
  Standing rules, Decisions, In-flight, Files touched, Ranked excerpts.
Tokens are an ESTIMATE: ceil(chars / chars_per_token), per transcript format from the policy (2.5 for Claude Code,
4 for Codex, the conservative 2.5 when unknown). It is used only for budgeting; it is not a tokenizer count.

The manifest records exactly which message (`m`) / tool event (`e`) indexes went into which section, with scores, so
phase 2 can turn later reuse into labels. Same Session + same policy -> byte-identical output.

Over the total budget, sections are trimmed lowest priority first: excerpts, files, decisions, recency pin. Goal and
Standing rules are never trimmed: if they alone exceed the total budget, HandoffBudgetError is raised (a handoff
without its goal or rules would restart the session wrong, which is worse than not restarting).
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

from .events import Session

ESTIMATE_NOTE = "tokens = ceil(chars / chars_per_token) per transcript format, an estimate, not a tokenizer count"

# Set per build from policy["chars_per_token"] and the session's source; 2.5 (the conservative Claude ratio) default.
_CPT = [2.5]


class HandoffBudgetError(ValueError):
    pass


def est_tokens(text: str, chars_per_token: float = 0) -> int:
    return math.ceil(len(text) / (chars_per_token or _CPT[0]))


RULE_RE = re.compile(r"\b(never|always|must|don't|do not|only|every)\b", re.I)
DECISION_RE = re.compile(r"\b(decided|decide|chose|chosen|because|approved?|merged|committed|reverted|agreed)\b", re.I)
PATH_RE = re.compile(r"(?:~|\.{0,2})?/?(?:[\w.-]+/)+[\w.-]+\.\w+|\b[\w-]+\.(?:py|json|md|ts|js|toml|yaml|yml|sh|txt)\b")
IDENT_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b[a-z0-9]+(?:_[a-z0-9]+)+\b")
HASH_RE = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
ERROR_RE = re.compile(r"\b(error|errors|fail|failed|failing|failure|traceback|exception)\b", re.I)
PR_RE = re.compile(r"(?:\bPR\s*#?|#)(\d{1,6})\b")
BRANCH_RE = re.compile(r"(?:\bbranch\s+|checkout\s+(?:-b\s+)?|switch\s+(?:-c\s+)?|worktree add\s+-b\s+)`?([\w][\w./-]+)")
TASK_RE = re.compile(r"\b(?:task|agent|run)[_-]?(?:id)?[:= ]+`?([\w-]{6,})|\b((?:task|agent)[_-][0-9a-z]{4,})\b", re.I)
WORKTREE_RE = re.compile(r"(?:~|/)[\w./-]*worktrees?/[\w./-]+")
SENT_RE = re.compile(r"[^.!?\n]+[.!?]?")
WORD_RE = re.compile(r"[a-z][a-z0-9_]{2,}")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
STOPWORDS = frozenset("""the and for that this with from are was were you your have has had not but can will would should
could into onto out about then than them they their there here what when where which who why how all any each some such
its it's our ours just also very more most much many other only own same too use used using make made need needs one two
new now get got let lets please want like know see yes okay done does did doing been being way may might""".split())


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in SENT_RE.findall(text) if s.strip()]


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + " [...%d chars cut]" % (len(text) - n)


def _render(title: str, lines: List[str]) -> str:
    body = "\n".join(lines) if lines else "_(none)_"
    return "## %s\n\n%s\n" % (title, body)


def _fill(items: List[dict], budget: int) -> Tuple[List[dict], int]:
    """Greedy: take items in the given order while they fit; return (taken, dropped count)."""
    used, taken, dropped = 0, [], 0
    for it in items:
        cost = est_tokens(it["line"]) + 1
        if used + cost <= budget:
            taken.append(it)
            used += cost
        else:
            dropped += 1
    return taken, dropped


def _goal(session: Session, policy: dict) -> List[dict]:
    users = [m for m in session.messages if m.role == "user" and m.text.strip()]
    return [{"kind": "m", "index": m.index, "line": "> [m%d] %s" % (m.index, m.text.strip().replace("\n", "\n> "))}
            for m in users[-policy["goal_prompts"]:]]


def _head_tail(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    return "%s [...%d of %d chars cut...] %s" % (text[:head], len(text) - head - tail, len(text),
                                                  text[-tail:] if tail else "")


def _recency_pin(session: Session, policy: dict, goal_items: List[dict], budget: int) -> Tuple[List[dict], int]:
    """Earlier recent user turns verbatim (those not already in Goal) and the last tool calls raw (input + result,
    the result cut head + tail only to fit the section)."""
    rp = policy["recency_pin"]
    goal_ids = {it["index"] for it in goal_items}
    users = [m for m in session.messages if m.role == "user" and m.text.strip() and m.index not in goal_ids]
    items = [{"kind": "m", "index": m.index, "order": (m.index, 0, 0),
              "line": "> [m%d user] %s" % (m.index, m.text.strip().replace("\n", "\n> "))}
             for m in (users[-rp["user_turns"]:] if rp["user_turns"] else [])]
    evs = list(session.events[-rp["tool_calls"]:]) if rp["tool_calls"] else []
    cpt = _CPT[0]
    room = int(budget * cpt / max(1, len(evs) or 1) / 2)  # half the section per call at most, in chars
    for e in evs:
        res = "(no result: interrupted)" if e.interrupted else _head_tail(e.result_text, room // 2, room // 4)
        items.append({"kind": "e", "index": e.index, "order": (e.message_index, 1, e.index),
                      "line": "- [e%d %s%s] input %s\n  result (%d chars): %s"
                              % (e.index, e.name, " ERROR" if e.is_error else "", _cut(_json(e.input), room // 4),
                                 e.result_chars, res)})
    # newest first under the budget, then chronological
    taken, dropped = _fill(sorted(items, key=lambda it: it["order"], reverse=True), budget)
    return sorted(taken, key=lambda it: it["order"]), dropped


def _rules(session: Session) -> List[dict]:
    latest: Dict[str, dict] = {}
    for m in session.messages:
        if m.role != "user":
            continue
        for s in _sentences(m.text):
            if RULE_RE.search(s):
                key = " ".join(s.lower().split())
                latest.pop(key, None)  # newest wins: re-insert at the newest position
                latest[key] = {"kind": "m", "index": m.index, "line": "- %s [m%d]" % (s, m.index)}
    return list(latest.values())


def _decisions(session: Session, budget: int) -> Tuple[List[dict], int]:
    found = []
    for m in session.messages:
        hits = [s for s in _sentences(m.text) if DECISION_RE.search(s)]
        if hits:
            found.append({"kind": "m", "index": m.index, "line": "- [m%d %s] %s" % (m.index, m.role, " ".join(hits))})
    taken, dropped = _fill(list(reversed(found)), budget)  # newest first under the budget
    return sorted(taken, key=lambda it: it["index"]), dropped


def _in_flight(session: Session, policy: dict, budget: int) -> Tuple[List[dict], int]:
    items = []
    for e in session.events:
        if e.interrupted:
            items.append({"kind": "e", "index": e.index,
                          "line": "- VERIFY: tool call `%s` [e%d] has no result (interrupted): %s"
                                  % (e.name, e.index, _cut(_json(e.input), 200))})
    k = policy["recency_window_messages"]
    recent = session.messages[-k:] if k else ()
    seen = set()
    for m in recent:
        texts = [m.text] + [_json(session.events[i].input) for i in m.tool_event_indexes]
        found = []
        for text in texts:
            for kind, rx in (("branch", BRANCH_RE), ("pr", PR_RE), ("commit", HASH_RE), ("task", TASK_RE),
                             ("worktree", WORKTREE_RE)):
                for mt in rx.finditer(text):
                    val = next((g for g in mt.groups() if g), None) if mt.groups() else mt.group(0)
                    if val and (kind, val) not in seen:
                        seen.add((kind, val))
                        found.append("%s %s" % (kind, val))
        if found:
            items.append({"kind": "m", "index": m.index, "line": "- [m%d] %s" % (m.index, "; ".join(found))})
    return _fill(items, budget)


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _files(session: Session, budget: int, max_files: int) -> Tuple[List[dict], int]:
    counts: Counter = Counter()
    where: Dict[str, List[int]] = {}
    tools: Dict[str, set] = {}
    for e in session.events:
        paths = []
        if e.name in EDIT_TOOLS and isinstance(e.input.get(EDIT_TOOLS[e.name]), str):
            paths.append(e.input[EDIT_TOOLS[e.name]])
        for s in _strings(e.input):
            paths.extend(p.strip() for p in PATCH_FILE_RE.findall(s))
        for p in paths:
            counts[p] += 1
            where.setdefault(p, []).append(e.index)
            tools.setdefault(p, set()).add(e.name)
    ordered = sorted(counts, key=lambda p: (-counts[p], p))
    items = [{"kind": "e", "indexes": where[p], "path": p, "count": counts[p],
              "line": "- `%s` x%d (%s)" % (p, counts[p], ", ".join(sorted(tools[p])))} for p in ordered]
    over = max(0, len(items) - max_files)
    taken, dropped = _fill(items[:max_files], budget)
    return taken, dropped + over


def _excerpts(session: Session, policy: dict, goal_ids: set, skip: set, budget: int) -> Tuple[List[dict], int]:
    """skip: ("m", i) / ("e", i) already shown verbatim in Goal or the recency pin."""
    goal_words = set(WORD_RE.findall(" ".join(session.messages[i].text for i in sorted(goal_ids)).lower())) - STOPWORDS
    n = len(session.messages)
    k = policy["recency_window_messages"]
    cands = []

    def score(text: str, msg_index: int) -> float:
        words = set(WORD_RE.findall(text.lower())) - STOPWORDS
        s = 3 * len(words & goal_words)
        for rx in (DECISION_RE, PATH_RE, IDENT_RE, HASH_RE, ERROR_RE):
            if rx.search(text):
                s += 2
        if k and msg_index >= n - k:
            s += policy["recency_bonus"]
        return s

    for m in session.messages:
        if ("m", m.index) in skip or not m.text.strip():
            continue
        line = "- [m%d %s] %s" % (m.index, m.role,
                                   _cut(m.text.strip(), policy["excerpt_message_truncation_chars"]).replace("\n", " "))
        cands.append({"kind": "m", "index": m.index, "order": (m.index, 0, 0), "score": score(m.text, m.index),
                      "line": line})
    for e in session.events:
        if not e.result_text or ("e", e.index) in skip:
            continue
        ht = policy["excerpt_tool_result_chars"]
        body = _head_tail(e.result_text.strip(), ht["head"], ht["tail"]).replace("\n", " ")
        line = "- [e%d %s%s] %s -> (%d chars) %s" % (e.index, e.name, " ERROR" if e.is_error else "",
                                                    _cut(_json(e.input), 160), e.result_chars, body)
        cands.append({"kind": "e", "index": e.index, "order": (e.message_index, 1, e.index),
                      "score": score(e.result_text + " " + _json(e.input), e.message_index), "line": line})
    ranked = sorted(cands, key=lambda c: (-c["score"], tuple(-x for x in c["order"])))
    taken, dropped = _fill(ranked, budget)
    return sorted(taken, key=lambda c: c["order"]), dropped


SECTION_TITLES = (("goal", "Goal"), ("recency_pin", "Recency pin (latest turns, verbatim)"),
                  ("rules", "Standing rules"), ("decisions", "Decisions"),
                  ("in_flight", "In-flight (verify before acting)"), ("files", "Files touched"),
                  ("excerpts", "Ranked excerpts"))
TRIM_ORDER = ("excerpts", "files", "decisions", "recency_pin")


def handoff_id(session: Session) -> str:
    h = hashlib.sha256()
    h.update(("%s\0%s\0%d\0%d" % (session.source, session.session, len(session.messages),
                                    len(session.events))).encode())
    for m in session.messages[-3:]:
        h.update(m.text.encode())
    return h.hexdigest()[:16]


def build_handoff(session: Session, policy: dict) -> Tuple[str, dict]:
    b = policy["section_budgets"]
    total_budget = policy["handoff_total_tokens"]
    sections: Dict[str, List[dict]] = {}
    dropped: Dict[str, int] = {}
    cpt = policy["chars_per_token"]
    _CPT[0] = cpt.get(session.source, cpt["unknown"])
    sections["goal"] = _goal(session, policy)
    dropped["goal"] = 0
    sections["recency_pin"], dropped["recency_pin"] = _recency_pin(session, policy, sections["goal"],
                                                                   b["recency_pin"])
    sections["rules"] = _rules(session)
    dropped["rules"] = 0
    sections["decisions"], dropped["decisions"] = _decisions(session, b["decisions"])
    sections["in_flight"], dropped["in_flight"] = _in_flight(session, policy, b["in_flight"])
    sections["files"], dropped["files"] = _files(session, b["files"], policy["files_max"])
    sections["excerpts"], dropped["excerpts"] = _excerpts(
        session, policy, {it["index"] for it in sections["goal"]},
        {(it["kind"], it["index"]) for k in ("goal", "recency_pin") for it in sections[k]}, b["excerpts"])

    hid = handoff_id(session)
    header = ("# CortadOS handoff\n\nhandoff-id: %s\n\nsession %s (%s), %d messages, %d tool calls. Token counts are "
              "estimates (ceil(chars/%s)). Resume from this document; re-read files before editing them.\n"
              % (hid, session.session, session.source, len(session.messages), len(session.events), _CPT[0]))

    def render_all() -> Tuple[str, Dict[str, int]]:
        parts, toks = [header], {}
        for key, title in SECTION_TITLES:
            text = _render(title, [it["line"] for it in sections[key]])
            toks[key] = est_tokens(text)
            parts.append(text)
        return "\n".join(parts), toks

    pinned = est_tokens(header) + sum(est_tokens(_render(t, [it["line"] for it in sections[k]]))
                                      for k, t in SECTION_TITLES if k in ("goal", "rules"))
    if pinned > total_budget:
        raise HandoffBudgetError("goal + standing rules alone need ~%d tokens, over the %d-token handoff budget; "
                                 "refusing to trim them" % (pinned, total_budget))
    md, toks = render_all()
    for key in TRIM_ORDER:
        # lowest-priority item first: excerpts by score, others oldest first
        while est_tokens(md) > total_budget and sections[key]:
            items = sections[key]
            victim = min(items, key=lambda it: (it.get("score", 0), it.get("index", 0))) if key == "excerpts" \
                else items[0]
            items.remove(victim)
            dropped[key] += 1
            md, toks = render_all()
    if est_tokens(md) > total_budget:
        raise HandoffBudgetError("handoff needs ~%d tokens after trimming, over the %d-token budget"
                                 % (est_tokens(md), total_budget))

    manifest = {
        "handoff_id": hid, "session": session.session, "source": session.source,
        "policy_status": policy.get("status"), "policy_version": policy.get("version"),
        "token_estimate": ESTIMATE_NOTE, "chars_per_token": _CPT[0], "total_tokens": est_tokens(md), "total_budget": total_budget,
        "sections": {},
    }
    for key, _title in SECTION_TITLES:
        entries = []
        for it in sections[key]:
            ent = {"kind": "message" if it["kind"] == "m" else "event"}
            if "indexes" in it:
                ent["indexes"] = it["indexes"]
                ent["path"] = it["path"]
                ent["count"] = it["count"]
            else:
                ent["index"] = it["index"]
            if "score" in it:
                ent["score"] = it["score"]
            entries.append(ent)
        manifest["sections"][key] = {"tokens": toks[key], "budget": b[key], "over_budget": toks[key] > b[key],
                                     "dropped": dropped[key], "items": entries}
    return md, manifest
