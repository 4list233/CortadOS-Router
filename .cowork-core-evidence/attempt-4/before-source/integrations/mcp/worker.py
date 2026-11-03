# PURPOSE: Own one gated native process tree and atomically close its execution evidence.
# CONTRACT: RT-MCP-001 sections 3, 6 and 7; watchdog never writes the canonical job database.
# DEPENDS: codex, native_process, grant_proxy; broker owns acceptance, stop intent and terminal state.
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from .codex import EventReader, native_arguments, native_environment, terminal_result
from .grant_proxy import GrantPayloadServer
from .native_process import ProcessTracker, atomic_json, process_identity
from .native_profile import check_version, record_version
from .schemas import WorkerError


def send(event):
    try:
        sys.stdout.buffer.write(json.dumps(event, ensure_ascii=False, allow_nan=False).encode() + b"\n")
        sys.stdout.buffer.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


class Watchdog:
    def __init__(self, bootstrap, *, execution_kind=None):
        self.bootstrap = bootstrap
        self.config, self.arguments, self.route = (bootstrap[name] for name in ("config", "arguments", "route"))
        self.job_id = bootstrap["job_id"]
        self.remote_binding = None
        self.remote_errors = ()
        if execution_kind is not None or 'execution_kind' in bootstrap or 'remote_binding' in bootstrap:
            from .remote_schemas import RemoteError, WorkerBinding
            from .remote_native import verify_binding, build_handoff, load_manifest
            if execution_kind != 'cowork_remote_v1' or bootstrap.get('execution_kind') != execution_kind or not bootstrap.get('remote_binding'):
                raise RemoteError('policy_unavailable')
            binding = WorkerBinding(**bootstrap['remote_binding'])
            if binding.job_id != self.job_id:
                raise RemoteError('policy_unavailable')
            verify_binding(binding, binding.native_manifest)
            if build_handoff(self.arguments, binding.input_manifest, self.config['limits']['max_handoff_bytes']) != binding.handoff:
                raise RemoteError('policy_unavailable')
            self.remote_binding = binding
            self.remote_errors = (RemoteError,)
        self.cli_version = record_version(bootstrap)
        self.directory = Path(self.config["state_dir"]) / "jobs" / self.job_id
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.identity = process_identity(os.getpid())
        if self.identity is None:
            raise OSError("Watchdog native process identity is unavailable")
        self.native = None
        self.tracker = None
        self.proxy = None
        self.error = None
        self.stop_at = None
        self.phase = 0
        self.bytes_read = 0
        self.reader = EventReader()
        self.selector = selectors.DefaultSelector()
        self.control = b""
        self.started = False
        self.artifacts = {"final_message": None, "events": None}
        self.streams = {}
        self.unknown_native = False
        self.poll_s = self.config["limits"]["process_poll_ms"] / 1000
        self.grace_s = self.config["limits"]["termination_grace_s"]
        self.selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "control")
        os.set_blocking(sys.stdin.fileno(), False)

    def stop(self, reason):
        if self.error is None:
            if self.remote_binding is not None:
                from .remote_schemas import RemoteError
                self.error = RemoteError(reason)
            else:
                self.error = WorkerError(reason, "Worker stopped: " + reason)
        if self.stop_at is None:
            self.stop_at = time.monotonic()
            if self.tracker:
                self.tracker.signal_groups(signal.SIGINT, root_only=True)
            self.phase = 1

    def _control(self):
        chunk = os.read(sys.stdin.fileno(), self.config["limits"]["max_handoff_bytes"])
        if not chunk:
            self.selector.unregister(sys.stdin.fileno())
            self.stop("broker_interrupted")
            return
        lines = (self.control + chunk).split(b"\n")
        self.control = lines.pop()
        for line in lines:
            try:
                command = json.loads(line)
            except (ValueError, UnicodeError):
                self.stop("broker_interrupted")
                continue
            if command.get("command") == "STOP":
                self.stop(command.get("reason", "broker_interrupted"))
            elif (command.get("command") == "START" and not self.started and self.error is None
                  and command.get("launch_nonce") == self.bootstrap["launch_nonce"]):
                self.start()
            else:
                self.stop("broker_interrupted")

    def start(self):
        self.started = True
        os.environ["CORTADOS_HOME"] = str(Path(self.config["state_dir"]) / "router")
        from cortados_router.route_task import handoff
        if self.arguments["permissions"]["mcp_servers"]:
            self.proxy = GrantPayloadServer(self.config, self.arguments["permissions"])
        self.streams = {name: (self.directory / (name + ".tmp")).open("wb")
                        for name in ("events.jsonl", "native.stderr")}
        argv = native_arguments(self.config, self.arguments, self.route,
                                self.directory / "final.txt.tmp", self.proxy.path if self.proxy else None,
                                **({'remote_binding': self.remote_binding} if self.remote_binding is not None else {}))
        environment = native_environment(self.config)
        check_version(self.config, time.monotonic() + self.config["limits"]["startup_timeout_s"],
                      expected=self.cli_version)
        if self.remote_binding is not None:
            from .remote_native import load_manifest, build_handoff, verify_inputs
            from .remote_schemas import json_bytes, RemoteError
            load_manifest(json_bytes(self.remote_binding.native_manifest), self.config, self.remote_binding.execution_policy)
            identity = os.stat(self.arguments['cwd'], follow_symlinks=False)
            expected = self.remote_binding.workspace_identity
            if ((identity.st_dev, identity.st_ino) != (expected.device, expected.inode)
                    or build_handoff(self.arguments, self.remote_binding.input_manifest,
                                     self.config['limits']['max_handoff_bytes']) != self.remote_binding.handoff):
                raise RemoteError('policy_unavailable')
            verify_inputs(self.remote_binding, self.arguments['cwd'])
        self.native = subprocess.Popen(argv, cwd=self.arguments["cwd"], env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, shell=False)
        identity = process_identity(self.native.pid)
        if identity is None:
            self.unknown_native = True
            raise WorkerError("containment_failed", "Native launch identity could not be observed")
        self.tracker = ProcessTracker(identity)
        self.tracker.sample()
        packet = self.remote_binding.handoff.text if self.remote_binding is not None else handoff(self.arguments)
        self.handoff = memoryview(packet.encode("utf-8"))
        for stream, label, event in ((self.native.stdout, "events.jsonl", selectors.EVENT_READ),
                                     (self.native.stderr, "native.stderr", selectors.EVENT_READ),
                                     (self.native.stdin, "handoff", selectors.EVENT_WRITE)):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, event, label)
        if not send({"event": "running", "native": identity}):
            self.stop("broker_interrupted")

    def _output(self, key):
        # Read chunks are bounded by the operator's output budget, never a hidden retention limit.
        chunk = os.read(key.fileobj.fileno(), self.config["limits"]["max_output_bytes"])
        if not chunk:
            self.selector.unregister(key.fileobj)
            key.fileobj.close()
            return
        self.bytes_read += len(chunk)
        if self.bytes_read > self.config["limits"]["max_output_bytes"]:
            self.stop("output_limit")
            return
        self.streams[key.data].write(chunk)
        if key.data == "events.jsonl" and self.error is None:
            try:
                self.reader.feed(chunk)
            except (WorkerError, *self.remote_errors) as exc:
                self.stop(exc.code)

    def _write_handoff(self, key):
        try:
            count = os.write(key.fileobj.fileno(), self.handoff)
            self.handoff = self.handoff[count:]
        except BrokenPipeError:
            self.handoff = memoryview(b"")
        if not self.handoff:
            self.selector.unregister(key.fileobj)
            key.fileobj.close()

    def _tick(self):
        if self.tracker is None:
            return self.error is not None
        alive = self.tracker.remaining()
        root_done = self.native.poll() is not None
        if self.tracker.lost_ownership:
            self.stop("containment_failed")
        if root_done and alive and self.stop_at is None:
            # Native root exit is not proof that its MCP/tool descendants have exited.
            self.stop_at = time.monotonic()
            self.phase = 1
            self.tracker.signal_groups(signal.SIGINT, root_only=True)
        if self.stop_at is not None and alive:
            elapsed = time.monotonic() - self.stop_at
            if self.phase == 1 and elapsed >= self.grace_s:
                self.tracker.signal_groups(signal.SIGTERM)
                self.phase = 2
            elif self.phase == 2 and elapsed >= 2 * self.grace_s:
                self.tracker.signal_groups(signal.SIGKILL)
                self.phase = 3
            elif self.phase == 3 and elapsed >= 3 * self.grace_s:
                # Each escalation receives the configured grace in seconds; less can race exit,
                # more would retain claims longer after a process resists KILL.
                self.error = WorkerError("containment_failed", "Owned process termination could not be proved")
                return True
        return root_done and not alive and not any(key.data in self.streams for key in self.selector.get_map().values())

    def run(self):
        if not send({"event": "ready", "watchdog": self.identity}):
            self.stop("broker_interrupted")
        while True:
            try:
                for key, _ in self.selector.select(self.poll_s):
                    if key.data == "control":
                        self._control()
                    elif key.data == "handoff":
                        self._write_handoff(key)
                    else:
                        self._output(key)
                if self._tick():
                    break
            except (WorkerError, *self.remote_errors) as exc:
                self.stop(exc.code)
            except (OSError, ValueError) as exc:
                self.stop("storage_error" if isinstance(exc, OSError) else "native_protocol_error")
        return self.finish()

    def _close_artifacts(self):
        for name, stream in self.streams.items():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(self.directory / (name + ".tmp"), self.directory / name)
        if self.streams:
            self.artifacts["events"] = "events.jsonl"
        final = self.directory / "final.txt.tmp"
        if final.exists():
            with final.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(final, self.directory / "final.txt")
            self.artifacts["final_message"] = "final.txt"

    def finish(self):
        contained = not self.unknown_native and (self.tracker.contained() if self.tracker else True)
        summary = usage = None
        if self.proxy and self.proxy.failure and self.error is None:
            self.error = WorkerError("connector_unavailable", "Selected connector startup or transport failed")
        if contained:
            if self.native:
                self.native.wait()
            if self.error is None:
                try:
                    summary, usage = self.reader.completion(self.native.returncode, self.directory / "final.txt.tmp")
                except WorkerError as exc:
                    self.error = exc
            try:
                self._close_artifacts()
            except OSError:
                self.error = WorkerError("storage_error", "Native artifacts could not be closed atomically")
                contained = False
        else:
            self.error = WorkerError("containment_failed", "Owned process termination could not be proved")
        if self.proxy:
            self.proxy.close()
        receipt = {"schema": "cortados.mcp-worker.termination.v1", "job_id": self.job_id,
                   "launch_nonce": self.bootstrap["launch_nonce"], "host_boot_id": self.bootstrap["host_boot_id"],
                   "watchdog": self.identity, "native": self.tracker.native if self.tracker else None,
                   "observed": list(self.tracker.observed.values()) if self.tracker else [],
                   "contained": contained, "observed_at_ms": time.time_ns() // 1_000_000}
        atomic_json(self.directory / "termination.json", receipt)
        result = terminal_result(self.route, self.job_id, self.artifacts, summary=summary, usage=usage,
                                 cli_version=self.cli_version, warnings=self.reader.warnings, error=self.error)
        send({"event": "done", "result": result, "state": "failed" if self.error else "succeeded",
              "reason": self.error.code if self.error else None, "contained": contained})
        self.selector.close()
        return 0 if contained else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--execution-kind")
    args = parser.parse_args()
    # Bootstrap is trusted broker control, read once before any model/proxy starts.
    bootstrap = json.loads(sys.stdin.buffer.raw.readline())
    if bootstrap["config"]["state_dir"] != args.state_dir:
        raise ValueError("Watchdog state directory does not match bootstrap")
    watchdog = Watchdog(bootstrap, execution_kind=args.execution_kind)
    signal.signal(signal.SIGTERM, lambda *_: watchdog.stop("broker_interrupted"))
    signal.signal(signal.SIGINT, lambda *_: watchdog.stop("broker_interrupted"))
    return watchdog.run()


if __name__ == "__main__":
    raise SystemExit(main())
