"""Codex transcripts (cortados_router/provider/transcripts.py): writes, permissions, redaction, caps, the switch and
replays that must never multiply writes. Fakes only: a throwaway CORTADOS_HOME, the fake codex app-server for the
end-to-end test, never a real vendor CLI and never ~/.cortados."""
import json
import os
import stat
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from cortados_router import router_db
from cortados_router.provider import codex_bridge, transcripts
from cortados_router.provider.server import ProviderServer, Registry
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs)

FAKE_CODEX = Path(__file__).with_name("fixtures") / "fake_codex_app_server.py"
FAKE_CLAUDE = Path(__file__).with_name("fixtures") / "fake_claude_cli.py"
TOKEN = "transcript-test-owner-token-0123456789"
READ_TOOL = {"name": "Read", "description": "Read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
ROUTE = {"provider": "codex-cli", "model": "gpt-test", "band": "R2", "route_id": "r-tx-1",
         "route": {"chosen": {"lane": "codex"}}, "config_dir": "/should/not/be/kept"}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cortados-tx-home-")
        env = {"CORTADOS_HOME": self.home, "CORTADOS_TRANSCRIPTS": "1"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for k in ("CORTADOS_TRANSCRIPT_FILE_MAX_MB", "CORTADOS_TRANSCRIPT_MIN_FREE_GB", "CORTADOS_PROVIDER_TOKEN"):
            os.environ.pop(k, None)
        self.writer = transcripts.Writer()
        self.wp = mock.patch.object(transcripts, "WRITER", self.writer)
        self.wp.start()
        router_db._reset()

    def tearDown(self):
        self.writer.shutdown(2.0)
        self.wp.stop()
        self.env.stop()
        router_db._reset()

    def session(self, sid="0123abcd" * 4, history=()):
        return types.SimpleNamespace(id=sid, history=list(history), route=dict(ROUTE), opened_at=1795000000.0,
                                     system="You are Codex.", tool_names={"Read", "Bash"})

    def turn(self, s, text="hello", **kw):
        args = dict(user_content=[{"type": "text", "text": text}], tool_results={},
                    assistant_blocks=[{"type": "text", "text": "hi back"}], stop="end_turn",
                    usage={"input_tokens": 3, "output_tokens": 2}, started_at=1795000001.0,
                    meta={"run_id": "t20261124114310-e519aba2", "session": "5e55105e55105e55", "client": "codex_exec"},
                    tokens=(TOKEN,))
        args.update(kw)
        ok = transcripts.record_turn(s, **args)
        self.assertTrue(self.writer.flush(5.0))
        return ok

    def path(self, s, day="2026-11-18"):
        return Path(self.home, "transcripts", "codex", day, f"{s.id}.jsonl")

    def lines(self, s):
        p = self.path(s)
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


class WriteTests(Base):
    def test_a_turn_is_appended_after_a_schema_header_with_its_metadata(self):
        s = self.session()
        self.assertTrue(self.turn(s, "first"))
        s.history.append("fp1")
        self.assertTrue(self.turn(s, "second", tool_results={"call_1": {"content": [{"type": "text", "text": "ok"}],
                                                                        "isError": False}}))
        head, t0, t1 = self.lines(s)
        self.assertEqual(head["schema"], transcripts.SCHEMA)
        self.assertEqual(head["kind"], "conversation")
        self.assertEqual(head["conversation_id"], s.id)
        self.assertEqual(head["system"], "You are Codex.")
        self.assertEqual(head["tools"], ["Bash", "Read"])
        self.assertEqual(head["route"], {"route_id": "r-tx-1", "provider": "codex-cli", "model": "gpt-test",
                                         "lane": "codex", "band": "R2", "pinned": False})
        self.assertNotIn("config_dir", json.dumps(head))
        self.assertEqual((t0["kind"], t0["n"], t1["n"]), ("turn", 0, 1))
        self.assertEqual(t0["user"], [{"type": "text", "text": "first"}])
        self.assertEqual(t0["assistant"], [{"type": "text", "text": "hi back"}])
        self.assertEqual((t0["run_id"], t0["session"], t0["client"]), ("t20261124114310-e519aba2", "5e55105e55105e55", "codex_exec"))
        self.assertEqual(t0["route"]["model"], "gpt-test")
        self.assertEqual(t0["started_at"], "2026-11-18T11:06:41.000Z")
        self.assertTrue(t0["ended_at"])
        self.assertEqual(t0["usage"], {"input_tokens": 3, "output_tokens": 2})
        self.assertEqual(t1["tool_results"], [{"tool_use_id": "call_1", "content": [{"type": "text", "text": "ok"}],
                                               "isError": False}])
        self.assertEqual(self.writer.written, 2)
        self.assertEqual(router_db.stats()["count"], 0)

    def test_dirs_are_0700_and_files_0600(self):
        s = self.session()
        self.turn(s)
        p = self.path(s)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        for d in (p.parent, p.parent.parent, p.parent.parent.parent):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)

    def test_inline_media_is_replaced_by_its_size(self):
        s = self.session()
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 5000}}
        self.turn(s, user_content=[img])
        (_, t0) = self.lines(s)
        self.assertEqual(t0["user"][0]["source"]["media_type"], "image/png")
        self.assertIn("5000 characters of inline media", t0["user"][0]["source"]["data"])

    def test_a_symlinked_day_dir_is_refused_and_counted(self):
        s = self.session()
        root = Path(self.home, "transcripts", "codex")
        root.mkdir(parents=True)
        outside = tempfile.mkdtemp()
        os.symlink(outside, root / "2026-11-18")
        self.turn(s)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(self.writer.write_failures, 1)
        self.assertEqual(router_db.stats()["last_op"], "transcript")

    def test_the_writer_never_raises_into_the_caller(self):
        s = self.session()
        with mock.patch.object(transcripts.json, "dumps", side_effect=ValueError("boom")):
            self.assertFalse(transcripts.record_turn(s, user_content=[], tool_results={}, assistant_blocks=[]))
        self.assertEqual(self.writer.schedule_failures, 1)
        self.assertEqual(router_db.stats()["count"], 1)
        self.assertEqual(getattr(s, "transcript_last", -1), -1)   # not queued: the turn may still be written later


