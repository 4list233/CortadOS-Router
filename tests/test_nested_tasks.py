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
            result("started a worker\\nHANDOFF: " + gid + "  \\n(still running, yours now)")
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
            c = ct.cancel(sub["task_id"])                       # the cascade: the host tombstones the id
        self.assertEqual([(x["task_id"], x["cancelled"], x["state"]) for x in c["children"]],
                         [(tid, True, ct.START_FAILED)])
        # the start arrives late: idempotent on its id, nothing is launched
        code, so, _ = real(["ssh", "-o", "BatchMode=yes", "--", "air-fake", "x"], held["req"], 60)
        rep = json.loads(so)["result"]
        self.assertEqual((rep["task_id"], rep["state"], rep["duplicate"]), (tid, "cancelled", True))
        tdir = os.path.join(self.air_home, "tasks", tid)
        self.assertFalse(os.path.exists(os.path.join(tdir, "meta.json")))      # never started
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], ct.START_FAILED)        # never started there: settled by the host's placeholder

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

class LifecycleFixRound2Tests(NestedBase):
    """Fix round 2 (review r2 on 880f0e0): every finding reproduced, then shown fixed."""

    fake_sub = LifecycleFixTests.fake_sub
    air_tasks = LifecycleFixTests.air_tasks

    def stub_dirs(self):
        return sorted(x for x in self.task_dirs() if ct.TASK_ID_RE.fullmatch(x))

    # --- P1: ssh exit 255 is not proof that nothing launched -----------------------------------------------------
    def test_ssh_255_after_the_launch_keeps_the_worker_under_its_parent_and_the_cascade_cancels_it(self):
        self._air()
        sub = self.fake_sub()
        real = ct.exec_hosts.TRANSPORT

        def drop_after_launch(argv, stdin, timeout):
            rc, so, se = real(argv, stdin, timeout)            # the host really starts it...
            if json.loads(stdin)["op"] == "start":              # ...then the established connection drops
                return 255, b"", b"Connection to air-fake closed by remote host."
            return rc, so, se
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", drop_after_launch), \
                mock.patch.object(ct, "caller_identity", return_value=sub), \
                mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            r, _ = self.call("start_task", task="x", host="air")
        self.assertTrue(r.get("isError"))
        self.assertIn(ct.START_UNCONFIRMED, r["content"][0]["text"])
        kids = ct.children_of(sub["dir"])
        self.assertEqual(len(kids), 1)                          # kept under its parent (was: forgotten)
        tid = kids[0]
        self.assertEqual(self.air_tasks(), [tid])
        self.assertTrue(_load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))[ct.START_UNCONFIRMED])
        c = ct.cancel(sub["task_id"])
        self.assertEqual([(x["task_id"], x["cancelled"]) for x in c["children"]], [(tid, True)])
        self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    # --- r3 P1: stderr is not proof of non-delivery; a failed start's intent is never deleted ----------------------
    REFUSED = b"Warning: x\nssh: connect to host air-fake port 22: Connection refused\n"

    def test_r3_a_worker_launched_then_255_with_a_pre_session_like_line_is_kept_and_cancelled_by_the_cascade(self):
        # The reviewer's reproduction: the remote command starts the worker, then exits 255 with a diagnostic that
        # reads like ssh's own pre-session failure (an ancillary connection's), and no JSON reply.
        self._air()
        sub = self.fake_sub()
        real = ct.exec_hosts.TRANSPORT

        def launch_then_255(argv, stdin, timeout):
            rc, so, se = real(argv, stdin, timeout)
            if json.loads(stdin)["op"] == "start":
                return 255, b"", self.REFUSED
            return rc, so, se
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", launch_then_255), \
                mock.patch.object(ct, "caller_identity", return_value=sub), \
                mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            r, _ = self.call("start_task", task="x", host="air")
        self.assertTrue(r.get("isError"))
        text = r["content"][0]["text"]
        self.assertIn("Connection refused", text)
        kids = ct.children_of(sub["dir"])
        self.assertEqual(len(kids), 1)                          # kept under its parent (r2: deleted)
        tid = kids[0]
        self.assertIn(tid, text)                                # the caller is told the id
        self.assertEqual(self.air_tasks(), [tid])               # the worker runs there
        self.assertTrue(_load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))[ct.START_UNCONFIRMED])
        c = ct.cancel(sub["task_id"])
        self.assertEqual([(x["task_id"], x["cancelled"]) for x in c["children"]], [(tid, True)])
        self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    def test_r3_the_host_saying_no_such_id_makes_it_start_failed_not_running(self):
        self._air()
        sub = self.fake_sub()
        real = ct.exec_hosts.TRANSPORT

        def never_delivered(argv, stdin, timeout):
            if json.loads(stdin)["op"] == "start":
                return 255, b"", self.REFUSED
            return real(argv, stdin, timeout)
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", never_delivered), \
                mock.patch.object(ct, "caller_identity", return_value=sub):
            self.call("start_task", task="x", host="air")
        tid = ct.children_of(sub["dir"])[0]
        self.assertEqual(self.air_tasks(), [])
        # the end report asks the host for exactly this id: start_failed, terminal, kept for the report
        result = dict(OK_RESULT)
        ct._children_report(sub["dir"], result)
        self.assertEqual([(x["task_id"], x["state"]) for x in result["children"]], [(tid, ct.START_FAILED)])
        self.assertNotIn("children_cancelled", result)
        self.assertNotIn("outcome", result)                     # nothing was left running
        stub = _load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))
        self.assertNotIn(ct.START_UNCONFIRMED, stub)
        self.assertTrue(stub[ct.START_FAILED])
        self.assertEqual(ct.children_of(sub["dir"]), [tid])     # the record stays
        # the settle left the host's cancelled placeholder: start_failed stays true, a late start launches nothing
        tdir = os.path.join(self.air_home, "tasks", tid)
        self.assertTrue(os.path.exists(os.path.join(tdir, "cancelled")))
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual((st["state"], st["never_started"]), (ct.START_FAILED, True))

    def test_r3_an_unreachable_host_leaves_it_start_unconfirmed_in_the_report(self):
        self._air()
        sub = self.fake_sub()
        real = ct.exec_hosts.TRANSPORT
        down = mock.patch.object(ct.exec_hosts, "TRANSPORT", lambda a, s, t: (
            real(a, s, t) if json.loads(s)["op"] == "capabilities" else (255, b"", self.REFUSED)))
        down.start()
        self.addCleanup(down.stop)
        with mock.patch.object(ct, "caller_identity", return_value=sub):
            r, _ = self.call("start_task", task="x", host="air")
        self.assertIn(ct.START_UNCONFIRMED, r["content"][0]["text"])
        tid = ct.children_of(sub["dir"])[0]
        result = dict(OK_RESULT)
        ct._children_report(sub["dir"], result)
        self.assertEqual([(x["task_id"], x["state"]) for x in result["children"]], [(tid, ct.START_UNCONFIRMED)])
        self.assertEqual([u["task_id"] for u in result["children_unreachable"]], [tid])
        self.assertTrue(_load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))[ct.START_UNCONFIRMED])
        with self.assertRaises(ct.TaskError) as cm:             # status says what it is; never running or failed
            ct.status(tid)
        self.assertIn(ct.START_UNCONFIRMED, str(cm.exception))
        # the daily pass leaves it as it is while the host does not answer, and prune keeps it however old
        rep = ct.reconcile_unconfirmed()
        self.assertEqual((rep["checked"], rep["settled"], rep["still_unconfirmed"]), (1, 0, [tid]))
        ct.prune(ct.tasks_dir(), now=time.time() + 10 * ct.PRUNE_AGE_S)
        self.assertTrue(os.path.isdir(os.path.join(ct.tasks_dir(), tid)))

    def test_r3_cancel_of_an_unconfirmed_id_writes_the_placeholder_and_a_delayed_start_launches_nothing(self):
        self._air()
        sub = self.fake_sub()
        real = ct.exec_hosts.TRANSPORT
        held = {}

        def hold(argv, stdin, timeout):
            if json.loads(stdin)["op"] == "start":
                held["req"] = stdin
                return 255, b"", self.REFUSED
            return real(argv, stdin, timeout)
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", hold), \
                mock.patch.object(ct, "caller_identity", return_value=sub):
            self.call("start_task", task="x", host="air")
        tid = ct.children_of(sub["dir"])[0]
        c = ct.cancel(tid)
        self.assertEqual((c["state"], c["cancelled"], c["tombstone"]), (ct.START_FAILED, True, True))
        tdir = os.path.join(self.air_home, "tasks", tid)
        self.assertTrue(os.path.exists(os.path.join(tdir, "cancelled")))
        code, so, _ = real(["ssh", "-o", "BatchMode=yes", "--", "air-fake", "x"], held["req"], 60)
        rep = json.loads(so)["result"]
        self.assertEqual((rep["task_id"], rep["state"], rep["duplicate"]), (tid, "cancelled", True))
        self.assertFalse(os.path.exists(os.path.join(tdir, "meta.json")))      # never started
        self.assertEqual(ct.status(tid)["state"], ct.START_FAILED)

    def test_r3_the_daily_pass_settles_an_unconfirmed_start_its_host_never_got(self):
        self._air()
        real = ct.exec_hosts.TRANSPORT
        with mock.patch.object(ct.exec_hosts, "TRANSPORT",
                               lambda a, s, t: (255, b"", b"x") if json.loads(s)["op"] == "start" else real(a, s, t)):
            r, _ = self.call("start_task", task="x", host="air")
        rep = ct.reconcile_unconfirmed()
        self.assertEqual((rep["checked"], rep["settled"], rep["still_unconfirmed"]), (1, 1, []))

    def test_r3_ssh_exit_255_is_always_ambiguous(self):
        self.assertFalse(hasattr(ct.exec_hosts, "PRE_SESSION_RE"))
        host = ct.exec_hosts.Host("air", {"ssh": "air", "hostnames": ["air"], "root": "/r", "router": "/x"})
        for err in (self.REFUSED, b"Host key verification failed.", b""):
            with self.assertRaises(ct.exec_hosts.HostUnreachable) as cm:
                ct.exec_hosts.call(host, "start", {}, transport=lambda a, s, t, e=err: (255, b"", e))
            self.assertTrue(cm.exception.ambiguous, err)

    # --- P2: a start paused past META_GRACE_S, then cancelled -----------------------------------------------------
    def test_a_start_paused_past_the_grace_then_cancelled_launches_nothing(self):
        w = self.folder("w")
        paused, release = threading.Event(), threading.Event()
        real_prepare = ct._prepare

        def slow_prepare(*a, **kw):                             # after mkdir, before meta.json
            paused.set()
            release.wait(30)
            return real_prepare(*a, **kw)
        box = {}

        def starter():
            try:
                box["out"] = ct.start("x", w, provider_url=self.cfg["url"], token_file=self.cfg["token_file"])
            except ct.TaskError as exc:
                box["err"] = str(exc)
        with mock.patch.object(ct, "_prepare", slow_prepare), mock.patch.object(ct, "META_GRACE_S", 0), \
                mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            t = threading.Thread(target=starter)
            t.start()
            self.assertTrue(paused.wait(30))
            [tid] = self.stub_dirs()
            self.assertEqual(ct._state(os.path.join(ct.tasks_dir(), tid))[0], "failed")   # past the grace
            c = ct.cancel(tid)
            self.assertEqual((c["state"], c["cancelled"]), ("cancelling", True))   # was: "already failed", no marker
            release.set()
            t.join(60)
        self.assertNotIn("out", box)
        self.assertIn("cancelled before it launched", box["err"])
        self.assertFalse(os.path.exists(os.path.join(w, "fake-argv.json")))        # claude never ran
        d = os.path.join(ct.tasks_dir(), tid)
        self.assertEqual(ct._state(d)[0], "cancelled")
        self.assertEqual([x for x in os.listdir(d) if x.endswith("settings.json")], [])
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "cancelled")

    def test_a_cancel_marks_an_ended_task_dir_without_changing_its_state(self):
        sub = self.fake_sub(running=False)
        ct._write_json(os.path.join(sub["dir"], "result.json"), OK_RESULT)
        c = ct.cancel(sub["task_id"])
        self.assertEqual((c["state"], c["cancelled"]), ("succeeded", False))
        self.assertTrue(os.path.exists(os.path.join(sub["dir"], "cancelled")))

    # --- P2: a taken id is never removed -------------------------------------------------------------------------
    def _existing(self, tid):
        d = os.path.join(ct._private_root(), tid)
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": 1.0, "host": "here"})
        ct._write_json(os.path.join(d, "result.json"), OK_RESULT)
        return d, sorted(os.listdir(d))

    def test_a_stub_id_collision_leaves_the_existing_task_untouched_and_draws_a_new_id(self):
        self._air()
        taken, fresh = "t20261125000000-dddddddd", "t20261125000001-eeeeeeee"
        d, before = self._existing(taken)
        ids = iter([taken, fresh])
        with mock.patch.object(ct, "_new_id", lambda: next(ids)):
            out = self.start_task(cwd=None, host="air")
        self.assertEqual(out["task_id"], fresh)
        self.assertEqual(sorted(os.listdir(d)), before)
        self.assertEqual(ct._state(d)[0], "succeeded")
        self.assertEqual(self.air_tasks(), [fresh])
        self.wait_done(fresh)

    def test_ids_that_stay_taken_refuse_the_start_and_touch_nothing(self):
        self._air()
        taken = "t20261125000000-dddddddd"
        d, before = self._existing(taken)
        sub = self.fake_sub()
        with mock.patch.object(ct, "_new_id", lambda: taken), mock.patch.object(ct, "caller_identity", return_value=sub):
            r, _ = self.call("start_task", task="x", host="air")
        self.assertIn("fresh task ids were all taken", r["content"][0]["text"])
        self.assertEqual(sorted(os.listdir(d)), before)
        self.assertEqual(ct.children_of(sub["dir"]), [])
        self.assertEqual(self.air_tasks(), [])                  # nothing was sent

    # --- P2: only an exact HANDOFF line is a handoff -------------------------------------------------------------
    def test_only_a_standalone_handoff_line_outside_code_is_a_handoff(self):
        a, b = "t20261125000000-aaaaaaaa", "t20261125000000-bbbbbbbb"
        named = ct._named_handoffs
        for text in (f"No handoff is authorized for {a}",
                     f"> HANDOFF: {a}",
                     f"```\nHANDOFF: {a}\n```",
                     f"~~~text\nHANDOFF: {a}\n~~~",
                     f"  HANDOFF: {a}",
                     f"HANDOFF: {a} (still running)",
                     f"handoff: {a}",
                     f"HANDOFF: {a}x"):
            self.assertEqual(named(text, [a]), [], text)
        self.assertEqual(named(f"done\nHANDOFF: {a} \t\nworker {b}: running", [a, b]), [a])
        self.assertEqual(named(f"```\nx\n```\nHANDOFF: {b}", [a, b]), [b])   # after a closed fence


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


