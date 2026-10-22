"""The isolation policy of the Codex backend: what `codex app-server` is started with, and what its effective
config, login and tool set must look like before a thread may start.

Owner of the "no built-in tool ever runs" rule for Codex (bridge.py's invariant): the command line disables every
built-in tool feature, every MCP server declared in config.toml, hooks, plugins, skills, apps and web search, and
pins the provider to the OpenAI subscription; config/read and account/read are then checked against EXPECTED_CONFIG
and EXPECTED_ACCOUNT_TYPE, because a managed layer or a later edit of the home can override any `-c` flag. The
leak detection used while a turn runs (LEAKED_ITEMS, BENIGN_MCP_TOOLS, the approval requests) and the tool-name
aliases Codex needs live here too. codex_bridge.CodexSession drives the protocol; it decides nothing about isolation.

Verified against Codex CLI 0.163.0 (docs/PROVIDER-PLAN.md, P2 status and live findings).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cortados_router.provider.bridge import STRIPPED_ENV as CLAUDE_STRIPPED_ENV, BridgeError

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
# openai_base_url repoints the built-in provider even under a ChatGPT login (found live: `ollama launch codex` writes
# it, and Codex then sent a ChatGPT-authenticated turn to the local Ollama endpoint); model_catalog_json replaces the
# metadata of the models the route names. Both default to null and must stay so.
EXPECTED_CONFIG = {"notify": [], "web_search": "disabled", "include_environment_context": False, "project_doc_max_bytes": 0,
                   "model_provider": "openai", "chatgpt_base_url": "https://chatgpt.com/backend-api/",
                   "openai_base_url": None, "model_catalog_json": None}
# The login the lane bills to: a ChatGPT subscription. An API-key login (or none) would bill elsewhere, or fail later
# as unauthorized after the route was logged as a subscription lane.
EXPECTED_ACCOUNT_TYPE = "chatgpt"
# `--disable <feature>` for every built-in tool and every integration (brief §B, verified: the tool list Codex then
# builds is exactly request_user_input plus the dynamic tools). code_mode_host stays ON: dynamic tools are served
# through it, and with it off the model reports them unavailable.
DISABLED_FEATURES = ("shell_tool", "unified_exec", "unified_exec_tty", "view_image", "image_generation", "apps",
                     "plugins", "hooks", "multi_agent", "sleep_tool", "skill_search", "tool_suggest", "goals",
                     "memories", "browser_use", "computer_use", "collaboration_modes")
# `--enable <feature>`: the one feature the dynamic tools need. A home whose config turns it off would start a thread
# whose advertised client tools cannot run (the first spike, with it off: "Read tool is unavailable"), so it is asked
# for on the command line and checked in the effective config like the disabled ones.
ENABLED_FEATURES = ("code_mode_host",)
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
    off = sorted(f for f in ENABLED_FEATURES if (features.get(f) if isinstance(features, dict) else None) is not True)
    if off:
        bad.append(f"features the dynamic tools need are off: {off}")
    if bad:
        raise BridgeError("codex app-server's effective config would not isolate the thread: " + "; ".join(bad) +
                          ". A setting the user's own Codex needs can stay there: give the backend a dedicated, logged-in "
                          "home instead (--codex-home / CORTADOS_PROVIDER_CODEX_HOME)")


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




def command_line(prefix: list[str], codex_home: str) -> list[str]:
    """The argv that starts an isolated app-server: `prefix` is the binary (a list lets tests run a fake), `codex_home`
    the home whose config.toml names the MCP servers to disable (the one read of it)."""
    args = prefix + ["app-server"]
    for override in CONFIG_OVERRIDES:
        args += ["-c", override]
    for name in mcp_server_names(codex_home):
        args += ["-c", f"mcp_servers.{name}.enabled=false"]
    for feature in DISABLED_FEATURES:
        args += ["--disable", feature]
    for feature in ENABLED_FEATURES:
        args += ["--enable", feature]
    return args


def child_env(home: str) -> dict[str, str]:
    """The child's environment: STRIPPED_ENV removed and CODEX_HOME set to the one absolute home this process read
    config.toml from (dedicated, inherited or the user's own), so both readers name the same directory whatever
    the child's cwd. The home is never copied: a throwaway one has no login, and a copied auth.json would spend the
    single-use refresh token."""
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env["CODEX_HOME"] = home
    return env
