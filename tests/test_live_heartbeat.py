"""cortados_router/live_heartbeat.py: the host heartbeat sender. Fakes only: a fake launchctl (`run`), a fake hardware
sample, a fake /health fetch, a fake HTTP opener and a fake disk; config and token files in a throwaway directory. No
real launchctl, scorer, provider or network is touched."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
import unittest
import unittest.mock
import urllib.error
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cortados_router import live_heartbeat as hbs
from cortados_router.provider import live

TOKEN = "heartbeat-token-0123456789abcdef"
SAMPLED = round(time.time(), 3)   # the sender keeps a sample time only within the server's clock skew
Usage = namedtuple("Usage", "total used free")

PRINT_RUNNING = """gui/501/com.cortados.provider = {
\tactive count = 1
\tpath = /Users/someone/Library/LaunchAgents/com.cortados.provider.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /bin/zsh
\targuments = {
\t\t/bin/zsh
\t\t-c
\t\tset -a; . "$HOME/.cortados/provider.env"; exec python3 SECRET-ARG
\t}
\tenvironment = {
\t\tCORTADOS_PROVIDER_TOKEN => SECRET-ENV
\t\tstate = nested-ignored
\t}
\truns = 11
\tpid = 88013
\tlast exit code = (never exited)
}
"""
PRINT_DONE = "gui/501/x = {\n\tstate = not running\n\truns = 4\n\tlast exit code = 0\n}\n"
PRINT_FAILED = "gui/501/x = {\n\tstate = not running\n\truns = 1\n\tlast exit code = 78: EX_CONFIG\n}\n"
PRINT_FAILED_NUM = "gui/501/x = {\n\tstate = not running\n\truns = 2\n\tlast exit code = 1\n}\n"
PRINT_ODD = "gui/501/x = {\n\tstate = not running\n\truns = 1\n\tlast exit code = (signal) SIGKILL\n}\n"
PRINT_RUNNING_AFTER_FAIL = "gui/501/x = {\n\tstate = running\n\tpid = 9\n\truns = 3\n\tlast exit code = 78: EX_CONFIG\n}\n"
PRINT_NEVER = "gui/501/x = {\n\tstate = not running\n\truns = 0\n\tlast exit code = (never exited)\n}\n"


def fake_launchctl(outputs):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        label = argv[-1].rsplit("/", 1)[-1]
        out = outputs.get(label)
        if isinstance(out, Exception):
            raise out
        if out is None:
            return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
        return subprocess.CompletedProcess(argv, 0, out, "")
    run.calls = calls
    return run


def fake_sample():
    return {"schema": 1, "sampled_at": SAMPLED, "host": "Owners-MacBook-Air", "os": "Darwin",
            "load_avg": [2.5, 2.0, 1.5], "ncpu": 8, "chip": "Apple M2", "mem_total_bytes": 16 * 2 ** 30,
            "mem_available_bytes": 6 * 2 ** 30, "cpu_busy_pct": 29.84, "gpu_busy_pct": {"available": False,
                                                                                       "reason": "x"},
            "ollama_models": [{"name": "qwen3"}], "errors": [{"field": "gpu_busy_pct", "error": "/usr/sbin/ioreg"}]}


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.data = self.dir / "train" / "data"
        self.data.mkdir(parents=True)
        self.token = self.dir / "token"
        self.token.write_text(TOKEN + "\n")
        os.chmod(self.token, 0o600)

    def config(self, **over):
        cfg = {"version": 1, "host": "studio", "provider_url": "http://provider.test:8432",
               "token_file": str(self.token), "ttl_s": 180,
               "jobs": [{"label": "com.cortados.s1-serve", "name": "s1-scorer"}, "com.cortados.s1-nightly"],
               "hardware": True, "disk": True,
               "s1": {"data_dir": str(self.data), "health_url": "http://127.0.0.1:8431"}}
        cfg.update(over)
        p = self.dir / "heartbeat.json"
        p.write_text(json.dumps(cfg))
        return p

    def main(self, *argv, outputs=None, opener=None, fetch=None, sample=fake_sample, task_events=lambda: [], peek=None):
        run = fake_launchctl(outputs if outputs is not None else
                             {"com.cortados.s1-serve": PRINT_RUNNING, "com.cortados.s1-nightly": PRINT_DONE})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = hbs.main(list(argv), run=run, sample=sample,
                            fetch=fetch or (lambda url: json.dumps({"ok": True, "model": "CortadOS Router S1 v12 (run-a)",
                                                                    "loaded_s": 40.04}).encode()),
                            opener=opener or (lambda req, timeout: FakeResponse(b"{}")),
                            disk_usage=lambda p: Usage(1000, 400, 600), task_events=task_events, peek=peek)
        return code, out.getvalue(), err.getvalue()


class LaunchdTest(Base):
    def test_states(self):
        run = fake_launchctl({"a": PRINT_RUNNING, "b": PRINT_DONE, "c": PRINT_FAILED, "d": PRINT_NEVER,
                              "f": PRINT_FAILED_NUM, "e": OSError("boom"), "g": PRINT_ODD,
                              "h": PRINT_RUNNING_AFTER_FAIL})
        got = {n: hbs.launchd_job(n, n, run) for n in "abcdefgh" + "z"}
        self.assertEqual(got["a"], {"name": "a", "state": "running", "summary": "pid up, 11 runs, never exited"})
        self.assertEqual(got["b"], {"name": "b", "state": "ok", "summary": "4 runs, last exit 0"})
        # launchd words a failure "78: EX_CONFIG": the leading code counts, the wording stays out of the summary
        self.assertEqual(got["c"], {"name": "c", "state": "failed", "summary": "1 run, last exit 78"})
        self.assertEqual(got["g"], {"name": "g", "state": "idle", "summary": "1 run"})   # an odd status: not a guess
        self.assertEqual(got["h"], {"name": "h", "state": "running", "summary": "pid up, 3 runs, last exit 78"})
        self.assertEqual(got["f"], {"name": "f", "state": "failed", "summary": "2 runs, last exit 1"})
        self.assertEqual(got["d"], {"name": "d", "state": "idle", "summary": "0 runs, never exited"})
        self.assertEqual(got["e"], {"name": "e", "state": "stopped", "summary": "launchctl failed: OSError"})
        self.assertEqual(got["z"], {"name": "z", "state": "stopped", "summary": "not loaded"})
        self.assertEqual(run.calls[0], [hbs.LAUNCHCTL, "print", f"gui/{os.getuid()}/a"])
        self.assertNotIn("SECRET", json.dumps(got))

    def test_job_name(self):
        self.assertEqual(hbs.job_name("com.cortados.provider"), "com.cortados.provider")
        self.assertEqual(hbs.job_name("Com.Example.Some Job"), "com.example.some-job")
        self.assertTrue(live._JOB_RE.fullmatch(hbs.job_name("x" * 200)))


class S1Test(Base):
    def test_flags_pending_and_health(self):
        for f in ("no-promote", "nightly.stop", "sweep-request.json"):
            (self.data / f).write_text("SECRET-CONTENT /Users/someone")
        (self.data / "pending-candidate").write_text("/Users/x/sft /Users/x/eval promotion_held run-20261109\n")
        urls = []
        s = hbs.s1_block({"data_dir": str(self.data), "health_url": "http://127.0.0.1:8431/"},
                         lambda u: urls.append(u) or b'{"ok": true, "model": "CortadOS Router S1 v12 (run-a)", "loaded_s": 4}')
        self.assertEqual(s, {"health": "up", "model": "CortadOS Router S1 v12 (run-a)",
                             "flags": ["no-promote", "nightly-stop", "sweep-requested"], "pending": "promotion_held",
                             "loaded_s": 4.0})
        self.assertEqual(urls, ["http://127.0.0.1:8431/health"])
        self.assertNotIn("SECRET", json.dumps(s))
        self.assertNotIn("/Users", json.dumps(s))

    def test_down_and_odd_markers(self):
        (self.data / "pending-candidate").write_text("a b /Users/x/evil c\n")
        s = hbs.s1_block({"data_dir": str(self.data), "health_url": "http://x"}, lambda u: (_ for _ in ()).throw(OSError()))
        self.assertEqual((s["health"], s["pending"], s["flags"], s["model"]), ("down", "unreadable", [], None))
        s = hbs.s1_block({"data_dir": str(self.data), "health_url": "http://x"},
                         lambda u: b'{"ok": false, "model": "at 10.0.0.4"}')
        self.assertEqual((s["health"], s["model"]), ("down", None))
        s = hbs.s1_block({"data_dir": str(self.dir / "absent"), "health_url": "http://x"}, lambda u: b"not json")
        self.assertEqual((s["health"], s["pending"], s["flags"]), ("down", None, []))


class BodyTest(Base):
    def test_dry_run_body_is_metadata_only_and_valid(self):
        (self.data / "no-train-tonight").touch()
        code, out, err = self.main("--config", str(self.config()), "--dry-run")
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertEqual([(j["name"], j["state"]) for j in body["jobs"]],
                         [("s1-scorer", "running"), ("com.cortados.s1-nightly", "ok")])
        self.assertEqual(body["hardware"], {"cpu_busy_pct": 29.8, "gpu_busy_pct": None,
                                            "mem_available_bytes": 6 * 2 ** 30, "mem_total_bytes": 16 * 2 ** 30,
                                            "load_1m": 2.5, "chip": "Apple M2", "sampled_at": SAMPLED,
                                            "disk_free_pct": 60.0})
        self.assertEqual(body["s1"]["flags"], ["no-train-tonight"])
        self.assertEqual(live.validate_heartbeat(body, time.time())[1], None)
        for bad in (TOKEN, "SECRET", "/Users", str(self.dir), "Owners", "qwen3", "ioreg"):
            self.assertNotIn(bad, out, bad)

    def test_no_s1_no_hardware(self):
        code, out, _ = self.main("--config", str(self.config(s1=None, hardware=False, disk=False, tasks=False)),
                                 "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)), {"host", "ttl_s", "jobs"})

    def test_check_uses_the_server_rules(self):
        with self.assertRaises(hbs.ConfigError) as cm:
            hbs.check({"host": "studio", "jobs": [{"name": "x", "state": "exploded"}]})
        self.assertIn("jobs[0].state", str(cm.exception))
        big = {"host": "studio", "jobs": [{"name": f"j{i}", "state": "ok", "summary": "x" * 80} for i in range(16)],
               "hardware": {"chip": "y" * 80}, "s1": {"model": "z" * 80}}
        self.assertLessEqual(len(hbs.check(big)), live.HEARTBEAT_MAX_BYTES)

    def test_bad_configs(self):
        cases = [dict(host="Studio!"), dict(ttl_s=5), dict(jobs=["bad label with spaces"]),
                 dict(jobs=[{"label": "a.b", "name": "Bad Name"}]), dict(jobs=["a.b", {"label": "c.d", "name": "a.b"}]),
                 dict(jobs=[{"label": "a.b", "name": "s1"}]), dict(jobs=[f"j.{i}" for i in range(live.JOBS_MAX)]),
                 dict(s1="yes")]
        for over in cases:
            code, _, err = self.main("--config", str(self.config(**over)))
            self.assertEqual(code, 2, over)
            self.assertIn("live_heartbeat:", err)
        code, _, err = self.main("--config", str(self.dir / "absent.json"))
        self.assertEqual(code, 2)
        self.assertIn("install_live_heartbeat.sh", err)


class PostTest(Base):
    def test_posts_with_the_token_from_the_file(self):
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
            return FakeResponse(b'{"ok": true}')
        code, out, err = self.main("--config", str(self.config()), opener=opener)
        self.assertEqual((code, out, err), (0, "", ""))
        url, auth, body = seen[0]
        self.assertEqual((url, auth), ("http://provider.test:8432" + live.HEARTBEAT_PATH, "Bearer " + TOKEN))
        self.assertEqual(body["host"], "studio")

    def test_token_file_rules(self):
        os.chmod(self.token, 0o644)
        code, _, err = self.main("--config", str(self.config()))
        self.assertEqual(code, 2)
        self.assertIn("0600", err)
        self.assertNotIn(TOKEN, err)
        os.chmod(self.token, 0o600)
        code, _, err = self.main("--config", str(self.config(token_file=str(self.dir / "none"))))
        self.assertEqual(code, 2)
        self.token.write_text("two tokens\n")
        code, _, err = self.main("--config", str(self.config()))
        self.assertEqual(code, 2)

    def test_provider_url_from_the_hosts_pointer_file(self):
        home = self.dir / "home"
        (home / ".config" / "cortados").mkdir(parents=True)
        (home / ".config" / "cortados" / "provider-url").write_text("http://air.tail:8432/\n")
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            self.assertEqual(hbs.provider_url({}), "http://air.tail:8432")
            (home / ".config" / "cortados" / "provider-url").unlink()
            self.assertEqual(hbs.provider_url({}), hbs.DEFAULT_URL)
            with self.assertRaises(hbs.ConfigError):
                hbs.provider_url({"provider_url": "file:///etc/passwd"})
        finally:
            os.environ["HOME"] = old

    def test_an_older_provider_gets_the_legacy_body(self):
        seen = []

        def opener(req, timeout):
            body = json.loads(req.data)
            seen.append(body)
            if "hardware" in body:
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(
                    b'{"type": "error", "error": {"message": "cortados: heartbeat field (unknown field) is invalid"}}'))
            return FakeResponse(b"{}")
        code, _, err = self.main("--config", str(self.config()), opener=opener)
        self.assertEqual(code, 0, err)
        self.assertIn("legacy body", err)
        self.assertIn("refused the tasks block", err)           # the newest block is dropped first
        self.assertEqual(["tasks" in b for b in seen], [True, False, False])
        legacy = seen[-1]
        self.assertEqual(set(legacy), {"host", "ttl_s", "jobs"})
        jobs = {j["name"]: j for j in legacy["jobs"]}
        self.assertEqual(jobs["hardware"]["summary"], "cpu 30%, mem 62% used, disk 60% free")
        self.assertEqual((jobs["s1"]["state"], jobs["s1"]["summary"]), ("running", "CortadOS Router S1 v12 (run-a)"))
        self.assertIsNone(live.validate_heartbeat(legacy, time.time())[1])

    def test_failures_are_reported(self):
        def refused(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(
                b'{"error": {"message": "cortados: this client posts for host air only"}}'))
        code, _, err = self.main("--config", str(self.config(s1=None, hardware=False, disk=False)), opener=refused)
        self.assertEqual(code, 1)
        self.assertIn("403", err)
        self.assertNotIn(TOKEN, err)

        def down(req, timeout):
            raise urllib.error.URLError("refused")
        code, _, err = self.main("--config", str(self.config()), opener=down)
        self.assertEqual(code, 1)
        self.assertIn("no answer URLError", err)


PRO_TAG = "aaaaaaaaaaaaaaa1"
SECRET_PROMPT = "SECRET-PROMPT-TEXT-0118"


def host_events(now):
    """This host's child-task log as child_tasks.log_event writes it on the Pro (host "here" runs as host "pro"), with
    the cwd and an injected prompt the block must never carry."""
    base = {"cwd": "/Users/someone/secret-project", "orchestrator": "claude-app", "chrome": False,
            "prompt": SECRET_PROMPT, "task_preview": SECRET_PROMPT, "session": "1111111111111111"}
    mine = {"origin_session": PRO_TAG, "origin_project": "CortadOS-Router", "origin_device": "pro"}
    return [
        {"ts": now - 30 * 3600, "event": "started", "task_id": "t20261113100000-00000001", **base, **mine},
        {"ts": now - 600, "event": "started", "task_id": "t20261114160000-00000002", **base, **mine, "host": "pro",
         "title": "Fix the pane"},
        {"ts": now - 900, "event": "started", "task_id": "t20261114155000-00000003", **base, **mine, "host": "pro",
         "title": "Write the docs", "lane": "claude#a", "model": "claude-sonnet-5"},
        {"ts": now - 300, "event": "finished", "task_id": "t20261114155000-00000003", **base, **mine, "host": "pro",
         "status": "succeeded", "duration_s": 600.0, "workspace_state": "archived"},
        {"ts": now - 120, "event": "started", "task_id": "t20261114165800-00000004", **base, "host": "pro",
         "parent_task_id": "t20261114160000-00000002", "origin_via": "parent", **mine, "chrome": True},
    ]


class TasksBlockTest(Base):
    """The heartbeat's `tasks` block (#118): this host's own recent tasks, metadata only, valid by the server's rules."""

    def test_metadata_only_valid_and_running_first(self):
        now = time.time()
        block = hbs.tasks_block("pro", now, events=lambda: host_events(now),
                                peek=lambda tid: {"phase": "queued", "ended": False} if tid.endswith("4") else None)
        ids = [e["task_id"] for e in block["items"]]
        self.assertEqual(ids, ["t20261114165800-00000004", "t20261114160000-00000002", "t20261114155000-00000003"])
        self.assertFalse(block["truncated"])
        queued, running, done = block["items"]
        self.assertEqual((queued["state"], queued["phase"], queued["browser"], queued["parent_task_id"]),
                         ("queued", "waiting for the host's Chrome", True, "t20261114160000-00000002"))
        self.assertEqual((running["state"], running["title"], running["origin_session"], running["host"]),
                         ("running", "Fix the pane", PRO_TAG, "pro"))
        self.assertNotIn("ended_at", running)
        self.assertEqual((done["state"], done["model"], done["lane"], done["workspace"]),
                         ("succeeded", "claude-sonnet-5", "claude#a", "archived"))
        self.assertAlmostEqual(done["ended_at"] - done["started_at"], 600.0, places=2)
        # the child's own Office tag (the routed traffic's x-cortados-session, which the provider already sees) is sent
        # to bind the run's worker to the task (review round 3, P1); the view compares it and never publishes it
        self.assertEqual((running["session"], done["session"]), ("1111111111111111", "1111111111111111"))
        self.assertNotIn("session", queued)                         # a browser task has no Office tags
        raw = json.dumps(block)
        for bad in (SECRET_PROMPT, "/Users", "secret-project", "claude-app", "cwd"):
            self.assertNotIn(bad, raw, bad)
        for e in block["items"]:
            self.assertLessEqual(set(e), live._HB_TASK_KEYS)
        self.assertEqual(live.validate_tasks(block, "pro", now)[1], None)

    def test_the_heartbeat_carries_it_and_the_server_accepts_it(self):
        now = time.time()
        code, out, err = self.main("--config", str(self.config(host="pro", s1=None)), "--dry-run",
                                   task_events=lambda: host_events(now))
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertEqual(len(body["tasks"]["items"]), 3)
        hb, bad = live.validate_heartbeat(body, time.time())
        self.assertIsNone(bad)
        self.assertEqual({e["host"] for e in hb["tasks"]["items"]}, {"pro"})
        self.assertNotIn(SECRET_PROMPT, out)

    def test_capped_newest_first_and_said(self):
        now = time.time()
        evs = []
        for i in range(live.HB_TASKS_MAX + 10):
            tid = f"t20261114100000-{i:08x}"
            evs += [{"ts": now - 5000 + i, "event": "started", "task_id": tid, "host": "pro"},
                    {"ts": now - 4000 + i, "event": "finished", "task_id": tid, "host": "pro", "status": "failed"}]
        block = hbs.tasks_block("pro", now, events=lambda: evs)
        self.assertEqual(len(block["items"]), live.HB_TASKS_MAX)
        self.assertTrue(block["truncated"])
        self.assertEqual(block["items"][0]["task_id"], f"t20261114100000-{live.HB_TASKS_MAX + 9:08x}")

    def test_an_unreadable_log_costs_the_block_only(self):
        def boom():
            raise PermissionError("/Users/someone/.cortados/child-tasks.jsonl")
        code, out, err = self.main("--config", str(self.config(host="pro", s1=None)), "--dry-run", task_events=boom)
        self.assertEqual(code, 0, err)
        self.assertNotIn("tasks", json.loads(out))
        self.assertIn("tasks block skipped: PermissionError", err)
        self.assertNotIn("/Users", err)

    def test_an_oversize_body_sheds_task_entries_before_anything_else(self):
        now = time.time()
        evs = [{"ts": now - 100 - i, "event": "started", "task_id": f"t20261114100000-{i:08x}", "host": "pro",
                "title": "x" * 80, "origin_session": "a" * 64} for i in range(live.HB_TASKS_MAX)]
        body = {"host": "pro", "ttl_s": 180, "jobs": [], "tasks": hbs.tasks_block("pro", now, events=lambda: evs)}
        small = len(json.dumps({**body, "tasks": {"items": body["tasks"]["items"][:5], "truncated": True}},
                               separators=(",", ":")))
        err = io.StringIO()
        with unittest.mock.patch.object(live, "HEARTBEAT_MAX_BYTES", small + 10), redirect_stderr(err):
            sent, raw = hbs.check_fit(body, now)
        self.assertEqual(len(sent["tasks"]["items"]), 5)
        self.assertTrue(sent["tasks"]["truncated"])
        self.assertEqual(sent["jobs"], [])
        self.assertIn("task entries", err.getvalue())

    def old_provider(self, cap=24 * 1024, knows_tasks=False):
        """A fake provider from before #118: its 24 KiB cap is checked first (413, before any field), then an unknown
        block is a 400. Records every body it was sent."""
        seen = []

        def opener(req, timeout):
            seen.append(json.loads(req.data))
            if len(req.data) > cap:
                raise urllib.error.HTTPError(req.full_url, 413, "big", {}, io.BytesIO(
                    b'{"type": "error", "error": {"message": "cortados: the heartbeat is too large"}}'))
            if "tasks" in seen[-1] and not knows_tasks:
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(
                    b'{"type": "error", "error": {"message": "cortados: heartbeat field (unknown field) is invalid"}}'))
            return FakeResponse(b"{}")
        opener.seen = seen
        return opener

    def full_log(self, now):
        """40 tasks with full-length titles and tags: a valid tasks block over the old 24 KiB cap."""
        return [{"ts": now - 50 - i, "event": "finished", "task_id": f"t20261114100000-{i:08x}", "host": "pro",
                 "status": "succeeded", "duration_s": 50.0, "workspace_state": "archived"}
                for i in range(live.HB_TASKS_MAX)] + \
            [{"ts": now - 100 - i, "event": "started", "task_id": f"t20261114100000-{i:08x}", "host": "pro",
                 "title": "Long task title " + "x" * 64, "origin_session": "a" * 64, "origin_project": "p" * 64,
                 "origin_device": "d" * 32, "session": "b" * 64, "lane": "claude#a", "model": "claude-sonnet-5",
                 "parent_task_id": f"t20261114090000-{i:08x}", "origin_via": "parent"}
                for i in range(live.HB_TASKS_MAX)]

    def test_an_old_providers_413_sheds_task_entries_then_the_block_never_the_jobs(self):
        now = time.time()
        for knows_tasks in (False, True):
            opener = self.old_provider(knows_tasks=knows_tasks)
            code, _, err = self.main("--config", str(self.config(host="pro")), opener=opener,
                                     task_events=lambda: self.full_log(now))
            self.assertEqual(code, 0, err)
            seen = opener.seen
            first = json.dumps(seen[0], separators=(",", ":")).encode()
            self.assertEqual(len(seen[0]["tasks"]["items"]), live.HB_TASKS_MAX)
            self.assertGreater(len(first), 24 * 1024)                     # valid for this provider, too big for that
            self.assertLessEqual(len(first), live.HEARTBEAT_MAX_BYTES)
            self.assertIsNone(live.validate_heartbeat(seen[0], time.time())[1])
            self.assertIn("provider refused the size", err)
            self.assertEqual([len(b["tasks"]["items"]) for b in seen if "tasks" in b][:2], [40, 20])
            for b in seen:                                                 # every try kept the jobs, hardware and S1
                self.assertEqual([j["name"] for j in b["jobs"]], ["s1-scorer", "com.cortados.s1-nightly"])
                self.assertIn("hardware", b)
                self.assertIn("s1", b)
            last = seen[-1]
            if knows_tasks:                                                # a smaller block fit: it is said cut
                self.assertEqual((len(last["tasks"]["items"]), last["tasks"]["truncated"]), (20, True))
            else:                                                          # it fit, then the old fields rule said no
                self.assertNotIn("tasks", last)
                self.assertIn("refused the tasks block", err)

    def test_a_413_that_never_fits_sheds_only_the_optional_blocks(self):
        now = time.time()
        opener = self.old_provider(cap=10)                                 # nothing fits: the post fails, said
        code, _, err = self.main("--config", str(self.config(host="pro")), opener=opener,
                                 task_events=lambda: self.full_log(now))
        self.assertEqual(code, 1)
        self.assertIn("413", err)
        self.assertEqual([len(b["tasks"]["items"]) for b in opener.seen if "tasks" in b], [40, 20, 10, 5, 2, 1])
        last = opener.seen[-1]
        self.assertEqual(set(last), {"host", "ttl_s", "jobs", "hardware", "s1"})   # never the jobs or hardware
        self.assertEqual(len(last["jobs"]), 2)

    def test_tasks_config_is_a_switch(self):
        self.assertFalse(hbs.load_config(self.config(tasks=False))["tasks"])
        self.assertTrue(hbs.load_config(self.config())["tasks"])                # absent: on
        with self.assertRaises(hbs.ConfigError):
            hbs.load_config(self.config(tasks="yes"))


