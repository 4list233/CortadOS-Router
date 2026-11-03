"""Isolation policy for the Cursor lane (ticket C1 of docs/CURSOR-LANE.md).

cursor-agent cannot remove its built-in tools, only gate them (C0). Forest's terms (2026-11-03): run headless with
deny-all permissions and ``--sandbox enabled`` in a throwaway per-turn workspace; the client's tools arrive only through
MCP (mcp_shim); ANY built-in tool call that does not come back denied aborts the turn and blocks the lane (hard-fail).

This module builds that workspace and argv, and classifies each stream-json event (``check_event``). Stdlib only.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

SHIM_SERVER = "cortados"

# Every built-in permission kind we know of, denied on every pattern. Shell/Read/Write/WebFetch/Mcp are the documented
# kinds; Glob/Grep are tried because C0 showed glob still executing under a Read deny (it returned an empty listing).
# An unrecognised kind is harmless to list (it denies nothing); check_event is the enforcement, not this list.
DENY_RULES = ("Shell(*)", "Shell(**)", "Read(*)", "Read(**)", "Write(*)", "Write(**)", "Glob(*)", "Glob(**)",
              "Grep(*)", "Grep(**)", "WebFetch(*)", "WebSearch(*)", "Edit(*)", "Edit(**)", "Delete(*)", "Delete(**)")

# Flags that widen what cursor-agent may touch; never on our command line. Reason: each grants host access or
# skips the permission layer the whole lane rests on.
FORBIDDEN_FLAGS = ("--force", "-f", "--yolo", "--worktree", "--add-dir", "--sandbox=disabled", "--browser")


class CursorIsolationError(RuntimeError):
    """A built-in tool ran, or the turn's setup is not the isolated one. The lane must block until fixed."""


def cli_config(server: str = SHIM_SERVER) -> dict[str, Any]:
    return {"permissions": {"allow": [f"Mcp({server}:*)"], "deny": list(DENY_RULES)}}


def build_workspace(shim_command: list[str], server: str = SHIM_SERVER) -> str:
    """A fresh 0700 dir holding only .cursor/cli.json (deny-all) and .cursor/mcp.json (the shim). No AGENTS.md/rules."""
    if not shim_command:
        raise CursorIsolationError("shim command is empty")
    ws = tempfile.mkdtemp(prefix="cortados-cursor-")
    os.chmod(ws, 0o700)
    cdir = os.path.join(ws, ".cursor")
    os.mkdir(cdir, 0o700)
    with open(os.path.join(cdir, "cli.json"), "w") as f:
        json.dump(cli_config(server), f)
    mcp = {"mcpServers": {server: {"command": shim_command[0], "args": list(shim_command[1:])}}}
    with open(os.path.join(cdir, "mcp.json"), "w") as f:
        json.dump(mcp, f)
    return ws


def build_argv(binary: str, model: str, workspace: str, prompt: str) -> list[str]:
    # cursor-agent documents no switch that skips user rules or ~/.cursor/mcp.json; that is C2's preflight (dedicated
    # HOME + CURSOR_API_KEY, or refuse when ~/.cursor/mcp.json lists servers). --workspace pins the trusted root.
    argv = [binary, "-p", "--output-format", "stream-json", "--trust", "--sandbox", "enabled", "--approve-mcps",
            "--model", model, "--workspace", workspace, prompt]
    check_command_line(argv[:-1])
    return argv


def check_command_line(args: list[str]) -> None:
    for a in args:
        if a in FORBIDDEN_FLAGS or a.split("=", 1)[0] in ("--force", "--yolo", "--worktree", "--add-dir"):
            raise CursorIsolationError(f"forbidden cursor-agent flag: {a}")
    if "--sandbox" not in args or args[args.index("--sandbox") + 1] != "enabled":
        raise CursorIsolationError("cursor-agent must run with --sandbox enabled")


# A refusal by cursor-agent's own permission layer. C0: shell -> permissionDenied, read -> error "Permission denied".
DENIED_RESULTS = ("permissionDenied", "rejected", "denied")


def _is_denied(result: Any) -> bool:
    if not isinstance(result, dict) or len(result) != 1:
        return False
    (kind, body), = result.items()
    if kind in DENIED_RESULTS:
        return True
    if kind == "error":
        text = json.dumps(body).lower()
        return "permission denied" in text or "blocked by permission" in text
    return False


def check_event(event: dict[str, Any], server: str = SHIM_SERVER) -> None:
    """Raise CursorIsolationError if this stream-json event shows a built-in tool that was not denied."""
    if not isinstance(event, dict):
        raise CursorIsolationError(f"non-object stream event: {event!r:.200}")
    etype = event.get("type")
    looks_tool = etype == "tool_call" or any("tool" in str(k).lower() for k in event if k not in ("type", "subtype"))
    if not looks_tool:
        return
    call = event.get("tool_call")
    # Real shape (C1 run): {"getMcpToolsToolCall": {...}, "hookAdditionalContexts": [], "toolCallId": ..., "startedAtMs": ...}
    # Exactly one "*ToolCall" key names the tool; anything else is unknown and fails closed.
    kinds = [k for k in call if str(k).endswith("ToolCall")] if isinstance(call, dict) else []
    if etype != "tool_call" or len(kinds) != 1:
        raise CursorIsolationError(f"unrecognised tool-like event: {json.dumps(event)[:300]}")
    kind, body = kinds[0], call[kinds[0]]
    body = body if isinstance(body, dict) else {}
    if kind == "mcpToolCall":
        name = str(body.get("args", {}).get("name") or body.get("args", {}).get("toolName") or "")
        prov = str(body.get("args", {}).get("providerIdentifier") or body.get("args", {}).get("serverName") or "")
        if prov == server or name.startswith(server + "-"):
            return
        raise CursorIsolationError(f"MCP call to a server other than the shim: {json.dumps(body.get('args'))[:200]}")
    if kind == "getMcpToolsToolCall":
        # Allowed: cursor-agent's internal schema lookup for MCP tools (C0: runs before every mcpToolCall). It reads
        # only the tool list of the MCP servers this workspace configured (the shim alone), touches no file, shell or
        # network, so it has no host effect. Denying it would make the shim unusable. It is still pinned to our server.
        srv = str(body.get("args", {}).get("server") or body.get("args", {}).get("serverName") or server)
        if srv != server:
            raise CursorIsolationError(f"MCP schema lookup for a non-shim server: {srv}")
        return
    if event.get("subtype") == "started" or "result" not in body:
        return  # nothing has run yet; the completed event carries the verdict
    if _is_denied(body["result"]):
        return
    raise CursorIsolationError(f"built-in tool {kind} was not denied: {json.dumps(body)[:300]}")


def check_stream(lines: list[str], server: str = SHIM_SERVER) -> list[dict[str, Any]]:
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        check_event(ev, server)
        events.append(ev)
    return events
