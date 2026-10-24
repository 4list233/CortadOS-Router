"""S1 outcome loop (docs/S1-OUTCOME-LOOP.md, RT-S1-OUTCOME-001), AC-01..AC-11. Fakes only: no vendor CLI, no quota."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
sys.path.insert(0, str(REPO / "s1"))

from outcome import agents, generate, judge, mine, quota, run, store, tasks  # noqa: E402


def _jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _spec(**over):
    spec = {"id": "task.1", "seed_id": "seed.1", "prompt": "Create solution.txt", "category": "python-bugfix",
            "work_kind": "code", "fixture": {"files": {"README.md": "hi\n"}},
            "check": {"argv": [sys.executable, "-c",
                               "import pathlib,sys; sys.exit(0 if pathlib.Path('solution.txt').exists() else 1)"],
                      "timeout_s": 30},
            "rubric": ["solution.txt exists"], "outward_tools": []}
    spec.update(over)
    return spec


class OutcomeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CORTADOS_OUTCOME_HOME": self.home})
        self.env.start()
        self.addCleanup(self.env.stop)


class MineTests(OutcomeCase):
    def _transcripts(self, root: Path):
        _jsonl(root / ".claude/projects/p/s1.jsonl", [
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-caveat>x"}},
            {"type": "user", "message": {"role": "user", "content": "<command-name>/login</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>ignore me</system-reminder>Fix the flaky upload test"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        ])
        with (root / ".claude/projects/p/s1.jsonl").open("a") as f:
            f.write("{not json\n")
        _jsonl(root / "Library/Application Support/Claude/local-agent-mode-sessions/a/b/local_c/audit.jsonl", [
            {"type": "system", "subtype": "init"},
            {"type": "user", "message": {"role": "user", "content": "Draft the quarterly invoice summary"}},
        ])
        _jsonl(root / ".codex/sessions/2026/10/24/r.jsonl", [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for /x\n..."}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Add retries to the sync job"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        ])

    def test_mine_extracts_the_first_real_request_from_each_transcript_kind(self):
        root = Path(tempfile.mkdtemp())
        self._transcripts(root)
        counts = mine.mine(root, "pro")
        seeds = {s["source"]: s for s in store.read("seeds")}
        self.assertEqual(seeds["claude-code"]["text"], "Fix the flaky upload test")
        self.assertEqual(seeds["claude-code"]["tools"], ["Bash"])
        self.assertEqual(seeds["cowork"]["text"], "Draft the quarterly invoice summary")
        self.assertEqual(seeds["codex"]["text"], "Add retries to the sync job")
        self.assertEqual(seeds["codex"]["tools"], ["shell"])
        self.assertEqual(counts["unreadable_lines"], 1)
        self.assertEqual({s["host"] for s in seeds.values()}, {"pro"})

    def test_mine_is_idempotent(self):
        root = Path(tempfile.mkdtemp())
        self._transcripts(root)
        mine.mine(root, "pro")
        counts = mine.mine(root, "pro")
        self.assertEqual(len(store.read("seeds")), 3)
        self.assertEqual(counts["already"], 3)


class SpecTests(OutcomeCase):
    def test_valid_spec_passes(self):
        self.assertEqual(tasks.validate(_spec())["id"], "task.1")

    def test_spec_refusals_name_the_rule(self):
        cases = {
            "fields": _spec(extra=1),
            "relative": _spec(fixture={"files": {"/etc/passwd": "x"}}),
            "inside the workspace": _spec(fixture={"files": {"../up.txt": "x"}}),
            "exceeds": _spec(fixture={"files": {"big.txt": "x" * (tasks.MAX_FIXTURE_BYTES + 1)}}),
            "timeout_s": _spec(check={"argv": ["true"], "timeout_s": tasks.MAX_CHECK_TIMEOUT_S + 1}),
            "work_kind": _spec(work_kind="cooking"),
            "outward_tools": _spec(outward_tools=["rm -rf"]),
            "rubric": _spec(rubric=[]),
        }
        for rule, spec in cases.items():
            with self.subTest(rule=rule), self.assertRaisesRegex(tasks.SpecError, rule):
                tasks.validate(spec)

    def test_materialize_writes_a_committed_repo_and_refuses_a_used_workspace(self):
        ws = tempfile.mkdtemp()
        tasks.materialize(_spec(fixture={"files": {"src/a.py": "x = 1\n"}}), ws)
        self.assertEqual(Path(ws, "src/a.py").read_text(), "x = 1\n")
        log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout
        self.assertIn("fixture", log)
        with self.assertRaisesRegex(tasks.SpecError, "empty"):
            tasks.materialize(_spec(), ws)


class LadderTests(OutcomeCase):
    def _resolve(self, decision, **kw):
        self.calls.append(kw)
        b = kw.get("minimum_band")
        return {"band": b, "chosen": {"lane": "codex", "provider": "codex-cli", "model": f"m-{b}"}, "skipped": []}

    def test_ladder_resolves_each_band_through_core_resolve_with_minimum_band(self):
        self.calls = []
        with mock.patch.object(run.core, "resolve", self._resolve):
            rungs = run.ladder("R2", "code", (("codex-cli", "m"),))
        self.assertEqual([r["band"] for r in rungs], ["R1", "R2", "R3"])
        self.assertEqual([c["minimum_band"] for c in self.calls], ["R1", "R2", "R3"])
        self.assertTrue(all(c["execution"] and c["allowed_routes"] == (("codex-cli", "m"),) for c in self.calls))
        self.assertEqual(rungs[0]["route"]["model"], "m-R1")

    def test_ladder_stays_inside_r0_to_r4(self):
        self.calls = []
        with mock.patch.object(run.core, "resolve", self._resolve):
            self.assertEqual([r["band"] for r in run.ladder("R0", "code", ())], ["R0", "R1"])
            self.assertEqual([r["band"] for r in run.ladder("R4", "code", ())], ["R3", "R4"])

    def test_zero_decision_carries_only_the_work_kind(self):
        d = run.zero_decision("writing")
        self.assertFalse(d["degraded"])
        self.assertEqual(d["answers"]["work_kind"], "writing")
        self.assertTrue(all(d["answers"][f] == 0 for f in run.core.FEATURES))


FAKE_CLAUDE = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    with open(os.environ["FAKE_ENV_DUMP"], "w") as f:
        json.dump({{"env": dict(os.environ), "argv": sys.argv, "cwd": os.getcwd()}}, f)
    prompt = sys.stdin.read()
    if "create" in prompt.lower():
        open("solution.txt", "w").write("done\\n")
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Write"}}]}}}}))
    print(json.dumps({{"type": "result", "result": "Created solution.txt", "num_turns": 2, "usage": {{"output_tokens": 5}}}}))
    """)


