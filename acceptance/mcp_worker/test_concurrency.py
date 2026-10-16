# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Does not claim real provider permissions or task correctness.

from .support import WorkerCase


class Concurrency(WorkerCase):
    async def test_ac09_overlapping_and_symlink_workspaces_refuse(self):
        self.configure_native(hang=True)
        nested = self.workspace / "nested"
        nested.mkdir()
        alias = self.temp / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        async with self.session() as client:
            first = await self.call(client, "delegate", self.request())
            await self.wait_native()
            for path in (nested, alias, self.temp):
                await self.call(client, "delegate", self.request(cwd=str(path)), error="workspace_busy")
            self.assertEqual(len(self.native_calls("exec")), 1)
            await self.call(client, "job_cancel", {"job_id": first["job"]["job_id"]})
            await self.terminal(client, first["job"]["job_id"])

    async def test_ac09_disjoint_jobs_run_until_capacity_is_exhausted(self):
        self.configure_native(hang=True)
        other = self.temp / "other"
        third = self.temp / "third"
        other.mkdir()
        third.mkdir()
        async with self.session() as client:
            one = await self.call(client, "delegate", self.request())
            two = await self.call(client, "delegate", self.request(cwd=str(other)))
            await self.call(client, "delegate", self.request(cwd=str(third)), error="capacity_exhausted")
            (self.native_home / "release-worker").touch()
            await self.terminal(client, one["job"]["job_id"])
            await self.terminal(client, two["job"]["job_id"])
            self.assertEqual(len(self.native_calls("exec")), 2)
