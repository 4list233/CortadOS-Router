"""Credential redaction shared by the child-task runner (child_tasks.Redactor) and the provider's refusal records.

One implementation: every credential and its JSON-escaped spelling (JSON output escapes quotes and backslashes in
it), matched in a single alternation, longest first, so a shorter credential that is a prefix of a longer one never
splits it. Redact BEFORE serializing or truncating: truncating first can cut a credential to a prefix that no longer
matches and leak that prefix.
"""
from __future__ import annotations

import json
import re

MASK = "***"


class Redactor:
    def __init__(self, tokens=()):
        toks = {t for t in tokens if t}
        self.raw = sorted(toks)
        toks |= {json.dumps(t)[1:-1] for t in toks}
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


def excerpt(red: Redactor, s: str, limit: int) -> str:
    """At most `limit` characters of `s`, redacted first and truncated after (see the module docstring)."""
    out = red.text(str(s))
    return out if len(out) <= limit else out[:limit] + "…"
