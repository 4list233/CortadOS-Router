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
  4. SETTINGS GUARD (`check_settings`): the files agy 1.5.16 reads its configuration from are read (never written)
     before any start and must match ALLOWLIST schemas: antigravity-cli/settings.json (`trustedWorkspaces` only; missing
     = first run pending), config/config.json (`plugins.swarm.enabled` only), config/mcp_config.json (no MCP server),
     and the Gemini CLI's settings.json if present (no API-key / Vertex / ADC auth mode, no MCP server). A non-object,
     an unknown key anywhere, an unparsable non-empty file: refused (fail closed).
  4a. DOTENV GUARD (`check_dotenv`, #144 round 3): the Gemini CLI family loads a `.env` file into its own process
     environment AFTER we built it: the first of `<dir>/.gemini/.env`, `<dir>/.env` walking up from its working
     directory, then `~/.gemini/.env`, `~/.env`. A `GEMINI_API_KEY` or `GOOGLE_APPLICATION_CREDENTIALS` there would
     restore exactly what rule 1 stripped. Every one of those files that exists (not only the first: which one agy reads
     is not documented) is read before any start; a Google-family name (FORBIDDEN_PREFIXES) or an unparsable file is
     refused (`forbidden-env`). The launch folder is a fixed parent we own plus an empty per-run folder, so the walk is
     the same on every run.
  4b. SUBSCRIPTION PROOF (`check_subscription`): agy stores no auth mode, so the probe proves the personal Google AI
     subscription from agy's own start-up banner (`<email> (Google AI Pro)`; the email is scrubbed, never kept).
  5. ONE LAUNCH PATH (`prepare`): rules 1-4a run, in that order, before every agy process: the quota probe
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
import re
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


# A name is echoed in an error only when it looks like a variable / setting name (#144 round 3: an email used as a key
# reached the error, the state and stderr). Anything else is counted, never shown.
SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,40}$")


def safe_names(names) -> str:
    """The names that match SAFE_NAME, sorted, plus a count of the others: what an error may say about names."""
    names = list(names)
    shown = sorted({n for n in names if isinstance(n, str) and SAFE_NAME.match(n)})
    hidden = len(names) - sum(1 for n in names if isinstance(n, str) and SAFE_NAME.match(n))
    parts = ", ".join(shown)
    if hidden:
        parts += (", " if parts else "") + f"{hidden} other name(s) not shown"
    return parts


class ForbiddenEnv(AgyLaneError):
    kind = "forbidden-env"

    def __init__(self, names: list[str], where: str = "the router's env file"):
        # names only (safe ones; others counted), never values: a value here is a credential
        super().__init__(f"forbidden variables configured for the agy lane in {where}: "
                         + safe_names(names) + " (remove them; they bill outside the subscription)")
        self.names = sorted(n for n in names if SAFE_NAME.match(n))


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
# agy's files are validated against allowlist schemas, never scanned for bad words (#144 review round 1: the earlier
# blacklist accepted `[]` and any unknown setting). A leaf is a type (bool / str: any value of that type), a tuple of the
# allowed values, or `[type]` (a list whose every item is that type). Everything not listed is refused.
#
# Round 2 (2026-11-24): the shapes are agy 1.5.16's real files, read on the Air (key names and types only, values not
# printed). agy stores NO auth mode in its settings (round 1 required `security.auth.selectedType`, guessed from the
# Gemini CLI's file on the Pro, and would have refused the owner's real install). The subscription login is proven from
# what agy shows instead (`check_subscription`, below).
_DISPLAY_ONLY = "display only: changes how the screen is drawn, never what agy runs or how it bills"
# ~/.gemini/antigravity-cli/settings.json. MISSING = agy's first run is pending (FirstRunPending, not "unsafe").
AGY_SETTINGS_SCHEMA: dict[str, Any] = {
    # the folders the owner answered "trust" for; trust only skips agy's own prompt for that folder, it grants no tool and
    # changes no billing. The /usage probe's fixed folder is trusted here by the owner once, by hand (docs/AGY-LANE.md);
    # the router reads this file, never writes it
    "trustedWorkspaces": [str],
}
# ~/.gemini/config/config.json. Missing is fine: no plugin is configured.
CONFIG_RELPATH = ".gemini/config/config.json"
CONFIG_SCHEMA: dict[str, Any] = {
    # Swarm is agy's built-in multi-agent plugin (on by default in 1.5.16). It adds no outside tool and no billing path,
    # but one request can fan out to several agents: it MULTIPLIES quota use (docs/AGY-LANE.md). Allowed on or off.
    "plugins": {"swarm": {"enabled": bool}},
}
# ~/.gemini/config/mcp_config.json: no MCP server may be configured. agy 1.5.16 leaves it as an EMPTY file (0 bytes).
MCP_CONFIG_RELPATH = ".gemini/config/mcp_config.json"
# ~/.gemini/settings.json is the GEMINI CLI's file, absent on the Air; agy may read it, so when it exists its auth mode
# must be the Google login and it may configure no MCP server. Its other keys are the Gemini CLI's business.
GEMINI_CLI_SETTINGS_RELPATH = ".gemini/settings.json"
SUBSCRIPTION_AUTH = "oauth-personal"              # the Gemini CLI's "Login with Google" (read on the Pro, 2026-11-24)
BILLING_AUTH_MODES = ("gemini-api-key", "api-key", "vertex-ai", "compute-default-credentials", "cloud-shell", "adc")
# Named in the refusal so the owner sees why, not just "unknown key" (all are refused by the allowlist anyway).
_NAMED_REFUSALS = {
    "mcpservers": "MCP servers are configured (they give agy tools the router never reviewed)",
    "mcp": "MCP settings are configured",
    "tools": "tool settings are configured (auto-approve / allowed tools)",
    "approvalmode": "an approval mode is configured (auto-approve runs tools without review)",
    "autoaccept": "auto-accept is configured (runs tools without review)",
    "yolo": "yolo mode is configured (runs every tool without review)",
    "security": "security settings are configured (agy 1.5.16 writes none; an auth mode there may bill elsewhere)",
}


