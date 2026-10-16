#!/usr/bin/env python3
# PURPOSE: Observable upstream MCP effects for the independent permission exam.
# CONTRACT: RT-MCP-001 AC-MCP-07; never imports the production grant proxy.
# DEPENDS: Official MCP SDK, stdlib, and a private test-owned event file.
# CEILING: Synthetic credentials and local marker operations only; no real connector access.
import json
import os
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP

EVENTS = Path(sys.argv[1])


def record(kind):
    row = {"kind": kind, "pid": os.getpid()}
    if kind == "started":
        row["marker_environment"] = {key: os.environ[key] for key in
            ("MCP_FIXTURE_SECRET", "MCP_UNDECLARED_SECRET", "OPENAI_API_KEY") if key in os.environ}
    with EVENTS.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


record("started")
if "--fail-start" in sys.argv:
    raise SystemExit(64)

server = FastMCP("permission-fixture", stateless_http=True)


@server.tool()
def visible_marker() -> str:
    """Return the granted marker without disclosing the fixture credential."""
    record("visible_tool")
    return "GRANTED_TOOL_REACHED"


@server.tool()
def hidden_marker() -> str:
    """Record an ungranted tool call so the exam detects accidental forwarding."""
    record("hidden_tool")
    return "FORBIDDEN_TOOL_REACHED"


@server.resource("fixture://secret")
def hidden_resource() -> str:
    record("hidden_resource")
    return "FORBIDDEN_RESOURCE_REACHED"


@server.resource("fixture://templates/{name}")
def hidden_template(name: str) -> str:
    record("hidden_template")
    return "FORBIDDEN_TEMPLATE_REACHED"


@server.prompt()
def hidden_prompt() -> str:
    record("hidden_prompt")
    return "FORBIDDEN_PROMPT_REACHED"


def run_http():
    import socket
    import uvicorn

    class AuthenticatedApp:
        def __init__(self):
            self.application = server.streamable_http_app()

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                supplied = dict(scope["headers"]).get(b"authorization")
                expected = ("Bearer " + os.environ["MCP_FIXTURE_SECRET"]).encode()
                if supplied != expected:
                    record("http_rejected")
                    await send({"type": "http.response.start", "status": 401, "headers": []})
                    await send({"type": "http.response.body", "body": b"Fixture bearer required"})
                    return
                record("http_authorized")
            await self.application(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    ready = Path(sys.argv[sys.argv.index("--http") + 1])

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/mcp"}))

    # Port zero lets the OS allocate an unused loopback port without a reserve/rebind race.
    ReadyServer(uvicorn.Config(AuthenticatedApp(), log_level="error")).run(sockets=[listener])


if "--http" in sys.argv:
    run_http()
else:
    server.run(transport="stdio")
