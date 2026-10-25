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

from outcome import agents, codex_preflight, containment, generate, judge, mine, quota, run, store, tasks  # noqa: E402


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

    def test_the_loops_own_sessions_never_become_seeds(self):
        root = Path(tempfile.mkdtemp())
        store.append("tasks", _spec(prompt="Refactor the invoice parser into a module"))
        _jsonl(root / ".codex/sessions/a.jsonl", [{"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": generate.PROMPT[:200]}]}}])
        _jsonl(root / ".codex/sessions/b.jsonl", [{"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text",
                                                            "text": "Refactor the invoice parser into a module"}]}}])
        counts = mine.mine(root, "studio")
        self.assertEqual(store.read("seeds"), [])
        self.assertEqual(counts["loop_own"], 2)

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

    def test_ladder_starts_at_r0(self):
        self.calls = []
        with mock.patch.object(run.core, "resolve", self._resolve):
            self.assertEqual([r["band"] for r in run.ladder("R0", "code", ())], ["R0", "R1"])

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

    def test_each_claude_run_gets_a_private_short_scratch_root_that_is_removed(self):
        run.run_one(_spec(), self._rung())
        scratch = self._dump()["env"]["CLAUDE_CODE_TMPDIR"]
        self.assertTrue(scratch.startswith("/tmp/oc-"))
        self.assertLess(len(scratch), 40)
        self.assertFalse(os.path.exists(scratch))

    def test_claude_run_isolates_settings_and_mcp(self):
        run.run_one(_spec(), self._rung())
        argv = self._dump()["argv"]
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
        asked = []

        def admit(lane, model):
            asked.append((lane, model))
            return (len(asked) > 1, "test gate")
        with mock.patch.object(run, "ladder", return_value=rungs):
            first = run.run_task(spec, served, admit=admit)
            self.assertEqual(first["held"], 1)
            self.assertNotIn(spec["id"], store.ids("ladders"))
            second = run.run_task(spec, served, admit=lambda lane, model: (True, "ok"))
        self.assertEqual(asked[0], ("claude", "claude-0"))
        self.assertEqual(sum(1 for r in store.read("runs") if r["task_id"] == spec["id"]), 3)
        self.assertEqual(second["ran"], 1)
        self.assertIn(spec["id"], store.ids("ladders"))
        self.assertEqual(run.pending(), [])
        self.assertEqual(len(store.read("attempts")), 3)

    def test_two_bands_on_one_model_share_one_run(self):
        spec = _spec()
        store.append("scores", {"id": "s", "task_id": spec["id"], "band": "R1", "answers": {}, "engine": "s1", "D": 1})
        with mock.patch.object(run, "ladder", return_value=[self._rung("R0"), self._rung("R1")]):
            counts = run.run_task(spec, (), admit=lambda lane, model: (True, "ok"))
        self.assertEqual(counts["ran"], 1)
        self.assertTrue(store.read("runs")[1]["reused"])

    def test_a_claimed_task_is_not_run_twice(self):
        self.assertTrue(run.CLAIMS.take("task.1"))
        try:
            self.assertEqual(run.run_task(_spec(), (), admit=lambda *a: self.fail("admitted"))["skipped"],
                             "claimed by another worker")
        finally:
            run.CLAIMS.release("task.1")

    def test_claude_run_is_os_sandboxed_without_unsandboxed_escape(self):
        run.run_one(_spec(), self._rung())
        argv = self._dump()["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], agents.CLAUDE_TOOLS)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings["sandbox"], {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
                                               "allowUnsandboxedCommands": False, "excludedCommands": [],
                                               "network": {"allowedDomains": []}})
        self.assertNotIn("WebFetch", argv[argv.index("--tools") + 1])

    def test_outward_call_evidence_cannot_be_forged_from_the_workspace(self):
        forged = json.dumps({"tool": "send_email", "arguments": {}}) + "\n"
        spec = _spec(fixture={"files": {"calls.jsonl": forged, ".outcome-mock-calls.jsonl": forged}},
                     outward_tools=["send_email"])
        result = run.run_one(spec, self._rung())
        self.assertEqual(result["trace"]["outward_calls"], [])


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
        admitted = []
        counts = judge.judge(ask=ask, admit=lambda lane, model: (admitted.append((lane, model)) or True, "ok"))
        again = judge.judge(ask=ask, admit=lambda lane, model: (True, "ok"))
        self.assertEqual(set(admitted), {("claude", "claude-fable-5-1")})
        self.assertEqual((counts["judged"], again["judged"]), (3, 0))
        self.assertEqual(set(asked), {"claude-fable-5-1"})
        outcomes = store.read("outcomes")
        self.assertEqual({o["outcome_band"] for o in outcomes}, {"R1"})
        self.assertEqual({o["teacher"] for o in outcomes}, {"claude-fable-5-1"})

    def test_invalid_teacher_labels_are_refused_and_retried(self):
        store.append("tasks", _spec())
        store.append("ladders", {"id": "task.1", "s1_band": "R1", "bands": ["R1"]})
        bad = lambda *a: json.dumps({"verdicts": {}, "labels": {"steps": 9}})
        counts = judge.judge(ask=bad, admit=lambda lane, model: (True, "ok"))
        self.assertEqual((counts["judged"], counts["failed"]), (0, 1))
        self.assertEqual(store.read("outcomes"), [])

    def test_judging_waits_when_the_claude_pool_is_held(self):
        store.append("tasks", _spec())
        store.append("ladders", {"id": "task.1", "s1_band": "R1", "bands": ["R1"]})
        counts = judge.judge(ask=lambda *a: self.fail("asked while held"), admit=lambda lane, model: (False, "95%"))
        self.assertEqual(counts["held"], 1)


