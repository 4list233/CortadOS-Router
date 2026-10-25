"""One headless Claude CLI per client conversation, with the client's tools bridged in through mcp_shim.

Owner of the rule that the headless CLI never runs a tool itself: it starts with no built-in tools, no user or
project settings, no hooks and an empty scratch directory, and its only tools are the client's, served by the shim.
A turn ends either when the CLI finishes (end_turn) or when it calls a bridged tool the client has not answered yet
(tool_use); the client's next request carries the results, which unblock the waiting shim calls.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

BRIDGE_SERVER = "bridge"
TOOL_PREFIX = f"mcp__{BRIDGE_SERVER}__"
SHIM = Path(__file__).with_name("mcp_shim.py")
# Dropped from the child's environment. BASE_URL and AUTH_TOKEN would send the child back into this endpoint;
# API_KEY would bill an API key instead of the CLI's subscription login.
STRIPPED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# After the first unanswered tool call, the CLI's stdout may still be delivering other tool_use blocks of the same
# assistant message (the shim's HTTP call and the CLI's stdout race). Wait this long for stdout to go quiet.
SETTLE_S = 0.3
logger = logging.getLogger(__name__)


class BridgeError(RuntimeError):
    """The CLI died, timed out or reported an error; the server answers 502 (or 409 for a lost session)."""


def write_line(proc: subprocess.Popen, line: str, timeout: float, who: str) -> None:
    """Write one line to a child's stdin under a deadline. A child that stops reading stdin would block the write
    forever once the pipe buffer fills, so the write runs on its own thread and the child is killed if it misses the
    deadline. The one owner of that rule for both bridges (the Claude CLI and the Codex app-server)."""
    failed: list[BaseException] = []

    def write():
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            failed.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(max(timeout, 0))
    if writer.is_alive():
        proc.kill()
        raise BridgeError(f"{who} did not read its input within {timeout:.0f}s; killed")
    if failed:
        raise BridgeError(f"{who} is gone: {failed[0]}")


class BridgeSession:
    def __init__(self, *, system: str, tools: list[dict[str, Any]], gateway_url: str, token: str,
                 claude_bin: str | list[str] = "claude", model: str | None = None, scratch_root: str | None = None,
                 config_dir: str | None = None, popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.config_dir = config_dir            # CLAUDE_CONFIG_DIR of the routed account; None = the default login
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()                    # one client request at a time per conversation
        self.last_used = time.time()
        self.emitted: set[str] = set()                  # tool_use ids handed to the client
        self.history: list[str] = []                    # reply_fingerprint of each reply, in order
        self._results: dict[str, dict[str, Any]] = {}
        self._waiting: set[str] = set()                 # tool_use ids a shim call is blocked on
        self._cv = threading.Condition()
        self._closed = False
        self._events: queue.Queue = queue.Queue()
        self.dir = Path(tempfile.mkdtemp(prefix="cortados-bridge-", dir=scratch_root))
        self._stderr = None
        prefix = [claude_bin] if isinstance(claude_bin, str) else list(claude_bin)   # a list lets tests run a fake CLI
        try:
            (self.dir / "work").mkdir()
            args = prefix + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                    "--tools", "", "--setting-sources", "", "--settings", json.dumps({"disableAllHooks": True}),
                    "--system-prompt", system or "You are a helpful assistant."]
            if tools:
                (self.dir / "tools.json").write_text(json.dumps(tools))
                (self.dir / "mcp.json").write_text(json.dumps({"mcpServers": {BRIDGE_SERVER: {
                    "command": sys.executable,
                    "args": ["-I", str(SHIM), gateway_url, self.id, str(self.dir / "tools.json")],
                    "env": {"CORTADOS_PROVIDER_TOKEN": token}}}}))
                args += ["--strict-mcp-config", "--mcp-config", str(self.dir / "mcp.json"),
                         "--allowedTools", ",".join(TOOL_PREFIX + t["name"] for t in tools)]
            else:
                args += ["--strict-mcp-config"]
            if model:
                args += ["--model", model]
            env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
            if config_dir:
                env["CLAUDE_CONFIG_DIR"] = config_dir       # run this turn on the routed account's login, not the default
            env["MCP_TOOL_TIMEOUT"] = str(3600 * 1000)      # matches mcp_shim.CALL_TIMEOUT_S
            self._stderr = open(self.dir / "stderr.log", "w")
            self.proc = popen(args, cwd=self.dir / "work", env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=self._stderr, text=True, bufsize=1)
        except OSError as exc:
            # No process exists yet, so close() cannot run: release the stderr handle and the scratch dir by hand and
            # report the start failure (binary gone or not executable, fork refused) as the BridgeError server.py
            # answers with 502, instead of an OSError that would drop the client's connection without a response.
            if self._stderr:
                self._stderr.close()
            shutil.rmtree(self.dir, ignore_errors=True)
            raise BridgeError(f"cannot start {prefix[0]!r} as the headless Claude CLI: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True, name=f"cortados-bridge-{self.id[:8]}").start()

    # ------------------------------------------------------------------ CLI side
    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._events.put(("cli", json.loads(line)))
                except ValueError:
                    logger.warning("cortados bridge %s: non-JSON stdout line: %.200s", self.id[:8], line)
        except ValueError:
            pass                                        # close() shut stdout under us: the session is ending anyway
        self._events.put(("exit", self.proc.wait()))

    def send_user(self, content: list[dict[str, Any]], timeout: float) -> None:
        """Write one user message, under write_line's deadline rule."""
        self.last_used = time.time()
        write_line(self.proc, json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n",
                   timeout, "the headless CLI")

    # ------------------------------------------------------------------ shim side
    def wait_result(self, tool_use_id: str, timeout: float) -> dict[str, Any]:
        """Called on the shim's /bridge/call thread: block until the client has answered this tool_use."""
        with self._cv:
            if tool_use_id not in self._results:
                self._waiting.add(tool_use_id)
                self._events.put(("call", tool_use_id))
                if not self._cv.wait_for(lambda: tool_use_id in self._results or self._closed, timeout=timeout):
                    self._waiting.discard(tool_use_id)
                    raise BridgeError(f"no result for {tool_use_id} within {timeout:.0f}s")
                if self._closed and tool_use_id not in self._results:
                    raise BridgeError("the session was closed")
            self._waiting.discard(tool_use_id)
            return self._results.pop(tool_use_id)

    def deliver(self, results: dict[str, dict[str, Any]], timeout: float | None = None) -> None:
        """Hand the client's results to the shim's waiting calls. In-process, so `timeout` (the request's remaining
        budget, part of the session contract the Codex bridge needs for its pipe writes) has nothing to bound here."""
        unknown = [i for i in results if i not in self.emitted]
        if unknown:
            raise BridgeError(f"tool results for ids this session never issued: {unknown}")
        self.last_used = time.time()
        with self._cv:
            self._results.update(results)
            self._cv.notify_all()

    # ------------------------------------------------------------------ one turn
    def run_turn(self, timeout: float) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        """Read the CLI until it finishes or blocks on a bridged tool. Returns (blocks, stop_reason, usage)."""
        blocks: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        pending: set[str] = set()                        # tool_use ids of this turn, not yet answered
        blocked_on: str | None = None
        deadline = time.time() + timeout
        settle_until = None
        while True:
            wait = (settle_until if settle_until else deadline) - time.time()
            try:
                kind, item = self._events.get(timeout=max(wait, 0))
            except queue.Empty:
                if settle_until:
                    return blocks, "tool_use", usage
                raise BridgeError(f"the headless CLI produced nothing for {timeout:.0f}s")
            if kind == "exit":
                raise BridgeError(f"the headless CLI exited with code {item}; see {self.dir / 'stderr.log'}")
            if kind == "call":
                if item in pending:
                    blocked_on = item
                    settle_until = time.time() + SETTLE_S
                elif item in self.emitted:
                    raise BridgeError(f"the client answered an earlier turn without a result for {item}")
                else:
                    # The call came before its tool_use reached stdout; it is put back once the block arrives.
                    blocked_on = item
                continue
            ev = item
            if ev.get("type") == "assistant":
                msg = ev.get("message") or {}
                usage = msg.get("usage") or usage
                for b in msg.get("content") or []:
                    if b.get("type") == "text" and b.get("text"):
                        blocks.append({"type": "text", "text": b["text"]})
                    elif b.get("type") == "tool_use":
                        name = b.get("name", "")
                        if not name.startswith(TOOL_PREFIX):
                            raise BridgeError(f"the headless CLI used a non-bridged tool {name!r}")
                        blocks.append({"type": "tool_use", "id": b["id"], "name": name[len(TOOL_PREFIX):],
                                       "input": b.get("input") or {}})
                        pending.add(b["id"])
                        self.emitted.add(b["id"])
                if blocked_on in pending and not settle_until:
                    settle_until = time.time() + SETTLE_S
                elif settle_until:
                    settle_until = time.time() + SETTLE_S
            elif ev.get("type") == "result":
                if ev.get("is_error"):
                    raise BridgeError(f"the headless CLI reported an error: {ev.get('result') or ev.get('subtype')}")
                return blocks, "end_turn", usage or ev.get("usage") or {}

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.stdin.close()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        finally:
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass                                # a broken pipe to a dead CLI: nothing left to flush
            if self._stderr:
                self._stderr.close()
            with self._cv:
                self._closed = True
                self._cv.notify_all()
            shutil.rmtree(self.dir, ignore_errors=True)
