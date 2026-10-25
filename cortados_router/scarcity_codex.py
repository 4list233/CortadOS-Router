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
import time
from datetime import datetime, timedelta

PINNED_VERSIONS = ("0.163.0", "0.164.0")                           # screen layouts verified against a real capture
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


def reset_time(when: str | None, span: str, left: int, now: float) -> float | None:
    """When the window resets, as a timestamp. Missing text is allowed only for an untouched window (100 % left: the
    screen runs no reset clock); otherwise the text must read fully and land inside the window, or the sample fails.

    The screen gives a wall-clock time, with a day and month for a week and nothing else for a 5-hour window, and no
    year. Each calendar reading it could mean (yesterday, today, tomorrow; last year, this year, next year) becomes a
    timestamp, and the earliest one not more than SLACK_S in the past wins: a reset inside the past hour is the one the
    screen was painted before ("11:59 PM" read at 00:00:01, "31 Dec" read on 1 Jan), anything older is the next
    occurrence. Elapsed seconds, never naive date arithmetic, so a DST change neither refuses a reset 45 minutes past
    nor admits one seven hours ahead (fourth adversarial review, 2026-10-21)."""
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
    today = datetime.fromtimestamp(now)                             # the local calendar the screen is written in
    if m.group(4):
        month, day = datetime.strptime(m.group(5), "%b").month, int(m.group(4))
        dates = [(today.year + k, month, day) for k in (-1, 0, 1)]
    else:
        dates = [(today + timedelta(days=k)).timetuple()[:3] for k in (-1, 0, 1)]
    stamps = set()
    for y, mo, d in dates:
        for fold in (0, 1):                                         # the hour that repeats when DST ends: both readings
            try:
                ts = datetime(y, mo, d, hour, minute, fold=fold).timestamp()
            except ValueError:                                      # 29 Feb in a year without one
                continue
            if datetime.fromtimestamp(ts).timetuple()[:5] != (y, mo, d, hour, minute):   # a time DST skips normalises instead of failing: not on the calendar
                continue
            stamps.add(ts)
    candidates = sorted(ts for ts in stamps if ts - now >= -SLACK_S)
    if not candidates:
        raise ValueError(f"reset text {when!r} has no reading on the calendar")
    ahead = candidates[0] - now
    if ahead > SPAN_S[span] + SLACK_S:                              # a week's reset on the week's last day is still inside
        raise ValueError(f"{span} reset {when!r} is {ahead / 3600:.1f} h away, outside its {SPAN_S[span] / 3600:.0f}-hour window")
    if len(candidates) > 1 and candidates[1] - now <= SPAN_S[span] + SLACK_S:   # only the repeated hour puts two readings inside one window
        raise ValueError(f"reset text {when!r} names two moments inside the {span} window (the hour DST repeats); refusing to guess")
    return candidates[0]


def parse_status(text: str, now: float | None = None) -> list[dict]:
    """Every '<label> limit: […] N% left (resets …)' entry on the screen, each label read from its own line.
    Returns [{span: "5-hour"|"week", model: "" | "GPT-…", left_pct, reset_at, version}]."""
    at = now or time.time()
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
