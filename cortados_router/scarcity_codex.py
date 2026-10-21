"""The Codex CLI /status screen: the one parser (SUBAGENT-MCP-PLAN §11.7, built 2026-10-21 on a real 0.163.0 capture).

A pool label is the text on the SAME LINE as "limit:", matched against a strict grammar. The 2026-10-13 breakage:
codex 0.162 put "Session: <uuid>" directly above "Weekly limit:", and the old whitespace-collapsing regex took the
uuid as a model name, so every sample became its own pool; the reset, now written "12:01 PM on 27 Oct", was never
read, so the week always looked under a day from its reset.

Rules: an unknown label, unreadable or leftover reset text, a reset outside its window, or a CLI version this parser
was not verified against fails loudly, and the sample is then stale, never fresh. Nothing is guessed. Every pinned
version has its real redacted screen in tests/fixtures/codex-status-<version>.txt.
Loaded by path from scarcity.py (this plugin is not a package); it imports nothing from there.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

PINNED_VERSIONS = ("0.163.0",)                                     # screen layouts verified against a real capture
VERSION = re.compile(r"OpenAI Codex \(v(\d+\.\d+\.\d+)\)")
LABEL = re.compile(r"^(?:(GPT-\d+(?:\.\d+)*(?:-[A-Za-z][A-Za-z0-9]*)*) )?(5h|Weekly)$")
LIMIT_LINE = re.compile(r"^[\s│|]*(?P<label>[^\[\]]*?) limit: \[[^\]]*\](?P<rest>.*)$")
ENTRY = re.compile(r"^(\d{1,3})% left(?: \(resets (?P<when>[^()]+)\))?$")   # the whole entry: nothing missing, nothing trailing, "(resets )" is not "no reset"
FIELD = re.compile(r"^[\s│|]*[A-Za-z0-9][A-Za-z0-9 .\-]*: ")                  # "Name: value" (also "5h limit:"): the next field ends an entry's wrap
ANY_LIMIT = re.compile(r"\blimit:", re.I)
RESET = re.compile(r"^(\d{1,2}):(\d{2})(?: ?([AaPp][Mm]))?(?: on (\d{1,2}) ([A-Z][a-z]{2}))?$")
SPAN_S = {"5h": 5 * 3600.0, "Weekly": 7 * 86400.0}
SLACK_S = 3600.0            # the screen shows minutes, and a reset can sit a few minutes past the window's nominal length
TAIL_LINES = 4              # a wrapped entry ("…]", "100%", "left (resets …)") never spans more lines than this


def version(text: str) -> str:
    m = VERSION.search(text)
    if not m:
        raise ValueError("Codex /status screen shows no 'OpenAI Codex (vX.Y.Z)' line")
    if m.group(1) not in PINNED_VERSIONS:
        raise ValueError(f"Codex CLI v{m.group(1)} is not one this parser was verified against {PINNED_VERSIONS}; sample refused")
    return m.group(1)


def reset_time(when: str | None, span: str, left: int, now: datetime) -> float | None:
    """When the window resets, as a timestamp. Missing text is allowed only for an untouched window (100 % left: the
    screen runs no reset clock); otherwise the text must read fully and land inside the window, or the sample fails."""
    if when is None:                                                # the group was absent; an empty "(resets )" never gets here
        if left >= 100:
            return None
        raise ValueError(f"{span} limit shows {left}% left but no reset time")
    m = RESET.match(" ".join(when.split()))
    if not m:
        raise ValueError(f"unreadable reset text {when!r}")
    hour, minute, meridiem = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if not (0 <= minute <= 59 and (1 <= hour <= 12 if meridiem else 0 <= hour <= 23)):   # validate before converting: "25:00 PM" is not 13:00
        raise ValueError(f"impossible clock time in reset text {when!r}")
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if m.group(4):
        reset = reset.replace(month=datetime.strptime(m.group(5), "%b").month, day=int(m.group(4)))
        if reset < now - timedelta(days=1):                        # a January date read in December
            reset = reset.replace(year=now.year + 1)
    elif reset < now - timedelta(seconds=SLACK_S):                 # time only (5-hour windows): the next such time. The screen shows minutes and the
        reset += timedelta(days=1)                                  # clocks differ, so a time inside the past hour is the reset just passed (as the date form reads it), not tomorrow's
    ahead = (reset - now).total_seconds()
    if not -SLACK_S <= ahead <= SPAN_S[span] + SLACK_S:            # a week's reset on the week's last day is still inside
        raise ValueError(f"{span} reset {when!r} is {ahead / 3600:.1f} h away, outside its {SPAN_S[span] / 3600:.0f}-hour window")
    return reset.timestamp()


def parse_status(text: str, now: float | None = None) -> list[dict]:
    """Every '<label> limit: […] N% left (resets …)' entry on the screen, each label read from its own line.
    Returns [{span: "5-hour"|"week", model: "" | "GPT-…", left_pct, reset_at, version}]."""
    at = datetime.fromtimestamp(now) if now else datetime.now()
    ver = version(text)
    lines = text.splitlines()
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = LIMIT_LINE.match(line)
        if not m:
            if ANY_LIMIT.search(line):                              # a quota line the grammar cannot read must not vanish silently
                raise ValueError(f"unreadable limit line {line.strip()!r} on the Codex /status screen")
            continue
        label = " ".join(m.group("label").split())                  # runs of spaces inside a label collapse before matching
        g = LABEL.match(label)
        if not g:
            raise ValueError(f"unknown limit label {label!r} on the Codex /status screen; refusing to guess a pool")
        model, span = g.group(1) or "", g.group(2)
        # The entry's "N% left (resets …)" may wrap onto the next lines. It ends at the next "Name: value" field, a blank
        # line or TAIL_LINES, and must then read completely: nothing missing, nothing trailing, nothing borrowed from
        # the entry below it.
        parts = [m.group("rest")]
        for nxt in lines[i + 1:i + 1 + TAIL_LINES]:
            if not nxt.strip() or FIELD.match(nxt) or ANY_LIMIT.search(nxt):
                break
            parts.append(nxt)
        entry_text = " ".join(" ".join(p.split()) for p in parts).strip(" │|")
        e = ENTRY.match(entry_text)
        if not e:
            raise ValueError(f"'{label} limit' entry unreadable: {entry_text!r}")
        left = int(e.group(1))
        if left > 100:
            raise ValueError(f"'{label} limit' shows {left}% left")
        entry = {"span": "5-hour" if span == "5h" else "week", "model": model, "left_pct": left,
                 "reset_at": reset_time(e.group("when"), span, left, at), "version": ver}
        # a repainted screen shows an entry more than once: the latest paint is the screen's final state
        out = [e for e in out if (e["span"], e["model"]) != (entry["span"], model)] + [entry]
    if not out:
        raise ValueError("no 'limit: […] N% left' entry on the Codex /status screen")
    return out
