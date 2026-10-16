# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

import asyncio
from .support import WorkerCase


class Admission(WorkerCase):
    async def test_ac04_api_login_refuses_without_native_turn(self):
        self.configure_native(auth="api")
        async with self.session() as client:
            await self.call(client, "delegate", self.request(), error="subscription_required")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac04_unknown_and_spent_quota_refuse_both_modes(self):
        async with self.session() as client:
            for quota, code in (("unknown", "quota_unknown"), ("spent", "quota_spent"), ("missing_permission", "quota_unknown")):
                self.configure_native(quota=quota)
                for selection in ({"mode": "explicit", "model": "gpt-6-luna"}, {"mode": "s1", "routing_brief": "Fixture task"}):
                    with self.subTest(quota=quota, selection=selection):
                        await self.call(client, "delegate", self.request(selection=selection), error=code)
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac04_missing_native_model_and_empty_allowlist_refuse(self):
        self.configure_native(models=["gpt-6-astra"])
        async with self.session() as client:
            await self.call(client, "delegate", self.request(), error="model_unavailable")
            await self.call(client, "delegate", self.request(constraints={"allowed_models": []}), error="model_unavailable")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac04_delegate_rechecks_quota_after_route_advice(self):
        async with self.session() as client:
            await self.call(client, "route", self.route_request())
            self.configure_native(quota="spent")
            await asyncio.sleep(1.05)  # Exceed configured 1 s metadata age; stale advice cannot authorize launch.
            await self.call(client, "delegate", self.request(), error="quota_spent")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac04_model_inventory_follows_pages_and_rejects_cursor_cycles(self):
        self.configure_native(pagination="two_pages")
        async with self.session() as client:
            await self.call(client, "route", self.route_request())
            requests = [row for row in self.native_calls("metadata") if row["method"] == "model/list"]
            self.assertTrue(any((row["params"] or {}).get("cursor") == "second-page" for row in requests))
            self.configure_native(pagination="loop")
            await asyncio.sleep(1.05)
            await self.call(client, "route", self.route_request(), error="model_unavailable")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac04_invalid_quota_geometry_never_becomes_neutral_capacity(self):
        self.configure_native(quota="malformed")
        async with self.session() as client:
            await self.call(client, "delegate", self.request(), error="quota_unknown")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_ac07_managed_policy_preflight_refuses_before_worker(self):
        self.configure_native(policy="enterpriseManaged")
        with self.assertRaises(Exception):
            async with self.session():
                pass
        self.assertIn("permission_refused", self.stderr_path.read_text())
        self.assertEqual(self.native_calls("exec"), [])

    async def test_ac07_requirements_preflight_refuses_before_worker(self):
        self.configure_native(requirements=True)
        with self.assertRaises(Exception):
            async with self.session():
                pass
        self.assertIn("permission_refused", self.stderr_path.read_text())
        self.assertEqual(self.native_calls("exec"), [])
