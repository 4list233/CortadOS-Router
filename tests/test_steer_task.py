"""steer_task and the sub-orchestrator cap (docs/STEER-TASK.md). Fakes only: a fake claude binary that runs the hooks
its settings file declares, the suite's throwaway tasks dir and CORTADOS_HOME, and a subprocess transport in place of
ssh. No real child, ssh or launchctl."""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from cortados_router import bot_gateway, session, steer
from cortados_router import child_tasks as ct
from cortados_router import subagent_mcp as m
from cortados_router.provider import live, office
from cortados_router.provider import server as provider_server
from tests.test_child_tasks import ChildTaskBase

NOTE = "Use the blue config file, not the red one.\n\tThen stop after step 2."

# The fake child: records the hooks its settings declare, says "ready", waits for "go", then plays three tool
# boundaries (a subagent's call, a main-loop call, a main-loop failure), records each hook's output, and stays running
# until "end". FAKE_SELF_STEER: before that, it calls steer_task on itself from a process under its runner.
STEER_FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, subprocess, sys, time
    prompt = sys.stdin.read()
    argv = sys.argv[1:]
    cwd = os.getcwd()
    settings = json.load(open(argv[argv.index("--settings") + 1]))
    hooks = settings.get("hooks", {{}})
    json.dump({{"argv": argv, "hooks": hooks}}, open(os.path.join(cwd, "fake-argv.json"), "w"))

    def run_hook(event, extra):
        outs = []
        for entry in hooks.get(event, []):
            for h in entry["hooks"]:
                p = subprocess.run(h["command"], shell=True, capture_output=True, text=True, timeout=30,
                                   input=json.dumps({{"hook_event_name": event, "tool_name": "Bash", **extra}}))
                outs.append({{"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}})
        return outs

    if os.environ.get("FAKE_SELF_STEER"):
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from cortados_router import subagent_mcp as m; "
                "print(json.dumps(m.call_child_tool('steer_task', {{'task_id': sys.argv[2], 'message': 'x'}}, {{}})))")
        p = subprocess.run([sys.executable, "-B", "-c", code, os.environ["FAKE_REPO"], os.environ["CORTADOS_TASK_ID"]],
                           capture_output=True, text=True, timeout=60)
        json.dump({{"stdout": p.stdout, "stderr": p.stderr[-2000:]}}, open(os.path.join(cwd, "self-steer.json"), "w"))
    open(os.path.join(cwd, "ready"), "w").close()
    deadline = time.time() + 90
    while not os.path.exists(os.path.join(cwd, "go")) and time.time() < deadline:
        time.sleep(0.05)
    calls = {{"subagent": run_hook("PostToolUse", {{"agent_id": "a-sub-1"}}),
             "first": run_hook("PostToolUse", {{}}),
             "second": run_hook("PostToolUseFailure", {{}})}}
    tmp = os.path.join(cwd, "hook-calls.tmp")
    json.dump(calls, open(tmp, "w"))
    os.rename(tmp, os.path.join(cwd, "hook-calls.json"))
    while not os.path.exists(os.path.join(cwd, "end")) and time.time() < deadline:
        time.sleep(0.05)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "steered"}}))
