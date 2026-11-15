"""Control-character-safe text at the SINKS: every log record and every error message the provider, the child-task
tools and the MCP servers hand to a client (#117 review round 4).

Rounds 3 and 4 escaped client strings call site by call site (anthropic_wire.shown_name / log_safe); each review found
more sites. This module closes the class where the text leaves the process instead:

- `SafeFormatter` wraps a handler's formatter: the formatted record (message, arguments, traceback) has every control
  and line-separator character escaped and is capped, so no log record can carry a raw newline, whatever a call site
  passed. `install_log_sanitizer()` puts it on every handler of the root logger and on logging.lastResort; the
  provider calls it at startup (server.serve).
- `safe_error_text()` is applied by every error builder (anthropic_wire.error_json, responses_wire.error_json /
  sse_error, local_lane._error, subagent_mcp._err and its JSON-RPC errors, mcp_shim's replies): the DECODED text a
  model or user sees has no raw newline from client input; it shows as a visible `\\n`.
- `safe_error_body()` does the same for an upstream HTTP error body relayed to a client (anthropic_proxy, local_lane;
  #117 review round 5): its error message escaped and capped, every other field as sent. Successful answers never pass
  through it.

Escaping keeps every printable character (backslashes too, so a Windows path stays readable) and turns each other one
into a Python-style escape: `\\n`, `\\r`, `\\t`, `\\xNN`, `\\uNNNN`, `\\UNNNNNNNN`. "Other" is what str.isprintable
rejects except the plain space: C0/C1 controls, DEL, U+2028/U+2029, bidi and other format characters, non-ASCII
spaces. Stdlib only. subagent_mcp.py and provider/mcp_shim.py run standalone (as scripts, without the package), so each
carries a copy of `escape_controls`; tests/test_safe_text.py pins the copies to this one.
"""
from __future__ import annotations

import json
import logging
from typing import Any

ERROR_CAP = 2000            # an error message shown to a client: room for a routing error's pin list, never a flood
LOG_RECORD_CAP = 16000      # one log record, traceback included


def _escape_char(c: str) -> str:
    if c == "\n":
        return "\\n"
    if c == "\r":
        return "\\r"
    if c == "\t":
        return "\\t"
    o = ord(c)
    if o < 0x100:
        return f"\\x{o:02x}"
    if o < 0x10000:
        return f"\\u{o:04x}"
    return f"\\U{o:08x}"


def escape_controls(value: Any, cap: int) -> str:
    """`value` (str() of it if not a string) with every non-printable character but the plain space escaped, at most
    `cap` characters of escaped text plus a '...(+N chars)' suffix when cut. Never contains a raw control character."""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:                   # a hostile __str__ never breaks an error or log path
            value = f"<{type(value).__name__}>"
    head = value[:cap]                      # escaping only lengthens: what lies past `cap` input chars is cut anyway
    out = head if head.isprintable() else "".join(c if c == " " or c.isprintable() else _escape_char(c) for c in head)
    if len(out) > cap:
        out = out[:cap]
        if out.endswith("\\"):              # never end on a lone backslash cut out of an escape
            out = out[:-1]
    return out + (f"...(+{len(value) - len(head)} chars)" if len(value) > cap else "")


QUOTED_CAP = 120


def quoted(value: Any, cap: int = QUOTED_CAP) -> str:
    """A client string inside provider-authored text: repr of at most `cap` characters of it (so quoted, every control
    character escaped), with a '...(+N chars)' suffix when cut; a non-string through escape_controls(repr(value))."""
    if isinstance(value, str):
        return escape_controls(repr(value[:cap]), 10 * cap + 2) + (
            f"...(+{len(value) - cap} chars)" if len(value) > cap else "")
    try:
        r = repr(value)
    except Exception:
        r = f"<{type(value).__name__}>"
    return escape_controls(r, cap)


def safe_error_text(message: Any, cap: int = ERROR_CAP) -> str:
    """An error message as a client may see it: control characters escaped visibly, length capped."""
    return escape_controls(message, cap)


def safe_error_body(raw: bytes, cap: int = ERROR_CAP) -> bytes | None:
    """An upstream HTTP error body (a JSON object) as a client may see it: each error MESSAGE string through
    safe_error_text, every other field left as sent (#117 review round 5). The message is found where the APIs put it:
    `error.message` (Anthropic, OpenAI), `error` as a string (Ollama's native shape) and a top-level `message`. A
    message that is not a string becomes its escaped JSON text. None when `raw` is not a JSON object: the caller then
    builds its own error. Only for ERROR bodies; a successful answer never passes through here."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None

    def clean(v: Any) -> str:
        return safe_error_text(v if isinstance(v, str) else json.dumps(v), cap)

    err = body.get("error")
    if isinstance(err, dict) and "message" in err:
        body["error"] = {**err, "message": clean(err["message"])}
    elif isinstance(err, str):
        body["error"] = clean(err)
    if "message" in body:
        body["message"] = clean(body["message"])
    return json.dumps(body).encode()


class SafeFormatter(logging.Formatter):
    """Formats with the wrapped formatter, then escapes control characters in the WHOLE record (a traceback becomes one
    line with visible `\\n`s) and caps it. The handler adds the record's single trailing newline (its terminator)."""

    def __init__(self, inner: logging.Formatter | None = None, cap: int = LOG_RECORD_CAP):
        super().__init__()
        self.inner = inner or logging.Formatter()
        self.cap = cap

    def format(self, record: logging.LogRecord) -> str:
        try:
            text = self.inner.format(record)
        except Exception:                   # a bad %-format at a call site: still one safe line, never a raw one
            text = f"<unformattable log record from {record.name}:{record.lineno}> {record.msg!r}"
        return escape_controls(text, self.cap)


def install_log_sanitizer(logger: logging.Logger | None = None) -> int:
    """Wrap the formatter of every handler on `logger` (default the root logger) and of logging.lastResort in a
    SafeFormatter. Idempotent. Returns how many handlers it wrapped now."""
    target = logger or logging.getLogger()
    handlers = list(target.handlers)
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    wrapped = 0
    for h in handlers:
        if not isinstance(h.formatter, SafeFormatter):
            h.setFormatter(SafeFormatter(h.formatter))
            wrapped += 1
    return wrapped


def handlers_sanitized(logger: logging.Logger | None = None) -> bool:
    """True when every handler of `logger` (default root) and logging.lastResort formats through a SafeFormatter."""
    target = logger or logging.getLogger()
    handlers = list(target.handlers) + ([logging.lastResort] if logging.lastResort is not None else [])
    return all(isinstance(h.formatter, SafeFormatter) for h in handlers)
