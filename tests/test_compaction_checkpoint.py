"""Phase 0 compaction checkpoint: parsers, handoff selection/budgets, policy validation, CLI. Synthetic data only."""
import contextlib
import copy
import io
import json
import os
import stat
import tempfile
import unittest

from cortados_router.compaction import checkpoint, handoff, policy, transcript
from cortados_router.compaction.events import Message, Session, ToolEvent


def _write(lines):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for ln in lines:
            fh.write((ln if isinstance(ln, str) else json.dumps(ln)) + "\n")
    return path


def cc_line(t, content, **kw):
    d = {"type": t, "sessionId": "sess-cc", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": t, "content": content}}
    d.update(kw)
    return d


CLAUDE_LINES = [
    {"type": "summary", "summary": "old"},
    cc_line("user", "Please fix the parser in foo.py. Never push to main."),
    cc_line("assistant", [{"type": "thinking", "thinking": "x", "signature": "s"},
                          {"type": "text", "text": "I decided to edit foo.py because it is broken."}]),
    cc_line("assistant", [{"type": "tool_use", "id": "t1", "name": "Edit",
                           "input": {"file_path": "/repo/foo.py", "old_string": "a", "new_string": "b"}}]),
    cc_line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
    cc_line("assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest"}}]),
    cc_line("user", [{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                      "content": [{"type": "text", "text": "1 failed"}]}]),
    cc_line("user", "side", isSidechain=True),
    cc_line("user", "injected", isMeta=True),
    {"type": "weird-new-type", "x": 1},
    "not json",
    cc_line("assistant", [{"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "/repo/bar.py",
                                                                                      "content": "x"}}]),
]

