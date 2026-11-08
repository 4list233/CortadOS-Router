"""compaction/store.py: the shadow content store (off by default) and its provider hook, with fakes only."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from cortados_router.compaction import store
from cortados_router.compaction.events import Session

FIXTURES = Path(__file__).with_name("fixtures")


class Env:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.p = mock.patch.dict(os.environ, self.kw)
        self.p.start()

    def __exit__(self, *a):
        self.p.stop()


def turn(**kw):
    base = dict(source="claude-code", user_content=[{"type": "text", "text": "read a.py"}], tool_results={},
                assistant_blocks=[{"type": "text", "text": "ok"},
                                  {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}],
                meta={"run_id": "run-1", "session": "s-1"})
    base.update(kw)
    return base


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home})
        self.env.start()
        os.environ.pop("CORTADOS_COMPACTION_STORE", None)
        self.s = store.Store()

    def tearDown(self):
        self.env.stop()

    def rec(self, cid, **kw):
        ok = self.s.record_turn(cid, **turn(**kw))
        self.assertTrue(self.s.flush())
        return ok

    def test_off_by_default_writes_nothing(self):
        self.assertFalse(self.rec("conv"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "compaction")))

    def test_on_writes_records_with_modes(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.assertTrue(self.rec("conv/../x"))
            self.assertTrue(self.rec("conv/../x", user_content=[], assistant_blocks=[{"type": "text", "text": "d"}],
                                     tool_results={"t1": {"content": [{"type": "text", "text": "x" * 10}], "isError": True}}))
        p = store.session_path("conv/../x")
        self.assertEqual(p.parent, store.sessions_dir())
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(store.sessions_dir()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(store.root()).st_mode), 0o700)
        recs = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(recs[0]["meta"], {"run_id": "run-1", "session": "s-1"})
        self.assertEqual([r["kind"] for r in recs[:4]], ["turn", "message", "message", "tool_call"])
        self.assertEqual(self.s.write_failures, 0)

    def test_cap_comes_from_policy_per_source(self):
        pol = store.load_policy()
        for src in ("claude-code", "codex", "other"):
            cpt = pol["chars_per_token"].get(src, pol["chars_per_token"]["unknown"])
            self.assertEqual(store.cap_chars(src), int(pol["provider_tool_result_cap_tokens"] * cpt))
        self.assertEqual((store.cap_chars("claude-code"), store.cap_chars("codex")), (20000, 32000))

    def test_result_and_input_truncated_with_original_length(self):
        cap = store.cap_chars("claude-code")
        recs = store.turn_records(source="claude-code", user_content=[], tool_results={
            "t0": {"content": [{"type": "text", "text": "y" * (cap + 5)}]}}, assistant_blocks=[
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "a", "content": "z" * (cap * 3)}},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a"}}])
        res = recs[1]
        self.assertEqual((len(res["text"]), res["chars"], res["truncated"]), (cap, cap + 5, True))
        w, r = [x for x in recs if x["kind"] == "tool_call"]
        self.assertTrue(w["input_truncated"])
        self.assertGreater(w["input_chars"], cap * 3)
        self.assertLessEqual(len(json.dumps(w["input"], ensure_ascii=False)), cap)
        self.assertEqual(set(w["input"]), {"file_path", "content"})
        self.assertEqual((r["input"], r["input_truncated"]), ({"file_path": "a"}, False))

    def test_input_cap_holds_on_final_json_for_huge_keys_and_structure(self):
        cap = 20000
        for inp in ({("k" * 30000): 1},                                  # one giant key
                    {f"k{i}": "v" * 50 for i in range(5000)},            # many keys
                    {"a": [{"b": ["\"" * 100] * 50}] * 50},             # deep structure, escaping
                    {"q": "\"" * 50000}):                               # escaping doubles the length
            out, n, cut = store.cap_input(inp, cap)
            self.assertTrue(cut)
            self.assertGreater(n, cap)
            self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), cap, list(out)[:1])

    def test_symlinked_file_refused_failure_counted_not_raised(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            os.makedirs(store.sessions_dir(), mode=0o700)
            target = os.path.join(self.home, "elsewhere")
            Path(target).write_text("")
            os.symlink(target, store.session_path("conv"))
            self.assertTrue(self.rec("conv"))                    # queued; the writer refuses it
        self.assertEqual(self.s.write_failures, 1)
        self.assertIn("OSError", self.s.last_failure)
        self.assertEqual(Path(target).read_text(), "")

    def test_symlinked_sessions_and_compaction_dirs_refused(self):
        for link in ("sessions", "compaction"):
            with self.subTest(link=link), Env(CORTADOS_HOME=tempfile.mkdtemp(), CORTADOS_COMPACTION_STORE="1"):
                s = store.Store()
                elsewhere = tempfile.mkdtemp()
                if link == "sessions":
                    os.makedirs(store.root(), mode=0o700)
                    os.symlink(elsewhere, store.sessions_dir())
                else:
                    os.symlink(elsewhere, store.root())
                s.record_turn("conv", **turn())
                s.flush()
                self.assertEqual(os.listdir(elsewhere), [])
                self.assertEqual(s.write_failures, 2)               # the turn and its retention pass, both refused

    def test_fifo_at_session_path_does_not_block(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            os.makedirs(store.sessions_dir(), mode=0o700)
            os.mkfifo(store.session_path("conv"))
            t0 = time.time()
            self.assertTrue(self.s.record_turn("conv", **turn()))
            self.assertLess(time.time() - t0, 0.5)               # the request path only enqueued
            self.assertTrue(self.s.flush(3))                     # and the writer did not hang on the FIFO
        self.assertEqual(self.s.write_failures, 1)

    def test_queue_overflow_drops_and_counts(self):
        s = store.Store(queue_max=1)
        gate = threading.Event()
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.assertTrue(s.submit("block", gate.wait))       # the writer holds this job
            time.sleep(0.05)
            self.assertTrue(s.submit("one", lambda: None))       # fills the queue
            t0 = time.time()
            self.assertFalse(s.submit("two", lambda: None))      # full: dropped at once, never blocks
            self.assertLess(time.time() - t0, 0.5)
            gate.set()
            s.flush()
        self.assertEqual(s.dropped, 1)

    def test_queue_budget_bounds_retained_bytes_not_just_jobs(self):
        """A stalled writer used to hold every queued turn's UNCAPPED content (the closure kept the request's kw
        until it ran): one 10 MB result retained 10 MB despite its 20,000-char cap. Now the turn is serialized and
        capped before admission, the closure holds only the capped text, and admission is bounded in bytes."""
        gate = threading.Event()
        big = "x" * (10 * 1024 * 1024)
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.assertTrue(self.s.submit("block", gate.wait))           # the writer is stuck on this job
            time.sleep(0.05)
            rc0 = sys.getrefcount(big)                                     # CPython: a retained reference shows here
            self.assertTrue(self.s.record_turn("c", **turn(tool_results={
                "t": {"content": [{"type": "text", "text": big}]}})))
            self.assertEqual(sys.getrefcount(big), rc0)                    # the queue does not reference the payload
            st = self.s.stats()
            self.assertEqual(st["queued"], 2)                              # the turn and its retention pass (0 bytes)
            self.assertLessEqual(st["queued_bytes"], store.cap_chars("claude-code") + 2000)   # capped text + headers
            self.assertGreater(st["queued_bytes"], store.cap_chars("claude-code"))
            # an unbuildable turn is counted, never raised into the request
            self.assertFalse(self.s.record_turn("c", **turn(assistant_blocks=5)))
            self.assertEqual(self.s.schedule_failures, 1)
            self.assertIn("not built", self.s.last_failure)
            gate.set()
            self.assertTrue(self.s.flush())
        self.assertEqual(self.s.stats()["queued_bytes"], 0)
        self.assertEqual(self.s.write_failures, 0)

    def test_queue_byte_budget_drops_and_counts(self):
        s = store.Store(queue_max_bytes=100)
        gate = threading.Event()
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.assertTrue(s.submit("block", gate.wait))
            time.sleep(0.05)
            self.assertTrue(s.submit("a", lambda: None, nbytes=60))
            self.assertFalse(s.submit("b", lambda: None, nbytes=60))      # 120 > 100: dropped at once
            self.assertTrue(s.submit("c", lambda: None, nbytes=40))       # exactly the budget: admitted
            self.assertFalse(s.submit("huge", lambda: None, nbytes=101))  # over the whole budget even when empty
            self.assertEqual(s.stats()["queued_bytes"], 100)
            gate.set()
            self.assertTrue(s.flush())
        self.assertEqual(s.dropped, 2)
        self.assertIn("byte budget", s.last_failure)
        self.assertEqual(s.stats()["queued_bytes"], 0)

    def test_shutdown_counts_unfinished_and_refuses_new_jobs(self):
        s = store.Store()
        gate = threading.Event()
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.assertTrue(s.submit("stuck", gate.wait))                 # the writer never finishes this one
            time.sleep(0.05)
            self.assertTrue(s.submit("one", lambda: None, nbytes=10))
            self.assertTrue(s.submit("two", lambda: None, nbytes=10))
            t0 = time.monotonic()
            self.assertEqual(s.shutdown(timeout=0.2), 3)                   # two queued + the one the writer is in
            self.assertLess(time.monotonic() - t0, 2.0)                    # bounded: the stuck disk does not hold exit
            self.assertEqual(s.lost, 3)
            self.assertIn("lost at shutdown", s.last_failure)
            self.assertFalse(s.submit("late", lambda: None))               # refused, counted, never queued
            self.assertEqual(s.dropped, 1)
            self.assertIn("shut down", s.last_failure)
            self.assertEqual(s.shutdown(timeout=0.05), 0)                  # idempotent: nothing counted twice
            st = s.stats()
            self.assertEqual((st["closed"], st["queued"], st["queued_bytes"], st["lost"]), (True, 0, 0, 3))
            gate.set()
            time.sleep(0.1)
            self.assertFalse(s._thread.is_alive())                         # closed and empty: the writer exits

    def test_shutdown_drains_accepted_writes_at_process_exit(self):
        """The writer is a daemon thread, which the interpreter freezes at exit: a turn accepted right before the
        process ends (Registry.drop enqueues decisions on the way out) used to vanish. A real process, not flush()."""
        code = (
            "import time\n"
            "from cortados_router.compaction import store\n"
            "def late():\n"
            "    time.sleep(0.3)\n"
            "    store.append_in(('compaction',), 'late.jsonl', 'late\\n')\n"
            "assert store.STORE.submit('late', late)\n"
            "assert store.STORE.record_turn('conv', source='claude-code', user_content=[{'type': 'text', 'text': 'hi'}],"
            " tool_results={}, assistant_blocks=[{'type': 'text', 'text': 'ok'}])\n")
        env = dict(os.environ, CORTADOS_HOME=self.home, CORTADOS_COMPACTION_STORE="1",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(Path(self.home, "compaction", "late.jsonl").read_text(), "late\n")
        self.assertTrue(store.session_path("conv").exists(), os.listdir(os.path.join(self.home, "compaction")))
        self.assertNotIn("lost at shutdown", r.stderr)

    def test_writer_start_failure_contained(self):
        with Env(CORTADOS_COMPACTION_STORE="1"), \
             mock.patch.object(self.s, "_ensure_writer", side_effect=RuntimeError("can't start new thread")):
            self.assertFalse(self.s.record_turn("conv", **turn()))
            self.assertFalse(self.s.maybe_prune_async(10_000.0))
            self.assertEqual(self.s._last_prune, 0.0)            # throttle reset: the next call retries
        self.assertEqual(self.s.schedule_failures, 3)            # the turn, its prune attempt, the direct prune

    def test_retention_and_size_cap(self):
        with Env(CORTADOS_COMPACTION_STORE="1", CORTADOS_COMPACTION_RETENTION_DAYS="14",
                 CORTADOS_COMPACTION_MAX_MB=str(1500 / 1024 / 1024)):
            os.makedirs(store.sessions_dir(), mode=0o700)
            now = time.time()
            for name, age_days in (("old", 20), ("mid", 3), ("new", 1)):
                p = store.sessions_dir() / f"{name}.jsonl"
                p.write_text("z" * 1000)
                os.utime(p, (now - age_days * 86400,) * 2)
            removed = store.prune_expired(now)
        self.assertEqual(removed, ["sessions/old.jsonl", "sessions/mid.jsonl"])   # expired, then oldest until under 1500 B
        self.assertEqual(os.listdir(store.sessions_dir()), ["new.jsonl"])

    def test_retention_covers_decisions_shards(self):
        """Decisions used to live outside retention (one unbounded decisions.jsonl). Now the shards age out and
        share the size budget with the sessions, oldest first across both directories."""
        with Env(CORTADOS_COMPACTION_STORE="1", CORTADOS_COMPACTION_RETENTION_DAYS="14",
                 CORTADOS_COMPACTION_MAX_MB=str(1 / 1024 / 1024)):                 # the review's one-byte budget
            ddir = store.root() / "decisions"
            os.makedirs(store.sessions_dir(), mode=0o700)
            os.makedirs(ddir, mode=0o700)
            now = time.time()
            for p, age_days in ((ddir / "decisions-2026-10-01.0.jsonl", 20),
                                (ddir / "decisions-2026-11-07.0.jsonl", 1),
                                (store.sessions_dir() / "s.jsonl", 2)):
                p.write_text("z" * 10_000)
                os.utime(p, (now - age_days * 86400,) * 2)
            removed = store.prune_expired(now)
        self.assertEqual(removed, ["decisions/decisions-2026-10-01.0.jsonl", "sessions/s.jsonl",
                                   "decisions/decisions-2026-11-07.0.jsonl"])
        self.assertEqual(os.listdir(ddir), [])
        with Env(CORTADOS_COMPACTION_STORE="1", CORTADOS_COMPACTION_MAX_MB=str(15_000 / 1024 / 1024)):
            (ddir / "decisions-2026-11-08.0.jsonl").write_text("z" * 10_000)
            (store.sessions_dir() / "s2.jsonl").write_text("z" * 10_000)
            os.utime(store.sessions_dir() / "s2.jsonl", (now - 3600,) * 2)        # the older of the two goes
            self.assertEqual(store.prune_expired(now), ["sessions/s2.jsonl"])

    def test_decisions_shards_roll_by_day_and_size(self):
        with Env(CORTADOS_COMPACTION_STORE="1"), mock.patch.object(store, "DECISIONS_SHARD_MAX_BYTES", 100):
            day1, day2 = 1_762_560_000.0, 1_762_560_000.0 + 86400                  # 2025-11-08 and the next UTC day
            names = [store.append_decisions("r" * 60 + "\n", day1) for _ in range(4)]
            names.append(store.append_decisions("x\n", day2))
        self.assertEqual(names, ["decisions-2025-11-08.0.jsonl", "decisions-2025-11-08.0.jsonl",   # 61 B, then 122 B
                                 "decisions-2025-11-08.1.jsonl", "decisions-2025-11-08.1.jsonl",   # rolled at >= 100
                                 "decisions-2025-11-09.0.jsonl"])                                  # a new day: shard 0
        shards = sorted(os.listdir(store.decisions_dir()))
        self.assertEqual(shards, ["decisions-2025-11-08.0.jsonl", "decisions-2025-11-08.1.jsonl",
                                  "decisions-2025-11-09.0.jsonl"])
        sizes = [os.path.getsize(store.decisions_dir() / s) for s in shards]
        self.assertEqual(sizes, [122, 122, 2])                      # each shard stops once it reaches the cap
        for s in shards:
            self.assertEqual(stat.S_IMODE(os.stat(store.decisions_dir() / s).st_mode), 0o600)

    def test_retention_never_leaves_the_store_through_a_symlinked_parent(self):
        outside = tempfile.mkdtemp()
        os.makedirs(os.path.join(outside, "sessions"))
        victim = os.path.join(outside, "sessions", "victim.jsonl")
        Path(victim).write_text("keep me")
        os.utime(victim, (1, 1))
        os.symlink(outside, store.root())                         # compaction/ -> outside
        with Env(CORTADOS_COMPACTION_STORE="1"):
            with self.assertRaises(OSError):
                store.prune_expired(time.time())
        self.assertTrue(os.path.exists(victim))

    def test_retention_rechecks_before_unlink(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            os.makedirs(store.sessions_dir(), mode=0o700)
            p = store.sessions_dir() / "old.jsonl"
            p.write_text("z")
            os.utime(p, (1, 1))
            real_stat, calls = os.stat, []

            def stat_then_touch(name, *a, **k):                   # a write lands between scan and unlink
                st = real_stat(name, *a, **k)
                calls.append(name)
                if len(calls) == 1:
                    os.utime(p, None)
                return st
            with mock.patch.object(store.os, "stat", stat_then_touch):
                self.assertEqual(store.prune_expired(time.time()), [])
        self.assertTrue(p.exists())

    def test_read_session_round_trip_and_refuses_symlink(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.rec("c")
            self.rec("c", user_content=[], tool_results={
                "t1": {"content": [{"type": "text", "text": "print(1)"}], "isError": False}},
                assistant_blocks=[{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rm x"}}])
            sess = store.read_session(store.session_path("c"))
            self.assertIsInstance(sess, Session)
            self.assertEqual(sess.source, "claude-code")
            self.assertEqual([m.role for m in sess.messages], ["user", "assistant", "assistant"])
            e0, e1 = sess.events
            self.assertEqual((e0.name, e0.result_text, e0.result_chars, e0.interrupted, e0.message_index),
                             ("Read", "print(1)", 8, False, 1))
            self.assertTrue(e1.interrupted)
            self.assertEqual(sess.messages[1].tool_event_indexes, (0,))
            outside = os.path.join(self.home, "outside.jsonl")
            Path(outside).write_text("")
            os.symlink(outside, store.session_path("link"))
            with self.assertRaises(OSError):
                store.read_session(store.session_path("link"))
            # an alias of a legitimate file names the same file and is read through the anchored fds
            alias = store.sessions_dir() / ".." / "sessions" / store.session_file_name("c")
            self.assertEqual(store.read_session(alias).events, sess.events)

    def test_read_session_refuses_aliased_paths_through_symlinked_parents_and_outside_paths(self):
        """Only an exact parent match used to take the anchored read; sessions/../sessions/x.jsonl fell to a direct
        os.open that followed a symlinked compaction/ and returned content from outside the store."""
        outside = tempfile.mkdtemp()
        os.makedirs(os.path.join(outside, "sessions"))
        Path(outside, "sessions", "f.jsonl").write_text(json.dumps({"kind": "turn", "source": "codex"}) + "\n")
        os.symlink(outside, store.root())                                  # compaction/ -> outside
        with Env(CORTADOS_COMPACTION_STORE="1"):
            for p in (store.sessions_dir() / "f.jsonl",
                      store.sessions_dir() / ".." / "sessions" / "f.jsonl",
                      Path(self.home) / "compaction" / "." / "sessions" / "f.jsonl"):
                with self.subTest(path=str(p)), self.assertRaises(OSError):
                    store.read_session(p)
            for p in (Path(outside) / "sessions" / "f.jsonl", Path(self.home) / "f.jsonl",
                      store.sessions_dir() / "sub" / "f.jsonl", store.sessions_dir() / "f.txt", store.sessions_dir()):
                with self.subTest(path=str(p)), self.assertRaises(ValueError):
                    store.read_session(p)


class FakeSession:
    def __init__(self, sid):
        self.id, self.lock, self.history, self.closed = sid, threading.Lock(), ["fp"], False
        self.last_used, self.awaiting_tool_result = time.time(), False

    def close(self):
        self.closed = True


class TriggerTests(unittest.TestCase):
    """Registry close / reap start the shadow pass (direct calls, fake sessions); the store's prune is hourly."""

    def setUp(self):
        from cortados_router.provider import server
        self.server = server
        self.reg = server.Registry(gateway_url="", token="t", claude_bin="false")
        self.calls = []
        self.p = mock.patch.object(server.compaction_prune, "shadow_decide_async", self.calls.append)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_drop_and_fail_trigger(self):
        a, b = FakeSession("conv-a"), FakeSession("conv-b")
        self.reg.sessions.update({a.id: a, b.id: b})
        self.reg.drop(a)
        self.reg.fail(b, RuntimeError("x"), None)
        self.assertEqual(self.calls, ["conv-a", "conv-b"])
        self.assertTrue(a.closed and b.closed)

    def test_reap_triggers_only_expired(self):
        old, fresh = FakeSession("conv-old"), FakeSession("conv-fresh")
        old.last_used = time.time() - 10_000
        self.reg.sessions.update({old.id: old, fresh.id: fresh})
        self.reg.reap(60)
        self.assertEqual(self.calls, ["conv-old"])
        self.assertIn("conv-fresh", self.reg.sessions)


class PruneThrottleTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": tempfile.mkdtemp(), "CORTADOS_COMPACTION_STORE": "1"})
        self.env.start()
        self.ran = []
        self.p = mock.patch.object(store, "prune_expired", self.ran.append)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.env.stop()

    def test_hourly(self):
        s = store.Store()
        t0 = 1_000_000.0
        self.assertTrue(s.maybe_prune_async(t0))
        self.assertFalse(s.maybe_prune_async(t0 + 60))
        self.assertFalse(s.maybe_prune_async(t0 + store.PRUNE_INTERVAL_S - 1))
        self.assertTrue(s.maybe_prune_async(t0 + store.PRUNE_INTERVAL_S))
        self.assertTrue(s.flush())
        self.assertEqual(self.ran, [t0, t0 + store.PRUNE_INTERVAL_S])

    def test_off_never_prunes(self):
        with mock.patch.dict(os.environ, {"CORTADOS_COMPACTION_STORE": "0"}):
            self.assertFalse(store.Store().maybe_prune_async(1.0))
        self.assertEqual(self.ran, [])


class ProviderHookTests(unittest.TestCase):
    """Both wires through the real server with the fake claude CLI: records on, identical client bytes on/off."""

    def setUp(self):
        from cortados_router.provider import server
        self.server = server
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home})
        self.env.start()
        os.environ.pop("CORTADOS_COMPACTION_STORE", None)
        self.sent = []
        orig = server._claude_session
        sent = self.sent

        def wrapped(*a, **kw):
            s = orig(*a, **kw)
            su, dl = s.send_user, s.deliver
            s.send_user = lambda c, **k: (sent.append(json.dumps(c, sort_keys=True)), su(c, **k))[1]
            s.deliver = lambda r, **k: (sent.append(json.dumps(r, sort_keys=True)), dl(r, **k))[1]
            return s
        # Registry builds sessions through SESSION_FACTORIES, so the wrapper goes there (patching the module
        # function would leave the dict's reference untouched and record nothing).
        self.patch = mock.patch.dict(server.SESSION_FACTORIES, {"claude-cli": wrapped})
        self.patch.start()
        route = {"provider": "claude-cli", "model": "claude-test-model", "band": "R1", "route_id": "r1"}
        self.reg = server.Registry(gateway_url="", token="t", claude_bin=[sys.executable, str(FIXTURES / "fake_claude_cli.py")],
                                   router=lambda body, client, **k: dict(route), executed=lambda r: None)
        self.httpd = server.ProviderServer(("127.0.0.1", 0), self.reg)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.reg.gateway_url = self.url
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for s in list(self.reg.sessions.values()):
            self.reg.drop(s)
        self.patch.stop()
        self.env.stop()

    def post(self, path, body, ua):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST", headers={
            "content-type": "application/json", "authorization": "Bearer t", "user-agent": ua,
            "x-cortados-run-id": "run-abc12345", "x-cortados-session": "sess-1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def both_wires(self):
        a = self.post("/v1/messages", {"model": "cortados", "max_tokens": 50,
                                       "messages": [{"role": "user", "content": "hello store"}]}, "claude-cli/2.0")
        b = self.post("/v1/responses", {"model": "cortados", "stream": False, "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello codex"}]}]},
            "codex_exec/0.163.0")
        return a, b

    @staticmethod
    def strip_ids(o):
        if isinstance(o, dict):
            return {k: ProviderHookTests.strip_ids(v) for k, v in o.items() if k not in ("id", "created_at")}
        if isinstance(o, (list, tuple)):
            return [ProviderHookTests.strip_ids(v) for v in o]
        return o

    def test_off_then_on_same_backend_bytes_and_records(self):
        off = self.both_wires()
        sent_off = list(self.sent)
        self.assertFalse(os.path.exists(os.path.join(self.home, "compaction")))
        self.sent.clear()
        with Env(CORTADOS_COMPACTION_STORE="1"):
            on = self.both_wires()
            store.STORE.flush()
        self.assertEqual(len(sent_off), 2)                      # one send_user per wire: the wrapper saw them
        self.assertEqual(self.sent, sent_off)
        self.assertEqual(self.strip_ids(on), self.strip_ids(off))
        files = sorted(store.sessions_dir().glob("*.jsonl"))
        self.assertEqual(len(files), 2)
        recs = [json.loads(l) for f in files for l in f.read_text().splitlines()]
        heads = [r for r in recs if r["kind"] == "turn"]
        self.assertEqual(sorted(h["source"] for h in heads), ["claude-code", "codex"])
        for h in heads:
            self.assertEqual((h["meta"]["run_id"], h["meta"]["session"]), ("run-abc12345", "sess-1"))
        texts = [r["text"] for r in recs if r["kind"] == "message" and r["role"] == "user"]
        self.assertTrue(any("hello store" in t for t in texts) and any("hello codex" in t for t in texts))
        self.assertEqual(store.STORE.write_failures, 0)

    def tool_round_trip(self):
        """use tool -> tool_use -> the client's tool_result, which the provider hands to deliver()."""
        read = {"name": "Read", "description": "Read a file", "input_schema": {
            "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
        history = [{"role": "user", "content": "use tool"}]
        first = self.post("/v1/messages", {"model": "cortados", "max_tokens": 50, "tools": [read],
                                           "messages": history}, "claude-cli/2.0")
        use = first["content"][-1]
        self.assertEqual(use["type"], "tool_use")
        history += [{"role": "assistant", "content": first["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                                  "content": [{"type": "text", "text": "file body " * 50}]}]}]
        second = self.post("/v1/messages", {"model": "cortados", "max_tokens": 50, "tools": [read],
                                            "messages": history}, "claude-cli/2.0")
        return use["id"], self.strip_ids([first, second])

    def test_deliver_bytes_identical_on_and_off(self):
        id_off, out_off = self.tool_round_trip()
        sent_off = [x.replace(id_off, "ID") for x in self.sent]
        self.sent.clear()
        with Env(CORTADOS_COMPACTION_STORE="1"):
            id_on, out_on = self.tool_round_trip()
            store.STORE.flush()
        sent_on = [x.replace(id_on, "ID") for x in self.sent]
        self.assertTrue(any("file body" in x for x in sent_on))   # deliver() carried the tool result
        self.assertEqual(sent_on, sent_off)
        self.assertEqual(json.dumps(out_on).replace(id_on, "ID"), json.dumps(out_off).replace(id_off, "ID"))
        recs = [json.loads(l) for f in store.sessions_dir().glob("*.jsonl") for l in f.read_text().splitlines()]
        res = [r for r in recs if r["kind"] == "tool_result"]
        self.assertEqual(len(res), 1)
        self.assertIn("file body", res[0]["text"])

    def test_store_failures_never_break_the_turn_or_its_continuation(self):
        for how in ("writer", "record"):
            with self.subTest(how=how), Env(CORTADOS_COMPACTION_STORE="1"):
                if how == "writer":
                    p = mock.patch.object(store.STORE, "_ensure_writer", side_effect=RuntimeError("can't start new thread"))
                else:
                    p = mock.patch.object(store.STORE, "record_turn", side_effect=RuntimeError("boom"))
                with p:
                    before = store.STORE.schedule_failures
                    _, (first, second) = self.tool_round_trip()     # the continuation must not 409
                    self.assertEqual(second["stop_reason"], "end_turn")
                    self.assertGreater(store.STORE.schedule_failures, before)


if __name__ == "__main__":
    unittest.main()
