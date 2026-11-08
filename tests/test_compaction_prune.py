"""compaction/prune.py: table-driven shadow keep/drop rules and the decisions file."""
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from cortados_router.compaction import prune, store
from cortados_router.compaction.events import Message, Session, ToolEvent

T = prune.load_table()


def session(events, n_msgs=20):
    msgs = tuple(Message(session="s", index=i, role="assistant" if i % 2 else "user", text="") for i in range(n_msgs))
    evs = tuple(ToolEvent(session="s", index=i, message_index=mi, name=name, input=inp, result_text="r" * chars,
                          result_chars=chars, is_error=err, interrupted=False)
                for i, (mi, name, inp, chars, err) in enumerate(events))
    return Session(session="s", source="provider", messages=msgs, events=evs)


BIG = 5000


class RuleTests(unittest.TestCase):
    CASES = [
        # (message_index, name, input, chars, error) -> (rule, keep_call, keep_result)
        ((0, "Read", {"file_path": "a"}, BIG, False), ("pinned", True, True)),
        ((19, "Read", {"file_path": "z"}, BIG, False), ("pinned", True, True)),
        ((3, "Read", {"file_path": "b"}, BIG, True), ("error", True, True)),
        ((3, "Edit", {"file_path": "b"}, BIG, False), ("side_effect", True, True)),
        ((3, "Bash", {"command": "rm -rf build"}, BIG, False), ("side_effect", True, True)),
        ((3, "Bash", {"command": "cat a > b"}, BIG, False), ("side_effect", True, True)),
        ((3, "mcp__slack__send_message", {}, BIG, False), ("side_effect", True, True)),
        ((3, "Read", {"file_path": "c"}, 100, False), ("small", True, True)),
        ((3, "mcp__docs__search_docs", {}, BIG, False), ("not_rerunnable", True, True)),
        ((3, "Grep", {"pattern": "x"}, BIG, False), ("rerunnable_read", True, False)),
        ((3, "Bash", {"command": "git log -5"}, BIG, False), ("rerunnable_read", True, False)),
    ]

    def test_table(self):
        for ev, (rule, kc, kr) in self.CASES:
            with self.subTest(ev=ev):
                (d, saved), = prune.decide(session([ev]), table=T)
                self.assertEqual((d.decided_by, d.keep_call, d.keep_result), ("rule:" + rule, kc, kr))
                self.assertEqual(saved, 0 if kr else BIG - T["dropped_head_chars"])

    def test_superseded_read_dropped_even_if_small(self):
        out = prune.decide(session([(3, "Read", {"file_path": "a"}, 100, False),
                                    (5, "Read", {"file_path": "a"}, 100, False)]), table=T)
        self.assertEqual([d.decided_by for d, _ in out], ["rule:superseded", "rule:small"])

    def test_pin_window_follows_table(self):
        t = dict(T, pin_last_messages=2)
        (d, _), = prune.decide(session([(17, "Read", {"file_path": "a"}, BIG, False)]), table=t)
        self.assertEqual(d.decided_by, "rule:rerunnable_read")

    def test_every_table_value_has_a_why(self):
        raw = json.loads(prune.TABLE_PATH.read_text())
        for k, v in raw.items():
            if isinstance(v, dict):
                self.assertTrue(v.get("why"), k)
        self.assertEqual((T["pin_last_messages"], T["small_result_chars"], T["dropped_head_chars"]), (8, 600, 300))


class ShadowDecideTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home, "CORTADOS_COMPACTION_STORE": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_decisions_file(self):
        s = store.Store()
        s.record_turn("c", source="claude-code", user_content=[{"type": "text", "text": "go"}], tool_results={},
                      assistant_blocks=[{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}])
        s.record_turn("c", source="claude-code", user_content=[], assistant_blocks=[{"type": "text", "text": "ok"}],
                      tool_results={"t1": {"content": [{"type": "text", "text": "x" * 900}]}})
        ds = prune.shadow_decide(store.session_path("c"))
        self.assertEqual(len(ds), 1)
        p = prune.decisions_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        row = json.loads(p.read_text().splitlines()[0])
        self.assertEqual((row["mode"], row["event_index"], row["decided_by"]), ("shadow", 0, "rule:pinned"))
        self.assertIn("saved_tokens_est", row)

    def test_async_is_noop_when_off_or_missing(self):
        self.assertIsNone(prune.shadow_decide_async("nope"))
        with mock.patch.dict(os.environ, {"CORTADOS_COMPACTION_STORE": "0"}):
            self.assertIsNone(prune.shadow_decide_async("c"))

    def test_async_runs_in_background(self):
        store.Store().record_turn("c", source="codex", user_content=[{"type": "text", "text": "go"}], tool_results={},
                                  assistant_blocks=[{"type": "tool_use", "id": "t1", "name": "LS", "input": {}}])
        th = prune.shadow_decide_async("c")
        th.join(5)
        self.assertTrue(prune.decisions_path().exists())


if __name__ == "__main__":
    unittest.main()
