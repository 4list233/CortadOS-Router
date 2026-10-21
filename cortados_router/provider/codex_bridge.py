"""One `codex app-server` thread per client conversation, with the client's tools served as Codex dynamic tools.

Owner of the rule that the headless Codex never runs a tool itself (bridge.py's invariant, for Codex): the app-server
starts with every built-in tool feature disabled, every MCP server of its config disabled, no hooks, plugins, skills,
apps or web search, in a read-only sandbox with approvals off and an empty scratch cwd; its only tools are the
client's, declared on thread/start as dynamicTools and executed by the client through item/tool/call. A turn ends
when Codex finishes (end_turn) or asks for a client tool the client has not answered yet (tool_use); the client's
next request carries the results, which answer the waiting JSON-RPC server requests. Anything that shows a built-in
tool or an MCP server ran is a BridgeError, never tolerated.

Verified against Codex CLI 0.163.0 (docs/PROVIDER-PLAN.md, P2). The app-server API is experimental, so the CLI
version is pinned (SUPPORTED_CLI_VERSIONS). The child runs on the user's real CODEX_HOME unless `codex_home` names a
dedicated, logged-in one: a throwaway home has no login, and a copied auth.json would spend the single-use refresh
token and break the user's own login. With the real home its AGENTS.md and skills still reach the thread; that is
logged (instructionSources), not hidden.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from cortados_router.provider.bridge import STRIPPED_ENV as CLAUDE_STRIPPED_ENV, BridgeError

# The dynamic-tools API is experimental: each release is verified by hand before it joins this tuple (the /status
# parser in scarcity_codex.py pins the same way).
SUPPORTED_CLI_VERSIONS = ("0.163.0",)
# Dropped from the child's environment: an OpenAI/Codex API key or access token would bill an API account instead of
# the subscription login. The Anthropic trio is harmless to a Codex child and stripped for consistency with bridge.py.
# OPENAI_BASE_URL would point the built-in openai provider at another endpoint.
STRIPPED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL") + CLAUDE_STRIPPED_ENV
# `-c key=value` overrides (brief §B): no desktop notifications, no web search, no environment/project context in the
# prompt (the client's system prompt is the whole prompt; project docs would read the scratch cwd anyway), and the
# built-in openai provider, so a `model_provider` the home's config.toml sets (a mirror, another billing account)
# cannot redirect the subscription lane the router logged.
CONFIG_OVERRIDES = ("notify=[]", 'web_search="disabled"', "include_environment_context=false", "project_doc_max_bytes=0",
                    'model_provider="openai"')
# What config/read must show before thread/start (the effective config, every layer merged: config.toml, the -c
# overrides, a managed requirements.toml). The overrides above are asked for and then checked, because a managed layer
# wins over -c; chatgpt_base_url is the default backend of the ChatGPT login, the one the subscription bills to.
EXPECTED_CONFIG = {"notify": [], "web_search": "disabled", "include_environment_context": False, "project_doc_max_bytes": 0,
                   "model_provider": "openai", "chatgpt_base_url": "https://chatgpt.com/backend-api/"}
# The login the lane bills to: a ChatGPT subscription. An API-key login (or none) would bill elsewhere, or fail later
# as unauthorized after the route was logged as a subscription lane.
EXPECTED_ACCOUNT_TYPE = "chatgpt"
# `--disable <feature>` for every built-in tool and every integration (brief §B, verified: the tool list Codex then
# builds is exactly request_user_input plus the dynamic tools). code_mode_host stays ON: dynamic tools are served
# through it, and with it off the model reports them unavailable.
DISABLED_FEATURES = ("shell_tool", "unified_exec", "unified_exec_tty", "view_image", "image_generation", "apps",
                     "plugins", "hooks", "multi_agent", "sleep_tool", "skill_search", "tool_suggest", "goals",
                     "memories", "browser_use", "computer_use", "collaboration_modes")
# Thread items that only a built-in tool produces: their appearance means isolation failed (brief §A.11, plus `sleep`
# and `imageGeneration`, the items of the sleep_tool and image_generation features: the invariant is "no built-in ran",
# so every ThreadItem variant a built-in produces is listed, whether or not its feature flag is expected to hold).
LEAKED_ITEMS = frozenset({"commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView",
                          "collabAgentToolCall", "subAgentActivity", "sleep", "imageGeneration"})
# The one mcpToolCall that is not a leak: Codex's own MCP *resource* tools (server "codex"), which it registers
# whenever config.toml declares MCP servers, disabled or not (verified live, 0.163.0: the model called
# list_mcp_resources and got {"resources": []}). They run inside the app-server against the configured servers, all
# of which the effective-config check proved disabled, so nothing external runs; they are logged, not fatal.
BENIGN_MCP_TOOLS = ("codex", frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}))
# Codex's own tool names (0.163.0: the handlers under core/src/tools/handlers and the tool specs in the binary). A
# dynamic tool carrying one of these names passes thread/start but never reaches the model, even with the built-in's
# feature disabled (verified live: a dynamic `exec_command` was never offered and the model gave up; the same tool as
# `client_exec_command` was called at once). Codex as a client sends exactly these names, so a colliding client tool
# is declared under ALIAS_PREFIX + name and the item/tool/call is mapped back to the client's name. A collision with a
# name outside this set cannot be detected here (P2 known limit).
CODEX_TOOL_NAMES = frozenset({
    "exec_command", "write_stdin", "shell", "shell_command", "local_shell", "apply_patch", "view_image", "update_plan",
    "request_user_input", "request_user_input_async", "request_permissions", "web_search", "image_generation",
    "get_goal", "create_goal", "update_goal", "sleep", "current_time", "get_context_remaining", "new_context_window",
    "spawn_agent", "send_input", "send_message", "send_message_to_user", "send_message_to_user_async", "wait_agent", "wait",
    "close_agent", "resume_agent", "interrupt_agent", "list_agents", "followup_task", "assign_agent_task",
    "search_tools", "tool_search", "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
    "node_repl", "js_repl", "list_available_plugins_to_install", "request_plugin_install", "wait_for_environment",
    "test_sync_tool", "exec"})
ALIAS_PREFIX = "client_"
# Approval requests mean a built-in tool is about to run; they are declined AND reported as a violation (brief §A.10).
V2_APPROVALS = {"item/commandExecution/requestApproval": {"decision": "decline"},
                "item/fileChange/requestApproval": {"decision": "decline"}}
V1_APPROVALS = {"execCommandApproval": {"decision": "denied"}, "applyPatchApproval": {"decision": "denied"}}
# Every `[mcp_servers.<name>]` table of config.toml is disabled by name: `-c mcp_servers={}` merges and does nothing.
# The header may be indented (nested [mcp_servers.<name>.env] tables usually are), carry spaces inside the brackets,
# a quoted root or child key, or a trailing comment: all TOML the toml crate accepts, so all shapes Codex would start
# a server from. The name is captured without surrounding whitespace, because it becomes the
# `-c mcp_servers.<name>.enabled=false` key. This reader is the pre-spawn best effort; config/read is the authority
# (check_effective_config refuses any server the effective config still enables, whatever spelling declared it).
_ROOT = r"""(?:mcp_servers|"mcp_servers"|'mcp_servers')"""
MCP_TABLE = re.compile(r"""^[ \t]*\[[ \t]*""" + _ROOT + r"""[ \t]*\.[ \t]*("[^"]*"|'[^']*'|[A-Za-z0-9_-]+)[ \t]*\][ \t]*(?:#.*)?$""", re.M)
# mcp_servers declared any other way (a dotted key `mcp_servers.x.command = ...`, an inline table `mcp_servers = {...}`,
# keys under a bare `[mcp_servers]` table, or an array of tables) cannot be enumerated by this reader, so the file is
# refused at spawn time: a server it could not name would otherwise start and fail every turn as an isolation error.
MCP_OTHER_FORM = re.compile(r"^[ \t]*(?:" + _ROOT + r"[ \t]*(?:=|\.)|\[[ \t]*" + _ROOT + r"[ \t]*\]|\[\[[ \t]*" + _ROOT + r"\b)", re.M)
# Settle window after the first unanswered tool call of a turn. Codex dispatches each item/tool/call as its
# function_call item finishes streaming, so sibling calls of one model message arrive seconds apart when their
# arguments are long (a Write payload), not in one stdout burst as the Claude CLI's do (bridge.SETTLE_S = 0.3):
# a shorter window splits one model message into two tool_use replies. thread/tokenUsage/updated cannot replace the
# timer: in the live spike it arrived only after the tool result, not when the model's message ended.
CODEX_SETTLE_S = 2.0
# The app-server loads config and may refresh the login before it answers initialize/thread/start.
HANDSHAKE_S = 60.0
# A JSON-RPC reply to a server request is one short line; a pipe that cannot take it in this time is a dead server.
REPLY_WRITE_S = 10.0
# turn/interrupt is local to the app-server; its turn/completed follows within seconds, never minutes.
INTERRUPT_S = 30.0
# What a pending tool call is answered with when the client sends a new user message instead of its result (§A.13).
MOVED_ON = "cortados: the client moved on without a result"
logger = logging.getLogger(__name__)