class GenerateTests(OutcomeCase):
    def test_generate_validates_specs_records_refusals_and_never_retries_a_seed(self):
        for i in range(3):
            store.append("seeds", {"id": f"seed.{i}", "text": f"request {i}", "tools": []})
        good = {k: v for k, v in _spec().items() if k not in ("id", "seed_id")}

        def ask(model, prompt, effort=None):
            self.assertEqual((model, effort), (generate.GENERATOR, generate.EFFORT))
            return json.dumps([{**good, "seed": 0}, {**good, "seed": 1, "fixture": {"files": {"/abs": "x"}}}])
        ok = lambda lane, model: (True, "ok")
        counts = generate.generate(ask=ask, admit=ok)
        self.assertEqual((counts["tasks"], counts["refused"]), (1, 2))
        self.assertEqual(store.read("tasks")[0]["seed_id"], "seed.0")
        self.assertEqual(generate.generate(ask=lambda *a, **k: self.fail("seed retried"), admit=ok)["batches"], 0)

    def test_a_failed_generation_call_leaves_its_seeds_pending(self):
        store.append("seeds", {"id": "seed.0", "text": "r", "tools": []})

        def boom(*a, **k):
            raise RuntimeError("codex exited 1")
        self.assertEqual(generate.generate(ask=boom, admit=lambda lane, model: (True, "ok"))["failed_batches"], 1)
        self.assertEqual(len(generate._pending_seeds()), 1)

    def test_every_generation_call_passes_the_quota_door(self):
        for i in range(generate.BATCH * 2):
            store.append("seeds", {"id": f"seed.{i}", "text": f"r{i}", "tools": []})
        admitted = []

        def admit(lane, model):
            admitted.append((lane, model))
            return (len(admitted) == 1, "held after one")
        counts = generate.generate(ask=lambda *a, **k: "[]", admit=admit)
        self.assertEqual((counts["batches"], counts["held"]), (1, 1))
        self.assertEqual(admitted, [("codex", generate.GENERATOR)] * 2)


class FakeScarcity:
    def __init__(self, windows, fetched_at=None, scarcity=0.1, refusing=False, per_model=None):
        self.windows, self.fetched = windows, fetched_at or 1000.0
        self.view = {"scarcity": scarcity, "refusing": refusing}
        self.per_model = per_model

    def current(self):
        lane = {"fetched_at": self.fetched, "windows": self.windows}
        if self.per_model is not None:
            lane["per_model"] = self.per_model
        return {"lanes": {"claude": lane}}

    def lane_scarcity(self, max_reading_age_s):
        return {"claude": self.view}

    def pool_for(self, lane, model, pools):
        return "fable" if "fable" in (model or "") else None    # matches the real pool_for: Fable is pooled even when unobserved