class FirstRunPending(AgyLaneError):
    """agy's own settings file does not exist yet: agy has never finished its first run on this account."""
    kind = "first-run-pending"


class SubscriptionUnproven(AgyLaneError):
    """agy's screen does not prove the Google AI subscription login (an API key / Vertex / enterprise screen, no tier
    shown, or the tier pattern not yet confirmed against a real capture)."""
    kind = "subscription-unproven"


def _settings_problems(node: Any, schema: Any, path: str, out: list[str]) -> None:
    if isinstance(schema, dict):
        if not isinstance(node, dict):
            out.append(f"{path or 'the file'} is not a JSON object")
            return
        unknown = 0
        for k, v in node.items():
            if k not in schema:
                canon = str(k).lower().replace("_", "").replace("-", "")
                named = _NAMED_REFUSALS.get(canon)
                if named:       # the canonical name, never the key as written
                    out.append(f"{f'{path}.{canon}' if path else canon}: {named}")
                else:
                    unknown += 1
                continue
            _settings_problems(v, schema[k], f"{path}.{k}" if path else str(k), out)
        if unknown:
            # counted, never echoed (#144 round 3: an email used as a key was echoed); `path` holds schema keys only
            out.append(f"{unknown} unknown setting(s) " + (f"under {path!r}" if path else "at the top level"))
        return
    if isinstance(schema, list):
        if not isinstance(node, list):
            out.append(f"{path} is not a list")
        elif any(type(x) is not schema[0] for x in node):
            out.append(f"{path} holds an item that is not a {schema[0].__name__}")
        return
    if isinstance(schema, tuple):
        if not any(node is ok or (type(node) is type(ok) and node == ok) for ok in schema):
            out.append(f"{path} has a value outside {list(schema)!r}")
        return
    if type(node) is not schema:            # bool is not int, str is str: no coercion
        out.append(f"{path} is not a {schema.__name__}")


def _read(p: Path, label: str) -> tuple[bool, str]:
    """(exists, text) of one file, read-only. Over the size cap or unreadable: SettingsRefused (fail closed)."""
    try:
        if not p.exists():
            return False, ""
        if p.stat().st_size > SETTINGS_MAX_BYTES:
            raise SettingsRefused(f"{label} is over {SETTINGS_MAX_BYTES} bytes; not read")
        return True, p.read_text()
    except SettingsRefused:
        raise
    except (OSError, ValueError) as exc:
        raise SettingsRefused(f"{label} unreadable ({type(exc).__name__}); refused, fail closed")