class RedactionTests(Base):
    def test_known_credentials_and_secret_shapes_are_redacted_before_writing(self):
        env_token = "env-provider-token-abcdefghijkl"
        os.environ["CORTADOS_PROVIDER_TOKEN"] = env_token
        s = self.session()
        s.system = f"system with {TOKEN}"
        self.turn(s, f"my token is {TOKEN} and {env_token}",
                  tool_results={"c1": {"content": [{"type": "text", "text": "key sk-ant-api03-ABCDEFGHIJKLMNOP"}]}},
                  assistant_blocks=[{"type": "tool_use", "id": "c2", "name": "Bash",
                                     "input": {"command": f"curl -H 'Authorization: Bearer {TOKEN}'"}}])
        raw = self.path(s).read_text()
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn(env_token, raw)
        self.assertNotIn("sk-ant-api03-ABCDEFGHIJKLMNOP", raw)
        head, t0 = self.lines(s)
        self.assertEqual(head["system"], "system with ***")
        self.assertEqual(t0["user"][0]["text"], "my token is *** and ***")
        self.assertIn("[scrubbed:anthropic_key]", t0["tool_results"][0]["content"][0]["text"])
        self.assertIn("anthropic_key", t0["redaction"]["scrubbed"])

    def test_prose_that_looks_like_json_is_kept_and_still_redacted(self):
        s = self.session()
        self.turn(s, f"[1] see {{not json {TOKEN}",
                  assistant_blocks=[{"type": "text", "text": '{"k": "' + TOKEN + '"}'}],
                  tool_results={"c1": {"content": [{"type": "text", "text": "{broken " + TOKEN}]}})
        _, t0 = self.lines(s)
        self.assertEqual(t0["user"][0]["text"], "[1] see {not json ***")
        self.assertEqual(json.loads(t0["assistant"][0]["text"]), {"k": "***"})
        self.assertEqual(t0["tool_results"][0]["content"][0]["text"], "{broken ***")
        self.assertEqual(t0["redaction"]["withheld"], 0)

    def test_the_one_canonical_redactor_is_used(self):
        s = self.session()
        with mock.patch.object(transcripts.child_tasks, "redact_for_export",
                               wraps=transcripts.child_tasks.redact_for_export) as spy:
            self.turn(s)
        self.assertGreaterEqual(spy.call_count, 2)                   # the header and the turn