class RunTests(OutcomeCase):
    def setUp(self):
        super().setUp()
        bindir = Path(tempfile.mkdtemp())
        fake = bindir / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.dump = str(bindir / "env.json")
        patch = mock.patch.dict(os.environ, {"CORTADOS_OUTCOME_CLAUDE_BIN": str(fake), "FAKE_ENV_DUMP": self.dump,
                                             "ANTHROPIC_BASE_URL": "http://127.0.0.1:8432",
                                             "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k"})
        patch.start()
        self.addCleanup(patch.stop)

    def _dump(self):
        with open(self.dump) as f:
            return json.load(f)

    def _rung(self, band="R2"):
        return {"band": band, "route": {"lane": "claude", "provider": "claude-cli", "model": "claude-x"}}

    def test_run_one_runs_the_agent_and_the_check_in_a_fresh_repo_then_removes_it(self):
        result = run.run_one(_spec(), self._rung())
        self.assertTrue(result["check"]["passed"])
        self.assertEqual(result["trace"]["tools"], {"Write": 1})
        self.assertEqual(result["trace"]["final"], "Created solution.txt")
        self.assertIn("solution.txt", result["diff"])
        cwd = self._dump()["cwd"]
        self.assertTrue(os.path.basename(cwd).startswith("outcome-"))
        self.assertFalse(os.path.exists(cwd))

    def test_child_environment_carries_no_gateway_or_api_key(self):
        run.run_one(_spec(), self._rung())
        env = self._dump()["env"]
        for key in agents.SCRUBBED_ENV:
            self.assertNotIn(key, env)

    def test_claude_run_disallows_web_tools_and_isolates_settings(self):
        run.run_one(_spec(), self._rung())
        argv = self._dump()["argv"]
        self.assertIn("WebFetch", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)

    def test_failed_check_is_recorded_as_a_fail(self):
        result = run.run_one(_spec(prompt="do nothing"), self._rung())
        self.assertFalse(result["check"]["passed"])

    def test_run_task_resumes_without_duplicating_and_marks_the_ladder_only_when_nothing_is_held(self):
        spec = _spec()
        store.append("tasks", spec)
        served = (("claude-cli", "claude-x"),)
        score = {"id": "score.1", "task_id": spec["id"], "band": "R2", "answers": {}, "engine": "s1", "D": 4}
        store.append("scores", score)
        rungs = [self._rung(b) for b in ("R1", "R2", "R3")]
        for i, r in enumerate(rungs):
            r["route"] = dict(r["route"], model=f"claude-{i}")
        held = {"n": 0}

        def gate():
            held["n"] += 1
            return {"claude": (held["n"] > 1, "test gate")}
        with mock.patch.object(run, "ladder", return_value=rungs):
            first = run.run_task(spec, served, gate=gate)
            self.assertEqual(first["held"], 1)
            self.assertNotIn(spec["id"], store.ids("ladders"))
            second = run.run_task(spec, served, gate=lambda: {"claude": (True, "ok")})
        self.assertEqual(sum(1 for r in store.read("runs") if r["task_id"] == spec["id"]), 3)
        self.assertEqual(second["ran"], 1)
        self.assertIn(spec["id"], store.ids("ladders"))
        self.assertEqual(run.pending(), [])

    def test_two_bands_on_one_model_share_one_run(self):
        spec = _spec()
        store.append("scores", {"id": "s", "task_id": spec["id"], "band": "R1", "answers": {}, "engine": "s1", "D": 1})
        with mock.patch.object(run, "ladder", return_value=[self._rung("R0"), self._rung("R1")]):
            counts = run.run_task(spec, (), gate=lambda: {"claude": (True, "ok")})
        self.assertEqual(counts["ran"], 1)
        self.assertTrue(store.read("runs")[1]["reused"])


