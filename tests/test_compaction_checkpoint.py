"""Phase 0 compaction checkpoint: parsers, handoff selection/budgets, policy validation, CLI. Synthetic data only."""
import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest

from cortados_router.compaction import checkpoint, handoff, policy, transcript
from cortados_router.compaction.events import Message, Session, ToolEvent

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class ReviewRegressionTests(unittest.TestCase):
    """One test per finding of the b811196 review."""

    def setUp(self):
        self.pol = load_policy()

    def run_cli(self, *argv):
        return CliTests.run_cli(self, *argv)

    def test_1_codex_structured_result_kept(self):
        lines = [CODEX_LINES[0], CODEX_LINES[3],
                 {"type": "response_item", "payload": {"type": "function_call", "name": "spawn", "call_id": "k",
                                                       "arguments": "{}"}},
                 {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "k",
                                                       "output": json.dumps({"task_id": "task_1234abcd",
                                                                             "state": "running"})}}]
        s = transcript.read_codex(_write(lines))
        self.assertIn("task_1234abcd", s.events[0].result_text)
        self.assertEqual(json.loads(s.events[0].result_text)["state"], "running")
        self.assertFalse(s.events[0].interrupted)

    def test_2_rule_with_path_not_cut(self):
        msgs = (Message("s", 0, "user", "Never modify ~/.cortados/router.db. Always run python3 -B -m a.b.c first."),
                Message("s", 1, "user", "go"))
        md, _ = handoff.build_handoff(Session("s", "claude-code", msgs, ()), self.pol)
        rules = md[md.index("## Standing rules"):md.index("## Decisions")]
        self.assertIn("Never modify ~/.cortados/router.db.", rules)
        self.assertIn("Always run python3 -B -m a.b.c first.", rules)

    def test_3_force_write_never_wider_than_0600_and_no_symlink(self):
        d = tempfile.mkdtemp()
        md = os.path.join(d, "handoff.md")
        with open(md, "w") as fh:
            fh.write("old")
        os.chmod(md, 0o644)
        rc, _, _ = self.run_cli("build", "--transcript", _write(CLAUDE_LINES), "--out", d, "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(stat.S_IMODE(os.stat(md).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])
        d2 = tempfile.mkdtemp()
        target = os.path.join(d2, "elsewhere.txt")
        with open(target, "w") as fh:
            fh.write("keep")
        os.symlink(target, os.path.join(d2, "handoff.md"))
        rc, _, err = self.run_cli("build", "--transcript", _write(CLAUDE_LINES), "--out", d2, "--force")
        self.assertEqual(rc, 3)
        self.assertIn("symlink", err)
        self.assertEqual(_read(target), "keep")

    def test_4_goal_and_rules_caps_include_heading(self):
        md, man = handoff.build_handoff(synthetic_session(n_filler=60), self.pol)
        for key, sec in man["sections"].items():
            self.assertLessEqual(sec["tokens"], sec["budget"], key)
            self.assertFalse(sec["over_budget"], key)
            self.assertIsNone(sec["overflow"], key)

    def test_goal_overflow_goes_to_sibling_file(self):
        prompt = "START " + "goal words " * 300 + " END"
        s = Session("s", "claude-code", (Message("s", 0, "user", prompt),), ())
        md, man, extra = handoff.build_handoff_files(s, self.pol)
        sec = man["sections"]["goal"]
        self.assertLessEqual(sec["tokens"], sec["budget"])
        name = sec["overflow"]["file"]
        self.assertRegex(name, r"^handoff\.goal-full\.[0-9a-f]{16}\.md$")
        self.assertEqual(name, handoff.overflow_file_name("goal", extra[name]))  # content-addressed
        self.assertGreater(sec["overflow"]["full_tokens"], sec["budget"])
        goal = md[md.index("## Goal"):md.index("## Recency pin")]
        self.assertIn("START", goal)
        self.assertIn("END", goal)
        self.assertIn("full text: %s (%d tokens)" % (name, sec["overflow"]["full_tokens"]), goal)
        self.assertIn("read it before acting", goal)
        self.assertIn(prompt.strip(), extra[name])
        self.assertEqual([it["index"] for it in sec["items"]], [0])

    def test_rules_overflow_goes_to_sibling_file_via_cli(self):
        rules = " ".join("Never do thing number %d ever." % i for i in range(300))
        lines = [cc_line("user", rules), cc_line("user", "go")]
        d = tempfile.mkdtemp()
        rc, _, err = self.run_cli("build", "--transcript", _write(lines), "--out", d)
        self.assertEqual(rc, 0, err)
        man = json.loads(_read(os.path.join(d, "handoff.manifest.json")))
        name = man["sections"]["rules"]["overflow"]["file"]
        self.assertRegex(name, r"^handoff\.rules-full\.[0-9a-f]{16}\.md$")
        full = os.path.join(d, name)
        self.assertEqual(stat.S_IMODE(os.stat(full).st_mode), 0o600)
        self.assertIn("Never do thing number 299 ever.", _read(full))
        self.assertIn("Never do thing number 150 ever.", _read(full))
        self.assertLessEqual(man["sections"]["rules"]["tokens"], man["sections"]["rules"]["budget"])
        self.assertIn("full text: %s " % full, _read(os.path.join(d, "handoff.md")))
        # a rebuild without --force refuses (overflow file exists too); with --force a short build drops the stale file
        rc, _, _ = self.run_cli("build", "--transcript", _write(lines), "--out", d)
        self.assertEqual(rc, 3)
        rc, _, _ = self.run_cli("build", "--transcript", _write(CLAUDE_LINES), "--out", d, "--force")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(full))

    def test_overflow_pathological_cap_raises(self):
        pol = copy.deepcopy(self.pol)
        pol["section_budgets"]["goal"] = 10
        s = Session("s", "claude-code", (Message("s", 0, "user", "x" * 2000),), ())
        with self.assertRaisesRegex(handoff.HandoffBudgetError, "cannot hold"):
            handoff.build_handoff(s, pol)

    def test_5_caps_apply_without_window(self):
        level, _ = checkpoint.classify(self.pol, 31, 310000, None)
        self.assertEqual(level, "force")
        level, _ = checkpoint.classify(self.pol, None, 210000, None)
        self.assertEqual(level, "checkpoint")
        level, _ = checkpoint.classify(self.pol, None, 95000, None, model="api/claude-haiku-5-5")
        self.assertEqual(level, "force")

    def test_6_circuit_breaker_from_restart_log(self):
        log = os.path.join(tempfile.mkdtemp(), "restarts.jsonl")
        now = 10000.0
        for ts in (now - 600, now - 300):
            self.assertEqual(self.run_cli("record-restart", "--restart-log", log, "--forced",
                                          "--now", str(ts))[0], 0)
        st = CliTests.status(self, total_input_tokens=150000, context_window_size=200000)
        rc, out, _ = self.run_cli("check", "--status-file", st, "--restart-log", log, "--now", str(now))
        self.assertTrue(out.startswith("escalate"), out)
        rc, out, _ = self.run_cli("check", "--status-file", st, "--restart-log", log, "--now", str(now + 3600))
        self.assertTrue(out.startswith("checkpoint"), out)  # outside the 30-min window
        ok = CliTests.status(self, total_input_tokens=1000, context_window_size=200000)
        rc, out, _ = self.run_cli("check", "--status-file", ok, "--restart-log", log, "--now", str(now))
        self.assertTrue(out.startswith("ok"), out)  # breaker only blocks a restart recommendation

    def test_7_statusline_passes_idle_seconds(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, CORTADOS_HOME=home, REPO=REPO, CORTADOS_CACHE_TTL_CLASS="claude-subscription")
        script = os.path.join(REPO, "integrations", "checkpoint", "statusline.sh")

        def run(tokens):
            payload = json.dumps({"session_id": "idle", "context_window": {"total_input_tokens": tokens,
                                                                           "context_window_size": 200000}})
            return subprocess.run(["sh", script], input=payload, env=env, capture_output=True, text=True,
                                  check=True).stdout
        self.assertTrue(run(20000).startswith("ok"))  # first (fresh) sample creates the history
        hist = os.path.join(home, "checkpoint", "idle.history")
        with open(hist, "w") as fh:
            fh.write("111000\n")  # a previous turn end at >= warn (110k on a 200k window)
        self.assertTrue(run(111500).startswith("warn"))  # just active: no idle checkpoint
        old = time.time() - 4000  # longer than the 1 h subscription cache TTL
        os.utime(hist, (old, old))
        out = run(112000)
        self.assertTrue(out.startswith("checkpoint idle"), out)

    def test_8_handoff_id_covers_tool_results_and_policy(self):
        a = synthetic_session()
        evs = list(a.events)
        evs[0] = ToolEvent("s", 0, evs[0].message_index, "Edit", {"file_path": "src/exporter.py"}, "different",
                           9, False, False)
        b = Session(a.session, a.source, a.messages, tuple(evs))
        ida = handoff.build_handoff(a, self.pol)[1]["handoff_id"]
        idb = handoff.build_handoff(b, self.pol)[1]["handoff_id"]
        self.assertNotEqual(ida, idb)
        pol2 = copy.deepcopy(self.pol)
        pol2["recency_bonus"] = 5
        self.assertNotEqual(ida, handoff.build_handoff(a, pol2)[1]["handoff_id"])
        md, man = handoff.build_handoff(a, self.pol)
        self.assertIn("handoff-id: %s" % man["handoff_id"], md)
        self.assertNotIn(handoff.ID_PLACEHOLDER, md)

    def test_9_counts_must_be_positive_ints(self):
        raw = json.loads(_read(policy.DEFAULT_PATH))
        for setting, key, bad in (("velocity", "lookback_turns", 0.5), ("velocity", "lookback_turns", 0),
                                  ("velocity", "projection_turns", 1.5), ("circuit_breaker", "max_forced_restarts", 0),
                                  ("circuit_breaker", "window_s", 1.5), ("pruning", "keep_last_results", 2.5),
                                  ("recency_pin", "tool_calls", 1.5), ("velocity", "single_turn_pct_of_window", 100)):
            r = copy.deepcopy(raw)
            r[setting]["value"][key] = bad
            with self.assertRaises(policy.PolicyError, msg="%s.%s=%r" % (setting, key, bad)):
                policy.validate(r)
        r = copy.deepcopy(raw)
        r["velocity"]["value"]["lookback_turns"] = 1
        self.assertEqual(policy.validate(r)["velocity"]["lookback_turns"], 1)