def _json(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SettingsRefused(f"{label} is not JSON ({type(exc).__name__}); refused, fail closed")


def _check_schema(data: Any, schema: dict[str, Any], label: str) -> None:
    problems: list[str] = []
    _settings_problems(data, schema, "", problems)
    if problems:
        # key paths and fixed words only: a value is never echoed (it may be a credential or a private path)
        raise SettingsRefused(f"{label} refused: " + "; ".join(sorted(set(problems))))


def _check_mcp_config(home: Path) -> None:
    """Allowed only missing, empty (0 bytes or whitespace), `{}` or `{"mcpServers": {}}`: no server entry at all."""
    label = MCP_CONFIG_RELPATH
    exists, text = _read(home / label, label)
    if not exists or not text.strip():
        return
    data = _json(text, label)
    if data == {} or (isinstance(data, dict) and set(data) == {"mcpServers"} and data["mcpServers"] == {}):
        return
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) and data["mcpServers"]:
        raise SettingsRefused(f"{label} refused: MCP servers are configured (they give agy tools the router never reviewed)")
    raise SettingsRefused(f"{label} refused: only an empty file, {{}} or {{\"mcpServers\": {{}}}} is allowed")


def _check_gemini_cli_settings(home: Path) -> None:
    """The Gemini CLI's ~/.gemini/settings.json, if it exists (agy may read it): a JSON object whose auth mode, where one
    is set, is the Google login, with no MCP server. Absent is fine (the Air has none)."""
    label = GEMINI_CLI_SETTINGS_RELPATH
    exists, text = _read(home / label, label)
    if not exists:
        return
    data = _json(text, label) if text.strip() else {}
    if not isinstance(data, dict):
        raise SettingsRefused(f"{label} refused: the file is not a JSON object")
    problems = []
    sec = data.get("security")
    auth = sec.get("auth") if isinstance(sec, dict) else None
    # current Gemini CLI: security.auth.selectedType; older ones: top-level selectedAuthType
    for where, mode in (("security.auth.selectedType", auth.get("selectedType") if isinstance(auth, dict) else None),
                        ("selectedAuthType", data.get("selectedAuthType"))):
        if mode is None or mode == SUBSCRIPTION_AUTH:
            continue
        if isinstance(mode, str) and mode.strip().lower() in BILLING_AUTH_MODES:
            problems.append(f"{where} is {mode.strip().lower()!r}, which bills outside the Google AI Pro subscription")
        else:
            problems.append(f"{where} is not the Google login {SUBSCRIPTION_AUTH!r}")   # the value is never echoed
    if (sec is not None and not isinstance(sec, dict)) or (auth is not None and not isinstance(auth, dict)):
        problems.append("security / security.auth is not a JSON object")
    if data.get("mcpServers"):
        problems.append("mcpServers: MCP servers are configured (they give agy tools the router never reviewed)")
    if problems:
        raise SettingsRefused(f"{label} refused: " + "; ".join(sorted(set(problems))))


def check_settings(home: str | os.PathLike) -> None:
    """Read-only checks of every file agy 1.5.16 reads its configuration from, before any agy process starts:
    antigravity-cli/settings.json (AGY_SETTINGS_SCHEMA; missing: FirstRunPending), config/config.json (CONFIG_SCHEMA;
    missing is fine), config/mcp_config.json (no server) and, if present, the Gemini CLI's settings.json (no billing auth
    mode, no MCP server). Unparsable non-empty files, non-objects and unknown keys are refused (SettingsRefused)."""
    home = Path(home)
    exists, text = _read(home / SETTINGS_RELPATH, SETTINGS_RELPATH)
    if not exists:
        raise FirstRunPending(f"{SETTINGS_RELPATH} does not exist: agy's first run is pending (start agy once by hand)")
    _check_schema(_json(text, SETTINGS_RELPATH), AGY_SETTINGS_SCHEMA, SETTINGS_RELPATH)
    exists, text = _read(home / CONFIG_RELPATH, CONFIG_RELPATH)
    if exists:
        _check_schema(_json(text, CONFIG_RELPATH), CONFIG_SCHEMA, CONFIG_RELPATH)
    _check_mcp_config(home)
    _check_gemini_cli_settings(home)


# --------------------------------------------------------------------------------------------- dotenv guard (#144 round 3)
# The Gemini CLI family's dotenv search: from the working directory up to the root, `<dir>/.gemini/.env` then
# `<dir>/.env`; then `~/.gemini/.env`, `~/.env`. It loads the first one found into its process environment, after our
# allowlist was applied. Every existing candidate is checked (which file agy 1.5.16 reads is not documented).
DOTENV_NAMES = (".gemini/.env", ".env")
# One assignment per line as the dotenv parser takes it: optional `export`, a name, `=` (or `: `), a value. A quoted
# value must close on its line: a multi-line value could carry a name we never saw, so it is refused as unparsable.
_DOTENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*(?:=|:\s)\s*(.*)$")


