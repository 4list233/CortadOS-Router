# PURPOSE: Compose listener-free synthetic MCP HTTP and exact raw transfer routes.
# CONTRACT: RT-COWORK-MCP-008 v5 R052,055,075–076,086–087.
# DEPENDS: Existing MCP SDK session manager and already-bound CoreClient.
# CEILING: No OAuth, listener, stdio startup, identity headers or task lifetime ownership.
from contextlib import asynccontextmanager
import asyncio
import time
import json
import re
from urllib.parse import quote

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool

from . import remote_schemas as s

INSTRUCTIONS = ('Keep planning and final review in the calling Claude session. Call context_requirements before route or delegate and answer its questions using relevant task evidence. Reuse one task_group_id across assignment revisions. Create a task with task_create; use its task_id instead of Studio paths. Upload only selected inputs with file_upload_ticket and include sealed artifact IDs when delegating. Use S1 or an explicit configured model subject to fresh quota admission. Retain the returned job_id; after disconnect inspect that job instead of launching another attempt. Inspect job_result and downloaded artifacts, verify their hashes, then record honest job_feedback. Workers do not inherit your conversation, files, connectors or skills. Request missing context explicitly. Delete task payloads only when requested.')
DESCRIPTIONS = {
    'task_create': 'Create an idempotent task workspace with immutable group and effective limits.',
    'task_get': 'Read the owned task and current artifact descriptors, including pending uploads.',
    'task_delete': 'Explicitly delete idle task payloads and preserve its history and tombstone.',
    'context_requirements': 'Call before route or delegate; answer structural questions and reuse task_group_id.',
    'route': 'Recommend an admitted route for sealed selected inputs after context_requirements; does not start work.',
    'delegate': 'After context_requirements, accept one task-owned attempt with complete evidence and sealed input IDs.',
    'job_status': 'Inspect the owned accepted job after reconnect before attempting another delegation.',
    'job_cancel': 'Record idempotent stop intent; completion requires proven termination.',
    'job_result': 'Inspect terminal native outcome and immutable output descriptors; review hashes before job_feedback.',
    'job_feedback': 'Record an honest planner verdict after reviewing the result and downloaded artifacts.',
    'file_upload_ticket': 'Issue or renew an exact binary PUT capability; send raw bytes with returned Authorization header.',
    'file_download_ticket': 'Issue a full immutable binary GET capability; use returned Authorization header and verify hash.'}
STATUS = {'invalid_request': 400, 'length_mismatch': 400, 'hash_mismatch': 400,
          'ticket_invalid': 401, 'ticket_expired': 401, 'permission_refused': 403,
          'artifact_not_found': 404, 'transfer_busy': 409, 'ticket_consumed': 409,
          'task_deleted': 409, 'task_quarantined': 409, 'upload_incomplete': 409,
          'output_limit': 413, 'capacity_exhausted': 507, 'storage_error': 503,
          'artifact_corrupt': 503, 'policy_unavailable': 503, 'timeout': 408}


async def _json(send, status, payload, headers=()):
    body = s.json_bytes(payload)
    await send({'type': 'http.response.start', 'status': status, 'headers': [
        (b'content-type', b'application/json'), (b'cache-control', b'no-store'),
        (b'content-length', str(len(body)).encode()), *headers]})
    await send({'type': 'http.response.body', 'body': body})


