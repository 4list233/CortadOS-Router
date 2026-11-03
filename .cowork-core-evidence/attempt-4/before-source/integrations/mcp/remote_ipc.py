# PURPOSE: Own the sole bounded socketpair frame codec and private client protocol.
# CONTRACT: RT-COWORK-MCP-008 v5 R013,043–047 and section 4 operation table.
# DEPENDS: Trusted CoreBroker, raw sockets, remote records and transfer descriptor owner.
# CEILING: No listener, socket paths, frontend database, resend or implicit job cancellation.
import asyncio
import socket
import struct
import time

from . import remote_schemas as s
from . import task_store as store
from . import transfer_tickets as tickets
from . import file_transfer as transfer

STREAM_FIELDS = {'open_put': ('ticket_id', 'capability', 'content_length'),
                 'put_chunk': ('lease_id', 'offset'), 'finish_put': ('lease_id',),
                 'open_get': ('ticket_id', 'capability'),
                 'read_chunk': ('lease_id', 'offset', 'max_bytes'), 'close': ('lease_id', 'reason')}
CONNECTION_MESSAGE = 'Private core connection unavailable'


async def _exact(sock, size):
    result = bytearray()
    loop = asyncio.get_running_loop()
    while len(result) < size:
        part = await loop.sock_recv(sock, size - len(result))
        if not part:
            raise EOFError()
        result.extend(part)
    return bytes(result)


async def _read(sock, limits):
    length = struct.unpack('!I', await _exact(sock, 4))[0]
    if not 0 < length <= limits.control_bytes:
        raise EOFError()
    header = s.strict_json(await _exact(sock, length))
    if not isinstance(header, dict) or type(header.get('body_bytes')) is not int or not 0 <= header['body_bytes'] <= limits.chunk_bytes:
        raise EOFError()
    return header, await _exact(sock, header['body_bytes'])


def _encode(header, body, limits):
    data = s.json_bytes(header)
    if not 0 < len(data) <= limits.control_bytes or len(body) > limits.chunk_bytes:
        raise s.RemoteError('output_limit', 'Control response exceeds configured bound')
    return struct.pack('!I', len(data)) + data + body


async def _write(sock, header, body, limits, *, deadline=None, encoded=None):
    frame = _encode(header, body, limits) if encoded is None else encoded
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError()
    await asyncio.get_running_loop().sock_sendall(sock, frame)


class CoreClient:
    def __init__(self, socket, *, principal, config_digest, limits):
        self.socket = socket
        self.socket.setblocking(False)
        self.principal = principal
        self.config_digest = config_digest
        self.limits = limits
        self.lock = asyncio.Lock()
        self.seq = 0
        self.bound = False
        self.closed = False
        self.adopted = False

    async def _request(self, op, args, body=b''):
        async with self.lock:
            if self.closed or self.seq > 9007199254740991:
                raise s.RemoteError('storage_error', CONNECTION_MESSAGE)
            try:
                async with asyncio.timeout(self.limits.ipc_idle_ms / 1000):
                    header = dict(v=1, seq=self.seq, op=op, body_bytes=len(body), args=s.plain(args))
                    await _write(self.socket, header, body, self.limits)
                    response, data = await _read(self.socket, self.limits)
                if (set(response) != {'v', 'seq', 'op', 'body_bytes', 'result'}
                        or response['v'] != 1 or response['seq'] != self.seq or response['op'] != op):
                    raise EOFError()
                self.seq += 1
                return {'result': response['result'], 'body': data}
            except asyncio.CancelledError:
                await self.close()
                raise
            except (OSError, EOFError, TimeoutError, s.RemoteError) as exc:
                await self.close()
                raise s.RemoteError('storage_error', CONNECTION_MESSAGE) from exc

    async def bind(self):
        if self.bound:
            raise s.RemoteError('storage_error', CONNECTION_MESSAGE)
        frame = await self._request('bind', {'principal': self.principal, 'config_digest': self.config_digest})
        if not frame['result']['ok']:
            error = frame['result']['error']
            await self.close()
            raise s.RemoteError(error['code'], error['message'])
        self.bound = True
        self.connection_id = frame['result']['connection_id']
        return frame['result']

    async def call(self, tool, args):
        if not self.bound:
            raise s.RemoteError('storage_error', CONNECTION_MESSAGE)
        return (await self._request('tool', {'tool': tool, 'arguments': args}))['result']

    async def stream(self, op, args, body=b''):
        if not self.bound or op not in STREAM_FIELDS:
            raise s.RemoteError('invalid_request')
        return await self._request(op, args, body)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


