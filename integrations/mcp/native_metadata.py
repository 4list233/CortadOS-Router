# PURPOSE: Read complete, bounded native subscription/inventory/policy observations.
# CONTRACT: RT-MCP-001 sections 4.1 and 5; never refresh credentials or start a turn.
# DEPENDS: Pinned native app-server JSONL methods, codex environment owner, schemas.
"""Synchronous metadata owner with a complete-snapshot, age-bounded cache."""

import copy
import json
import os
import selectors
import subprocess
import threading
import time

from .schemas import WorkerError
from .native_profile import check_version


# Bytes per pipe read, not an admission cap: larger chunks increase transient
# allocations and smaller chunks add syscalls; max_output_bytes bounds retention.
READ_CHUNK_BYTES = 65536


class _Session:
    """One native read-only session and one overall monotonic startup deadline."""

    def __init__(self, config, deadline):
        from .codex import native_environment
        self.config, self.deadline = config, deadline
        self.selector, self.pending, self.number, self.size = selectors.DefaultSelector(), b"", 0, 0
        argv = [config["codex_bin"], "app-server", "--listen", "stdio://"]
        for feature in ("hooks", "plugins", "apps", "multi_agent"):
            argv.extend(("--disable", feature))
        try:
            self.process = subprocess.Popen(argv, cwd=config["state_dir"], env=native_environment(config),
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, shell=False)
        except OSError as exc:
            self.selector.close()
            raise WorkerError("account_unavailable", "Native metadata process could not start") from exc
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.selector.register(self.process.stderr, selectors.EVENT_READ)

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=self.config["limits"]["termination_grace_s"])
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()

    def _line(self, code):
        while b"\n" not in self.pending:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError(code, "Native metadata startup deadline expired")
            for key, _ in self.selector.select(remaining):
                data = os.read(key.fileobj.fileno(), READ_CHUNK_BYTES)
                if not data:
                    self.selector.unregister(key.fileobj)
                    if key.fileobj is self.process.stdout:
                        raise WorkerError(code, "Native metadata closed without a complete response")
                    continue
                self.size += len(data)
                if self.size > self.config["limits"]["max_output_bytes"]:
                    raise WorkerError(code, "Native metadata exceeded the operator output limit")
                if key.fileobj is self.process.stdout:
                    self.pending += data
        line, self.pending = self.pending.split(b"\n", 1)
        return line

    def call(self, method, params, code):
        self.number += 1
        request = {"id": self.number, "method": method, "params": params}
        try:
            self.process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
            self.process.stdin.flush()
            while True:
                line = self._line(code)
                if not line.strip():
                    continue
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("Native response is not an object")
                if message.get("id") != self.number:
                    continue
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise WorkerError(code, "Native metadata method refused or returned an invalid result")
                return message["result"]
        except (OSError, ValueError, UnicodeError) as exc:
            raise WorkerError(code, "Native metadata protocol failed") from exc

    def initialize(self):
        result = self.call("initialize", {"clientInfo": {"name": "cortados-mcp-worker", "version": "1.0.0"},
                                          "capabilities": {"experimentalApi": True}}, "native_version_mismatch")
        if result.get("platformOs") != "macos":
            raise WorkerError("native_version_mismatch", "Only the captured native macOS profile is accepted")
        self.process.stdin.write(b'{"method":"initialized","params":{}}\n')
        self.process.stdin.flush()


def _policy(config, requirements):
    layers = config.get("layers")
    if not isinstance(layers, list) or not layers or "requirements" not in requirements:
        raise WorkerError("permission_refused", "Native managed policy provenance is unavailable")
    allowed = {"packagedDefaults", "system", "user", "project", "sessionFlags"}
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
            raise WorkerError("permission_refused", "Native managed policy provenance is invalid")
        kind, values = layer["name"].get("type"), layer.get("config")
        if kind not in allowed or not isinstance(values, dict) or (kind == "system" and values):
            raise WorkerError("permission_refused", "Native managed or unknown policy refuses worker startup")
        if kind == "project" and not layer.get("disabledReason"):
            raise WorkerError("permission_refused", "An active native project layer refuses worker startup")
    if requirements["requirements"] is not None:
        raise WorkerError("permission_refused", "Native managed requirements refuse worker startup")


class NativeMetadata:
    """Only successful, fully validated observations are cacheable."""

    def __init__(self, config):
        self.config, self._cached = config, None
        self._lock = threading.RLock()
        self.version = None

    def preflight(self):
        with self._lock:
            deadline = time.monotonic() + self.config["limits"]["startup_timeout_s"]
            version = check_version(self.config, deadline)
            session = _Session(self.config, deadline)
            try:
                session.initialize()
                policy = session.call("config/read", {"includeLayers": True}, "permission_refused")
                requirements = session.call("configRequirements/read", {}, "permission_refused")
                _policy(policy, requirements)
            finally:
                session.close()
            self.version = version

    def snapshot(self):
        with self._lock:
            if self._cached is not None and time.monotonic() - self._cached["sampled_monotonic"] <= self.config["limits"]["metadata_max_age_s"]:
                return copy.deepcopy(self._cached)
            self._cached = None
            if self.version is None:
                self.preflight()
            snapshot = self._collect()
            # Admission rejects failed account/quota observations before caching;
            # otherwise a refusal could incorrectly outlive a corrected native read.
            from cortados_router.worker_admission import validate_snapshot
            validate_snapshot(snapshot)
            self.version = snapshot["version"]
            self._cached = snapshot
            return copy.deepcopy(snapshot)

    def _collect(self):
        deadline = time.monotonic() + self.config["limits"]["startup_timeout_s"]
        version = check_version(self.config, deadline)
        session = _Session(self.config, deadline)
        try:
            session.initialize()
            account = session.call("account/read", {"refreshToken": False}, "account_unavailable")
            models, cursor, seen = [], None, set()
            while True:
                page = session.call("model/list", {"includeHidden": False, "cursor": cursor}, "model_unavailable")
                if not isinstance(page.get("data"), list) or "nextCursor" not in page:
                    raise WorkerError("model_unavailable", "Native inventory returned an invalid page")
                for row in page["data"]:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                        raise WorkerError("model_unavailable", "Native inventory returned an invalid model")
                    if row.get("hidden") is not True:
                        models.append(row["id"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise WorkerError("model_unavailable", "Native model inventory repeated an invalid cursor")
                seen.add(cursor)
            quota = session.call("account/rateLimits/read", {}, "quota_unknown")
            return {"account": account, "models": sorted(set(models)), "rate_limits": quota,
                    "sampled_at_ms": time.time_ns() // 1_000_000,
                    "sampled_monotonic": time.monotonic(), "version": version}
        finally:
            session.close()
