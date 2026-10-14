"""Opt-in dual reviews: provenance, constrained selection and durable dispatch tickets.

No model calls or Discord calls on import. Transport is injected by the plugin.
Model vendor is distinct from the subscription selling access to that model.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

MARKER = re.compile(r"\[cortados-review:([0-9a-f]{32}):(samantha|rachel)\]")


def vendor(model: str) -> str | None:
    name = model.lower().removeprefix("cursor-")
    for prefix, maker in (("claude", "anthropic"), ("gpt", "openai"), ("o1", "openai"),
                          ("o3", "openai"), ("o4", "openai"), ("gemini", "google"),
                          ("grok", "xai"), ("kimi", "moonshot"), ("glm", "zhipu"),
                          ("composer", "cursor"), ("muse", "meta")):
        if name.startswith(prefix):
            return maker
    return None


def model_row(core, model: str) -> dict:
    rows = (core.model_scores() or {}).get("models", [])
    # Exact catalog/reach match first; do not guess from a face's configured default.
    name = model.lower().removesuffix("[1m]")
    for row in rows:
        if name in {row["family"].lower(), *(r["model"].lower() for r in row["reach"])}:
            return row
    raise ValueError(f"No benchmark entry for actual author model {model!r}; refresh the inventory first")


def plan(core, author: dict, use_case: str, *, health=None, history_chars=0,
         roles=("samantha", "rachel"), locked_routes=None) -> dict:
    ms, table = core.model_scores(), core.table()
    row = model_row(core, author["model"])
    maker = vendor(row["family"])
    if not maker:
        raise ValueError("Author model vendor is unknown")
    if use_case not in {"general", *ms.get("use_cases", {})}:
        raise ValueError("Unknown review use case")
    cfg = table.get("review_policy", {})
    gap = float(cfg.get("comparable_index_gap", 20))
    floor = float(cfg.get("minimum_general_index", 50))
    strong = bool(row["ceiling"])
    score = row["index"].get(use_case, row["index"]["general"])
    health = core.lane_health() if health is None else health
    candidates = []
    for c in core.scored_candidates(table, "R4", use_case) or []:
        cv = vendor(c["family"])
        h = core.candidate_health(c, health)
        if not cv or cv == maker or c["lane"] == "local" or not h.get("ok", True):
            continue
        if c["general"] < min(floor, row["index"]["general"]) or core.turn_tokens(history_chars) > core.lane_ceiling(c["lane"], c["model"]):
            continue
        native = core.native_lane_for(c["family"])
        # Accessing Claude through Cursor is still Anthropic; prefer native when reachable.
        if native and native != c["lane"]:
            native_routes = [r for r in model_row(core, c["model"])["reach"] if r["lane"] == native]
            if any(core.candidate_health(r, health).get("ok", True) for r in native_routes):
                continue
        c = dict(c, vendor=cv, scarcity=h.get("scarcity") or 0)
        candidates.append(c)
    partner = {"openai": "anthropic", "anthropic": "openai"}.get(maker)
    selected = {}
    for role in roles:
        target_strong = strong if role == "samantha" else not strong
        eligible = [c for c in candidates if bool(c["ceiling"]) == target_strong]
        if role == "samantha":
            eligible = [c for c in eligible if abs(c["index"] - score) <= gap]
        elif strong:
            eligible = [c for c in eligible if c["general"] < row["index"]["general"]]
        else:
            eligible = [c for c in eligible if c["general"] > row["index"]["general"]]
        if not eligible:
            raise ValueError(f"No eligible {role} model: keep the vendor/capability constraints; retry after availability changes")
        def rank(c):
            # Relationship first. Scarcity breaks near capability ties, never changes the role.
            distance = abs(c["index"] - score) if role == "samantha" else -c["index"]
            return (0 if not partner or c["vendor"] == partner else 1,
                    round(distance / 5), c["scarcity"], distance,
                    c.get("price") or 0, c["model"], c["lane"])
        locked = (locked_routes or {}).get(role)
        if locked:
            eligible = [c for c in eligible if (c["model"], c["lane"]) == (locked["model"], locked["lane"])]
            if not eligible:
                raise ValueError(f"Assigned {role} route is unavailable; request an explicit retry")
        selected[role] = min(eligible, key=rank)
    return {"author": author, "author_vendor": maker, "author_ceiling": strong,
            "use_case": use_case, "reviewers": selected}


@contextlib.contextmanager
def database(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    path = state / "reviews.db"
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        path.chmod(0o600)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS authors (
              turn_id TEXT PRIMARY KEY, session_id TEXT, profile TEXT, model TEXT, provider TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS requests (
              id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, payload TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS deliveries (
              id TEXT, role TEXT, status TEXT, message_id TEXT, error TEXT,
              PRIMARY KEY (id, role));
        """)
        yield db
        db.commit()
    finally:
        db.close()