CODEX_LINES = [
    {"type": "session_meta", "payload": {"id": "sess-cx", "cwd": "/repo"}},
    {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                          "content": [{"type": "input_text", "text": "sys"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "Add a test. Always run it."}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                                          "arguments": json.dumps({"cmd": "ls"})}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                                          "output": json.dumps({"output": "a b", "metadata": {"exit_code": 1}})}},
    {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2",
                                          "input": "*** Begin Patch\n*** Add File: tests/test_x.py\n+x\n*** End Patch"}},
    {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done"}},
    {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
    {"type": "compacted", "payload": {"message": "summary"}},
    {"type": "event_msg", "payload": {"type": "token_count"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": "Committed abc1234f."}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c3",
                                          "arguments": "{}"}},
]


def _read(path):
    with open(path) as fh:
        return fh.read()


def load_policy():
    return policy.load()


class ParserTests(unittest.TestCase):
    def test_claude_code(self):
        s, st = transcript.parse(_write(CLAUDE_LINES))
        self.assertEqual(s.source, "claude-code")
        self.assertEqual(s.session, "sess-cc")
        self.assertEqual([m.role for m in s.messages], ["user", "assistant"])
        self.assertIn("decided", s.messages[1].text)
        self.assertEqual(s.messages[1].tool_event_indexes, (0, 1, 2))
        e0, e1, e2 = s.events
        self.assertEqual((e0.result_text, e0.interrupted, e0.is_error), ("ok", False, False))
        self.assertTrue(e1.is_error)
        self.assertEqual(e1.result_text, "1 failed")
        self.assertTrue(e2.interrupted)
        self.assertEqual(st["interrupted"], 1)
        for k in ("type:summary", "type:weird-new-type", "bad_json", "sidechain", "meta", "block:thinking"):
            self.assertEqual(st["skipped"].get(k), 1, k)

    def test_codex(self):
        s, st = transcript.parse(_write(CODEX_LINES))
        self.assertEqual((s.source, s.session), ("codex", "sess-cx"))
        self.assertEqual([m.role for m in s.messages], ["user", "assistant"])
        self.assertEqual(s.messages[0].text, "Add a test. Always run it.")
        self.assertEqual(len(s.events), 3)
        self.assertTrue(s.events[0].is_error)
        self.assertEqual(s.events[0].result_text, "a b")
        self.assertEqual(s.events[1].result_text, "Done")
        self.assertTrue(s.events[2].interrupted)
        self.assertEqual(st["counts"]["compacted"], 1)
        self.assertEqual(st["skipped"]["role:developer"], 1)
        self.assertEqual(st["skipped"]["meta"], 1)
        self.assertEqual(st["skipped"]["item:reasoning"], 1)
        self.assertEqual(st["skipped"]["type:event_msg"], 1)

    def test_read_helpers_and_detect(self):
        self.assertEqual(transcript.read_codex(_write(CODEX_LINES)).source, "codex")
        self.assertEqual(transcript.read_claude_code(_write(CLAUDE_LINES)).source, "claude-code")
        with self.assertRaises(ValueError):
            transcript.parse(_write(["nope"]))


def synthetic_session(n_filler=0, rule_text=None):
    msgs, evs = [], []

    def add(role, text, events=()):
        msgs.append(Message("s", len(msgs), role, text, tuple(events)))

    add("user", "Build the widget exporter. You must never delete data.")
    add("assistant", "We chose sqlite because it is stdlib.")
    for i in range(n_filler):
        add("user" if i % 2 else "assistant", "filler message %d about nothing in particular %s" % (i, "z" * 200))
    add("user", rule_text or "Do not touch the widget exporter tests. Never delete data.")
    evs.append(ToolEvent("s", 0, len(msgs), "Edit", {"file_path": "src/exporter.py"}, "updated", 7, False, False))
    evs.append(ToolEvent("s", 1, len(msgs), "Edit", {"file_path": "src/exporter.py"}, "updated", 7, False, False))
    evs.append(ToolEvent("s", 2, len(msgs), "Bash", {"command": "git checkout -b feat/exporter"}, "", 0, False, True))
    add("assistant", "Working on branch feat/exporter, commit 1a2b3c4d, PR #42.", events=(0, 1, 2))
    add("user", "Now export the widget data to csv.")
    return Session("s", "test", tuple(msgs), tuple(evs))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.pol = load_policy()

    def test_sections_and_manifest(self):
        s = synthetic_session()
        md, man = handoff.build_handoff(s, self.pol)
        order = [md.index(h) for h in ("## Goal", "## Recency pin", "## Standing rules", "## Decisions", "## In-flight",
                                       "## Files touched", "## Ranked excerpts")]
        self.assertEqual(order, sorted(order))
        self.assertIn("handoff-id: %s" % man["handoff_id"], md)
        goal = [it["index"] for it in man["sections"]["goal"]["items"]]
        self.assertEqual(goal, [4])
        pin = man["sections"]["recency_pin"]["items"]
        self.assertEqual([(it["kind"], it["index"]) for it in pin],
                         [("message", 0), ("message", 2), ("event", 1), ("event", 2)])
        self.assertIn("(no result: interrupted)", md)
        excerpt_keys = {(it["kind"], it["index"]) for it in man["sections"]["excerpts"]["items"]}
        self.assertFalse(excerpt_keys & {("message", 0), ("message", 2), ("message", 4), ("event", 1)})
        # newest wins: "Never delete data." appears in m0 and m2; kept once, attributed to m2
        rules = md[md.index("## Standing rules"):md.index("## Decisions")]
        self.assertEqual(rules.count("Never delete data."), 1)
        self.assertIn("Never delete data. [m2]", md)
        self.assertIn("We chose sqlite because", md)
        self.assertIn("VERIFY", md)
        for tok in ("branch feat/exporter", "pr 42", "commit 1a2b3c4d"):
            self.assertIn(tok, md)
        self.assertIn("`src/exporter.py` x2 (Edit)", md)
        files = man["sections"]["files"]["items"]
        self.assertEqual(files[0]["indexes"], [0, 1])
        for it in man["sections"]["excerpts"]["items"]:
            self.assertIn("score", it)
        self.assertEqual(man["token_estimate"], handoff.ESTIMATE_NOTE)

    def test_deterministic(self):
        s = synthetic_session(n_filler=20)
        a = handoff.build_handoff(s, self.pol)
        b = handoff.build_handoff(s, self.pol)
        self.assertEqual(a[0], b[0])
        self.assertEqual(json.dumps(a[1], sort_keys=True), json.dumps(b[1], sort_keys=True))

    def test_excerpt_budget_and_chronology(self):
        pol = copy.deepcopy(self.pol)
        pol["section_budgets"]["excerpts"] = 200
        md, man = handoff.build_handoff(synthetic_session(n_filler=40), pol)
        sec = man["sections"]["excerpts"]
        self.assertLessEqual(sec["tokens"], 200 + 10)
        self.assertGreater(sec["dropped"], 0)
        idx = [it["index"] for it in sec["items"] if it["kind"] == "message"]
        self.assertEqual(idx, sorted(idx))

    def test_total_budget_trims_excerpts_first(self):
        pol = copy.deepcopy(self.pol)
        pol["handoff_total_tokens"] = 500
        md, man = handoff.build_handoff(synthetic_session(n_filler=40), pol)
        self.assertLessEqual(handoff.est_tokens(md), 500)
        self.assertGreater(man["sections"]["excerpts"]["dropped"], 0)
        self.assertTrue(man["sections"]["goal"]["items"])
        self.assertTrue(man["sections"]["rules"]["items"])

    def test_goal_and_rules_never_trimmed(self):
        pol = copy.deepcopy(self.pol)
        pol["handoff_total_tokens"] = 50
        with self.assertRaises(handoff.HandoffBudgetError):
            handoff.build_handoff(synthetic_session(rule_text="You must always " + "check " * 400), pol)

    def test_est_tokens_per_format(self):
        self.assertEqual(handoff.est_tokens("abcde", 2.5), 2)
        self.assertEqual(handoff.est_tokens("abcdefghi", 4), 3)
        self.assertEqual(handoff.est_tokens("", 4), 0)
        s = synthetic_session()
        _, man = handoff.build_handoff(s, self.pol)  # source "test" -> unknown -> conservative 2.5
        self.assertEqual(man["chars_per_token"], 2.5)
        cx = Session(s.session, "codex", s.messages, s.events)
        _, man = handoff.build_handoff(cx, self.pol)
        self.assertEqual(man["chars_per_token"], 4)

    def test_files_cap_and_head_tail(self):
        pol = copy.deepcopy(self.pol)
        pol["files_max"] = 1
        pol["recency_pin"] = {"user_turns": 3, "tool_calls": 0}
        msgs = (Message("s", 0, "user", "go"), Message("s", 1, "assistant", "x", (0, 1, 2, 3, 4)))
        long = "H" * 300 + "M" * 1000 + "T" * 300
        evs = tuple(ToolEvent("s", i, 1, "Write", {"file_path": "f%d.py" % i}, long, len(long), False, False)
                    for i in range(5))
        md, man = handoff.build_handoff(Session("s", "codex", msgs, evs), pol)
        self.assertEqual(len(man["sections"]["files"]["items"]), 1)
        self.assertEqual(man["sections"]["files"]["dropped"], 4)
        ex = md[md.index("## Ranked excerpts"):]
        self.assertIn("(1600 chars)", ex)
        self.assertIn("of 1600 chars cut", ex)
        self.assertIn("T" * 160, ex)


class PolicyTests(unittest.TestCase):
    def raw(self):
        return json.loads(_read(policy.DEFAULT_PATH))

    def test_loads(self):
        p = policy.load()
        self.assertEqual(p["status"], "researched-2026-11-08")
        self.assertEqual(p["checkpoint"], {"pct": 70, "cap_tokens": 200000})
        self.assertEqual(p["handoff_total_tokens"], 16000)
        self.assertIn("compaction-prune.json", self.raw()["pruning"]["why"])

    def test_thresholds(self):
        p = policy.load()
        self.assertEqual(policy.thresholds(p, 200000), {"warn": 110000, "checkpoint": 140000, "force": 170000})
        self.assertEqual(policy.thresholds(p, 1000000), {"warn": 150000, "checkpoint": 200000, "force": 300000})
        self.assertEqual(policy.thresholds(p, 500000, "api/gpt-6.1-sol")["force"], 262000)
        haiku = policy.thresholds(p, 200000, "api/claude-haiku-5-5")
        self.assertEqual(haiku["force"], 90000)
        self.assertLessEqual(haiku["checkpoint"], haiku["force"])

    def test_every_value_has_why(self):
        for k, v in self.raw().items():
            if k not in policy.META_KEYS:
                self.assertTrue(v["why"].strip(), k)

    def test_unknown_missing_and_bad(self):
        r = self.raw()
        r["surprise"] = {"value": 1, "why": "x"}
        with self.assertRaisesRegex(policy.PolicyError, "unknown"):
            policy.validate(r)
        r = self.raw()
        del r["force"]
        with self.assertRaisesRegex(policy.PolicyError, "missing"):
            policy.validate(r)
        r = self.raw()
        r["warn"]["why"] = ""
        with self.assertRaisesRegex(policy.PolicyError, "reason"):
            policy.validate(r)
        r = self.raw()
        r["warn"]["value"]["pct"] = 90
        with self.assertRaisesRegex(policy.PolicyError, "warn.pct <"):
            policy.validate(r)
        r = self.raw()
        r["section_budgets"]["value"]["extra"] = 1
        with self.assertRaisesRegex(policy.PolicyError, "section_budgets"):
            policy.validate(r)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = checkpoint.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_build_and_overwrite_refusal(self):
        d = tempfile.mkdtemp()
        tr = _write(CLAUDE_LINES)
        rc, out, _ = self.run_cli("build", "--transcript", tr, "--out", d)
        self.assertEqual(rc, 0)
        md = os.path.join(d, "handoff.md")
        man = json.loads(_read(os.path.join(d, "handoff.manifest.json")))
        self.assertEqual(stat.S_IMODE(os.stat(md).st_mode), 0o600)
        self.assertIn(man["handoff_id"], out)
        self.assertEqual(man["parse_stats"]["skipped"]["bad_json"], 1)
        before = _read(md)
        rc, _, err = self.run_cli("build", "--transcript", _write(CODEX_LINES), "--out", d)
        self.assertEqual(rc, 3)
        self.assertIn("refusing", err)
        self.assertEqual(_read(md), before)
        rc, _, _ = self.run_cli("build", "--transcript", _write(CODEX_LINES), "--format", "codex", "--out", d,
                                "--force")
        self.assertEqual(rc, 0)
        self.assertNotEqual(_read(md), before)

    def status(self, **cw):
        return _write([{"session_id": "x", "context_window": cw}])

    def check(self, *extra, **cw):
        return self.run_cli("check", "--status-file", self.status(**cw), *extra)

    def test_check_levels_200k_binds_on_pct(self):
        for used, want in ((50000, "ok"), (115000, "warn"), (145000, "checkpoint"), (175000, "force")):
            rc, out, _ = self.check(total_input_tokens=used, context_window_size=200000)
            self.assertEqual((rc, out.split()[0]), (0, want), used)

    def test_check_levels_1m_binds_on_caps(self):
        for used, want in ((140000, "ok"), (160000, "warn"), (210000, "checkpoint"), (310000, "force")):
            rc, out, _ = self.check(context_window_size=1000000,
                                    current_usage={"input_tokens": 10, "cache_read_input_tokens": used - 10})
            self.assertEqual(out.split()[0], want, used)
        rc, out, _ = self.check(used_percentage=30, context_window_size=1000000)  # 300k from the percentage
        self.assertEqual(out.split()[0], "force")

    def test_check_price_cliff(self):
        rc, out, _ = self.check("--model", "api/gpt-6.1-sol", total_input_tokens=265000, context_window_size=500000)
        self.assertEqual(out.split()[0], "force")
        rc, out, _ = self.check("--model", "sub/opus", total_input_tokens=265000, context_window_size=500000)
        self.assertEqual(out.split()[0], "checkpoint")

    def test_check_velocity(self):
        hist = _write(["60000", "80000", "100000"])
        rc, out, _ = self.check("--history-file", hist, total_input_tokens=120000, context_window_size=1000000)
        self.assertTrue(out.startswith("ok"), out)  # 120k + 2 x 20k = 160k < 300k
        hist = _write(["20000", "60000", "100000"])
        rc, out, _ = self.check("--history-file", hist, total_input_tokens=135000, context_window_size=200000)
        self.assertTrue(out.startswith("checkpoint velocity"), out)  # 135k + 2 x ~38k >= 170k
        hist = _write(["10000"])
        rc, out, _ = self.check("--history-file", hist, total_input_tokens=35000, context_window_size=200000)
        self.assertTrue(out.startswith("checkpoint velocity: last turn"), out)  # +25k >= 10% of 200k

    def test_check_idle_ttl_and_fresh(self):
        rc, out, _ = self.check("--idle-seconds", "4000", "--cache-ttl-class", "claude-subscription",
                                total_input_tokens=115000, context_window_size=200000)
        self.assertTrue(out.startswith("checkpoint idle"), out)
        rc, out, _ = self.check("--idle-seconds", "100", "--cache-ttl-class", "claude-subscription",
                                total_input_tokens=115000, context_window_size=200000)
        self.assertTrue(out.startswith("warn"), out)
        rc, _, err = self.check("--cache-ttl-class", "nope", total_input_tokens=115000, context_window_size=200000)
        self.assertEqual(rc, 2)
        rc, out, _ = self.check("--fresh", total_input_tokens=75000, context_window_size=200000)
        self.assertTrue(out.startswith("escalate"), out)  # > 50% of 140k
        rc, out, _ = self.check("--fresh", total_input_tokens=20000, context_window_size=200000)
        self.assertTrue(out.startswith("ok"), out)

    def test_check_missing_and_pct_only(self):
        rc, out, err = self.check(context_window_size=200000)
        self.assertEqual(rc, 2)
        self.assertIn("error", err)
        rc, out, _ = self.check(used_percentage=72)
        self.assertTrue(out.startswith("checkpoint") and "window unknown" in out, out)


if __name__ == "__main__":
    unittest.main()