class Round2RegressionTests(unittest.TestCase):
    """One test per finding of the eae738d review."""

    def setUp(self):
        self.pol = load_policy()

    def run_cli(self, *argv):
        return CliTests.run_cli(self, *argv)

    def test_1_rule_lines_kept_whole(self):
        msgs = (Message("s", 0, "user", "Never run cleanup from . or /important/production.\nunrelated line"),
                Message("s", 1, "user", "go\nnever run cleanup from .   or /important/production."))
        md, man = handoff.build_handoff(Session("s", "claude-code", msgs, ()), self.pol)
        rules = md[md.index("## Standing rules"):md.index("## Decisions")]
        self.assertIn("never run cleanup from .   or /important/production. [m1]", rules)
        # Round-3 exact-line dedup preserves both: case/internal whitespace can change a constraint.
        self.assertIn("Never run cleanup from . or /important/production. [m0]", rules)
        self.assertEqual(rules.lower().count("cleanup"), 2)
        self.assertNotIn("unrelated", rules)

    def test_2_structured_envelope_output_serialized(self):
        out = json.dumps({"output": [{"agent_id": "task_1234", "state": "running"}]})
        lines = [CODEX_LINES[0], CODEX_LINES[3],
                 {"type": "response_item", "payload": {"type": "function_call", "name": "spawn", "call_id": "k",
                                                       "arguments": "{}"}},
                 {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "k",
                                                       "output": out}}]
        s = transcript.read_codex(_write(lines))
        self.assertEqual(json.loads(s.events[0].result_text), [{"agent_id": "task_1234", "state": "running"}])
        self.assertEqual(transcript._result_text([{"type": "output_text", "text": "hi"}]), "hi")
        self.assertEqual(transcript._result_text([{"type": "text", "text": "a"}, {"type": "image"}]), "a\n[image]")
        self.assertIn("weird", transcript._result_text([{"type": "text", "text": "a"}, {"type": "weird"}]))

    def _overflowing_rules_lines(self):
        rules = " ".join("Never do thing number %d ever." % i for i in range(300))
        return [cc_line("user", rules), cc_line("user", "go")]

    def test_3_failed_force_rebuild_keeps_old_overflow(self):
        d = tempfile.mkdtemp()
        self.assertEqual(self.run_cli("build", "--transcript", _write(self._overflowing_rules_lines()),
                                      "--out", d)[0], 0)
        # Round-3 overflow files are content-addressed; follow the published pointer, not a fixed filename.
        full = json.loads(_read(os.path.join(d, "handoff.manifest.json")))["sections"]["rules"]["overflow"]["path"]
        real = checkpoint._write_private

        def failing(path, text, force):
            if path.endswith("handoff.manifest.json"):
                raise OSError("injected failure")
            return real(path, text, force)
        checkpoint._write_private = failing
        try:
            with self.assertRaises(OSError):
                self.run_cli("build", "--transcript", _write(CLAUDE_LINES), "--out", d, "--force")
        finally:
            checkpoint._write_private = real
        self.assertTrue(os.path.exists(full))
        self.assertEqual(self.run_cli("build", "--transcript", _write(CLAUDE_LINES), "--out", d, "--force")[0], 0)
        self.assertFalse(os.path.exists(full))

    def test_4_overflow_pointer_resolves_from_other_cwd(self):
        base = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(base)
        try:
            rc, out, _ = self.run_cli("build", "--transcript", _write(self._overflowing_rules_lines()),
                                      "--out", "rel-out")
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        md_path = out.split()[2]
        self.assertTrue(os.path.isabs(md_path))
        md = _read(md_path)
        os.chdir(tempfile.mkdtemp())  # the restarted session's cwd is somewhere else
        try:
            pointer = [ln for ln in md.splitlines() if ln.startswith("full text: ")][0]
            path = pointer[len("full text: "):].split(" (")[0]
            self.assertTrue(os.path.isabs(path))
            self.assertIn("Never do thing number 150 ever.", _read(path))
        finally:
            os.chdir(cwd)
        man = json.loads(_read(os.path.join(base, "rel-out", "handoff.manifest.json")))
        self.assertEqual(man["sections"]["rules"]["overflow"]["path"], path)

    def test_5_escalate_persists_until_ack(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, CORTADOS_HOME=home, REPO=REPO)
        script = os.path.join(REPO, "integrations", "checkpoint", "statusline.sh")

        def run(tokens):
            payload = json.dumps({"session_id": "b", "context_window": {"total_input_tokens": tokens,
                                                                        "context_window_size": 200000}})
            return subprocess.run(["sh", script], input=payload, env=env, capture_output=True, text=True,
                                  check=True).stdout
        self.assertTrue(run(80000).startswith("escalate fresh session"))  # > 50% of 140k on open
        for n in (80100, 80200):
            self.assertTrue(run(n).startswith("escalate breaker tripped"), n)
        breaker = os.path.join(home, "checkpoint", "breaker.json")
        self.assertEqual(stat.S_IMODE(os.stat(breaker).st_mode), 0o600)
        self.assertEqual(self.run_cli("ack", "--breaker-file", breaker)[0], 0)
        self.assertFalse(os.path.exists(breaker))
        self.assertTrue(run(80300).startswith("ok"))

    def test_6_section_caps_zero_or_heading_minimum(self):
        raw = json.loads(_read(policy.DEFAULT_PATH))
        r = copy.deepcopy(raw)
        r["section_budgets"]["value"]["files"] = 1
        with self.assertRaisesRegex(policy.PolicyError, "files=1"):
            policy.validate(r)
        r = copy.deepcopy(raw)
        r["section_budgets"]["value"]["goal"] = 0
        with self.assertRaisesRegex(policy.PolicyError, "goal"):
            policy.validate(r)
        r = copy.deepcopy(raw)
        r["section_budgets"]["value"]["files"] = 0
        pol = policy.validate(r)
        md, man = handoff.build_handoff(synthetic_session(), pol)
        self.assertNotIn("## Files touched", md)
        self.assertTrue(man["sections"]["files"]["disabled"])
        self.assertEqual(man["sections"]["files"]["items"], [])
        self.assertEqual(man["sections"]["files"]["tokens"], 0)
        need = handoff.section_min_tokens("files", min(pol["chars_per_token"].values()))
        r["section_budgets"]["value"]["files"] = need
        policy.validate(r)