def record_author(state, turn_id, session_id, profile, model, provider):
    if not turn_id or not model:
        return
    with database(state) as db:
        db.execute("INSERT OR REPLACE INTO authors VALUES (?,?,?,?,?,?)",
                   (turn_id, session_id, profile, model, provider, time.time()))


def authors(core, limit=10):
    with database(core.STATE) as db:
        return [dict(r) for r in db.execute("SELECT * FROM authors ORDER BY ts DESC LIMIT ?", (max(1, min(int(limit), 30)),))]


def author_for(core, args, current_turn=""):
    turn = args.get("author_turn_id")
    job = args.get("author_job_id")
    if bool(turn) == bool(job):
        raise ValueError("Supply exactly one author_turn_id or author_job_id for the ORIGINAL work; use action=authors to inspect actual calls")
    if job:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job):
            raise ValueError("Invalid job id")
        meta = json.loads((core.STATE / "jobs" / job / "meta.json").read_text())
        if meta.get("exit_code") != 0 or meta.get("status") != "exited":
            raise ValueError("The author worker must have completed successfully")
        return {"job_id": job, "model": meta["model"], "provider": meta["runtime"], "evidence": "completed worker metadata"}
    turn = current_turn if turn == "current" else turn
    with database(core.STATE) as db:
        row = db.execute("SELECT * FROM authors WHERE turn_id=?", (turn,)).fetchone()
    if not row:
        raise ValueError("Actual author call was not recorded; do not substitute the orchestrator's or face's default model")
    return dict(row, evidence="post_api_request actual model")


def dispatch(core, args, sender, sender_bot_id, post, current_turn=""):
    if sender in {"samantha", "rachel"}:
        raise ValueError("The original author or Jessica must request the pair; a reviewer cannot wake its own bot")
    brief = str(args.get("brief") or "").strip()
    if not 1 <= len(brief) <= 1000:
        raise ValueError("brief must be 1..1000 characters: source/artifact path, objective and acceptance criteria")
    author = author_for(core, args, current_turn)
    use_case = args.get("use_case", "general")
    identity = {k: author.get(k) for k in ("turn_id", "job_id", "model")}
    fingerprint = hashlib.sha256(json.dumps([sender, identity, use_case, brief], sort_keys=True).encode()).hexdigest()
    with database(core.STATE) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT id,payload FROM requests WHERE fingerprint=?", (fingerprint,)).fetchone()
        if old:
            ticket, payload = old["id"], json.loads(old["payload"])
            retry_statuses = {"pending", "failed"} | ({"blocked"} if args.get("retry_blocked") else set())
            retry_roles = [r["role"] for r in db.execute("SELECT role,status FROM deliveries WHERE id=?", (ticket,)) if r["status"] in retry_statuses]
            if retry_roles:
                payload["reviewers"].update(plan(core, author, use_case, roles=retry_roles)["reviewers"])
                db.execute("UPDATE requests SET payload=?,ts=? WHERE id=?", (json.dumps(payload), time.time(), ticket))
                if args.get("retry_blocked"):
                    db.execute("UPDATE deliveries SET status='pending',message_id=NULL,error=NULL WHERE id=? AND status='blocked'", (ticket,))
        else:
            payload = plan(core, author, use_case)
            ticket = uuid.uuid4().hex
            payload.update(sender=sender, sender_bot_id=sender_bot_id, brief=brief)
            db.execute("INSERT INTO requests VALUES (?,?,?,?)", (ticket, fingerprint, json.dumps(payload), time.time()))
            for role in payload["reviewers"]:
                db.execute("INSERT INTO deliveries VALUES (?,?, 'pending', NULL, NULL)", (ticket, role))
    # One explicit request always dispatches BOTH roles. Retrying only sends definitely failed legs.
    for role, chosen in payload["reviewers"].items():
        with database(core.STATE) as db:
            changed = db.execute("UPDATE deliveries SET status='sending' WHERE id=? AND role=? AND status IN ('pending','failed')", (ticket, role)).rowcount
        if not changed:
            continue
        text = (f"[cortados-review:{ticket}:{role}] Review requested; independently review the original work. "
                f"Author: {author['model']}. Your route: {chosen['model']} ({chosen['vendor']}). "
                + ("Match the author's capability. " if role == "samantha" else "Adversarial review at the opposite capability level. ")
                + "Do not read the other review before submitting yours. Report BLOCK/FIX/NIT/QUESTION with source evidence; do not edit or deploy. " + brief)
        try:
            # bus.post's timestamp-derived default would put simultaneous reviews in ONE thread.
            result = json.loads(post({"to": role, "tag": "ask", "thread": ticket[:5] + role[0], "text": text}))
            status = "sent" if result.get("success") else ("unknown" if re.search(r"timed? ?out|connection|urlopen", result.get("error", ""), re.I) else "failed")
        except Exception as exc:
            # A timeout might have posted. Do not blindly duplicate that review on retry.
            result, status = {"error": str(exc)}, "unknown"
        with database(core.STATE) as db:
            db.execute("UPDATE deliveries SET status=CASE WHEN status='sending' THEN ? ELSE status END,message_id=?,error=? WHERE id=? AND role=?",
                       (status, result.get("message_id"), result.get("error"), ticket, role))
    return status_for(core, ticket)


