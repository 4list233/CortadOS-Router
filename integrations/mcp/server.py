# PURPOSE: Expose the five worker tools through the maintained official MCP SDK.
# CONTRACT: RT-MCP-001 strict inputs, typed errors, durable broker-owned execution.
# DEPENDS: mcp==1.30.0, schemas validation, private broker RPC.
import asyncio
import json

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .broker import rpc
from .schemas import TOOLS, WorkerError, emit_event, validate


def object_schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


def tool_schema(name):
    text = {'type': 'string'}
    nonempty = {'type': 'string', 'minLength': 1}
    names = {'type': 'array', 'items': nonempty, 'uniqueItems': True}
    identity = {'type': 'string', 'format': 'uuid'}
    if name.startswith('job_'):
        return object_schema({'job_id': identity}, ('job_id',))
    grant = object_schema({'name': nonempty, 'tools': dict(names, minItems=1)}, ('name', 'tools'))
    properties = {
        'task': nonempty, 'context': text,
        'selection': {'oneOf': [
            object_schema({'mode': {'const': 'explicit'}, 'model': nonempty}, ('mode', 'model')),
            object_schema({'mode': {'const': 's1'}, 'routing_brief': text}, ('mode',))]},
        'constraints': object_schema({'allowed_models': names,
                                     'min_band': {'enum': ['R0', 'R1', 'R2', 'R3', 'R4']}}),
        'permissions': object_schema({'network': {'type': 'boolean'},
                                     'mcp_servers': {'type': 'array', 'items': grant}}),
        'account_ref': {'const': 'native-default'}}
    required = ['task', 'selection']
    if name == 'delegate':
        properties.update(cwd=nonempty, request_id=identity, timeout_s={'type': 'integer', 'minimum': 1})
        required.extend(('cwd', 'request_id', 'timeout_s'))
    return object_schema(properties, required)


async def serve(config, digest):
    server = Server('cortados-mcp-worker', version='1.0.0')
    descriptions = {
        'route': 'Advise a native Codex route after quota and permission admission; does not execute.',
        'delegate': 'Accept one native Codex attempt. Claude remains its planner and reviewer.',
        'job_status': 'Read the current durable job snapshot.',
        'job_result': 'Read the durable snapshot and accepted terminal evidence, when available.',
        'job_cancel': 'Record cancellation intent; completion waits for observed process termination.'}

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=descriptions[name], inputSchema=tool_schema(name)) for name in TOOLS]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            normalized = validate(name, arguments, config)
        except WorkerError as exc:
            candidate = arguments if isinstance(arguments, dict) else {}
            emit_event(config, 'refusal', request_id=candidate.get('request_id'),
                       job_id=candidate.get('job_id'), reason=exc.code)
            payload = exc.payload()
        else:
            try:
                payload = await asyncio.to_thread(rpc, config, digest, name, normalized)
            except WorkerError as exc:
                emit_event(config, 'refusal', request_id=normalized.get('request_id'),
                           job_id=normalized.get('job_id'), reason=exc.code)
                payload = exc.payload()
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload, ensure_ascii=False))],
                              structuredContent=payload, isError=not payload['ok'])

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
