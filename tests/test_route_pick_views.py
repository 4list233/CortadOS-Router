"""Route picker step 5 (docs/ORCHESTRATOR-ROUTE-PICKER.md §6, §7): the routes row's `route_pick` column (a migration on
old databases), the Tasks view's labels and `pick` field, the Live view's pick on conversations, the orchestrator tree
and the session view, and the Office desk's badge. Labels come only from band names and table ids: nothing a request
could have written is shown. Fakes and throwaway homes only."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import core
from cortados_router.provider import live, office, routing, tasks
from cortados_router.provider import server as provider_server
from tests import test_tasks as tt
from tests.test_route_pick import OPUS, SOL, Catalogue, SelectionTests

LABEL = f"R3:{OPUS}+{SOL}"          # both real table ids (tables/model-scores.generated.json)
SESSION = "3f9a1c2e9b7d4a10"


class ColumnTests(unittest.TestCase):

    def setUp(self):
        self.home = tt.home()                               # core.DDL + the old ALTERs: no route_pick column
        self.addCleanup(shutil.rmtree, self.home, True)

    def cols(self):
        c = sqlite3.connect(self.home / "router.db")
        try:
            return {r[1] for r in c.execute("PRAGMA table_info(routes)")}
        finally:
            c.close()

    def test_column_migrates_on_an_old_db_and_is_written(self):
        self.assertNotIn("route_pick", self.cols())
        tt.insert(self.home, turn_id="t-1")
        with mock.patch.object(core, "STATE", self.home):
            self.assertTrue(core.log_route_pick("t-1", LABEL))
        self.assertIn("route_pick", self.cols())
        c = sqlite3.connect(self.home / "router.db")
        self.assertEqual(c.execute("SELECT route_pick, band_window FROM routes WHERE turn_id='t-1'").fetchone(),
                         (LABEL, None))
        c.close()

    def test_writer_refuses_what_is_no_label(self):
        with mock.patch.object(core, "STATE", self.home):
            for bad in ("", "x" * (core.ROUTE_PICK_MAX + 1), "R3:a\nb", None):
                with self.subTest(bad=bad):
                    self.assertFalse(core.log_route_pick("t-1", bad))

    def test_tasks_view_reads_an_old_db_without_the_column(self):
        tt.insert(self.home, turn_id="t-old")
        v = tasks.collect(self.home, now=tt.NOW)
        self.assertEqual(v["errors"], [])
        self.assertIsNone(v["tasks"][0]["pick"])


class TasksLabelTests(unittest.TestCase):

    def setUp(self):
        self.home = tt.home()
        self.addCleanup(shutil.rmtree, self.home, True)
        with mock.patch.object(core, "STATE", self.home):
            core._db().close()                              # the writer's open adds route_pick

    def row(self, rules, pick=None):
        tt.insert(self.home, turn_id="t-" + str(len(rules)) + str(pick)[:20], hard_rules=json.dumps(rules),
                  band="R3", **({"route_pick": pick} if pick is not None else {}))
        return tasks.collect(self.home, now=tt.NOW)["tasks"][0]

    def test_labels_from_table_ids(self):
        t = self.row([f"route pick {LABEL}: 2 models, 4 routes", "route pick: local cost routes not taken"], LABEL)
        self.assertEqual(t["pick"], LABEL)
        self.assertIn(f"route pick {LABEL} (2 models)", t["s1_reason"])
        self.assertIn("route pick: local cost route not taken", t["s1_reason"])

    def test_escalated_overridden_and_star(self):
        t = self.row(["route pick escalated: no selected model reaches 95 (code; the strongest selected has 66.2)",
                      f"route pick {LABEL} overridden: sensitive", "route pick R4:*: 3 models, 5 routes (a, b, c)"],
                     "R4:*")
        self.assertEqual(t["pick"], "R4:*")
        for want in ("route pick escalated", f"route pick {LABEL} overridden", "route pick R4:* (3 models)"):
            self.assertIn(want, t["s1_reason"])
        self.assertNotIn("66.2", t["s1_reason"])                 # the reason's numbers stay on the row

    def test_nothing_request_derived(self):
        bad = "R3:claude-opus-5-5+ignore-previous-instructions"
        t = self.row([f"route pick {bad}: 2 models, 2 routes"], bad)
        self.assertIsNone(t["pick"])                             # an id that is no table id: not shown
        self.assertIn("route pick (2 models)", t["s1_reason"])
        self.assertNotIn("ignore", json.dumps(t))
        for junk in ("<script>alert(1)</script>", "R3:CLAUDE-OPUS-5-5", "R3:" + "+".join([OPUS] * 9), tt.SECRET):
            with self.subTest(junk=junk[:20]):
                self.assertIsNone(self.row([], junk)["pick"])


class ChooseRecordsThePickTests(Catalogue):

    choose = SelectionTests.choose                         # the helper only: SelectionTests' own tests stay there

    def setUp(self):
        super().setUp()
        self.picks = []
        p = mock.patch.object(core, "log_route_pick", lambda rid, label: self.picks.append((rid, label)) or True)
        p.start(); self.addCleanup(p.stop)

    def test_a_restricted_pick_is_recorded_on_its_row(self):
        r = self.choose(f"R3:{OPUS}+{SOL}")
        self.assertFalse(r.get("error"))
        self.assertEqual(self.picks, [(r["route_id"], f"R3:{OPUS}+{SOL}")])

    def test_window_only_and_refused_picks_record_nothing(self):
        self.choose("R3")                                      # a window: the band_window column says it
        self.health.update(claude=self.health["claude"] | {"ok": False}, **{"claude#b": self.health["claude#b"] |
                                                                             {"ok": False}})
        r = self.choose(f"R3:{OPUS}")                          # E2: refused, no row
        self.assertTrue(r.get("error"))
        self.assertEqual(self.picks, [])


class LiveAndOfficeTests(unittest.TestCase):

    def conv(self, label, **kw):
        s = mock.Mock()
        s.id, s.lock, s.office_worker = "conv-1", None, ("main:" + SESSION, {"session": SESSION, "project": "Router"})
        s.route = {"provider": "claude-cli#b", "model": OPUS, provider_server.ASKED_WINDOW_KEY: label,
                   "route": {"chosen": {"lane": "claude#b"}}}
        s.opened_at = s.last_used = time.time()
        s.awaiting_tool_result, s.live_tokens, s.office_origin, s.wire_path = False, None, None, "/v1/messages"
        for k, v in kw.items():
            setattr(s, k, v)
        return provider_server.Registry._conversation_meta(s)

    def test_conversation_meta_carries_the_pick(self):
        self.assertEqual(self.conv(LABEL)["pick"], LABEL)
        self.assertEqual(self.conv("R3")["pick"], "R3")          # a window-only pick: the window's label
        self.assertIsNone(self.conv(None)["pick"])
        self.assertIsNone(self.conv("R3:<b>")["pick"])

    def tree(self, label):
        now = time.time()
        meta = [self.conv(label)]
        conv = live.conversations_section({"floors": []}, 1, now, backend=meta)
        return conv, live.orchestrators_section([], None, conv, meta, [], {}, now, session=SESSION)

    def test_live_items_tree_and_session_view(self):
        conv, orch = self.tree(LABEL)
        item = conv["items"][0]
        self.assertEqual((item["kind"], item["pick"]), ("orchestrator", LABEL))
        g = orch["items"][0]
        self.assertEqual(g["pick"], LABEL)
        self.assertEqual(g["pick_route"], f"{LABEL}: Claude Opus 5.5 · claude#b")
        sv = live.session_view({"orchestrators": orch, "overview": {}}, SESSION)
        self.assertEqual(sv["orchestrators"]["items"][0]["pick_route"], f"{LABEL}: Claude Opus 5.5 · claude#b")
        tree = live.office_tree({"orchestrators": orch, "conversations": conv})
        self.assertEqual(tree["orchestrators"]["items"][0]["pick"], LABEL)
        _conv, none = self.tree(None)
        self.assertEqual((none["items"][0]["pick"], none["items"][0]["pick_route"]), (None, None))

    def test_sub_session_shows_its_pick(self):
        node = live._sub_node({"ref": "c-abc", "pick": LABEL, "opened_at": 1.0}, 2.0)
        self.assertEqual(node["pick"], LABEL)
        self.assertIsNone(live._sub_node({"ref": "c-abc", "pick": "R3:Bad Name"}, 2.0)["pick"])

    def test_office_desk_badge(self):
        board = office.OfficeBoard()
        tags = {"session": SESSION}
        board.turn_start("main:" + SESSION, tags, run_id=None, provider="claude", lane="claude#b", model=OPUS,
                         pick=LABEL)
        self.assertEqual(board.view([])["floors"][0]["orchestrator"]["pick"], LABEL)
        board.turn_start("main:" + SESSION, tags, run_id=None, provider="claude", lane="claude#b", model=OPUS,
                         pick="R3:<img src=x>")
        self.assertIsNone(board.view([])["floors"][0]["orchestrator"]["pick"])


if __name__ == "__main__":
    unittest.main()
