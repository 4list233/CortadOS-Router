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
        self.assertEqual(rec["withheld_reason"], ct.EXPORT_LOGS_WITHHELD)
        self.assertIsNone(rec["prompt"])
        self.assertEqual(rec["events"], [])
        self.assertNotIn(TOKEN, json.dumps(rec))

    # --- the prompt is redacted by the runner; a late export never redacts raw material with other credentials -------
    def _rotate(self):
        with open(self.cfg["token_file"], "w") as f:
            f.write("a-rotated-token-0005")
        self.cfg["token"] = "a-rotated-token-0005"

    def _forget_export(self, d, res, **drop):
        """As if the runner died between the final result.json and its export."""
        for p in (self.export_file(), self.export_file() + ".ids"):
            if os.path.exists(p):
                os.unlink(p)
        res.pop("export", None)
        for k in drop:
            res.pop(k, None)
        ct._write_json(os.path.join(d, "result.json"), res)

    def test_runner_redacts_the_prompt_in_place(self):
        tid, d, res = self.run_export_task(task=f"use {TOKEN} please")
        self.assertIs(res["prompt_redacted"], True)
        self.assertIs(res["logs_redacted"], True)
        p = os.path.join(d, "prompt.txt")
        with open(p) as f:
            self.assertEqual(f.read(), "use *** please")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        [rec] = self.records()
        self.assertEqual(rec["prompt"], "use *** please")
        self.assertNotIn("withheld_reason", rec)

    def test_rotated_token_and_a_crash_after_the_final_result_never_export_the_old_token(self):
        """#91 review P1: the runner died after result.json, the credentials rotated, then status exported late."""
        tid, d, res = self.run_export_task(task=f"secret {TOKEN}")
        self._forget_export(d, res)
        self._rotate()
        _, st = self.call("task_status", task_id=tid)
        self.assertTrue(st["export"]["ok"], st["export"])
        with open(self.export_file()) as f:
            raw = f.read()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("000000001", raw)
        [rec] = self.records()
        self.assertEqual(rec["prompt"], "secret ***")            # redacted on disk by the runner, with the launch token
        self.assertEqual(rec["logs"], "included")

    def test_an_unmarked_prompt_is_withheld_when_the_credentials_rotated(self):
        """A prompt and logs the runner never marked redacted (its prompt pass failed, or an older runner): with
        rotated credentials the late export (status, then prune) withholds them instead of redacting with the new
        token; the counts stay."""
        tid, d, res = self.run_export_task(task="x")
        with open(os.path.join(d, "prompt.txt"), "w") as f:
            f.write(f"raw {TOKEN}")
        self._forget_export(d, res, prompt_redacted=True, logs_redacted=True)
        self._rotate()
        _, st = self.call("task_status", task_id=tid)
        self.assertTrue(st["export"]["ok"], st["export"])
        self.assertTrue(st["export"]["logs_withheld"])
        self.assertTrue(st["export"]["prompt_withheld"])
        with open(self.export_file()) as f:
            self.assertNotIn(TOKEN, f.read())
        [rec] = self.records()
        self.assertIsNone(rec["prompt"])
        self.assertEqual(rec["events"], [])
        self.assertEqual(rec["withheld_reason"], ct.EXPORT_LOGS_WITHHELD)
        self.assertEqual(rec["event_counts"].get("tool_use"), 1)    # counts kept
        self.assertEqual(rec["tool_uses"], {"Bash": 1})
        # the same through prune's retry
        self._forget_export(d, _load(os.path.join(d, "result.json")))
        self.assertEqual(ct.prune(ct._private_root(), now=time.time() + ct.PRUNE_AGE_S + 60), [tid])
        [rec] = self.records()
        self.assertIsNone(rec["prompt"])
        self.assertNotIn(TOKEN, json.dumps(rec))

    def test_an_unmarked_prompt_is_redacted_when_the_launch_credentials_match(self):
        tid, d, res = self.run_export_task(task="x")
        with open(os.path.join(d, "prompt.txt"), "w") as f:
            f.write(f"raw {TOKEN}")
        self._forget_export(d, res, prompt_redacted=True, logs_redacted=True)
        _, st = self.call("task_status", task_id=tid)              # same credentials as at launch
        self.assertTrue(st["export"]["ok"], st["export"])
        [rec] = self.records()
        self.assertEqual(rec["prompt"], "raw ***")
        self.assertEqual(rec["logs"], "included")
        self.assertNotIn(TOKEN, json.dumps(rec))

    # --- export file permissions -----------------------------------------------------------------------------------
    def test_loose_export_files_and_dir_are_tightened_and_recorded(self):
        path = self.export_file()
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o755)
        for p in (path, path + ".lock", path + ".ids"):
            with open(p, "w"):
                pass
            os.chmod(p, 0o644)
        tid, d, res = self.run_export_task()
        self.assertTrue(res["export"]["ok"], res["export"])
        self.assertEqual(sorted(res["export"]["tightened"]), ["dir", "file", "ids", "lock"])
        for p in (path, path + ".lock", path + ".ids"):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        ev = [e for e in _events(all_events=True) if e.get("task_id") == tid and e["event"] == "task_exported"]
        self.assertEqual(sorted(ev[0]["tightened"]), ["dir", "file", "ids", "lock"])

    def test_an_export_file_that_cannot_be_tightened_is_refused(self):
        path = self.export_file()
        os.makedirs(os.path.dirname(path), mode=0o700)
        with open(path, "w"):
            pass
        os.chmod(path, 0o644)
        with mock.patch.object(ct.os, "fchmod", side_effect=PermissionError("no")):
            with self.assertRaises(PermissionError):
                ct.append_export(b'{"task_id":"t20200101000000-0000000d"}\n', "t20200101000000-0000000d")
        self.assertEqual(os.path.getsize(path), 0)

    def test_a_symlinked_export_file_is_refused(self):
        path = self.export_file()
        os.makedirs(os.path.dirname(path), mode=0o700)
        target = os.path.join(self.tmp, "elsewhere.jsonl")
        with open(target, "w"):
            pass
        os.symlink(target, path)
        with self.assertRaises(OSError):
            ct.append_export(b'{"task_id":"t20200101000000-0000000e"}\n', "t20200101000000-0000000e")
        self.assertEqual(os.path.getsize(target), 0)

    def test_an_override_in_a_group_or_world_writable_dir_is_refused(self):
        shared = os.path.join(self.tmp, "shared")
        os.mkdir(shared)
        os.chmod(shared, 0o777)
        os.environ[ct.EXPORT_ENV] = os.path.join(shared, "runs.jsonl")
        with self.assertRaises(PermissionError):
            ct.append_export(b'{"task_id":"t20200101000000-0000000f"}\n', "t20200101000000-0000000f")
        self.assertEqual(os.listdir(shared), [])
        os.chmod(shared, 0o755)                                   # readable by others is the owner's choice
        kind, already, _ = ct.append_export(b'{"task_id":"t20200101000000-0000000f"}\n', "t20200101000000-0000000f")
        self.assertEqual((kind, already), ("env", False))

    # --- prune's export retries share one time budget ----------------------------------------------------------------
    def test_a_stuck_export_lock_does_not_hold_start_task(self):
        root = ct._private_root()
        tids = [f"t20200101000000-0000020{i}" for i in range(3)]
        for t in tids:
            os.mkdir(os.path.join(root, t), 0o700)
            ct._write_json(os.path.join(root, t, "meta.json"), {"task_id": t, "started_at": 1.0, "runner_pid": 0})
            ct._write_json(os.path.join(root, t, "result.json"), {"exit_code": 0, "ended_at": 2.0})
        path = self.export_file()
        os.makedirs(os.path.dirname(path), mode=0o700)
        held = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, held)
        import fcntl
        fcntl.flock(held, fcntl.LOCK_EX)
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        with mock.patch.object(ct, "PRUNE_EXPORT_BUDGET_S", 1.0):
            t0 = time.monotonic()
            r, out = self.call("start_task", task="hello", cwd=self.work)
            took = time.monotonic() - t0
        self.assertFalse(r.get("isError"), r)
        self.assertLess(took, 8, took)                          # unbounded: 3 x EXPORT_LOCK_WAIT_S = 90 s
        errs = [e for e in out.get("prune_errors", []) if "not pruned" in e]
        self.assertEqual(len(errs), 3, out.get("prune_errors"))
        self.assertEqual(sum("TimeoutError" in e for e in errs), 1, errs)
        self.assertEqual(sum("budget" in e for e in errs), 2, errs)
        for t in tids:
            self.assertTrue(os.path.isdir(os.path.join(root, t)))   # kept for the next start
        fcntl.flock(held, fcntl.LOCK_UN)
        self.call("wait_task", task_id=out["task_id"], timeout_s=30)
        _wait_export(ct.task_path(out["task_id"]))

    # --- best-effort scrub of well-known secret shapes ----------------------------------------------------------------
    SCRUB_CASES = [
        ("authorization", 'curl -H "Authorization: token abcdef0123456789" https://x', "Authorization: [scrubbed:authorization]"),
        ("authorization", "Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==", "Proxy-Authorization: [scrubbed:authorization]"),
        ("bearer", "use Bearer abcdefgh12345678.xyz now", "use Bearer [scrubbed:bearer] now"),
        ("anthropic_key", "key sk-ant-api03-AbCdEfGh123456_-x end", "key [scrubbed:anthropic_key] end"),
        ("openai_key", "key sk-proj-AbCdEfGh1234567890 end", "key [scrubbed:openai_key] end"),
        ("github_token", "ghp_" + "a1" * 18 + " gho_" + "b2" * 18 + " ghs_" + "c3" * 18,
         "[scrubbed:github_token] [scrubbed:github_token] [scrubbed:github_token]"),
        ("github_token", "github_pat_11ABCDEFG0123456789_abcdefghij", "[scrubbed:github_token]"),
        ("slack_token", "xoxb-123456789012-abcdef and xoxp-1-2-3-abcdefgh", "[scrubbed:slack_token] and [scrubbed:slack_token]"),
        ("aws_access_key", "id AKIAIOSFODNN7EXAMPLE.", "id [scrubbed:aws_access_key]."),
        ("private_key", "a\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz\naGtleQ==\n-----END OPENSSH PRIVATE KEY-----\nz",
         "a\n[scrubbed:private_key]\nz"),
        ("jwt", "t=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl;", "t=[scrubbed:jwt];"),
    ]

    def test_each_scrub_pattern(self):
        for kind, text, want in self.SCRUB_CASES:
            with self.subTest(kind=kind, text=text):
                cap = ct._Cap()
                got = cap.scrub(text)
                self.assertIn(want, got)
                self.assertIn(kind, cap.scrubbed)
        cap = ct._Cap()
        prose = "a task-runner, sk-learn-style docs, Authorization: ***, Bearer ***, the bearer of news"
        self.assertEqual(cap.scrub(prose), prose)                 # already masked or not a token shape: untouched
        self.assertEqual(cap.scrubbed, {})
        self.assertEqual(cap.tree({"headers": {"Authorization": "opaque0123456789", "Accept": "x"}}),
                         {"headers": {"Authorization": "[scrubbed:authorization]", "Accept": "x"}})

    def _record_for(self, prompt, tokens=(TOKEN,)):
        tid, d = self._manual_task("ok")
        with open(os.path.join(d, "prompt.txt"), "w") as f:
            f.write(prompt)
        res = {"exit_code": 0, "ended_at": time.time(), "prompt_redacted": True, "logs_redacted": True}
        return ct.build_export_record(d, {}, res, ct.Redactor(tokens), None)

    def test_scrub_applies_to_export_text_before_the_cap_and_is_counted(self):
        key = "sk-ant-api03-" + "Z9" * 20
        with mock.patch.object(ct, "EXPORT_TEXT_CHARS", 30):
            rec = self._record_for("x" * 19 + " " + key)         # cut first, a key prefix would stay
        self.assertTrue(rec["prompt"].startswith("x" * 19 + " [scrubbed"), rec["prompt"])
        self.assertNotIn("Z9Z9", rec["prompt"])
        self.assertEqual(rec["scrubbed"], {"anthropic_key": 1})
        self.assertEqual(rec["truncated"], 1)

    def test_scrub_never_runs_before_the_known_value_redaction(self):
        known = "sk-ant-api03-KNOWNknown0123456789"
        rec = self._record_for(f"a {known} b", tokens=(known,))
        self.assertEqual(rec["prompt"], "a *** b")               # the known credential's mask, not a scrub marker
        self.assertEqual(rec["scrubbed"], {})

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