def mcp_server_names(codex_home: str) -> list[str]:
    """The names of the `[mcp_servers.<name>]` tables in that home's config.toml (the one allowed read of it)."""
    path = Path(codex_home) / "config.toml"
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError as exc:
        raise BridgeError(f"cannot read {path} to disable its MCP servers: {exc}")
    other = MCP_OTHER_FORM.search(text)
    if other:
        raise BridgeError(f"{path} declares mcp_servers as dotted keys, an inline table or a bare [mcp_servers] table "
                          f"({other.group(0).strip()!r}); this bridge can only disable servers declared as "
                          f"[mcp_servers.<name>] tables, and an MCP server it cannot name would break isolation")
    return sorted(set(MCP_TABLE.findall(text)))


def check_effective_config(cfg: dict[str, Any]) -> None:
    """Refuse an effective config (config/read, every layer merged) under which the thread would not be the isolated
    subscription thread this bridge promises. Runs before thread/start, which is when MCP servers are started, so a
    server this check names never ran. Raises BridgeError naming every violation."""
    bad = []
    for key, want in EXPECTED_CONFIG.items():
        if cfg.get(key) != want:
            bad.append(f"{key}={json.dumps(cfg.get(key))} (expected {json.dumps(want)})")
    providers = cfg.get("model_providers") or {}
    if isinstance(providers, dict) and "openai" in providers:
        # Redefining the built-in provider is how a config points "openai" at another base_url or credential.
        bad.append("model_providers.openai is redefined in the config")
    servers = cfg.get("mcp_servers") or {}
    enabled = sorted(n for n, v in servers.items() if not (isinstance(v, dict) and v.get("enabled") is False)) \
        if isinstance(servers, dict) else ["<mcp_servers is not a table>"]
    if enabled:
        bad.append(f"MCP servers still enabled: {enabled} (declared in a form the config.toml reader could not disable, "
                   f"or by a config layer outside it)")
    features = cfg.get("features") or {}
    on = sorted(f for f in DISABLED_FEATURES if (features.get(f) if isinstance(features, dict) else None) is not False)
    if on:
        bad.append(f"built-in tool features still enabled: {on}")
    if bad:
        raise BridgeError("codex app-server's effective config would not isolate the thread: " + "; ".join(bad))