class QuotaTests(OutcomeCase):
    """Windows come from the real producer (scarcity.window_view), never a hand-made shape (Codex review)."""
    WEEK = 7 * 86400

    def _w(self, used=40.0, hours=30.0, elapsed_frac=None, pool="all", window="week", window_s=None):
        import time as _t
        window_s = window_s or self.WEEK
        now = _t.time()
        w = run.core._scarcity_module().window_view(window, used, now + hours * 3600, window_s, now)
        return dict(w, pool=pool)

    def _core(self, sc):
        return mock.Mock(_scarcity_module=lambda: sc)

    def test_real_window_view_output_is_admitted_when_there_is_room(self):
        w = self._w(used=10, hours=24)
        self.assertIsInstance(w["reset_epoch"], float)
        self.assertTrue(quota.admit(self._core(FakeScarcity([w])), "claude", "m")[0])

    def test_quota_rule_on_real_windows(self):
        import time as _t
        cases = [([self._w(used=10, hours=100)], True), ([self._w(used=96, hours=100)], False),
                 ([self._w(used=10, hours=0.5)], False),
                 ([self._w(used=10, hours=100), self._w(used=10, hours=0.5, window="5-hour", window_s=5 * 3600)], False),
                 ([], False)]
        for windows, allowed in cases:
            with self.subTest(windows=[(w["window"], w["used_pct"], w["resets_in_h"]) for w in windows]):
                self.assertEqual(quota.verdict(windows, now=_t.time())[0] if windows else False, allowed)

    def test_a_reading_written_before_reset_epoch_existed_uses_its_sample_time(self):
        w = self._w(used=10, hours=0.8)
        w.pop("reset_epoch")
        sc = FakeScarcity([w], fetched_at=__import__("time").time() - 2 * 3600)   # sampled 2 h ago: reset passed
        self.assertFalse(quota.admit(self._core(sc), "claude", "m")[0])

    def test_a_model_pool_at_its_stop_refuses_that_model_only(self):
        sc = FakeScarcity([self._w(used=10, hours=100), self._w(used=96, hours=100, pool="fable")])
        self.assertFalse(quota.admit(self._core(sc), "claude", "claude-fable-5-1")[0])
        self.assertTrue(quota.admit(self._core(sc), "claude", "claude-sonnet-5-5")[0])

    def test_stale_or_refusing_readings_refuse(self):
        for sc in (FakeScarcity([self._w()], scarcity=None), FakeScarcity([self._w()], refusing=True)):
            self.assertFalse(quota.admit(self._core(sc), "claude", "m")[0])

    def _near_stop(self, extra_pool=False, used=90.0):
        w = self._w(used=used, hours=1.5)                    # 1.5 h before a weekly reset: ~99 % of the window gone
        return [w] + ([dict(w, pool="fable", used_pct=10.0, projected_pct_at_reset=10)] if extra_pool else [])

    @staticmethod
    def _expected_calls(w):
        """How many calls fit by the producer's own pace formula (used / elapsed share)."""
        k = 0
        while (w["used_pct"] + (k + 1) * quota.ASSUMED_PCT_PER_CALL) / max(w["elapsed_pct"] / 100, 0.05) <= quota.STOP_PROJECTED_PCT:
            k += 1
        return k

    def _fresh_now(self):
        return __import__("time").time()

    def test_calls_admitted_on_one_reading_count_toward_the_stop(self):
        windows = self._near_stop()
        sc = FakeScarcity(windows, fetched_at=self._fresh_now())
        results = [quota.admit(self._core(sc), "claude", "m")[0] for _ in range(8)]
        allowed = self._expected_calls(windows[0])
        self.assertGreater(allowed, 0)
        self.assertEqual(results, [True] * allowed + [False] * (8 - allowed))

    def test_a_reservation_counts_at_the_pace_the_producer_projects(self):
        # 47 % used at half the window projects 94 %: one more call (48 %) projects 96 % and must be refused.
        w = self._w(used=47.0, hours=84.0)
        self.assertAlmostEqual(w["elapsed_pct"], 50.0, delta=0.5)
        self.assertFalse(quota.verdict([dict(w, key="k")], now=self._fresh_now())[0])
        self.assertTrue(quota.verdict([dict(self._w(used=46.0, hours=84.0), key="k")], now=self._fresh_now())[0])

    def test_a_shared_window_is_one_budget_across_models(self):
        windows = self._near_stop(extra_pool=True)
        core = self._core(FakeScarcity(windows, fetched_at=self._fresh_now()))
        results = [quota.admit(core, "claude", m)[0] for m in ["claude-fable-5-1"] * 3 + ["claude-sonnet-5-5"] * 5]
        self.assertEqual(sum(results), self._expected_calls(windows[0]))

    def test_reservations_survive_a_restart(self):
        import importlib
        windows = self._near_stop()
        sc = FakeScarcity(windows, fetched_at=self._fresh_now())
        allowed = self._expected_calls(windows[0])
        for _ in range(allowed - 1):
            quota.admit(self._core(sc), "claude", "m")
        fresh = importlib.reload(quota)
        self.assertEqual([fresh.admit(self._core(sc), "claude", "m")[0] for _ in range(2)], [True, False])

    def test_in_flight_work_still_counts_after_a_new_sample(self):
        windows = self._near_stop()
        t = self._fresh_now()
        allowed = self._expected_calls(windows[0])
        for _ in range(allowed):
            quota.admit(self._core(FakeScarcity(windows, fetched_at=t - 600)), "claude", "m")
        # A newer sample of the same windows, 10 minutes later, does not yet show the running work.
        self.assertFalse(quota.admit(self._core(FakeScarcity(windows, fetched_at=t)), "claude", "m")[0])

    def test_a_model_pool_missing_from_the_reading_is_refused_once_it_has_been_seen(self):
        general = self._w(used=10, hours=100)
        with_pool = FakeScarcity([general, dict(self._w(used=10, hours=100), pool="fable")], fetched_at=self._fresh_now())
        self.assertTrue(quota.admit(self._core(with_pool), "claude", "claude-fable-5-1")[0])
        without = FakeScarcity([general], fetched_at=self._fresh_now())
        ok, why = quota.admit(self._core(without), "claude", "claude-fable-5-1")
        self.assertFalse(ok)
        self.assertIn("missing", why)
        self.assertTrue(quota.admit(self._core(without), "claude", "claude-sonnet-5-5")[0])

    def test_a_carried_forward_model_pool_is_refused(self):
        windows = [self._w(used=10, hours=100), dict(self._w(used=10, hours=100), pool="fable", stale=True)]
        sc = FakeScarcity(windows, fetched_at=self._fresh_now())
        ok, why = quota.admit(self._core(sc), "claude", "claude-fable-5-1")
        self.assertFalse(ok)
        self.assertIn("carried-forward", why)
        self.assertTrue(quota.admit(self._core(sc), "claude", "claude-sonnet-5-5")[0])

    def test_a_pooled_model_is_held_when_its_pool_is_unreadable_but_a_shared_window_model_runs(self):
        # Fresh home, per-model panel rate-limited so no pool is observed. Fable has its own pool (pool_for knows it),
        # so it is refused — its cap is unreadable. The run models ride only the visible shared window and are admitted
        # (Forest 2026-10-25: corrects the blanket panel refusal that stalled haiku/sonnet/opus runs).
        sc = FakeScarcity([self._w(used=10, hours=100)], fetched_at=self._fresh_now(),
                          per_model="not shown this read (vendor rate limit on the per-model panel)")
        ok, why = quota.admit(self._core(sc), "claude", "claude-fable-5-1")
        self.assertFalse(ok)
        self.assertIn("missing", why)
        self.assertTrue(quota.admit(self._core(sc), "claude", "claude-sonnet-5-5")[0])

    def test_an_unknown_model_is_refused_when_the_panel_is_unavailable(self):
        # Neither Fable (pooled) nor a confirmed shared model: with the panel down its ownership is unknown, so it is
        # refused rather than spent blind on the shared windows (Codex review 2026-10-25).
        sc = FakeScarcity([self._w(used=10, hours=100)], fetched_at=self._fresh_now(),
                          per_model="not shown this read (vendor rate limit on the per-model panel)")
        ok, why = quota.admit(self._core(sc), "claude", "claude-newmodel-9")
        self.assertFalse(ok)
        self.assertIn("ownership is unknown", why)
        # when the panel IS available, the same unknown model admits against the shared window normally
        ok2, _ = quota.admit(self._core(FakeScarcity([self._w(used=10, hours=100)], fetched_at=self._fresh_now())), "claude", "claude-newmodel-9")
        self.assertTrue(ok2)

    def test_a_missing_general_window_refuses_even_a_poolless_model(self):
        both = [self._w(used=10, hours=100, window="week"), self._w(used=10, hours=4, window="5-hour", window_s=5 * 3600)]
        self.assertTrue(quota.admit(self._core(FakeScarcity(both, fetched_at=self._fresh_now())), "claude", "claude-sonnet-5-5")[0])
        only_5h = [self._w(used=10, hours=4, window="5-hour", window_s=5 * 3600)]
        ok, why = quota.admit(self._core(FakeScarcity(only_5h, fetched_at=self._fresh_now())), "claude", "claude-sonnet-5-5")
        self.assertFalse(ok)
        self.assertIn("week", why)


