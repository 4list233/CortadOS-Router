"""Which model server the local gate fronts on its node, and how: `ollama` (today's) or `vllm-mlx` (an MLX
continuous-batching server, docs/LOCAL-RESOURCES.md "MLX backend"). Read by the gate (local_gate.py) at startup and
by scripts/install_mlx_backend.sh (through this module's CLI) to build the vllm-mlx LaunchAgent. Stdlib only.

Config: a JSON file on the node, default ~/.config/cortados/local-backend.json (CORTADOS_LOCAL_BACKEND_CONFIG or the
gate's --backend-config). No file means `ollama` with no governor: the gate behaves exactly as before this file existed.
Example with every key and its reason: tables/local-backend.example.json.

Why no protocol translator: vllm-mlx 0.6.0 serves the Anthropic Messages API itself (POST /v1/messages, read from its
source: server.create_anthropic_message, api/anthropic_adapter.py): text, tool_use / tool_result, streaming SSE
(message_start … content_block_delta incl. input_json_delta and thinking_delta … message_stop), stop reasons and usage.
So the provider's lane relays turns unchanged and the gate adapts only what differs, per backend:

  client route (unchanged)   ollama upstream      vllm-mlx upstream
  GET  /api/tags             GET /api/tags        GET /v1/models, answered in /api/tags shape (models_to_tags)
  POST /v1/messages          POST /v1/messages    POST /v1/messages, body adapted (adapt_messages_body)
  switch status models       GET /api/ps          GET /v1/models

The adaptation lives in the gate, not the provider's lane, because only the gate knows its node's backend: the lane
stays one Anthropic relay for every node, the gate's client-facing route set (ROUTES) does not grow, and the upstream
route each backend may receive is a fixed table here (UPSTREAM). What it changes on POST /v1/messages:
  * thinking: vllm-mlx ignores Anthropic's `thinking` field; it takes chat_template_kwargs.enable_thinking (Qwen3's
    chat template switch). `thinking.type` "disabled" → false, any other type → true; absent → the server's default
    (--default-chat-template-kwargs, false in the example). A client's own chat_template_kwargs.enable_thinking wins.
    vllm-mlx has no per-request thinking budget on this endpoint, so a think variant's budget_tokens is not enforced:
    max_tokens bounds thinking + answer, and the lane's #108 guard catches a turn that thinks its whole budget away.
  * model: vllm-mlx serves one model and 404s any other name. The request must name the served model (`name` and
    `name:latest` the same, as for Ollama) and is sent with the exact served name; another model is a clean 404 here,
    never silently served by the MLX model.
  * errors: FastAPI answers {"detail": ...}; the gate rewraps a non-Anthropic error body in Anthropic's error shape.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from cortados_router import local_governor

BACKENDS = ("ollama", "vllm-mlx")
DEFAULT_CONFIG = "~/.config/cortados/local-backend.json"
CONFIG_ENV = "CORTADOS_LOCAL_BACKEND_CONFIG"
# The upstream request each client route becomes, per backend. Nothing outside this table reaches a backend.
UPSTREAM = {
    "ollama": {("GET", "/api/tags"): ("GET", "/api/tags"), ("POST", "/v1/messages"): ("POST", "/v1/messages")},
    "vllm-mlx": {("GET", "/api/tags"): ("GET", "/v1/models"), ("POST", "/v1/messages"): ("POST", "/v1/messages")},
}
# The switch status's "models loaded" source.
LOADED = {"ollama": "/api/ps", "vllm-mlx": "/v1/models"}
LAUNCH_LABEL = "com.cortados.vllm-mlx"
# The prompt guard: its source in this checkout, and its name in the venv's bin/ (install_mlx_backend.sh copies it).
GUARD_SOURCE = Path(__file__).resolve().parent.parent / "integrations" / "vllm-mlx" / "cortados_vllm_mlx_guard.py"
GUARD_NAME = "cortados-vllm-mlx-guard.py"
# The prefix of the guard's refusal (cortados_vllm_mlx_guard.REFUSAL; a test keeps the two equal).
GUARD_REFUSAL = "cortados guard: prompt too long for this node's context"
# The chat_template_kwargs a request may carry to vllm-mlx, with the type each must have. Only what routing needs:
# enable_thinking, which the gate also sets from Anthropic's `thinking`. Anything else could change what the template
# renders (chat_template, tools, add_generation_prompt, ...) beyond what the input bound reads from the body.
ALLOWED_TEMPLATE_KWARGS: dict[str, type] = {"enable_thinking": bool}
# vllm-mlx serve settings the example sets, and why (each becomes a CLI flag in serve_argv):
SERVE_DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",            # loopback only: the gate is the one door from the tailnet (as for Ollama)
    "port": 8000,                   # vllm-mlx's default; the gate's upstream follows it
    "tool_call_parser": "auto",     # detects Qwen's XML and Hermes JSON tool calls; set the parser the bench used
    "reasoning_parser": "qwen3",    # splits <think>…</think> into Anthropic thinking blocks
    "max_tokens": 32768,            # default max_tokens when a request names none
    "max_request_tokens": 65536,    # the largest max_tokens a client may ask (Claude Code asks up to 64k)
    "timeout_s": 900,               # per request: 8 at once ran 131 s for 512 tokens; long answers need minutes
    "default_chat_template_kwargs": {"enable_thinking": False},   # no thinking unless asked (#108's rule)
}


# vllm_mlx.extra_args may add only these vllm-mlx 0.6.0 serve flags (vllm_mlx/cli.py), each matched by its exact name,
# as `--flag value` or `--flag=value` (True: takes a value) or alone (False). An allowlist, not a denylist: argparse
# keeps the LAST value of a repeated flag and expands abbreviations (`--ho` is `--host`), so an extra argument could
# otherwise undo the flags serve_argv sets for safety: --host (a backend with no authentication must stay on loopback
# behind the gate), --port, --max-num-seqs (the backend's own bound under the gate's admission), --max-tokens and
# --max-request-tokens (the reservation's default and ceiling), --timeout, the served name and the parsers. Every flag
# here changes sampling defaults, output pacing or lowers memory, never exposure, admission or the memory per request.
# Anything else (--api-key, --mcp-config, --kv-cache-quantization, --max-kv-size, ...) belongs in serve or the
# generator, with a review, not in extra_args.
EXTRA_ARGS_ALLOWED: dict[str, bool] = {
    "--default-temperature": True,
    "--default-top-p": True,
    "--default-top-k": True,
    "--default-min-p": True,
    "--default-presence-penalty": True,
    "--default-repetition-penalty": True,
    "--stream-interval": True,            # tokens per streamed chunk
    "--chunked-prefill-tokens": True,     # prefill in chunks: lowers peak memory
    "--disable-prefix-cache": False,      # no cross-request prefix cache: less memory
}


class BackendConfigError(ValueError):
    pass


def check_extra_args(extra: Any) -> list[str]:
    """vllm_mlx.extra_args, validated against EXTRA_ARGS_ALLOWED (BackendConfigError naming the first bad entry)."""
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        raise BackendConfigError("vllm_mlx.extra_args must be a list of strings")
    allowed = ", ".join(sorted(EXTRA_ARGS_ALLOWED))
    i = 0
    while i < len(extra):
        arg = extra[i]
        flag, eq, value = arg.partition("=")
        if flag not in EXTRA_ARGS_ALLOWED:
            what = f"flag {flag!r}" if flag.startswith("-") else f"argument {arg!r} (not a flag)"
            raise BackendConfigError(f"vllm_mlx.extra_args: {what} is not allowed (serve_argv sets host, port, "
                                     f"max-num-seqs, max-tokens and the rest itself); allowed: {allowed}")
        if EXTRA_ARGS_ALLOWED[flag]:
            if not eq:
                if i + 1 >= len(extra):
                    raise BackendConfigError(f"vllm_mlx.extra_args: {flag} needs a value")
                i += 1
                value = extra[i]
            if not value or (value.startswith("-") and not _number(value)):
                raise BackendConfigError(f"vllm_mlx.extra_args: {flag} needs a value, got {value!r}")
        elif eq:
            raise BackendConfigError(f"vllm_mlx.extra_args: {flag} takes no value")
        i += 1
    return list(extra)


def _number(v: str) -> bool:
    try:
        float(v)
    except ValueError:
        return False
    return True


def _expand(p: str) -> str:
    return os.path.expanduser(p)


def load(path: str | None = None, *, explicit: bool = False) -> dict[str, Any]:
    """The validated backend config: {"backend", "upstream", "served_model" (vllm-mlx), "model" (the model entry,
    vllm-mlx), "serve" (vllm-mlx settings), "governor" (local_governor settings, or None), "path"}.
    A missing file at the default path is Ollama with no governor; a missing file the caller named is an error."""
    p = path or os.environ.get(CONFIG_ENV) or None
    explicit = explicit or p is not None
    p = _expand(p or DEFAULT_CONFIG)
    if not os.path.exists(p):
        if explicit:
            raise BackendConfigError(f"backend config {p} not found")
        return {"backend": "ollama", "upstream": None, "served_model": None, "model": None, "serve": None,
                "governor": None, "read_timeout_s": None, "path": None}
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        raise BackendConfigError(f"backend config {p} unreadable: {type(exc).__name__}: {exc}") from None
    return check(raw, p)


def check(raw: Any, path: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BackendConfigError("backend config must be a JSON object")
    backend = raw.get("backend", "ollama")
    if backend not in BACKENDS:
        raise BackendConfigError(f"backend must be one of {', '.join(BACKENDS)}, got {backend!r}")
    out: dict[str, Any] = {"backend": backend, "served_model": None, "model": None, "serve": None, "path": path}
    model = None
    if backend == "vllm-mlx":
        section = raw.get("vllm_mlx")
        if not isinstance(section, dict):
            raise BackendConfigError("backend vllm-mlx needs a `vllm_mlx` object")
        models = section.get("models")
        name = section.get("model")
        if not isinstance(models, dict) or not isinstance(name, str) or name not in models:
            raise BackendConfigError("vllm_mlx.model must name an entry of vllm_mlx.models (the model path is config)")
        model = models[name]
        if not isinstance(model, dict):
            raise BackendConfigError(f"vllm_mlx.models.{name} must be an object")
        for key in ("path", "served_model_name"):
            if not isinstance(model.get(key), str) or not model[key].strip():
                raise BackendConfigError(f"vllm_mlx.models.{name}.{key} must be a non-empty string")
        serve = dict(SERVE_DEFAULTS)
        for key, v in (section.get("serve") or {}).items():
            if key.startswith("_"):
                continue
            if key not in SERVE_DEFAULTS:
                raise BackendConfigError(f"vllm_mlx.serve: unknown key {key!r}")
            serve[key] = v
        if serve["host"] not in ("127.0.0.1", "::1", "localhost"):
            raise BackendConfigError("vllm_mlx.serve.host must be loopback: the gate is the only door to the backend")
        if isinstance(serve["port"], bool) or not isinstance(serve["port"], int) or not 0 < serve["port"] < 65536:
            raise BackendConfigError("vllm_mlx.serve.port must be a TCP port")
        venv = section.get("venv")
        if not isinstance(venv, str) or not venv.strip():
            raise BackendConfigError("vllm_mlx.venv must name the venv holding vllm-mlx")
        extra = check_extra_args(section.get("extra_args", []))
        out.update(served_model=model["served_model_name"].strip(), model={"key": name, **model}, serve=serve,
                   venv=venv, extra_args=extra)
    # Ollama without an `upstream` key: None, so the gate keeps its own --upstream (today's setting). vllm-mlx: the
    # address it is served on, from vllm_mlx.serve, unless `upstream` says otherwise.
    upstream = raw.get("upstream") or (f"http://{_host_for_url(out['serve']['host'])}:{out['serve']['port']}"
                                       if out["serve"] else None)
    if upstream is not None:
        u = urllib.parse.urlparse(upstream)
        if u.scheme != "http" or u.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise BackendConfigError(f"upstream must be http:// on loopback, got {upstream!r}")
    out["upstream"] = upstream
    # The gate's wait for each upstream read (local_gate --read-timeout, 300 s by default). A non-streaming vllm-mlx
    # answer sends nothing until it is whole, and at 8 at once a long one takes minutes, so the example sets it to the
    # server's own timeout_s. Absent: the gate's setting.
    rt = raw.get("read_timeout_s")
    if rt is not None and (isinstance(rt, bool) or not isinstance(rt, (int, float)) or not 0 < rt < 86400):
        raise BackendConfigError(f"read_timeout_s must be a positive number of seconds, got {rt!r}")
    out["read_timeout_s"] = float(rt) if rt is not None else None
    gov = raw.get("governor")
    if gov is not None and not isinstance(gov, dict):
        raise BackendConfigError("governor must be an object or absent")
    if gov is not None and out["serve"] and "default_max_tokens" not in gov:
        # A request naming no max_tokens gets vllm-mlx's own default, so the governor reserves for that.
        gov = {**gov, "default_max_tokens": out["serve"]["max_tokens"]}
    try:
        out["governor"] = local_governor.check_config(gov, model) if gov is not None else None
    except local_governor.GovernorConfigError as exc:
        raise BackendConfigError(str(exc)) from None
    return out


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _normal(name: str) -> str:
    n = (name or "").strip()
    return n if not n or ":" in n.rsplit("/", 1)[-1] else n + ":latest"


TOO_DEEP = "body is nested too deeply"


class Refusal(Exception):
    """A request the gate answers itself: (status, Anthropic error type, message). Never retryable: the same body
    gets the same answer (the gate sends x-should-retry: false)."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind = status, kind


