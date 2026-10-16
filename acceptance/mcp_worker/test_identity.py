# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

import asyncio
import copy
from .support import WorkerCase


class Identity(WorkerCase):
    async def test_ac05_concurrent_identical_request_has_one_job_and_one_effect(self):
        self.configure_native(hang=True)
        arguments = self.request()
        async with self.session() as client:
            first, second = await asyncio.gather(self.call(client, "delegate", arguments), self.call(client, "delegate", copy.deepcopy(arguments)))
            self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
            await self.wait_native()
            self.assertEqual(len(self.native_calls("exec")), 1)
            (self.native_home / "release-worker").touch()
            await self.terminal(client, first["job"]["job_id"])
            self.assertEqual((self.workspace / "worker-effect.txt").read_text().splitlines(), ["one accepted native attempt"])

    async def test_ac05_conflicting_arguments_do_not_create_second_effect(self):
        self.configure_native(hang=True)
        arguments = self.request()
        async with self.session() as client:
            first = await self.call(client, "delegate", arguments)
            changed = dict(arguments, context=arguments["context"] + " changed")
            await self.call(client, "delegate", changed, error="idempotency_conflict")
            await self.wait_native()
            self.assertEqual(len(self.native_calls("exec")), 1)
            await self.call(client, "job_cancel", {"job_id": first["job"]["job_id"]})
            await self.terminal(client, first["job"]["job_id"])

    async def test_ac05_terminal_identity_replay_keeps_original_result(self):
        arguments = self.request()
        async with self.session() as client:
            first = await self.call(client, "delegate", arguments)
            finished = await self.terminal(client, first["job"]["job_id"])
            replay = await self.call(client, "delegate", dict(reversed(list(arguments.items()))))
            self.assertEqual(replay["job"], finished["job"])
            self.assertEqual(len(self.native_calls("exec")), 1)

    async def test_ac05_default_insertion_and_realpath_have_one_identity(self):
        request = self.request()
        alias = self.temp / "workspace-alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        async with self.session() as client:
            first = await self.call(client, "delegate", request)
            await self.terminal(client, first["job"]["job_id"])
            repeated = dict(request, cwd=str(alias), constraints={}, account_ref="native-default",
                            permissions={"network": False, "mcp_servers": []})
            again = await self.call(client, "delegate", repeated)
            self.assertEqual(again["job"]["job_id"], first["job"]["job_id"])
            self.assertEqual(len(self.native_calls("exec")), 1)

    async def test_ac05_invalid_input_does_not_claim_request_id(self):
        request = self.request()
        async with self.session() as client:
            await self.call(client, "delegate", dict(request, unknown_field=True), error="invalid_request")
            first = await self.call(client, "delegate", request)
            result = await self.terminal(client, first["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "succeeded")
            self.assertEqual(len(self.native_calls("exec")), 1)
