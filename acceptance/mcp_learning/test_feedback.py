# PURPOSE: Pin durable, attributed planner feedback independently of native completion.
# CONTRACT: RT-MCP-LEARNING-003 AC-L05; protected append-only and idempotency exam.
# DEPENDS: Public MCP transport, isolated SQLite fault injection, captured native fixture.
# CEILING: A passed check is a planner assertion, never independently verified truth.
import asyncio
import json
import os
import signal
import sqlite3
import uuid

from acceptance.mcp_worker.support import process_rows
from .support import WorkerCase, canonical, digest, feedback, jobs, rows


class Feedback(WorkerCase):
    async def completed(self, client):
        accepted = await self.call(client, "delegate", self.request())
        return await self.terminal(client, accepted["job"]["job_id"])

    async def test_l05_feedback_receipt_replay_correction_and_immutable_result(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            job_id = terminal["job"]["job_id"]
            before = jobs(self)
            arguments = feedback(job_id)
            receipt = await self.call(client, "job_feedback", arguments)
            self.assertEqual(set(receipt), {"ok", "feedback"})
            value = receipt["feedback"]
            self.assertEqual(set(value), {"job_id", "feedback_id", "seq", "created_at_ms", "verdict", "verification_status"})
            self.assertEqual(value["verification_status"], "planner_asserted")
            self.assertGreaterEqual(value["seq"], 1)
            self.assertGreaterEqual(value["created_at_ms"], 0)
            repeated = await self.call(client, "job_feedback", dict(reversed(list(arguments.items()))))
            self.assertEqual(repeated, receipt)
            await self.call(client, "job_feedback", dict(arguments, summary="A different claim"), error="idempotency_conflict")
            correction = feedback(job_id, verdict="rejected", checks=[{"kind": "command", "reference": "fixture failure log", "claim": "failed"}])
            second = await self.call(client, "job_feedback", correction)
            self.assertGreater(second["feedback"]["seq"], value["seq"])
            self.assertEqual(jobs(self), before, "planner assertions must not rewrite accepted/native evidence")
            stored = rows(self.state / "jobs.db", "SELECT payload,argument_hash FROM job_feedback ORDER BY seq")
            self.assertEqual([json.loads(row[0]) for row in stored], [arguments, correction])
            self.assertEqual(stored[0][1], digest(canonical(arguments)))
            self.assertEqual(await self.call(client, "job_result", {"job_id": job_id}), terminal)

    async def test_l05_terminal_and_ownership_checks_precede_feedback_insertion(self):
        self.configure_native(hang=True)
        async with self.session() as client:
            accepted = await self.call(client, "delegate", self.request())
            job_id = accepted["job"]["job_id"]
            await self.call(client, "job_feedback", feedback(job_id), error="job_not_terminal")
            await self.call(client, "job_feedback", feedback(str(uuid.uuid4())), error="job_not_found")
            await self.call(client, "job_cancel", {"job_id": job_id})
            await self.terminal(client, job_id)
            with sqlite3.connect(self.state / "jobs.db") as db:
                db.execute("UPDATE jobs SET client_id='other-client' WHERE job_id=?", (job_id,))
            await self.call(client, "job_feedback", feedback(job_id), error="job_not_found")
            self.assertEqual(rows(self.state / "jobs.db", "SELECT * FROM job_feedback"), [])

    async def test_l05_closed_schema_and_verdict_evidence_rules_refuse(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            job_id = terminal["job"]["job_id"]
            invalid = [feedback(job_id, checks=[]), feedback(job_id, verdict="rejected"),
                       feedback(job_id, checks=[{"kind": "review", "reference": "fixture", "claim": "unknown"}]),
                       feedback(job_id, checks=[{"kind": "review", "reference": "fixture", "claim": "passed", "verified": True}]),
                       feedback(job_id, verdict="verified"), feedback(job_id, feedback_id="invalid"),
                       feedback(job_id, extra=True), feedback(job_id, summary="λ" * 65536)]
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    await self.call(client, "job_feedback", arguments, error="invalid_request")
            result = await self.call(client, "job_feedback", feedback(job_id, verdict="inconclusive", checks=[]))
            self.assertEqual(result["feedback"]["verdict"], "inconclusive")
            self.assertEqual(len(rows(self.state / "jobs.db", "SELECT * FROM job_feedback")), 1)

    async def test_l05_feedback_rows_cannot_be_updated_or_deleted(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            await self.call(client, "job_feedback", feedback(terminal["job"]["job_id"]))
            with sqlite3.connect(self.state / "jobs.db") as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                for query in ("UPDATE job_feedback SET payload='{}'", "DELETE FROM job_feedback"):
                    with self.subTest(query=query), self.assertRaises(sqlite3.DatabaseError):
                        db.execute(query)
                db.rollback()
            self.assertEqual(len(rows(self.state / "jobs.db", "SELECT * FROM job_feedback")), 1)

    async def test_l05_insert_failure_has_one_refusal_no_receipt_and_retry_can_commit(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            arguments = feedback(terminal["job"]["job_id"], summary="PRIVATE_SUMMARY_SENTINEL")
            with sqlite3.connect(self.state / "jobs.db") as db:
                db.execute("CREATE TRIGGER exam_fail_feedback BEFORE INSERT ON job_feedback BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
            result = await self.call(client, "job_feedback", arguments, error="storage_error")
            self.assertNotIn("feedback", result)
            self.assertEqual(rows(self.state / "jobs.db", "SELECT * FROM job_feedback"), [])
            events = (self.state / "broker.jsonl").read_text()
            self.assertNotIn("PRIVATE_SUMMARY_SENTINEL", events)
            failures = [json.loads(line) for line in events.splitlines() if line.startswith("{")]
            self.assertEqual(sum(row.get("event") == "refusal" and row.get("reason") == "storage_error" for row in failures), 1)
            with sqlite3.connect(self.state / "jobs.db") as db:
                db.execute("DROP TRIGGER exam_fail_feedback")
            await self.call(client, "job_feedback", arguments)

    async def test_l05_broker_restart_replays_committed_feedback_receipt(self):
        async with self.session() as client:
            terminal = await self.completed(client)
            arguments = feedback(terminal["job"]["job_id"])
            receipt = await self.call(client, "job_feedback", arguments)
        owned = [pid for pid, _, _, command in process_rows() if "--broker" in command and str(self.config_path) in command]
        self.assertEqual(len(owned), 1)
        os.kill(owned[0], signal.SIGKILL)
        await asyncio.sleep(0.05)
        async with self.session() as client:
            self.assertEqual(await self.call(client, "job_feedback", arguments), receipt)
            self.assertEqual(len(rows(self.state / "jobs.db", "SELECT * FROM job_feedback")), 1)