def adapt_messages_body(cfg: dict[str, Any], raw: bytes) -> bytes:
    """POST /v1/messages as the backend gets it (module docstring). Ollama: the bytes unchanged. vllm-mlx: model checked
    and set to the served name, thinking mapped to chat_template_kwargs.enable_thinking. Refusal on a bad request."""
    if cfg["backend"] != "vllm-mlx":
        return raw
    try:
        body = json.loads(raw)
    except RecursionError:
        raise Refusal(400, "invalid_request_error", TOO_DEEP) from None
    except ValueError:
        raise Refusal(400, "invalid_request_error", "body is not JSON") from None
    if not isinstance(body, dict):
        raise Refusal(400, "invalid_request_error", "body must be a JSON object")
    served = cfg["served_model"]
    if not isinstance(body.get("model"), str) or _normal(body["model"]) != _normal(served):
        raise Refusal(404, "not_found_error", "model not served by this node's backend")
    body["model"] = served
    thinking = body.get("thinking")
    kwargs = body.get("chat_template_kwargs")
    if kwargs is not None and not isinstance(kwargs, dict):
        raise Refusal(400, "invalid_request_error", "chat_template_kwargs must be an object")
    kwargs = dict(kwargs or {})
    for key, value in kwargs.items():
        kind = ALLOWED_TEMPLATE_KWARGS.get(key)
        if kind is None:
            shown = key if key.isidentifier() and len(key) <= 40 else "a key"
            raise Refusal(400, "invalid_request_error", f"chat_template_kwargs: {shown} is not allowed (allowed: "
                                                        f"{', '.join(sorted(ALLOWED_TEMPLATE_KWARGS))})")
        if type(value) is not kind:
            raise Refusal(400, "invalid_request_error", f"chat_template_kwargs: {key} must be a {kind.__name__}")
    if isinstance(thinking, dict) and "enable_thinking" not in kwargs:
        kwargs["enable_thinking"] = thinking.get("type") != "disabled"
    if kwargs:
        body["chat_template_kwargs"] = kwargs
    try:
        return json.dumps(body).encode()
    except RecursionError:
        raise Refusal(400, "invalid_request_error", TOO_DEEP) from None


