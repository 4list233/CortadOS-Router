# PURPOSE: Expose the seven worker tools through the maintained official MCP SDK.
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
    if name == 'job_feedback':
        check = object_schema({'kind': {'enum': ['command', 'artifact', 'review']},
            'reference': nonempty, 'claim': {'enum': ['passed', 'failed', 'unknown']}}, ('kind', 'reference', 'claim'))
        return object_schema({'job_id': identity, 'feedback_id': identity,
            'verdict': {'enum': ['accepted', 'rejected', 'inconclusive']},
            'summary': nonempty, 'checks': {'type': 'array', 'items': check}},
            ('job_id', 'feedback_id', 'verdict', 'summary', 'checks'))
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
    evidence = {'schema': {'const': 'cortados.mcp-task-evidence.v1'}, 'task_group_id': identity}
    evidence.update({field: names for field in ('components', 'dependencies', 'constraints', 'uncertainties', 'verification')})
    evidence['references'] = {'type': 'array', 'items': object_schema({'source': nonempty, 'text': nonempty}, ('source', 'text'))}
    properties['task_evidence'] = object_schema(evidence, ('schema', 'task_group_id'))
    if name == 'context_requirements':
        properties = {key: properties[key] for key in ('task', 'context', 'selection', 'task_evidence')}
    required = ['task', 'selection']
    if name == 'delegate':
        properties.update(cwd=nonempty, request_id=identity, timeout_s={'type': 'integer', 'minimum': 1})
        required.extend(('cwd', 'request_id', 'timeout_s'))
    return object_schema(properties, required)


async def serve(config, digest):
    server = Server('cortados-mcp-worker', version='1.0.0')
    descriptions = {
        'route': 'Advise a native Codex route after quota and permission admission; does not execute. First use context_requirements to fill task_evidence and keep the same task_group_id across retries and revisions.',
        'delegate': 'Accept one native Codex attempt. First call context_requirements, answer its context questions, and pass complete task_evidence with the same task_group_id for this assignment. Review the terminal result and record job_feedback.',
        'job_status': 'Read the current durable job snapshot.',
        'job_result': 'Read durable terminal evidence when available. Review the actual result, artifacts and checks, then use job_feedback; native completion alone is not correctness.',
        'context_requirements': 'Call before routing or delegation with task, context, selection and available task_evidence. Answer returned questions in your next call; reuse task_group_id across retries/revisions. Reports structural gaps and budgets without inference or admission.',
        'job_feedback': 'After reviewing a terminal job result, append your accepted/rejected/inconclusive verdict with checks and evidence references. Use a new feedback_id for corrections. These are fallible planner assertions, not independent verification.',
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
                if name == 'context_requirements':
                    from cortados_router.task_evidence import context_requirements
                    payload = context_requirements(normalized, config)
                else:
                    payload = await asyncio.to_thread(rpc, config, digest, name, normalized)
            except WorkerError as exc:
                emit_event(config, 'refusal', request_id=normalized.get('request_id'),
                           job_id=normalized.get('job_id'), reason=exc.code)
                payload = exc.payload()
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload, ensure_ascii=False))],
                              structuredContent=payload, isError=not payload['ok'])

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
