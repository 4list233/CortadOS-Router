"""Nested child tasks (docs/NESTED-TASKS.md): depth 0 main session -> depth 1 sub-orchestrator (`delegate: true`) ->
depth 2 workers. A FAKE claude binary plays the sub-orchestrator and the workers: it starts tasks exactly as the
cortados-route server inside a child does (subagent_mcp.call_child_tool in a process under the child's runner), so the
depth check sees a real process tree. No real claude, no ssh (a subprocess transport), no launchctl, throwaway state."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

from cortados_router import child_tasks as ct
from cortados_router import subagent_mcp as m
from tests.test_child_tasks import ChildTaskBase, _load

CLAIMS = ("CORTADOS_CHILD_TASK", "CORTADOS_TASK_ID", "CORTADOS_TASK_DEPTH", "CORTADOS_TASK_DELEGATE")

NESTED_FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, subprocess, sys, time
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    cwd = os.getcwd()
    argv = sys.argv[1:]

    def dump(name, obj):
        with open(os.path.join(cwd, name), "w") as f:
            json.dump(obj, f)

    dump("fake-argv.json", {{"argv": argv, "prompt": prompt,
         "env": {{k: os.environ.get(k) for k in {claims!r}}},
         "mcp": json.load(open(argv[argv.index("--mcp-config") + 1])) if "--mcp-config" in argv else None}})
    # what the child's cortados-route server does for a start_task call, in a process under this child
    STARTER = (
        "import json, sys\\n"
        "sys.path.insert(0, sys.argv[1])\\n"
        "from cortados_router import subagent_mcp as m\\n"
        "print(json.dumps(m.call_child_tool('start_task', json.loads(sys.argv[2]), json.loads(sys.argv[3]))))\\n")

    def start_child(args, extra):
        env = dict(os.environ)
        env.update(extra)
        if os.environ.get("FAKE_GC_MAX_S"):     # the worker's own cap, apart from this child's (CORTADOS_CHILD_MAX_S)
            env["CORTADOS_CHILD_MAX_S"] = os.environ["FAKE_GC_MAX_S"]
        if os.environ.get("FAKE_SCRUB"):         # the forgery: every claim variable removed
            for k in {claims!r}:
                env.pop(k, None)
        p = subprocess.run([sys.executable, "-B", "-c", STARTER, os.environ["FAKE_REPO"], json.dumps(args),
                            os.environ["FAKE_CFG"]], capture_output=True, text=True, env=env, timeout=120)
        try:
            r = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            return {{"ok": False, "error": "starter crashed: " + p.stderr[-3000:]}}
        text = r["content"][0]["text"]
        return {{"ok": False, "error": text}} if r.get("isError") else {{"ok": True, "out": json.loads(text)}}

    def result(text):
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text}}))
        sys.stdout.flush()
        sys.exit(0)

    if mode == "try-start":
        r = start_child(json.loads(os.environ.get("FAKE_START_ARGS") or '{{"task": "x"}}'), {{}})
        dump("start-result.json", r)
        result("tried")
    if mode in ("suborch", "suborch-sleep", "suborch-leave", "suborch-handoff", "suborch-fail"):
        args = {{"task": "piece", "cwd": os.environ["FAKE_GC_CWD"]}}
        args.update(json.loads(os.environ.get("FAKE_GC_ARGS") or "{{}}"))
        r = start_child(args, {{"FAKE_CLAUDE_MODE": os.environ.get("FAKE_GC_MODE", "ok")}})
        dump("start-result.json", r)
        if not r["ok"]:
            result("could not start: " + r["error"])
        gid = r["out"]["task_id"]
        if mode == "suborch-sleep":
            time.sleep(60)
        if mode == "suborch-leave":
            result("started a worker and left; worker " + gid + ": running")
        if mode == "suborch-handoff":
            result("started a worker\\nHANDOFF: " + gid + " (still running, yours now)")
        if mode == "suborch-fail":
            print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True,
                               "result": "gave up"}}))
            sys.stdout.flush()
            sys.exit(1)
        # the documented foreground wait, with a short timeout so the exit-3 loop runs here
        cmd = r["out"]["wait_command"].replace(" --timeout 540", " --timeout 1 --interval 0.2")
        codes = []
        for _ in range(60):
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            codes.append(p.returncode)
            if p.returncode != 3:
                break
        dump("wait-codes.json", {{"codes": codes, "last": p.stdout, "cmd": r["out"]["wait_command"]}})
        result("worker " + gid + ": " + json.loads(p.stdout.strip().splitlines()[-1])["state"])
    if mode == "slow-ok":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "2.5")))
        result("piece done")
    if mode == "sleep":
        time.sleep(60)
    result("done")
''')


class NestedBase(ChildTaskBase):
    def setUp(self):
        super().setUp()
        fake = os.path.join(self.tmp, "fake-nested-claude")
        with open(fake, "w") as f:
            f.write(NESTED_FAKE.format(py=sys.executable, claims=CLAIMS))
        os.chmod(fake, 0o755)
        p = mock.patch.dict(os.environ, {"CORTADOS_CHILD_CLAUDE_BIN": fake, "FAKE_REPO": str(ct.REPO_ROOT),
                                         "FAKE_CFG": json.dumps(self.cfg)})
        p.start()
        self.addCleanup(p.stop)
        for k in CLAIMS:
            os.environ.pop(k, None)

    def folder(self, name):
        d = os.path.join(self.tmp, name)
        os.mkdir(d)
        return d

    def start_task(self, **kw):
        r, out = self.call("start_task", task=kw.pop("task", "hello"), **kw)
        self.assertFalse(r.get("isError"), r)
        return out

    def task_dirs(self):
        return os.listdir(ct.tasks_dir()) if os.path.isdir(ct.tasks_dir()) else []

    def wait_done(self, tid, timeout=90):
        end = time.time() + timeout
        while True:
            r, w = self.call("wait_task", task_id=tid, timeout_s=20)
            self.assertFalse(r.get("isError"), r)
            if w["state"] in ct.TERMINAL_STATES or time.time() > end:
                break
        self.assertIn(w["state"], ct.TERMINAL_STATES, w)
        return w

    def events(self):
        path = os.path.join(os.environ["CORTADOS_HOME"], ct.EVENT_LOG)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(x) for x in f if x.strip()]

    def meta(self, tid, root=None):
        return _load(os.path.join(root or ct.tasks_dir(), tid, "meta.json"))


class DelegatePlumbingTests(NestedBase):
    def test_a_sub_orchestrator_gets_its_directive_the_route_server_and_its_record(self):
        w1 = self.folder("w1")
        out = self.start_task(cwd=w1, delegate=True)
        self.assertEqual(out["depth"], 1)
        self.assertTrue(out["delegate"])
        self.assertNotIn("wait_command", out)                      # depth 0 waits in the background as before
        self.wait_done(out["task_id"])
        seen = _load(os.path.join(w1, "fake-argv.json"))
        a = seen["argv"]
        self.assertEqual(a[a.index("--append-system-prompt") + 1], ct.SUB_ORCHESTRATOR_DIRECTIVE)
        self.assertIn("cortados-route", seen["mcp"]["mcpServers"])
        self.assertNotIn("--strict-mcp-config", a)                 # added to the host's servers, not instead of them
        self.assertEqual(seen["env"], {"CORTADOS_CHILD_TASK": "1", "CORTADOS_TASK_ID": out["task_id"],
                                       "CORTADOS_TASK_DEPTH": "1", "CORTADOS_TASK_DELEGATE": "1"})
        meta = self.meta(out["task_id"])
        self.assertEqual((meta["depth"], meta["delegate"]), (1, True))

    def test_a_plain_child_is_unchanged(self):
        w1 = self.folder("w1")
        out = self.start_task(cwd=w1)
        self.assertEqual(out["depth"], 1)
        self.assertNotIn("delegate", out)
        self.wait_done(out["task_id"])
        seen = _load(os.path.join(w1, "fake-argv.json"))
        a = seen["argv"]
        self.assertEqual(a[a.index("--append-system-prompt") + 1], ct.CHILD_DIRECTIVE)
        self.assertIsNone(seen["mcp"])
        self.assertIsNone(seen["env"]["CORTADOS_TASK_DELEGATE"])
        self.assertEqual(seen["env"]["CORTADOS_TASK_ID"], out["task_id"])
        self.assertEqual(self.meta(out["task_id"])["delegate"], False)

    def test_delegate_must_be_a_boolean_and_is_not_used_with_chrome(self):
        r, _ = self.call("start_task", task="x", cwd=self.work, delegate="yes")
        self.assertIn("delegate must be true or false", r["content"][0]["text"])
        r, _ = self.call("start_task", task="x", cwd=self.work, delegate=True, chrome=True)
        self.assertIn("not used with chrome", r["content"][0]["text"])
        self.assertEqual(self.task_dirs(), [])


class DepthRuleTests(NestedBase):
    """check_depth on its own: every forbidden depth, and what is allowed."""
    SUB = {"task_id": "t20261125000000-aaaaaaaa", "dir": "/nonexistent", "depth": 1, "delegate": True, "host": "here"}
    PLAIN = dict(SUB, delegate=False)
    WORKER = dict(SUB, depth=2, delegate=False)

    def test_allowed(self):
        self.assertEqual(ct.check_depth(None, False, False), 1)
        self.assertEqual(ct.check_depth(None, True, False), 1)
        self.assertEqual(ct.check_depth(None, False, True), 1)
        self.assertEqual(ct.check_depth(self.SUB, False, False), 2)

    def test_refused(self):
        cases = [(self.WORKER, False, False, None, "workers never start tasks"),
                 (self.PLAIN, False, False, None, "not started with delegate: true"),
                 (self.SUB, True, False, None, "only the main session may start a sub-orchestrator"),
                 (self.SUB, False, True, None, "chrome: true refused from a sub-orchestrator"),
                 (None, True, False, 2, "only the main session may start a sub-orchestrator"),
                 (None, False, True, 2, "chrome: true refused"),
                 (None, False, False, 3, "at most 2 levels"),
                 (self.SUB, False, False, 3, "at most 2 levels")]
        for caller, delegate, chrome, req, text in cases:
            with self.subTest(text=text, req=req):
                with self.assertRaises(ct.TaskError) as cm:
                    ct.check_depth(caller, delegate, chrome, req)
                self.assertIn(text, str(cm.exception))
        refused = [e for e in self.events() if e["event"] == "refused"]
        self.assertEqual(len(refused), len(cases))
        self.assertTrue(all(e["reason"] == "depth_limit" for e in refused))

    def test_a_requesting_hosts_depth_only_ever_deepens(self):
        self.assertEqual(ct.check_depth(None, False, False, 2), 2)
        self.assertEqual(ct.check_depth(self.SUB, False, False, 1), 2)
        with self.assertRaises(ct.TaskError):
            ct.check_depth(None, False, False, 0)


class AncestryTests(NestedBase):
    def test_the_nearest_runner_among_the_ancestors_is_the_caller(self):
        root = ct.tasks_dir()
        sub, gc = f"{root}/t20261125000000-aaaaaaaa", f"{root}/t20261125000001-bbbbbbbb"
        table = {50: (40, "python3 -c start"), 40: (30, "claude -p"),
                 30: (20, f"/usr/bin/python3 -B /x/cortados_router/child_tasks.py --run {gc} --lock-fd 7"),
                 20: (10, "python3 subagent_mcp.py"), 10: (5, f"python3 -B /x/cortados_router/child_tasks.py --run {sub}"),
                 5: (1, "zsh")}
        self.assertEqual(ct._ancestor_run_dir(50, table), gc)
        self.assertEqual(ct._ancestor_run_dir(20, table), sub)     # launchd form: no --lock-fd
        self.assertIsNone(ct._ancestor_run_dir(5, table))
        loop = {7: (8, "a"), 8: (7, "b")}                          # a cycle in a racy table ends, it never spins
        self.assertIsNone(ct._ancestor_run_dir(7, loop))

    def test_scope_tasks_dir_ignores_a_runner_of_another_root(self):
        other = "/elsewhere/tasks/t20261125000000-aaaaaaaa"
        table = {50: (10, "python3"), 10: (1, f"python3 child_tasks.py --run {other} --lock-fd 3")}
        self.assertIsNone(ct._ancestor_run_dir(50, table))         # the suite's scope (tests/__init__.py)
        with mock.patch.object(ct, "ANCESTRY_SCOPE", "any"):
            self.assertEqual(ct._ancestor_run_dir(50, table), other)

    def test_a_failed_ps_refuses_rather_than_reading_as_depth_zero(self):
        with mock.patch.object(ct.subprocess, "run", side_effect=OSError("no ps")):
            r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertIn("cannot tell whether the caller runs inside a child task", r["content"][0]["text"])
        self.assertEqual(self.task_dirs(), [])

    def test_a_claim_without_a_runner_ancestor_is_refused(self):
        for k, v in (("CORTADOS_CHILD_TASK", "1"), ("CORTADOS_TASK_ID", "t20261125000000-aaaaaaaa")):
            with self.subTest(k=k), mock.patch.dict(os.environ, {k: v}):
                r, _ = self.call("start_task", task="x", cwd=self.work)
                self.assertIn("no running child task on this host is its ancestor", r["content"][0]["text"])

    def test_a_detached_claim_of_a_running_task_gets_that_tasks_record(self):
        w1 = self.folder("w1")
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            out = self.start_task(cwd=w1)                          # a plain child, running
        try:
            with mock.patch.dict(os.environ, {"CORTADOS_TASK_ID": out["task_id"]}):
                ident = ct.caller_identity()
                self.assertEqual((ident["task_id"], ident["depth"], ident["delegate"]), (out["task_id"], 1, False))
                r, _ = self.call("start_task", task="x", cwd=self.work)
            self.assertIn("not started with delegate: true", r["content"][0]["text"])
        finally:
            ct.cancel(out["task_id"])
            self.wait_done(out["task_id"])


class ProcessTreeTests(NestedBase):
    """The real thing: children start tasks from a process under their runner, with and without their claim vars."""

    def run_child(self, mode, delegate=False, **env):
        w1 = self.folder("w1")
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": mode, **env}):
            out = self.start_task(cwd=w1, delegate=delegate)
        w = self.wait_done(out["task_id"])
        return out, w, w1

    def test_a_plain_child_cannot_start_even_with_its_env_scrubbed(self):
        for scrub in ("", "1"):
            with self.subTest(scrub=scrub):
                out, w, w1 = self.run_child("try-start", FAKE_SCRUB=scrub,
                                            FAKE_START_ARGS=json.dumps({"task": "x", "cwd": self.work}))
                r = _load(os.path.join(w1, "start-result.json"))
                self.assertFalse(r["ok"], r)
                self.assertIn("not started with delegate: true", r["error"])
                os.rename(w1, w1 + scrub + "-done")
        refused = [e for e in self.events() if e["event"] == "refused"]
        self.assertEqual(len(refused), 2)
        self.assertEqual({e["caller_depth"] for e in refused}, {1})

    def test_a_worker_cannot_start_and_a_sub_orchestrator_cannot_make_another(self):
        gc = self.folder("gc")
        # the worker tries to start, its env scrubbed (the forgery)
        out, w, w1 = self.run_child("suborch", delegate=True, FAKE_GC_MODE="try-start", FAKE_GC_CWD=gc,
                                    FAKE_START_ARGS=json.dumps({"task": "x", "cwd": self.work}), FAKE_SCRUB="1")
        self.assertEqual(w["state"], "succeeded", w)
        r = _load(os.path.join(gc, "start-result.json"))
        self.assertFalse(r["ok"], r)
        self.assertIn("workers never start tasks", r["error"])
        # the sub-orchestrator asks for a sub-orchestrator
        gc2 = self.folder("gc2")
        os.rename(w1, w1 + "-1")
        out, w, w1 = self.run_child("suborch", delegate=True, FAKE_GC_CWD=gc2,
                                    FAKE_GC_ARGS=json.dumps({"delegate": True}))
        r = _load(os.path.join(w1, "start-result.json"))
        self.assertIn("only the main session may start a sub-orchestrator", r["error"])
        # and a chrome worker
        os.rename(w1, w1 + "-2")
        out, w, w1 = self.run_child("suborch", delegate=True, FAKE_GC_CWD=gc2,
                                    FAKE_GC_ARGS=json.dumps({"chrome": True}))
        r = _load(os.path.join(w1, "start-result.json"))
        self.assertIn("chrome: true refused from a sub-orchestrator", r["error"])
        self.assertEqual(os.listdir(gc2), [])                      # nothing started

    def test_sub_orchestrator_end_to_end_parent_host_origin_and_the_foreground_wait(self):
        gc = self.folder("gc")
        origin = {"session": "ab" * 8, "project": "proj", "device": "dev", "machine": None}
        with mock.patch.object(m, "task_origin", return_value=origin):
            out, w, w1 = self.run_child("suborch", delegate=True, FAKE_GC_MODE="slow-ok", FAKE_GC_CWD=gc)
        self.assertEqual(w["state"], "succeeded", w)
        sub = out["task_id"]
        started = _load(os.path.join(w1, "start-result.json"))["out"]
        gid = started["task_id"]
        self.assertEqual(started["depth"], 2)
        self.assertEqual(started["parent_task_id"], sub)
        self.assertEqual(started["host"], "here")                  # the sub-orchestrator's own host by default
        self.assertTrue(started["wait_command"].endswith(f"--watch {gid} --timeout {ct.SUB_WAIT_TIMEOUT_S}"))
        self.assertEqual(started["origin_session"], "ab" * 8)      # the main session's, inherited from the parent
        gmeta = self.meta(gid)
        self.assertEqual((gmeta["depth"], gmeta["delegate"], gmeta["parent_task_id"]), (2, False, sub))
        self.assertEqual(gmeta["origin"]["origin_session"], "ab" * 8)
        # the foreground wait: exit 3 at least once while the worker ran, then 0 when it succeeded
        codes = _load(os.path.join(w1, "wait-codes.json"))["codes"]
        self.assertEqual(codes[-1], 0, codes)
        self.assertIn(3, codes)
        self.assertEqual(w["result"], f"worker {gid}: succeeded")
        # the sub-orchestrator's status lists its children with their states, and no warning (none left running)
        self.assertEqual(w["children"], [{"task_id": gid, "state": "succeeded", "host": "here"}])
        self.assertFalse([x for x in w.get("warnings", []) if "child task" in x], w.get("warnings"))
        self.assertEqual(w["depth"], 1)
        _, gst = self.call("task_status", task_id=gid)
        self.assertEqual((gst["depth"], gst["parent_task_id"]), (2, sub))
        started_events = {e["task_id"]: e for e in self.events() if e["event"] == "started"}
        self.assertEqual(started_events[gid]["parent_task_id"], sub)

    def test_the_live_tree_nests_the_worker_under_its_sub_orchestrator(self):
        from tests import test_orchestrator_tree as tot
        gc = self.folder("gc")
        tag = "ef" * 8
        origin = {"session": tag, "project": "proj", "device": "dev", "machine": None}
        with mock.patch.object(m, "task_origin", return_value=origin):
            out, w, w1 = self.run_child("suborch", delegate=True, FAKE_GC_MODE="slow-ok", FAKE_GC_CWD=gc)
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        evs = self.events()                     # the real event lines this start and its worker wrote
        now = time.time()
        b = tot.board()
        v = tot.view(child_events=lambda: evs, now=now, remote_stubs=lambda: [],
                     office_view=lambda ev: b.view(ev, now=now, uncapped=True))
        mine = tot.groups(v)[tag]
        self.assertEqual([t["task_id"] for t in mine["tasks"]], [out["task_id"]])
        sub = mine["tasks"][0]
        self.assertEqual([(t["task_id"], t["parent_task_id"]) for t in sub["tasks"]], [(gid, out["task_id"])])
        # and the session's own view (the pulse pane) has the same two levels
        mine = tot.view(tag, child_events=lambda: evs, now=now, remote_stubs=lambda: [],
                        office_view=lambda ev: b.view(ev, now=now, uncapped=True))["orchestrators"]["items"][0]
        self.assertEqual(tot.ids(mine), [out["task_id"], gid])

    def test_cancelling_a_sub_orchestrator_cancels_its_workers(self):
        gc = self.folder("gc")
        w1 = self.folder("w1")
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "suborch-sleep", "FAKE_GC_MODE": "sleep",
                                          "FAKE_GC_CWD": gc}):
            out = self.start_task(cwd=w1, delegate=True)
        sub = out["task_id"]
        deadline = time.time() + 60
        while not os.path.exists(os.path.join(w1, "start-result.json")) and time.time() < deadline:
            time.sleep(0.1)
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        _, c = self.call("cancel_task", task_id=sub)
        self.assertTrue(c["cancelled"], c)
        self.assertEqual(c["children"], [{"task_id": gid, "state": "cancelling", "cancelled": True}])
        self.assertEqual(self.wait_done(gid)["state"], "cancelled")
        self.assertEqual(self.wait_done(sub)["state"], "cancelled")
        # once marked, the sub-orchestrator can start nothing more
        caller = ct._identity_of(os.path.join(ct.tasks_dir(), sub))
        with self.assertRaises(ct.TaskError) as cm:
            with ct._children_guard(caller):
                pass
        self.assertIn("is being cancelled", str(cm.exception))

    def test_a_sub_orchestrator_that_leaves_a_worker_running_unnamed_is_incomplete_and_the_worker_cancelled(self):
        # owner default: listing the worker's id and state is not a handoff; only a HANDOFF line is
        gc = self.folder("gc")
        out, w, w1 = self.run_child("suborch-leave", delegate=True, FAKE_GC_MODE="sleep", FAKE_GC_CWD=gc)
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        self.assertEqual(w["state"], "failed", w)
        self.assertEqual(w["outcome"], "incomplete")
        self.assertIn(gid, w["error"])
        self.assertIn("HANDOFF", w["error"])
        self.assertEqual(w["children_cancelled"], [gid])
        self.assertEqual(self.wait_done(gid)["state"], "cancelled")

    def test_a_named_handoff_keeps_running_and_the_sub_orchestrator_succeeds(self):
        gc = self.folder("gc")
        out, w, w1 = self.run_child("suborch-handoff", delegate=True, FAKE_GC_MODE="sleep", FAKE_GC_CWD=gc)
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        try:
            self.assertEqual(w["state"], "succeeded", w)
            self.assertNotIn("outcome", w)
            self.assertEqual(w["children_running"], [gid])
            self.assertEqual(w["children"], [{"task_id": gid, "state": "running", "host": "here"}])
            _, g = self.call("task_status", task_id=gid)
            self.assertEqual(g["state"], "running")
        finally:
            ct.cancel(gid)
            self.wait_done(gid)

    def test_a_failed_sub_orchestrator_cancels_its_workers(self):
        gc = self.folder("gc")
        out, w, w1 = self.run_child("suborch-fail", delegate=True, FAKE_GC_MODE="sleep", FAKE_GC_CWD=gc)
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        self.assertEqual(w["state"], "failed", w)
        self.assertNotIn("outcome", w)                             # failed on its own, not "incomplete"
        self.assertEqual(w["children_cancelled"], [gid])
        self.assertEqual(self.wait_done(gid)["state"], "cancelled")

    def test_a_sub_orchestrator_at_its_run_time_cap_cancels_its_workers(self):
        gc = self.folder("gc")
        out, w, w1 = self.run_child("suborch-sleep", delegate=True, FAKE_GC_MODE="sleep", FAKE_GC_CWD=gc,
                                    CORTADOS_CHILD_MAX_S="6", FAKE_GC_MAX_S="600")
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        self.assertEqual(w["state"], "failed", w)
        self.assertTrue(w.get("timed_out"), w)
        self.assertEqual(w["children_cancelled"], [gid])
        self.assertEqual(self.wait_done(gid)["state"], "cancelled")

    def test_a_cancel_of_a_finished_sub_orchestrator_still_cancels_its_running_workers(self):
        gc = self.folder("gc")
        out, w, w1 = self.run_child("suborch-handoff", delegate=True, FAKE_GC_MODE="sleep", FAKE_GC_CWD=gc)
        gid = _load(os.path.join(w1, "start-result.json"))["out"]["task_id"]
        c = ct.cancel(out["task_id"])
        self.assertFalse(c["cancelled"])                           # itself: already ended
        self.assertEqual(c["children"][0]["task_id"], gid)
        self.assertTrue(c["children"][0]["cancelled"])
        self.assertEqual(self.wait_done(gid)["state"], "cancelled")


class RemoteNestingTests(NestedBase):
    """A sub-orchestrator naming another host: the requesting side computes the depth and keeps the child record."""

    def fake_sub(self):
        root = ct._private_root()
        d = os.path.join(root, "t20261125000000-aaaaaaaa")
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": os.path.basename(d), "depth": 1, "delegate": True,
                                                      "origin": {"origin_session": "cd" * 8}})
        return ct._identity_of(d)

    def test_a_worker_on_another_host_is_recorded_at_depth_two_under_its_parent(self):
        self._air()
        sub = self.fake_sub()
        with mock.patch.object(ct, "caller_identity", return_value=sub):
            out = self.start_task(cwd=None, host="air")
        tid = out["task_id"]
        self.assertEqual((out["depth"], out["parent_task_id"]), (2, sub["task_id"]))
        air_meta = self.meta(tid, os.path.join(self.air_home, "tasks"))
        self.assertEqual((air_meta["depth"], air_meta["delegate"], air_meta["parent_task_id"]),
                         (2, False, sub["task_id"]))
        self.assertEqual(air_meta["origin"]["origin_session"], "cd" * 8)   # the parent's, sent along
        stub = _load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))
        self.assertEqual(stub["parent_task_id"], sub["task_id"])
        self.assertEqual(ct.children_of(sub["dir"]), [tid])
        self.assertNotIn("warnings", out)                         # the host confirmed the depth
        self.wait_done(tid)

    def test_a_host_that_does_not_confirm_the_depth_is_named_in_a_warning(self):
        self._air()
        real = ct._remote

        def old_host(host_name, op, args, timeout_s=None):     # an older checkout: no depth in its reply
            out = real(host_name, op, args, timeout_s)
            if op == "start":
                out.pop("depth", None)
                out.pop("delegate", None)
            return out
        with mock.patch.object(ct, "_remote", side_effect=old_host):
            out = self.start_task(cwd=None, host="air", delegate=True)
        self.assertTrue(any("did not confirm depth 1 or delegate: true" in x for x in out["warnings"]), out)
        self.wait_done(out["task_id"])

    def test_the_host_refuses_what_the_sent_depth_forbids(self):
        self._air()
        sub = self.fake_sub()
        with mock.patch.object(ct, "caller_identity", return_value=sub):
            r, _ = self.call("start_task", task="x", host="air", delegate=True)
        self.assertIn("only the main session may start a sub-orchestrator", r["content"][0]["text"])
        # the host's own check, given depth 2 and delegate directly (a requester that skipped its own check)
        req = {"v": ct.exec_hosts.PROTOCOL, "op": "start", "host": "air",
               "args": {"task": "x", "depth": 2, "delegate": True}}
        code, so, _ = self._run_air(req)
        self.assertIn("only the main session may start a sub-orchestrator", json.loads(so)["error"])

    def _run_air(self, req):
        argv = ["ssh", "-o", "BatchMode=yes", "--", "air-fake", "x"]
        return ct.exec_hosts.TRANSPORT(argv, json.dumps(req).encode(), 60)


OK_RESULT = {"exit_code": 0, "cancelled": False, "ended_at": 1.0, "export": {"ok": True},
             "output": {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}}


class LifecycleFixTests(NestedBase):
    """Fix round 1 (review r1): every finding reproduced, then shown fixed."""

    def fake_sub(self, running=True):
        """A sub-orchestrator's record, no runner: within its start grace it reads as running (still starting)."""
        root = ct._private_root()
        d = os.path.join(root, "t20261125000000-aaaaaaaa")
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": os.path.basename(d), "depth": 1, "delegate": True,
                                                      "started_at": time.time() if running else 1.0,
                                                      "origin": {"origin_session": "cd" * 8}})
        return ct._identity_of(d)

    def air_tasks(self):
        d = os.path.join(self.air_home, "tasks")
        return sorted(x for x in os.listdir(d) if ct.TASK_ID_RE.fullmatch(x)) if os.path.isdir(d) else []

    # --- P1: a lost remote-start reply ---------------------------------------------------------------------------
    def test_a_lost_start_reply_is_recorded_unconfirmed_then_reconciled_and_cancelled_by_the_cascade(self):
        self._air()
        sub = self.fake_sub()
        real = ct.exec_hosts.TRANSPORT

        def lose_start_reply(argv, stdin, timeout):
            rc, so, se = real(argv, stdin, timeout)            # the host really starts it...
            if json.loads(stdin)["op"] == "start":
                return 0, b"", b""                              # ...and the reply is lost
            return rc, so, se
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", lose_start_reply), \
                mock.patch.object(ct, "caller_identity", return_value=sub), \
                mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            r, _ = self.call("start_task", task="x", host="air")
        msg = r["content"][0]["text"]
        self.assertIn("start_unconfirmed", msg)
        kids = ct.children_of(sub["dir"])
        self.assertEqual(len(kids), 1)                          # under its parent: the cascade reaches it
        tid = kids[0]
        self.assertIn(tid, msg)
        self.assertEqual(self.air_tasks(), [tid])               # the host holds it under the requester's id
        self.assertTrue(_load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))[ct.START_UNCONFIRMED])
        # status by that id reconciles it
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual((st["state"], st["depth"], st["parent_task_id"]), ("running", 2, sub["task_id"]))
        self.assertNotIn(ct.START_UNCONFIRMED, _load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB)))
        # and the parent's cancel cascades to it on its host
        c = ct.cancel(sub["task_id"])
        self.assertEqual([(x["task_id"], x["cancelled"]) for x in c["children"]], [(tid, True)])
        self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    def test_a_start_whose_reply_and_arrival_are_both_late_is_tombstoned_by_cancel(self):
        self._air()
        sub = self.fake_sub()
        held = {}

        def time_out(argv, stdin, timeout):                     # the request is in flight when ssh gives up
            if json.loads(stdin)["op"] == "start":
                held["req"] = stdin
                raise subprocess.TimeoutExpired(argv, timeout)
            return real(argv, stdin, timeout)
        real = ct.exec_hosts.TRANSPORT
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", time_out), \
                mock.patch.object(ct, "caller_identity", return_value=sub):
            r, _ = self.call("start_task", task="x", host="air")
            self.assertIn("start_unconfirmed", r["content"][0]["text"])
            tid = ct.children_of(sub["dir"])[0]
            # status says what it is, and how to settle it
            r, _ = self.call("task_status", task_id=tid)
            self.assertIn("cancel_task " + tid, r["content"][0]["text"])
            c = ct.cancel(sub["task_id"])                       # the cascade: the host tombstones the id
        self.assertEqual([(x["task_id"], x["cancelled"]) for x in c["children"]], [(tid, True)])
        # the start arrives late: idempotent on its id, nothing is launched
        code, so, _ = real(["ssh", "-o", "BatchMode=yes", "--", "air-fake", "x"], held["req"], 60)
        rep = json.loads(so)["result"]
        self.assertEqual((rep["task_id"], rep["state"], rep["duplicate"]), (tid, "cancelled", True))
        tdir = os.path.join(self.air_home, "tasks", tid)
        self.assertFalse(os.path.exists(os.path.join(tdir, "meta.json")))      # never started
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "cancelled")

    def test_the_remote_start_is_idempotent_on_its_id(self):
        self._air()
        req = {"v": ct.exec_hosts.PROTOCOL, "op": "start", "host": "air",
               "args": {"task": "x", "depth": 1, "task_id": "t20261125000000-bbbbbbbb"}}
        argv = ["ssh", "-o", "BatchMode=yes", "--", "air-fake", "x"]
        first = json.loads(ct.exec_hosts.TRANSPORT(argv, json.dumps(req).encode(), 60)[1])["result"]
        second = json.loads(ct.exec_hosts.TRANSPORT(argv, json.dumps(req).encode(), 60)[1])["result"]
        self.assertEqual(first["task_id"], "t20261125000000-bbbbbbbb")
        self.assertNotIn("duplicate", first)
        self.assertEqual((second["task_id"], second["duplicate"]), (first["task_id"], True))
        self.assertEqual(self.air_tasks(), ["t20261125000000-bbbbbbbb"])
        with self.assertRaises(ct.TaskError):                   # a caller never names the id
            ct.start("x", self.work, task_id="t20261125000000-cccccccc")

    def test_a_clean_refusal_by_the_host_leaves_no_record(self):
        self._air()
        sub = self.fake_sub()
        with mock.patch.object(ct, "caller_identity", return_value=sub):
            r, _ = self.call("start_task", task="x", host="air", cwd="/no/such/dir/anywhere")
        self.assertTrue(r.get("isError"))
        self.assertEqual(ct.children_of(sub["dir"]), [])
        self.assertEqual([x for x in os.listdir(ct.tasks_dir()) if ct.TASK_ID_RE.fullmatch(x)], [sub["task_id"]])

    # --- P2: a start racing a cancel whose lock wait timed out ---------------------------------------------------
    def test_a_start_racing_a_timed_out_cancel_cancels_the_worker_it_launched(self):
        w1, gc = self.folder("w1"), self.folder("gc")
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            out = self.start_task(cwd=w1, delegate=True)        # a real sub-orchestrator, running
        sub = ct._identity_of(os.path.join(ct.tasks_dir(), out["task_id"]))
        in_guard, release = threading.Event(), threading.Event()
        real_record = ct._record_child

        def slow_record(caller, child_id):                      # past the marker check, holding the children lock
            in_guard.set()
            release.wait(30)
            return real_record(caller, child_id)
        box = {}

        def starter():
            try:
                box["out"] = ct.start("piece", gc, provider_url=self.cfg["url"], token_file=self.cfg["token_file"])
            except ct.TaskError as exc:
                box["err"] = str(exc)
        with mock.patch.object(ct, "_record_child", slow_record), \
                mock.patch.object(ct, "caller_identity", return_value=sub), \
                mock.patch.object(ct, "CHILDREN_LOCK_WAIT_S", 0.3), \
                mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            t = threading.Thread(target=starter)
            t.start()
            self.assertTrue(in_guard.wait(30))
            c = ct.cancel(out["task_id"])                       # its lock wait times out: marked without it
            self.assertIn("marked cancelled without it", c.get("note", ""))
            self.assertNotIn("children", c)                     # the worker was not recorded yet
            release.set()
            t.join(60)
        self.assertNotIn("out", box)
        self.assertIn("was cancelled while this start ran", box["err"])
        gid = ct.children_of(sub["dir"])[0]
        self.assertIn(f"the worker {gid} it launched was cancelled", box["err"])
        self.assertEqual(self.wait_done(gid)["state"], "cancelled")
        self.assertEqual(self.wait_done(out["task_id"])["state"], "cancelled")

    # --- P2: every child, however many -----------------------------------------------------------------------
    def test_a_cancel_cascade_reaches_all_501_children(self):
        sub = self.fake_sub()
        kids = [f"t20261125{i:06d}-0000000a" for i in range(501)]
        os.mkdir(os.path.join(sub["dir"], ct.CHILDREN_DIR))
        for k in kids:
            open(os.path.join(sub["dir"], ct.CHILDREN_DIR, k), "w").close()
        seen = []
        with mock.patch.object(ct, "_cancel_child", lambda k: seen.append(k) or {"task_id": k, "cancelled": True}):
            c = ct.cancel(sub["task_id"])
        self.assertEqual(seen, kids)
        self.assertEqual(len(c["children"]), 501)
        st = ct.status(sub["task_id"])                          # presentation stays bounded
        self.assertEqual((len(st["children"]), st["children_total"]), (ct.CHILDREN_SHOWN, 501))

    def test_201_completed_children_are_reported_as_completed(self):
        sub = self.fake_sub()
        root = ct.tasks_dir()
        kids = [f"t20261125{i:06d}-0000000b" for i in range(201)]
        os.mkdir(os.path.join(sub["dir"], ct.CHILDREN_DIR))
        for k in kids:
            os.mkdir(os.path.join(root, k), 0o700)
            ct._write_json(os.path.join(root, k, "meta.json"), {"task_id": k, "started_at": 1.0, "host": "here"})
            ct._write_json(os.path.join(root, k, "result.json"), OK_RESULT)
            open(os.path.join(sub["dir"], ct.CHILDREN_DIR, k), "w").close()
        result = json.loads(json.dumps(OK_RESULT))
        with mock.patch.object(ct, "_cancel_child", side_effect=AssertionError("nothing to cancel")):
            ct._children_report(sub["dir"], result)
        self.assertEqual(len(result["children"]), 201)
        self.assertEqual({c["state"] for c in result["children"]}, {"succeeded"})
        for k in ("children_running", "children_warning", "outcome", "error"):
            self.assertNotIn(k, result)
        self.assertEqual(ct.classify(result), "succeeded")

    def test_remote_children_are_read_by_id_in_batches_and_unreachable_is_not_running(self):
        self._air(transport=lambda a, s, t: (255, b"", b"down"))
        sub = self.fake_sub()
        root = ct.tasks_dir()
        kids = [f"t20261125{i:06d}-0000000c" for i in range(3)]
        for k in kids:
            ct._write_stub(root, k, "air", {}, sub["task_id"])
            ct._record_child(sub, k)
        result = json.loads(json.dumps(OK_RESULT))
        ct._children_report(sub["dir"], result)
        self.assertEqual({c["state"] for c in result["children"]}, {"unreachable"})
        self.assertNotIn("children_running", result)
        self.assertEqual([u["task_id"] for u in result["children_unreachable"]], kids)
        self.assertIn("could not be cancelled", result["children_warning"])
        self.assertEqual(result["outcome"], "incomplete")       # their end is not known: not a success
        # batches: the host is asked by explicit ids, CHILDREN_LIST_BATCH at a time
        calls = []

        def fake_remote(host, op, args, timeout_s=None):
            calls.append((op, list(args.get("ids") or [])))
            if op == "list":
                return [{"task_id": k, "state": "succeeded"} for k in args["ids"]]
            raise AssertionError(op)
        with mock.patch.object(ct, "_remote", fake_remote), mock.patch.object(ct, "CHILDREN_LIST_BATCH", 2):
            rows = ct._children_states(kids)
        self.assertEqual(calls, [("list", kids[:2]), ("list", kids[2:])])
        self.assertEqual({r["state"] for r in rows.values()}, {"succeeded"})

    # --- older hosts: the capability handshake --------------------------------------------------------------
    def test_delegate_to_a_host_without_the_capabilities_is_refused_before_launch(self):
        self._air()
        real = ct._remote
        ops = []

        def old_host(host_name, op, args, timeout_s=None):
            ops.append(op)
            if op == "capabilities":
                raise ct.TaskError("unknown remote operation 'capabilities'")
            return real(host_name, op, args, timeout_s)
        with mock.patch.object(ct, "_remote", side_effect=old_host):
            r, _ = self.call("start_task", task="x", host="air", delegate=True)
            self.assertIn("did not confirm it supports nested tasks", r["content"][0]["text"])
            self.assertEqual(ops, ["capabilities"])             # nothing was sent to start
            self.assertEqual(self.air_tasks(), [])
            self.assertEqual(self.task_dirs(), [])
            # a plain task stays backward compatible: no handshake
            out = self.start_task(cwd=None, host="air")
        self.assertNotIn("capabilities", ops[1:])
        self.wait_done(out["task_id"])


