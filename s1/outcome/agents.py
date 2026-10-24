"""Headless runs of the subscription CLIs: an agent doing a task in a workspace, or a stateless text call.

One owner for the child environment (AC-06) and the per-lane command lines. Isolation mirrors the provider bridge:
no user settings, hooks or CLAUDE.md reach a Claude child; no inherited gateway or API key can redirect or bill it.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ANTHROPIC_BASE_URL / AUTH_TOKEN would loop a child back into a cortados endpoint; ANTHROPIC_API_KEY would bill an
# API key instead of the subscription (same rule as cortados_router/provider/bridge.py).
SCRUBBED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# A task run may read, test and edit inside its workspace, never fetch from the web: the outcome must come from
# the model and the fixture, and a networked run could cause a real outward effect.
CLAUDE_DISALLOWED = ("WebFetch", "WebSearch")
# Bash is limited to inspecting and testing the workspace. A wider allowlist would let a run write outside it.
CLAUDE_BASH_ALLOWED = ("Bash(python3:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(head:*)",
                       "Bash(tail:*)", "Bash(wc:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(node:*)", "Bash(npm test:*)")
MOCK_SERVER = str(Path(__file__).with_name("mock_tools.py"))
FINAL_TEXT_CHARS = 4000


def claude_bin() -> str:
    return os.environ.get("CORTADOS_OUTCOME_CLAUDE_BIN", "claude")


def codex_bin() -> str:
    return os.environ.get("CORTADOS_OUTCOME_CODEX_BIN", "codex")


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    env.update(extra or {})
    return env


def _codex_env() -> dict:
    home = os.environ.get("CORTADOS_OUTCOME_CODEX_HOME")
    return child_env({"CODEX_HOME": home} if home else None)


def _run(argv, *, cwd, timeout, env, stdin=None) -> tuple[int | None, str, str, float]:
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return None, out, "timeout", time.time() - t0


def run_task(lane: str, model: str, prompt: str, workspace: str, outward_tools: list, timeout_s: int) -> dict:
    """The agent carries out `prompt` in `workspace`. Returns a trace summary; never raises for a failed run."""
    calls = os.path.join(workspace, ".outcome-mock-calls.jsonl")
    if lane == "claude":
        mcp = {"mcpServers": {"mock": {"command": sys.executable, "args": [MOCK_SERVER, calls, *outward_tools]}}} \
            if outward_tools else {"mcpServers": {}}
        allowed = [*CLAUDE_BASH_ALLOWED, *(f"mcp__mock__{t}" for t in outward_tools)]
        argv = [claude_bin(), "-p", "--model", model, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                "--disallowedTools", *CLAUDE_DISALLOWED, "--allowedTools", *allowed,
                "--strict-mcp-config", "--mcp-config", json.dumps(mcp),
                "--setting-sources", "", "--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence"]
        code, out, err, secs = _run(argv, cwd=workspace, timeout=timeout_s, env=child_env(), stdin=prompt)
        trace = _claude_trace(out)
    elif lane == "codex":
        overrides = []
        if outward_tools:
            overrides = ["-c", f"mcp_servers.mock.command={json.dumps(sys.executable)}",
                         "-c", f"mcp_servers.mock.args={json.dumps([MOCK_SERVER, calls, *outward_tools])}"]
        argv = [codex_bin(), "exec", "-m", model, "-s", "workspace-write", "-C", workspace, "--skip-git-repo-check",
                "--json", *overrides, "-"]
        code, out, err, secs = _run(argv, cwd=workspace, timeout=timeout_s, env=_codex_env(), stdin=prompt)
        trace = _codex_trace(out)
    else:
        raise ValueError(f"lane {lane!r} has no task runner")
    trace.update(exit=code, seconds=round(secs, 1), timed_out=code is None,
                 stderr_tail=err[-1000:] if code not in (0, None) else "",
                 outward_calls=_read_calls(calls))
    return trace


def _read_calls(path: str) -> list:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    os.unlink(path)                                       # not part of the agent's work product or the diff
    return rows


def _claude_trace(out: str) -> dict:
    tools, final, usage, turns, is_error = collections.Counter(), "", {}, None, None
    for line in out.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "assistant":
            for b in (rec.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools[b.get("name", "?")] += 1
        elif rec.get("type") == "result":
            final, usage, turns, is_error = rec.get("result") or "", rec.get("usage") or {}, rec.get("num_turns"), rec.get("is_error")
    return {"tools": dict(tools), "final": final[-FINAL_TEXT_CHARS:], "usage": usage, "turns": turns, "is_error": is_error}


def _codex_trace(out: str) -> dict:
    kinds, final, usage = collections.Counter(), "", {}
    for line in out.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        item = rec.get("item") if isinstance(rec.get("item"), dict) else None
        if rec.get("type") == "item.completed" and item:
            kinds[item.get("type", "?")] += 1
            if item.get("type") == "agent_message":
                final = item.get("text") or final
        elif rec.get("type") == "turn.completed":
            usage = rec.get("usage") or usage
    return {"tools": dict(kinds), "final": final[-FINAL_TEXT_CHARS:], "usage": usage, "turns": None, "is_error": None}


def ask_claude(model: str, system: str, user: str, timeout_s: int = 900) -> str:
    """A stateless Claude text call: no tools, no session, no user settings. Returns the reply text."""
    argv = [claude_bin(), "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--max-turns", "1",
            "--tools", "", "--disable-slash-commands", "--strict-mcp-config", "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True}), "--system-prompt", system]
    with tempfile.TemporaryDirectory() as cwd:
        code, out, err, _ = _run(argv, cwd=cwd, timeout=timeout_s, env=child_env(), stdin=user)
    if code != 0:
        raise RuntimeError(f"claude {model} exited {code}: {(err or out)[-300:]}")
    reply = json.loads(out)
    if reply.get("is_error"):
        raise RuntimeError(f"claude {model}: {str(reply.get('result'))[:300]}")
    return reply.get("result") or ""


def ask_codex(model: str, prompt: str, timeout_s: int = 900) -> str:
    """A Codex text call in an empty read-only folder. Returns its last message."""
    with tempfile.TemporaryDirectory() as cwd:
        last = os.path.join(cwd, ".last-message.txt")
        argv = [codex_bin(), "exec", "-m", model, "--sandbox", "read-only", "--skip-git-repo-check", "-C", cwd,
                "--output-last-message", last, "-"]
        code, out, err, _ = _run(argv, cwd=cwd, timeout=timeout_s, env=_codex_env(), stdin=prompt)
        if code != 0:
            raise RuntimeError(f"codex {model} exited {code}: {(err or out)[-300:]}")
        return open(last).read() if os.path.exists(last) else out


def json_payload(text: str, opener: str = "[") -> object:
    """The JSON array/object in a model reply (models sometimes wrap it in prose or a fence)."""
    closer = "]" if opener == "[" else "}"
    m = re.search(re.escape(opener) + r"[\s\S]*" + re.escape(closer), text)
    if not m:
        raise ValueError("reply has no JSON payload")
    return json.loads(m.group(0))
