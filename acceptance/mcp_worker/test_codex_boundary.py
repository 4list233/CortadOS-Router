# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

from .support import WorkerCase


class CodexBoundary(WorkerCase):
    async def test_ac02_each_captured_model_reaches_real_terminal_shape(self):
        async with self.session() as client:
            for model in ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"):
                with self.subTest(model=model):
                    started = await self.call(client, "delegate", self.request(selection={"mode": "explicit", "model": model}))
                    result = await self.terminal(client, started["job"]["job_id"])
                    self.assertEqual(result["job"]["state"], "succeeded")
                    self.assertEqual(result["result"]["summary"], "MCP_WORKER_CAPTURE_OK")
                    self.assertEqual(result["result"]["worker"]["requested_model"], model)
                    self.assertEqual(result["result"]["worker"]["reported_model"], None)

    async def test_ac02_unaccepted_native_version_never_starts_worker(self):
        self.configure_native(version="0.0.0")
        async with self.session() as client:
            await self.call(client, "delegate", self.request(), error="native_version_mismatch")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac02_corrupt_missing_and_conflicting_terminal_are_not_success(self):
        async with self.session() as client:
            for behavior in ("malformed", "no_terminal", "duplicate_terminal", "no_final", "mismatched_final"):
                self.configure_native(behavior=behavior)
                with self.subTest(behavior=behavior):
                    started = await self.call(client, "delegate", self.request())
                    result = await self.terminal(client, started["job"]["job_id"])
                    self.assertEqual(result["job"]["state"], "failed")
                    self.assertEqual(result["result"]["error"]["code"], "native_protocol_error")

    async def test_ac02_nonzero_exit_cannot_be_completed_by_success_shaped_output(self):
        self.configure_native(behavior="nonzero")
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "failed")
            self.assertEqual(result["result"]["error"]["code"], "native_failed")
