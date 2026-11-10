"""Task run export (#91): the finalized, redacted, append-only record of each child run, for Grounds to capture.
Fakes only (the fake claude of tests/test_child_tasks.py, a throwaway CORTADOS_HOME and tasks dir): no real child, no
provider, never ~/.cortados."""
import json
import os
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

from cortados_router import child_tasks as ct
from tests.test_child_tasks import ChildTaskBase, _events, _load, _wait_file

TOKEN = "tok-test-000000001"            # the fixture's provider token: the child's ANTHROPIC_AUTH_TOKEN


def _wait_export(d, timeout=30):
    """result.json once the runner has recorded its export (it is written right after the final result)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            res = _load(os.path.join(d, "result.json"))
            if "export" in res:
                return res
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("the runner recorded no export")


class TaskExportTests(ChildTaskBase):
    def export_file(self):
        return os.path.join(os.environ["CORTADOS_HOME"], "exports", "task-runs.jsonl")

    def lines(self, path=None):
        with open(path or self.export_file(), "rb") as f:
            return f.read().split(b"\n")[:-1]

    def records(self, path=None):
        return [json.loads(x) for x in self.lines(path)]

    def _manual_task(self, mode):
        """A task dir prepared by hand, for driving ct._run in-process (as ChildTaskTests._manual_task)."""
        tid = ct._new_id()
        d = os.path.join(ct._private_root(), tid)
        os.mkdir(d, 0o700)
        with open(os.path.join(d, "prompt.txt"), "w") as f:
            f.write("p")
        ct._write_json(os.path.join(d, "cmd.json"), {"argv": [os.environ["CORTADOS_CHILD_CLAUDE_BIN"]], "cwd": self.work,
                                                    "max_s": 60, "grace_s": 1, "event": {"task_id": tid},
                                                    "started_at": time.time()})
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": time.time(), "runner_pid": None})
        os.environ["FAKE_CLAUDE_MODE"] = mode
        return tid, d

    def run_export_task(self, **kw):
        os.environ["FAKE_CLAUDE_MODE"] = "stream-export"
        tid = self.start(**kw)
        self.call("wait_task", task_id=tid, timeout_s=30)
        d = ct.task_path(tid)
        return tid, d, _wait_export(d)

    # --- written only after redaction, from the redacted run --------------------------------------------------------
    def test_export_is_redacted_complete_and_written_after_the_final_result(self):
        tid, d, res = self.run_export_task(task="export me", title="Export test")
        self.assertEqual(res["export"]["ok"], True, res["export"])
        self.assertEqual(res["export"]["path_kind"], "default")
        path = self.export_file()
        with open(path, "rb") as f:
            raw = f.read()
        self.assertNotIn(TOKEN.encode(), raw)                     # the planted token, in no spelling
        self.assertNotIn(json.dumps(TOKEN)[1:-1].encode(), raw)
        self.assertNotIn(b"000000001", raw)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        [rec] = self.records()
        self.assertEqual(rec["schema"], "cortados.task-run/1")
        self.assertEqual(rec["task_id"], tid)
        self.assertEqual(rec["state"], "succeeded")
        self.assertEqual(rec["title"], "Export test")
        self.assertEqual(rec["prompt"], "export me")
        self.assertEqual(rec["logs"], "included")
        self.assertEqual(rec["orchestrator"], "cortados")
        self.assertEqual(rec["host"], "here")
        self.assertIn("router_run", rec)                          # the runner's fetch (a field, also when it fails)
        self.assertEqual(rec["tool_uses"], {"Bash": 1})
        kinds = [e["type"] for e in rec["events"]]
        self.assertEqual(kinds, ["text", "tool_use", "tool_result", "withheld", "text", "result"])
        self.assertEqual(rec["event_counts"], {"text": 2, "tool_use": 1, "tool_result": 1, "withheld": 1, "result": 1})
        ev = rec["events"]
        self.assertEqual(ev[0]["text"], "planning with ***")
        self.assertEqual(ev[1]["name"], "Bash")
        self.assertEqual(ev[1]["input"], {"command": "echo ***"})
        self.assertEqual(ev[2], {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False,
                                 "content": "out ***"})
        self.assertEqual(ev[5]["text"], "done ***")
        # the unparseable line was withheld by the runner and carried over as its count
        self.assertEqual(res["redact_unparsed_lines"], 1)
        self.assertEqual(rec["redact_unparsed_lines"], 1)
        self.assertEqual(rec["withheld"], 1)
        # the 40000-character text is cut at the field cap, with a marker, and counted
        self.assertEqual(rec["truncated"], 1)
        self.assertTrue(ev[4]["text"].startswith("L" * ct.EXPORT_TEXT_CHARS))
        self.assertIn("[truncated by cortados: 8000 of 40000 characters cut]", ev[4]["text"])
        # after the final result: the export line is newer than result.json's end, and the lifecycle log has it after
        # `finished`
        self.assertGreaterEqual(rec["exported_at"], res["ended_at"])
        names = [e["event"] for e in _events(all_events=True) if e.get("task_id") == tid]
        self.assertLess(names.index("finished"), names.index("task_exported"))
        _, st = self.call("task_status", task_id=tid)
        self.assertTrue(st["export"]["ok"])

    def test_runner_exports_only_after_redaction(self):
        """In-process runner: when the export is built, stdout.log on disk is already redacted and result.json final."""
        tid, d = self._manual_task("stream-export")
        seen = {}
        real = ct._export_task

        def spy(d_, meta, res, red, router_run):
            with open(os.path.join(d_, "stdout.log")) as f:
                seen["stdout"] = f.read()
            seen["result"] = _load(os.path.join(d_, "result.json"))
            return real(d_, meta, res, red, router_run)
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with mock.patch.object(ct, "_export_task", spy), \
                mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "runner-token-000001"}):
            ct._run(d)
        self.assertNotIn("runner-token-000001", seen["stdout"])
        self.assertIn("ended_at", seen["result"])               # final, before the export
        self.assertNotIn("export", seen["result"])
        res = _load(os.path.join(d, "result.json"))
        self.assertTrue(res["export"]["ok"])
        with open(self.export_file()) as f:
            self.assertNotIn("runner-token-000001", f.read())

    # --- parent marker ---------------------------------------------------------------------------------------------
    def test_missing_parent_marker(self):
        tid, d, _ = self.run_export_task()
        [rec] = self.records()
        self.assertIsNone(rec["parent_task_id"])
        self.assertEqual(rec["parent"], "missing")

    def test_parent_from_the_argument_or_the_environment(self):
        parent = "t20261110000000-0123abcd"
        self.run_export_task(parent_task_id=parent)
        os.environ[ct.PARENT_ENV] = "t20261110000001-0123abce"
        self.run_export_task()
        os.environ[ct.PARENT_ENV] = "not-a-task-id"
        self.run_export_task()
        a, b, c = self.records()
        self.assertEqual((a["parent_task_id"], a["parent"]), (parent, "known"))
        self.assertEqual((b["parent_task_id"], b["parent"]), ("t20261110000001-0123abce", "known"))
        self.assertEqual((c["parent_task_id"], c["parent"]), (None, "missing"))
        self.assertIn(ct.PARENT_ENV, c["parent_note"])

    def test_invalid_parent_argument_is_refused(self):
        r, _ = self.call("start_task", task="x", cwd=self.work, parent_task_id="../../etc")
        self.assertTrue(r.get("isError"))
        self.assertIn("parent_task_id", r["content"][0]["text"])

    # --- append-only, written once ------------------------------------------------------------------------------------
    def test_append_only_and_written_once(self):
        path = self.export_file()
        os.makedirs(os.path.dirname(path), mode=0o700)
        with open(path, "wb") as f:
            f.write(b'{"schema":"someone-else","task_id":"t20200101000000-00000001"}\n')
        tid, d, res = self.run_export_task()
        first = self.lines()
        self.assertEqual(first[0], b'{"schema":"someone-else","task_id":"t20200101000000-00000001"}')
        self.assertEqual(len(first), 2)
        meta = _load(os.path.join(d, "meta.json"))
        red = ct.Redactor([TOKEN])
        # a second finish (e.g. a retry after a lost result.json write): recognised by id, nothing appended
        again = ct._export_task(d, meta, res, red, None)
        self.assertEqual(again["ok"], True)
        self.assertTrue(again["already"])
        # the sidecar index lost: the file itself is read, still no duplicate
        os.unlink(path + ".ids")
        self.assertTrue(ct._export_task(d, meta, res, red, None)["already"])
        # a writer that died between its line and its id: the file's tail is read
        with open(path + ".ids", "w") as f:
            f.write(f"t20200101000000-00000001 {len(first[0]) + 1}\n")
        self.assertTrue(ct._export_task(d, meta, res, red, None)["already"])
        self.assertEqual(self.lines(), first)                     # the existing lines untouched, none added
        # status and prune of an exported task export nothing again
        self.call("task_status", task_id=tid)
        ct.prune(ct._private_root(), now=time.time() + ct.PRUNE_AGE_S + 60)
        self.assertEqual(self.lines(), first)
        self.assertFalse(os.path.exists(d))                       # exported: pruned as usual

    def test_a_torn_last_line_does_not_swallow_the_next_record(self):
        path = self.export_file()
        os.makedirs(os.path.dirname(path), mode=0o700)
        with open(path, "wb") as f:
            f.write(b'{"task_id":"t2020')                          # a writer died mid-line
        ct.append_export(b'{"task_id":"t20200101000000-0000000c"}\n', "t20200101000000-0000000c")
        self.assertEqual(self.lines(), [b'{"task_id":"t2020', b'{"task_id":"t20200101000000-0000000c"}'])

    def test_concurrent_writers_never_interleave(self):
        path = os.path.join(self.tmp, "exp", "runs.jsonl")
        code = ("import sys; from cortados_router import child_tasks as ct\n"
                "tid = sys.argv[1]\n"
                "line = ('{\"task_id\":\"%s\",\"pad\":\"%s\"}\\n' % (tid, tid[-1] * 400000)).encode()\n"
                "ct.append_export(line, tid)\n")
        tids = [f"t20200101000000-000000{i:02x}" for i in range(8)]
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code, t], cwd=str(ct.REPO_ROOT),
                                  env={**os.environ, ct.EXPORT_ENV: path}) for t in tids]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        recs = self.records(path)
        self.assertEqual(sorted(r["task_id"] for r in recs), tids)   # each once, each whole
        for r in recs:
            self.assertEqual(r["pad"], r["task_id"][-1] * 400000)

    # --- a failed export is recorded, and prune keeps the task until it succeeds ---------------------------------------
    def test_failed_export_is_recorded_and_prune_keeps_then_exports(self):
        blocker = os.path.join(self.tmp, "not-a-dir")
        with open(blocker, "w") as f:
            f.write("x")
        os.environ[ct.EXPORT_ENV] = os.path.join(blocker, "runs.jsonl")   # its parent is a file: the export fails
        tid, d, res = self.run_export_task()
        self.assertEqual(res["export"]["ok"], False)
        self.assertIn(res["export"]["error"], ("NotADirectoryError", "FileExistsError"))
        _, st = self.call("task_status", task_id=tid)
        self.assertFalse(st["export"]["ok"])
        self.assertIn("task_export_failed", [e["event"] for e in _events(all_events=True) if e.get("task_id") == tid])
        later = time.time() + ct.PRUNE_AGE_S + 60
        errors = []
        self.assertEqual(ct.prune(ct._private_root(), now=later, errors=errors), [])
        self.assertTrue(os.path.isdir(d))                         # kept ...
        self.assertTrue(any(tid in e and "not pruned" in e for e in errors), errors)   # ... and reported
        self.assertIn("task_kept_unexported", [e["event"] for e in _events(all_events=True) if e.get("task_id") == tid])
        del os.environ[ct.EXPORT_ENV]                             # fixed: the next prune exports, then removes it
        errors = []
        self.assertEqual(ct.prune(ct._private_root(), now=later, errors=errors), [tid])
        self.assertEqual(errors, [])
        [rec] = self.records()
        self.assertEqual(rec["task_id"], tid)
        self.assertNotIn(TOKEN, json.dumps(rec))
        self.assertEqual(rec["router_run"], ct.EXPORT_LATE_ROUTER_RUN)

    def test_prune_retries_a_bounded_number_of_exports_per_call(self):
        root = ct._private_root()
        tids = [f"t20200101000000-0000010{i}" for i in range(3)]
        for t in tids:
            os.mkdir(os.path.join(root, t), 0o700)
            ct._write_json(os.path.join(root, t, "meta.json"), {"task_id": t, "started_at": 1.0, "runner_pid": 0})
            ct._write_json(os.path.join(root, t, "result.json"), {"exit_code": 0, "ended_at": 2.0})
        errors = []
        with mock.patch.object(ct, "PRUNE_EXPORT_RETRIES", 2):
            removed = ct.prune(root, errors=errors)
        self.assertEqual(len(removed), 2)                         # exported (pre-#91 records), then removed
        self.assertEqual(len(errors), 1)
        self.assertIn("export not retried in this prune", errors[0])
        self.assertEqual(sorted(r["task_id"] for r in self.records()), sorted(removed))
        self.assertEqual(ct.prune(root), [next(t for t in tids if t not in removed)])   # the next prune takes it

    # --- recovery of a dead runner -----------------------------------------------------------------------------------
    def _kill_runner(self, tid):
        pid = _load(os.path.join(ct.task_path(tid), "meta.json"))["runner_pid"]
        os.kill(pid, signal.SIGKILL)
        ct._REAP[tid].wait()

    def test_recovered_task_is_exported_once_its_logs_are_released(self):
        os.environ["FAKE_CLAUDE_MODE"] = "echo-sleep"
        tid = self.start()
        self.assertTrue(_wait_file(os.path.join(self.work, "echoed")))
        self._kill_runner(tid)
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        self.assertTrue(st["export"]["ok"], st["export"])
        [rec] = self.records()
        self.assertEqual((rec["task_id"], rec["state"], rec["logs"]), (tid, "failed", "included"))
        self.assertEqual(rec["prompt"], "hello")
        self.assertNotIn(TOKEN, json.dumps(rec))
        self.call("task_status", task_id=tid)                     # a second recovery look appends nothing
        self.assertEqual(len(self.lines()), 1)

    def test_a_runner_that_died_after_its_result_is_exported_by_status(self):
        tid, d, res = self.run_export_task()
        os.unlink(self.export_file())                             # as if the export never happened
        os.unlink(self.export_file() + ".ids")
        del res["export"]
        ct._write_json(os.path.join(d, "result.json"), res)
        _, st = self.call("task_status", task_id=tid)
        self.assertTrue(st["export"]["ok"], st["export"])
        [rec] = self.records()
        self.assertEqual((rec["task_id"], rec["logs"]), (tid, "included"))
        self.assertNotIn(TOKEN, json.dumps(rec))
        self.call("task_status", task_id=tid)
        self.assertEqual(len(self.lines()), 1)

    def test_recovered_task_with_rotated_credentials_is_exported_logs_withheld_before_prune(self):
        os.environ["FAKE_CLAUDE_MODE"] = "echo-sleep"
        tid = self.start()
        self.assertTrue(_wait_file(os.path.join(self.work, "echoed")))
        self._kill_runner(tid)
        with open(self.cfg["token_file"], "w") as f:
            f.write("a-rotated-token-0004")
        self.cfg["token"] = "a-rotated-token-0004"
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["export"]["error"], ct.EXPORT_PENDING)   # not exported while the logs are unredacted
        self.assertFalse(os.path.exists(self.export_file()))
        later = time.time() + ct.PRUNE_AGE_S + 60
        self.assertEqual(ct.prune(ct._private_root(), now=later), [tid])
        [rec] = self.records()
        self.assertEqual(rec["logs"], ct.EXPORT_LOGS_WITHHELD)
        self.assertIsNone(rec["prompt"])
        self.assertEqual(rec["events"], [])
        self.assertNotIn(TOKEN, json.dumps(rec))

    # --- pieces ------------------------------------------------------------------------------------------------------
    def test_cap_marks_and_counts_every_cut(self):
        cap = ct._Cap(5)
        self.assertEqual(cap.text("12345"), "12345")
        self.assertEqual(cap.tree({"a": ["1234567", 3]}),
                         {"a": ["12345\n[truncated by cortados: 2 of 7 characters cut]", 3]})
        self.assertEqual(cap.truncated, 1)

    def test_export_path_default_and_override(self):
        self.assertEqual(ct.export_path(), (self.export_file(), "default"))
        os.environ[ct.EXPORT_ENV] = os.path.join(self.tmp, "x.jsonl")
        self.assertEqual(ct.export_path(), (os.path.join(self.tmp, "x.jsonl"), "env"))

    def test_a_record_that_still_matches_a_credential_is_never_written(self):
        tid, d = self._manual_task("ok")
        ct._write_json(os.path.join(d, "result.json"), {"exit_code": 0, "ended_at": time.time()})
        red = ct.Redactor([TOKEN])
        with mock.patch.object(ct, "build_export_record", return_value={"task_id": tid, "events": [], "x": TOKEN,
                                                                        "logs": "included"}):
            out = ct._export_task(d, {}, {}, red, None)
        self.assertEqual(out, {"ok": False, "at": out["at"], "error": "_ExportLeak"})
        self.assertFalse(os.path.exists(self.export_file()))


if __name__ == "__main__":
    unittest.main()