class LifecycleFixRound4Tests(NestedBase):
    """Fix round 4 (review r4 on 8f222bb): every remote start needs the current protocol (cached handshake); a start in
    flight is never reconciled; a structured refusal settles at once; the live view shows start_failed."""

    fake_sub = LifecycleFixTests.fake_sub
    air_tasks = LifecycleFixTests.air_tasks

    def stub_ids(self):
        return sorted(x for x in self.task_dirs() if ct.TASK_ID_RE.fullmatch(x)
                      and os.path.exists(os.path.join(ct.tasks_dir(), x, ct.REMOTE_STUB)))

    def ops_through(self, answer_caps=None):
        """Wrap the Air's transport: record every op; `answer_caps` (a reply dict) replaces the host's own answer."""
        real, ops = ct.exec_hosts.TRANSPORT, []

        def t(argv, stdin, timeout):
            op = json.loads(stdin)["op"]
            ops.append(op)
            if op == "capabilities" and answer_caps is not None:
                return 1, json.dumps({"v": ct.exec_hosts.PROTOCOL, **answer_caps}).encode(), b""
            return real(argv, stdin, timeout)
        p = mock.patch.object(ct.exec_hosts, "TRANSPORT", t)
        p.start()
        self.addCleanup(p.stop)
        return ops, real

    # --- P1 / P3: older hosts are refused before anything is sent, plain starts included ---------------------------
    def test_a_host_without_the_capabilities_is_refused_before_send_plain_and_nested(self):
        self._air()
        older = [{"ok": False, "error": "unknown remote operation 'capabilities'"},          # before round 1
                 {"ok": True, "result": {"capabilities": ["depth", "delegate", "start_id", "cancel_tombstone",
                                                         "status_tombstone"]}}]                  # 8f222bb
        for reply in older:
            ops, _ = self.ops_through(reply)
            for kw in ({}, {"delegate": True}):
                with self.subTest(reply=reply, kw=kw):
                    del ops[:]
                    r, _ = self.call("start_task", task="x", host="air", **kw)
                    text = r["content"][0]["text"]
                    self.assertTrue(r.get("isError"))
                    self.assertIn("update this host's cortados-router checkout", text)
                    self.assertIn("nothing was sent", text)
                    self.assertEqual(ops, ["capabilities"])     # no start request, ever
            if reply["ok"]:
                self.assertIn("lacks start_refusal", text)
        self.assertEqual(self.stub_ids(), [])                   # nothing recorded here
        self.assertEqual(self.air_tasks(), [])                  # nothing launched there

    def test_the_capability_cache_spares_the_handshake_and_a_protocol_error_drops_it(self):
        self._air()
        ops, _ = self.ops_through()
        a = self.start_task(cwd=None, host="air")
        b = self.start_task(cwd=None, host="air")
        self.assertEqual(ops.count("capabilities"), 1)          # the second start: cache hit, one ssh call only
        self.assertEqual(ops.count("start"), 2)
        root, air = ct.tasks_dir(), ct._resolve_host("air")
        self.assertTrue(ct._cached_capabilities(root, air))
        with mock.patch.object(ct.time, "time", return_value=time.time() + ct.CAPABILITY_CACHE_S + 1):
            self.assertFalse(ct._cached_capabilities(root, air))   # expired after 10 min
        # a protocol error from the host (here: an operation it does not know) drops the entry
        with self.assertRaises(ct.TaskError):
            ct._remote("air", "no-such-op", {})
        self.assertFalse(ct._cached_capabilities(root, air))
        self.start_task(cwd=None, host="air")
        self.assertEqual(ops.count("capabilities"), 2)
        for out in (a, b):
            self.wait_done(out["task_id"])

    # --- P2: a start in flight is never reconciled ----------------------------------------------------------------
    def gate_start(self):
        sent, release = threading.Event(), threading.Event()
        real = ct.exec_hosts.TRANSPORT

        def gated(argv, stdin, timeout):
            if json.loads(stdin)["op"] == "start":
                sent.set()
                self.assertTrue(release.wait(60))
            return real(argv, stdin, timeout)
        p = mock.patch.object(ct.exec_hosts, "TRANSPORT", gated)
        p.start()
        self.addCleanup(p.stop)
        box = {}

        def starter():
            try:
                box["out"] = ct.start("x", None, host="air")
            except ct.TaskError as exc:
                box["err"] = str(exc)
        t = threading.Thread(target=starter)
        t.start()
        self.assertTrue(sent.wait(60))
        [tid] = self.stub_ids()
        return tid, release, t, box

    def test_an_in_flight_start_is_skipped_by_reconciliation_and_completes(self):
        # The reviewer's reproduction: the daily pass ran while the start RPC was in flight and tombstoned the id.
        self._air()
        tid, release, t, box = self.gate_start()
        rep = ct.reconcile_unconfirmed()
        self.assertEqual((rep["checked"], rep["settled"], rep["in_progress"]), (1, 0, [tid]))
        st = ct.status(tid)
        self.assertEqual((st["state"], st["start_in_progress"]), (ct.START_UNCONFIRMED, True))
        self.assertEqual(ct.wait(tid, timeout_s=0)["state"], ct.START_UNCONFIRMED)
        self.assertEqual(self.air_tasks(), [])                  # no tombstone was written there
        rows = ct.list_recent()
        self.assertEqual([(r["task_id"], r["state"], r.get("start_in_progress")) for r in rows],
                         [(tid, ct.START_UNCONFIRMED, True)])
        release.set()
        t.join(60)
        self.assertNotIn("err", box)
        self.assertEqual(box["out"]["task_id"], tid)
        self.assertNotIn("duplicate", box["out"])               # was: the start came back cancelled/duplicate
        self.assertNotIn(ct.START_UNCONFIRMED, _load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB)))
        self.assertEqual(self.wait_done(tid)["state"], "succeeded")

    def test_a_crashed_requesters_stub_is_settled_by_reconciliation(self):
        self._air()
        root = ct._private_root()
        tid, fd = ct._new_stub(root, "air", {}, None)           # the requester, before its RPC
        rep = ct.reconcile_unconfirmed()
        self.assertEqual((rep["settled"], rep["in_progress"]), (0, [tid]))
        os.close(fd)                                            # it crashes: the kernel releases the lock
        rep = ct.reconcile_unconfirmed()
        self.assertEqual((rep["checked"], rep["settled"], rep["in_progress"]), (1, 1, []))
        self.assertEqual(ct.status(tid)["state"], ct.START_FAILED)
        self.assertTrue(os.path.exists(os.path.join(self.air_home, "tasks", tid, "cancelled")))   # tombstoned there

    def test_a_cancel_during_an_in_flight_start_reaches_the_host_at_once(self):
        # Round 5 (review r5, P2): the cancel is sent by id even though the start holds START_LOCK; the host writes the
        # cancelled placeholder, so the start (arriving after it) launches nothing. (A sub-orchestrator's cascade
        # cancels each child by id exactly like this.)
        self._air()
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            tid, release, t, box = self.gate_start()
            c = ct.cancel(tid)
            self.assertEqual((c["state"], c["cancelled"], c.get("tombstone")), (ct.START_FAILED, True, True))
            self.assertEqual(self.air_tasks(), [tid])           # the placeholder is there before the start arrives
            self.assertFalse(ct._cancel_pending(os.path.join(ct.tasks_dir(), tid)))   # acknowledged
            release.set()
            t.join(60)
        self.assertNotIn("out", box)
        self.assertIn(f"task {tid} was cancelled while its start ran", box["err"])
        air_d = os.path.join(self.air_home, "tasks", tid)
        self.assertFalse(os.path.exists(os.path.join(air_d, "meta.json")))   # nothing launched there
        self.assertTrue(_load(os.path.join(air_d, "result.json"))["tombstone"])
        self.assertEqual(ct.status(tid)["state"], ct.START_FAILED)

    # --- P3: a structured refusal settles at once; list_tasks never reconciles; the live view shows start_failed ---
    def test_a_structured_refusal_by_the_host_settles_start_failed_immediately(self):
        self._air()
        sub = self.fake_sub()
        ops, _ = self.ops_through()
        with mock.patch.object(ct, "caller_identity", return_value=sub):
            r, _ = self.call("start_task", task="x", host="air", cwd="/no/such/dir/anywhere")
        text = r["content"][0]["text"]
        self.assertTrue(r.get("isError"))
        self.assertIn("does not exist on execution host", text)
        self.assertNotIn("_meta", r)                            # not ambiguous: the host said nothing launched
        tid = ct.children_of(sub["dir"])[0]
        self.assertIn(f"task {tid}: {ct.START_FAILED}", text)
        stub = _load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))
        self.assertNotIn(ct.START_UNCONFIRMED, stub)
        self.assertIn("does not exist on execution host", stub[ct.START_FAILED])
        self.assertEqual(ops, ["capabilities", "start"])        # settled by the start's own reply, nothing more
        self.assertEqual(ct.status(tid)["state"], ct.START_FAILED)
        self.assertEqual(ops, ["capabilities", "start"])        # status answered here
        self.assertEqual(self.air_tasks(), [])
        # an ssh failure is NOT a refusal: it stays start_unconfirmed
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", lambda a, s, t: (255, b"", b"down")):
            r, _ = self.call("start_task", task="x", host="air")
        self.assertIn(ct.START_UNCONFIRMED, r["content"][0]["text"])

    def test_list_tasks_shows_an_unconfirmed_start_without_asking_the_host(self):
        self._air()
        tid, fd = ct._new_stub(ct._private_root(), "air", {}, None)
        os.close(fd)
        with mock.patch.object(ct.exec_hosts, "TRANSPORT", lambda a, s, t: self.fail("list_tasks must not reconcile")):
            _, rows = self.call("list_tasks")
        self.assertEqual([(x["task_id"], x["state"]) for x in rows["tasks"]], [(tid, ct.START_UNCONFIRMED)])
        self.assertTrue(_load(os.path.join(ct.tasks_dir(), tid, ct.REMOTE_STUB))[ct.START_UNCONFIRMED])

    def test_the_live_view_shows_a_settled_start_failed_as_failed(self):
        from cortados_router.provider import live
        root = ct._private_root()
        failed, fd1 = ct._new_stub(root, "air", {}, None)
        pending, fd2 = ct._new_stub(root, "air", {}, None)
        for fd in (fd1, fd2):
            os.close(fd)
        ct._fail_stub(root, failed, "refused by execution host air: no")
        stubs = {r["task_id"]: r for r in ct.remote_stubs()}
        self.assertEqual((stubs[failed]["state"], stubs[pending]["state"]), (ct.START_FAILED, ct.START_UNCONFIRMED))
        self.assertEqual({r["task_id"]: r.get("state") for r in ct.remote_stubs_by_id([failed, pending])},
                         {failed: ct.START_FAILED, pending: ct.START_UNCONFIRMED})
        away = live._elsewhere(ct.remote_stubs(), set(), time.time())
        tree = live.orchestrators_section([], away, None, None, None, None, time.time())
        nodes = {}

        def walk(x):
            if isinstance(x, dict):
                if x.get("task_id") in (failed, pending):
                    nodes[x["task_id"]] = x
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(tree)
        self.assertEqual((nodes[failed]["state"], nodes[failed]["status"]), ("failed", "failed"))
        self.assertIn("never started on air", nodes[failed]["phase"])
        self.assertEqual(nodes[pending]["state"], "elsewhere")
        self.assertIn("start unconfirmed", nodes[pending]["phase"])



