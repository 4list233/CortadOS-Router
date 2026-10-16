# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

import asyncio
from .support import WorkerCase, process_alive


class Cancellation(WorkerCase):
    async def test_ac10_cancel_removes_native_and_separate_session_descendant(self):
        self.configure_native(hang=True, descendant="session", ignore_term=True)
        async with self.session() as client:
            first = await self.call(client, "delegate", self.request())
            child = await self.wait_native("descendant")
            native = self.native_calls("exec")[0]
            await self.call(client, "job_cancel", {"job_id": first["job"]["job_id"]})
            terminal = await self.terminal(client, first["job"]["job_id"])
            self.assertEqual(terminal["job"]["state"], "cancelled")
            self.assertFalse(process_alive(child["child_pid"]), "containment: separate native tool session survived cancellation")
            self.assertFalse(process_alive(native["pid"]), "containment: native process survived cancellation")
            again = await self.call(client, "job_cancel", {"job_id": first["job"]["job_id"]})
            self.assertEqual(again["job"], terminal["job"])

    async def test_ac10_timeout_is_observed_terminal_and_releases_workspace(self):
        self.configure_native(hang=True)
        async with self.session() as client:
            first = await self.call(client, "delegate", self.request(timeout_s=1))
            native = await self.wait_native()
            result = await self.terminal(client, first["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "timed_out")
            self.assertFalse(process_alive(native["pid"]))
            self.configure_native()
            second = await self.call(client, "delegate", self.request())
            self.assertEqual((await self.terminal(client, second["job"]["job_id"]))["job"]["state"], "succeeded")

    async def test_ac10_cancellation_cannot_rewrite_already_succeeded_job(self):
        async with self.session() as client:
            first = await self.call(client, "delegate", self.request())
            result = await self.terminal(client, first["job"]["job_id"])
            cancelled = await self.call(client, "job_cancel", {"job_id": first["job"]["job_id"]})
            self.assertEqual(cancelled["job"], result["job"])

    async def test_ac10_recorded_cancellation_intent_beats_late_success(self):
        self.configure_native(hang=True)
        async with self.session() as client:
            first = await self.call(client, "delegate", self.request())
            await self.wait_native()
            await self.call(client, "job_cancel", {"job_id": first["job"]["job_id"]})
            (self.native_home / "release-worker").touch()
            value = await self.terminal(client, first["job"]["job_id"])
            self.assertEqual(value["job"]["state"], "cancelled")