def dotenv_candidates(home: str | os.PathLike, workdir: str | os.PathLike) -> list[Path]:
    """Every dotenv file an agy started in `workdir` (or a fresh folder inside it) may load, existing or not, in search
    order. Both the path as given and its real path are walked (macOS: /var is /private/var)."""
    out: list[Path] = []
    starts = []
    for d in (os.path.abspath(workdir), os.path.realpath(workdir)):
        if d not in starts:
            starts.append(d)
    for start in starts:
        d = Path(start)
        while True:
            out += [d / n for n in DOTENV_NAMES]
            if d.parent == d:
                break
            d = d.parent
    for h in (os.path.abspath(home), os.path.realpath(home)):
        out += [Path(h) / n for n in DOTENV_NAMES]
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def dotenv_names(text: str) -> list[str]:
    """The variable names a dotenv file defines. ValueError (with a line number only) when a line is not a comment, a
    blank or one complete assignment."""
    names = []
    for i, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _DOTENV_LINE.match(line)
        if not m:
            raise ValueError(f"line {i} is not an assignment")
        value = m.group(2).strip()
        if value[:1] in ("'", '"', "`"):
            q = value[0]
            body = value[1:]
            close = re.search(r"(?<!\\)" + re.escape(q), body)
            if not close:
                raise ValueError(f"line {i} opens a quoted value that does not close on the line")
        names.append(m.group(1))
    return names


def _dotenv_label(p: Path, home: str | os.PathLike) -> str:
    for h in (os.path.abspath(home), os.path.realpath(home)):
        try:
            return "~/" + str(p.relative_to(h))
        except ValueError:
            continue
    return str(p)


def check_dotenv(home: str | os.PathLike, workdir: str | os.PathLike) -> None:
    """Refuse when any dotenv file an agy started in `workdir` may load defines a Google-family variable (ForbiddenEnv),
    or cannot be read or parsed (SettingsRefused, fail closed). Values are never read into a message; names only when SAFE_NAME."""
    for p in dotenv_candidates(home, workdir):
        label = _dotenv_label(p, home)
        try:
            if not (p.exists() or p.is_symlink()):
                continue
            if p.stat().st_size > SETTINGS_MAX_BYTES:
                raise SettingsRefused(f"{label} is over {SETTINGS_MAX_BYTES} bytes; not read, refused (fail closed)")
            text = p.read_text()
        except SettingsRefused:
            raise
        except (OSError, ValueError) as exc:
            raise SettingsRefused(f"{label} unreadable ({type(exc).__name__}); refused, fail closed")
        try:
            names = dotenv_names(text)
        except ValueError as exc:      # the message holds a line number only, never the line
            raise SettingsRefused(f"{label} is not a parsable dotenv file ({exc}); refused, fail closed")
        bad = [n for n in names if forbidden(n.upper())]
        if bad:
            raise ForbiddenEnv(bad, label)