REPO = Path(__file__).resolve().parents[1]
INSTALL =REPO / "scripts" / "install_live_heartbeat.sh"
FAKE_LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
if [ "$1" = list ]; then
  printf 'PID\tStatus\tLabel\n'
  printf '123\t0\tcom.cortados.provider\n-\t0\tcom.cortados.archive-cleanup\n'
  printf '77\t0\tcom.cortados.live-heartbeat\n88\t0\tcom.apple.something\n'
  printf '99\t0\tcom.cortados.task.t20261114170942-14922685\n'
fi
exit 0
"""


class InstallTest(unittest.TestCase):
    """scripts/install_live_heartbeat.sh in a throwaway HOME with a fake launchctl (CORTADOS_LAUNCHCTL)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.fake = self.home / "fake-launchctl"
        self.fake.write_text(FAKE_LAUNCHCTL)
        os.chmod(self.fake, 0o755)
        self.log = self.home / "launchctl.log"
        self.token = self.home / ".config" / "cortados" / "live-heartbeat-token"

    def install(self, *args):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CORTADOS_LAUNCHCTL": str(self.fake),
               "FAKE_LOG": str(self.log), "CORTADOS_HOME": str(self.home / ".cortados")}
        return subprocess.run(["/bin/bash", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def mint(self, mode=0o600):
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text(TOKEN + "\n")
        os.chmod(self.token, mode)

    def test_installs_config_and_plist_and_loads_it(self):
        import plistlib
        self.mint()
        r = self.install("studio", "--provider-url", "http://air.tail:8432")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads((self.home / ".config" / "cortados" / "heartbeat.json").read_text())
        self.assertEqual(cfg, {"version": 1, "host": "studio", "token_file": "~/.config/cortados/live-heartbeat-token",
                               "ttl_s": 180, "jobs": ["com.cortados.archive-cleanup", "com.cortados.provider"],
                               "hardware": True, "disk": True, "provider_url": "http://air.tail:8432",
                               "s1": {"data_dir": "~/cortados/train/data"}, "tasks": True})
        plist_path = self.home / "Library" / "LaunchAgents" / "com.cortados.live-heartbeat.plist"
        raw = plist_path.read_text()
        pl = plistlib.loads(raw.encode())
        self.assertEqual(pl["Label"], "com.cortados.live-heartbeat")
        self.assertEqual(pl["ProgramArguments"][1:4], ["-B", "-m", "cortados_router.live_heartbeat"])
        self.assertEqual((pl["WorkingDirectory"], pl["StartInterval"]), (str(REPO), 60))
        self.assertEqual(set(pl["EnvironmentVariables"]), {"PATH"})
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("__", raw)
        uid = os.getuid()
        self.assertEqual(self.calls(), ["list", f"bootout gui/{uid}/com.cortados.live-heartbeat",
                                        f"bootstrap gui/{uid} {plist_path}"])
        self.assertEqual(hbs.load_config(self.home / ".config" / "cortados" / "heartbeat.json")["host"], "studio")
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_a_home_with_xml_and_sed_specials_gives_a_valid_plist(self):
        # Review (2026-11-11): the plist came from sed over a template; a path with & or | corrupted it and XML
        # specials were not escaped. plistlib writes it now.
        odd = Path(tempfile.mkdtemp(prefix="h&o|m<e>'\"-"))
        self.addCleanup(shutil.rmtree, odd, True)
        self.home, self.token = odd, odd / ".config" / "cortados" / "live-heartbeat-token"
        self.mint()
        r = self.install("air")
        self.assertEqual(r.returncode, 0, r.stderr)
        import plistlib
        pl = plistlib.loads((odd / "Library" / "LaunchAgents" / "com.cortados.live-heartbeat.plist").read_bytes())
        self.assertEqual(pl["ProgramArguments"][-2:], ["--config", str(odd / ".config" / "cortados" / "heartbeat.json")])
        self.assertEqual(pl["StandardOutPath"], str(odd / ".cortados" / "live-heartbeat.out"))
        self.assertEqual(pl["StandardErrorPath"], pl["StandardOutPath"])
        self.assertEqual(json.loads((odd / ".config" / "cortados" / "heartbeat.json").read_text())["host"], "air")

    def test_air_has_no_s1_block_and_labels_can_be_given(self):
        self.mint()
        r = self.install("air", "--labels", "com.cortados.provider,com.cortados.bot-gateway")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads((self.home / ".config" / "cortados" / "heartbeat.json").read_text())
        self.assertEqual((cfg["jobs"], "s1" in cfg, "provider_url" in cfg),
                         (["com.cortados.provider", "com.cortados.bot-gateway"], False, False))

    def test_orchestrator_block_is_opt_in(self):
        self.mint()
        r = self.install("air", "--labels", "com.cortados.provider", "--orchestrator")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads((self.home / ".config" / "cortados" / "heartbeat.json").read_text())
        self.assertEqual(cfg["orchestrator"], {"task_roots": ["~/CortadOS"]})
        self.assertEqual(hbs.load_config(self.home / ".config" / "cortados" / "heartbeat.json")["orchestrator"]["every_s"],
                         hbs.ORCH_EVERY_S)

    def test_the_pro_a_gui_exec_host_reports_its_tasks_never_its_task_agents(self):
        # #118: the Pro runs host "here" children, some as per-task LaunchAgents (com.cortados.task.<id>): those come
        # and go, so the default label sweep skips them; its tasks travel in the heartbeat's tasks block instead.
        self.mint()
        r = self.install("pro", "--orchestrator")
        self.assertEqual(r.returncode, 0, r.stderr)
        conf = self.home / ".config" / "cortados" / "heartbeat.json"
        cfg = json.loads(conf.read_text())
        self.assertEqual(cfg["jobs"], ["com.cortados.archive-cleanup", "com.cortados.provider"])
        self.assertEqual((cfg["tasks"], "s1" in cfg, cfg["orchestrator"]), (True, False, {"task_roots": ["~/CortadOS"]}))
        loaded = hbs.load_config(conf)
        self.assertEqual((loaded["host"], loaded["tasks"]), ("pro", True))
        self.assertIn(f"bootstrap gui/{os.getuid()}", " ".join(self.calls()))
        r = self.install("pro", "--no-tasks", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.loads(conf.read_text())["tasks"])

    def test_refusals_load_nothing(self):
        r = self.install("studio")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no token", r.stderr)
        self.mint(0o644)
        r = self.install("studio")
        self.assertEqual(r.returncode, 2)
        self.assertIn("mode 600", r.stderr)
        self.mint()
        for bad in (["Studio"], ["studio", "--provider-url", "ftp://x"], ["studio", "--bogus"]):
            self.assertEqual(self.install(*bad).returncode, 2, bad)
        self.mint()
        r = self.install("studio", "--labels", "bad label")             # the sender's own check refuses the config
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("bootstrap", " ".join(self.calls()))
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.cortados.live-heartbeat.plist").exists())

    def test_dry_run_changes_nothing_and_a_config_is_kept(self):
        r = self.install("pro", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("com.cortados.live-heartbeat", r.stdout)
        self.assertFalse((self.home / ".config").exists())
        self.assertNotIn("bootstrap", " ".join(self.calls()))
        self.mint()
        conf = self.home / ".config" / "cortados" / "heartbeat.json"
        conf.write_text(json.dumps({"host": "pro", "jobs": []}))
        self.assertEqual(self.install("pro").returncode, 0)
        self.assertEqual(json.loads(conf.read_text()), {"host": "pro", "jobs": []})
        self.assertEqual(self.install("pro", "--force").returncode, 0)
        self.assertEqual(json.loads(conf.read_text())["ttl_s"], 180)

if __name__ == "__main__":
    unittest.main()
