"""Seeds from Forest's own work: the first real request of each Claude Code, Cowork and Codex session (AC-01, AC-02).

Transcripts are external Evidence: read-only, never copied whole. A seed keeps the request text (clipped to S1's
request clip) and the names of the tools the session went on to use, so generated tasks resemble real work.

  python3 -B -m outcome.mine [--home ~]
"""
from __future__ import annotations

import argparse
import collections
import json
import platform
from pathlib import Path

import common
from cortados_router.provider.responses_wire import CODEX_INJECTED_PREFIXES
from cortados_router.provider.routing import strip_injected
from outcome import store

# S1's request clip (core.decide_s1 sends request[:6000]): a longer seed is clipped by the scorer anyway.
SEED_CHARS = 6000
# Claude Code writes local slash-command echoes and their caveat as user messages; they are not requests.
CLAUDE_NOISE_PREFIXES = ("<command-", "<local-command", "Caveat:", "[Request interrupted")
# The loop's own prompts, should a session of it ever be persisted (sessions before --ephemeral were): the
# generator's instruction and the judge's. Mining them would feed the loop its own synthetic work.
LOOP_PROMPT_PREFIXES = ("You turn real requests into runnable benchmark tasks", "You are now the JUDGE and TEACHER")


def _own_prompts() -> set:
    """Prompts the loop itself produced (task prompts): a session that ran one is benchmark work, not a seed."""
    return {t["prompt"].strip() for t in store.read("tasks") if isinstance(t.get("prompt"), str)}


def _sources(home: Path) -> list[tuple[str, Path]]:
    claude = sorted((home / ".claude/projects").glob("*/*.jsonl"))
    cowork = sorted((home / "Library/Application Support/Claude/local-agent-mode-sessions").glob("**/audit.jsonl"))
    codex = sorted((home / ".codex/sessions").glob("**/*.jsonl"))
    return [("claude-code", p) for p in claude] + [("cowork", p) for p in cowork] + [("codex", p) for p in codex]


def _lines(path: Path):
    bad = 0
    with path.open(errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                bad += 1
    if bad:
        yield {"_unreadable": bad}


def _claude_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _claude_session(path: Path) -> tuple[str | None, collections.Counter, int]:
    """Claude Code and Cowork share the Claude stream shape: user/assistant records with a `message`."""
    first, tools, unreadable = None, collections.Counter(), 0
    for rec in _lines(path):
        unreadable += rec.get("_unreadable", 0)
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        if msg is None:
            continue
        if rec.get("type") == "assistant" and isinstance(msg.get("content"), list):
            tools.update(b.get("name", "") for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_use")
        if first is None and rec.get("type") == "user" and msg.get("role") == "user" and not rec.get("isMeta"):
            text = strip_injected(_claude_text(msg.get("content")))
            if text and not text.startswith(CLAUDE_NOISE_PREFIXES):
                first = text
    return first, tools, unreadable


def _codex_session(path: Path) -> tuple[str | None, collections.Counter, int]:
    first, tools, unreadable = None, collections.Counter(), 0
    for rec in _lines(path):
        unreadable += rec.get("_unreadable", 0)
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else None
        if rec.get("type") != "response_item" or p is None:
            continue
        if p.get("type") in ("function_call", "custom_tool_call") and p.get("name"):
            tools[p["name"]] += 1
        if first is None and p.get("type") == "message" and p.get("role") == "user":
            texts = [c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict) and c.get("type") == "input_text"]
            texts = [t for t in texts if t.strip() and not t.lstrip().startswith(CODEX_INJECTED_PREFIXES)]
            if texts:
                first = strip_injected("\n".join(texts))
    return first, tools, unreadable


def mine(home: Path, host: str | None = None) -> dict:
    """Append one seed per session not seen before. Returns counts, including unreadable lines (never fatal).

    `home` is a user home or a copy of one (the Pro's transcripts copied to the Studio); `host` names whose it is."""
    have = store.ids("seeds")
    host = host or platform.node()
    own = _own_prompts()
    counts = collections.Counter()
    for source, path in _sources(home):
        read = _codex_session if source == "codex" else _claude_session
        try:
            text, tools, unreadable = read(path)
        except OSError:
            counts["unreadable_files"] += 1
            continue
        counts["unreadable_lines"] += unreadable
        text = common.clean_request(text[:SEED_CHARS]) if text else None
        if not text:
            counts["no_request"] += 1
            continue
        if text.startswith(LOOP_PROMPT_PREFIXES) or text.strip() in own:
            counts["loop_own"] += 1                    # the loop's own generation or benchmark prompt, not real work
            continue
        seed_id = store.digest_id("seed", host, str(path))
        if seed_id in have:
            counts["already"] += 1
            continue
        store.append("seeds", {"id": seed_id, "source": source, "host": host, "session": path.stem, "text": text,
                               "tools": [n for n, _ in tools.most_common(20) if n]})
        have.add(seed_id)
        counts[f"new_{source}"] += 1
    return dict(counts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(Path.home()))
    ap.add_argument("--host", default=None)
    a = ap.parse_args()
    with store.single_instance():
        print(json.dumps(mine(Path(a.home).expanduser(), a.host)))


if __name__ == "__main__":
    main()