class GuardTests(Base):
    def test_past_the_file_cap_one_stopped_line_then_nothing_and_every_turn_is_counted(self):
        os.environ["CORTADOS_TRANSCRIPT_FILE_MAX_MB"] = str(3000 / (1024 * 1024))    # ~3000 bytes
        s = self.session()
        for i in range(10):
            self.turn(s, "x" * 400)
            s.history.append(f"fp{i}")
        lines = self.lines(s)
        stopped = [l for l in lines if l.get("kind") == "stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["reason"], "file_cap")
        self.assertEqual(lines[-1]["kind"], "stopped")
        written = sum(1 for l in lines if l.get("kind") == "turn")
        self.assertLess(written, 10)
        self.assertLess(self.path(s).stat().st_size, 3000 + 500)    # the cap plus one short stopped line
        self.assertEqual(router_db.stats()["count"], 10 - written)
        self.assertEqual(router_db.stats()["lost"], 10 - written)
        self.assertEqual(self.writer.write_failures, 10 - written)

    def test_below_the_free_disk_floor_nothing_is_written_and_it_is_counted(self):
        os.environ["CORTADOS_TRANSCRIPT_MIN_FREE_GB"] = str(10 ** 9)          # more than any disk has
        s = self.session()
        self.turn(s)
        self.assertEqual(self.lines(s), [])
        self.assertEqual(router_db.stats()["count"], 1)
        self.assertEqual(self.writer.write_failures, 1)
        self.assertIn("disk floor", self.writer.last_failure)

    def test_disabled_writes_nothing(self):
        for off in ("0", "off", "false", "no"):
            os.environ["CORTADOS_TRANSCRIPTS"] = off
            self.assertFalse(transcripts.enabled())
            s = self.session()
            self.assertFalse(self.turn(s))
        self.assertFalse(Path(self.home, "transcripts").exists())
        os.environ.pop("CORTADOS_TRANSCRIPTS")
        self.assertTrue(transcripts.enabled())                      # default on


class ReplayTests(Base):
    def test_a_repeated_turn_is_written_once_however_often_it_is_offered(self):
        s = self.session()
        for _ in range(50):
            self.turn(s)                                          # same session, same history length
        lines = self.lines(s)
        self.assertEqual([l["kind"] for l in lines], ["conversation", "turn"])
        self.assertEqual(self.writer.duplicates, 49)
        self.assertEqual(router_db.stats()["count"], 0)

    def test_the_writer_refuses_a_turn_index_it_already_wrote_even_from_another_session_object(self):
        a, b = self.session(), self.session()                    # same id, separate counters
        self.turn(a)
        self.turn(b)
        self.assertEqual(sum(1 for l in self.lines(a) if l["kind"] == "turn"), 1)
        self.assertEqual(self.writer.duplicates, 1)

    def test_concurrent_offers_of_one_turn_queue_it_once(self):
        s = self.session()
        go = threading.Event()
        results = []

        def offer():
            go.wait()
            results.append(transcripts.record_turn(s, user_content=[], tool_results={}, assistant_blocks=[]))
        ts = [threading.Thread(target=offer) for _ in range(16)]
        for t in ts:
            t.start()
        go.set()
        for t in ts:
            t.join()
        self.assertTrue(self.writer.flush(5.0))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(sum(1 for l in self.lines(s) if l["kind"] == "turn"), 1)


class EndToEndTests(Base):
    """The provider over HTTP on a codex-cli route with the fake app-server: each turn is kept once, a retried
    (replayed) turn adds nothing, and a Claude-backed conversation writes no provider transcript."""

    def setUp(self):
        super().setUp()
        self.scratch = tempfile.mkdtemp()
        self.codex_home = os.path.join(self.scratch, "codex-home")
        os.mkdir(self.codex_home)
        Path(self.codex_home, "config.toml").write_text('model = "gpt-test"\n')
        for k, name in (("FAKE_CODEX_ARGV", "codex-argv.json"), ("FAKE_CODEX_LOG", "codex-in.jsonl"),
                        ("FAKE_CLAUDE_ARGV", "claude-argv.json")):
            os.environ[k] = os.path.join(self.scratch, name)
        self.settle = codex_bridge.CODEX_SETTLE_S
        codex_bridge.CODEX_SETTLE_S = 0.3
        self.route = dict(ROUTE)
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=[sys.executable, str(FAKE_CLAUDE)],
                            codex_bin=[sys.executable, str(FAKE_CODEX)], codex_home=self.codex_home,
                            router=lambda body, client: dict(self.route), executed=lambda route: None)
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.reg.gateway_url = self.url
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for s in list(self.reg.sessions.values()):
            self.reg.drop(s)
        codex_bridge.CODEX_SETTLE_S = self.settle
        super().tearDown()

    def post(self, body, headers=None):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              "user-agent": "claude-cli/2.1.304 (external, cli)",
                                              "x-claude-code-session-id": "conv-tx",
                                              "x-cortados-session": "e2e0e2e0e2e0e2e0", **(headers or {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def files(self):
        root = Path(self.home, "transcripts", "codex")
        return sorted(root.rglob("*.jsonl")) if root.exists() else []

    def test_each_codex_turn_is_kept_once_and_a_replayed_retry_adds_nothing(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"model": "cortados", "tools": [READ_TOOL], "messages": history})
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        body = {"model": "cortados", "tools": [READ_TOOL], "messages": history}
        r2 = self.post(body)
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        for _ in range(3):
            self.assertEqual(self.post(body), r2)                  # retries: served from the stored reply
        self.assertTrue(transcripts.WRITER.flush(5.0))
        (f,) = self.files()
        lines = [json.loads(l) for l in f.read_text().splitlines()]
        self.assertEqual([l["kind"] for l in lines], ["conversation", "turn", "turn"])
        head, t0, t1 = lines
        (sess,) = self.reg.sessions.values()
        self.assertEqual(f.name, f"{sess.id}.jsonl")
        self.assertEqual(head["route"]["model"], "gpt-test")
        self.assertEqual(head["tools"], ["Read"])
        self.assertEqual(t0["user"], [{"type": "text", "text": "please use tool"}])
        self.assertEqual(t0["assistant"][-1]["type"], "tool_use")
        self.assertEqual(t0["session"], "e2e0e2e0e2e0e2e0")
        self.assertEqual(t1["tool_results"][0]["tool_use_id"], use["id"])
        self.assertEqual(t1["assistant"], [{"type": "text", "text": "tool said: hello"}])
        self.assertNotIn(TOKEN, f.read_text())

    def test_a_claude_backed_conversation_writes_no_provider_transcript(self):
        self.route = {"provider": "claude-cli", "model": "claude-test", "band": "R1", "route_id": "r-cl"}
        self.post({"model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]},
                  headers={"x-claude-code-session-id": "conv-claude"})
        self.assertTrue(transcripts.WRITER.flush(5.0))
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main()
