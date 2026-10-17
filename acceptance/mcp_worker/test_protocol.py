# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

import uuid
from .support import WorkerCase


class Protocol(WorkerCase):
    async def test_ac01_exact_seven_tools_and_typed_route(self):
        async with self.session() as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools.tools}, {"route", "delegate", "job_status", "job_result", "job_cancel", "context_requirements", "job_feedback"})
            value = await self.call(client, "route", self.route_request())
            route = value["route"]
            self.assertEqual(uuid.UUID(route["route_id"]).version, 4)
            self.assertEqual(route["chosen"]["provider"], "codex-cli")
            self.assertEqual(route["chosen"]["account_ref"], "native-default")
            self.assertEqual(self.native_calls("exec"), [], "route advice must not execute")

    async def test_ac01_unknown_nested_and_top_level_fields_refuse(self):
        bad = [self.route_request(typo=True), self.route_request(selection={"mode": "explicit", "model": "gpt-6-luna", "tier": "small"}),
               self.route_request(permissions={"network": False, "mcp_servers": [], "full_access": True})]
        async with self.session() as client:
            for arguments in bad:
                with self.subTest(arguments=arguments):
                    await self.call(client, "route", arguments, error="invalid_request")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac01_unknown_job_is_typed_refusal(self):
        async with self.session() as client:
            for tool in ("job_status", "job_result", "job_cancel"):
                await self.call(client, tool, {"job_id": str(uuid.uuid4())}, error="job_not_found")

    async def test_ac01_boolean_timeout_and_non_uuid_identity_refuse(self):
        async with self.session() as client:
            for arguments in (self.request(timeout_s=True), self.request(request_id="not-a-uuid"), self.request(task="  ")):
                await self.call(client, "delegate", arguments, error="invalid_request")
            self.assertEqual(self.native_calls("exec"), [])