async def _stream(runtime, connection_id, op, args, body):
    if not isinstance(args, dict) or set(args) != set(STREAM_FIELDS[op]):
        raise s.RemoteError('invalid_request')
    for key in ('ticket_id', 'lease_id'):
        if key in args:
            s._validate('ID', args[key], s.WIRE)
    if 'offset' in args and (type(args['offset']) is not int or not 0 <= args['offset'] <= 9007199254740991):
        raise s.RemoteError('invalid_request')
    if 'capability' in args and not isinstance(args['capability'], str):
        raise s.RemoteError('invalid_request')
    if op != 'close':
        try:
            runtime.authorize(runtime.config.principal, op)
        except s.RemoteError:
            lease = runtime.leases.get(args.get('lease_id'))
            if lease is not None and lease.connection_id == connection_id:
                transfer.close(lease, 'abort')
            raise
    if op in ('open_put', 'open_get'):
        if type(args.get('content_length', 0)) is not int or args.get('content_length', 0) < 0:
            raise s.RemoteError('invalid_request')
        direction = 'put' if op == 'open_put' else 'get'
        with runtime.broker.store.transaction() as tx:
            lease = tickets.claim(tx, args['ticket_id'], args['capability'], direction, connection_id)
        if direction == 'put':
            ticket = store.rows(runtime.broker.store.db, 'remote_tickets', 'ticket_id=?', (lease.ticket_id,))[0]
            if args['content_length'] != ticket.bytes:
                with runtime.broker.store.transaction() as tx:
                    store.save(tx, 'remote_tickets', store.changed(ticket, state='revoked', lease_id=None, connection_id=None))
                raise s.RemoteError('length_mismatch')
        try:
            artifact = transfer.open_lease(lease)
        except Exception:
            with runtime.broker.store.transaction() as tx:
                ticket = store.rows(tx, 'remote_tickets', 'ticket_id=?', (lease.ticket_id,))[0]
                store.save(tx, 'remote_tickets', store.changed(ticket, state='revoked', lease_id=None, connection_id=None))
            raise
        result = {'ok': True, 'lease_id': lease.lease_id}
        result.update(offset=0) if direction == 'put' else result.update(artifact=s.artifact_projection(artifact))
        return result, b''
    if op == 'close' and args['reason'] not in ('complete', 'disconnect', 'timeout', 'abort'):
        raise s.RemoteError('invalid_request')
    lease = runtime.leases.get(args['lease_id'])
    if lease is None and op == 'close':
        return {'ok': True}, b''
    if lease is None or lease.connection_id != connection_id:
        raise s.RemoteError('ticket_invalid')
    if op == 'put_chunk':
        return {'ok': True, 'offset': transfer.put_chunk(lease, args['offset'], body)}, b''
    if op == 'finish_put':
        return transfer.finish_put(lease), b''
    if op == 'read_chunk':
        result = transfer.read_chunk(lease, args['offset'], args['max_bytes'])
        return {'ok': True, 'offset': result.offset, 'eof': result.eof}, result.body
    transfer.close(lease, args['reason'])
    return {'ok': True}, b''