class MockToolsTests(unittest.TestCase):
    def test_mock_tools_record_each_call_and_return_success(self):
        record = os.path.join(tempfile.mkdtemp(), "calls.jsonl")
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "send_email", "arguments": {"to": "a@b"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "wire_money", "arguments": {}}}]
        p = subprocess.run([sys.executable, agents.MOCK_SERVER, record, "send_email"], capture_output=True, text=True,
                           input="".join(json.dumps(m) + "\n" for m in msgs), timeout=30)
        replies = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
        self.assertEqual([t["name"] for t in replies[2]["result"]["tools"]], ["send_email"])
        self.assertFalse(replies[3]["result"].get("isError"))
        self.assertTrue(replies[4]["result"]["isError"])
        with open(record) as f:
            calls = [json.loads(l) for l in f]
        self.assertEqual([(c["tool"], c["arguments"]) for c in calls], [("send_email", {"to": "a@b"})])


class JudgeTests(OutcomeCase):
    def _runs(self, checks):
        return [{"band": b, "route": {"model": f"m-{b}"},
                 "result": {"trace": {"final": "x"}, "diff": "", "check": c}} for b, c in checks]

    def test_outcome_band_is_the_lowest_band_that_passed_check_and_judge(self):
        runs = self._runs([("R1", {"ran": True, "passed": False}), ("R2", {"ran": True, "passed": True}),
                           ("R3", {"ran": True, "passed": True})])
        verdicts = {"R1": {"pass": True}, "R2": {"pass": True}, "R3": {"pass": True}}
        self.assertEqual(judge.outcome_band(verdicts, runs), "R2")

    def test_no_pass_is_above_and_a_missing_check_leaves_the_judge_to_decide(self):
        runs = self._runs([("R1", {"ran": False, "passed": None}), ("R2", {"ran": False, "passed": None})])
        self.assertEqual(judge.outcome_band({"R1": {"pass": False}, "R2": {"pass": False}}, runs), "above")
        self.assertEqual(judge.outcome_band({"R1": {"pass": False}, "R2": {"pass": True}}, runs), "R2")

    def _labels(self):
        return {"steps": 1, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
                "outward": False, "work_kind": "code", "urgency": "now", "needs_clarification": False, "parts": 0,
                "diversity": False}

    def test_judge_writes_one_outcome_per_ladder_with_the_fable_teacher_on_both_splits(self):
        for tid in ("task.a", "task.b", "task.c"):
            store.append("tasks", _spec(id=tid))
            store.append("ladders", {"id": tid, "s1_band": "R1", "bands": ["R0", "R1", "R2"]})
            for r in self._runs([("R0", {"ran": True, "passed": False}), ("R1", {"ran": True, "passed": True})]):
                store.append("runs", {**r, "task_id": tid})
        asked = []

        def ask(model, system, user):
            asked.append(model)
            return json.dumps({"verdicts": {"R0": {"pass": False, "why": "-"}, "R1": {"pass": True, "why": "-"}},
                               "labels": self._labels()})
        counts = judge.judge(ask=ask, gate=lambda: {"claude": (True, "ok")})
        again = judge.judge(ask=ask, gate=lambda: {"claude": (True, "ok")})
        self.assertEqual((counts["judged"], again["judged"]), (3, 0))
        self.assertEqual(set(asked), {"claude-fable-5-1"})
        outcomes = store.read("outcomes")
        self.assertEqual({o["outcome_band"] for o in outcomes}, {"R1"})
        self.assertEqual({o["teacher"] for o in outcomes}, {"claude-fable-5-1"})

    def test_invalid_teacher_labels_are_refused_and_retried(self):
        store.append("tasks", _spec())
        store.append("ladders", {"id": "task.1", "s1_band": "R1", "bands": ["R1"]})
        bad = lambda *a: json.dumps({"verdicts": {}, "labels": {"steps": 9}})
        counts = judge.judge(ask=bad, gate=lambda: {"claude": (True, "ok")})
        self.assertEqual((counts["judged"], counts["failed"]), (0, 1))
        self.assertEqual(store.read("outcomes"), [])

    def test_judging_waits_when_the_claude_pool_is_held(self):
        store.append("tasks", _spec())
        store.append("ladders", {"id": "task.1", "s1_band": "R1", "bands": ["R1"]})
        counts = judge.judge(ask=lambda *a: self.fail("asked while held"), gate=lambda: {"claude": (False, "95%")})
        self.assertEqual(counts["held"], 1)