class StoreTests(OutcomeCase):
    def test_a_torn_last_line_is_skipped(self):
        store.append("runs", {"id": "a"})
        with store.path("runs").open("a") as f:
            f.write('{"id": "b"')
        self.assertEqual([r["id"] for r in store.read("runs")], ["a"])

    def test_an_append_after_a_torn_line_survives_and_the_tail_is_quarantined(self):
        store.append("runs", {"id": "a"})
        with store.path("runs").open("a") as f:
            f.write('{"id": "b"')
        store.append("runs", {"id": "c"})
        self.assertEqual([r["id"] for r in store.read("runs")], ["a", "c"])
        self.assertIn('{"id": "b"', store.path("runs").with_suffix(".torn").read_text())

    def test_a_tail_torn_inside_a_multibyte_character_is_recovered(self):
        store.append("runs", {"id": "a", "text": "é"})
        with store.path("runs").open("ab") as f:
            f.write('{"id": "b", "text": "é'.encode()[:-1])
        self.assertEqual([r["id"] for r in store.read("runs")], ["a"])
        store.append("runs", {"id": "c"})
        self.assertEqual([r["id"] for r in store.read("runs")], ["a", "c"])

    def test_claims_are_exclusive_until_released(self):
        claims = store.Claims()
        self.assertTrue(claims.take("t"))
        self.assertFalse(claims.take("t"))
        claims.release("t")
        self.assertTrue(claims.take("t"))

    def test_one_loop_per_data_home(self):
        lock = store.single_instance()
        try:
            with self.assertRaisesRegex(RuntimeError, "another outcome loop"):
                store.single_instance()
        finally:
            lock.close()


