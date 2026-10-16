# PURPOSE: Expose only frozen per-job tool grants through the official MCP SDK.
# CONTRACT: RT-MCP-001 section 5; credentials remain on a private channel, never native argv/env.
# DEPENDS: mcp==1.30.0, stdlib, schemas; both upstream transports use the official SDK.
import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading

# Absolute-file entry works both in a source checkout and in the installed wheel without PYTHONPATH.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from integrations.mcp.codex import ENVIRONMENT_NAMES
from integrations.mcp.schemas import WorkerError


def _payload(config, grant):
    definition = config["connector_registry"][grant["name"]]
    references = (definition["env_vars"] if definition["transport"] == "stdio"
                  else [definition["bearer_token_env_var"]] if definition["bearer_token_env_var"] else [])
    if any(name not in os.environ for name in references):
        raise WorkerError("connector_unavailable", "A selected connector environment reference is unavailable")
    return {"definition": definition, "tools": grant["tools"],
            "credentials": {name: os.environ[name] for name in references},
            "startup_timeout_s": config["limits"]["startup_timeout_s"]}


@asynccontextmanager
async def upstream(payload):
    """Own one SDK connection; no raw endpoint or credential is returned to native Codex."""
    definition = payload["definition"]
    async with AsyncExitStack() as stack:
        if definition["transport"] == "stdio":
            environment = {key: os.environ[key] for key in ENVIRONMENT_NAMES if key in os.environ}
            environment.update(payload["credentials"])
            parameters = StdioServerParameters(command=definition["command"], args=definition["args"], env=environment)
            # Upstream diagnostics can include credentials; never put them in persisted job evidence.
            errors = stack.enter_context(open(os.devnull, "w"))
            streams = await stack.enter_async_context(stdio_client(parameters, errlog=errors))
        else:
            reference = definition["bearer_token_env_var"]
            headers = {"Authorization": "Bearer " + payload["credentials"][reference]} if reference else {}
            streams = await stack.enter_async_context(streamablehttp_client(
                definition["url"], headers=headers, timeout=payload["startup_timeout_s"]))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        yield session


async def _catalog(session, granted):
    found, cursor, seen = {}, None, set()
    while True:
        page = await session.list_tools(cursor=cursor)
        for tool in page.tools:
            if tool.name in found:
                raise WorkerError("connector_unavailable", "Connector returned duplicate tool identities")
            found[tool.name] = tool
        cursor = page.nextCursor
        if cursor is None:
            break
        if cursor in seen:
            raise WorkerError("connector_unavailable", "Connector returned repeated discovery cursor")
        seen.add(cursor)
    if any(name not in found for name in granted):
        raise WorkerError("connector_unavailable", "Connector does not expose every granted tool")
    return [found[name] for name in granted]


async def _preflight(config, permissions):
    async with asyncio.timeout(config["limits"]["startup_timeout_s"]):
        for grant in permissions["mcp_servers"]:
            payload = _payload(config, grant)
            async with upstream(payload) as session:
                await _catalog(session, payload["tools"])


def preflight(config, permissions):
    """Synchronous admission seam; only discover configured, policy-admitted grants."""
    if not permissions["mcp_servers"]:
        return
    try:
        asyncio.run(_preflight(config, permissions))
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("connector_unavailable", "Selected connector startup or discovery failed") from exc


class GrantPayloadServer:
    """Private one-use per-grant bootstrap and failure channel held by the watchdog."""
    def __init__(self, config, permissions):
        self.payloads = {grant["name"]: _payload(config, grant) for grant in permissions["mcp_servers"]}
        self.timeout = config["limits"]["startup_timeout_s"]
        self.failure = False
        self.closed = threading.Event()
        self.connections = []
        self.threads = []
        self.lock = threading.Lock()
        # sockaddr_un has a short native path bound; private /tmp allocation avoids long state paths.
        self.directory = Path(tempfile.mkdtemp(prefix="cmw-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp"))
        self.path = self.directory / "grant.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.listener.listen()
        self.listener.settimeout(self.timeout)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.closed.is_set():
                    self.failure = True
                return
            self.connections.append(connection)
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _serve(self, connection):
        try:
            connection.settimeout(self.timeout)
            stream = connection.makefile("rwb")
            with stream:
                request = json.loads(stream.readline())
                with self.lock:
                    payload = self.payloads.pop(request.get("server"), None)
                if payload is None:
                    raise ValueError("Proxy grant channel is unavailable")
                stream.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
                stream.flush()
                connection.settimeout(None)
                for line in stream:
                    if json.loads(line).get("event") == "failed":
                        self.failure = True
        except (OSError, ValueError):
            if not self.closed.is_set():
                self.failure = True
        finally:
            connection.close()

    def close(self):
        self.closed.set()
        self.listener.close()
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                continue
        self.path.unlink(missing_ok=True)
        self.directory.rmdir()
        self.payloads.clear()


def _refusal():
    return McpError(types.ErrorData(code=-32001, message="permission_refused: operation is outside this tool grant"))


def _server(session, catalog):
    server = Server("cortados-job-tool-grant")
    allowed = {tool.name for tool in catalog}

    @server.list_tools()
    async def list_tools():
        return catalog

    @server.call_tool()
    async def call_tool(name, arguments):
        if name not in allowed:
            payload = {"ok": False, "error": {"code": "permission_refused", "message": "Tool is outside this job grant"}}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))],
                                        structuredContent=payload, isError=True)
        return await session.call_tool(name, arguments)

    @server.list_resources()
    async def list_resources():
        return []

    @server.list_resource_templates()
    async def list_templates():
        return []

    @server.list_prompts()
    async def list_prompts():
        return []

    @server.read_resource()
    async def read_resource(uri):
        raise _refusal()

    @server.get_prompt()
    async def get_prompt(name, arguments):
        raise _refusal()

    return server


async def _run(payload):
    async with upstream(payload) as session:
        catalog = await _catalog(session, payload["tools"])
        server = _server(session, catalog)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(args.socket)
        with connection.makefile("rwb") as channel:
            channel.write(json.dumps({"server": args.server}).encode() + b"\n")
            channel.flush()
            payload = json.loads(channel.readline())
            try:
                asyncio.run(_run(payload))
            except Exception:
                channel.write(b'{"event":"failed"}\n')
                channel.flush()
                # Do not print exception text: upstream errors can contain secret headers/values.
                print("connector_unavailable: selected connector transport failed", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
