# PURPOSE: Black-box MCP client and temporary state for the independent worker exam.
# CONTRACT: RT-MCP-001; no production imports and no real model/credential access.
# DEPENDS: official MCP 1.30 client, unittest, stdlib and the native fixture executable.
# CEILING: Test observations do not replace real native permission/provider captures.
from contextlib import asynccontextmanager
from datetime import timedelta
import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
# Test wait bounds detect a stalled implementation; they do not set production policy.
TEST_WAIT_S = 12


def process_rows():
    text = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,state=,args="], text=True)
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4:
            rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
    return rows


def process_alive(pid):
    return any(row[0] == pid and not row[2].startswith("Z") for row in process_rows())


class WorkerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-worker-exam-")
        self.temp = Path(self.temporary.name).resolve()
        self.state = self.temp / "state"
        self.native_home = self.temp / "native"
        self.workspace = self.temp / "workspace"
        for path in (self.state, self.native_home, self.workspace):
            path.mkdir(mode=0o700)
        self.config_path = self.temp / "config.json"
        self.config = {
            "schema": "cortados.mcp-worker.config.v1", "client_id": "acceptance-client",
            "state_dir": str(self.state), "codex_bin": str(HERE / "fake_codex.py"),
            "codex_home": str(self.native_home), "model_allowlist": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
            "allow_network": False, "connector_registry": {},
            "limits": {"max_concurrent_jobs": 2, "max_timeout_s": 30,
                       "termination_grace_s": 1, "startup_timeout_s": 5,
                       "max_handoff_bytes": 65536, "max_output_bytes": 1048576,
                       "metadata_max_age_s": 1, "process_poll_ms": 20},
        }
        self.write_config()
        self.configure_native()
        self.server_env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG") if key in os.environ}
        self.server_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.server_env.get("PATH", "")
        self.server_env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1",
                               OPENAI_API_KEY="FORBIDDEN_ACCEPTANCE_API_KEY",
                               ANTHROPIC_API_KEY="FORBIDDEN_ACCEPTANCE_ANTHROPIC_KEY")
        self.stderr_path = self.temp / "frontend-stderr.log"

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config, sort_keys=True))

    def configure_native(self, **control):
        (self.native_home / "fixture-control.json").write_text(json.dumps(control))

    def native_calls(self, kind=None):
        path = self.native_home / "fixture-calls.jsonl"
        values = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [value for value in values if kind is None or value["kind"] == kind]

    def request(self, **updates):
        value = {"task": "Perform the fixture task.", "context": "Keep the exact fixture context.",
                 "selection": {"mode": "explicit", "model": "gpt-6-luna"},
                 "cwd": str(self.workspace), "request_id": str(uuid.uuid4()), "timeout_s": 20}
        value.update(updates)
        return value

    def route_request(self, **updates):
        value = self.request(**updates)
        for key in ("cwd", "request_id", "timeout_s"):
            value.pop(key)
        return value

    @asynccontextmanager
    async def session(self):
        parameters = StdioServerParameters(command=sys.executable,
            args=["-B", "-m", "integrations.mcp", "--config", str(self.config_path)],
            env=self.server_env, cwd=str(ROOT))
        with self.stderr_path.open("a") as errors:
            async with stdio_client(parameters, errlog=errors) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=TEST_WAIT_S)) as client:
                    await client.initialize()
                    yield client

    async def call(self, client, tool, arguments, *, error=None):
        result = await client.call_tool(tool, arguments)
        payload = result.structuredContent
        if payload is None:
            texts = [item.text for item in result.content if item.type == "text"]
            self.assertEqual(len(texts), 1, "protocol: expected one JSON result text")
            payload = json.loads(texts[0])
        self.assertIsInstance(payload, dict, "protocol: result must be a typed object")
        if error is None:
            self.assertFalse(result.isError, payload)
            self.assertIs(payload.get("ok"), True, payload)
        else:
            self.assertTrue(result.isError, payload)
            self.assertIs(payload.get("ok"), False, payload)
            self.assertEqual(payload["error"]["code"], error, payload)
        return payload

    async def terminal(self, client, job_id):
        end = time.monotonic() + TEST_WAIT_S
        while time.monotonic() < end:
            value = await self.call(client, "job_result", {"job_id": job_id})
            if value["job"]["state"] in TERMINAL:
                self.assertIsNotNone(value.get("result"), "results: terminal snapshot needs a result object")
                return value
            await asyncio.sleep(0.03)
        self.fail("lifecycle: job did not reach an observable terminal state")

    async def wait_native(self, kind="exec"):
        end = time.monotonic() + TEST_WAIT_S
        while time.monotonic() < end:
            calls = self.native_calls(kind)
            if calls:
                return calls[-1]
            await asyncio.sleep(0.03)
        self.fail("native boundary: expected fixture subprocess never started")

    def tearDown(self):
        # Only processes with this private test directory in argv or recorded by this test's native double.
        records = self.native_calls()
        owned = {row.get("pid") for row in records} | {row.get("child_pid") for row in records}
        rows = process_rows()
        targets = {pid for pid, _, _, command in rows if str(self.temp) in command}
        for pid, _, _, command in rows:
            if pid in owned and (str(HERE / "fake_codex.py") in command or str(self.temp) in command):
                targets.add(pid)
        for pid in targets:
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.temporary.cleanup()
