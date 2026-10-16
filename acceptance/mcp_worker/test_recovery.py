# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Synthetic faults do not replace real native boundary evidence.

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import time
from .support import WorkerCase, process_alive, process_rows, TEST_WAIT_S


class Recovery(WorkerCase):
    def broker_and_watchdog(self, native):
        rows = {pid: (parent, command) for pid, parent, _, command in process_rows()}
        watchdog = native["ppid"]
        self.assertIn(watchdog, rows, "lifecycle: native process needs a live watchdog")
        broker = rows[watchdog][0]
        self.assertIn(broker, rows)
        self.assertIn(str(self.temp), rows[broker][1], "fixture refuses to signal a process not bound to its private state")
        return broker, watchdog

    async def wait_dead(self, *pids):
        end = time.monotonic() + TEST_WAIT_S
        while time.monotonic() < end and any(process_alive(pid) for pid in pids):
            await asyncio.sleep(0.03)
        self.assertFalse(any(process_alive(pid) for pid in pids), "containment: owned process survived")

    async def test_ac06_frontend_disconnect_preserves_running_job_and_identity(self):
        self.configure_native(hang=True)
        request = self.request()
        async with self.session() as client:
            first = await self.call(client, "delegate", request)
            native = await self.wait_native()
        self.assertTrue(process_alive(native["pid"]), "frontend: disconnect killed accepted worker")
        async with self.session() as client:
            repeated = await self.call(client, "delegate", request)
            self.assertEqual(repeated["job"]["job_id"], first["job"]["job_id"])
            (self.native_home / "release-worker").touch()
            result = await self.terminal(client, first["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "succeeded")
            self.assertEqual(len(self.native_calls("exec")), 1)

    async def test_ac06_broker_crash_contains_descendants_then_interrupts_without_retry(self):
        self.configure_native(hang=True, descendant="session", ignore_term=True)
        request = self.request()
        async with self.session() as client:
            first = await self.call(client, "delegate", request)
            child = await self.wait_native("descendant")
            native = self.native_calls("exec")[0]
            broker, watchdog = self.broker_and_watchdog(native)
            os.kill(broker, signal.SIGKILL)
            await self.wait_dead(native["pid"], child["child_pid"], watchdog)
        async with self.session() as client:
            result = await self.terminal(client, first["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "interrupted")
            self.assertIsNone(result["result"]["summary"])
            replay = await self.call(client, "delegate", request)
            self.assertEqual(replay["job"], result["job"])
            self.assertEqual(len(self.native_calls("exec")), 1)

    async def test_ac06_unproven_termination_keeps_claim_and_never_restarts_worker(self):
        self.configure_native(hang=True)
        request = self.request()
        async with self.session() as client:
            first = await self.call(client, "delegate", request)
            native = await self.wait_native()
            broker, watchdog = self.broker_and_watchdog(native)
            os.kill(watchdog, signal.SIGSTOP)
            os.kill(broker, signal.SIGKILL)
            os.kill(watchdog, signal.SIGKILL)
            receipt = self.state / "jobs" / first["job"]["job_id"] / "termination.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({"contained": True}))
        async with self.session() as client:
            status = await self.call(client, "job_status", {"job_id": first["job"]["job_id"]})
            self.assertEqual(status["job"]["reason"], "containment_failed")
            self.assertNotIn(status["job"]["state"], {"succeeded", "failed", "cancelled", "timed_out", "interrupted"})
            await self.call(client, "delegate", self.request(), error="workspace_busy")
            self.assertEqual(len(self.native_calls("exec")), 1)
            self.assertTrue(process_alive(native["pid"]), "invalid receipt must not authorize a signal")

    async def test_ac06_corrupt_store_refuses_without_replacing_bytes(self):
        store = self.state / "jobs.db"
        store.write_bytes(b"not a sqlite database: preserved forensic evidence\x00")
        digest = hashlib.sha256(store.read_bytes()).hexdigest()
        with self.assertRaises(Exception):
            async with self.session():
                pass
        self.assertEqual(hashlib.sha256(store.read_bytes()).hexdigest(), digest)
        self.assertIn("storage_error", self.stderr_path.read_text())
        self.assertEqual(self.native_calls("exec"), [])

    async def test_ac06_stopped_backup_restore_preserves_terminal_identity_and_artifacts(self):
        self.configure_native(hang=True)
        request = self.request()
        async with self.session() as client:
            first = await self.call(client, "delegate", request)
            native = await self.wait_native()
            broker, _ = self.broker_and_watchdog(native)
            (self.native_home / "release-worker").touch()
            original = await self.terminal(client, first["job"]["job_id"])
        os.kill(broker, signal.SIGTERM)
        await self.wait_dead(broker)
        backup = self.temp / "backup"
        backup.mkdir()
        with sqlite3.connect(self.state / "jobs.db") as source, sqlite3.connect(backup / "jobs.db") as target:
            source.backup(target)
        shutil.copytree(self.state / "jobs", backup / "jobs")
        for suffix in ("", "-wal", "-shm"):
            (self.state / ("jobs.db" + suffix)).unlink(missing_ok=True)
        shutil.rmtree(self.state / "jobs")
        shutil.copy2(backup / "jobs.db", self.state / "jobs.db")
        shutil.copytree(backup / "jobs", self.state / "jobs")
        async with self.session() as client:
            replay = await self.call(client, "delegate", request)
            restored = await self.call(client, "job_result", {"job_id": replay["job"]["job_id"]})
            self.assertEqual(restored, original)
            self.assertEqual(len(self.native_calls("exec")), 1)
            self.assertEqual((self.state / restored["result"]["artifacts"]["final_message"]).read_text(), "MCP_WORKER_CAPTURE_OK")
