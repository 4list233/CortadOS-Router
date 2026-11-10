"""`child_tasks.py --watch` (issue #87): the orchestrator's background watcher, and start_task's `watch_command`.
Same fakes as tests/test_child_tasks.py (fake claude, throwaway dirs, a subprocess transport in place of ssh): no real
child, no ssh, no provider."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import child_tasks as ct
from tests.test_child_tasks import ChildTaskBase, _wait_file


class WatchTests(ChildTaskBase):
    def watch(self, *ids, **kw):
        """Run watch() on the named tasks with fast polling; (exit code, parsed JSON rows, raw text)."""
        buf = io.StringIO()
        kw.setdefault("interval_s", 0.1)
        rc = ct.watch(list(ids), out=buf, **kw)
        text = buf.getvalue()
        rows = [json.loads(line) for line in text.splitlines() if line]
        return rc, rows, text

    def start_sleeping(self, **kw):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start(**kw)
        self.assertTrue(_wait_file(os.path.join(kw.get("cwd", self.work), "fake-argv.json")))
        return tid

    # ---- outcomes
    def test_success_exits_0_with_one_metadata_line_and_no_result_text(self):
        tid = self.start(task="build the secret widget")
        rc, rows, text = self.watch(tid)
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["task_id"], row["state"], row["host"], row["exit_code"]), (tid, "succeeded", "here", 0))
        self.assertIsInstance(row["elapsed_s"], (int, float))
        self.assertIsInstance(row["watched_s"], (int, float))
        # metadata only: the model's result text ("done: <prompt>"), the prompt and the task dir paths stay out
        self.assertNotIn("secret widget", text)
        for k in ("result", "stderr_tail", "dir", "stdout_log", "cwd"):
            self.assertNotIn(k, row)
        self.assertEqual(text.count("\n"), 1)                   # one compact line, no indentation

    def test_failure_exits_1_and_keeps_the_stderr_out_of_the_line(self):
        os.environ["FAKE_CLAUDE_MODE"] = "fail"                 # exits 3, "boom: something broke" on stderr
        tid = self.start()
        rc, rows, text = self.watch(tid)
        self.assertEqual(rc, 1, text)
        self.assertEqual((rows[0]["state"], rows[0]["exit_code"]), ("failed", 3))
        self.assertNotIn("boom", text)

    def test_cancelled_exits_1(self):
        tid = self.start_sleeping()
        _, c = self.call("cancel_task", task_id=tid)
        self.assertTrue(c["cancelled"], c)
        rc, rows, text = self.watch(tid)
        self.assertEqual(rc, 1, text)
        self.assertEqual(rows[0]["state"], "cancelled")

    def test_a_running_task_that_shows_no_progress_is_stalled(self):
        tid = self.start_sleeping()                             # the fake writes nothing to its stream after starting
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "running")
        self.assertIsInstance(st["last_output_at"], float)      # the progress signal task_status now reports
        t0 = time.monotonic()
        rc, rows, text = self.watch(tid, stall_s=0.6)
        self.assertEqual(rc, 1, text)
        row = rows[0]
        self.assertEqual((row["state"], row["last_state"], row["host"]), ("stalled", "running", "here"))
        self.assertGreaterEqual(row["stalled_after_s"], 0.6)
        self.assertIn("still running on its host", row["error"])
        self.assertLess(time.monotonic() - t0, 10)
        _, st = self.call("task_status", task_id=tid)           # a report, not a cancel
        self.assertEqual(st["state"], "running")

    def test_progress_resets_the_stall_window(self):
        tid = self.start_sleeping()
        d = ct.task_path(tid)
        stdout = os.path.join(d, "stdout.log")
        self.assertTrue(_wait_file(stdout))
        ticks = {"n": 0}
        real_sleep = time.sleep

        def touching_sleep(s):
            ticks["n"] += 1
            if ticks["n"] <= 6:                                 # keep the child "writing" for the first six polls
                os.utime(stdout, None)
            real_sleep(s)
        buf = io.StringIO()
        t0 = time.monotonic()
        rc = ct.watch([tid], interval_s=0.1, stall_s=0.5, out=buf, sleep=touching_sleep)
        self.assertEqual(rc, 1)
        row = json.loads(buf.getvalue())
        self.assertEqual(row["state"], "stalled")
        self.assertGreater(time.monotonic() - t0, 1.0)           # six touches of 0.1 s pushed the 0.5 s window back

    def test_timeout_exits_3_and_lists_the_pending_tasks(self):
        tid = self.start_sleeping()
        t0 = time.monotonic()
        rc, rows, text = self.watch(tid, timeout_s=0.5)
        self.assertEqual(rc, 3, text)
        self.assertEqual((rows[0]["task_id"], rows[0]["state"], rows[0]["watch"]), (tid, "running", "timeout"))
        self.assertLess(time.monotonic() - t0, 5)

    def test_several_tasks_one_line_each_as_they_end_and_the_worst_exit(self):
        ok = self.start(task="fine")
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        bad_dir = os.path.join(self.tmp, "work2")
        os.mkdir(bad_dir)
        bad = self.start(task="breaks", cwd=bad_dir)
        rc, rows, text = self.watch(ok, bad, ok)                # a repeated id is watched once
        self.assertEqual(rc, 1, text)
        self.assertEqual(sorted(r["task_id"] for r in rows), sorted([ok, bad]))
        by = {r["task_id"]: r for r in rows}
        self.assertEqual((by[ok]["state"], by[bad]["state"]), ("succeeded", "failed"))

    # ---- the watch itself cannot run
    def test_an_unknown_or_malformed_id_is_refused_before_anything_is_watched(self):
        good = self.start()
        with self.assertRaises(ct.TaskError):
            self.watch(good, "t00000000000000-00000000")
        with self.assertRaises(ct.TaskError):
            self.watch("not-an-id")
        with self.assertRaises(ct.TaskError):
            self.watch()
        for bad in ({"interval_s": 0}, {"stall_s": -1}, {"timeout_s": float("nan")}):
            with self.assertRaises(ct.TaskError):
                self.watch(good, **bad)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ct.watch_main(["t00000000000000-00000000"]), 2)
            self.assertEqual(ct.watch_main([]), 2)
            self.assertEqual(ct.watch_main([good, "--interval"]), 2)
            self.assertEqual(ct.watch_main([good, "--interval", "soon"]), 2)
            self.assertEqual(ct.watch_main([good, "--bogus"]), 2)
        self.assertIn("no such task", err.getvalue())
        self.assertIn("usage:", err.getvalue())
        self.assertIn("needs a number of seconds", err.getvalue())

    # ---- remote tasks: the same status path, over the fake transport
    def test_a_remote_task_is_followed_on_its_host(self):
        self._air()
        r, out = self.call("start_task", task="hello")
        self.assertFalse(r.get("isError"), r)
        tid = out["task_id"]
        self.assertEqual(out["host"], "air")
        n = len(self.transport_calls)
        rc, rows, text = self.watch(tid)
        self.assertEqual(rc, 0, text)
        row = rows[0]
        self.assertEqual((row["task_id"], row["state"], row["host"]), (tid, "succeeded", "air"))
        self.assertEqual(row["workspace"]["state"], "archived")
        self.assertEqual(row["workspace"]["path"], os.path.join(self.air_root, "archive", tid))
        self.assertGreater(len(self.transport_calls), n)        # every poll went to the host
        self.assertNotIn("done: hello", text)

    def test_a_host_that_goes_away_mid_watch_is_a_stall_not_a_crash(self):
        self._air()
        r, out = self.call("start_task", task="hello")
        tid = out["task_id"]
        self.call("wait_task", task_id=tid, timeout_s=30)        # let the fake finish there
        reads = {"n": 0}
        good = ct.exec_hosts.TRANSPORT

        def flaky(argv, stdin, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                # first read works but must look non-terminal, else the watch simply ends: answer "running"
                return 0, json.dumps({"v": ct.exec_hosts.PROTOCOL, "ok": True,
                                      "result": {"task_id": tid, "state": "running", "elapsed_s": 1.0}}).encode(), b""
            return 255, b"", b"ssh: connect to host air port 22: No route to host"
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", flaky):
            rc, rows, text = self.watch(tid, stall_s=0.5)
        self.assertEqual(rc, 1, text)
        row = rows[0]
        self.assertEqual((row["state"], row["last_state"]), ("stalled", "running"))
        self.assertIn("unreadable", row["error"])
        self.assertIn("is unreachable", row["error"])
        self.assertGreater(reads["n"], 2)
        del good

    def test_a_host_unreachable_from_the_start_is_exit_2_after_three_attempts(self):
        self._air()
        r, out = self.call("start_task", task="hello")
        tid = out["task_id"]
        calls = []
        down = lambda argv, stdin, timeout: (calls.append(1), (255, b"", b"ssh: connect to host air port 22: No route to host"))[1]
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", down):
            t0 = time.monotonic()
            rc, rows, text = self.watch(tid)
        self.assertEqual(rc, 2, text)
        self.assertEqual(rows[0]["state"], "unwatchable")
        self.assertIn("3 attempts", rows[0]["error"])
        self.assertIn("No route to host", rows[0]["error"])
        self.assertEqual(len(calls), ct.WATCH_CONNECT_ATTEMPTS)
        self.assertLess(time.monotonic() - t0, 5)

    def test_unwatchable_outranks_a_failed_sibling(self):
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        local = self.start()
        self._air()
        _, out = self.call("start_task", task="hello")
        remote = out["task_id"]
        down = lambda argv, stdin, timeout: (255, b"", b"down")
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", down):
            rc, rows, text = self.watch(local, remote)
        self.assertEqual(rc, 2, text)
        self.assertEqual({r["task_id"]: r["state"] for r in rows}, {local: "failed", remote: "unwatchable"})

    # ---- review of 5456e94 (#87): exit precedence, unexpected errors, queued tasks, the deadline
    def test_an_unwatchable_task_outranks_the_timeout(self):
        local = self.start_sleeping()                           # still running when the timeout passes
        self._air()
        _, out = self.call("start_task", task="hello")
        remote = out["task_id"]
        down = lambda argv, stdin, timeout: (255, b"", b"down")
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", down):
            rc, rows, text = self.watch(local, remote, timeout_s=0.8)
        self.assertEqual(rc, 2, text)                            # was 3: the timeout branch forgot `cannot`
        by = {r["task_id"]: r for r in rows}
        self.assertEqual(by[remote]["state"], "unwatchable")
        self.assertEqual((by[local]["state"], by[local]["watch"]), ("running", "timeout"))

    def test_a_corrupt_meta_json_is_an_unreadable_poll_named_by_type_only(self):
        tid = self.start_sleeping()
        other = self.start_sleeping(cwd=self.tmp)               # both started before anything is corrupted
        meta = os.path.join(ct.task_path(tid), "meta.json")
        polls = {"n": 0}
        real_sleep = time.sleep

        def corrupting_sleep(s):
            polls["n"] += 1
            if polls["n"] == 1:                                 # after the first (good) read
                with open(meta, "w") as f:
                    f.write("{ not json: /Users/someone/secret-path")
            real_sleep(s)
        buf = io.StringIO()
        rc = ct.watch([tid], interval_s=0.1, stall_s=0.5, out=buf, sleep=corrupting_sleep)
        text = buf.getvalue()
        # Round 2 (P2-a): every failed read since the good one raised HERE, so this is "the watcher cannot read the
        # record" (unwatchable, exit 2), not "the task stalled on its host"; the line says so, by type only.
        self.assertEqual(rc, 2, text)
        row = json.loads(text)
        self.assertEqual((row["state"], row["last_state"]), ("unwatchable", "running"))
        self.assertIn("local read error", row["error"])
        self.assertIn("on this machine", row["error"])
        self.assertIn("JSONDecodeError", row["error"])
        self.assertGreaterEqual(row["stalled_after_s"], 0.5)
        self.assertNotIn("secret-path", text)                    # the type, never the message (it quotes the content)
        self.assertNotIn("not json", text)
        _, st = self.call("task_status", task_id=other)          # the other task is untouched by that verdict
        self.assertEqual(st["state"], "running")
        # never readable at all: the connect rule, exit 2
        with open(os.path.join(ct.task_path(other), "meta.json"), "w") as f:
            f.write("garbage")
        rc, rows, text = self.watch(other)
        self.assertEqual(rc, 2, text)
        self.assertEqual(rows[0]["state"], "unwatchable")
        self.assertIn("JSONDecodeError", rows[0]["error"])

    def test_a_task_dir_that_vanishes_mid_watch_is_a_stall_not_a_crash(self):
        tid = self.start_sleeping()
        d = ct.task_path(tid)
        polls = {"n": 0}
        real_sleep = time.sleep

        def removing_sleep(s):
            polls["n"] += 1
            if polls["n"] == 1:
                shutil.rmtree(d)
            real_sleep(s)
        buf = io.StringIO()
        rc = ct.watch([tid], interval_s=0.1, stall_s=0.5, out=buf, sleep=removing_sleep)
        row = json.loads(buf.getvalue())
        self.assertEqual(rc, 1, buf.getvalue())
        self.assertEqual((row["state"], row["last_state"]), ("stalled", "running"))
        self.assertIn("no such task", row["error"])
        # the narrower race (the dir or a file goes between task_path and a read inside status): any non-TaskError
        # is the same unreadable poll, reported by type only
        tid2 = self.start_sleeping(cwd=self.tmp)
        with mock.patch.object(ct, "_last_output_at", side_effect=PermissionError("/Users/someone/private")):
            rc, rows, text = self.watch(tid2, stall_s=0.3)
        self.assertEqual(rc, 2, text)                            # never readable once: the connect rule
        self.assertIn("PermissionError", rows[0]["error"])
        self.assertNotIn("/Users/someone", text)

    def test_a_local_read_error_mixed_with_a_host_answer_is_still_a_stall(self):
        # P2-a's exit 2 needs EVERY failed poll since the last good read to be local: once one of them is the host's
        # or the id's answer (a TaskError: here the dir goes, "no such task"), the task may be running and it is a
        # stall, exit 1, as before.
        tid = self.start_sleeping()
        d = ct.task_path(tid)
        polls = {"n": 0}
        real_sleep = time.sleep

        def corrupt_then_remove(s):
            polls["n"] += 1
            if polls["n"] == 1:
                with open(os.path.join(d, "meta.json"), "w") as f:
                    f.write("garbage")
            elif polls["n"] == 3:
                shutil.rmtree(d)
            real_sleep(s)
        buf = io.StringIO()
        rc = ct.watch([tid], interval_s=0.1, stall_s=0.6, out=buf, sleep=corrupt_then_remove)
        row = json.loads(buf.getvalue())
        self.assertEqual(rc, 1, buf.getvalue())
        self.assertEqual((row["state"], row["last_state"]), ("stalled", "running"))
        self.assertIn("no such task", row["error"])
        self.assertNotIn("local read error", row["error"])
        self.assertGreater(polls["n"], 3)

    def test_a_reply_that_is_not_a_record_is_an_unreadable_poll(self):
        # P2-c: _remote lets a list through; st.get on it must not end the whole watch with a traceback
        self._air()
        _, out = self.call("start_task", task="hello")
        tid = out["task_id"]
        self.call("wait_task", task_id=tid, timeout_s=30)
        reads = {"n": 0}

        def listy(argv, stdin, timeout):
            reads["n"] += 1
            result = {"task_id": tid, "state": "running", "elapsed_s": 1.0} if reads["n"] == 1 else ["not", "a", "record"]
            return 0, json.dumps({"v": ct.exec_hosts.PROTOCOL, "ok": True, "result": result}).encode(), b""
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", listy):
            rc, rows, text = self.watch(tid, stall_s=0.5)       # read once, then unreadable: the host's answer, a stall
        self.assertEqual(rc, 1, text)
        self.assertEqual((rows[0]["state"], rows[0]["last_state"]), ("stalled", "running"))
        self.assertIn("list, not a record", rows[0]["error"])
        self.assertNotIn("Traceback", text)
        reads["n"] = 5                                           # never a record: the connect rule, exit 2
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", listy):
            rc, rows, text = self.watch(tid)
        self.assertEqual(rc, 2, text)
        self.assertEqual(rows[0]["state"], "unwatchable")
        self.assertIn("list, not a record", rows[0]["error"])

    def test_a_far_deadline_never_lengthens_the_ssh_timeout(self):
        # P1: with --timeout 7000 the remote poll used to get a 7000 s ssh timeout; one hung ssh would starve every
        # other task's report. Each ssh call is capped at one RPC (exec_hosts.SSH_RPC_TIMEOUT_S).
        self._air()
        _, out = self.call("start_task", task="hello")
        tid = out["task_id"]
        timeouts = []
        real = ct.exec_hosts.TRANSPORT

        def recording(argv, stdin, timeout):
            timeouts.append(timeout)
            return real(argv, stdin, timeout)
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", recording):
            rc, rows, text = self.watch(tid, timeout_s=7000)
        self.assertEqual(rc, 0, text)
        self.assertTrue(timeouts)
        self.assertLessEqual(max(timeouts), ct.exec_hosts.SSH_RPC_TIMEOUT_S, timeouts)
        self.assertGreater(min(timeouts), ct.RUN_FETCH_MIN_S)
        # wait_task's own status() goes the same way: a deadline under one RPC shrinks the timeout to what is left
        timeouts.clear()
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", recording):
            ct.status(tid, deadline=time.monotonic() + 3)
        self.assertLessEqual(max(timeouts), 3)
        self.assertGreaterEqual(max(timeouts), ct.RUN_FETCH_MIN_S)

    def test_watch_main_turns_an_unexpected_error_into_exit_2_naming_only_the_type(self):
        tid = self.start()
        err = io.StringIO()
        with mock.patch.object(ct, "watch", side_effect=RuntimeError("/Users/someone/private token-ish text")):
            with contextlib.redirect_stderr(err):
                self.assertEqual(ct.watch_main([tid]), 2)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("private", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_queued_task_stalls_only_after_the_long_window(self):
        # the queue is not one deep: a third chrome task waits up to the runner's queue cap, so the window is that cap
        # plus the ordinary one (round 2, P2-b), not the child cap
        self.assertEqual(ct.WATCH_QUEUED_STALL_S, ct.CHROME_QUEUE_MAX_S + ct.WATCH_STALL_S)
        self.assertGreater(ct.WATCH_QUEUED_STALL_S, ct.CHILD_MAX_S_DEFAULT + ct.WATCH_STALL_S)
        tid = self.start_sleeping()
        open(os.path.join(ct.task_path(tid), ct.QUEUED_MARKER), "w").close()   # as the runner does while it waits
        _, st = self.call("task_status", task_id=tid)
        self.assertIn("queued", st["phase"])
        # the ordinary window does not apply while the task has a phase: this run ends by timeout, not stall
        rc, rows, text = self.watch(tid, stall_s=0.2, queued_stall_s=60, timeout_s=0.7)
        self.assertEqual(rc, 3, text)
        self.assertEqual(rows[0]["watch"], "timeout")
        # the long window does: a queue that never moves is reported (a report, not a cancel)
        t0 = time.monotonic()
        rc, rows, text = self.watch(tid, stall_s=0.2, queued_stall_s=0.6)
        self.assertEqual(rc, 1, text)
        row = rows[0]
        self.assertEqual((row["state"], row["last_state"]), ("stalled", "running"))
        self.assertIn("queued", row["last_phase"])
        self.assertIn("queued", row["error"])
        self.assertGreaterEqual(row["stalled_after_s"], 0.6)
        self.assertLess(time.monotonic() - t0, 10)
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "running")
        with self.assertRaises(ct.TaskError):
            self.watch(tid, queued_stall_s=0)

    def test_the_deadline_is_checked_before_every_poll_and_caps_the_ssh_timeout(self):
        self._air()
        tids = []
        for i in range(4):
            _, out = self.call("start_task", task=f"hello {i}")
            tids.append(out["task_id"])
        calls = []

        def slow(argv, stdin, timeout):
            calls.append(timeout)
            time.sleep(0.4)                                     # a slow host: each poll costs 0.4 s
            tid = json.loads(stdin)["args"]["task_id"]
            return 0, json.dumps({"v": ct.exec_hosts.PROTOCOL, "ok": True,
                                  "result": {"task_id": tid, "state": "running", "elapsed_s": 1.0}}).encode(), b""
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", slow):
            t0 = time.monotonic()
            rc, rows, text = self.watch(*tids, timeout_s=0.5)
            took = time.monotonic() - t0
        self.assertEqual(rc, 3, text)
        self.assertEqual(sorted(r["task_id"] for r in rows), sorted(tids))    # every task gets its timeout line
        self.assertTrue(all(r["watch"] == "timeout" for r in rows))
        self.assertLessEqual(len(calls), 2, calls)              # not one full pass of four slow polls (1.6 s)
        self.assertLess(took, 1.3, took)
        self.assertLessEqual(max(calls), max(ct.RUN_FETCH_MIN_S, 0.5))   # ssh timeout shrunk from 30 s to the time left

    # ---- the command start_task hands back
    def test_start_task_returns_a_watch_command_for_this_machine_that_runs(self):
        r, out = self.call("start_task", task="hello", cwd=self.work)
        self.assertFalse(r.get("isError"), r)
        cmd = out["watch_command"]
        argv = shlex.split(cmd)
        self.assertEqual(argv[0], "env")                        # the test env points tasks and hosts elsewhere
        self.assertIn(f"CORTADOS_TASKS_DIR={os.environ['CORTADOS_TASKS_DIR']}", argv)
        self.assertIn(f"CORTADOS_EXEC_HOSTS_FILE={self.hosts_file}", argv)
        self.assertEqual(argv[-5:], [sys.executable, "-B", str(Path(ct.__file__).resolve()), "--watch", out["task_id"]])
        self.assertTrue(os.path.isabs(argv[-3]))
        self.assertNotIn("tok-test", cmd)
        # the command really runs, as the orchestrator would run it (a subprocess of this module; the fake child)
        p = subprocess.run(argv + ["--interval", "0.1"], capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in os.environ.items() if k not in ("CORTADOS_TASKS_DIR", "CORTADOS_EXEC_HOSTS_FILE")})
        self.assertEqual(p.returncode, 0, p.stderr)
        row = json.loads(p.stdout)
        self.assertEqual((row["task_id"], row["state"]), (out["task_id"], "succeeded"))
        self.assertEqual(p.stderr, "")

    def test_watch_command_has_no_env_prefix_when_the_defaults_are_in_force(self):
        with mock.patch.dict(os.environ, {"CORTADOS_TASKS_DIR": "", "CORTADOS_EXEC_HOSTS_FILE": ""}):
            cmd = ct.watch_command("t20261109000000-000000aa")
        self.assertEqual(shlex.split(cmd), [sys.executable, "-B", str(Path(ct.__file__).resolve()), "--watch",
                                            "t20261109000000-000000aa"])

    def test_the_cli_usage_mentions_watch(self):
        p = subprocess.run([sys.executable, "-B", str(Path(ct.__file__)), "--nonsense"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--watch", p.stderr)

    def test_start_task_tool_description_tells_the_orchestrator_to_run_it(self):
        from cortados_router import subagent_mcp as m
        desc = [t for t in m.CHILD_TOOLS if t["name"] == "start_task"][0]["description"]
        self.assertIn("watch_command", desc)
        self.assertIn("BACKGROUND", desc)


if __name__ == "__main__":
    unittest.main()
