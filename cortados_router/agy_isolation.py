"""Isolation of every Antigravity CLI (`agy`) process the router starts (#144). The one owner of these rules.

Today the only agy process is the quota probe (scarcity._antigravity: `/usage` in a PTY). A future served backend (#144
PR B, [ESCALATE]) must start agy through the same functions, so the rules cannot drift between the two.

The rules, each with its reason:
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
  4. SETTINGS GUARD (`check_settings`): agy's own settings file is read (never written) before any start and must match
     an ALLOWLIST schema (`SETTINGS_SCHEMA`): a JSON object, only listed keys, the auth mode the subscription login
     (`SUBSCRIPTION_AUTH`). Anything else is refused: a non-object, an unknown key anywhere (MCP servers, tool /
     approval modes included), an API-key / Vertex / ADC auth mode, a missing or unreadable file (fail closed).
  5. ONE LAUNCH PATH (`prepare`): rules 1-4 run, in that order, before every agy process: the quota probe
     (scarcity._antigravity) and `agy models` (`run_models`, the CLI `python3 -B -m cortados_router.agy_isolation models`
     that bin/refresh-inventory.sh calls).

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


# --------------------------------------------------------------------------------------------- settings guard (ALLOWLIST)
# agy's settings.json is validated against this schema, never scanned for bad words (#144 review round 1: the earlier
# blacklist accepted `[]`, `{"security":{"auth":{"selectedType":"api-key"}}}` and any unknown security setting).
# A leaf is a type (bool / str: any value of that type) or a tuple of the allowed values. Everything not listed is refused.
#
# The auth mode is REQUIRED and must be the subscription login. "oauth-personal" is the value the owner's signed-in
# Google login writes ("Login with Google"; read 2026-11-24 from the owner's ~/.gemini/settings.json on the Pro, the
# Gemini CLI file agy's settings descend from: security.auth.selectedType). Every other mode bills elsewhere:
# "gemini-api-key" / "api-key" (API-key billing), "vertex-ai" (a Cloud project), "compute-default-credentials" /
# "cloud-shell" / ADC (Application Default Credentials, Cloud billing). If agy 1.5.16 on the Air writes a different key or
# value, the lane says "refused: unsafe agy settings" with the reason, and this schema is updated to the captured file.
SUBSCRIPTION_AUTH = "oauth-personal"
BILLING_AUTH_MODES = ("gemini-api-key", "api-key", "vertex-ai", "compute-default-credentials", "cloud-shell", "adc")
_DISPLAY_ONLY = "display only: changes how the screen is drawn, never what agy runs or how it bills"
SETTINGS_SCHEMA: dict[str, Any] = {
    "security": {"auth": {"selectedType": (SUBSCRIPTION_AUTH,)}},
    # Allowed outside security, each provably harmless (the reason per key):
    "ui": {
        "theme": str,                         # the first-run color scheme; _DISPLAY_ONLY
        "showStatusInTitle": bool,            # _DISPLAY_ONLY (terminal title)
        "showMemoryUsage": bool,              # _DISPLAY_ONLY
        "hideBanner": bool,                   # _DISPLAY_ONLY
        "hideTips": bool,                     # _DISPLAY_ONLY
        "footer": {"hideContextPercentage": bool},   # _DISPLAY_ONLY
    },
    "ide": {
        "enabled": (False,),                  # IDE integration OFF only: when on, agy connects to an IDE companion server
                                              # that can add tools and diffs; off is the absence of that connection
        "hasSeenNudge": bool,                 # a "has seen the IDE hint" flag; no effect on what runs
    },
    "hasSeenIdeIntegrationNudge": bool,       # the same flag at the older top-level name
}
REQUIRED_SETTINGS = (("security", "auth", "selectedType"),)
# Named in the refusal so the owner sees why, not just "unknown key" (all are refused by the allowlist anyway).
_NAMED_REFUSALS = {
    "mcpservers": "MCP servers are configured (they give agy tools the router never reviewed)",
    "mcp": "MCP settings are configured",
    "tools": "tool settings are configured (auto-approve / allowed tools)",
    "approvalmode": "an approval mode is configured (auto-approve runs tools without review)",
    "autoaccept": "auto-accept is configured (runs tools without review)",
    "yolo": "yolo mode is configured (runs every tool without review)",
}


def _settings_problems(node: Any, schema: Any, path: str, out: list[str]) -> None:
    if isinstance(schema, dict):
        if not isinstance(node, dict):
            out.append(f"{path or 'the file'} is not a JSON object")
            return
        for k, v in node.items():
            where = f"{path}.{k}" if path else str(k)
            if k not in schema:
                named = _NAMED_REFUSALS.get(str(k).lower().replace("_", "").replace("-", ""))
                out.append(f"{where}: {named}" if named else f"unknown setting {where!r}")
                continue
            _settings_problems(v, schema[k], where, out)
        return
    if isinstance(schema, tuple):
        if not any(node is ok or (type(node) is type(ok) and node == ok) for ok in schema):
            if path == "security.auth.selectedType":
                if isinstance(node, str) and node.strip().lower() in BILLING_AUTH_MODES:
                    out.append(f"{path} is {node.strip().lower()!r}, which bills outside the Google AI Pro subscription")
                else:
                    out.append(f"{path} is not the subscription login {SUBSCRIPTION_AUTH!r}")
            else:
                out.append(f"{path} has a value outside {list(schema)!r}")
        return
    if type(node) is not schema:            # bool is not int, str is str: no coercion
        out.append(f"{path} is not a {schema.__name__}")


def check_settings(home: str | os.PathLike) -> None:
    """Refuse (SettingsRefused) unless agy's settings file matches SETTINGS_SCHEMA exactly: a JSON object, only listed keys,
    the subscription login as the auth mode. Read-only. A missing or unreadable file is refused (fail closed): without
    the file the auth mode is unknown, and an unknown auth mode is not proven to be the subscription."""
    p = Path(home) / SETTINGS_RELPATH
    try:
        if not p.exists():
            raise SettingsRefused(f"agy settings file {SETTINGS_RELPATH} missing: the auth mode is unknown; sign in with "
                                  f"Google once (it records security.auth.selectedType = {SUBSCRIPTION_AUTH!r})")
        if p.stat().st_size > SETTINGS_MAX_BYTES:
            raise SettingsRefused(f"agy settings file is over {SETTINGS_MAX_BYTES} bytes; not read")
        data = json.loads(p.read_text())
    except SettingsRefused:
        raise
    except (OSError, ValueError) as exc:
        raise SettingsRefused(f"agy settings file unreadable ({type(exc).__name__}); refused, fail closed")
    problems: list[str] = []
    _settings_problems(data, SETTINGS_SCHEMA, "", problems)
    if isinstance(data, dict):
        for req in REQUIRED_SETTINGS:
            node = data
            for k in req:
                node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                problems.append(f"{'.'.join(req)} missing (must be {SUBSCRIPTION_AUTH!r}, the subscription login)")
    if problems:
        # key paths and fixed words only: a value that is not a known auth mode is never echoed (it may be a credential)
        raise SettingsRefused("agy settings refused: " + "; ".join(sorted(set(problems))))


# --------------------------------------------------------------------------------------------- the one launch path
def prepare(configured: str | None, environ: Mapping[str, str], env_file: Mapping[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """Every agy process the router starts goes through here first (the /usage probe and `agy models`): the configured
    absolute binary, the allowlisted env (Google-family names in the env file: ForbiddenEnv), the settings guard.
    Returns (binary, env); raises an AgyLaneError before anything is started."""
    binary = resolve_binary(configured)
    env = child_env(environ, env_file)
    check_settings(env["HOME"])
    return binary, env


def run_models(configured: str | None, environ: Mapping[str, str], env_file: Mapping[str, str] | None = None,
               timeout_s: float = 60) -> str:
    """`agy models` through prepare(): stdout as text. Run by absolute path with execve semantics (no PATH search), in an
    empty per-run folder, stdin closed. Raises AgyLaneError on a refusal, RuntimeError on a failed run."""
    import shutil
    import subprocess
    import tempfile
    binary, env = prepare(configured, environ, env_file)
    work = tempfile.mkdtemp(prefix="cortados-agy-models-")
    try:
        r = subprocess.run([binary, "models"], cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout_s, executable=binary)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if r.returncode != 0:
        raise RuntimeError(f"agy models exited {r.returncode}")
    return r.stdout.decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    """CLI: `python3 -B -m cortados_router.agy_isolation models` prints `agy models` (bin/refresh-inventory.sh). The binary
    is CORTADOS_AGY_BIN (absolute; never searched), the env file the router's ($CORTADOS_HOME/env). Exit 0 printed, 2 refused
    or not configured (the reason on stderr), 1 the run failed."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["models"]:
        print("usage: python3 -B -m cortados_router.agy_isolation models", file=sys.stderr)
        return 2
    from cortados_router import scarcity
    env_file = scarcity._env_file_vars()
    configured = os.environ.get("CORTADOS_AGY_BIN") or env_file.get("CORTADOS_AGY_BIN")
    try:
        sys.stdout.write(run_models(configured, os.environ, env_file))
    except AgyLaneError as exc:
        print(f"agy: {exc.kind}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"agy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
