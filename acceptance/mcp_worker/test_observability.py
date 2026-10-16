# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Synthetic faults do not replace real native boundary evidence.

import json
from .support import WorkerCase


class Observability(WorkerCase):
    async def test_ac11_each_transition_and_refusal_is_logged_once_without_private_payload(self):
        async with self.session() as client:
            first = await self.call(client, "delegate", self.request(task="PRIVATE_TASK_SENTINEL", context="PRIVATE_CONTEXT_SENTINEL"))
            result = await self.terminal(client, first["job"]["job_id"])
            for _ in range(4):
                await self.call(client, "job_status", {"job_id": first["job"]["job_id"]})
            await self.call(client, "delegate", self.request(unrecognized="PRIVATE_REFUSAL_SENTINEL"), error="invalid_request")
        broker = self.state / "broker.jsonl"
        self.assertTrue(broker.is_file(), "observability: detached broker stderr evidence is missing")
        text = broker.read_text() + self.stderr_path.read_text()
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        fields = {"schema", "at_ms", "event", "client_id", "request_id", "route_id", "job_id", "from_state", "state", "reason"}
        for event in events:
            self.assertEqual(set(event), fields)
            self.assertEqual(event["schema"], "cortados.mcp-worker.event.v1")
        transitions = [event for event in events if event["event"] == "state_transition" and event["job_id"] == first["job"]["job_id"]]
        self.assertEqual([event["state"] for event in transitions], ["queued", "running", "succeeded"])
        self.assertEqual([event["from_state"] for event in transitions], [None, "queued", "running"])
        self.assertEqual(len([event for event in events if event["event"] == "refusal" and event["reason"] == "invalid_request"]), 1)
        for secret in ("PRIVATE_TASK_SENTINEL", "PRIVATE_CONTEXT_SENTINEL", "PRIVATE_REFUSAL_SENTINEL", "FORBIDDEN_ACCEPTANCE_API_KEY", "FORBIDDEN_ACCEPTANCE_ANTHROPIC_KEY"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["job"]["state"], "succeeded")
