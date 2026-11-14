"""cortados_router/orchestrator_usage.py: main-session tokens from Claude Code transcripts. Fixture jsonl files in a
throwaway projects folder only; the real ~/.claude is never read (every scan names its folder, and read_salt its glob)."""
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from cortados_router import orchestrator_usage as ou
from cortados_router.provider import live, work_split

NOW = 1_800_000_000.0
SALT = "0123456789abcdef0123456789abcdef"
SECRET = "SECRET-PROMPT-TEXT-7781"
MAIN = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-aaaaaaaaaaaa"
CHILD = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def line(sid, mid, ts, *, inp=0, out=0, cr=0, cw=0, model="claude-opus-5", cwd="/Users/o/code/CortadOS-Router", **extra):
    e = {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": iso(ts), "isSidechain": False,
         "uuid": f"u-{mid}-{ts}", "requestId": f"req-{mid}",
         "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": SECRET}],
                     "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw}}}
    e.update(extra)
    return e


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, entries, mtime=NOW):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in entries))
        os.utime(p, (mtime, mtime))
        return p

    def scan(self, **kw):
        return ou.scan(self.root, now=NOW, salt=SALT, **kw)

    def test_sums_a_main_session_per_window_once_per_message(self):
        self.write(f"-Users-o-code-CortadOS-Router/{MAIN}.jsonl", [
            {"type": "user", "sessionId": MAIN, "message": {"role": "user", "content": SECRET}},
            # streaming: one line per content block, each repeating the usage; the last line carries the final count
            line(MAIN, "msg_a", NOW - 3600, inp=10, out=1, cr=1000, cw=200),
            line(MAIN, "msg_a", NOW - 3600, inp=10, out=40, cr=1000, cw=200),
            line(MAIN, "msg_b", NOW - 3 * 86400, inp=5, out=50, cr=3000, cw=0),
            line(MAIN, "msg_old", NOW - 9 * 86400, inp=999, out=999),               # outside both windows
            line(MAIN, "msg_syn", NOW - 60, model="<synthetic>"),
        ])
        s = self.scan()
        self.assertEqual(s["totals"]["24h"], {"input": 10, "output": 40, "cache_read": 1000, "cache_write": 200, "sessions": 1})
        self.assertEqual(s["totals"]["7d"], {"input": 15, "output": 90, "cache_read": 4000, "cache_write": 200, "sessions": 1})
        [row] = s["sessions"]
        self.assertEqual(row["session"], ou.session_tag(SALT, MAIN))
        self.assertEqual(len(row["session"]), 16)
        self.assertEqual((row["project"], row["model"], row["last_at"]), ("CortadOS-Router", "claude-opus-5", NOW - 3600))
        raw = json.dumps(s)
        for leak in (SECRET, MAIN, "/Users/o", "msg_a", "req-"):
            self.assertNotIn(leak, raw)
        # the provider accepts it as a heartbeat block (and the sender's check is the same rule)
        self.assertIsNone(work_split.validate_block(s, NOW, live.TIME_SKEW_S)[1])
        self.assertIsNone(live.validate_heartbeat({"host": "air", "jobs": [], "orchestrator": s}, NOW)[1])

    def test_session_tag_matches_the_mod(self):
        # register.ts sessionTag: sha256(`${salt}:${sessionId}`).hex.slice(0, 16)
        import hashlib
        self.assertEqual(ou.session_tag(SALT, MAIN), hashlib.sha256(f"{SALT}:{MAIN}".encode()).hexdigest()[:16])

    def test_direct_model_subagents_count_as_orchestrator_routed_ones_do_not(self):
        self.write(f"-Users-o-code-X/{MAIN}.jsonl", [
            line(MAIN, "m1", NOW - 60, inp=1, out=2, cwd="/Users/o/code/X"),
            line(MAIN, "m2", NOW - 60, inp=10, out=10, cwd="/Users/o/code/X", isSidechain=True),
            line(MAIN, "m3", NOW - 60, inp=100, out=100, cwd="/Users/o/code/X", model="cortados"),
            line(MAIN, "m4", NOW - 60, inp=100, out=100, cwd="/Users/o/code/X", model="sub/gpt-6.1-sol"),
            line(MAIN, "m6", NOW - 60, inp=100, out=100, cwd="/Users/o/code/X", model="api/claude-haiku-5"),
        ])
        # a subagent's own transcript, one level down: an Agent-tool subagent on a direct model is the main session's
        # work (its session id is the parent's, also when a line has none); a routed one asks for `cortados`
        self.write(f"-Users-o-code-X/{MAIN}/subagents/agent-1.jsonl", [
            line(MAIN, "m5", NOW - 60, inp=500, out=500, cwd="/Users/o/code/X", isSidechain=True,
                 model="claude-sonnet-5"),
            line(None, "m7", NOW - 3 * 86400, inp=7, cwd="/Users/o/code/X", isSidechain=True, sessionId=None),
            line(MAIN, "m8", NOW - 60, inp=900, out=900, cwd="/Users/o/code/X", isSidechain=True, model="cortados"),
        ])
        s = self.scan()
        self.assertEqual(s["totals"]["24h"], {"input": 511, "output": 512, "cache_read": 0, "cache_write": 0, "sessions": 1})
        self.assertEqual(s["totals"]["7d"]["input"], 518)
        self.assertEqual(s["subagent"]["24h"], {"input": 510, "output": 510, "cache_read": 0, "cache_write": 0})
        self.assertEqual(s["subagent"]["7d"]["input"], 517)
        [row] = s["sessions"]                    # one session: the subagents' tokens are on their main session's row
        self.assertEqual((row["session"], row["24h"]["input"]), (ou.session_tag(SALT, MAIN), 511))
        self.assertEqual(s["excluded"]["routed_model_entries"], 4)
        self.assertNotIn("sidechain_entries", s["excluded"])
        self.assertIsNone(work_split.validate_block(s, NOW, live.TIME_SKEW_S)[1])

    def test_provider_bridge_transcripts_are_not_the_orchestrators(self):
        """The provider's Claude lane runs `claude -p` on the default login in <tmp>/cortados-bridge-<random>/work: its
        transcript lands in the same projects dir on a real model, and the same turn is in router.db (log_tokens)."""
        from cortados_router.provider import bridge, search_lane
        bwork = f"/private/var/folders/x/T/{bridge.SCRATCH_PREFIX}k3_9zq1a/work"
        swork = f"/private/var/folders/x/T/{search_lane.SCRATCH_PREFIX}p0q1r2s3"
        # by cwd
        self.write(f"{ou._slug(bwork)}/{CHILD}.jsonl",
                   [line(CHILD, "b1", NOW - 60, inp=800, out=80, model="claude-opus-5-5", cwd=bwork)])
        # by slug alone (no cwd), the search lane's one-shot CLI too
        self.write(f"{ou._slug(swork)}/{OTHER}.jsonl",
                   [line(OTHER, "b2", NOW - 60, inp=800, model="claude-haiku-4-5-20251001", cwd=None)])
        # by cwd while the slug looks ordinary (a cwd is the stronger evidence)
        self.write(f"-Users-o-elsewhere/{OTHER}z.jsonl",
                   [line(OTHER + "z", "b3", NOW - 60, inp=800, model="claude-opus-5-5", cwd=bwork)])
        # a main session whose project name merely mentions the bridge
        self.write(f"-Users-o-code-cortados-bridge/{MAIN}.jsonl",
                   [line(MAIN, "m1", NOW - 60, inp=3, cwd="/Users/o/code/cortados-bridge")])
        s = self.scan()
        self.assertEqual(s["totals"]["24h"]["input"], 3)
        self.assertEqual(s["excluded"]["provider_bridge_files"], 3)
        self.assertEqual(s["excluded"]["child_task_files"], 0)
        self.assertEqual([r["project"] for r in s["sessions"]], ["cortados-bridge"])
        self.assertIsNone(work_split.validate_block(s, NOW, live.TIME_SKEW_S)[1])

    def test_the_scratch_prefixes_are_the_bridges_own(self):
        from cortados_router.provider import bridge, search_lane
        self.assertEqual(ou.PROVIDER_SCRATCH_PREFIXES, (bridge.SCRATCH_PREFIX, search_lane.SCRATCH_PREFIX))
        tmp = "/private/var/folders/0w/x/T"
        # tempfile's suffixes are [a-z0-9_]; one starting with '_' slugs to a doubled '-' (cortados-bridge--...)
        for suffix in ("k3x9zq1a", "_a1b2c3d", "a1b2c3d_", "ab__cd_e", "-a1b2c3d"):
            bwork = f"{tmp}/{bridge.SCRATCH_PREFIX}{suffix}/{bridge.SCRATCH_WORK}"
            swork = f"{tmp}/{search_lane.SCRATCH_PREFIX}{suffix}"
            for path in (bwork, swork):
                with self.subTest(path=path):
                    self.assertTrue(ou.is_provider_path(path, ""))
                    self.assertTrue(ou.is_provider_path(None, ou._slug(path)))

    def test_project_folders_named_like_a_scratch_prefix_are_kept(self):
        from cortados_router.provider import bridge, search_lane
        for path in ("/Users/me/cortados-bridge-v2", "/Users/me/cortados-bridge-v2/src",
                     "/Users/me/cortados-search-v2/src", f"/Users/me/{bridge.SCRATCH_PREFIX}", "/Users/me/cortados-bridge"):
            with self.subTest(path=path):
                self.assertFalse(ou.is_provider_path(path, ""))
                if "search" not in path:   # a slug cannot tell <search scratch>-src from a folder; the cwd can
                    self.assertFalse(ou.is_provider_path(None, ou._slug(path)))
        # the real bridge scratch path and the search lane's are still excluded
        self.assertTrue(ou.is_provider_path(f"/private/var/folders/0w/x/T/{bridge.SCRATCH_PREFIX}co6jigea/work", ""))
        self.assertTrue(ou.is_provider_path(f"/private/var/folders/0w/x/T/{search_lane.SCRATCH_PREFIX}p0q1r2s3", ""))

    def test_child_task_transcripts_are_excluded_by_cwd_or_slug(self):
        task = "t20261113153431-36062624"
        # by cwd: any <...>/{active,archive,held}/<task id>[/sub] folder
        self.write(f"-Users-o-CortadOS-held-{task}/{CHILD}.jsonl",
                   [line(CHILD, "c1", NOW - 60, inp=700, cwd=f"/Users/o/CortadOS/held/{task}")])
        self.write(f"-Users-o-elsewhere/{OTHER}.jsonl",
                   [line(OTHER, "c2", NOW - 60, inp=700, cwd=f"/Users/o/CortadOS/active/{task}/sub")])
        # by a configured task root, even when the folder name is not a task id
        self.write(f"-Users-o-roots-x/{OTHER}x.jsonl",
                   [line(OTHER + "x", "c3", NOW - 60, inp=700, cwd="/Users/o/roots/archive/some-folder")])
        # by slug alone (no cwd on any line)
        self.write(f"-Users-o-CortadOS-archive-{task}/{CHILD}y.jsonl",
                   [line(CHILD + "y", "c4", NOW - 60, inp=700, cwd=None)])
        # a main session whose project merely contains the word "held"
        self.write(f"-Users-o-held-project/{MAIN}.jsonl", [line(MAIN, "m1", NOW - 60, inp=3, cwd="/Users/o/held/project")])
        s = self.scan(task_roots=["/Users/o/roots"])
        self.assertEqual(s["totals"]["24h"]["input"], 3)
        self.assertEqual(s["excluded"]["child_task_files"], 4)
        self.assertEqual([r["project"] for r in s["sessions"]], ["project"])

    def test_resumed_sessions_and_old_files(self):
        # a resumed session copies earlier lines into its new file: the same message ids count once
        self.write(f"-Users-o-p/{MAIN}.jsonl", [line(MAIN, "m1", NOW - 600, inp=10, cwd="/Users/o/p")])
        self.write(f"-Users-o-p/{OTHER}.jsonl", [line(MAIN, "m1", NOW - 600, inp=10, cwd="/Users/o/p"),
                                                  line(OTHER, "m2", NOW - 60, inp=1, cwd="/Users/o/p")])
        # a file untouched for longer than the widest window is not opened at all
        self.write(f"-Users-o-p/{CHILD}.jsonl", [line(CHILD, "m3", NOW - 60, inp=1000, cwd="/Users/o/p")],
                   mtime=NOW - 8 * 86400)
        self.write(f"-Users-o-p/bad.jsonl", ['{"type": "assistant", not json', "", "[1, 2]"])
        s = self.scan()
        self.assertEqual(s["totals"]["24h"]["input"], 11)
        self.assertEqual(s["excluded"]["bad_lines"], 1)

    def test_no_salt_no_tags_and_the_session_cap(self):
        for i in range(5):
            self.write(f"-Users-o-p/s{i}.jsonl", [line(f"s{i}", f"m{i}", NOW - 60, inp=10 + i, cwd="/Users/o/p")])
        s = ou.scan(self.root, now=NOW, salt=None, sessions_max=3)
        self.assertFalse(s["salt"])
        self.assertEqual([r["session"] for r in s["sessions"]], [None, None, None])
        self.assertEqual([r["24h"]["input"] for r in s["sessions"]], [14, 13, 12])     # most fresh tokens first
        self.assertEqual(s["sessions_hidden"], 2)
        self.assertEqual(s["totals"]["24h"]["sessions"], 5)                           # the totals keep every session
        self.assertEqual(s["totals"]["24h"]["input"], 60)

    def test_missing_folder_is_an_empty_summary(self):
        s = ou.scan(self.root / "absent", now=NOW, salt=SALT)
        self.assertEqual(s["sessions"], [])
        self.assertEqual(s["totals"]["7d"]["sessions"], 0)

    def test_read_salt(self):
        store = self.root / "store"
        store.mkdir()
        self.assertIsNone(ou.read_salt(str(store / "cortados-routed-subagents_*.json")))
        (store / "cortados-routed-subagents_a.json").write_text(json.dumps({"officeSalt": "not-hex"}))
        self.assertIsNone(ou.read_salt(str(store / "cortados-routed-subagents_*.json")))
        (store / "cortados-routed-subagents_b.json").write_text(json.dumps({"officeSalt": SALT}))
        self.assertEqual(ou.read_salt(str(store / "cortados-routed-subagents_*.json")), SALT)

    def test_timestamps(self):
        self.assertEqual(ou._ts("2027-01-15T08:00:00Z"), NOW)
        self.assertEqual(ou._ts("2027-01-15T08:00:00.000Z"), NOW)
        self.assertAlmostEqual(ou._ts("2027-01-15T08:00:00.1234567Z") - ou._ts("2027-01-15T08:00:00Z"), 0.1234567, places=5)
        self.assertEqual(ou._ts("2027-01-15T09:00:00+01:00"), ou._ts("2027-01-15T08:00:00Z"))
        for bad in (None, "", "yesterday", 5, "2027-13-45T00:00:00Z"):
            self.assertIsNone(ou._ts(bad), bad)

    def test_cli_prints_the_summary(self):
        self.write(f"-Users-o-p/{MAIN}.jsonl", [line(MAIN, "m1", NOW - 60, inp=7, cwd="/Users/o/p")])
        out = StringIO()
        with redirect_stdout(out), mock.patch.object(ou, "read_salt", return_value=None):
            self.assertEqual(ou.main(["--projects", str(self.root), "--now", str(NOW)]), 0)
        d = json.loads(out.getvalue())
        self.assertEqual(d["totals"]["24h"]["input"], 7)
        self.assertIn("scan_ms", d)


if __name__ == "__main__":
    unittest.main()
