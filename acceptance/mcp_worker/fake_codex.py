#!/usr/bin/env python3
# PURPOSE: Deterministic native-process double replaying independently captured Codex shapes.
# CONTRACT: RT-MCP-001 acceptance only; no provider access, real credentials, or production imports.
# DEPENDS: stdlib and protected fixture bytes; control comes only from a private synthetic CODEX_HOME.
# CEILING: This double proves orchestration/fault containment, never native sandbox or provider quality.
import asyncio
import json
import tomllib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

FIXTURES = Path(__file__).parent / "fixtures"
HOME_DIR = Path(os.environ["CODEX_HOME"])
CONTROL = HOME_DIR / "fixture-control.json"
CALLS = HOME_DIR / "fixture-calls.jsonl"


def control():
    return json.loads(CONTROL.read_text()) if CONTROL.exists() else {}


def record(kind, **fields):
    row = {"kind": kind, "pid": os.getpid(), "ppid": os.getppid(),
           "pgid": os.getpgrp(), "sid": os.getsid(0), **fields}
    with CALLS.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def metadata():
    records = [json.loads(line) for line in (FIXTURES / "native-metadata-wire.jsonl").read_text().splitlines()]
    by_method = dict(zip(("initialize", "account/read", "account/rateLimits/read", "model/list"), records))
    policy = FIXTURES / "native-boundaries" / "policy-wire-redacted"
    by_method["config/read"] = json.loads((policy / "config-read.redacted.jsonl").read_text())
    by_method["configRequirements/read"] = json.loads((policy / "requirements-read.redacted.jsonl").read_text())
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        record("metadata", method=method, params=request.get("params"))
        config = control()
        if method not in by_method:
            send({"id": request["id"], "error": {"code": -32601, "message": "Fixture has no such method"}})
            continue
        result = json.loads(json.dumps(by_method[method]["result"]))
        if config.get("metadata_hang"):
            time.sleep(120)  # Test-only bound; admission must stop this within its configured startup deadline.
        if method == "config/read" and config.get("policy"):
            mode = config["policy"]
            result["layers"].append({"name": {"type": mode}, "version": "synthetic-policy-version", "config": {"fixture": True}})
        if method == "configRequirements/read" and config.get("requirements"):
            result["requirements"] = {"allowedApprovalPolicies": ["on-request"]}
        if method == "account/read":
            if config.get("auth") == "api":
                result["account"] = {"type": "apiKey"}
            elif config.get("auth") == "missing":
                result["account"] = None
        if method == "account/rateLimits/read":
            # Synthetic current times keep a shape fixture useful after its original reset date.
            for bucket in [result.get("rateLimits"), *(result.get("rateLimitsByLimitId") or {}).values()]:
                if bucket:
                    for key in ("primary", "secondary"):
                        if bucket.get(key):
                            bucket[key]["resetsAt"] = int(time.time()) + bucket[key]["windowDurationMins"] * 30
            quota = config.get("quota", "fresh")
            if quota == "unknown":
                send({"id": request["id"], "error": {"code": -32000, "message": "Quota temporarily unavailable"}})
                continue
            if quota == "spent":
                result["ordinaryUsageAllowed"] = False
                result["rateLimits"]["primary"]["usedPercent"] = 100
                result["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 100
            if quota == "missing_permission":
                result.pop("ordinaryUsageAllowed", None)
            if quota == "malformed":
                result["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = "not-a-percent"
        if method == "model/list" and "models" in config:
            result["data"] = [row for row in result["data"] if row["id"] in config["models"]]
        if method == "model/list" and config.get("pagination"):
            cursor = (request.get("params") or {}).get("cursor")
            if config["pagination"] == "loop":
                result["nextCursor"] = "same-cursor"
            elif cursor is None:
                result["data"] = [row for row in result["data"] if row["id"] != "gpt-6-luna"]
                result["nextCursor"] = "second-page"
            else:
                result["data"] = [row for row in result["data"] if row["id"] == "gpt-6-luna"]
                result["nextCursor"] = None
        send({"id": request["id"], "result": result})


def argument(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def native_config():
    merged = {}
    def update(target, source):
        for key, value in source.items():
            if isinstance(value, dict):
                update(target.setdefault(key, {}), value)
            else:
                target[key] = value
    for index, item in enumerate(sys.argv[:-1]):
        if item in ("-c", "--config"):
            update(merged, tomllib.loads(sys.argv[index + 1]))
    return merged


async def probe_proxy():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    observed = []
    for name, definition in native_config().get("mcp_servers", {}).items():
        if definition.get("enabled") is False:
            continue
        row = {"name": name}
        params = StdioServerParameters(command=definition["command"], args=definition.get("args", []),
            env={**os.environ, **definition.get("env", {})})
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                row["tools"] = [item.name for item in (await session.list_tools()).tools]
                for label, tool in (("visible_call", "visible_marker"), ("hidden_call", "hidden_marker")):
                    try:
                        value = await session.call_tool(tool, {})
                        row[label] = {"is_error": value.isError, "content": [item.model_dump() for item in value.content]}
                    except Exception as exc:
                        row[label] = {"error": str(exc), "is_error": True}
                for label, call, attr, field in (
                    ("resources", session.list_resources, "resources", "uri"),
                    ("templates", session.list_resource_templates, "resourceTemplates", "uriTemplate"),
                    ("prompts", session.list_prompts, "prompts", "name")):
                    try:
                        value = await call()
                        row[label] = [str(getattr(item, field)) for item in getattr(value, attr)]
                    except Exception as exc:
                        row[label], row[label + "_error"] = [], str(exc)
                for label, call in (("resource_read", lambda: session.read_resource("fixture://secret")),
                                    ("prompt_get", lambda: session.get_prompt("hidden_prompt", {}))):
                    try:
                        row[label] = (await call()).model_dump(mode="json")
                    except Exception as exc:
                        row[label] = {"error": str(exc)}
        observed.append(row)
    record("proxy_probe", servers=observed)


def execute():
    config = control()
    if argument("-C") or argument("--cd"):
        os.chdir(argument("-C") or argument("--cd"))
    prompt = sys.stdin.read()
    record("exec", argv=sys.argv[1:], cwd=str(Path.cwd()), prompt=prompt,
           env_names=sorted(os.environ),
           marker_environment={key: os.environ[key] for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MCP_FIXTURE_SECRET", "MCP_UNDECLARED_SECRET", "CODEX_HOME") if key in os.environ})
    if config.get("mcp_probe"):
        asyncio.run(probe_proxy())
    # This file is the externally visible effect used to count duplicate native execution.
    with (Path.cwd() / "worker-effect.txt").open("a") as stream:
        stream.write("one accepted native attempt\n")
    if config.get("descendant"):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)"
        # A separate PGID reproduces the real Codex tool child missed by killpg(root).
        child_args = [sys.executable, "-c", code, str(HOME_DIR)]
        child = (subprocess.Popen(child_args, start_new_session=True)
                 if config.get("descendant") == "session"
                 else subprocess.Popen(child_args, process_group=0))
        record("descendant", child_pid=child.pid)
    if config.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if config.get("hang"):
        # Test-only 120 s ceiling prevents an abandoned double living forever; normal tests cancel within seconds.
        end = time.monotonic() + 120
        while not (HOME_DIR / "release-worker").exists() and time.monotonic() < end:
            time.sleep(0.02)
    model = argument("-m") or argument("--model") or "gpt-6-luna"
    label = {"gpt-6-luna": "luna", "gpt-6.1-sol": "sol", "gpt-6-astra": "astra"}.get(model, "luna")
    events = [json.loads(line) for line in (FIXTURES / f"codex-{label}-command.jsonl").read_text().splitlines()]
    behavior = config.get("behavior", "success")
    if behavior == "malformed":
        print("{broken json", flush=True)
        return 0
    if behavior == "no_terminal":
        events = [row for row in events if row["type"] != "turn.completed"]
    if behavior == "duplicate_terminal":
        events.append(events[-1])
    if behavior == "unknown_event":
        events.insert(-1, {"type": "future.inert_event", "payload": {"keep_exactly": "native evidence"}})
    if behavior == "missing_usage":
        events[-1].pop("usage", None)
    if behavior == "partial_usage":
        events[-1]["usage"] = {"input_tokens": 10}
    if behavior == "bad_usage":
        events[-1]["usage"]["input_tokens"] = -1
    if behavior == "excess_output":
        print("x" * config.get("output_bytes", 2_000_000), flush=True)
    for row in events:
        send(row)
    final = argument("-o") or argument("--output-last-message")
    if final and behavior != "no_final":
        Path(final).write_text("DIFFERENT" if behavior == "mismatched_final" else "MCP_WORKER_CAPTURE_OK")
    return 7 if behavior == "nonzero" else 0


if "--version" in sys.argv:
    print("codex-cli " + control().get("version", "0.162.1"))
elif "app-server" in sys.argv:
    metadata()
elif "exec" in sys.argv:
    raise SystemExit(execute())
elif sys.argv[1:3] == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    record("unexpected", argv=sys.argv[1:])
    raise SystemExit(64)
