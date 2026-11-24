"""Isolation of every Antigravity CLI (`agy`) process the router starts (#144). The one owner of these rules.

Today the only agy process is the quota probe (scarcity._antigravity: `/usage` in a PTY). A future served backend (#144
PR B, [ESCALATE]) must start agy through the same functions, so the rules cannot drift between the two.

Three rules, each with its reason:
  1. Environment ALLOWLIST (`child_env`): agy gets HOME, USER, LOGNAME, TMPDIR, LANG / LC_* and a fixed PATH; TERM is
     set by the PTY capture. Nothing else reaches it. Reason: `GEMINI_API_KEY` / `GOOGLE_GEMINI_BASE_URL` switch agy to
     API-key billing or another endpoint, `AGY_ADC_AUTH=true` to Application Default Credentials billing; all three
     bill outside the Google AI Pro subscription. The scarcity probes used to pass the whole process environment
     (`scarcity._cli_env`), so any of them would have reached agy.
  2. HARD FAILURE when the router's shared env file (`$CORTADOS_HOME/env`, which scarcity applies to every vendor CLI)
     names a Google-family variable (`FORBIDDEN_PREFIXES`). Reason: a key written there is an owner's mistake to
     surface, not something to strip silently and leave in place. Other vendors' variables in that file are meant for
     their own lanes (CLAUDE_CONFIG_DIR, CODEX_HOME, ...): they are dropped by the allowlist, not fatal.
  3. A configured ABSOLUTE binary (`resolve_binary`): `--agy-bin` on the provider, or CORTADOS_AGY_BIN. PATH never
     chooses. Unset, the lane is "not configured" and is never sampled (no PTY is opened).
  4. SETTINGS GUARD (`check_settings`): agy's own settings file is read (never written) before any start; a file that
     configures MCP servers, an auto-approving tool mode, or an API-key / ADC login is refused. PROVISIONAL: the key
     names of agy 1.5.16's settings.json have not been captured yet, so the guard matches the documented VALUES
     (`always-proceed`, `proceed-in-sandbox`) and key names by family (mcp*, *api*key*, adc) at any depth, and a file it
     cannot parse is refused (fail closed). Tighten it to the exact keys once the file is captured on the Air.

What isolation does NOT cover (recorded for PR B): agy saves every interactive session as a conversation ("Resume with
-c ... agy --conversation=<uuid>" on start). The CLI reference (antigravity.google/docs/cli/reference, 2026-11-24) has
no flag to start without saving, so each `/usage` probe may leave one empty saved conversation in agy's own store.
The probe's per-run folder is removed; agy's store is not touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

# Inherited when set, one reason each. TERM is not listed: the PTY capture sets its own.
ENV_ALLOW = frozenset({
    "HOME",      # agy finds its settings (~/.gemini/antigravity-cli) and the login Keychain through it
    "USER",      # the Keychain lookup reads the user name
    "LOGNAME",   # the same, for libraries that read LOGNAME instead of USER
    "TMPDIR",    # macOS's per-user temp dir; without it the CLI falls back to the shared /tmp
    "LANG",      # UTF-8 handling of the screen (LC_* by prefix below)
})
ENV_ALLOW_PREFIXES = ("LC_",)
# PATH is fixed, never inherited: agy is started by absolute path, and this PATH only serves the helpers it may run
# (security(1) for the Keychain, a Node runtime from Homebrew). ~/.local/bin is where the installer puts agy itself.
FIXED_PATH = "{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Configured for this lane (the shared env file) => hard failure. One reason per family:
FORBIDDEN_PREFIXES = {
    "GEMINI_": "GEMINI_API_KEY / GEMINI_* select API-key billing outside the Google AI Pro subscription",
    "GOOGLE_": "GOOGLE_GEMINI_BASE_URL redirects agy; GOOGLE_API_KEY / GOOGLE_APPLICATION_CREDENTIALS bill a Cloud project",
    "AGY_": "AGY_ADC_AUTH=true switches agy to Application Default Credentials (Cloud billing)",
    "CLOUDSDK_": "gcloud configuration feeds Application Default Credentials (Cloud billing)",
}
# Never reach agy and never fatal (they belong to other lanes' CLIs or to this router): dropped by the allowlist. Named
# so a test can assert they are absent even if someone widens ENV_ALLOW by mistake.
OTHER_VENDOR_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CODEX_", "CLAUDE_", "CURSOR_", "CORTADOS_")

SETTINGS_RELPATH = ".gemini/antigravity-cli/settings.json"
AUTO_APPROVE_VALUES = ("always-proceed", "proceed-in-sandbox")    # documented tool permission modes that run tools unasked
SETTINGS_MAX_BYTES = 1 << 20                                      # a settings file is a few KB; more is not one


class AgyLaneError(RuntimeError):
    """A refusal of the agy lane with a stable `kind` (scarcity._failed_record stores it as error_kind)."""
    kind = "agy-error"


class LaneNotConfigured(AgyLaneError):
    """No agy binary is configured: the lane is not sampled at all. Not a failure (scarcity records it as a state)."""
    kind = "not-configured"


class ProbeBinaryMissing(AgyLaneError):
    kind = "binary-missing"

    def __init__(self, searched: list[str], why: str):
        super().__init__(f"agy CLI not installed: {why}")
        self.searched = list(searched)


class ForbiddenEnv(AgyLaneError):
    kind = "forbidden-env"

    def __init__(self, names: list[str]):
        # names only, never values: a value here is a credential
        super().__init__("forbidden variables configured for the agy lane in the router's env file: "
                         + ", ".join(sorted(names)) + " (remove them; they bill outside the subscription)")
        self.names = sorted(names)


class SettingsRefused(AgyLaneError):
    kind = "settings-refused"


def forbidden(name: str) -> bool:
    return name.startswith(tuple(FORBIDDEN_PREFIXES))


def allowed(name: str) -> bool:
    if forbidden(name) or name.startswith(OTHER_VENDOR_PREFIXES):
        return False
    return name in ENV_ALLOW or name.startswith(ENV_ALLOW_PREFIXES)


def child_env(environ: Mapping[str, str], env_file: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of an agy process: the allowlisted names of `environ` (the env file's values win, as they do for
    every other probe), PATH fixed. Raises ForbiddenEnv when `env_file` names a Google-family variable."""
    env_file = dict(env_file or {})
    bad = [k for k in env_file if forbidden(k)]
    if bad:
        raise ForbiddenEnv(bad)
    merged = {**dict(environ), **env_file}
    env = {k: v for k, v in merged.items() if allowed(k)}
    home = env.get("HOME") or str(Path.home())
    env["HOME"] = home
    env["PATH"] = FIXED_PATH.format(home=home)
    return env


