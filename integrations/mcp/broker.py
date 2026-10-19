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
from .native_profile import record_version


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
    def __init__(self, config, digest, *, core_runtime=None):
        from .native_metadata import NativeMetadata
        from .native_process import host_boot_id
        self.config, self.digest = config, digest
        self.core_runtime = core_runtime
        if core_runtime is not None and core_runtime.lock_fd is None:
            raise WorkerError('storage_error', 'Core runtime does not own the broker lock')
        self.store = JobStore(config, host_boot_id())
        self.metadata = NativeMetadata(config)
        try:
            self.metadata.preflight()
        except WorkerError as exc:
            # An unsupported executable remains a typed tool refusal. Managed
            # policy failures still refuse frontend startup before tool access.
            if exc.code != 'native_version_mismatch':
                self.store.close()
                raise
        self.workers, self.tasks = {}, set()
        self.admission_lock = asyncio.Lock()
        self.stopping = False
        if core_runtime is None:
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
        if tool == 'context_requirements':
            from cortados_router.task_evidence import context_requirements
            return context_requirements(arguments, self.config)
        if tool == 'job_feedback':
            return self.store.feedback(arguments)
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
                record, created = self.store.accept(arguments, route, cli_version=self.metadata.version)
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
        try:
            record = self.store.get(job_id)
            if record['state'] in TERMINAL or record['stop_intent']:
                return record
            record = self.store.transition(job_id, 'cancelling', reason=reason)
        except Exception:
            if self.core_runtime is not None:
                try:
                    self.core_runtime.fatal()
                finally:
                    process = self.workers.get(job_id)
                    if process:
                        await self.send(process, {'command': 'STOP', 'reason': reason})
            raise
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

    async def guard_remote_launch(self, job_id):
        runtime = self.core_runtime
        runtime.observe_monitor(runtime.monitor_task)
        if runtime.fatal_failed or runtime.capacity_failed or runtime.broker.store.admission_closed:
            # A pending spawn may have missed the containment enumeration. Use
            # this launch's identity and the existing STOP/receipt owner instead.
            await self.stop(job_id, 'capacity_exhausted')

    async def run_worker(self, accepted):
        job_id, process, launch_error = accepted['job_id'], None, None
        timer = asyncio.create_task(self.deadline(job_id))
        job_dir = Path(self.config['state_dir']) / 'jobs' / job_id
        remote = accepted.get('execution_kind') == 'cowork_remote_v1'
        remote_errors = ()
        if self.core_runtime is not None:
            from .remote_schemas import RemoteError
            remote_errors = (RemoteError,)
        try:
            if self.core_runtime is not None:
                remote = self.core_runtime.classify(self.store.get(job_id))
            remote_bootstrap = {}
            remote_cli = []
            if remote:
                from .remote_native import worker_binding
                from .task_workspace import prepare
                binding = self.core_runtime.binding(accepted)
                task = self.core_runtime.task(binding.seed.task_id)
                prepare(task, binding)
                self.core_runtime.materialize_inputs(binding)
                remote_bootstrap = {'execution_kind': 'cowork_remote_v1', 'remote_binding':
                                    self.core_runtime.record_dict(worker_binding(binding))}
                remote_cli = ['--execution-kind', 'cowork_remote_v1']
            job_dir.mkdir(mode=0o700, parents=True)
            # JSON string escapes may expand observed output sixfold. This IPC
            # frame limit preserves the operator's output bound without clipping
            # valid final messages; lower drops evidence, higher permits waste.
            with (job_dir / 'watchdog-stderr.txt').open('wb') as errors:
                process = await asyncio.create_subprocess_exec(sys.executable, '-B', '-m',
                    'integrations.mcp.worker', '--state-dir', self.config['state_dir'],
                    *remote_cli,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=errors, start_new_session=True,
                    limit=(self.config['limits']['max_output_bytes'] +
                           self.config['limits']['max_handoff_bytes']) * 6)
            self.workers[job_id] = process
            await self.send(process, {'config': self.config, 'arguments': accepted['arguments'],
                'route': accepted['route'], 'job_id': job_id, 'launch_nonce': accepted['launch_nonce'],
                'host_boot_id': accepted['host_boot_id'], 'cli_version': record_version(accepted), **remote_bootstrap})
            if remote:
                await self.guard_remote_launch(job_id)
            while line := await process.stdout.readline():
                message = json.loads(line)
                if remote and message.get('event') == 'ready':
                    await self.guard_remote_launch(job_id)
                record = self.store.get(job_id)
                if message.get('event') == 'ready':
                    record = self.store.identify(job_id, watchdog=message['watchdog'])
                    if not record['stop_intent']:
                        try:
                            await self.admit_launch(record)
                        except (WorkerError, *remote_errors) as exc:
                            launch_error = exc
                    if remote:
                        await self.guard_remote_launch(job_id)
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
        except (WorkerError, *remote_errors, OSError, ValueError, KeyError, TypeError) as exc:
            record = self.store.get(job_id)
            if process is None:
                code = exc.code if remote and isinstance(exc, remote_errors) else 'native_failed'
                result = failure_result(record, code, 'Watchdog could not start')
                state = self.stop_state(record) if record['stop_intent'] else 'failed'
                self.store.transition(job_id, state, reason=record['stop_intent'] or code, result=result)
            else:
                reason = exc.code if remote and isinstance(exc, remote_errors) else 'native_protocol_error'
                await self.send(process, {'command': 'STOP', 'reason': reason})
                if remote:
                    # Keep ownership until the existing watchdog closes and its
                    # termination receipt proves the native tree is contained.
                    done = None
                    while line := await process.stdout.readline():
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if message.get('event') == 'done':
                            done = message
                    await process.wait()
                    if self.receipt(self.store.get(job_id)):
                        self.finish(job_id, done or {'state': 'failed', 'reason': reason,
                            'result': failure_result(record, reason, 'Remote launch refused')}, exc)
                    else:
                        self.store.containment_failed(job_id)
                else:
                    self.store.containment_failed(job_id)
        finally:
            timer.cancel()
            if not remote or process is None or self.receipt(self.store.get(job_id)):
                self.workers.pop(job_id, None)
            if process and process.stdin:
                process.stdin.close()
            if remote:
                await self.core_runtime.publish(job_id)

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

    async def contain_remote(self, reason):
        jobs = set(self.workers)
        try:
            jobs.update(record['job_id'] for record in self.store.active()
                        if record.get('execution_kind') == 'cowork_remote_v1')
        except Exception:
            self.signal_remote_failure()
        for job_id in jobs:
            try:
                await self.stop(job_id, reason)
            except Exception:
                self.signal_remote_failure()

    def signal_remote_failure(self):
        try:
            self.core_runtime.fatal()
        except Exception:
            # Runtime retains the failed sink for teardown.
            self.core_runtime.fatal_signal_failed = True

    async def shutdown(self):
        self.stopping = True
        if self.core_runtime is not None:
            await self.contain_remote('cancel_requested')
            if self.tasks:
                results = await asyncio.gather(*list(self.tasks), return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    self.signal_remote_failure()
            for job_id in tuple(self.workers):
                if not self.receipt(self.store.get(job_id)):
                    raise WorkerError('containment_failed', 'Worker termination is unproven')
                self.workers.pop(job_id, None)
            return
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