''')


def _wait_for(path, timeout=60):
    end = time.time() + timeout
    while not os.path.exists(path):
        if time.time() > end:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.05)


def _events(path):
    try:
        with open(path) as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except FileNotFoundError:
        return []


class SteerBase(ChildTaskBase):
    def setUp(self):
        super().setUp()
        fake = os.path.join(self.tmp, "fake-steer-claude")
        with open(fake, "w") as f:
            f.write(STEER_FAKE.format(py=sys.executable))
        os.chmod(fake, 0o755)
        os.environ["CORTADOS_CHILD_CLAUDE_BIN"] = fake       # restored by ChildTaskBase's patch.dict
        os.environ["FAKE_REPO"] = str(ct.REPO_ROOT)
        self.log = os.path.join(os.environ["CORTADOS_HOME"], ct.EVENT_LOG)

    def steer_call(self, task_id, message=NOTE):
        return self.call("steer_task", task_id=task_id, message=message)

    def assert_no_text(self, blob, where):
        for piece in ("blue config", "stop after step 2", NOTE):
            self.assertNotIn(piece, blob, where)


class LocalSteerTests(SteerBase):
    def test_a_note_is_delivered_once_at_the_next_main_loop_tool_boundary(self):
        tid = self.start(task="work on it")
        _wait_for(os.path.join(self.work, "ready"))
        argv = json.load(open(os.path.join(self.work, "fake-argv.json")))
        self.assertEqual(sorted(argv["hooks"]), ["PostToolUse", "PostToolUseFailure", "PreToolUse"])
        r, out = self.steer_call(tid)
        self.assertFalse(r.get("isError"), r)
        self.assertEqual((out["task_id"], out["state"], out["pending"]), (tid, "queued", 1))
        self.assertRegex(out["steer_id"], ct.STEER_ID_RE)
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["steer"], {"pending": 1, "delivered": 0})
        open(os.path.join(self.work, "go"), "w").close()
        _wait_for(os.path.join(self.work, "hook-calls.json"))
        calls = json.load(open(os.path.join(self.work, "hook-calls.json")))
        self.assertEqual([c["stdout"] for c in calls["subagent"]], [""])      # a subagent's call delivers nothing
        [first] = calls["first"]
        self.assertEqual(first["rc"], 0)
        hso = json.loads(first["stdout"])["hookSpecificOutput"]
        self.assertEqual(hso, {"hookEventName": "PostToolUse",
                               "additionalContext": "Steering note from the owner's orchestrator: " + NOTE.strip()})
        self.assertEqual([c["stdout"] for c in calls["second"]], [""])        # once: the next boundary has nothing
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["steer"], {"pending": 0, "delivered": 1})
        open(os.path.join(self.work, "end"), "w").close()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        self.assertEqual(w["steer"], {"pending": 0, "delivered": 1})
        evs = _events(self.log)
        sent = [e for e in evs if e["event"] == "steer_sent"]
        got = [e for e in evs if e["event"] == "steer_delivered"]
        self.assertEqual([(e["task_id"], e["steer_id"]) for e in sent], [(tid, out["steer_id"])])
        self.assertEqual([(e["task_id"], e["steer_id"]) for e in got], [(tid, out["steer_id"])])
        self.assertEqual(set(got[0]), {"ts", "event", "task_id", "steer_id"})   # metadata only
        with open(self.log) as f:
            self.assert_no_text(f.read(), "child-tasks.jsonl")
        # the views built from that log: no note text, and the steering events are not taken for task states
        now = time.time()
        recs = office.read_child_events(pathlib.Path(self.log), now)
        self.assert_no_text(json.dumps(office._children(recs, now)), "office")
        self.assertEqual(office._children(recs, now)[tid]["final"], "done")
        self.assert_no_text(json.dumps(live.tasks_section(recs, None, None, now)), "live")
        # an ended task is refused, and nothing is queued for it
        r, _ = self.steer_call(tid, "again")
        self.assertTrue(r.get("isError"))
        self.assertIn("has ended (succeeded)", r["content"][0]["text"])
        self.assertEqual(steer.counts(w["dir"]), {"pending": 0, "delivered": 1})

    def test_unknown_and_malformed_ids_and_bad_notes_are_refused(self):
        r, _ = self.steer_call("t20261127000000-00000000")
        self.assertIn("no such task", r["content"][0]["text"])
        r, _ = self.steer_call("nope")
        self.assertIn("invalid task_id", r["content"][0]["text"])
        tid = self.start()
        _wait_for(os.path.join(self.work, "ready"))
        for bad, why in (("   ", "empty"), ("x" * (steer.MAX_NOTE_CHARS + 1), "at most 2000"),
                         ("ring \x07 bell", "non-printable"), ("esc \x1b[31m", "non-printable")):
            r, _ = self.steer_call(tid, bad)
            self.assertTrue(r.get("isError"), bad)
            self.assertIn(why, r["content"][0]["text"])
        r, _ = self.call("steer_task", task_id=tid)
        self.assertTrue(r.get("isError"))                                    # message is required
        self.assertEqual(self.call("task_status", task_id=tid)[1]["steer"], {"pending": 0, "delivered": 0})
        self.assertFalse([e for e in _events(self.log) if e["event"] == "steer_sent"])
        ct.cancel(tid)

    def test_a_plain_child_cannot_steer_itself_by_the_process_tree(self):
        os.environ["FAKE_SELF_STEER"] = "1"
        tid = self.start()
        _wait_for(os.path.join(self.work, "ready"))
        res = json.load(open(os.path.join(self.work, "self-steer.json")))
        r = json.loads(res["stdout"].strip().splitlines()[-1])
        self.assertTrue(r.get("isError"), res)
        self.assertIn("plain child", r["content"][0]["text"])
        ct.cancel(tid)
        refused = [e for e in _events(self.log) if e["event"] == "refused" and e.get("reason") == "steer_scope"]
        self.assertEqual([(e["task_id"], e["caller_task_id"]) for e in refused], [(tid, tid)])

    def test_scope_worker_plain_child_and_foreign_task_refused_own_worker_allowed(self):
        tid = self.start()
        _wait_for(os.path.join(self.work, "ready"))
        root = ct._private_root()

        def ident(name, depth, delegate, kids=()):
            d = os.path.join(root, name)
            os.mkdir(d, 0o700)
            ct._write_json(os.path.join(d, "meta.json"), {"task_id": name, "depth": depth, "delegate": delegate})
            for k in kids:
                os.makedirs(os.path.join(d, ct.CHILDREN_DIR), exist_ok=True)
                open(os.path.join(d, ct.CHILDREN_DIR, k), "w").close()
            return ct._identity_of(d)

        worker = ident("t20261127000001-00000001", 2, False)
        plain = ident("t20261127000002-00000002", 1, False)
        other_sub = ident("t20261127000003-00000003", 1, True)
        own_sub = ident("t20261127000004-00000004", 1, True, kids=[tid])
        for caller, why in ((worker, "worker"), (plain, "plain child"), (other_sub, "not one of this")):
            with mock.patch.object(ct, "caller_identity", return_value=caller):
                r, _ = self.steer_call(tid)
            self.assertTrue(r.get("isError"), why)
            self.assertIn(why, r["content"][0]["text"])
        with mock.patch.object(ct, "caller_identity", side_effect=ct.TaskError("ps failed")):
            r, _ = self.steer_call(tid)
        self.assertIn("cannot tell who is calling", r["content"][0]["text"])
        self.assertEqual(steer.counts(ct.task_path(tid)), {"pending": 0, "delivered": 0})
        with mock.patch.object(ct, "caller_identity", return_value=own_sub):
            r, out = self.steer_call(tid)
        self.assertFalse(r.get("isError"), r)
        self.assertEqual(out["pending"], 1)
        sent = [e for e in _events(self.log) if e["event"] == "steer_sent"]
        self.assertEqual(sent[-1]["caller_task_id"], own_sub["task_id"])
        refused = [e for e in _events(self.log) if e["event"] == "refused" and e.get("reason") == "steer_scope"]
        self.assertEqual(len(refused), 4)
        ct.cancel(tid)

    def test_pending_notes_are_bounded(self):
        tid = self.start()
        _wait_for(os.path.join(self.work, "ready"))
        for i in range(steer.MAX_PENDING):
            r, _ = self.steer_call(tid, f"note {i}")
            self.assertFalse(r.get("isError"), r)
        r, _ = self.steer_call(tid, "one too many")
        self.assertIn("already waiting", r["content"][0]["text"])
        ct.cancel(tid)

    def test_tools_list_and_the_bot_gateway(self):
        r = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.cfg)
        [tool] = [t for t in r["result"]["tools"] if t["name"] == "steer_task"]
        self.assertEqual(tool["inputSchema"]["required"], ["task_id", "message"])
        self.assertNotIn("steer_task", bot_gateway.EXPOSED)                   # owner-only: the bot never steers
        r, _ = self.call("steer_task", task_id="t20261127000000-00000000", message="x", extra=1)
        self.assertIn("unknown argument 'extra'", r["content"][0]["text"])


class RemoteSteerTests(SteerBase):
    def test_a_note_for_a_remote_task_is_forwarded_to_its_host_and_delivered_there(self):
        self._air()
        tid = self.start(cwd=None, host="air")
        air_log = os.path.join(self.air_home, "state", ct.EVENT_LOG)
        end = time.time() + 60
        while True:
            _, st = self.call("task_status", task_id=tid)
            path = (st.get("workspace") or {}).get("path")
            if path and os.path.exists(os.path.join(path, "ready")):
                break
            self.assertLess(time.time(), end, st)
            time.sleep(0.2)
        before = len(self.transport_calls)
        r, out = self.steer_call(tid)
        self.assertFalse(r.get("isError"), r)
        self.assertEqual((out["task_id"], out["host"], out["pending"]), (tid, "air", 1))
        self.assertGreater(len(self.transport_calls), before)               # forwarded over the (fake) ssh hop
        # the note sits in the HOST's task dir, not in this machine's stub
        self.assertEqual(steer.counts(os.path.join(self.air_home, "tasks", tid)), {"pending": 1, "delivered": 0})
        self.assertFalse(os.path.exists(os.path.join(ct.task_path(tid), steer.NOTES)))
        open(os.path.join(path, "go"), "w").close()
        _wait_for(os.path.join(path, "hook-calls.json"))
        calls = json.load(open(os.path.join(path, "hook-calls.json")))
        self.assertIn(NOTE.strip(), json.loads(calls["first"][0]["stdout"])["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(calls["second"][0]["stdout"], "")
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["steer"], {"pending": 0, "delivered": 1})
        open(os.path.join(path, "end"), "w").close()
        end = time.time() + 60
        while self.call("task_status", task_id=tid)[1]["state"] == "running":
            self.assertLess(time.time(), end)
            time.sleep(0.2)
        here = [e for e in _events(self.log) if e["event"] == "steer_sent"]
        self.assertEqual([(e["task_id"], e["steer_id"], e["host"]) for e in here], [(tid, out["steer_id"], "air")])
        there = _events(air_log)
        self.assertEqual([e["steer_id"] for e in there if e["event"] == "steer_sent"], [out["steer_id"]])
        self.assertEqual([e["steer_id"] for e in there if e["event"] == "steer_delivered"], [out["steer_id"]])
        for p in (self.log, air_log):
            with open(p) as f:
                self.assert_no_text(f.read(), p)

    def _stub(self, **flags):
        tid = "t20261127100000-0000beef"
        ct._write_stub(ct._private_root(), tid, "air")
        if flags:
            p = os.path.join(ct._private_root(), tid, ct.REMOTE_STUB)
            ct._write_json(p, {**json.load(open(p)), **flags})
        return tid

    def test_a_host_without_the_steer_capability_is_refused_before_anything_is_sent(self):
        seen = []

        def transport(argv, stdin, timeout):
            op = json.loads(stdin)["op"]
            seen.append(op)
            if op == "capabilities":
                old = [c for c in ct.REMOTE_CAPABILITIES if c != "steer"]
                return 0, json.dumps({"v": ct.exec_hosts.PROTOCOL, "ok": True,
                                      "result": {"capabilities": old}}).encode(), b""
            self.fail(f"nothing but the handshake may be sent, got {op}")
        self._air(transport=transport, caps=False)
        tid = self._stub()
        r, _ = self.steer_call(tid)
        self.assertTrue(r.get("isError"))
        text = r["content"][0]["text"]
        self.assertIn("steer_task refused", text)
        self.assertIn("lacks steer", text)
        self.assertIn("update this host's cortados-router checkout", text)
        self.assertEqual(seen, ["capabilities"])
        self.assertFalse([e for e in _events(self.log) if e["event"] == "steer_sent"])

    def test_unconfirmed_failed_and_cancelling_stubs_are_refused_without_a_round_trip(self):
        self._air(transport=lambda a, s, t: self.fail("no round trip expected"), caps=False)
        for flags, why in (({ct.START_UNCONFIRMED: True}, "not confirmed"),
                           ({ct.START_FAILED: "gone"}, ct.START_FAILED)):
            tid = self._stub(**flags)
            r, _ = self.steer_call(tid)
            self.assertIn(why, r["content"][0]["text"])
            shutil.rmtree(os.path.join(ct._private_root(), tid))
        tid = self._stub()
        open(os.path.join(ct._private_root(), tid, ct.CANCEL_PENDING), "w").close()
        r, _ = self.steer_call(tid)
        self.assertIn("being cancelled", r["content"][0]["text"])

    def test_the_hosts_absent_answer_is_no_such_task(self):
        self._air()
        tid = self._stub()                       # this machine holds a stub; the Air never had the task
        r, _ = self.steer_call(tid)
        self.assertIn("has no task", r["content"][0]["text"])


class HookUnitTests(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp())

    def hook(self, ev):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(steer.hook(self.d, io.StringIO(json.dumps(ev)), out, err), 0)
        return out.getvalue(), err.getvalue()

    def test_take_is_once_and_bounded_per_boundary(self):
        ids = [steer.add(self.d, "n" * 1990)[0] for _ in range(5)]
        a = steer.take(self.d)
        self.assertEqual([n["id"] for n in a], ids[:3])         # 3 x ~2035 chars fit in 8000, a 4th does not
        self.assertEqual([n["id"] for n in steer.take(self.d)], ids[3:])
        self.assertEqual(steer.take(self.d), [])
        self.assertEqual(steer.counts(self.d), {"pending": 0, "delivered": 5})
        self.assertEqual(steer.delivered_ids(self.d), ids)

    def test_the_hook_returns_additional_context_only_and_always_exits_0(self):
        steer.add(self.d, "turn left")
        out, _ = self.hook({"hook_event_name": "PostToolUseFailure"})
        rec = json.loads(out)
        self.assertEqual(set(rec), {"hookSpecificOutput"})
        self.assertEqual(rec["hookSpecificOutput"], {"hookEventName": "PostToolUseFailure", "additionalContext":
                                                     "Steering note from the owner's orchestrator: turn left"})
        self.assertEqual(self.hook({})[0], "")
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(steer.hook("relative/dir", io.StringIO("{}"), out, err), 0)   # broken: exit 0, one line
        self.assertEqual(out.getvalue(), "")
        self.assertIn("cortados steer hook", err.getvalue())
        self.assertEqual(steer.main(["--wrong"]), 0)

    def test_the_hook_script_runs_isolated_as_installed(self):
        steer.add(self.d, "from the script")
        [entry] = session.steer_hook_entries(self.d)["PostToolUse"]
        cmd = entry["hooks"][0]["command"]
        self.assertIn(" -I -S -B ", cmd)
        p = subprocess.run(cmd, shell=True, input='{"hook_event_name": "PostToolUse"}', capture_output=True,
                           text=True, timeout=30, cwd="/")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("from the script", json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"])


class SessionHookTests(unittest.TestCase):
    def test_settings_carry_the_steering_hook_only_when_asked(self):
        kw = dict(provider_url="http://127.0.0.1:9", token="tok-test-000000001", gate=False)
        self.assertEqual(set(session.settings_content(**kw)["hooks"]), {"PreToolUse"})
        h = session.settings_content(steer_dir="/tmp/x", **kw)["hooks"]
        self.assertEqual(set(h), {"PreToolUse", "PostToolUse", "PostToolUseFailure"})
        self.assertEqual(h["PostToolUse"][0]["matcher"], "*")
        with self.assertRaises(session.SessionError):
            session.steer_hook_entries("relative")
        with self.assertRaises(session.SessionError):        # the script inside the project folder
            session.steer_hook_entries("/tmp/x", project=str(ct.REPO_ROOT))


class CapTests(ChildTaskBase):
    def test_a_sub_orchestrator_gets_6_hours_every_other_child_3(self):
        self.assertEqual((ct._max_s(), ct._max_s(True)), (3 * 3600, 6 * 3600))
        with mock.patch.dict(os.environ, {ct.SUB_MAX_S_ENV: "7"}):
            self.assertEqual((ct._max_s(), ct._max_s(True)), (3 * 3600, 7))
        with mock.patch.dict(os.environ, {ct.MAX_S_ENV: "8"}):
            self.assertEqual((ct._max_s(), ct._max_s(True)), (8, 6 * 3600))
        with mock.patch.dict(os.environ, {ct.SUB_MAX_S_ENV: "soon"}):
            with self.assertRaises(ct.TaskError) as cm:
                ct._max_s(True)
            self.assertIn(ct.SUB_MAX_S_ENV, str(cm.exception))
        plain = self.start()
        sub = self.start(delegate=True)
        for tid, cap, var in ((plain, 3 * 3600, ct.MAX_S_ENV), (sub, 6 * 3600, ct.SUB_MAX_S_ENV)):
            d = ct.task_path(tid)
            self.assertEqual(json.load(open(os.path.join(d, "meta.json")))["max_s"], cap)
            spec = json.load(open(os.path.join(d, "cmd.json")))
            self.assertEqual((spec["max_s"], spec["max_s_env"]), (cap, var))
            self.call("wait_task", task_id=tid, timeout_s=30)
        # the provider keeps a run's long tool-wait limit for as long as the longest cap
        self.assertGreaterEqual(provider_server.CHILD_RUN_MAX_S, ct.SUB_ORCHESTRATOR_MAX_S_DEFAULT)
        # the office and live windows still see a sub-orchestrator running its whole cap
        self.assertGreaterEqual(office.CHILD_WINDOW_S, ct.SUB_ORCHESTRATOR_MAX_S_DEFAULT)

    def test_the_timeout_error_names_the_variable_that_applies(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        with mock.patch.dict(os.environ, {ct.SUB_MAX_S_ENV: "1"}):
            tid = self.start(delegate=True)
        _, w = self.call("wait_task", task_id=tid, timeout_s=40)
        self.assertTrue(w.get("timed_out"), w)
        self.assertIn(ct.SUB_MAX_S_ENV, w["error"])


if __name__ == "__main__":
    unittest.main()