def resolve_binary(configured: str | None) -> str:
    """The configured agy binary as an absolute path. LaneNotConfigured when nothing is configured; ProbeBinaryMissing
    (with `searched`) when the path is relative or is not an executable file. PATH is never searched."""
    if not configured or not configured.strip():
        raise LaneNotConfigured("no agy binary configured (provider --agy-bin or CORTADOS_AGY_BIN)")
    path = os.path.expanduser(configured.strip())
    if not os.path.isabs(path):
        raise ProbeBinaryMissing([configured], f"configured path {configured!r} is not absolute")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise ProbeBinaryMissing([path], f"{path} is not an executable file")
    return path


def _walk(node: Any, key: str = ""):
    """Yield (key, value) for every scalar in a JSON tree; the key is the nearest dict key."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, str(k))
            if isinstance(v, (dict, list)):
                yield str(k), v
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


def _risky_key(key: str) -> str | None:
    k = key.lower().replace("_", "").replace("-", "")
    if k.startswith("mcp"):
        return "MCP servers are configured"
    if "apikey" in k:
        return "an API key is configured"
    if k in ("adc", "adcauth", "useadc") or "applicationdefaultcredentials" in k:
        return "Application Default Credentials are configured"
    return None


def check_settings(home: str | os.PathLike) -> None:
    """Refuse (SettingsRefused) when agy's settings would let it act on its own or bill elsewhere. Read-only. A missing
    file passes (agy's defaults: request-review, subscription login). See the module docstring: PROVISIONAL matching."""
    p = Path(home) / SETTINGS_RELPATH
    try:
        if not p.exists():
            return
        if p.stat().st_size > SETTINGS_MAX_BYTES:
            raise SettingsRefused(f"agy settings file is over {SETTINGS_MAX_BYTES} bytes; not read")
        data = json.loads(p.read_text())
    except SettingsRefused:
        raise
    except (OSError, ValueError) as exc:
        raise SettingsRefused(f"agy settings file unreadable ({type(exc).__name__}); refused, fail closed")
    problems: list[str] = []
    for key, value in _walk(data):
        why = _risky_key(key)
        if why and value not in (None, False, "", [], {}):
            problems.append(why)
        if isinstance(value, str) and value.strip().lower() in AUTO_APPROVE_VALUES:
            problems.append(f"tool permission mode {value.strip().lower()!r} runs tools without review")
    if problems:
        raise SettingsRefused("agy settings refused: " + "; ".join(sorted(set(problems))))