class GenerateTests(OutcomeCase):
    def test_generate_validates_specs_records_refusals_and_never_retries_a_seed(self):
        for i in range(3):
            store.append("seeds", {"id": f"seed.{i}", "text": f"request {i}", "tools": []})
        good = {k: v for k, v in _spec().items() if k not in ("id", "seed_id")}

        def ask(model, prompt):
            self.assertEqual(model, generate.GENERATOR)
            return json.dumps([{**good, "seed": 0}, {**good, "seed": 1, "fixture": {"files": {"/abs": "x"}}}])
        counts = generate.generate(ask=ask)
        self.assertEqual((counts["tasks"], counts["refused"]), (1, 2))
        self.assertEqual(store.read("tasks")[0]["seed_id"], "seed.0")
        self.assertEqual(generate.generate(ask=lambda *a: self.fail("seed retried"))["batches"], 0)

    def test_a_failed_generation_call_leaves_its_seeds_pending(self):
        store.append("seeds", {"id": "seed.0", "text": "r", "tools": []})

        def boom(*a):
            raise RuntimeError("codex exited 1")
        self.assertEqual(generate.generate(ask=boom)["failed_batches"], 1)
        self.assertEqual(len(generate._pending_seeds()), 1)


class QuotaTests(unittest.TestCase):
    def _r(self, projected, used, resets_in, **kw):
        return {"scarcity": 0.1, "refusing": False, **kw,
                "tightest": {"projected_pct_at_reset": projected, "used_pct": used, "resets_in_h": resets_in}}

    def test_quota_rule(self):
        cases = [(self._r(60, 40, 30), True), (self._r(95, 40, 30), False), (self._r(50, 96, 30), False),
                 (self._r(60, 40, 0.5), False), (None, False), (self._r(60, 40, 30, refusing=True), False),
                 (self._r(60, 40, 30, scarcity=None), False), (self._r(None, 40, 30), False)]
        for reading, allowed in cases:
            with self.subTest(reading=reading):
                self.assertEqual(quota.verdict(reading)[0], allowed)


class StoreTests(OutcomeCase):
    def test_a_torn_last_line_is_skipped_and_appends_continue(self):
        store.append("runs", {"id": "a"})
        with store.path("runs").open("a") as f:
            f.write('{"id": "b"')
        self.assertEqual([r["id"] for r in store.read("runs")], ["a"])


if __name__ == "__main__":
    unittest.main()