def status_for(core, ticket):
    with database(core.STATE) as db:
        request = db.execute("SELECT payload FROM requests WHERE id=?", (ticket,)).fetchone()
        deliveries = [dict(r) for r in db.execute("SELECT role,status,message_id,error FROM deliveries WHERE id=?", (ticket,))]
    if not request:
        raise ValueError("Unknown review request")
    return {"request_id": ticket, "success": all(r["status"] in {"sent", "running", "reviewed"} for r in deliveries),
            "plan": json.loads(request["payload"]), "deliveries": deliveries,
            "note": "Sent means requested; reviewed means a model produced a review, not delivery confirmation. Unknown delivery needs inspection, not a blind retry."}


def ticket_for(core, text, profile, sender_id):
    # Inspect only the triggering message, not a quoted reply or recent channel history.
    current = text.rsplit("[New message]", 1)[-1]
    if "[Recent channel messages]" in current:
        return None
    current = re.sub(r'^\[Replying to:.*?\]\s*', '', current, flags=re.S)
    current = re.sub(r'\[Triggering message id:[^\]]*\]\s*', '', current)
    marker = re.match(r"(?:\[(?:ask|assign)#[a-z0-9]+\]\s*)?(?:<@!?[0-9]+>\s*)?" + MARKER.pattern, current.strip())
    if not marker:
        return None
    ticket, role = marker.groups()
    if role != profile:
        raise ValueError("Review ticket is addressed to another profile")
    with database(core.STATE) as db:
        row = db.execute("SELECT payload,ts FROM requests WHERE id=?", (ticket,)).fetchone()
    if not row or time.time() - row["ts"] > 86400:
        raise ValueError("Unknown or expired review ticket")
    payload = json.loads(row["payload"])
    if str(sender_id) != str(payload["sender_bot_id"]):
        raise ValueError("Review ticket sender does not match its requester")
    return dict(payload, request_id=ticket, role=role)


def mark(core, ticket, role, status, error=None):
    with database(core.STATE) as db:
        db.execute("UPDATE deliveries SET status=?,error=? WHERE id=? AND role=?", (status, error, ticket, role))


def claim(core, ticket, role, message_id):
    with database(core.STATE) as db:
        changed = db.execute("""UPDATE deliveries SET status='running'
          WHERE id=? AND role=? AND status IN ('sending','sent')
          AND (message_id IS NULL OR message_id=?)""", (ticket, role, str(message_id or ""))).rowcount
    if not changed:
        raise ValueError("Review already started or triggering message does not match the dispatch")


SCHEMA = {
    "name": "router_review",
    "description": "Only when review is explicitly requested: dispatch BOTH Samantha (comparable capability, different model vendor) and Rachel (opposite capability, different vendor). Never automatic. Use authors to find the original author's actual turn; use its worker job id if a worker wrote the artifact. Do not use the orchestrator's model. Both receive the same original brief independently. No delegation for ordinary tasks.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["authors", "request", "status"]},
        "author_turn_id": {"type": "string", "description": "Original author's turn id from action=authors; 'current' ONLY if you yourself wrote the work in this turn."},
        "author_job_id": {"type": "string", "description": "Completed agent_run job that actually wrote the work (instead of author_turn_id)."},
        "brief": {"type": "string", "maxLength": 1000, "description": "Original artifact/source path, review objective, acceptance criteria. No other review's conclusions."},
        "use_case": {"type": "string", "enum": ["general", "code", "ops", "research", "planning", "writing", "conversation", "capture/log"]},
        "request_id": {"type": "string", "description": "For action=status."},
        "retry_blocked": {"type": "boolean", "description": "Explicitly retry blocked reviewers on an identical request; already sent or reviewed legs are not repeated."},
    }, "required": ["action"]},
}