class LifecycleFixRound5Tests(NestedBase):
    """Fix round 5 (review r5 on 7a423aa): a durable cancel_pending marker forwarded by reconciliation; an explicit
    cancel always reaches the host, START_LOCK held or not; the capability cache's writes are serialized."""

    air_tasks = LifecycleFixTests.air_tasks
    stub_ids = LifecycleFixRound4Tests.stub_ids

    def transport(self, after_start=False):
        """The Air's transport with a gated start (held before it is sent, or with `after_start` after the host has
        launched, its reply held) and a switch that makes `cancel` fail like a dropped ssh."""
        real = ct.exec_hosts.TRANSPORT
        sent, release, down = threading.Event(), threading.Event(), {"cancel": False}

        def t(argv, stdin, timeout):
            op = json.loads(stdin)["op"]
            if op == "cancel" and down["cancel"]:
                return 255, b"", b"ssh: connection reset"
            if op == "start":
                if after_start:
                    out = real(argv, stdin, timeout)
                    sent.set()
                    self.assertTrue(release.wait(60))
                    return out
                sent.set()
                self.assertTrue(release.wait(60))
            return real(argv, stdin, timeout)
        p = mock.patch.object(ct.exec_hosts, "TRANSPORT", t)
        p.start()
        self.addCleanup(p.stop)
        return sent, release, down

    def air_state(self, tid):
        return ct._remote("air", "status", {"task_id": tid})["state"]

    def crash_after_confirm(self):
        """The reviewer's reproduction of P1: cancel marks the in-flight stub but its own send fails; the start then
        succeeds, _confirm_stub clears start_unconfirmed, and the requester dies before its post-RPC re-check. Returns
        the id of the worker left running on the Air."""
        self._air()
        sent, release, down = self.transport(after_start=True)

        class Crash(BaseException):
            pass
        real_confirm = ct._confirm_stub

        def confirm_then_crash(root, tid):
            real_confirm(root, tid)
            raise Crash()
        box = {}

        def starter():
            try:
                ct.start("x", None, host="air")
            except Crash:
                box["crashed"] = True
        with mock.patch.object(ct, "_confirm_stub", confirm_then_crash):
            th = threading.Thread(target=starter)
            th.start()
            self.assertTrue(sent.wait(60))
            [tid] = self.stub_ids()
            down["cancel"] = True
            c = ct.cancel(tid)
            self.assertEqual((c["state"], c["start_in_progress"]), ("cancelling", True))
            self.assertIn("did not reach the host", c["note"])
            down["cancel"] = False
            release.set()
            th.join(60)
        self.assertTrue(box.get("crashed"))
        d = os.path.join(ct.tasks_dir(), tid)
        self.assertNotIn(ct.START_UNCONFIRMED, _load(os.path.join(d, ct.REMOTE_STUB)))   # confirmed
        self.assertTrue(ct._cancel_pending(d))                                          # the cancel is not lost
        self.assertEqual(self.air_state(tid), "running")                               # the worker runs
        return tid, d

    def test_p1_a_pending_cancel_of_a_confirmed_stub_is_forwarded_by_status(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            tid, d = self.crash_after_confirm()
            ct.status(tid)                                      # reconciliation forwards the cancel by id
            self.assertFalse(ct._cancel_pending(d))             # the host acknowledged it
            self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    def test_p1_a_pending_cancel_of_a_confirmed_stub_is_forwarded_by_the_daily_pass(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            tid, d = self.crash_after_confirm()
            # the stub is not pruned by age while its cancel is pending
            self.assertNotIn(tid, ct.prune(ct.tasks_dir(), now=time.time() + 30 * 86400))
            self.assertTrue(os.path.isdir(d))
            rep = ct.reconcile_unconfirmed()
            self.assertEqual((rep["checked"], rep["cancels_forwarded"], rep["cancel_pending"]), (1, 1, []))
            self.assertFalse(ct._cancel_pending(d))
            self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    def test_p1_an_unreachable_host_keeps_the_cancel_pending(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            tid, d = self.crash_after_confirm()
            with mock.patch.object(ct.exec_hosts, "TRANSPORT", lambda a, s, t: (255, b"", b"down")):
                with self.assertRaises(ct.TaskError) as cm:
                    ct.status(tid)
                self.assertIn(ct.CANCEL_PENDING, str(cm.exception))
                rep = ct.reconcile_unconfirmed()
                self.assertEqual(rep["cancel_pending"], [tid])
            self.assertTrue(ct._cancel_pending(d))
            rows = ct.list_recent()                             # a listing forwards it too once the host answers
            self.assertEqual([(r["task_id"], r.get("cancel_forwarded")) for r in rows], [(tid, True)])
            self.assertFalse(ct._cancel_pending(d))
            self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    def test_p2_a_suspended_requester_does_not_block_an_explicit_cancel_of_a_running_worker(self):
        # The start reached the host (the worker runs there) and the requester is suspended holding START_LOCK.
        self._air()
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "sleep"}):
            sent, release, _ = self.transport(after_start=True)
            box = {}

            def starter():
                try:
                    box["out"] = ct.start("x", None, host="air")
                except ct.TaskError as exc:
                    box["err"] = str(exc)
            th = threading.Thread(target=starter)
            th.start()
            self.assertTrue(sent.wait(60))
            [tid] = self.stub_ids()
            d = os.path.join(ct.tasks_dir(), tid)
            self.assertTrue(ct._start_in_flight(d))
            self.assertTrue(ct.status(tid).get("start_in_progress"))   # passive reconciliation still skips it
            c = ct.cancel(tid)                                  # explicit: sent to the host at once
            self.assertTrue(c["cancelled"], c)
            self.assertTrue(c["start_in_progress"])
            self.assertFalse(ct._cancel_pending(d))
            self.assertIn(self.air_state(tid), ("cancelled", "running"))   # cancelling there
            release.set()                                       # the requester wakes up
            th.join(60)
        self.assertIn(f"task {tid} was cancelled while its start ran", box["err"])
        self.assertEqual(self.wait_done(tid)["state"], "cancelled")

    def test_p2_the_cascade_reaches_a_child_whose_start_is_held(self):
        self._air()
        sub = LifecycleFixTests.fake_sub(self)
        root = ct._private_root()
        tid, fd = ct._new_stub(root, "air", {}, sub["task_id"])   # a requester suspended before its start RPC
        try:
            ct._record_child(sub, tid)
            out = ct.cancel(sub["task_id"])
            [row] = out["children"]
            self.assertEqual((row["task_id"], row["cancelled"]), (tid, True))
            self.assertEqual(self.air_tasks(), [tid])           # the placeholder: a later start launches nothing
            self.assertTrue(_load(os.path.join(self.air_home, "tasks", tid, "result.json"))["tombstone"])
        finally:
            os.close(fd)

    def test_p3_concurrent_cache_writers_cannot_resurrect_an_invalidated_entry(self):
        self._air()
        root, air = ct.tasks_dir(), ct._resolve_host("air")
        os.makedirs(root, exist_ok=True)
        ct._update_capabilities(root, air, list(ct.REMOTE_CAPABILITIES))
        inside, go = threading.Event(), threading.Event()
        real = ct._update_capabilities_locked

        def slow(path, host, have):
            if have is not None:                                # the confirming writer: read, then pause
                inside.set()
                self.assertTrue(go.wait(30))
            return real(path, host, have)
        with mock.patch.object(ct, "_update_capabilities_locked", slow):
            a = threading.Thread(target=ct._update_capabilities, args=(root, air, list(ct.REMOTE_CAPABILITIES)))
            a.start()
            self.assertTrue(inside.wait(30))
            b = threading.Thread(target=ct._update_capabilities, args=(root, air, None))   # the invalidation
            b.start()
            b.join(0.5)
            self.assertTrue(b.is_alive())                       # serialized: it waits for the writer's lock
            go.set()
            a.join(30)
            b.join(30)
        self.assertFalse(ct._cached_capabilities(root, air))    # the invalidation sticks


if __name__ == "__main__":
    unittest.main()