# --------------------------------------------------------------------------------------------- subscription login proof
# agy's settings hold no auth mode, so the Google AI subscription login is proven from what agy itself SHOWS. The tier is
# on the start-up banner, the account line under "Antigravity CLI 1.5.16": `<email> (Google AI Pro)`. The /usage
# "Models & Quota" box shows only `Account: <email>`, no tier (owner's agy 1.5.16 screens, 2026-11-24; fixture
# tests/fixtures/agy-banner.txt). Read-only evidence; the account email is matched and dropped (`scrub_account`) before
# anything is checked, never stored, logged or echoed.
#
# Confirmed against the owner's agy 1.5.16 banner on 2026-11-24 (#144). The flag stays so a future agy whose banner
# changes can be put back to refusing in one line.
TIER_PATTERN_CONFIRMED = True
# The banner's account line, after the scrub: `<account> (Google AI Pro)`. Personal Google AI subscription tiers only;
# Ultra is a personal tier too (same login, same billing path). Any other parenthesised text (a free / enterprise tier, no
# tier at all) does not match and refuses. Horizontal whitespace only (`[ \t]`, never `\s`, which matches newlines): the tier
# must sit on the account line itself; a tier on a later line, LF or CRLF, is not this account's tier and refuses.
TIER_PATTERN = re.compile(r"<account>[ \t]*\([ \t]*Google[ \t]+AI[ \t]+(Pro|Ultra)[ \t]*\)", re.I)
# Any of these on agy's screen means the session bills outside a personal subscription: refused even with a tier shown.
NON_SUBSCRIPTION = re.compile(
    r"\bapi[\s_-]?key\b|\bvertex\b|\benterprise\b|pay[\s-]*as[\s-]*you[\s-]*go|application default credentials"
    r"|\bADC\b|cloud project|billing account|code assist", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def scrub_account(text: str) -> str:
    """The screen with every email address (the Account: line's included) replaced by `<account>`."""
    return EMAIL.sub("<account>", text)


def banner_tier(text: str) -> str | None:
    """The tier word ("Pro" / "Ultra") of the start-up banner's account line in `text` (raw or scrubbed), else None.

    The ONE account-line proof (#144 fix round): the tier check below, the probe's boot readiness and the clearing of
    agy's transient "not signed in" notice all call this. A bare email anywhere else on screen (a "Choose account"
    picker, a sign-in prompt) is not an account line and never counts."""
    m = TIER_PATTERN.search(scrub_account(text))
    return m.group(1) if m else None


def check_subscription(banner: str, usage: str = "") -> str:
    """The personal tier agy's start-up banner shows ("AI Pro" / "AI Ultra"), or SubscriptionUnproven. The tier is read
    from `banner` only (the account line `<email> (Google AI Pro)`); both `banner` and `usage` (the /usage answer) are
    checked for a non-subscription login: an API key / Vertex / enterprise / pay-as-you-go / Cloud billing anywhere
    refuses. Also refused: no tier, an unknown tier, or TIER_PATTERN_CONFIRMED False. Messages hold fixed words and the
    tier only, never screen text."""
    banner_text = scrub_account(banner)
    bad = NON_SUBSCRIPTION.search(banner_text + "\n" + scrub_account(usage))
    if bad:
        raise SubscriptionUnproven("agy's screen names a non-subscription login or billing "
                                   f"({re.sub(r'[^a-z ]', ' ', bad.group(0).lower()).strip()!r}); refused")
    word = banner_tier(banner)
    if not word:
        raise SubscriptionUnproven("agy's start-up banner shows no personal Google AI subscription tier; refused")
    tier = f"AI {word.title()}"
    if not TIER_PATTERN_CONFIRMED:
        raise SubscriptionUnproven(f"tier {tier!r} matched a pattern not confirmed for this agy version; refused")
    return tier


# --------------------------------------------------------------------------------------------- the one launch path
def prepare(configured: str | None, environ: Mapping[str, str], env_file: Mapping[str, str] | None = None, *,
            workdir: str | os.PathLike) -> tuple[str, dict[str, str]]:
    """Every agy process the router starts goes through here first (the /usage probe and `agy models`): the configured
    absolute binary, the allowlisted env (Google-family names in the env file: ForbiddenEnv), the settings guard, the
    dotenv guard for `workdir` (the /usage probe's fixed folder, or the fixed parent `agy models`' per-run folder is made
    in). The caller checks the folder it starts agy in again with check_dotenv just before the start. Returns (binary, env); raises an AgyLaneError before
    anything is started."""
    binary = resolve_binary(configured)
    env = child_env(environ, env_file)
    check_settings(env["HOME"])
    check_dotenv(env["HOME"], workdir)
    return binary, env


def run_models(configured: str | None, environ: Mapping[str, str], env_file: Mapping[str, str] | None = None,
               timeout_s: float = 60, workdir: str | os.PathLike | None = None) -> str:
    """`agy models` through prepare(): stdout as text. Run by absolute path with execve semantics (no PATH search), in an
    empty per-run folder inside `workdir` (default: the router's own `$CORTADOS_HOME/usage-probe`, the probe's fixed
    parent, so the dotenv walk is the same as the probe's and never the caller's cwd), stdin closed. Raises AgyLaneError
    on a refusal, RuntimeError on a failed run."""
    import shutil
    import subprocess
    import tempfile
    if workdir is None:
        from cortados_router import scarcity
        workdir = Path(scarcity.STATE) / "usage-probe"
    binary, env = prepare(configured, environ, env_file, workdir=workdir)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    # "agy-models-*", not "p*": the probe reaper's stale sweep only touches p* folders
    work = tempfile.mkdtemp(prefix="agy-models-", dir=str(workdir))
    try:
        check_dotenv(env["HOME"], work)          # the per-run folder itself (fresh and empty; checked, not assumed)
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
