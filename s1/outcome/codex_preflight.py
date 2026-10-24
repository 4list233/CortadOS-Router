"""Before the loop uses Codex: prove the effective config and login are the isolated subscription setup it asks for.

`codex exec` has no config/read, so the same overrides are given to a short-lived `codex app-server`, whose config/read
shows every layer merged (config.toml, the -c overrides, a managed requirements.toml) and whose account/read shows the
login. The checks are the provider's (provider/codex_isolation.py: EXPECTED_CONFIG, the redefined-provider rule,
check_account) plus the task sandbox pins below. Codex review 2026-10-24: launch flags alone do not prove the policy.
"""
from __future__ import annotations

import json
import subprocess
import time

from cortados_router.provider import codex_isolation
from cortados_router.provider.bridge import BridgeError

# The workspace is the only writable root: no extra roots from the home, no $TMPDIR or /tmp (where the runner's own
# files live), no network. Defaults include the temp roots, so they are pinned off and then checked.
SANDBOX_PINS = ("sandbox_workspace_write.writable_roots=[]", "sandbox_workspace_write.network_access=false",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true", "sandbox_workspace_write.exclude_slash_tmp=true")
EXPECTED_SANDBOX = {"writable_roots": [], "network_access": False, "exclude_tmpdir_env_var": True,
                    "exclude_slash_tmp": True}
# initialize / account/read / config/read load the home and may contact the login service.
HANDSHAKE_S = 60
# A home can change during the night (an `ollama launch codex`, a login switch): recheck at most an hour apart.
RECHECK_S = 3600

_checked: dict[str, float] = {}


def check_config(cfg: dict, kept_features: tuple) -> list[str]:
    bad = [f"{k}={json.dumps(cfg.get(k))} (expected {json.dumps(v)})"
           for k, v in codex_isolation.EXPECTED_CONFIG.items() if cfg.get(k) != v]
    if "openai" in (cfg.get("model_providers") or {}):
        bad.append("model_providers.openai is redefined")
    servers = cfg.get("mcp_servers") or {}
    on = sorted(n for n, v in servers.items() if not (isinstance(v, dict) and v.get("enabled") is False)) \
        if isinstance(servers, dict) else ["<mcp_servers is not a table>"]
    if on:
        bad.append(f"MCP servers still enabled: {on}")
    features = cfg.get("features") or {}
    still = sorted(f for f in codex_isolation.DISABLED_FEATURES
                   if f not in kept_features and (features.get(f) if isinstance(features, dict) else None) is not False)
    if still:
        bad.append(f"integrations still enabled: {still}")
    sandbox = cfg.get("sandbox_workspace_write") or {}
    for k, v in EXPECTED_SANDBOX.items():
        got = sandbox.get(k) if isinstance(sandbox, dict) else None
        if (got or []) != v if k == "writable_roots" else got != v:
            bad.append(f"sandbox_workspace_write.{k}={json.dumps(got)} (expected {json.dumps(v)})")
    return bad


def _rpc(proc, rid: int, method: str, params: dict, deadline: float) -> dict:
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise BridgeError(f"codex app-server exited during {method}")
        msg = json.loads(line)
        if msg.get("id") == rid and "method" not in msg:
            if msg.get("error"):
                raise BridgeError(f"codex app-server refused {method}: {msg['error']}")
            return msg.get("result") or {}
    raise BridgeError(f"codex app-server did not answer {method} in time")


def preflight(argv_prefix: list[str], overrides: list[str], env: dict, kept_features: tuple) -> None:
    """Raises BridgeError naming every violation; returns when the setup is the isolated one."""
    key = json.dumps([argv_prefix, overrides, env.get("CODEX_HOME")])
    if time.time() - _checked.get(key, 0) < RECHECK_S:
        return
    proc = subprocess.Popen([*argv_prefix, "app-server", *overrides], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
    try:
        deadline = time.time() + HANDSHAKE_S
        _rpc(proc, 1, "initialize", {"clientInfo": {"name": "cortados-outcome", "version": "0"},
                                     "capabilities": {"experimentalApi": True}}, deadline)
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        proc.stdin.flush()
        codex_isolation.check_account(_rpc(proc, 2, "account/read", {"refreshToken": False}, deadline))
        cfg = _rpc(proc, 3, "config/read", {}, deadline)
        bad = check_config(cfg["config"] if isinstance(cfg.get("config"), dict) else cfg, kept_features)
        if bad:
            raise BridgeError("the Codex home's effective config would not isolate outcome runs: " + "; ".join(bad))
    finally:
        proc.kill()
        proc.wait()
    _checked[key] = time.time()
