"""PURPOSE: Pin exact native profile admission and truthful terminal provenance.
CONTRACT: RT-MCP-NATIVE-002; original MCP exams remain byte-identical.
DEPENDS: official MCP public client, protected synthetic double, real native captures.
CEILING: Version orchestration faults do not substitute for real 0.163.0 captures.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import unittest

from acceptance.mcp_worker.support import WorkerCase
from acceptance.mcp_worker import test_recovery as recovery_exam

HERE = Path(__file__).resolve().parent


class NativeCaptureEvidence(unittest.TestCase):
    def test_n05_real_profile_corpus_keeps_per_model_boundary_ground_truth(self):
        capture = HERE / "fixtures/0.163.0"
        for line in (capture / "CAPTURE-SHA256SUMS").read_text().splitlines():
            expected, relative = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((capture / relative).read_bytes()).hexdigest(), expected, relative)
        for model in ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"):
            for kind in ("command-permissions", "connector", "handoff-boundary", "cancellation"):
                with self.subTest(model=model, kind=kind):
                    directory = capture / kind / model
                    meta = json.loads((directory / "meta.json").read_text())
                    self.assertEqual(meta["cli_version"], "0.163.0")
                    self.assertEqual(meta["model"], model)
                    for relative, expected in meta["hashes"].items():
                        self.assertEqual(hashlib.sha256((directory / relative).read_bytes()).hexdigest(), expected)
                    events = [json.loads(line) for line in (directory / "stdout.jsonl").read_text().splitlines()]
                    if kind == "cancellation":
                        self.assertIsNone(meta["child_after"])
                        self.assertNotEqual(meta["native_exit_code"], 0)
                        self.assertNotEqual(meta["child_identity"]["pgid"], meta["native_identity"]["pgid"])
                        continue
                    completed = [event for event in events if event["type"] == "turn.completed"]
                    self.assertEqual(len(completed), 1)
                    self.assertGreater(completed[0]["usage"]["input_tokens"], 0)
                    items = [event["item"] for event in events if event["type"] == "item.completed"]
                    final = (directory / "final.txt").read_text()
                    self.assertEqual([item["text"] for item in items if item["type"] == "agent_message"][-1], final)
                    if kind == "command-permissions":
                        command = [item for item in items if item["type"] == "command_execution"][-1]
                        self.assertEqual(command["exit_code"], 0)
                        self.assertEqual(json.loads(command["aggregated_output"]), {"outside": "DENIED", "network": "DENIED"})
                    elif kind == "connector":
                        calls = [item for item in items if item["type"] == "mcp_tool_call"]
                        self.assertEqual([item["tool"] for item in calls], ["visible_marker"])
                        self.assertEqual(final, "GRANTED_TOOL_REACHED")
                    else:
                        self.assertEqual(len((directory / "handoff.txt").read_bytes()), 65536)
                        self.assertEqual(final, "FIRST_SENTINEL_9df3 LAST_SENTINEL_a712")
        proxy = json.loads((capture / "proxy-denials/result.json").read_text())
        self.assertEqual(proxy["tools"], ["visible_marker"])
        self.assertTrue(proxy["hidden_tool"]["returned"]["isError"])
        self.assertEqual(proxy["resources"]["returned"]["resources"], [])
        self.assertEqual(proxy["prompts"]["returned"]["prompts"], [])
        self.assertIn("permission_refused", proxy["read_resource"]["message"])
        self.assertIn("permission_refused", proxy["get_prompt"]["message"])


class NativeProfile(WorkerCase):
    broker_and_watchdog = recovery_exam.Recovery.broker_and_watchdog
    wait_dead = recovery_exam.Recovery.wait_dead

    def setUp(self):
        super().setUp()
        self.config["codex_bin"] = str(HERE / "profile_codex.py")
        self.write_config()

    async def test_n01_both_captured_profiles_report_observed_success_version(self):
        async with self.session() as client:
            for version in ("0.162.1", "0.163.0"):
                with self.subTest(version=version):
                    self.configure_native(version=version)
                    # Exceed this exam's one-second metadata age to force a fresh
                    # observation; less would intentionally test the cached version.
                    await asyncio.sleep(1.05)
                    started = await self.call(client, "delegate", self.request())
                    result = await self.terminal(client, started["job"]["job_id"])
                    self.assertEqual(result["job"]["state"], "succeeded")
                    self.assertEqual(result["result"]["worker"]["cli_version"], version)

    async def test_n02_native_failure_retains_observed_version(self):
        self.configure_native(version="0.163.0", behavior="nonzero")
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "failed")
            self.assertEqual(result["result"]["error"]["code"], "native_failed")
            self.assertEqual(result["result"]["worker"]["cli_version"], "0.163.0")
            self.assertIsNone(result["result"]["summary"])

    async def test_n02_cancellation_retains_observed_version(self):
        self.configure_native(version="0.163.0", hang=True)
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            await self.wait_native()
            await self.call(client, "job_cancel", {"job_id": started["job"]["job_id"]})
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "cancelled")
            self.assertEqual(result["result"]["worker"]["cli_version"], "0.163.0")

    async def test_n02_timeout_retains_observed_version(self):
        self.configure_native(version="0.163.0", hang=True)
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request(timeout_s=1))
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "timed_out")
            self.assertEqual(result["result"]["worker"]["cli_version"], "0.163.0")

    async def interrupted(self, version, *, legacy=False):
        self.configure_native(version=version, hang=True)
        request = self.request()
        async with self.session() as client:
            started = await self.call(client, "delegate", request)
            native = await self.wait_native()
            broker, watchdog = self.broker_and_watchdog(native)
            os.kill(broker, signal.SIGKILL)
            await self.wait_dead(native["pid"], watchdog)
        if legacy:
            # This is the old schema-1 shape produced only by the accepted 0.162.1
            # implementation. Remove exactly the new provenance field, no state.
            with sqlite3.connect(self.state / "jobs.db") as db:
                record = json.loads(db.execute("SELECT record FROM jobs WHERE job_id=?", (started["job"]["job_id"],)).fetchone()[0])
                record.pop("cli_version", None)
                db.execute("UPDATE jobs SET record=? WHERE job_id=?", (json.dumps(record), started["job"]["job_id"]))
        self.configure_native(version="0.162.1")
        async with self.session() as client:
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "interrupted")
            self.assertEqual(result["result"]["worker"]["cli_version"], version)
            replay = await self.call(client, "delegate", request)
            self.assertEqual(replay["job"], result["job"])
            self.assertEqual(len(self.native_calls("exec")), 1)

    async def test_n03_recovery_uses_job_version_after_native_binary_change(self):
        await self.interrupted("0.163.0")

    async def test_n03_old_schema_one_record_keeps_historical_profile(self):
        await self.interrupted("0.162.1", legacy=True)

    async def test_n04_unknown_profile_refuses_before_execution(self):
        self.configure_native(version="0.164.0")
        async with self.session() as client:
            await self.call(client, "delegate", self.request(), error="native_version_mismatch")
            self.assertEqual(self.native_calls("exec"), [])

    async def test_n04_accepted_profile_change_before_start_refuses_attempt(self):
        # One hour is an exam cache horizon, not a production default. It prevents
        # metadata expiry from shifting the third (pre-exec) version observation.
        # A shorter interval makes a loaded test nondeterministic; a larger one
        # adds no coverage because the test process lasts only seconds.
        self.config["limits"]["metadata_max_age_s"] = 3600
        self.write_config()
        self.configure_native(version_sequence=["0.162.1", "0.162.1", "0.163.0"])
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "failed")
            self.assertEqual(result["result"]["error"]["code"], "native_version_mismatch")
            self.assertEqual(result["result"]["worker"]["cli_version"], "0.162.1")
            self.assertEqual(self.native_calls("exec"), [], "N04: changed binary executed under another admitted profile")