def check_account(account: dict[str, Any]) -> None:
    """Refuse a login that is not the ChatGPT subscription the codex-cli lane bills to."""
    acct = account.get("account")
    if not isinstance(acct, dict):
        raise BridgeError("codex app-server has no login in this CODEX_HOME; the codex-cli lane needs a ChatGPT "
                          "subscription login (run `codex login` in that home)")
    if acct.get("type") != EXPECTED_ACCOUNT_TYPE:
        raise BridgeError(f"codex app-server is logged in as {acct.get('type')!r}, not a ChatGPT subscription; the "
                          f"codex-cli lane is a subscription lane and must not bill another account")


def tool_aliases(tools: list[dict[str, Any]]) -> dict[str, str]:
    """Dynamic-tool name -> client tool name, for the client tools whose names Codex would swallow (CODEX_TOOL_NAMES).
    Refuses an alias that is itself a client tool's name: the call could not be mapped back unambiguously."""
    names = {t["name"] for t in tools}
    aliases = {ALIAS_PREFIX + n: n for n in sorted(names & CODEX_TOOL_NAMES)}
    taken = sorted(set(aliases) & names)
    if taken:
        raise BridgeError(f"client tools {taken} collide with the aliases of Codex-named tools; rename them")
    return aliases


def _content_items(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP tool-result content (anthropic_wire.mcp_content) as Codex DynamicToolCallResponse content items."""
    out = []
    for c in content:
        if c.get("type") == "text":
            out.append({"type": "inputText", "text": c.get("text", "")})
        elif c.get("type") == "image" and c.get("data"):
            # Codex refuses remote image URLs; a data URL is the one form that reaches the model.
            out.append({"type": "inputImage", "imageUrl": f"data:{c.get('mimeType') or 'image/png'};base64,{c['data']}"})
        else:
            out.append({"type": "inputText", "text": json.dumps(c)})
    return out


def _turn_input(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic user content blocks as turn/start UserInput items."""
    out = []
    for b in content:
        if b.get("type") == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
            src = b["source"]
            out.append({"type": "image", "url": f"data:{src.get('media_type') or 'image/png'};base64,{src.get('data', '')}"})
        else:
            out.append({"type": "text", "text": json.dumps(b)})     # unknown block: the model still sees it, as JSON
    return out


class CodexSession:
    """Same contract as bridge.BridgeSession for server.py: id, lock, last_used, history, emitted, route,
    route_logged, dir, send_user, deliver, run_turn, close. Tool results arrive in-process (JSON-RPC), so there is
    no wait_result / MCP shim path; the Codex callId is the tool_use id handed to the client."""

    def __init__(self, *, system: str, tools: list[dict[str, Any]], model: str | None = None,
                 codex_bin: str | list[str] = "codex", codex_home: str | None = None, scratch_root: str | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()                    # one client request at a time per conversation
        self.last_used = time.time()
        self.emitted: set[str] = set()                  # tool_use ids (Codex callIds) handed to the client
        self.history: list[str] = []                    # reply_fingerprint of each reply, in order
        self.route: dict[str, Any] | None = None        # set by the registry
        self.route_logged = False
        self._pending: dict[str, Any] = {}              # callId -> JSON-RPC id of the item/tool/call awaiting its result
        self._delivered: set[str] = set()               # callIds answered by deliver() whose item/completed is still due
        self._events: queue.Queue = queue.Queue()       # ("request"|"notify", msg) | ("exit", code), from the reader
        self._replies: dict[Any, dict[str, Any]] = {}   # replies to our requests, by id
        self._cv = threading.Condition()
        self._closed = False
        self._exited: int | None = None
        self._fatal: BridgeError | None = None          # set by the reader on an isolation failure
        self._next_id = 0
        self._usage_total: dict[str, Any] = {}          # tokenUsage.total as last reported
        self._usage_mark: dict[str, Any] = {}           # tokenUsage.total at the last run_turn boundary
        self.thread_id: str | None = None
        self._turn_id: str | None = None
        self._aliases: dict[str, str] = {}              # dynamic tool name -> client tool name (tool_aliases)
        if codex_home:
            # The child's cwd is the scratch work dir while this process reads config.toml from its own cwd, so a
            # relative home would name two different directories for the two readers; make it one absolute path.
            codex_home = os.path.abspath(os.path.expanduser(codex_home))
        self.dir = Path(tempfile.mkdtemp(prefix="cortados-codex-", dir=scratch_root))
        self._stderr = None
        prefix = [codex_bin] if isinstance(codex_bin, str) else list(codex_bin)   # a list lets tests run a fake
        try:
            (self.dir / "work").mkdir()
            # CODEX_HOME is set only from a dedicated home; otherwise the child finds the user's own (see module doc).
            home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
            args = prefix + ["app-server"]
            for override in CONFIG_OVERRIDES:
                args += ["-c", override]
            for name in mcp_server_names(home):
                args += ["-c", f"mcp_servers.{name}.enabled=false"]
            for feature in DISABLED_FEATURES:
                args += ["--disable", feature]
            env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
            if codex_home:
                env["CODEX_HOME"] = codex_home
            self._stderr = open(self.dir / "stderr.log", "w")
            self.proc = popen(args, cwd=self.dir / "work", env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=self._stderr, text=True, bufsize=1)
        except (OSError, BridgeError) as exc:
            # No process exists yet, so close() cannot run: release the stderr handle and the scratch dir by hand and
            # report the start failure as the BridgeError server.py answers with 502 (a backend that refused to start),
            # instead of an OSError that would drop the client's connection without a response.
            if self._stderr:
                self._stderr.close()
            shutil.rmtree(self.dir, ignore_errors=True)
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError(f"cannot start {prefix[0]!r} as codex app-server: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True, name=f"cortados-codex-{self.id[:8]}").start()
        try:
            self._handshake(system, tools, model)
        except BaseException:
            self.close()                                # never leave an app-server behind a refused session
            raise

    # ------------------------------------------------------------------ app-server side
    def _handshake(self, system: str, tools: list[dict[str, Any]], model: str | None) -> None:
        info = self._call("initialize", {"clientInfo": {"name": "cortados-provider", "version": "0"},
                                         "capabilities": {"experimentalApi": True}}, HANDSHAKE_S)
        self._notify("initialized")
        # Both checks run before thread/start: that is when Codex starts MCP servers and binds the provider, so a
        # refusal here means nothing ran. refreshToken=false: a refresh would spend the home's single-use token.
        check_account(self._call("account/read", {"refreshToken": False}, HANDSHAKE_S))
        config = self._call("config/read", {}, HANDSHAKE_S)
        check_effective_config(config["config"] if isinstance(config.get("config"), dict) else config)
        self._aliases = tool_aliases(tools)
        declared = {v: k for k, v in self._aliases.items()}
        if self._aliases:
            logger.info("codex conversation %s: client tools named like Codex's own are declared as %s", self.id[:8],
                        sorted(self._aliases))
        params: dict[str, Any] = {
            "cwd": str(self.dir / "work"), "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
            "baseInstructions": system or "You are a helpful assistant.",
            "dynamicTools": [{"type": "function", "name": declared.get(t["name"], t["name"]),
                              "description": t.get("description", ""),
                              "inputSchema": t.get("input_schema") or {"type": "object"}} for t in tools]}
        if model:
            params["model"] = model
        started = self._call("thread/start", params, HANDSHAKE_S)
        thread = started.get("thread") or {}
        version = thread.get("cliVersion")
        if version not in SUPPORTED_CLI_VERSIONS:
            raise BridgeError(f"codex app-server reports cliVersion {version!r}; this bridge is verified for "
                              f"{', '.join(SUPPORTED_CLI_VERSIONS)} only (the dynamic-tools API is experimental)")
        if not thread.get("id"):
            raise BridgeError("codex app-server answered thread/start without a thread id")
        if model and started.get("model") != model:
            # The route log and the x-cortados-route header name the routed model as the one that ran; a thread
            # Codex started on another slug (one the account's model/list lacks) must be refused, never run quietly.
            raise BridgeError(f"codex app-server started the thread on {started.get('model')!r}, not the routed "
                              f"{model!r}; the route log must name the model that ran")
        sandbox = (started.get("sandbox") or {}).get("type")
        if sandbox != "readOnly" or started.get("approvalPolicy") != "never":
            # Both were asked for on thread/start; a managed requirements.toml can override either, and a thread that
            # could write or ask for approvals is not the isolated one this bridge promises.
            raise BridgeError(f"codex app-server started the thread with sandbox {sandbox!r} and approvalPolicy "
                              f"{started.get('approvalPolicy')!r}, not the read-only / never that was requested")
        self.thread_id = thread["id"]
        logger.info("codex conversation %s: thread %s, model %s, codexHome %s, instructionSources %s", self.id[:8],
                    self.thread_id, started.get("model"), info.get("codexHome"), started.get("instructionSources"))
        self._check_fatal()

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    logger.warning("cortados codex %s: non-JSON stdout line: %.200s", self.id[:8], line)
                    continue
                if not isinstance(msg, dict):
                    logger.warning("cortados codex %s: non-object stdout line: %.200s", self.id[:8], line)
                    continue
                if "method" in msg:                     # server request (with id) or notification (without)
                    if msg["method"] == "mcpServer/startupStatus/updated":
                        self._set_fatal(BridgeError("codex app-server started an MCP server "
                                                    f"({json.dumps(msg.get('params'))[:200]}): isolation failed"))
                    self._events.put(("request" if "id" in msg else "notify", msg))
                else:                                   # reply to one of our requests
                    with self._cv:
                        self._replies[msg.get("id")] = msg
                        self._cv.notify_all()
        except ValueError:
            pass                                        # close() shut stdout under us: the session is ending anyway
        code = self.proc.wait()
        with self._cv:
            self._exited = code
            self._cv.notify_all()
        self._events.put(("exit", code))

    def _set_fatal(self, exc: BridgeError) -> None:
        with self._cv:
            self._fatal = self._fatal or exc
            self._cv.notify_all()

    def _check_fatal(self) -> None:
        if self._fatal:
            raise self._fatal

    def _write(self, line: str, timeout: float) -> None:
        """A server that stops reading stdin would block the write forever once the pipe buffer fills, so the write
        runs on its own thread and the server is killed if it misses the deadline (bridge.send_user's rule)."""
        failed: list[BaseException] = []

        def write():
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                failed.append(exc)

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(max(timeout, 0))
        if writer.is_alive():
            self.proc.kill()
            raise BridgeError(f"codex app-server did not read its input within {timeout:.0f}s; killed")
        if failed:
            raise BridgeError(f"codex app-server is gone: {failed[0]}")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        self._write(json.dumps(msg) + "\n", REPLY_WRITE_S)

    def _reply(self, rid: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
               timeout: float = REPLY_WRITE_S) -> None:
        msg: dict[str, Any] = {"id": rid}
        msg.update({"error": error} if error else {"result": result})
        self._write(json.dumps(msg) + "\n", timeout)

    @staticmethod
    def _budget(deadline: float, what: str) -> float:
        """The write budget left under an absolute deadline; a passed deadline is a BridgeError, never a 0 s write
        (which would kill a healthy server for being a few microseconds late)."""
        left = deadline - time.time()
        if left <= 0:
            raise BridgeError(f"the request deadline passed {what}")
        return min(REPLY_WRITE_S, left)

    def _call(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """One client request; returns its result or raises BridgeError for an error reply, exit or timeout."""
        self._next_id += 1
        rid = f"c-{self._next_id}"                      # strings: never in the server's integer id space
        deadline = time.time() + timeout
        self._write(json.dumps({"id": rid, "method": method, "params": params}) + "\n", timeout)
        with self._cv:
            self._cv.wait_for(lambda: rid in self._replies or self._closed or self._exited is not None or self._fatal,
                              timeout=max(deadline - time.time(), 0))
            reply = self._replies.pop(rid, None)
        self._check_fatal()
        if reply is None:
            if self._exited is not None:
                raise BridgeError(f"codex app-server exited with code {self._exited} before answering {method}; "
                                  f"see {self.dir / 'stderr.log'}")
            if self._closed:
                raise BridgeError("the session was closed")
            raise BridgeError(f"codex app-server did not answer {method} within {timeout:.0f}s")
        if "error" in reply:
            err = reply.get("error") or {}
            raise BridgeError(f"codex app-server refused {method}: {err.get('message')} (code {err.get('code')})")
        return reply.get("result") or {}

    # ------------------------------------------------------------------ client side
    def send_user(self, content: list[dict[str, Any]], timeout: float) -> None:
        """Start a turn with one user message. Pending tool calls are abandoned first (§A.13)."""
        self.last_used = time.time()
        deadline = time.time() + timeout
        if self._pending:
            self._abandon_turn(deadline)
        self._budget(deadline, "before the turn could start")      # never start backend work for an expired request
        params: dict[str, Any] = {"threadId": self.thread_id, "summary": "none", "input": _turn_input(content)}
        effort = (self.route or {}).get("effort")       # only a route that carries one; otherwise the model default
        if effort:
            params["effort"] = effort
        result = self._call("turn/start", params, deadline - time.time())
        self._turn_id = (result.get("turn") or {}).get("id")
        if not self._turn_id:
            raise BridgeError("codex app-server answered turn/start without a turn id")

    def _abandon_turn(self, deadline: float) -> None:
        """The client sent a new user message instead of the results Codex is waiting for (a Claude Code interrupt):
        every pending call is failed with MOVED_ON, the turn is interrupted and drained, so the next turn/start starts
        a turn instead of steering the old one."""
        logger.warning("codex conversation %s: new user message while %d tool call(s) are unanswered; failing them "
                       "and interrupting the turn", self.id[:8], len(self._pending))
        # Every write here is bounded by what is left of the request's deadline, not by a fresh REPLY_WRITE_S each: a
        # server that drains stdin slowly would otherwise hold the session lock for pending × REPLY_WRITE_S.
        for rid in list(self._pending.values()):
            self._reply(rid, {"success": False, "contentItems": [{"type": "inputText", "text": MOVED_ON}]},
                        timeout=self._budget(deadline, "while failing the abandoned tool calls"))
        self._pending.clear()
        turn_id = self._turn_id
        try:
            self._call("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}, min(INTERRUPT_S, deadline - time.time()))
        except BridgeError as exc:
            # The turn may have ended on its own between the failed results and the interrupt; its turn/completed is
            # then already queued and the drain below finds it. Anything else surfaces there as a timeout.
            logger.warning("codex conversation %s: turn/interrupt refused: %s", self.id[:8], exc)
        drain_until = min(deadline, time.time() + INTERRUPT_S)
        while True:
            # Checked on every iteration, not only when the queue runs dry: a backlog that keeps the queue non-empty
            # (a server streaming errors) would otherwise hold the request thread and the lock past the deadline.
            if time.time() >= drain_until:
                raise BridgeError(f"codex app-server did not complete the interrupted turn within {INTERRUPT_S:.0f}s")
            try:
                kind, msg = self._events.get(timeout=max(drain_until - time.time(), 0))
            except queue.Empty:
                continue
            if kind == "exit":
                raise BridgeError(f"codex app-server exited with code {msg} while a turn was being interrupted")
            if kind == "request":
                budget = self._budget(drain_until, "while draining the abandoned turn")
                if msg["method"] == "item/tool/call":   # a late sibling call of the abandoned turn
                    self._reply(msg["id"], {"success": False, "contentItems": [{"type": "inputText", "text": MOVED_ON}]},
                                timeout=budget)
                else:
                    self._answer(msg, timeout=budget)
                continue
            self._note_notification(msg)
            if msg.get("method") == "turn/completed" and (msg.get("params") or {}).get("turn", {}).get("id") == turn_id:
                logger.info("codex conversation %s: abandoned turn %s ended %s", self.id[:8], turn_id,
                            msg["params"]["turn"].get("status"))
                return

    def deliver(self, results: dict[str, dict[str, Any]], timeout: float | None = None) -> None:
        """Answer the pending item/tool/call requests with the client's results, all of them in one request. With
        `timeout`, every reply write is bounded by what is left of it, not by a fresh REPLY_WRITE_S each."""
        deadline = time.time() + timeout if timeout is not None else None
        unknown = [i for i in results if i not in self.emitted]
        if unknown:
            raise BridgeError(f"tool results for ids this session never issued: {unknown}")
        stale = [i for i in results if i not in self._pending]
        if stale:
            raise BridgeError(f"tool results for calls no longer waiting (answered earlier, or abandoned when the "
                              f"client moved on): {stale}")
        missing = [i for i in self._pending if i not in results]
        if missing:
            # Codex continues only once every call of the model's message is answered; a partial answer would hang
            # the turn until the deadline. Refuse it instead.
            raise BridgeError(f"results for {len(results)} of {len(self._pending)} pending tool calls; every call of "
                              f"one assistant message must be answered in one request (missing {missing})")
        self.last_used = time.time()
        for call_id, r in results.items():
            budget = self._budget(deadline, "while delivering the tool results") if deadline is not None else REPLY_WRITE_S
            self._reply(self._pending.pop(call_id), {"success": not r.get("isError"),
                                                     "contentItems": _content_items(r.get("content") or [])}, timeout=budget)
            self._delivered.add(call_id)

    # ------------------------------------------------------------------ one turn
    def run_turn(self, timeout: float) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        """Read the app-server until the turn completes or blocks on client tool calls. Returns (blocks, stop, usage)."""
        blocks: list[dict[str, Any]] = []
        deadline = time.time() + timeout
        settle_until: float | None = None               # set once a call is pending: sibling calls arrive together
        while True:
            self._check_fatal()
            now = time.time()
            if now >= deadline:
                # The absolute deadline holds whatever keeps arriving: a server that streams notifications forever
                # (each one re-arming the settle window) would otherwise hold the request thread, the session lock and
                # the backend past TURN_TIMEOUT_S. Calls already collected are returned; nothing collected is a failure.
                if settle_until:
                    return self._finish(blocks, "tool_use")
                raise BridgeError(f"codex app-server did not finish the turn within {timeout:.0f}s")
            wait = min(settle_until, deadline) - now if settle_until else deadline - now
            try:
                kind, msg = self._events.get(timeout=max(wait, 0))
            except queue.Empty:
                if settle_until and time.time() >= settle_until:
                    return self._finish(blocks, "tool_use")
                continue                                # the deadline check at the top of the loop decides
            if kind == "exit":
                raise BridgeError(f"codex app-server exited with code {msg}; see {self.dir / 'stderr.log'}")
            if kind == "request":
                params = msg.get("params") or {}
                if msg["method"] == "item/tool/call":
                    call_id, args = params.get("callId"), params.get("arguments")
                    if args is None:
                        args = {}
                    if not isinstance(call_id, str) or not call_id or not isinstance(args, dict):
                        raise BridgeError(f"codex app-server sent a malformed item/tool/call: {json.dumps(params)[:300]}")
                    if call_id in self.emitted:
                        raise BridgeError(f"codex app-server reused tool call id {call_id}")
                    name = params.get("tool") or ""
                    blocks.append({"type": "tool_use", "id": call_id, "name": self._aliases.get(name, name), "input": args})
                    self._pending[call_id] = msg["id"]
                    self.emitted.add(call_id)
                else:
                    self._answer(msg, timeout=self._budget(deadline, "while answering a server request"))
            else:
                self._note_notification(msg)
                params = msg.get("params") or {}
                method = msg.get("method")
                if method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage" and item.get("text"):
                        blocks.append({"type": "text", "text": item["text"]})
                    elif item.get("type") == "dynamicToolCall":
                        self._delivered.discard(item.get("id"))     # Codex consumed this delivered result
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("status") == "completed":
                        if self._pending:
                            raise BridgeError(f"the Codex turn completed with {len(self._pending)} tool call(s) unanswered")
                        if self._delivered:
                            # A turn that completed around an outstanding call (its completion queued before the
                            # client's results arrived) never read those results; the agentMessage-less end_turn
                            # that follows would be an empty reply hiding a lost tool result.
                            raise BridgeError(f"the Codex turn completed without consuming the delivered tool "
                                              f"results for {sorted(self._delivered)}")
                        return self._finish(blocks, "end_turn")
                    err = turn.get("error") or {}
                    info = err.get("codexErrorInfo")
                    raise BridgeError(f"the Codex turn {turn.get('status')}: {err.get('message') or 'no message'} "
                                      f"({info if isinstance(info, str) else json.dumps(info)})")
            if self._pending:
                settle_until = time.time() + CODEX_SETTLE_S   # the server is still talking: keep collecting siblings

    def _note_notification(self, msg: dict[str, Any]) -> None:
        """Notifications that are not turn content: isolation and item checks, usage, mid-turn errors."""
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "mcpServer/startupStatus/updated":
            self._check_fatal()                         # the reader set it; raise it here, on the request thread
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            itype = item.get("type")
            if itype == "mcpToolCall" and item.get("server") == BENIGN_MCP_TOOLS[0] and item.get("tool") in BENIGN_MCP_TOOLS[1]:
                if method == "item/started":
                    logger.info("codex conversation %s: the model listed MCP resources (%s); none are served", self.id[:8], item.get("tool"))
            elif itype in LEAKED_ITEMS:
                raise BridgeError(f"codex app-server ran a built-in tool ({itype} item) despite isolation; "
                                  f"the headless Codex must never run a tool itself: {json.dumps(params.get('item'))[:400]}")
        elif method == "model/rerouted":
            # Codex moved the turn to another model (one it could not serve under the routed slug); the route log
            # would then name a model that did not answer. The routed model is the one that must run.
            raise BridgeError(f"codex rerouted the turn from {params.get('fromModel')!r} to {params.get('toModel')!r} "
                              f"({params.get('reason')!r}): the routed model is the one that must run")
        elif method == "thread/tokenUsage/updated":
            self._usage_total = (params.get("tokenUsage") or {}).get("total") or self._usage_total
        elif method == "error":
            logger.warning("codex conversation %s: mid-turn error %s (willRetry=%s)", self.id[:8],
                           json.dumps(params.get("error"))[:300], params.get("willRetry"))

    def _answer(self, msg: dict[str, Any], timeout: float = REPLY_WRITE_S) -> None:
        """Defensive answers to the server requests that are not tool calls (§A.10): the turn hangs otherwise."""
        method, rid = msg["method"], msg["id"]
        if method == "item/tool/requestUserInput":
            # request_user_input is the one built-in tool that cannot be switched off; an empty answer lets the turn go on.
            self._reply(rid, {"answers": {}}, timeout=timeout)
            logger.warning("codex conversation %s: the model asked the user a question; answered empty", self.id[:8])
        elif method in V2_APPROVALS:
            self._reply(rid, V2_APPROVALS[method], timeout=timeout)
            raise BridgeError(f"codex app-server asked for approval of a built-in tool ({method}) despite isolation")
        elif method in V1_APPROVALS:
            self._reply(rid, V1_APPROVALS[method], timeout=timeout)
            raise BridgeError(f"codex app-server asked for approval of a built-in tool ({method}) despite isolation")
        elif method == "item/permissions/requestApproval":
            self._reply(rid, {"permissions": {}}, timeout=timeout)
            logger.warning("codex conversation %s: permissions request declined", self.id[:8])
        elif method == "mcpServer/elicitation/request":
            self._reply(rid, {"action": "decline"}, timeout=timeout)
            logger.warning("codex conversation %s: MCP elicitation declined", self.id[:8])
        else:
            self._reply(rid, error={"code": -32601, "message": f"cortados: unsupported server request {method}"}, timeout=timeout)
            logger.warning("codex conversation %s: unsupported server request %s refused", self.id[:8], method)

    def _finish(self, blocks: list[dict[str, Any]], stop: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        # One client request may span several model requests; tokenUsage.total is cumulative per thread, so the
        # request's usage is the delta since the previous boundary.
        # The session usage contract is the Messages shape, which the Messages wire passes through: input_tokens
        # excludes cache reads and writes, which come as cache_read_input_tokens / cache_creation_input_tokens. Codex's
        # inputTokens includes its cachedInputTokens (spike: 12309 with 3968 cached), so the cached part is taken out.
        total, mark = self._usage_total, self._usage_mark
        delta = lambda k: int(total.get(k) or 0) - int(mark.get(k) or 0)
        usage = {"input_tokens": delta("inputTokens") - delta("cachedInputTokens"), "output_tokens": delta("outputTokens"),
                 "cache_read_input_tokens": delta("cachedInputTokens"),
                 "cache_creation_input_tokens": delta("cacheWriteInputTokens"),
                 "reasoning_output_tokens": delta("reasoningOutputTokens")}
        self._usage_mark = dict(total)
        self._delivered.clear()                         # the delivered-results check is scoped to one run_turn
        return blocks, stop, usage

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                if self.thread_id:
                    try:
                        self._write(json.dumps({"id": "c-close", "method": "thread/unsubscribe",
                                                "params": {"threadId": self.thread_id}}) + "\n", 1)
                    except BridgeError as exc:          # a server that cannot take one line is dead; stdin close + kill follow
                        logger.debug("codex conversation %s: unsubscribe not written: %s", self.id[:8], exc)
                try:
                    self.proc.stdin.close()             # "stdin closed" ends the app-server
                except OSError:
                    pass                                # a broken pipe to a dead server: nothing left to flush
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
                    pass
            if self._stderr:
                self._stderr.close()
            with self._cv:
                self._closed = True
                self._cv.notify_all()
            shutil.rmtree(self.dir, ignore_errors=True)
