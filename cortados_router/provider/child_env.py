"""The environment of every vendor CLI the provider starts (#106): an explicit ALLOWLIST, never this process's
environment minus a list.

The provider runs with secrets the backends must never see: its own bearer token (CORTADOS_*), GitHub and cloud
credentials (GITHUB_TOKEN, GH_*, AWS_*), the ssh agent socket (SSH_AUTH_SOCK), API keys and base URLs that would bill
or redirect a subscription lane (OPENAI_*, ANTHROPIC_*). Prefix stripping (#84) only kept out the families someone had
named; anything else reached the child. Here nothing reaches it unless it is listed below, with its reason.

Spawn sites using it (the only ones under cortados_router/provider/): bridge.BridgeSession and search_lane.run, both
through bridge.claude_env; codex_bridge.CodexSession through codex_isolation.child_env. The login-identity readers
(floor.executing_login, server's reveal) call the same two builders, so the login they name is the one the child runs.
Not covered, on purpose: child_tasks.py (whole Claude Code tasks with the owner's tools and connectors, another trust
level) and the scarcity / S1 probes.

Adding a variable: put it in BASE_ENV (every backend needs it) or in the backend's set in BACKEND_ENV, with a one-line
reason saying why that backend cannot run without it and that it carries no secret the backend should not hold. A
variable the provider sets for one call on purpose (CODEX_HOME, CLAUDE_CONFIG_DIR, MCP_TOOL_TIMEOUT) is passed as
`extra` by the caller instead, and always wins over an inherited value.
"""
from __future__ import annotations

import os
from typing import Mapping

# Inherited by every backend CLI, when set in the provider's environment. One reason per entry:
BASE_ENV = frozenset({
    "PATH",                         # the CLIs are Node/Rust launchers that find node, git, security(1) etc. on PATH
    "HOME",                         # ~/.claude, ~/.codex and the login Keychain are found through it
    "USER",                         # the Keychain lookup and the CLIs' own account checks read the user name
    "LOGNAME",                      # the same, for the libraries that read LOGNAME instead of USER
    "SHELL",                        # the CLIs probe the user's shell at startup (no tool runs through it here)
    "TMPDIR",                       # macOS's per-user temp dir; without it the CLIs fall back to the shared /tmp
    "LANG",                         # locale: UTF-8 handling of prompts and tool output (LC_* by prefix below)
    "TZ",                           # local time in the CLI's environment block and logs
    "HTTP_PROXY", "http_proxy",     # a host that reaches the vendor only through a proxy (both cases are read)
    "HTTPS_PROXY", "https_proxy",
    "NO_PROXY", "no_proxy",
    "ALL_PROXY", "all_proxy",       # the catch-all proxy (curl, Node agents); an authenticated URL (user:pass@)
                                    # passes unchanged: proxy credentials the owner configured, an accepted exception
    "SSL_CERT_FILE",                # a custom CA bundle (TLS interception, or the system roots exported, #103)
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",          # the same for the Node-based Claude CLI
    "__CF_USER_TEXT_ENCODING",      # macOS CoreFoundation text encoding, set by launchd for every user process
})
# Inherited by prefix (case-sensitive): the locale categories LC_ALL, LC_CTYPE, ... (same reason as LANG).
BASE_PREFIXES = ("LC_",)
# What each backend needs beyond BASE_ENV, inherited when set. One reason per entry:
BACKEND_ENV = {
    "codex": frozenset({
        "CODEX_HOME",               # the home whose auth.json / config.toml the child runs on; codex_isolation.child_env
                                    # always passes the resolved absolute home as an extra, so the inherited one never wins
        "CODEX_CA_CERTIFICATE",     # the PEM bundle Codex trusts for TLS; the Air sets it to the macOS system roots
                                    # (#103, Codex failed with `invalid peer certificate: UnknownIssuer` without it)
    }),
    "claude": frozenset({
        "CLAUDE_CONFIG_DIR",        # the default login's config dir when the provider was started on a non-default one;
                                    # a routed account passes its own as an extra (bridge.claude_env)
        "CLAUDE_CODE_OAUTH_TOKEN",  # the default login when the provider was started with a long-lived subscription
                                    # token instead of a Keychain login; dropped under an explicit account
                                    # (bridge.claude_env), whose stored login must win
    }),
}


def allowed(name: str, backend: str) -> bool:
    """Whether `name` may be inherited by `backend`'s child ("codex" or "claude")."""
    return name in BASE_ENV or name.startswith(BASE_PREFIXES) or name in BACKEND_ENV[backend]


def backend_env(backend: str, extra: Mapping[str, str] | None = None,
                environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a `backend` CLI ("codex" or "claude"): the allowlisted variables of `environ` (this
    process's by default), then `extra`, the variables the caller sets on purpose for this call, which win."""
    if backend not in BACKEND_ENV:
        raise ValueError(f"unknown backend {backend!r}; one of {sorted(BACKEND_ENV)}")
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if allowed(k, backend)}
    env.update(extra or {})
    return env
