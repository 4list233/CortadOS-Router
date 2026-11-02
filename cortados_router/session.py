"""Run a whole Claude Code session on the cortados provider.

    python3 -m cortados_router.session [--orchestrator fable|opus|sonnet|<model id>|cortados]
        [--provider-url URL] [--token-file PATH] [--no-delegate-directive] [--print-env] [-- <claude args>]

The main loop (the orchestrator) is pinned to a subscription model (`sub/<id>`, no S1). Claude Code's NATIVE
subagents, which keep every tool including MCP, are sent to `cortados` so S1 routes each one. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ORCHESTRATORS = {
    "fable": "sub/claude-fable-5-1",
    "opus": "sub/claude-opus-5-5",
    "sonnet": "sub/claude-sonnet-5-5",
}
DEFAULT_ORCHESTRATOR = "fable"
DEFAULT_TOKEN_FILE = "~/.config/cortados/provider-token"
# Side calls (titles, summaries) skip S1: they are tiny, and S1 scoring would add latency to every one.
SMALL_FAST_MODEL = "sub/claude-haiku-5-5"
DIRECTIVE_FILE = Path(__file__).resolve().parent.parent / "integrations" / "orchestrator-directive.md"
# Same list the plugin blanks (integrations/claude-mod/cortados-routed-subagents/hooks/register.ts) so no
# alternative provider in the user's env can take the session away from ANTHROPIC_BASE_URL.
BLANKED = (
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
)
SECRET_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# The routed-subagents plugin would run subagents as separate `claude -p` processes without MCP tools and would add
# its directive a second time. It is turned off for this session only (`enabledPlugins` is the settings key in
# Claude Code 2.1.316; explicit enabledPlugins values win over defaults).
PLUGIN_ID = "cortados-routed-subagents@cortados-router"
SETTINGS_JSON = '{"enabledPlugins":{"%s":false}}' % PLUGIN_ID


class SessionError(Exception):
    pass


def resolve_model(name: str) -> str:
    name = name.strip()
    if not name:
        raise SessionError("empty --orchestrator")
    if name in ORCHESTRATORS:
        return ORCHESTRATORS[name]
    if name == "cortados" or name.startswith("sub/"):
        return name
    return "sub/" + name


def read_token(path: str) -> str:
    p = Path(os.path.expanduser(path))
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SessionError(f"cannot read provider token file {p}: {exc.strerror}") from None
    if not token:
        raise SessionError(f"provider token file {p} is empty")
    return token


def build_env(base: dict, provider_url: str, token: str) -> dict:
    env = dict(base)
    env.update(
        {
            "ANTHROPIC_BASE_URL": provider_url,
            # Both, so a saved Console key cannot leak in through either variable.
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_API_KEY": token,
            "CLAUDE_CODE_SUBAGENT_MODEL": "cortados",
            # Claude Code 2.1.316 lists CLAUDE_CODE_SUBAGENT_MODEL_FORCE next to CLAUDE_CODE_SUBAGENT_MODEL among its
            # model env vars (and among the model-selection vars a settings `env` may set). Per its name it forces
            # the subagent model even when an Agent call or agent definition names one (e.g. `model: haiku`); without
            # it such subagents would bypass S1. Set it so every subagent is routed. Not exercised end to end here.
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
            "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "cortados",
            "ANTHROPIC_SMALL_FAST_MODEL": SMALL_FAST_MODEL,
            "ANTHROPIC_CUSTOM_HEADERS": "x-cortados-reveal: 1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CORTADOS_WHOLE_SESSION": "1",
        }
    )
    for key in BLANKED:
        env[key] = ""
    return env


def build_argv(model: str, directive: str | None, extra: list) -> list:
    argv = ["claude", "--model", model, "--settings", SETTINGS_JSON]
    if directive:
        argv += ["--append-system-prompt", directive]
    return argv + list(extra)


def mask(env: dict) -> dict:
    return {k: ("***" if k in SECRET_KEYS and v else v) for k, v in env.items()}


def format_env(env: dict, base: dict) -> str:
    shown = mask({k: v for k, v in env.items() if base.get(k) != v or k in SECRET_KEYS})
    return "\n".join(f"{k}={shown[k]}" for k in sorted(shown))


def parse(argv: list):
    extra: list = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="python3 -m cortados_router.session", description=__doc__.split("\n")[0])
    ap.add_argument("--orchestrator", default=DEFAULT_ORCHESTRATOR)
    ap.add_argument("--provider-url")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--no-delegate-directive", action="store_true")
    ap.add_argument("--print-env", action="store_true")
    return ap.parse_args(argv), extra


def prepare(argv: list, base_env: dict):
    args, extra = parse(argv)
    url = (args.provider_url or base_env.get("CORTADOS_PROVIDER_URL") or "").strip()
    if not url:
        raise SessionError("no provider URL: pass --provider-url or set CORTADOS_PROVIDER_URL")
    model = resolve_model(args.orchestrator)
    token = read_token(args.token_file)
    directive = None
    if not args.no_delegate_directive:
        directive = DIRECTIVE_FILE.read_text(encoding="utf-8").strip()
    return args, build_env(base_env, url, token), build_argv(model, directive, extra)


def main(argv=None, base_env=None, out=sys.stdout, err=sys.stderr) -> int:
    base = dict(os.environ if base_env is None else base_env)
    try:
        args, env, cmd = prepare(list(sys.argv[1:] if argv is None else argv), base)
    except SessionError as exc:
        print(f"cortados session: {exc}", file=err)
        return 2
    if args.print_env:
        print(format_env(env, base), file=out)
        print("argv: " + " ".join(repr(a) if " " in a else a for a in cmd), file=out)
        return 0
    os.execvpe(cmd[0], cmd, env)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
