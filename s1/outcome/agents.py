"""Headless runs of the subscription CLIs: an agent doing a task in a workspace, or a stateless text call.

One owner for the child environment (AC-06) and the per-lane command lines. Isolation mirrors the provider bridge:
no user settings, hooks or CLAUDE.md reach a Claude child; no inherited gateway or API key can redirect or bill it.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cortados_router.provider import codex_isolation
from outcome import codex_preflight, store

# ANTHROPIC_BASE_URL / AUTH_TOKEN would loop a child back into a cortados endpoint; ANTHROPIC_API_KEY would bill an
# API key instead of the subscription (the provider's rule, reused: cortados_router/provider/bridge.py).
SCRUBBED_ENV = codex_isolation.CLAUDE_STRIPPED_ENV
# The built-in tools a Claude task run has. No web tools: the outcome must come from the model and the fixture, and
# a networked run could cause a real outward effect.
CLAUDE_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
# Claude Code's documented OS sandbox (code.claude.com/docs/en/sandboxing): Bash and every child it starts may write
# only in the working directory and its temp dir, with no network (empty allowlist). failIfUnavailable refuses to
# start rather than run unsandboxed; allowUnsandboxedCommands=false removes the per-command escape. Edit/Write are
# outside the sandbox: acceptEdits approves them only inside the working directory, and in -p anything needing
# approval is denied. Hooks are off, and no settings file is read.
CLAUDE_SETTINGS = {"disableAllHooks": True,
                   "sandbox": {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
                               "allowUnsandboxedCommands": False, "excludedCommands": [],
                               "network": {"allowedDomains": []}}}
# A Codex task run keeps its shell and patch tools (they run inside the workspace-write Seatbelt sandbox, network
# off); every other integration of the user's Codex home is disabled, as the provider's isolation owner does.
CODEX_TASK_TOOL_FEATURES = ("shell_tool", "unified_exec", "unified_exec_tty")
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


def codex_home() -> str:
    """The Codex home the loop runs on: a dedicated one when configured, else the inherited or the user's own,
    always absolute so the config reader and the child name the same directory (the provider's rule)."""
    home = os.environ.get("CORTADOS_OUTCOME_CODEX_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return os.path.abspath(os.path.expanduser(home))


def _codex_env() -> dict:
    env = codex_isolation.child_env(codex_home())
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    return env


def _journal_root() -> Path:
    root = store.home() / "journals"
    root.mkdir(exist_ok=True)
    return root


def codex_isolation_args(*, keep_tools: tuple = ()) -> list[str]:
    """Overrides that disable every MCP server, integration and (except `keep_tools`) built-in tool of the home."""
    args = []
    for override in (*codex_isolation.CONFIG_OVERRIDES, *codex_preflight.SANDBOX_PINS):
        args += ["-c", override]
    for name in codex_isolation.mcp_server_names(codex_home()):
        args += ["-c", f"mcp_servers.{name}.enabled=false"]
    for feature in codex_isolation.DISABLED_FEATURES:
        if feature not in keep_tools:
            args += ["--disable", feature]
    return args


def _run(argv, *, cwd, timeout, env, stdin=None) -> tuple[int | None, str, str, float]:
    """Run in its own process group, so a timeout kills the CLI and every tool process it started."""
    t0 = time.time()
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(stdin, timeout=timeout)
        return p.returncode, out, err, time.time() - t0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return None, out or "", "timeout", time.time() - t0


def run_task(lane: str, model: str, prompt: str, workspace: str, outward_tools: list, timeout_s: int) -> dict:
    """The agent carries out `prompt` in `workspace`. Returns a trace summary; never raises for a failed run.

    Every agent-writable root is the workspace itself: TMPDIR points inside it (`.tmp`, excluded from the diff), and
    the mock-call journal lives in the data home, which neither sandbox can write, so neither the fixture nor the
    agent can forge or erase outward-call evidence (Codex reviews 2026-10-24)."""
    journal_dir = tempfile.mkdtemp(prefix="calls-", dir=str(_journal_root()))
    calls = os.path.join(journal_dir, "calls.jsonl")
    private_tmp = os.path.join(workspace, ".tmp")
    os.makedirs(private_tmp, exist_ok=True)
    try:
        if lane == "claude":
            mcp = {"mcpServers": {"mock": {"command": sys.executable, "args": [MOCK_SERVER, calls, *outward_tools]}}} \
                if outward_tools else {"mcpServers": {}}
            argv = [claude_bin(), "-p", "--model", model, "--output-format", "stream-json", "--verbose",
                    "--tools", CLAUDE_TOOLS, "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                    *(["--allowedTools", *(f"mcp__mock__{t}" for t in outward_tools)] if outward_tools else []),
                    "--strict-mcp-config", "--mcp-config", json.dumps(mcp), "--setting-sources", "",
                    "--settings", json.dumps(CLAUDE_SETTINGS), "--no-session-persistence"]
            code, out, err, secs = _run(argv, cwd=workspace, timeout=timeout_s, env=child_env({"TMPDIR": private_tmp}),
                                        stdin=prompt)
            trace = _claude_trace(out)
        elif lane == "codex":
            isolation = codex_isolation_args(keep_tools=CODEX_TASK_TOOL_FEATURES)
            env = dict(_codex_env(), TMPDIR=private_tmp)
            codex_preflight.preflight([codex_bin()], isolation, env, CODEX_TASK_TOOL_FEATURES)
            mock = []
            if outward_tools:
                mock = ["-c", f"mcp_servers.outcome_mock.command={json.dumps(sys.executable)}",
                        "-c", f"mcp_servers.outcome_mock.args={json.dumps([MOCK_SERVER, calls, *outward_tools])}"]
            argv = [codex_bin(), "exec", "-m", model, "-s", "workspace-write", *isolation, *mock,
                    "-C", workspace, "--skip-git-repo-check", "--json", "-"]
            code, out, err, secs = _run(argv, cwd=workspace, timeout=timeout_s, env=env, stdin=prompt)
            trace = _codex_trace(out)
        else:
            raise ValueError(f"lane {lane!r} has no task runner")
        trace.update(exit=code, seconds=round(secs, 1), timed_out=code is None,
                     stderr_tail=err[-1000:] if code not in (0, None) else "", outward_calls=_read_calls(calls))
        return trace
    finally:
        shutil.rmtree(journal_dir, ignore_errors=True)


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


def ask_codex(model: str, prompt: str, timeout_s: int = 900, effort: str | None = None) -> str:
    """A tool-free Codex text call in an empty read-only folder: every built-in tool, MCP server and integration of
    the home is off, so mined text in the prompt cannot make it act. Returns its last message."""
    isolation, env = codex_isolation_args(), _codex_env()
    codex_preflight.preflight([codex_bin()], isolation, env, ())
    with tempfile.TemporaryDirectory() as cwd:
        last = os.path.join(cwd, ".last-message.txt")
        argv = [codex_bin(), "exec", "-m", model, "--sandbox", "read-only", *isolation,
                *(["-c", f"model_reasoning_effort={json.dumps(effort)}"] if effort else []),
                "--skip-git-repo-check", "-C", cwd, "--output-last-message", last, "-"]
        code, out, err, _ = _run(argv, cwd=cwd, timeout=timeout_s, env=env, stdin=prompt)
        if code != 0:
            raise RuntimeError(f"codex {model} exited {code}: {(err or out)[-300:]}")
        if not os.path.exists(last):
            raise RuntimeError(f"codex {model} wrote no last message")
        with open(last) as f:
            return f.read()


def json_payload(text: str, opener: str = "[") -> object:
    """The JSON array/object in a model reply (models sometimes wrap it in prose or a fence)."""
    closer = "]" if opener == "[" else "}"
    m = re.search(re.escape(opener) + r"[\s\S]*" + re.escape(closer), text)
    if not m:
        raise ValueError("reply has no JSON payload")
    return json.loads(m.group(0))