async def serve_endpoint(socket, core, principal):
    runtime = core.runtime
    limits = runtime.config.limits
    connection_id = next((session.connection_id for session in runtime.sessions.values() if session.socket is socket), store.uid())
    seq = 0
    bound = False
    socket.setblocking(False)
    try:
        while True:
            async with asyncio.timeout(limits.ipc_idle_ms / 1000):
                header, body = await _read(socket, limits)
            close_after = False
            result_body = b''
            op = header.get('op')
            request_args = header.get('args', {})
            prior_lease = runtime.leases.get(request_args.get('lease_id')) if isinstance(request_args, dict) and isinstance(request_args.get('lease_id'), str) else None
            try:
                if (set(header) != {'v', 'seq', 'op', 'body_bytes', 'args'} or type(header['v']) is not int
                        or header['v'] != 1 or type(header['seq']) is not int or header['seq'] != seq
                        or seq > 9007199254740991 or op not in ('bind', 'tool', *STREAM_FIELDS)
                        or (body and op != 'put_chunk')):
                    close_after = True
                    raise s.RemoteError('invalid_request')
                args = header['args']
                if not bound:
                    close_after = True
                    if op != 'bind' or not isinstance(args, dict) or set(args) != {'principal', 'config_digest'}:
                        raise s.RemoteError('invalid_request')
                    if s.Principal(**args['principal']) != principal or args['config_digest'] != runtime.snapshot.config_digest:
                        raise s.RemoteError('permission_refused')
                    runtime.authorize(principal, 'bind')
                    bound, close_after = True, False
                    result = {'ok': True, 'connection_id': connection_id}
                elif op == 'bind':
                    close_after = True
                    raise s.RemoteError('invalid_request')
                elif op == 'tool':
                    if not isinstance(args, dict) or set(args) != {'tool', 'arguments'}:
                        close_after = True
                        raise s.RemoteError('invalid_request')
                    result = await core.dispatch_remote(principal, args['tool'], args['arguments'])
                else:
                    result, result_body = await _stream(runtime, connection_id, op, args, body)
            except s.RemoteError as exc:
                result = exc.payload()
                with runtime.broker.store.transaction() as tx:
                    store.event(tx, 'runtime', None, reason=exc.code)
                if exc.code == 'invalid_request' and op in STREAM_FIELDS:
                    close_after = True
            except Exception:
                if runtime.broker.store.admission_closed:
                    runtime.fatal()
                    close_after = True
                result = s.RemoteError('storage_error').payload()
            deadline = time.monotonic() + limits.ipc_idle_ms / 1000
            args = header.get('args', {})
            lease = runtime.leases.get(args.get('lease_id')) if isinstance(args, dict) else None
            if lease is None and isinstance(result, dict):
                lease = runtime.leases.get(result.get('lease_id'))
            if lease is None:
                lease = prior_lease
            if lease is not None and lease.connection_id == connection_id:
                deadline = min(deadline, lease.deadline_ms / 1000, lease.idle_deadline_ms / 1000)
            response = dict(v=1, seq=seq, op=op, body_bytes=len(result_body), result=result)
            # Validate before the only write; uncertain sends cannot select a fallback.
            try:
                encoded = _encode(response, result_body, limits)
            except s.RemoteError:
                lease_id = result.get('lease_id')
                if lease_id in runtime.leases:
                    transfer.close(runtime.leases[lease_id], 'abort')
                response.update(body_bytes=0, result=s.RemoteError('output_limit', 'Control response exceeds configured bound').payload())
                result_body = b''
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                encoded = _encode(response, result_body, limits)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            async with asyncio.timeout_at(deadline):
                await _write(socket, response, result_body, limits, deadline=deadline, encoded=encoded)
            seq += 1
            if close_after:
                break
    except (EOFError, OSError, TimeoutError, s.RemoteError) as exc:
        # EOF and expired transport waits carry no domain request identity.
        # Malformed JSON/headers have not reached CoreBroker's refusal boundary.
        if isinstance(exc, s.RemoteError):
            with runtime.broker.store.transaction() as tx:
                store.event(tx, 'runtime', None, reason=exc.code)
    finally:
        try:
            for lease in tuple(runtime.leases.values()):
                if lease.connection_id == connection_id:
                    try:
                        transfer.close(lease, 'disconnect')
                    except Exception:
                        runtime.fatal()
        finally:
            socket.close()
