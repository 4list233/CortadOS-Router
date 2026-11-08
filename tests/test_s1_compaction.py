import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path

from cortados_router.compaction.events import KeepDecision, Message, Session, ToolEvent
from cortados_router.compaction.labels import label_session

S1 = Path(__file__).resolve().parents[1] / "s1"
sys.path.insert(0, str(S1))
import compaction_dataset as cd  # noqa: E402
import compaction_eval as ce  # noqa: E402
import compaction_train as ct  # noqa: E402

SECRET_LATER = "LATER-TURN-ONLY-TEXT-should-never-reach-the-prompt"


def make_session(sid: str, n_events: int = 3) -> Session:
    msgs = [Message(sid, 0, "user", "goal one"), Message(sid, 1, "assistant", "reading")]
    events = []
    for i in range(n_events):
        events.append(ToolEvent(sid, i, 1, "Read", {"file_path": f"/r/{sid}/{i}.py"}, "x" * 3000 if i == 0 else "ok",
                                3000 if i == 0 else 2, False, False))
    msgs += [Message(sid, 2, "user", "goal two"), Message(sid, 3, "assistant", f"see /r/{sid}/0.py")]
    events.append(ToolEvent(sid, n_events, 3, "Read", {"file_path": f"/r/{sid}/0.py"}, "x", 1, False, False))
    msgs += [Message(sid, 4, "assistant", SECRET_LATER)]   # after every labelled event's issuing message
    msgs += [Message(sid, k, "assistant", "filler") for k in range(5, 9)]
    events.append(ToolEvent(sid, n_events + 1, 8, "Read", {"file_path": "/tail"}, "t", 1, False, False))
    return Session(sid, "test", tuple(msgs), tuple(events))


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.sessions = [make_session(f"s{i}") for i in range(40)]
        self.labels = [r for s in self.sessions for r in label_session(s)]
        self.out = cd.build(self.sessions, self.labels)

    def test_no_label_leakage(self):
        for split in ("train", "valid", "test"):
            for ex in self.out[split]:
                self.assertNotIn(SECRET_LATER, ex["messages"][1]["content"])

    def test_state_truncation_and_goal(self):
        s = self.sessions[0]
        st = cd.state_text(s, s.events[0])
        self.assertIn("chars elided", st)
        self.assertIn("goal one", st)
        self.assertNotIn("goal two", st)          # issued before the second prompt

    def test_split_by_session(self):
        where = {}
        for r in self.out["test_rows"]:
            where.setdefault(r["session"], set()).add("test")
        for sid in where:
            self.assertEqual(cd.split_of(sid), "test")
        self.assertEqual(cd.split_of("abc"), cd.split_of("abc"))
        self.assertEqual(sum(self.out["stats"]["sessions"].values()), 40)

    def test_chat_format_and_answers(self):
        ex = self.out["train"][0]["messages"]
        self.assertEqual([m["role"] for m in ex], ["system", "user", "assistant"])
        self.assertIn(ex[2]["content"], ("yes", "no"))

    def test_minority_upweighted_in_train_only(self):
        st = self.out["stats"]
        self.assertGreaterEqual(st["reps"]["result"]["reps"], 1)
        self.assertLessEqual(st["reps"]["result"]["reps"], cd.MAX_REPS)
        n_valid_events = st["events"].get("valid", 0)
        self.assertEqual(len(self.out["valid"]), 2 * n_valid_events)

    def test_unknown_labels_skipped(self):
        self.assertGreater(self.out["stats"]["skipped_unknown"], 0)

    def test_session_roundtrip_and_write(self):
        s = self.sessions[0]
        self.assertEqual(cd.session_from_dict(json.loads(json.dumps(cd.session_to_dict(s)))), s)
        with tempfile.TemporaryDirectory() as d:
            cd.write(self.out, Path(d))
            self.assertTrue((Path(d) / "train.jsonl").is_file())
            self.assertIn("examples", json.loads((Path(d) / "stats.json").read_text()))


def eval_rows(n):
    return [{"session": "a", "event_index": i, "result_chars": 100, "input_chars": 10,
             "needed_call": i % 2 == 0, "needed_result": i % 4 == 0} for i in range(n)]


