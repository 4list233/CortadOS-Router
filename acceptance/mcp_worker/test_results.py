# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

from .support import WorkerCase


class Results(WorkerCase):
    async def test_ac08_observed_usage_final_artifacts_and_review_status(self):
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            value = await self.terminal(client, started["job"]["job_id"])
            result = value["result"]
            self.assertEqual(result["usage"], {"input_tokens": 38143, "cached_input_tokens": 29184, "cache_write_input_tokens": 0, "output_tokens": 56, "reasoning_output_tokens": 0})
            self.assertEqual(result["verification"], "planner_review_required")
            self.assertEqual(result["changed_paths"], None, "native capture makes no file_change claim")
            self.assertEqual((self.state / result["artifacts"]["final_message"]).read_text(), "MCP_WORKER_CAPTURE_OK")
            self.assertIn('"type":"turn.completed"', (self.state / result["artifacts"]["events"]).read_text().replace(" ", ""))
            again = await self.call(client, "job_result", {"job_id": started["job"]["job_id"]})
            self.assertEqual(again, value, "terminal result and elapsed time must be immutable")

    async def test_ac08_missing_usage_remains_unknown(self):
        self.configure_native(behavior="missing_usage")
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            value = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(value["job"]["state"], "succeeded")
            self.assertTrue(all(count is None for count in value["result"]["usage"].values()))

    async def test_ac08_unknown_native_events_retained_without_fabricated_meaning(self):
        self.configure_native(behavior="unknown_event")
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            value = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(value["job"]["state"], "succeeded")
            evidence = (self.state / value["result"]["artifacts"]["events"]).read_text()
            self.assertIn("future.inert_event", evidence)
            self.assertIn("native evidence", evidence)

    async def test_ac08_output_limit_contains_worker_and_never_returns_truncated_success(self):
        self.configure_native(behavior="excess_output", output_bytes=self.config["limits"]["max_output_bytes"] + 1)
        async with self.session() as client:
            job = await self.call(client, "delegate", self.request())
            result = await self.terminal(client, job["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "failed")
            self.assertEqual(result["result"]["error"]["code"], "output_limit")
            self.assertIsNone(result["result"]["summary"])
            self.assertTrue(all(value is None for value in result["result"]["usage"].values()))