class RouteTaskInsideChildTests(NestedBase):
    def test_route_task_is_allowed_to_a_sub_orchestrator_only(self):
        sub = {"task_id": "t20261125000000-aaaaaaaa", "dir": "/x", "depth": 1, "delegate": True, "host": "here"}
        with mock.patch.dict(os.environ, {m.NESTED_ENV: "1"}):
            with mock.patch.object(ct, "caller_identity", return_value=sub):
                r = m.route_task("", "", "task")
                self.assertIn("not configured", r["content"][0]["text"])      # past the child refusal
            for ident in (dict(sub, delegate=False), dict(sub, depth=2, delegate=False), None):
                with mock.patch.object(ct, "caller_identity", return_value=ident):
                    r = m.route_task("", "", "task")
                    self.assertIn("not available inside a CortadOS child task", r["content"][0]["text"])
            with mock.patch.object(ct, "caller_identity", side_effect=ct.TaskError("ps failed")):
                r = m.route_task("", "", "task")
                self.assertIn("not available inside a CortadOS child task", r["content"][0]["text"])

    def test_the_start_task_schema_offers_delegate(self):
        props = next(t for t in m.CHILD_TOOLS if t["name"] == "start_task")["inputSchema"]["properties"]
        self.assertEqual(props["delegate"]["type"], "boolean")


if __name__ == "__main__":
    unittest.main()
