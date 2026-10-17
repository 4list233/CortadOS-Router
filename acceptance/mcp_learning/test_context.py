# PURPOSE: Pin planner context dialogue, exact projection and legacy compatibility.
# CONTRACT: RT-MCP-LEARNING-003 AC-L01–04; independent protected exam.
# DEPENDS: Public official MCP client, synthetic HTTP scorer and captured native fixture.
# CEILING: Structural readiness does not claim semantic context sufficiency.
import copy
import json

from .support import WorkerCase, FIELDS, SECTION, canonical, digest, evidence, jobs, legacy_handoff, route_context, scorer


class Context(WorkerCase):
    async def test_l01_legacy_normalization_handoff_and_replay_remain_exact(self):
        request = self.request(task=" Fix café.\n", context=" reference λ ")
        async with self.session() as client:
            first = await self.call(client, "delegate", request)
            finished = await self.terminal(client, first["job"]["job_id"])
            expected = dict(request, constraints={}, permissions={"network": False, "mcp_servers": []}, account_ref="native-default")
            _, raw, record = jobs(self)[0]
            expected_canonical = {k: v for k, v in expected.items() if k != "request_id"}
            self.assertEqual(json.loads(raw), expected_canonical, "legacy canonical bytes must not gain optional defaults")
            self.assertEqual(json.loads(record)["arguments"], expected, "legacy normalized arguments must not gain optional defaults")
            self.assertEqual(json.loads(record)["argument_hash"], digest(canonical(expected_canonical)))
            self.assertEqual(self.native_calls("exec")[0]["prompt"], legacy_handoff(request["task"], request["context"]))
        async with self.session() as client:
            replay = await self.call(client, "delegate", request)
            self.assertEqual(replay["job"], finished["job"])
            self.assertEqual(len(self.native_calls("exec")), 1)

    async def test_l02_scoring_brief_cannot_hide_ordered_evidence_or_reference_boundary(self):
        supplied = evidence(components=["z.py", "a.py"])
        request = self.request(task="Full task", context="Full λ context", selection={"mode": "s1", "routing_brief": " Exact brief "}, task_evidence=supplied)
        expected = " Exact brief " + SECTION + canonical(supplied)
        with scorer(self) as calls:
            async with self.session() as client:
                result = await self.call(client, "delegate", request)
                await self.terminal(client, result["job"]["job_id"])
                self.assertEqual(calls[0]["request"], expected)
                self.assertEqual(self.native_calls("exec")[0]["prompt"], legacy_handoff(request["task"], request["context"]) + SECTION + canonical(supplied))
                stored = json.loads(jobs(self)[0][2])
                self.assertEqual(stored["arguments"]["task_evidence"], supplied)
                context = route_context(self, stored["route_id"])
                self.assertEqual(context["scoring_text"], expected)
                self.assertEqual(context["scoring_digest"], digest(expected))
                self.assertEqual(context["task_evidence"], supplied)
                self.assertEqual(context["task_group_id"], supplied["task_group_id"])
                self.assertEqual(context["projection_version"], 1)

    async def test_l02_closed_evidence_and_complete_budget_refuse_before_scoring(self):
        bad = [evidence(unknown=True), evidence(components=["same", "same"]), evidence(references=[{"source": "fixture", "text": "text", "permission": True}]), evidence(task_group_id=12)]
        with scorer(self) as calls:
            async with self.session() as client:
                for value in bad:
                    with self.subTest(value=value):
                        await self.call(client, "delegate", self.request(task_evidence=value), error="invalid_request")
                value = evidence()
                prefix = "λ" * (6000 - len(SECTION + canonical(value)))
                route = await self.call(client, "route", self.route_request(selection={"mode": "s1", "routing_brief": prefix}, task_evidence=value))
                self.assertEqual(route["route"]["context"]["scorer_chars"], 6000)
                self.assertEqual(calls[-1]["request"], prefix + SECTION + canonical(value))
                before = len(calls)
                await self.call(client, "delegate", self.request(selection={"mode": "s1", "routing_brief": prefix + "λ"}, task_evidence=value), error="scoring_context_too_large")
                self.assertEqual(len(calls), before)
                self.assertEqual(jobs(self), [])
                self.assertEqual(self.native_calls("exec"), [])

    async def test_l03_context_dialogue_is_pure_and_missing_is_distinct_from_none(self):
        with scorer(self) as calls:
            async with self.session() as client:
                before_native = self.native_calls()
                before_jobs = jobs(self)
                request = {"task": "Fix parser", "selection": {"mode": "s1"}}
                report = (await self.call(client, "context_requirements", request))["context_requirements"]
                self.assertEqual(report["status"], "needs_context")
                self.assertEqual(report["missing_fields"], ["task_group_id", *FIELDS])
                partial = evidence()
                del partial["dependencies"]
                partial["verification"] = []
                report = (await self.call(client, "context_requirements", dict(request, task_evidence=partial)))["context_requirements"]
                self.assertEqual(report["missing_fields"], ["dependencies", "verification"])
                self.assertEqual(report["questions"], [{"field": "dependencies", "prompt": "Provide dependencies; use [] only when explicitly none."}, {"field": "verification", "prompt": "Provide at least one observable verification check."}])
                complete = evidence()
                report = (await self.call(client, "context_requirements", dict(request, task_evidence=complete)))["context_requirements"]
                self.assertEqual(report["status"], "ready")
                self.assertEqual(report["missing_fields"], [])
                self.assertEqual(self.native_calls(), before_native)
                self.assertEqual(jobs(self), before_jobs)
                self.assertEqual(calls, [])
                self.assertFalse((self.state / "router/router.db").exists(), "context dialogue must not create route storage")

    async def test_l03_incomplete_evidence_refuses_without_reserving_identity(self):
        request = self.request(task_evidence=evidence(verification=[]))
        with scorer(self) as calls:
            async with self.session() as client:
                await self.call(client, "delegate", request, error="needs_clarification")
                self.assertEqual(jobs(self), [])
                request["task_evidence"]["verification"] = ["Inspect the output"]
                result = await self.call(client, "delegate", request)
                await self.terminal(client, result["job"]["job_id"])
                self.assertEqual(calls, [])

    async def test_l03_over_budget_precedes_missing_fields_and_never_truncates(self):
        partial = evidence()
        del partial["uncertainties"]
        async with self.session() as client:
            report = (await self.call(client, "context_requirements", {"task": "λ" * 6001, "selection": {"mode": "s1"}, "task_evidence": partial}))["context_requirements"]
            self.assertEqual(report["status"], "over_budget")
            self.assertEqual(report["missing_fields"], ["uncertainties"])
            self.assertGreater(report["scorer_chars"], report["scorer_limit_chars"])
            self.assertEqual(jobs(self), [])

    async def test_l04_explicit_route_logs_context_without_inventing_score(self):
        request = self.route_request(task_evidence=evidence())
        with scorer(self) as calls:
            async with self.session() as client:
                result = await self.call(client, "route", request)
                context = route_context(self, result["route"]["route_id"])
                self.assertEqual(context["selection_mode"], "explicit")
                self.assertEqual(context["task"], request["task"])
                self.assertEqual(context["context"], request["context"])
                self.assertIsNone(context["scoring_text"])
                self.assertIsNone(context["scoring_digest"])
                self.assertEqual(calls, [])