class EvalTests(unittest.TestCase):
    rows = eval_rows(80)     # >= MIN_ROWS, 20 needed results

    def test_perfect_scorer(self):
        rep = ce.evaluate(self.rows, lambda r, h: 1.0 if r[ce.HEADS[h]] else 0.0)
        self.assertEqual(rep["heads"]["result"]["auc"], 1.0)
        self.assertEqual(rep["heads"]["result"]["brier"], 0.0)
        m = rep["thresholds"]["0.5"]
        self.assertEqual(m["wrong_drop_rate"], 0.0)
        self.assertEqual(m["wrong_call_drops"], 0)
        self.assertEqual(m["chars_saved"], 60 * 100 + 40 * 10)
        self.assertEqual(sum(m["mix"].values()), 80)

    def test_constant_scorer_and_rules_comparison(self):
        rules = [KeepDecision("a", i, True, False, "rule:x", "r") for i in range(80)]
        rep = ce.evaluate(self.rows, lambda r, h: 0.5, rules)
        self.assertEqual(rep["heads"]["call"]["auc"], 0.5)
        self.assertEqual(rep["rules"]["dropped_results"], 80)
        self.assertEqual(rep["rules"]["wrong_drop_rate"], 0.25)
        self.assertEqual(rep["switch_on"], [])       # 0.7 drops needed calls; 0.3/0.5 keep all, saving less
        with tempfile.TemporaryDirectory() as d:
            ce.write(rep, Path(d))
            self.assertIn("Heads beat the rules", (Path(d) / "eval.md").read_text())
            self.assertEqual(json.loads((Path(d) / "eval.json").read_text())["n"], 80)

    def test_dropping_needed_side_effect_calls_blocks_switch_on(self):
        # Regression: heads that drop every call but keep needed results had 0 wrong (result) drops and were approved.
        rules = [KeepDecision("a", i, True, True, "rule:keep-all", "r") for i in range(80)]
        scorer = lambda r, h: (0.0 if h == "call" else (1.0 if r["needed_result"] else 0.0))
        rep = ce.evaluate(self.rows, scorer, rules)
        m = rep["thresholds"]["0.5"]
        self.assertEqual(m["wrong_drops"], 0)
        self.assertEqual(m["wrong_call_drops"], 20)      # needed_call rows that are not needed_result
        self.assertNotIn("0.5", rep["switch_on"])
        # the same heads keeping needed calls are switched on: fewer wrong drops, at least as many chars
        good = lambda r, h: 1.0 if r[ce.HEADS[h]] else 0.0
        self.assertIn("0.5", ce.evaluate(self.rows, good, rules)["switch_on"])

    def test_empty_or_small_eval_is_insufficient(self):
        for rows in ([], eval_rows(ce.MIN_ROWS - 1)):
            rules = [KeepDecision("a", r["event_index"], True, True, "rule:x", "r") for r in rows]
            rep = ce.evaluate(rows, lambda r, h: 0.0, rules)
            self.assertEqual(rep["switch_on"], [])
            self.assertIn("insufficient evidence", rep["switch_on_note"])
            self.assertIn("insufficient evidence", ce.markdown(rep))

    def test_auc_none_without_both_classes(self):
        self.assertIsNone(ce.auc([0.1, 0.2], [True, True]))