@unittest.skipUnless(os.path.exists(containment.SANDBOX_EXEC), "macOS Seatbelt only")
class ContainmentTests(unittest.TestCase):
    def test_a_generated_check_cannot_write_outside_or_reach_the_network(self):
        ws, outside = tempfile.mkdtemp(), tempfile.mkdtemp()
        probe = textwrap.dedent(f"""\
            import os, socket, sys
            open('inside.txt', 'w').write('ok')
            bad = []
            for p in ({os.path.join(outside, 'x')!r}, {str(Path.home() / 'outcome-probe-should-not-exist')!r}):
                try:
                    open(p, 'w').write('x'); bad.append(p)
                except OSError:
                    pass
            try:
                socket.create_connection(('1.1.1.1', 443), timeout=3); bad.append('network')
            except OSError:
                pass
            sys.exit(1 if bad else 0)
            """)
        spec = _spec(check={"argv": [sys.executable, "-c", probe], "timeout_s": 30})
        self.assertTrue(tasks.run_check(spec, ws)["passed"])
        self.assertTrue(os.path.exists(os.path.join(ws, "inside.txt")))
        self.assertEqual(os.listdir(outside), [])

    def test_fixtures_may_not_create_reserved_directories_in_any_case_or_depth(self):
        for path in [f"{d}/config" for d in tasks.RESERVED_DIRS] + [".GIT/config", ".Git/hooks/x", "src/.git/config",
                                                                      ".Claude/settings.json", ".CODEX/config.toml"]:
            with self.subTest(path=path), self.assertRaisesRegex(tasks.SpecError, "reserved"):
                tasks.validate(_spec(fixture={"files": {path: "x"}}))

    def _refused(self, argv):
        ws = tempfile.mkdtemp()
        spec = _spec(check={"argv": argv, "timeout_s": 30})
        return tasks.run_check(spec, ws)["passed"] is False

    def test_a_check_cannot_signal_an_outside_process(self):
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            self.assertTrue(self._refused([sys.executable, "-c", f"import os; os.kill({sleeper.pid}, 15)"]))
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_a_check_leaves_no_process_behind_even_a_detached_one(self):
        script = ("import subprocess, time\n"
                  "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
                  "open('child.pid', 'w').write(str(p.pid))\n"
                  "time.sleep(60)\n")
        ws = tempfile.mkdtemp()
        t0 = __import__("time").time()
        result = tasks.run_check(_spec(check={"argv": [sys.executable, "-c", script], "timeout_s": 3}), ws)
        self.assertEqual(result.get("why"), "check timed out")
        self.assertLess(__import__("time").time() - t0, 20)        # the detached child's pipe does not hold the run
        pid = int(Path(ws, "child.pid").read_text())
        __import__("time").sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def _detached_script(self, then_exit: bool) -> str:
        child = ("import os, time; os.chdir('/'); "
                 "[os.close(fd) for fd in (0, 1, 2)]; time.sleep(60)")
        return ("import subprocess, sys, time\n"
                f"p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True, cwd='/',\n"
                "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "open('child.pid', 'w').write(str(p.pid))\n"
                + ("" if then_exit else "time.sleep(60)\n"))

    def _assert_gone(self, ws):
        pid = int(Path(ws, "child.pid").read_text())
        __import__("time").sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_detached_child_that_left_the_workspace_is_killed_after_a_timeout(self):
        ws = tempfile.mkdtemp()
        tasks.run_check(_spec(check={"argv": [sys.executable, "-c", self._detached_script(False)], "timeout_s": 3}), ws)
        self._assert_gone(ws)

    def test_a_detached_child_that_left_the_workspace_is_killed_after_success(self):
        ws = tempfile.mkdtemp()
        result = tasks.run_check(_spec(check={"argv": [sys.executable, "-c", self._detached_script(True)],
                                              "timeout_s": 30}), ws)
        self.assertTrue(result["passed"])
        self._assert_gone(ws)

    def test_a_check_cannot_send_apple_events(self):
        self.assertTrue(self._refused(["/usr/bin/osascript", "-e", 'tell application "Finder" to get count of windows']))

    def test_the_runner_git_ignores_a_planted_fsmonitor(self):
        ws = tempfile.mkdtemp()
        tasks.materialize(_spec(), ws)
        marker = os.path.join(tempfile.mkdtemp(), "fsmonitor-ran")
        with open(os.path.join(ws, ".git", "config"), "a") as f:
            f.write(f"[core]\n\tfsmonitor = touch {marker}\n")
        Path(ws, "new.txt").write_text("x")
        self.assertIn("new.txt", tasks.workspace_diff(ws))
        self.assertFalse(os.path.exists(marker))