class Round3RegressionTests(unittest.TestCase):
    """One test per finding of the df56ff0 review."""

    def setUp(self):
        self.pol = load_policy()

    def run_cli(self, *argv):
        return CliTests.run_cli(self, *argv)

    @staticmethod
    def _overflowing_rules_lines(template):
        return [cc_line("user", " ".join(template % i for i in range(300))), cc_line("user", "go")]

    def test_1_failed_overflow_to_overflow_rebuild_keeps_old_generation(self):
        old_lines = self._overflowing_rules_lines("Never do thing number %d ever.")
        new_lines = self._overflowing_rules_lines("Always do chore number %d first.")
        for fail_at in ("handoff.md", "handoff.manifest.json"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as d:
                self.assertEqual(self.run_cli("build", "--transcript", _write(old_lines), "--out", d)[0], 0)
                md_path = os.path.join(d, "handoff.md")
                man_path = os.path.join(d, "handoff.manifest.json")
                before, before_man = _read(md_path), _read(man_path)
                old_full = json.loads(before_man)["sections"]["rules"]["overflow"]["path"]
                old_text = _read(old_full)
                self.assertIn("full text: %s " % old_full, before)
                self.assertIn("Never do thing number 150 ever.", old_text)
                real = checkpoint._write_private
                written = []

                def failing(path, text, force):
                    if os.path.basename(path) == fail_at:
                        raise OSError("injected failure")
                    result = real(path, text, force)
                    written.append(path)
                    return result
                checkpoint._write_private = failing
                try:
                    with self.assertRaisesRegex(OSError, "injected failure"):
                        self.run_cli("build", "--transcript", _write(new_lines), "--out", d, "--force")
                finally:
                    checkpoint._write_private = real
                # The old manifest's pointer must still resolve to exactly its original bytes.
                self.assertEqual(_read(man_path), before_man)
                self.assertEqual(_read(old_full), old_text)
                new_full = next(p for p in written if "-full" in os.path.basename(p))
                self.assertNotEqual(new_full, old_full)
                self.assertIn("Always do chore number 150 first.", _read(new_full))
                if fail_at == "handoff.md":
                    self.assertEqual(_read(md_path), before)
                else:
                    # The markdown was published before the manifest failed: its NEW pointer must resolve too.
                    self.assertIn("full text: %s " % new_full, _read(md_path))
                # Cleanup must wait for BOTH publications, including during a retry.
                def publishing(path, text, force):
                    self.assertTrue(os.path.exists(old_full), path)
                    return real(path, text, force)
                checkpoint._write_private = publishing
                try:
                    rc = self.run_cli("build", "--transcript", _write(new_lines), "--out", d, "--force")[0]
                finally:
                    checkpoint._write_private = real
                self.assertEqual(rc, 0)
                man = json.loads(_read(man_path))
                self.assertEqual(man["sections"]["rules"]["overflow"]["path"], new_full)
                self.assertFalse(os.path.exists(old_full))
                self.assertIn("full text: %s " % new_full, _read(md_path))
                self.assertEqual(stat.S_IMODE(os.stat(new_full).st_mode), 0o600)
                self.assertEqual([f for f in os.listdir(d) if "-full" in f], [os.path.basename(new_full)])

    def test_2_rule_dedup_is_exact_line_only(self):
        msgs = (Message("s", 0, "user", "Never modify `Token`.\nAlways print `a  b` verbatim.\nOnly use \"Foo\" here."),
                Message("s", 1, "user", "Never modify `token`.\nAlways print `a b` verbatim.\nOnly use \"Foo\" here."),
                Message("s", 2, "user", "go"))
        md, man = handoff.build_handoff(Session("s", "claude-code", msgs, ()), self.pol)
        rules = md[md.index("## Standing rules"):md.index("## Decisions")]
        # distinct identifiers (case) and distinct literals (internal whitespace) are distinct constraints: all kept
        self.assertIn("- Never modify `Token`. [m0]", rules)
        self.assertIn("- Never modify `token`. [m1]", rules)
        self.assertIn("- Always print `a  b` verbatim. [m0]", rules)
        self.assertIn("- Always print `a b` verbatim. [m1]", rules)
        # an identical line is still deduplicated, newest wins
        self.assertEqual(rules.count('Only use "Foo" here.'), 1)
        self.assertIn('- Only use "Foo" here. [m1]', rules)
        self.assertEqual([it["index"] for it in man["sections"]["rules"]["items"]], [0, 0, 1, 1, 1])

    def test_3_unchanged_refresh_does_not_clear_velocity_checkpoint(self):
        # the current sample equals the last recorded turn end: the same turn, not a zero-growth one
        level, reason = checkpoint.classify(self.pol, None, 35000, 200000, history=[10000, 35000])
        self.assertEqual((level, reason.startswith("velocity: last turn added 25000")), ("checkpoint", True), reason)
        level, _ = checkpoint.classify(self.pol, None, 36000, 200000, history=[10000, 35000])
        self.assertEqual(level, "ok")  # a real next turn that added little: the velocity verdict is over
        home = tempfile.mkdtemp()
        env = dict(os.environ, CORTADOS_HOME=home, REPO=REPO)
        script = os.path.join(REPO, "integrations", "checkpoint", "statusline.sh")

        def run(tokens):
            payload = json.dumps({"session_id": "v", "context_window": {"total_input_tokens": tokens,
                                                                        "context_window_size": 200000}})
            return subprocess.run(["sh", script], input=payload, env=env, capture_output=True, text=True,
                                  check=True).stdout.split()[0]
        self.assertEqual([run(n) for n in (10000, 35000, 35000)], ["ok", "checkpoint", "checkpoint"])
        hist = _read(os.path.join(home, "checkpoint", "v.history")).split()
        self.assertEqual(hist, ["10000", "35000"])  # the unchanged refresh did not become a turn


class Round4RegressionTests(unittest.TestCase):
    """Findings of the 6e237ac review."""

    def test_1_overlapping_builds_never_delete_a_live_overflow_file(self):
        lines_a = Round3RegressionTests._overflowing_rules_lines("Never do thing number %d ever.")
        lines_b = Round3RegressionTests._overflowing_rules_lines("Always do chore number %d first.")
        tr_a, tr_b = _write(lines_a), _write(lines_b)
        d = tempfile.mkdtemp()
        staged, release = threading.Event(), threading.Event()
        real = checkpoint._write_private
        results, errors = {}, {}

        def pausing(path, text, force):
            result = real(path, text, force)
            # Build A stops right after staging its overflow file, before handoff.md is published.
            if threading.current_thread().name == "build-a" and "-full" in os.path.basename(path):
                staged.set()
                release.wait(10)  # bound so a broken test cannot hang the suite
            return result

        def build(name, tr):
            try:
                results[name] = checkpoint.main(["build", "--transcript", tr, "--out", d, "--force"])
            except BaseException as exc:  # recorded and asserted on below, never swallowed
                errors[name] = exc

        a = threading.Thread(target=build, args=("a", tr_a), name="build-a")
        b = threading.Thread(target=build, args=("b", tr_b), name="build-b")
        checkpoint._write_private = pausing
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                a.start()
                self.assertTrue(staged.wait(10), "build A never staged its overflow file")
                b.start()
                b.join(1.0)  # without the lock, B runs to completion here and deletes A's staged file
                release.set()
                a.join(10)
                b.join(10)
        finally:
            release.set()
            checkpoint._write_private = real
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, {})
        self.assertEqual(results, {"a": 0, "b": 0})
        man = json.loads(_read(os.path.join(d, "handoff.manifest.json")))
        full = man["sections"]["rules"]["overflow"]["path"]
        self.assertTrue(os.path.exists(full), "published manifest points at a deleted overflow file")
        self.assertIn("full text: %s " % full, _read(os.path.join(d, "handoff.md")))
        self.assertEqual([f for f in os.listdir(d) if "-full" in f], [os.path.basename(full)])

    def test_2_lock_timeout_refuses(self):
        d = tempfile.mkdtemp()
        with checkpoint._out_dir_lock(d):
            with self.assertRaisesRegex(checkpoint.WriteRefused, "another build holds"):
                with checkpoint._out_dir_lock(d, timeout_s=0.1):
                    pass


if __name__ == "__main__":
    unittest.main()
