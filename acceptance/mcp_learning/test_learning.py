# PURPOSE: Prove exact, read-only learning snapshots and late-feedback revision semantics.
# CONTRACT: RT-MCP-LEARNING-003 AC-L06–09; independently authored before implementation.
# DEPENDS: Public MCP fixture jobs; standard SQLite transactions and filesystem fault injection.
# CEILING: Synthetic teacher assertions and fixtures are not hand-labelled routing quality.
import copy
import fcntl
import importlib
import json
import sqlite3
from unittest.mock import patch

from .support import WorkerCase, SECTION, canonical, digest, evidence, feedback, jobs, rows, scorer


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


class Collection(WorkerCase):
    def setUp(self):
        super().setUp()
        self.data = self.temp / "learning-data"
        self.data.mkdir(mode=0o700)

    def owner(self):
        return importlib.import_module("cortados_router.learning_data")

    def collect(self):
        return self.owner().collect_mcp(self.state, self.data)

    def observations(self):
        return read_rows(self.data / "mcp-observations.jsonl")

    def requests(self):
        return read_rows(self.data / "requests.jsonl")

    async def completed(self, client, **updates):
        accepted = await self.call(client, "delegate", self.request(**updates))
        return await self.terminal(client, accepted["job"]["job_id"])

    async def test_l06_completed_is_unreviewed_and_collection_only_opens_readonly_snapshots(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            before = jobs(self)
            real_connect, traces, opens = sqlite3.connect, [], []
            def audited_connect(database, *args, **kwargs):
                opens.append((str(database), kwargs))
                self.assertIn("mode=ro", str(database))
                self.assertNotIn("immutable=1", str(database))
                self.assertTrue(kwargs.get("uri"))
                connection = real_connect(database, *args, **kwargs)
                connection.set_trace_callback(traces.append)
                return connection
            with patch("sqlite3.connect", audited_connect):
                result = self.collect()
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["added"], 1)
            self.assertEqual(len(opens), 2, "collection opens one snapshot per source, not per job")
            self.assertGreaterEqual(sum(sql.strip().upper().startswith("BEGIN") for sql in traces), 2)
            self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")) for sql in traces))
            self.assertEqual(jobs(self), before)
            observed = self.observations()
            self.assertEqual(len(observed), 1, "joined advice must not duplicate accepted job")
            value = observed[0]
            self.assertEqual(value["job_id"], terminal["job"]["job_id"])
            self.assertEqual(value["native_outcome"], "completed")
            self.assertIsNone(value["planner_verdict"])
            self.assertEqual(value["feedback"], [])
            self.assertEqual(value["usage"], terminal["result"]["usage"])
            request = self.requests()[0]
            annotation = request["outcome_annotation"]
            self.assertEqual(annotation["verification_status"], "unreviewed")
            self.assertIsNone(annotation["summary"])
            self.assertEqual(annotation["checks"], [])
            self.assertEqual(annotation["feedback_ids"], [])
            self.assertNotIn("outcome_annotation", request["request"])
            self.assertNotIn(terminal["result"]["summary"], request["request"])

    async def test_l06_pending_and_orphan_routes_remain_explicit_observations(self):
        self.configure_native(hang=True)
        async with self.session() as client:
            advice = await self.call(client, "route", self.route_request())
            accepted = await self.call(client, "delegate", self.request())
            await self.wait_native()
            self.assertEqual(self.collect()["status"], "ok")
            observed = self.observations()
            pending = next(value for value in observed if value.get("job_id"))
            orphan = next(value for value in observed if not value.get("job_id"))
            self.assertEqual(pending["native_outcome"], "pending")
            self.assertIsNone(pending["elapsed_ms"])
            self.assertIsNone(pending["planner_verdict"])
            self.assertEqual(orphan["route_id"], advice["route"]["route_id"])
            self.assertFalse(orphan["eligibility"])
            self.assertEqual(orphan["exclusion_reason"], "no_accepted_job")
            old_id = pending["learning_id"]
            await self.call(client, "job_cancel", {"job_id": accepted["job"]["job_id"]})
            await self.terminal(client, accepted["job"]["job_id"])
            self.collect()
            current = next(value for value in self.observations() if value.get("job_id"))
            self.assertEqual(current["native_outcome"], "cancelled")
            self.assertIsNone(current["planner_verdict"])
            self.assertNotEqual(current["learning_id"], old_id)
            eligible = self.owner().eligible_rows(self.requests(), self.observations())
            self.assertEqual([row["id"] for row in eligible["rows"]], [current["learning_id"]])
            self.assertIn({"id": old_id, "reason": "superseded"}, eligible["excluded"])

    async def test_l07_late_feedback_appends_revision_preserves_prefix_and_selects_latest(self):
        async with self.session() as client:
            terminal = await self.completed(client, task_evidence=evidence())
            self.collect()
            old_requests = (self.data / "requests.jsonl").read_bytes()
            old_projection = (self.data / "mcp-observations.jsonl").read_bytes()
            first = self.requests()[0]
            self.assertEqual(self.collect()["added"], 0)
            self.assertEqual((self.data / "requests.jsonl").read_bytes(), old_requests)
            self.assertEqual((self.data / "mcp-observations.jsonl").read_bytes(), old_projection)
            review = feedback(terminal["job"]["job_id"])
            await self.call(client, "job_feedback", review)
            self.assertEqual(self.collect()["added"], 1)
            self.assertTrue((self.data / "requests.jsonl").read_bytes().startswith(old_requests))
            current = self.observations()[0]
            self.assertEqual(current["planner_verdict"], "accepted")
            self.assertEqual(current["feedback"][0]["feedback_id"], review["feedback_id"])
            second = next(row for row in self.requests() if row["id"] == current["learning_id"])
            self.assertEqual(second["group_key"], first["group_key"])
            self.assertEqual(second["request"], first["request"])
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(second["outcome_annotation"]["feedback_ids"], [review["feedback_id"]])
            self.assertEqual(first["outcome_annotation"]["verification_status"], "unreviewed")
            selected = self.owner().eligible_rows(self.requests(), self.observations())
            self.assertEqual([row["id"] for row in selected["rows"]], [second["id"]])
            material = {key: current[key] for key in ("job_id", "learning_text", "native_outcome", "reason", "worker", "usage", "elapsed_ms", "feedback")}
            self.assertEqual(current["observation_digest"], digest(canonical(material)))
            self.assertEqual(second["id"], "mcp." + current["job_id"] + "." + current["observation_digest"])

    async def test_l07_lock_corruption_and_missing_sources_keep_published_evidence(self):
        async with self.session() as client:
            await self.completed(client)
            self.collect()
            before = {name: (self.data / name).read_bytes() for name in ("requests.jsonl", "mcp-observations.jsonl")}
            with (self.data / "mcp-collection.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.collect()
            self.assertEqual(result["status"], "degraded")
            self.assertIn("mcp_collection_busy", [item["code"] for item in result["diagnostics"]])
            with sqlite3.connect(self.state / "jobs.db") as db:
                db.execute("UPDATE jobs SET record='not JSON'")
            result = self.collect()
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["added"], 0)
            self.assertIn("mcp_source_invalid", [item["code"] for item in result["diagnostics"]])
            result = self.owner().collect_mcp(self.temp / "absent-source", self.data)
            self.assertEqual(result["status"], "degraded")
            self.assertIn("mcp_source_unavailable", [item["code"] for item in result["diagnostics"]])
            self.assertFalse((self.temp / "absent-source").exists())
            for name, value in before.items():
                self.assertEqual((self.data / name).read_bytes(), value)

    async def test_l07_crash_between_publications_retains_old_snapshot_and_reuses_revision(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            self.collect()
            old_projection = (self.data / "mcp-observations.jsonl").read_bytes()
            await self.call(client, "job_feedback", feedback(terminal["job"]["job_id"]))
            import os
            replace = os.replace
            def fail_projection(source, destination, *args, **kwargs):
                if str(destination).endswith("mcp-observations.jsonl"):
                    raise OSError("synthetic crash before projection replace")
                return replace(source, destination, *args, **kwargs)
            with patch("os.replace", fail_projection):
                result = self.collect()
            self.assertEqual(result["status"], "degraded")
            self.assertEqual((self.data / "mcp-observations.jsonl").read_bytes(), old_projection)
            intermediate = self.requests()
            self.assertEqual(len(intermediate), 2)
            selected = self.owner().eligible_rows(intermediate, self.observations())
            self.assertEqual(len(selected["rows"]), 1)
            self.assertEqual(self.collect()["added"], 0)
            self.assertEqual(self.requests(), intermediate)
            self.assertEqual(self.observations()[0]["planner_verdict"], "accepted")

    async def test_l08_exact_s1_context_and_invalid_digest_or_explicit_oversize_exclusion(self):
        supplied = evidence()
        exact = " Task λ " + "\n\nCONTEXT:\n" + " Full context " + SECTION + canonical(supplied)
        with scorer(self):
            async with self.session() as client:
                terminal = await self.completed(client, task=" Task λ ", context=" Full context ", selection={"mode": "s1"}, task_evidence=supplied)
                self.collect()
                self.assertEqual(self.requests()[0]["request"], exact)
                self.assertEqual(self.requests()[0]["scoring_digest"], digest(exact))
                route_id = terminal["job"]["route_id"]
                with sqlite3.connect(self.state / "router/router.db") as db:
                    context = json.loads(db.execute("SELECT context FROM routes WHERE turn_id=?", (route_id,)).fetchone()[0])
                    context["scoring_digest"] = "0" * 64
                    db.execute("UPDATE routes SET context=? WHERE turn_id=?", (json.dumps(context), route_id))
                result = self.collect()
                observation = self.observations()[0]
                self.assertFalse(observation["eligibility"])
                self.assertEqual(observation["exclusion_reason"], "scoring_digest_mismatch")
                self.assertIn("scoring_digest_mismatch", [item["code"] for item in result["diagnostics"]])
                await self.completed(client, task="x" * 6001)
                self.collect()
                self.assertIn("scoring_context_too_large", [row["exclusion_reason"] for row in self.observations()])

    async def test_l09_retries_share_group_and_legacy_duplicate_partition_conflicts_quarantine(self):
        async with self.session() as client:
            supplied = evidence()
            await self.completed(client, task="Repair distinct synthetic parser", task_evidence=supplied)
            await self.completed(client, task="Repair distinct synthetic parser", context="Revised context", task_evidence=supplied)
            self.collect()
            requests, observations = self.requests(), self.observations()
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["group_key"], requests[1]["group_key"])
            original = copy.deepcopy((requests, observations))
            selected = self.owner().eligible_rows(requests, observations)
            self.assertEqual(len(set(selected["partitions"].values())), 1)
            partition = next(iter(selected["partitions"].values()))
            legacy = {"id": "legacy-same-task", "source": "hermes-discord", "unit_kind": "interactive",
                      "request": "Repair distinct synthetic parser", "split": "train" if partition == "holdout" else "holdout"}
            selected = self.owner().eligible_rows(requests + [legacy], observations)
            self.assertEqual(selected["rows"], [])
            self.assertEqual(selected["partitions"], {})
            self.assertEqual(selected["excluded"], [{"id": key, "reason": "split_conflict"} for key in sorted([legacy["id"], *(row["id"] for row in requests)])])
            self.assertEqual((requests, observations), original, "eligibility must not mutate evidence")