class LadderCapabilityTests(unittest.TestCase):
    def test_the_ladder_stops_at_r3(self):
        calls = []

        def resolve(decision, **kw):
            calls.append(kw["minimum_band"])
            return {"band": kw["minimum_band"], "chosen": None, "skipped": [], "required_index": 1}
        with mock.patch.object(run.core, "resolve", resolve):
            self.assertEqual([r["band"] for r in run.ladder("R3", "code", ())], ["R2", "R3"])
            self.assertEqual([r["band"] for r in run.ladder("R4", "code", ())], ["R2", "R3"])
        self.assertNotIn("R4", calls)

    def test_required_capability_rises_up_the_ladder_with_the_real_resolver_and_tables(self):
        from cortados_router.provider import routing
        served = routing.served_routes(run.core)
        for kind in ("code", "writing", "research"):
            with self.subTest(kind=kind):
                req = [r["required_index"] for r in run.ladder("R1", kind, served) + run.ladder("R3", kind, served)[1:]]
                self.assertEqual(req, sorted(req))
                self.assertLess(req[0], req[-1])


class CodexIsolationTests(unittest.TestCase):
    def test_codex_runs_disable_every_home_mcp_server_and_integration(self):
        home = tempfile.mkdtemp()
        Path(home, "config.toml").write_text('[mcp_servers.github]\ncommand = "x"\n[mcp_servers.erp]\ncommand = "y"\n')
        with mock.patch.dict(os.environ, {"CORTADOS_OUTCOME_CODEX_HOME": home}):
            task_args = agents.codex_isolation_args(keep_tools=agents.CODEX_TASK_TOOL_FEATURES)
            text_args = agents.codex_isolation_args()
        for name in ("github", "erp"):
            self.assertIn(f"mcp_servers.{name}.enabled=false", task_args)
        self.assertIn("hooks", task_args)
        self.assertNotIn("shell_tool", task_args)
        self.assertIn("shell_tool", text_args)
        self.assertIn('model_provider="openai"', task_args)
        for pin in codex_preflight.SANDBOX_PINS:
            self.assertIn(pin, task_args)

    def _cfg(self, **over):
        from cortados_router.provider import codex_isolation
        cfg = dict(codex_isolation.EXPECTED_CONFIG, mcp_servers={"github": {"enabled": False}},
                   features={f: False for f in codex_isolation.DISABLED_FEATURES},
                   sandbox_workspace_write=dict(codex_preflight.EXPECTED_SANDBOX))
        cfg.update(over)
        return cfg

    def test_preflight_accepts_the_isolated_config_and_names_every_violation(self):
        self.assertEqual(codex_preflight.check_config(self._cfg(), ()), [])
        bad = codex_preflight.check_config(self._cfg(
            openai_base_url="http://127.0.0.1:11434/v1", model_providers={"openai": {}},
            mcp_servers={"erp": {"enabled": True}}, features={"hooks": True},
            sandbox_workspace_write={"writable_roots": ["/Users/velox"], "network_access": True}), ())
        text = "; ".join(bad)
        for needle in ("openai_base_url", "model_providers.openai", "erp", "hooks", "writable_roots", "network_access",
                       "exclude_tmpdir_env_var"):
            self.assertIn(needle, text)

    def test_every_codex_call_is_ephemeral(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            last = argv[argv.index("--output-last-message") + 1] if "--output-last-message" in argv else None
            if last:
                Path(last).write_text("[]")
            return 0, "", "", 0.0
        ws = tempfile.mkdtemp()
        with mock.patch.object(agents, "_run", fake_run), mock.patch.object(codex_preflight, "preflight"):
            agents.ask_codex("gpt-6-astra", "hi")
            agents.run_task("codex", "gpt-6-luna", "hi", ws, [], 30)
        self.assertEqual(len(seen), 2)
        for argv in seen:
            self.assertEqual(argv[1:3], ["exec", "--ephemeral"])

    def test_preflight_gives_up_on_a_silent_app_server(self):
        fake = Path(tempfile.mkdtemp(), "codex")
        fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        fake.chmod(0o755)
        t0 = __import__("time").time()
        with mock.patch.object(codex_preflight, "HANDSHAKE_S", 1), self.assertRaises(Exception):
            codex_preflight.preflight([str(fake)], ["-c", "x=1"], dict(os.environ), ())
        self.assertLess(__import__("time").time() - t0, 15)

    def test_preflight_allows_the_kept_task_tools(self):
        cfg = self._cfg()
        cfg["features"] = {f: False for f in cfg["features"]}
        cfg["features"].update({f: True for f in agents.CODEX_TASK_TOOL_FEATURES})
        self.assertEqual(codex_preflight.check_config(cfg, agents.CODEX_TASK_TOOL_FEATURES), [])
        self.assertTrue(codex_preflight.check_config(cfg, ()))


def _olabels(**over):
    a = {"steps": 1, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
         "outward": False, "work_kind": "code", "urgency": "now", "needs_clarification": False, "parts": 0,
         "diversity": False}
    a.update(over)
    return a


def _outcome(oid, split, s1_band="R1", outcome_band="R1", labels=None):
    return {"id": oid, "task_id": oid, "split": split, "teacher": "claude-fable-5-1", "s1_band": s1_band,
            "request": f"do {oid}", "outcome_band": outcome_band, "verdicts": {},
            "labels": _olabels() if labels is None else labels}


def _nonvalid_id(prefix):
    """An outcome id that the valid-split hash (10%) does NOT select, so REPS/upweight are deterministic in a test."""
    import hashlib
    i = 0
    while int(hashlib.sha1(("v" + f"{prefix}.{i}").encode()).hexdigest(), 16) % 10 == 0:
        i += 1
    return f"{prefix}.{i}"


class OutcomeTrainGateTests(OutcomeCase):
    def test_counts_only_valid_labelled_by_split(self):
        from outcome import train_gate
        store.append("outcomes", _outcome("o.t1", "train"))
        store.append("outcomes", _outcome("o.t2", "train"))
        store.append("outcomes", _outcome("o.h1", "holdout"))
        store.append("outcomes", _outcome("o.bad", "train", labels={"steps": 9}))    # off-schema label: not training data
        c = train_gate.counts()
        self.assertEqual((c["train"], c["holdout"], c["unlabelled"]), (2, 1, 1))

    def test_decision_fires_at_threshold(self):
        from outcome import train_gate
        with mock.patch.object(train_gate, "MIN_TRAIN", 2):
            self.assertFalse(train_gate.decision()["fire"])
            store.append("outcomes", _outcome("o.1", "train"))
            store.append("outcomes", _outcome("o.2", "train"))
            d = train_gate.decision()
            self.assertTrue(d["fire"])
            self.assertEqual(d["counts"]["train"], 2)

    def test_arm_marker_round_trips(self):
        from outcome import train_gate
        self.assertFalse(train_gate.armed())
        (store.home() / train_gate.ARM_MARKER).write_text("x")
        self.assertTrue(train_gate.armed())


class OutcomeSftTests(OutcomeCase):
    def test_outcome_examples_are_train_split_valid_only(self):
        import build_sft
        from outcome import train_gate
        store.append("outcomes", _outcome("o.t", "train"))
        store.append("outcomes", _outcome("o.hold", "holdout"))
        store.append("outcomes", _outcome("o.bad", "train", labels={"steps": 9}))
        with mock.patch.object(train_gate, "MIN_TRAIN", 1):
            train, valid, bands, n = build_sft.outcome_examples()
        self.assertEqual(n, 1)                                                       # holdout + invalid excluded
        self.assertTrue(all("messages" in e and len(e["messages"]) == 3 for e in train + valid))

    def test_under_routing_is_upweighted(self):
        import build_sft
        from outcome import train_gate
        matched, under = _nonvalid_id("om"), _nonvalid_id("ou")
        store.append("outcomes", _outcome(matched, "train", s1_band="R2", outcome_band="R2"))   # no under-routing
        store.append("outcomes", _outcome(under, "train", s1_band="R0", outcome_band="R2"))     # S1 under-routed
        band = build_sft.band_of(_olabels())
        reps = build_sft.REPS.get(band, 1)
        with mock.patch.object(train_gate, "MIN_TRAIN", 1):
            train = build_sft.outcome_examples()[0]
        self.assertEqual(len(train), reps * (1 + build_sft.UPWEIGHT_HIGH))

    def test_outcomes_are_withheld_from_training_below_the_fire_threshold(self):
        import build_sft
        from outcome import train_gate
        for i in range(3):
            store.append("outcomes", _outcome(_nonvalid_id(f"ot{i}"), "train"))
        with mock.patch.object(train_gate, "MIN_TRAIN", 100):                        # 3 < 100: not training data yet
            train, valid, _bands, n = build_sft.outcome_examples()
            self.assertEqual((train, valid, n), ([], [], 0))
        with mock.patch.object(train_gate, "MIN_TRAIN", 3):                          # threshold met: included
            self.assertEqual(build_sft.outcome_examples()[3], 3)


class OutcomeEvalTests(OutcomeCase):
    def test_outcome_holdout_keeps_banded_truth_and_drops_above(self):
        import evaluate
        store.append("outcomes", _outcome("e.h1", "holdout", outcome_band="R2"))
        store.append("outcomes", _outcome("e.above", "holdout", outcome_band="above"))
        store.append("outcomes", _outcome("e.train", "train", outcome_band="R1"))
        rows = evaluate.outcome_holdout()
        self.assertEqual([(r["id"], truth) for r, truth in rows], [("e.h1", "R2")])

    def test_grade_outcome_counts_under_and_over(self):
        import evaluate
        rows = [({"id": "1", "request": "a", "unit_kind": "interactive"}, "R2"),
                ({"id": "2", "request": "b", "unit_kind": "interactive"}, "R1")]
        preds = {"a": "R0", "b": "R3"}                       # a under-routes (R0<R2); b over-routes (R3>R1)
        with mock.patch.object(evaluate, "band_of", lambda ans: preds[ans["r"]]):
            g = evaluate.grade_outcome(lambda r: {"answers": {"r": r["request"]}}, rows)
        self.assertEqual((g["n"], g["under_routed"], g["over_routed"], g["band_accuracy"]), (2, 0.5, 0.5, 0.0))

    def test_promote_decision_requires_a_valid_outcome_bar_when_holdouts_exist(self):
        import evaluate
        c = {"n": 100, "band_accuracy": 0.9, "under_routed": 0.1, "outward_recall": 1.0}
        bar = {"band_accuracy": 0.9, "under_routed": 0.1, "outward_recall": 1.0}
        under = {"n": 20, "under_routed": 0.5}                 # candidate under-routes the outcomes badly
        # outcome bar present + worse outcome under-routing → blocked
        self.assertEqual(evaluate.promote_decision(c, bar, {"under_routed": 0.0}, under, True)[0], False)
        # outcome holdouts exist but NO outcome bar (cache miss / grading failure) → blocked, not defaulted to OK
        self.assertEqual(evaluate.promote_decision(c, bar, None, under, True), (False, False))
        # outcome bar present and no regression → promotes, and records that the check ran
        self.assertEqual(evaluate.promote_decision(c, bar, {"under_routed": 0.5}, {"n": 20, "under_routed": 0.5}, True), (True, True))

    def test_promote_decision_unaffected_without_outcomes_and_never_promotes_without_a_bar(self):
        import evaluate
        c = {"n": 100, "band_accuracy": 0.9, "under_routed": 0.1, "outward_recall": 1.0}
        bar = {"band_accuracy": 0.9, "under_routed": 0.1, "outward_recall": 1.0}
        self.assertEqual(evaluate.promote_decision(c, bar, None, None, False), (True, False))   # no outcomes: teacher bar alone
        self.assertEqual(evaluate.promote_decision(c, None, None, None, False), (False, False)) # no bar at all: never promote


if __name__ == "__main__":
    unittest.main()