def create_test_app(*, core_client, principal, config):
    if core_client.closed or not core_client.bound:
        raise s.RemoteError('storage_error', 'Private core connection unavailable')
    if core_client.adopted or principal != core_client.principal or config.config_digest != core_client.config_digest:
        raise s.RemoteError('permission_refused')
    core_client.adopted = True
    server = Server('cortados-studio-workers', version='1.0.0', instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=DESCRIPTIONS[name], inputSchema=s.remote_tool_schema(name)) for name in s.TOOLS]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        result = await core_client.call(name, arguments)
        return CallToolResult(content=[TextContent(type='text', text=s.json_bytes(result).decode())],
                              structuredContent=result, isError=not result['ok'])

    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

    class App:
        running = False

        async def __call__(self, scope, receive, send):
            if scope['type'] == 'lifespan':
                await receive()
                try:
                    async with manager.run():
                        self.running = True
                        await send({'type': 'lifespan.startup.complete'})
                        await receive()
                        self.running = False
                    await core_client.close()
                    await send({'type': 'lifespan.shutdown.complete'})
                except BaseException:
                    self.running = False
                    await core_client.close()
                    raise
                return
            if scope['type'] != 'http':
                raise s.RemoteError('invalid_request')
            failure = s.RemoteError('invalid_request').payload()
            if not self.running:
                await _json(send, 503, s.RemoteError('storage_error').payload())
                return
            path = scope.get('raw_path', scope['path'].encode()).decode('ascii', errors='replace')
            if scope.get('query_string'):
                await _json(send, 400, failure)
                return
            match = re.fullmatch(r'/transfers/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})', path)
            if path != '/mcp' and match is None:
                await _json(send, 404, failure)
                return
            if path == '/mcp':
                if scope['method'] != 'POST':
                    await _json(send, 405, failure, [(b'allow', b'POST')])
                    return
                body = bytearray()
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    if len(body) + len(chunk) > config.limits.control_bytes:
                        await _json(send, 413, s.RemoteError('output_limit').payload())
                        return
                    body.extend(chunk)
                    if not message.get('more_body'):
                        break
                delivered = False
                async def buffered():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                    return await receive()
                await manager.handle_request(scope, buffered, send)
                return
            if scope['method'] not in ('GET', 'PUT'):
                await _json(send, 405, failure, [(b'allow', b'GET, PUT')])
                return
            await self.transfer(scope, receive, send, match[1])

        async def transfer(self, scope, receive, send, ticket_id):
            entered = time.monotonic()
            headers = {}
            duplicate = False
            for key, value in scope['headers']:
                key = key.lower()
                duplicate = duplicate or key in headers
                headers[key] = value.decode('latin-1')
            lease_id = None
            state = 'not_attempted'
            complete = False
            total = entered + config.limits.transfer_total_ms / 1000
            idle = entered + config.limits.transfer_idle_ms / 1000
            async def bounded(call, *args):
                deadline = min(total, idle)
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                async with asyncio.timeout_at(deadline):
                    result = await call(*args)
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                return result
            async def emit(message):
                nonlocal state
                if message['type'] == 'http.response.start':
                    if state != 'not_attempted':
                        raise s.RemoteError('storage_error')
                    state = 'headers_attempted'
                await bounded(send, message)
                if message['type'] == 'http.response.start':
                    state = 'headers_complete'
                elif not message.get('more_body', False):
                    state = 'finished'
            def progress():
                nonlocal idle
                idle = time.monotonic() + config.limits.transfer_idle_ms / 1000
            try:
                authorization = headers.get(b'authorization', '')
                if not authorization.startswith('Bearer '):
                    raise s.RemoteError('ticket_invalid')
                args = {'ticket_id': ticket_id, 'capability': authorization[7:]}
                put = scope['method'] == 'PUT'
                if put:
                    length = headers.get(b'content-length', '')
                    if (duplicate or not re.fullmatch(r'0|[1-9][0-9]*', length)
                            or headers.get(b'content-type') != 'application/octet-stream'
                            or any(key in headers for key in (b'transfer-encoding', b'content-encoding', b'range'))):
                        raise s.RemoteError('invalid_request')
                    args['content_length'] = int(length)
                opened = (await bounded(core_client.stream, 'open_put' if put else 'open_get', args))['result']
                if not opened['ok']:
                    raise s.RemoteError(**opened['error'])
                lease_id = opened['lease_id']
                if put:
                    offset = 0
                    while True:
                        message = await bounded(receive)
                        if message['type'] == 'http.disconnect':
                            return
                        body = memoryview(message.get('body', b''))
                        for index in range(0, len(body), config.limits.chunk_bytes):
                            result = (await bounded(core_client.stream, 'put_chunk', {'lease_id': lease_id, 'offset': offset}, bytes(body[index:index + config.limits.chunk_bytes])))['result']
                            if not result['ok']:
                                raise s.RemoteError(**result['error'])
                            offset = result['offset']
                            progress()
                        if not message.get('more_body'):
                            break
                    result = (await bounded(core_client.stream, 'finish_put', {'lease_id': lease_id}))['result']
                    if not result['ok']:
                        raise s.RemoteError(**result['error'])
                    complete = True
                    await bounded(_json, emit, 201, result)
                else:
                    if b'range' in headers:
                        complete = True
                        await bounded(_json, emit, 416, s.RemoteError('invalid_request').payload())
                        return
                    artifact = opened['artifact']
                    disposition = 'attachment; filename="artifact-' + artifact['artifact_id'] + '"; filename*=UTF-8\'\'' + quote(artifact['filename'], safe='')
                    await emit({'type': 'http.response.start', 'status': 200, 'headers': [
                        (b'content-type', b'application/octet-stream'), (b'content-length', str(artifact['bytes']).encode()),
                        (b'etag', ('"' + artifact['sha256'] + '"').encode()), (b'x-content-type-options', b'nosniff'),
                        (b'cache-control', b'no-store'), (b'content-disposition', disposition.encode('ascii'))]})
                    offset = 0
                    while True:
                        frame = await bounded(core_client.stream, 'read_chunk', {'lease_id': lease_id, 'offset': offset, 'max_bytes': config.limits.chunk_bytes})
                        result = frame['result']
                        if not result['ok']:
                            raise s.RemoteError(**result['error'])
                        offset = result['offset']
                        await emit({'type': 'http.response.body', 'body': frame['body'], 'more_body': not result['eof']})
                        if frame['body']:
                            progress()
                        if result['eof']:
                            complete = True
                            break
            except (TimeoutError, s.RemoteError) as exc:
                if state != 'not_attempted' or time.monotonic() >= min(total, idle):
                    raise s.RemoteError('timeout') from exc
                error = s.RemoteError('timeout') if isinstance(exc, TimeoutError) else exc
                await bounded(_json, emit, STATUS.get(error.code, 503), error.payload())
            finally:
                if time.monotonic() >= min(total, idle):
                    await core_client.close()
                if lease_id is not None and not core_client.closed:
                    try:
                        if time.monotonic() >= min(total, idle):
                            raise TimeoutError()
                        await bounded(core_client.stream, 'close',
                                      {'lease_id': lease_id, 'reason': 'complete' if complete else 'abort'})
                    except TimeoutError:
                        await core_client.close()

    return App()
