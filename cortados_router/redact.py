"""Credential redaction for the child-task runner (child_tasks.Redactor builds on this class).

One implementation: every credential and its JSON-escaped spelling (JSON output escapes quotes and backslashes in
it), matched in a single alternation, longest first, so a shorter credential that is a prefix of a longer one never
splits it. Redact BEFORE serializing or truncating: truncating first can cut a credential to a prefix that no longer
matches and leak that prefix.
"""
from __future__ import annotations

import json
import re

MASK = "***"
# Escaping layers matched per credential: JSON inside a JSON string escapes it again at each level (a tool result
# holding JSON text, inside claude's stream-json event, is two levels). Four covers every nesting seen in claude's
# output with room to spare; the structural pass (deep) decodes embedded JSON so its depth is relative anyway.
ESCAPE_DEPTH = 4


class Redactor:
    def __init__(self, tokens=()):
        toks = {t for t in tokens if t}
        self.raw = sorted(toks)
        for t in list(toks):
            spelled = t
            for _ in range(ESCAPE_DEPTH):
                spelled = json.dumps(spelled)[1:-1]
                toks.add(spelled)
        self.tokens = sorted(toks, key=len, reverse=True)
        self._re = re.compile("|".join(re.escape(t) for t in self.tokens)) if self.tokens else None

    def text(self, s: str) -> str:
        return self._re.sub(MASK, s) if self._re else s

    def leading_fragment(self, s: str) -> str:
        """Mask a token suffix at the very start of `s` (text read from mid-file may begin mid-token)."""
        for t in self.tokens:
            for k in range(len(t) - 1, 0, -1):
                if s.startswith(t[-k:]):
                    return MASK + s[k:]
        return s

    def tree(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.tree(x) for x in obj]
        if isinstance(obj, dict):
            return {self.text(str(k)): self.tree(v) for k, v in obj.items()}
        return obj

    def deep(self, obj):
        """Like tree, and a string that is itself JSON (an object or array, e.g. a tool result holding JSON text) is
        decoded, redacted the same way and re-encoded, so a credential at any nesting depth is found decoded."""
        if isinstance(obj, str):
            if obj.lstrip()[:1] in ("{", "["):
                # RecursionError too: json.loads (and this walk) recurse per nesting level, so a string like
                # "[[[[…" a thousand deep raises it instead of ValueError. Such a string is treated as plain text.
                try:
                    inner = json.loads(obj)
                    if isinstance(inner, (dict, list)):
                        return self.text(json.dumps(self.deep(inner), ensure_ascii=False))
                except (ValueError, RecursionError):
                    pass
            return self.text(obj)
        if isinstance(obj, list):
            return [self.deep(x) for x in obj]
        if isinstance(obj, dict):
            return {self.text(str(k)): self.deep(v) for k, v in obj.items()}
        return obj
