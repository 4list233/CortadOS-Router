# PURPOSE: Keep accepted worker attempts alive independently of MCP frontends.
# CONTRACT: RT-MCP-001 sections 5–7; durable START handshake and one job-store writer.
# DEPENDS: JobStore, native metadata/Router admission, identity-checking watchdog.
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from .schemas import WorkerError, emit_event, validate
from .job_store import JobStore, TERMINAL, failure_result


def socket_path(state_dir):
    # Unix sockaddr paths are short on macOS. A digest names the state without
    # truncating its identity, inside a separate owner-only directory.
    digest = hashlib.sha256(os.fsencode(state_dir)).hexdigest()
    directory = Path('/private/tmp') / ('cortados-mcp-' + str(os.getuid()) + '-' + digest[:32])
    if directory.is_symlink():
        raise WorkerError('permission_refused', 'Broker socket directory is a symbolic link')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WorkerError('permission_refused', 'Broker socket directory is not private')
    return str(directory / 'broker.sock')


def rpc(config, digest, tool, arguments):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(socket_path(config['state_dir']))
            stream = connection.makefile('rwb')
            stream.write(json.dumps({'config_digest': digest, 'tool': tool, 'arguments': arguments},
                                    ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n')
            stream.flush()
            response = stream.readline()
            if not response:
                raise WorkerError('storage_error', 'Broker connection ended before its response')
            return json.loads(response)
    except (OSError, ValueError) as exc:
        raise WorkerError('storage_error', 'Broker connection unavailable') from exc


def ensure_broker(config, config_path, digest):
    state = Path(config['state_dir'])
    with (state / 'frontend-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            response = rpc(config, digest, '__hello', {})
            if not response['ok']:
                error = WorkerError(response['error']['code'], response['error']['message'])
                error.emitted = True
                raise error
            return
        except WorkerError as exc:
            if exc.code != 'storage_error':
                raise
        error_path = state / 'startup-error.json'
        error_path.unlink(missing_ok=True)
        with (state / 'broker.jsonl').open('ab', buffering=0) as errors:
            process = subprocess.Popen([sys.executable, '-B', '-m', 'integrations.mcp',
                '--broker', '--config', str(config_path)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + config['limits']['startup_timeout_s']
        while time.monotonic() < deadline:
            if error_path.exists():
                error = json.loads(error_path.read_text())
                raise WorkerError(error['code'], error['message'])
            try:
                response = rpc(config, digest, '__hello', {})
                if not response['ok']:
                    error = WorkerError(response['error']['code'], response['error']['message'])
                    error.emitted = True
                    raise error
                return
            except WorkerError as exc:
                if exc.code != 'storage_error':
                    raise
            if process.poll() is not None:
                break
            # Operator process observation cadence also bounds startup polling:
            # smaller increases wakeups, larger adds reconnect latency.
            time.sleep(config['limits']['process_poll_ms'] / 1000)
        raise WorkerError('storage_error', 'Broker failed to start within startup_timeout_s')


class Broker:
    def __init__(self, config, digest):
        from .native_metadata import NativeMetadata
        from .native_process import host_boot_id
        self.config, self.digest = config, digest
        self.store = JobStore(config, host_boot_id())
        self.metadata = NativeMetadata(config)
        try:
            self.metadata.preflight()
        except WorkerError as exc:
            # An unsupported executable remains a typed tool refusal. Managed
            # policy failures still refuse frontend startup before tool access.
            if exc.code != 'native_version_mismatch':
                raise
        self.workers, self.tasks = {}, set()
        self.admission_lock = asyncio.Lock()
        self.stopping = False
        self.recover()

    def receipt(self, record):
        from .native_process import validate_receipt
        path = Path(self.config['state_dir']) / 'jobs' / record['job_id'] / 'termination.json'
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            return validate_receipt(value, record['job_id'], record['launch_nonce'],
                                    record['host_boot_id'], record['watchdog'])
        except (OSError, ValueError, TypeError):
            return False

    def recover(self):
        for record in self.store.active():
            if record['host_boot_id'] != self.store.boot_id or self.receipt(record):
                result = failure_result(record, 'broker_interrupted', 'Broker stopped during the worker attempt')
                self.store.transition(record['job_id'], 'interrupted', reason='broker_interrupted', result=result)
            else:
                self.store.containment_failed(record['job_id'])

    async def dispatch(self, tool, arguments):
        from cortados_router.route_task import route_task
        if self.stopping:
            raise WorkerError('storage_error', 'Broker is shutting down')
        if tool == '__hello':
            return {'ok': True}
        arguments = validate(tool, arguments, self.config)
        if tool == 'route':
            async with self.admission_lock:
                route = await asyncio.to_thread(route_task, arguments, self.config, self.metadata)
            return {'ok': True, 'route': route}
        if tool == 'delegate':
            # Identity is checked before any changed configuration/admission/scoring.
            async with self.admission_lock:
                previous = self.store.replay(arguments)
                if previous:
                    return self.store.response(previous)
                route = await asyncio.to_thread(route_task, arguments, self.config, self.metadata)
                record, created = self.store.accept(arguments, route)
                if created:
                    task = asyncio.create_task(self.run_worker(record))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                return self.store.response(record)
        record = self.store.get(arguments['job_id'])
        if tool == 'job_cancel' and record['state'] not in TERMINAL:
            record = await self.stop(record['job_id'], 'cancel_requested')
        return self.store.response(record, include_result=tool == 'job_result')

    async def connection(self, reader, writer):
        request = {}
        try:
            request = json.loads(await reader.readline())
            if request.get('config_digest') != self.digest:
                raise WorkerError('invalid_request', 'Configuration differs from the running broker')
            result = await self.dispatch(request['tool'], request['arguments'])
        except WorkerError as exc:
            args = request.get('arguments') or {}
            emit_event(self.config, 'refusal', request_id=args.get('request_id'),
                       job_id=args.get('job_id'), reason=exc.code)
            result = exc.payload()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            emit_event(self.config, 'refusal', reason='invalid_request')
            result = WorkerError('invalid_request', 'Invalid broker request').payload()
        try:
            writer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n')
            await writer.drain()
        except (ConnectionError, OSError):
            # Disconnect has no ownership of an already committed attempt.
            return
        finally:
            writer.close()

    async def send(self, process, command):
        try:
            process.stdin.write(json.dumps(command).encode('utf-8') + b'\n')
            await process.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionError):
            return False

    async def stop(self, job_id, reason):
        record = self.store.get(job_id)
        if record['state'] in TERMINAL or record['stop_intent']:
            return record
        record = self.store.transition(job_id, 'cancelling', reason=reason)
        process = self.workers.get(job_id)
        if process:
            await self.send(process, {'command': 'STOP', 'reason': reason})
        return record

    async def deadline(self, job_id):
        record = self.store.get(job_id)
        remaining = record['accepted_monotonic'] + record['arguments']['timeout_s'] - time.monotonic()
        await asyncio.sleep(max(0, remaining))
        await self.stop(job_id, 'timeout')

    async def admit_launch(self, record):
        from cortados_router.worker_admission import admit
        async with self.admission_lock:
            admission = await asyncio.to_thread(admit, record['arguments'], self.config, self.metadata)
            if record['route']['chosen']['model'] not in admission['models']:
                raise WorkerError('model_unavailable', 'Selected model is no longer admitted')

    async def run_worker(self, accepted):
        job_id, process, launch_error = accepted['job_id'], None, None
        timer = asyncio.create_task(self.deadline(job_id))
        job_dir = Path(self.config['state_dir']) / 'jobs' / job_id
        try:
            job_dir.mkdir(mode=0o700, parents=True)
            # JSON string escapes may expand observed output sixfold. This IPC
            # frame limit preserves the operator's output bound without clipping
            # valid final messages; lower drops evidence, higher permits waste.
            with (job_dir / 'watchdog-stderr.txt').open('wb') as errors:
                process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-m',
                    'integrations.mcp.worker', '--state-dir', self.config['state_dir'],
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=errors, start_new_session=True,
                    limit=(self.config['limits']['max_output_bytes'] +
                           self.config['limits']['max_handoff_bytes']) * 6)
            self.workers[job_id] = process
            await self.send(process, {'config': self.config, 'arguments': accepted['arguments'],
                'route': accepted['route'], 'job_id': job_id, 'launch_nonce': accepted['launch_nonce'],
                'host_boot_id': accepted['host_boot_id']})
            while line := await process.stdout.readline():
                message = json.loads(line)
                record = self.store.get(job_id)
                if message.get('event') == 'ready':
                    record = self.store.identify(job_id, watchdog=message['watchdog'])
                    if not record['stop_intent']:
                        try:
                            await self.admit_launch(record)
                        except WorkerError as exc:
                            launch_error = exc
                    record = self.store.get(job_id)
                    if record['stop_intent'] or launch_error:
                        reason = record['stop_intent'] or launch_error.code
                        await self.send(process, {'command': 'STOP', 'reason': reason})
                    else:
                        await self.send(process, {'command': 'START', 'launch_nonce': record['launch_nonce']})
                elif message.get('event') == 'running':
                    self.store.identify(job_id, native=message['native'])
                    if record['state'] == 'queued':
                        self.store.transition(job_id, 'running')
                elif message.get('event') == 'done':
                    self.finish(job_id, message, launch_error)
            await process.wait()
            record = self.store.get(job_id)
            if record['state'] not in TERMINAL and record['reason'] != 'containment_failed':
                if self.receipt(record):
                    self.finish(job_id, {'state': 'failed', 'reason': 'native_failed',
                        'result': failure_result(record, 'native_failed', 'Watchdog ended without completion')}, launch_error)
                else:
                    self.store.containment_failed(job_id)
        except (WorkerError, OSError, ValueError, KeyError, TypeError) as exc:
            record = self.store.get(job_id)
            if process is None:
                result = failure_result(record, 'native_failed', 'Watchdog could not start')
                state = self.stop_state(record) if record['stop_intent'] else 'failed'
                self.store.transition(job_id, state, reason=record['stop_intent'] or 'native_failed', result=result)
            else:
                await self.send(process, {'command': 'STOP', 'reason': 'native_protocol_error'})
                self.store.containment_failed(job_id)
        finally:
            timer.cancel()
            self.workers.pop(job_id, None)
            if process and process.stdin:
                process.stdin.close()

    @staticmethod
    def stop_state(record):
        return 'timed_out' if record['stop_intent'] == 'timeout' else 'cancelled'

    def finish(self, job_id, message, launch_error=None):
        record = self.store.get(job_id)
        if record['state'] in TERMINAL:
            return
        if not self.receipt(record):
            self.store.containment_failed(job_id)
            return
        if record['stop_intent']:
            state, reason = self.stop_state(record), record['stop_intent']
            result = failure_result(record, reason, 'Worker stopped after the committed stop request')
        elif launch_error:
            state, reason = 'failed', launch_error.code
            result = failure_result(record, reason, launch_error.message)
        else:
            state, reason, result = message['state'], message.get('reason'), message['result']
        self.store.transition(job_id, state, reason=reason, result=result)

    async def shutdown(self):
        self.stopping = True
        for job_id in list(self.workers):
            await self.stop(job_id, 'cancel_requested')
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


async def serve(config, digest):
    state = Path(config['state_dir'])
    with (state / 'broker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerError('storage_error', 'Another broker owns this state') from exc
        broker = Broker(config, digest)
        path = socket_path(config['state_dir'])
        Path(path).unlink(missing_ok=True)
        # The handoff bound plus serialized envelopes defines this private frame
        # bound. Unicode JSON escaping can expand bytes sixfold; lower drops valid
        # payloads, higher retains unnecessary memory on malformed local requests.
        frame_limit = config['limits']['max_handoff_bytes'] * 6 + config['limits']['max_output_bytes']
        server = await asyncio.start_unix_server(broker.connection, path=path, limit=frame_limit)
        os.chmod(path, 0o600)
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopping.set)
        try:
            async with server:
                await stopping.wait()
            await broker.shutdown()
        finally:
            broker.store.close()
            Path(path).unlink(missing_ok=True)