class TrainScriptTests(unittest.TestCase):
    def run_main(self, argv, allowed):
        policy = lambda now: (allowed, "inside" if allowed else "training skipped: outside night window", None)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ct.main(argv, now=datetime(2026, 11, 8, 12, 0), policy=policy)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_prints_plan_and_trains_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, _ = self.run_main(["--data", d, "--dry-run"], allowed=False)
            self.assertEqual(rc, 0)
            plan = json.loads(out)
            self.assertFalse(plan["policy"]["allowed"])
            self.assertIn("cortados_router.studio_capacity", plan["command"])
            self.assertIn("compaction-20261108-1200", plan["out"])
            self.assertNotIn("current.json", " ".join(plan["command"]))

    def test_refuses_outside_window(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self.run_main(["--data", d], allowed=False)
            self.assertEqual(rc, ct.REFUSED)
            self.assertIn("outside night window", err)

    def test_refuses_when_env_forbids(self):
        os.environ["CORTADOS_S1_TRAIN_ALLOWED"] = "0"
        try:
            with tempfile.TemporaryDirectory() as d:
                rc, _, _ = self.run_main(["--data", d], allowed=True)
                self.assertEqual(rc, ct.REFUSED)
        finally:
            del os.environ["CORTADOS_S1_TRAIN_ALLOWED"]

    def test_refuses_routing_dirs_and_missing_data(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self.run_main(["--data", d, "--out", str(S1 / "runs" / "2026-11-08")], allowed=True)
            self.assertEqual(rc, 2)
            self.assertIn("off limits", err)
            rc, _, err = self.run_main(["--data", d, "--out", str(S1 / "runs" / "compaction-test-nonexistent")],
                                       allowed=True)
            self.assertEqual(rc, 2)
            self.assertIn("missing train.jsonl", err)
            self.assertFalse((S1 / "runs" / "compaction-test-nonexistent").exists())

    def test_relative_paths_resolved_before_validation(self):
        with tempfile.TemporaryDirectory() as d:
            d = str(Path(d).resolve())     # /var -> /private/var on macOS: relpath must start from the real cwd
            (Path(d) / "data").mkdir()
            rel_out = os.path.relpath(S1 / "runs" / "compaction-rel-test", d)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                rc, out, _ = self.run_main(["--data", "data", "--out", rel_out, "--dry-run"], allowed=True)
            finally:
                os.chdir(cwd)
            plan = json.loads(out)
            cmd = plan["command"]
            data_arg = cmd[cmd.index("--data") + 1]
            self.assertEqual(data_arg, str((Path(d) / "data").resolve()))
            self.assertTrue(Path(cmd[cmd.index("--adapter-path") + 1]).is_absolute())
            self.assertIsNone(plan["out_problem"])
            self.assertEqual(plan["missing_data"], ["train.jsonl", "valid.jsonl"])

    def test_config_has_reasons(self):
        cfg = json.loads(ct.CONFIG.read_text())
        numeric = [k for k, v in cfg.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        self.assertGreaterEqual(len(numeric), 7)
        for k in numeric:
            self.assertTrue(cfg["_reasons"].get(k, "").strip(), f"no reason for {k}")


class FakeChild:
    def __init__(self, on_stop=None):
        self.pid, self.returncode, self.stopped, self.on_stop = 999999, None, False, on_stop

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class DeadlineTests(unittest.TestCase):
    """The night-window deadline bounds the gate wait and the training (one supervised process group)."""
    deadline = datetime(2026, 11, 8, 7, 0)

    def clock(self, times):
        it = iter(times)
        last = [times[-1]]
        return lambda: next(it, last[0])

    def run_sup(self, child, times, out):
        calls = []

        def stop(c, *, grace):
            calls.append(grace)
            c.returncode = -15
            if c.on_stop:
                c.on_stop()
        err = StringIO()
        with redirect_stderr(err):
            rc = ct.supervise(["gate"], self.deadline, out, now_fn=self.clock(times), popen=lambda *a, **k: child,
                              stop=stop, sleep=lambda s: None, grace=3.0)
        return rc, calls, err.getvalue()

    def test_expiry_while_waiting_for_gate(self):
        with tempfile.TemporaryDirectory() as d:
            child = FakeChild()     # the gate never got the lock: no adapter dir yet
            rc, calls, err = self.run_sup(child, [datetime(2026, 11, 8, 6, 59), datetime(2026, 11, 8, 6, 59, 30),
                                                  datetime(2026, 11, 8, 7, 0, 1)], Path(d))
            self.assertEqual(rc, ct.DEADLINE_RC)
            self.assertEqual(calls, [3.0])
            self.assertIn("checkpoints kept: none", err)

    def test_expiry_during_training_keeps_checkpoints(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "adapter").mkdir()
            (out / "adapter" / "0000100_adapters.safetensors").write_bytes(b"x")
            rc, calls, err = self.run_sup(FakeChild(), [datetime(2026, 11, 8, 6, 30)] * 3 +
                                          [datetime(2026, 11, 8, 7, 0)], out)
            self.assertEqual(rc, ct.DEADLINE_RC)
            self.assertTrue((out / "adapter" / "0000100_adapters.safetensors").exists())
            self.assertIn("0000100_adapters.safetensors", err)

    def test_deadline_already_passed_starts_nothing(self):
        started = []
        with redirect_stderr(StringIO()):
            rc = ct.supervise(["gate"], self.deadline, Path("/nonexistent"), now_fn=lambda: datetime(2026, 11, 8, 7, 5),
                              popen=lambda *a, **k: started.append(1))
        self.assertEqual(rc, ct.REFUSED)
        self.assertEqual(started, [])

    def test_finishes_before_deadline(self):
        child = FakeChild()
        child.returncode = 0
        rc, calls, _ = self.run_sup(child, [datetime(2026, 11, 8, 3, 0)], Path("/nonexistent"))
        self.assertEqual((rc, calls), (0, []))

    def test_real_group_is_stopped_and_reaped(self):
        # Real process group, real overnight.stop_group_drained: a sleeping grandchild must not survive the deadline.
        import time
        t0 = time.monotonic()
        base = datetime(2026, 11, 8, 6, 59)
        from datetime import timedelta
        now_fn = lambda: base + timedelta(seconds=(time.monotonic() - t0) * 120)   # 1 s real = 2 min fake
        cmd = [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
               "'import time; time.sleep(60)']); time.sleep(60)"]
        with tempfile.TemporaryDirectory() as d, redirect_stderr(StringIO()):
            rc = ct.supervise(cmd, self.deadline, Path(d), now_fn=now_fn, poll_s=0.05, grace=5.0)
        self.assertEqual(rc, ct.DEADLINE_RC)
        self.assertLess(time.monotonic() - t0, 10)


if __name__ == "__main__":
    unittest.main()