def guard_refusal(status: int, raw: bytes) -> str | None:
    """The backend prompt guard's refusal text (a 413 whose detail starts with GUARD_REFUSAL), or None."""
    if status != 413:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    detail = data.get("detail") if isinstance(data, dict) else None
    return detail[:300] if isinstance(detail, str) and detail.startswith(GUARD_REFUSAL) else None


def models_to_tags(raw: bytes) -> dict[str, Any] | None:
    """vllm-mlx's GET /v1/models ({"data": [{"id": ...}]}) as Ollama's /api/tags ({"models": [{"name", "model"}]}),
    or None when it is not that shape."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    names = [m.get("id") for m in items if isinstance(m, dict)]
    return {"models": [{"name": n, "model": n} for n in names if isinstance(n, str) and n]}


def anthropic_error(status: int, raw: bytes, backend: str) -> bytes:
    """An upstream error body in Anthropic's error shape: unchanged when it already is one, else the backend's
    `detail` (or text) wrapped, at most 300 characters."""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("type") == "error" and isinstance(data.get("error"), dict):
        return raw
    detail = data.get("detail") if isinstance(data, dict) else None
    text = detail if isinstance(detail, str) else json.dumps(detail) if detail is not None else \
        raw[:300].decode("utf-8", "replace")
    kind = {400: "invalid_request_error", 404: "not_found_error", 413: "request_too_large",
            429: "rate_limit_error"}.get(status, "api_error")
    return json.dumps({"type": "error", "error": {"type": kind, "message": f"{backend}: {text[:300]}"}}).encode()


def install_paths(cfg: dict[str, Any]) -> dict[str, str]:
    """Where the backend runs from: the venv's python, the guard's installed copy and its source, the model directory
    (install_mlx_backend.sh reads them through --install-paths)."""
    if cfg["backend"] != "vllm-mlx":
        raise BackendConfigError("install paths need backend vllm-mlx")
    bin_dir = Path(_expand(cfg["venv"])) / "bin"
    return {"python": str(bin_dir / "python"), "guard": str(bin_dir / GUARD_NAME), "guard_source": str(GUARD_SOURCE),
            "model": _expand(cfg["model"]["path"])}


def serve_argv(cfg: dict[str, Any]) -> list[str]:
    """The vllm-mlx command line for the configured model (the LaunchAgent's ProgramArguments): vllm-mlx's own
    `serve` arguments, run through the prompt guard with the governor's context_tokens as the prompt cap."""
    if cfg["backend"] != "vllm-mlx":
        raise BackendConfigError("serve_argv needs backend vllm-mlx")
    s, m = cfg["serve"], cfg["model"]
    gov = cfg["governor"] or local_governor.check_config({}, m)
    # The backend's own bound: never more sequences than the governor could ever admit (the idle cap, bounded by how
    # many empty-context reservations fit the byte budget), so a request that slipped past the gate could not add more.
    seqs = local_governor.max_seqs(gov)
    paths = install_paths(cfg)
    argv = [paths["python"], paths["guard"], "--context-tokens", str(gov["context_tokens"]), "--",
            "serve", paths["model"],
            "--served-model-name", cfg["served_model"], "--host", s["host"], "--port", str(s["port"]),
            "--continuous-batching", "--max-num-seqs", str(seqs),
            "--max-tokens", str(s["max_tokens"]), "--max-request-tokens", str(s["max_request_tokens"]),
            "--timeout", str(s["timeout_s"])]
    if s["tool_call_parser"]:
        argv += ["--enable-auto-tool-choice", "--tool-call-parser", str(s["tool_call_parser"])]
    if s["reasoning_parser"]:
        argv += ["--reasoning-parser", str(s["reasoning_parser"])]
    if s["default_chat_template_kwargs"]:
        argv += ["--default-chat-template-kwargs", json.dumps(s["default_chat_template_kwargs"])]
    # Re-checked here too: a cfg built without check() must not slip an override past the allowlist.
    return argv + check_extra_args(list(cfg.get("extra_args") or []))


def launch_agent_plist(cfg: dict[str, Any], log_dir: str) -> bytes:
    """The LaunchAgent running vllm-mlx on loopback (scripts/install_mlx_backend.sh writes and loads it)."""
    return plistlib.dumps({
        "Label": LAUNCH_LABEL,
        "ProgramArguments": serve_argv(cfg),
        "RunAtLoad": True,
        # Restart after a crash, not after a clean exit (bootout): a model server that dies mid-day comes back.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "StandardOutPath": str(Path(log_dir) / "vllm-mlx.log"),
        "StandardErrorPath": str(Path(log_dir) / "vllm-mlx.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    })


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m cortados_router.local_backend", description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=None, help=f"backend config (default ${CONFIG_ENV} or {DEFAULT_CONFIG})")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--print-config", action="store_true", help="validate and print the resolved config")
    what.add_argument("--argv", action="store_true", help="print the vllm-mlx command line, one argument per line")
    what.add_argument("--plist", metavar="LOG_DIR", help="print the LaunchAgent plist (logs under LOG_DIR)")
    what.add_argument("--install-paths", action="store_true",
                      help="print the venv python, the guard (installed and source) and the model directory as JSON")
    a = ap.parse_args(argv)
    try:
        cfg = load(a.config, explicit=a.config is not None)
        if a.print_config:
            print(json.dumps(cfg, indent=1, default=str))
        elif a.argv:
            print("\n".join(serve_argv(cfg)))
        elif a.install_paths:
            print(json.dumps(install_paths(cfg)))
        else:
            sys.stdout.buffer.write(launch_agent_plist(cfg, a.plist))
    except BackendConfigError as exc:
        print(f"local_backend: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
