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
WITHHELD = "[withheld: content could not be safely redacted]"
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
        # A tool's output is not prose: JSON-looking text that does not decode is withheld (fail closed), as in the
        # run export, never redacted as text (transcript review round 1).
        self.assertEqual(t0["tool_results"][0]["content"][0]["text"], WITHHELD)
        self.assertEqual(t0["redaction"]["withheld"], 1)

    def test_the_one_canonical_redactor_is_used(self):
        s = self.session()
        ct = transcripts.child_tasks
        with mock.patch.object(ct, "redact_known", wraps=ct.redact_known) as known, \
                mock.patch.object(ct, "cap_for_export", wraps=ct.cap_for_export) as cap:
            self.turn(s)
        self.assertGreaterEqual(known.call_count, 2)
        self.assertEqual(cap.call_count, 2)                          # once over the header, once over the whole turn

    # -- transcript review round 1 (gpt-6.1-sol): both scenarios failed on a5a2a3c.
    def raw_has_no(self, s, *secrets):
        raw = self.path(s).read_text()
        for x in secrets:
            self.assertNotIn(x, raw)
            self.assertNotIn(json.dumps(x)[1:-1], raw)

    def test_malformed_json_tool_output_with_a_unicode_escaped_token_is_withheld(self):
        escaped = "".join("\\u%04x" % ord(c) for c in TOKEN)
        malformed = '{"auth": "' + escaped + '", "more": '              # does not decode
        self.assertEqual(json.loads('"' + escaped + '"'), TOKEN)    # the token is recoverable from it
        s = self.session()
        self.turn(s, tool_results={"c1": {"content": [{"type": "text", "text": malformed}]},
                                   "c2": {"content": malformed},
                                   "c3": {"content": [{"type": "input_text", "text": malformed}]}})
        raw = self.path(s).read_text()
        self.assertNotIn(escaped, raw)
        self.assertNotIn(escaped.replace("\\", "\\\\"), raw)
        _, t0 = self.lines(s)
        by_id = {r["tool_use_id"]: r for r in t0["tool_results"]}
        self.assertEqual(by_id["c1"]["content"][0]["text"], WITHHELD)
        self.assertEqual(by_id["c2"]["content"], WITHHELD)
        self.assertEqual(by_id["c3"]["content"][0]["text"], WITHHELD)
        self.assertEqual(t0["redaction"]["withheld"], 3)
        # The run export's strict path withholds the same input: one rule for both records.
        red = transcripts.child_tasks.provider_redactor((TOKEN,))
        self.assertEqual(transcripts.child_tasks.redact_for_export(red, malformed)[:2], (WITHHELD, 1))

    def test_tool_output_blocks_inside_message_content_are_never_prose(self):
        s = self.session()
        self.turn(s, user_content=[{"type": "tool_result", "tool_use_id": "x",
                                    "content": [{"type": "text", "text": "{oops " + TOKEN}]},
                                   {"type": "function_call_output", "call_id": "y", "output": "[1, " + TOKEN}])
        _, t0 = self.lines(s)
        self.assertEqual(t0["user"][0]["content"][0]["text"], WITHHELD)
        self.assertEqual(t0["user"][1]["output"], WITHHELD)
        self.raw_has_no(s, TOKEN)

    def test_a_known_credential_in_an_encoded_spelling_is_withheld_even_in_prose(self):
        import base64
        import urllib.parse
        uni = "".join("\\u%04x" % ord(c) for c in TOKEN)
        pct = "".join("%%%02X" % ord(c) for c in TOKEN)
        encoded = {"unicode": f"[note] token {uni} here", "unicode_malformed": "{oops " + uni,
                   "percent": f"https://x/?t={pct}", "percent_twice": "https://x/?t=" + urllib.parse.quote(pct),
                   "base64": "creds " + base64.b64encode(("u:" + TOKEN).encode()).decode(),
                   "base64_url": "c=" + base64.urlsafe_b64encode(("ab" + TOKEN + "z").encode()).decode()}
        for name, text in encoded.items():
            with self.subTest(name):
                s = self.session(sid=f"{name:x<32}"[:32].replace("_", "-"))
                self.turn(s, text, tool_results={"c1": {"content": [{"type": "text", "text": text}]}},
                          assistant_blocks=[{"type": "text", "text": text}])
                _, t0 = self.lines(s)
                self.assertEqual(t0["user"][0]["text"], WITHHELD)
                self.assertEqual(t0["assistant"][0]["text"], WITHHELD)
                self.assertEqual(t0["tool_results"][0]["content"][0]["text"], WITHHELD)
                raw = self.path(s).read_text()
                self.assertNotIn(text, raw)
                for word in text.replace("=", " ").split():
                    if len(word) >= 20:                              # the encoded payload, in any part
                        self.assertNotIn(word, raw)
        s = self.session(sid="ordinary-" + "0" * 23)
        plain = "100% done; path C:\\temp\\x and dGhpcyBpcyBub3QgYSB0b2tlbg=="
        self.turn(s, plain)
        self.assertEqual(self.lines(s)[1]["user"][0]["text"], plain)  # no known credential: untouched

    def test_credential_named_keys_are_scrubbed_at_every_depth_and_in_lists(self):
        secret = "opaque-service-secret-0123456789"
        keys = ["x-api-key", "X-Api-Key", "api_key", "apiKey", "api-key", "Authorization", "proxy-authorization",
                "token", "access_token", "refreshToken", "id-token", "password", "Passwd", "client_secret",
                "Cookie", "set-cookie", "private_key", "credentials", "auth"]
        s = self.session()
        results, inputs = {}, []
        for i, k in enumerate(keys):
            # depth 1, 2 and 4, a list of values, and JSON text holding the key
            results[f"a{i}"] = {"content": [], "structuredContent": {k: secret}}
            results[f"b{i}"] = {"content": [{"type": "text", "text": "ok"}],
                                "structuredContent": {"outer": {"mid": [{"inner": {k: secret}}]}}}
            results[f"c{i}"] = {"content": [{"type": "text", "text": json.dumps({"headers": {k: secret}})}]}
            results[f"d{i}"] = {"content": [], "structuredContent": {k: [secret, {"v": secret}]}}
            inputs.append({"type": "tool_use", "id": f"u{i}", "name": "Fetch",
                           "input": {"url": "https://x", "headers": [{k: secret}]}})
        self.turn(s, tool_results=results, assistant_blocks=inputs,
                  user_content=[{"type": "text", "text": "hi"}, {"type": "x", "meta": {"deep": {keys[0]: secret}}}])
        self.raw_has_no(s, secret)
        _, t0 = self.lines(s)
        by_id = {r["tool_use_id"]: r for r in t0["tool_results"]}
        self.assertEqual(by_id["a0"]["structuredContent"], {"x-api-key": "[scrubbed:authorization]"})
        self.assertEqual(by_id["a7"]["structuredContent"], {"token": "[scrubbed:credential]"})
        self.assertEqual(json.loads(by_id["c11"]["content"][0]["text"]), {"headers": {"password": "[scrubbed:credential]"}})
        self.assertGreaterEqual(sum(t0["redaction"]["scrubbed"].values()), 6 * len(keys))
        # Ordinary keys ending alike, and numbers under credential-like keys, are left alone.
        s2 = self.session(sid="keys-ok-" + "0" * 24)
        self.turn(s2, tool_results={"c": {"content": [], "structuredContent": {
            "token_type": "Bearer", "tokenizer": "bpe", "author": "me", "max_tokens": 5, "token_count": 3}}})
        self.assertEqual(self.lines(s2)[1]["tool_results"][0]["structuredContent"],
                         {"token_type": "Bearer", "tokenizer": "bpe", "author": "me", "max_tokens": 5,
                          "token_count": 3})


    # -- transcript review round 2 (gpt-6.1-sol): each scenario failed on c88055b.
    def test_credential_keys_inside_double_and_triple_encoded_json_are_scrubbed(self):
        secret = "opaque_value_not_known_to_host"
        obj = {"x-api-key": secret, "max_tokens": 5, "input_tokens": 7, "cache_creation_input_tokens": 9,
               "token_count": 3, "password_policy": "strict"}
        double, triple = json.dumps(json.dumps(obj)), json.dumps(json.dumps(json.dumps(obj)))
        s = self.session()
        self.turn(s, tool_results={"d": {"content": [{"type": "text", "text": double}]},
                                   "t": {"content": triple},
                                   "n": {"content": [], "structuredContent": {"body": double}}})
        self.raw_has_no(s, secret)
        _, t0 = self.lines(s)
        by_id = {r["tool_use_id"]: r for r in t0["tool_results"]}
        kept = {k: v for k, v in obj.items() if k != "x-api-key"}
        want = dict(kept, **{"x-api-key": "[scrubbed:authorization]"})
        self.assertEqual(json.loads(json.loads(by_id["d"]["content"][0]["text"])), want)
        self.assertEqual(json.loads(json.loads(json.loads(by_id["t"]["content"]))), want)
        self.assertEqual(json.loads(json.loads(by_id["n"]["structuredContent"]["body"])), want)
        self.assertEqual(t0["redaction"]["withheld"], 0)
        # The run export's path scrubs the same input.
        red = transcripts.child_tasks.provider_redactor(())
        out, withheld, _, _ = transcripts.child_tasks.redact_for_export(red, double)
        self.assertNotIn(secret, out)
        self.assertEqual((json.loads(json.loads(out)), withheld), (want, 0))

    def test_json_encoded_past_the_layer_limit_is_withheld(self):
        secret = "opaque_value_not_known_to_host"
        deep = {"x-api-key": secret}
        layers = getattr(transcripts.child_tasks, "KEYED_STRING_LAYERS", 4)   # getattr: the test also runs on c88055b
        for _ in range(layers + 2):                     # layers + 1 string literals around the object: one too many
            deep = json.dumps(deep)
        s = self.session()
        self.turn(s, tool_results={"c": {"content": [{"type": "text", "text": deep}]}})
        self.raw_has_no(s, secret)
        _, t0 = self.lines(s)
        self.assertEqual(t0["tool_results"][0]["content"][0]["text"], WITHHELD)
        self.assertEqual(t0["redaction"]["withheld"], 1)
        # A quoted plain string is not JSON in disguise: kept as it is.
        s2 = self.session(sid="quoted-ok-" + "0" * 22)
        quoted = json.dumps(json.dumps(json.dumps("just text")))
        self.turn(s2, tool_results={"c": {"content": [{"type": "text", "text": quoted}]}})
        _, t1 = self.lines(s2)
        self.assertEqual(t1["tool_results"][0]["content"][0]["text"], quoted)
        self.assertEqual(t1["redaction"]["withheld"], 0)

    def test_escape_scanning_is_linear_in_long_backslash_runs(self):
        import time
        red = transcripts.child_tasks.provider_redactor((TOKEN,))
        for text in ("\\" * (1 << 20), "\\" * (1 << 20) + "u12", "a\\" * (1 << 19)):
            t = time.monotonic()
            self.assertFalse(red.encoded_hit(text))
            # Generous: about 0.02 s here; on c88055b 128k backslashes took 7 s and 1 MB would take minutes.
            self.assertLess(time.monotonic() - t, 1.0)
        # Escaped spellings are still found.
        self.assertTrue(red.encoded_hit("x " + "".join("\\\\u%04x" % ord(c) for c in TOKEN)))
        self.assertTrue(red.encoded_hit("".join("\\x%02x" % ord(c) for c in TOKEN)))

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
