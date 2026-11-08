import unittest

from cortados_router.compaction.events import Message, Session, ToolEvent
from cortados_router.compaction.labels import label_session, target

LONG_LINE = "def resolve(request, answers, degraded=False, engine='s1'):  # the band decision"


def ev(i, mi, name, inp, result="", **kw):
    return ToolEvent(session="s", index=i, message_index=mi, name=name, input=inp, result_text=result,
                     result_chars=len(result), is_error=False, interrupted=kw.get("interrupted", False))


def msg(i, role, text):
    return Message(session="s", index=i, role=role, text=text)


def session(events, texts):
    return Session(session="s", source="test", messages=tuple(msg(i, r, t) for i, (r, t) in enumerate(texts)),
                   events=tuple(events))


FILLER = [("user", "go on"), ("assistant", "ok"), ("user", "more"), ("assistant", "done")]


class LabelTests(unittest.TestCase):
    def rows(self, events, texts, horizon=4):
        return {r["event_index"]: r for r in label_session(session(events, texts), horizon=horizon)}

    def test_reread_same_file_is_needed(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/core.py"}, "x = 1"),
                       ev(1, 3, "Read", {"file_path": "/a/core.py"}, "x = 1")],
                      [("user", "fix"), ("assistant", "reading"), ("user", "again"), ("assistant", "rereading")] + FILLER)
        self.assertTrue(r[0]["needed_result"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "rerun")
        self.assertEqual(r[0]["evidence"][0]["index"], 1)

    def test_rerun_normalizes_whitespace(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "ls   -la"}, "a"), ev(1, 2, "Bash", {"command": "ls -la"}, "a")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertEqual(r[0]["evidence"][0]["rule"], "rerun")

    def test_quote_of_long_line(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/core.py"}, "short\n" + LONG_LINE + "\n")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "I will change `" + LONG_LINE + "` now")] + FILLER)
        self.assertTrue(r[0]["needed_result"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "quote")

    def test_identifier_first_seen_in_result(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "git log -1"}, "commit 9fe1b29c0ffee\nAuthor: x")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "reverting 9fe1b29c0ffee")] + FILLER)
        self.assertEqual(r[0]["evidence"][0]["rule"], "identifier")

    def test_identifier_already_known_does_not_count(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "git log -1"}, "commit 9fe1b29c0ffee")],
                      [("user", "look at 9fe1b29c0ffee"), ("assistant", "a"), ("assistant", "9fe1b29c0ffee again")] + FILLER)
        self.assertFalse(r[0]["needed_result"])

    def test_never_referenced_is_false(self):
        r = self.rows([ev(0, 1, "Grep", {"pattern": "foo"}, "nothing useful here")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "unrelated")] + FILLER)
        self.assertEqual(r[0]["status"], "labelled")
        self.assertFalse(r[0]["needed_result"])
        self.assertFalse(r[0]["needed_call"])
        self.assertEqual(r[0]["evidence"], [])

    def test_side_effect_keeps_call_not_result(self):
        r = self.rows([ev(0, 1, "Edit", {"file_path": "/a/x.py", "old_string": "a", "new_string": "b"}, "ok"),
                       ev(1, 1, "Bash", {"command": "git commit -m wip"}, "[main abc] wip")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        for i in (0, 1):
            self.assertTrue(r[i]["needed_call"])
            self.assertFalse(r[i]["needed_result"])
            self.assertEqual(r[i]["evidence"][0]["rule"], "side_effect")

    def test_later_write_is_not_a_reread(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/x.py"}, "body"),
                       ev(1, 2, "Write", {"file_path": "/a/x.py", "content": "new"}, "ok")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertFalse(r[0]["needed_result"])
        self.assertTrue(r[0]["needed_call"])          # the later write names the same path
        self.assertEqual(r[0]["evidence"][0]["rule"], "target")

    def test_target_mention_in_message(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/zz.py"}, "body")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "as /a/zz.py showed")] + FILLER)
        self.assertFalse(r[0]["needed_result"])
        self.assertTrue(r[0]["needed_call"])

    def test_tail_events_are_unknown(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a"}, "x"), ev(1, 6, "Read", {"file_path": "/b"}, "y")],
                      [("user", "u"), ("assistant", "a")] + FILLER + [("assistant", "z")], horizon=4)
        self.assertEqual(r[0]["status"], "labelled")
        self.assertEqual(r[1]["status"], "unknown")
        self.assertIsNone(r[1]["needed_result"])
        self.assertEqual(r[1]["horizon"]["min_future_messages"], 4)

    def test_interrupted_has_no_needed_result(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "make"}, "", interrupted=True),
                       ev(1, 2, "Bash", {"command": "make"}, "built")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertFalse(r[0]["needed_result"])

    def test_quote_with_quotes_and_backslashes_in_edit_input(self):
        line = 'pattern = re.compile(r"\\d+\\s*\"quoted\"")  # a regex line with escapes in it'
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/re.py"}, "x\n" + line + "\n"),
                       ev(1, 2, "Edit", {"file_path": "/a/re.py", "old_string": line, "new_string": "y"}, "ok")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertTrue(r[0]["needed_result"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "quote")
        self.assertEqual(r[0]["evidence"][0]["index"], 1)

    def test_identifier_in_nested_input_list(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "git log -1"}, 'commit "9fe1b29c0ffee"'),
                       ev(1, 2, "Tool", {"args": [{"ref": "9fe1b29c0ffee\\x"}]}, "ok")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertEqual(r[0]["evidence"][0]["rule"], "identifier")

    def test_target_kinds(self):
        self.assertEqual(target(ev(0, 0, "Grep", {"pattern": " a  b "})), ("query", "grep:a b"))
        self.assertIsNone(target(ev(0, 0, "Other", {"x": 1})))


if __name__ == "__main__":
    unittest.main()
