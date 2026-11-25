"""Credential redaction for the child-task runner (child_tasks.Redactor builds on this class).

Threat model: this protects against ACCIDENTAL credential exposure in logs and tool results (a token echoed by a
tool, quoted in an error, nested in JSON a tool printed) at any realistic encoding depth, and fails closed on anything
it cannot parse (WITHHELD). A whole credential left in a common encoding (unicode or other escapes, percent-encoding,
base64) is caught too and withheld (Redactor.encoded_hit). Encodings a child builds on purpose to smuggle out its own
token (splitting, arithmetic, other encodings) are out of scope: the child already holds that token in its environment, and the same OS user can read
the token file. The real fix for that is per-task scoped tokens, which the provider does not offer yet.

One implementation: every credential and its JSON-escaped spelling (JSON output escapes quotes and backslashes in
it), matched in a single alternation, longest first, so a shorter credential that is a prefix of a longer one never
splits it. Redact BEFORE serializing or truncating: truncating first can cut a credential to a prefix that no longer
matches and leak that prefix.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse

MASK = "***"
# What replaces content that cannot be redacted structurally (too deep to decode or walk, or not JSON where JSON was
# required): never a partial text redaction, which could miss a credential escaped more often than ESCAPE_DEPTH.
WITHHELD = "[withheld: content could not be safely redacted]"
# Escaping layers matched per credential: JSON inside a JSON string escapes it again at each level (a tool result
# holding JSON text, inside claude's stream-json event, is two levels). Four covers every nesting seen in claude's
# output with room to spare (8: backslashes double per layer, so this stays cheap); the structural pass (deep) decodes
# embedded JSON so its depth is relative anyway, and fails closed (WITHHELD) where it cannot decode.
ESCAPE_DEPTH = 8
# Why 16: a base64 fragment shorter than this could turn up by chance in ordinary text and withhold it; a provider
# token's fragment is far longer (a 32-character token leaves 40 stable characters at any alignment).
BASE64_MIN_FRAGMENT = 16
# The lookbehind lets a match start only at the first backslash of a run, so each run is scanned once: unanchored,
# `\\+u` retried at every backslash of a long run that is not followed by `u`, quadratic in the run's length (128k
# backslashes took 7 s; transcript review round 2). The leftmost match starts at the run's first backslash anyway, so
# what is matched is unchanged.
_UNI_ESC = re.compile(r"(?<!\\)\\+u([0-9a-fA-F]{4})")
_HEX_ESC = re.compile(r"(?<!\\)\\+x([0-9a-fA-F]{2})")
_ANY_ESC = re.compile(r"\\+(.)", re.S)


def _base64_fragments(token: str) -> set:
    """The base64 text that encodes `token` whatever its alignment in the encoded data: for each of the three byte
    offsets, the characters of the 4-character groups that hold token bytes only (standard and URL-safe alphabets)."""
    raw = token.encode("utf-8")
    out = set()
    for off in range(3):
        enc = base64.b64encode(b"\0" * off + raw).decode("ascii")
        first = -(-off // 3)                                  # first group that starts inside the token
        last = (off + len(raw)) // 3                           # groups before this one end inside the token
        frag = enc[4 * first:4 * last]
        if len(frag) >= BASE64_MIN_FRAGMENT:
            out.add(frag)
            out.add(frag.replace("+", "-").replace("/", "_"))
    return out


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
        self._b64 = sorted({f for t in self.raw for f in _base64_fragments(t)}, key=len, reverse=True)

    def text(self, s: str) -> str:
        return self._re.sub(MASK, s) if self._re else s

    def encoded_hit(self, s: str) -> bool:
        """True when `s` still holds a known credential in an encoded form text() does not match: escaped
        characters (`\\uXXXX`, `\\xXX`, a backslash before any character, at any escaping depth), percent-encoding
        (up to ESCAPE_DEPTH layers, mixed with escapes) or base64 of the raw credential. Callers withhold such text
        (fail closed): masking an encoded spelling in place is not attempted. Run it on text() output."""
        if not self.raw or not isinstance(s, str):
            return False
        if any(f in s for f in self._b64):
            return True
        if "\\" not in s and "%" not in s:
            return False
        cur = s
        for _ in range(ESCAPE_DEPTH):
            nxt = urllib.parse.unquote(cur, errors="replace") if "%" in cur else cur
            if "\\" in nxt:
                nxt = _UNI_ESC.sub(lambda m: chr(int(m.group(1), 16)), nxt)
                nxt = _HEX_ESC.sub(lambda m: chr(int(m.group(1), 16)), nxt)
                nxt = _ANY_ESC.sub(lambda m: m.group(1), nxt)
            if nxt == cur:
                break
            if any(t in nxt for t in self.raw):
                return True
            cur = nxt
        return False

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
        """deep_count's value only."""
        return self.deep_count(obj)[0]

    def deep_count(self, obj):
        """(value, withheld): like tree, and a string that is itself JSON (an object or array, e.g. a tool result
        holding JSON text) is decoded, redacted the same way and re-encoded, so a credential at any nesting depth is
        found decoded. Fail closed: a JSON-looking string that cannot be decoded and walked for depth (RecursionError,
        ~1000 levels) is replaced wholesale by WITHHELD and counted, never left to text(), which matches only
        ESCAPE_DEPTH escaping layers; a structure too deep to walk at all comes back as WITHHELD."""
        counter = [0]
        try:
            value = self._deep(obj, counter)
        except RecursionError:
            counter[0] += 1
            value = WITHHELD
        return value, counter[0]

    def _deep(self, obj, counter):
        if isinstance(obj, str):
            return self._deep_str(obj, counter)
        if isinstance(obj, list):
            return [self._deep(x, counter) for x in obj]
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                before = counter[0]
                key = self._deep_str(str(k), counter)
                if counter[0] != before or key in out:
                    # A key that could not be redacted safely, or two keys that redact to the same name (one would
                    # silently replace the other): the whole object is withheld, counted once.
                    counter[0] = before + 1
                    return WITHHELD
                out[key] = self._deep(v, counter)
            return out
        return obj

    def _deep_str(self, s: str, counter) -> str:
        """A string, structurally: JSON text (an object, array or string literal, after leading whitespace) is decoded,
        redacted the same way (recursively, so a chain of quoted strings is unwound) and re-encoded; plain text gets
        text(). Anything that looks like JSON but does not decode, or is too deep to decode or walk, is WITHHELD and
        counted: never left to text(), which matches only ESCAPE_DEPTH escaping layers."""
        if s.lstrip()[:1] not in ("{", "[", '"'):
            out = self.text(s)
            if self.encoded_hit(out):           # a credential in an encoded spelling text() cannot mask: withheld
                counter[0] += 1
                return WITHHELD
            return out
        try:
            inner = json.loads(s)
            if not isinstance(inner, (dict, list, str)):
                raise ValueError("not an object, array or string")
            return json.dumps(self._deep(inner, counter))
        except (ValueError, RecursionError):
            counter[0] += 1
            return WITHHELD
