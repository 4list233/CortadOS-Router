"""compaction/store.py: the shadow content store (off by default) and its provider hook, with fakes only."""
import json
import os
import stat
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

    def test_off_by_default_writes_nothing(self):
        self.assertFalse(self.s.record_turn("conv", **turn()))
        self.assertFalse(os.path.exists(os.path.join(self.home, "compaction")))

    def test_on_writes_records_with_modes(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.assertTrue(self.s.record_turn("conv/../x", **turn()))
            self.assertTrue(self.s.record_turn("conv/../x", **turn(
                user_content=[], tool_results={"t1": {"content": [{"type": "text", "text": "x" * 10}], "isError": True}},
                assistant_blocks=[{"type": "text", "text": "done"}])))
        p = store.session_path("conv/../x")
        self.assertEqual(p.parent, store.sessions_dir())
        self.assertNotIn("/", p.name.replace(".jsonl", "")[0:0] + p.name)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(store.sessions_dir()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(store.root()).st_mode), 0o700)
        recs = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(recs[0]["meta"], {"run_id": "run-1", "session": "s-1"})
        self.assertEqual([r["kind"] for r in recs[:4]], ["turn", "message", "message", "tool_call"])

    def test_result_truncated_with_original_length(self):
        rec = store._result_record("t", {"content": [{"type": "text", "text": "y" * (store.RESULT_CAP_CHARS + 5)}]})
        self.assertEqual(len(rec["text"]), store.RESULT_CAP_CHARS)
        self.assertEqual(rec["chars"], store.RESULT_CAP_CHARS + 5)
        self.assertTrue(rec["truncated"])

    def test_symlink_refused_and_failure_counted_not_raised(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            store.ensure_dir(store.sessions_dir())
            target = os.path.join(self.home, "elsewhere")
            Path(target).write_text("")
            os.symlink(target, store.session_path("conv"))
            self.assertFalse(self.s.record_turn("conv", **turn()))
        self.assertEqual(self.s.write_failures, 1)
        self.assertIn("OSError", self.s.last_failure)
        self.assertEqual(Path(target).read_text(), "")

    def test_symlinked_dir_refused(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            os.makedirs(store.root(), mode=0o700)
            elsewhere = tempfile.mkdtemp()
            os.symlink(elsewhere, store.sessions_dir())
            self.assertFalse(self.s.record_turn("conv", **turn()))
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(self.s.write_failures, 1)

    def test_retention_and_size_cap(self):
        with Env(CORTADOS_COMPACTION_STORE="1", CORTADOS_COMPACTION_RETENTION_DAYS="14",
                 CORTADOS_COMPACTION_MAX_MB=str(1500 / 1024 / 1024)):
            store.ensure_dir(store.root())
            store.ensure_dir(store.sessions_dir())
            now = time.time()
            for name, age_days in (("old", 20), ("mid", 3), ("new", 1)):
                p = store.sessions_dir() / f"{name}.jsonl"
                p.write_text("z" * 1000)
                os.utime(p, (now - age_days * 86400,) * 2)
            removed = store.prune_expired(now)
        self.assertEqual(removed, ["old.jsonl", "mid.jsonl"])     # expired, then oldest until under 1500 bytes
        self.assertEqual(os.listdir(store.sessions_dir()), ["new.jsonl"])

    def test_read_session_round_trip(self):
        with Env(CORTADOS_COMPACTION_STORE="1"):
            self.s.record_turn("c", **turn())
            self.s.record_turn("c", **turn(user_content=[], tool_results={
                "t1": {"content": [{"type": "text", "text": "print(1)"}], "isError": False}},
                assistant_blocks=[{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rm x"}}]))
        sess = store.read_session(store.session_path("c"))
        self.assertIsInstance(sess, Session)
        self.assertEqual([m.role for m in sess.messages], ["user", "assistant", "assistant"])
        self.assertEqual(len(sess.events), 2)
        e0, e1 = sess.events
        self.assertEqual((e0.name, e0.result_text, e0.result_chars, e0.interrupted, e0.message_index),
                         ("Read", "print(1)", 8, False, 1))
        self.assertTrue(e1.interrupted)
        self.assertEqual(sess.messages[1].tool_event_indexes, (0,))


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
        self.patch = mock.patch.object(server, "_claude_session", wrapped)
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


if __name__ == "__main__":
    unittest.main()
