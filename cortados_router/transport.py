"""Read-only, conservative pre-dispatch sizing; never trims conversation.

This is an admission estimate, not the provider's final serialized byte guard.
The latter must still run on every call, including the current tool loop.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))


def history_size(messages) -> int:
    # Include tool-call arguments, tool results and multimodal content, not just
    # text content. JSON framing conservatively covers per-message labels.
    fields = ('role', 'content', 'tool_calls', 'tool_call_id', 'name')
    return sum(encoded_size({k: m[k] for k in fields if k in m}) + 128
               for m in (messages or []) if isinstance(m, dict))


def stored_history_size(home: Path, session_id: str) -> int:
    """Read only the active face's exact session, never a substring/other face.

    A stale compression mapping is unknown, not an empty conversation. Let
    Hermes advance that mapping; this observer must not repair/write it.
    """
    with sqlite3.connect((home / 'state.db').resolve().as_uri() + '?mode=ro',
                         uri=True, timeout=0.25) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT end_reason FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None or row[0] == 'compression':
            raise ValueError('session missing or compression mapping not advanced')
        columns = {r[1] for r in db.execute('PRAGMA table_info(messages)')}
        # Display and API content are alternate representations of one message,
        # not two messages. Count the larger representation, plus tool framing.
        contents = [f"coalesce(length(cast({f} AS BLOB)),0)"
                    for f in ('content', 'api_content') if f in columns]
        body = f"max({','.join(contents)})" if len(contents) > 1 else contents[0]
        fields = [f for f in ('tool_calls', 'tool_call_id', 'tool_name') if f in columns]
        sizes = '+'.join([body, *(f"coalesce(length(cast({f} AS BLOB)),0)" for f in fields)])
        where = 'session_id=?'
        if 'active' in columns:
            where += ' AND active=1'
        if 'observed' in columns:
            where += ' AND observed=0'
        return int(db.execute(f'SELECT coalesce(sum({sizes}+128),0) FROM messages WHERE {where}',
                              (session_id,)).fetchone()[0])


def measure(gateway, key: str, current: str, recall: str, cache: dict,
            home: Path, config: dict) -> dict:
    sid, entry, known, reason = None, None, False, None
    try:
        entry = gateway.session_store.lookup_by_session_key(key)
        sid = getattr(entry, 'session_id', None)
        if not isinstance(sid, str) or not sid:
            raise ValueError('no exact session mapping')
        stored = stored_history_size(home, sid)
        known = True
    except Exception as exc:
        stored = 0
        reason = type(exc).__name__ + ': ' + str(exc)[:120]
    observation = cache.get(sid, {}) if sid else {}
    history = max(stored, int(observation.get('history_bytes') or observation.get('history_chars') or 0))
    # A cache miss/read failure is never classified as a known fresh session.
    harness = max(int(config.get('harness_tokens', 31000)) * 4,
                  int(observation.get('envelope_bytes') or 0))
    request = len(current.encode('utf-8')) + len(recall.encode('utf-8')) + 1024
    estimated_bytes = harness + history + request
    previous_tokens = int(getattr(entry, 'last_prompt_tokens', 0) or 0) if entry else 0
    tokens = int(max(estimated_bytes / 4, previous_tokens + request / 4)
                 * float(config.get('headroom', 1.2)))
    return {'session_id': sid, 'known': known, 'reason': reason,
            'history_bytes': history, 'current_and_recall_bytes': request,
            'envelope_bytes': harness, 'input_bytes': estimated_bytes,
            'input_tokens': tokens, 'source': 'exact_session_read_only_with_cached_envelope'}
