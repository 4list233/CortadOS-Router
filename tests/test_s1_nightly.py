"""s1/nightly.sh end to end on stubs: label failure stops, no new labels skips, promotion reaches the serving pointer
atomically with a .prev, a two-night label stall raises an ALERT, old logs rotate.

The real nightly.sh and nightly_state.py are copied into a temp "s1" next to stub dataset/label/build_sft/evaluate scripts and
a stub training python; HOME is the temp dir (stub launchctl, no ~/.cortados), so nothing real is read, trained or unloaded.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

S1 = Path(__file__).resolve().parents[1] / "s1"

STUBS = {
    "dataset.py": "open('data/ran-dataset', 'a').write('x')\n",
    "label.py": (
        "import json, os, sys\n"
        "open('data/label-args', 'w').write(' '.join(sys.argv[1:]))\n"
        "open('data/label-env', 'w').write(os.environ.get('CORTADOS_PROVIDER_URL', '<unset>'))\n"
        "p = 'data/labels.jsonl'\n"
        "have = sum(1 for _ in open(p)) if os.path.exists(p) else 0\n"
        "with open(p, 'a') as f:\n"
        "    for i in range(int(os.environ.get('STUB_NEW_LABELS', '0'))):\n"
        "        f.write(json.dumps({'id': f'r{have + i}', 'teacher': 'claude-sonnet-5', 'sample': 0}) + '\\n')\n"
        "if os.environ.get('STUB_STOP_REASON'): open('data/label-stop-reason', 'w').write(os.environ['STUB_STOP_REASON'] + '\\n')\n"
        "sys.exit(int(os.environ.get('STUB_LABEL_RC', '0')))   # partial labels are written before an early stop\n"
    ),
    "build_sft.py": (
        "import os\n"
        "os.makedirs('data/sft', exist_ok=True)\n"
        # deterministic like the real one: the SFT files follow the labels (+ STUB_SFT_EXTRA standing in for outcome/override rows)
        "labels = open('data/labels.jsonl').read() if os.path.exists('data/labels.jsonl') else ''\n"
        "open('data/sft/train.jsonl', 'w').write('{}\\n' + labels + os.environ.get('STUB_SFT_EXTRA', ''))\n"
        "open('data/sft/valid.jsonl', 'w').write('{}\\n')\n"
        "open('data/ran-build-sft', 'a').write('x')\n"
    ),
    "evaluate.py": (
        "import argparse, json, os\n"
        "ap = argparse.ArgumentParser(); ap.add_argument('--base'); ap.add_argument('--adapter'); ap.add_argument('--out')\n"
        "ap.add_argument('--baseline', action='store_true'); ap.add_argument('--holdout-key', action='store_true'); ap.add_argument('--holdout-count', action='store_true'); a = ap.parse_args()\n"
        "hk = open('data/stub-holdout').read() if os.path.exists('data/stub-holdout') else 'h0'\n"
        "floor = int(os.environ.get('CORTADOS_S1_MIN_HOLDOUT', '200'))\n"
        "if a.holdout_key: print(f'{hk}-min{floor}'); raise SystemExit(0)\n"
        "if a.holdout_count and os.environ.get('STUB_COUNT_RC'): print('Traceback: KeyError answers'); raise SystemExit(1)\n"
        "if a.holdout_count and os.environ.get('STUB_COUNT_GARBAGE'): print('fifty', floor); raise SystemExit(0)\n"
        "if a.holdout_count: print('labels: kept N Claude-teacher rows', file=__import__('sys').stderr)   # like read_labels\n"
        "if a.holdout_count: print(50 if hk == 'h0' else 300, floor); raise SystemExit(0)   # 50 rows until the backfill\n"
        "if os.environ.get('STUB_EVAL_GARBAGE'): open(os.path.join(a.out, 'report.json'), 'w').write('{trunc'); raise SystemExit(0)\n"
        "open('data/eval-count', 'a').write('x')\n"
        "if os.environ.get('STUB_EVAL_RC'): raise SystemExit(int(os.environ['STUB_EVAL_RC']))\n"
        "unavailable = os.environ.get('STUB_EVAL_UNAVAILABLE') == '1'\n"
        "blocked = os.environ.get('STUB_HOLDOUT_BLOCKED') == '1' and hk == 'h0' and floor > 50   # the stub holdout has 50 rows\n"
        "open('data/eval-pointer', 'w').write(os.environ.get('CORTADOS_S1_SERVE_POINTER', ''))\n"
        "c = {'band_accuracy': 0.9, 'under_routed': 0.01, 'over_routed': 0.1, 'outward_recall': 1.0}\n"
        "json.dump({'base': a.base, 'temperatures': {}, 'when': '2026-11-02', 'candidate': c,\n"
        "           'holdout_key': hk, **({'promote_blocked': 'teacher holdout too small'} if blocked else {}),\n"
        "           'evaluation_status': 'unavailable' if unavailable else 'ok',\n"
        "           **({'evaluation_unavailable': 'bar_error: incumbent load failed'} if unavailable else {}),\n"
        "           'promote': os.environ.get('STUB_PROMOTE', '1') == '1' and not blocked and not unavailable}, open(os.path.join(a.out, 'report.json'), 'w'))\n"
    ),
}

# stand-in for the mlx venv python: the base-model probe succeeds, `-m mlx_lm lora` writes a checkpoint, scripts run on python3
FAKE_PY = """#!/bin/bash
[ "$1" = "-c" ] && exit 0
if [ "$1" = "-m" ]; then
  while [ $# -gt 0 ]; do [ "$1" = "--adapter-path" ] && out="$2"; shift; done
  mkdir -p "$out"; echo w > "$out/0000100_adapters.safetensors"; echo w > "$out/adapters.safetensors"; exit 0
fi
exec python3 "$@"
"""


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class NightlyEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s1-nightly-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s1 = self.tmp / "repo" / "s1"
        (self.s1 / "data").mkdir(parents=True)
        for name in ("nightly.sh", "nightly_state.py", "common.py"):     # nightly_state counts labels with common's teacher filter
            shutil.copy(S1 / name, self.s1 / name)
        for name in ("cortados_router", "tables"):                         # common.py loads the router's question set
            (self.tmp / "repo" / name).symlink_to(S1.parent / name)
        for name, text in STUBS.items():
            (self.s1 / name).write_text(text)
        home = self.tmp / "home"
        (home / ".local/bin").mkdir(parents=True)
        _exe(home / ".local/bin/launchctl", "#!/bin/bash\necho \"launchctl $*\" >> \"$HOME/launchctl.log\"\n")
        _exe(self.tmp / "fake-py", FAKE_PY)
        self.pointer = self.s1 / "current.json"
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("CORTADOS_", "STUB_"))}
        self.env = {**keep, "HOME": str(home), "CORTADOS_NIGHTLY_SUPERVISED": "1", "CORTADOS_S1_PY": str(self.tmp / "fake-py"),
                    "CORTADOS_S1_GPU_SETTLE": "0", "CORTADOS_S1_EVAL_SETTLE": "0", "CORTADOS_ROUTER_REPO": str(self.tmp / "no-repo"),
                    "CORTADOS_HOME": str(self.tmp / "cortados-home")}
        self.night = 0

    def run_night(self, **env):
        self.night += 1
        run = f"runs/2026-01-{self.night:02d}"   # production-style: the runs/20*/ globs (retention, resume) run here
        out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh")], cwd=self.tmp,
                             env={**self.env, "CORTADOS_S1_RUN": run,
                                  "CORTADOS_S1_NIGHT_DATE": (date(2026, 1, 1) + timedelta(days=self.night)).isoformat(),
                                  **{k: str(v) for k, v in env.items()}},
                             capture_output=True, text=True, timeout=60)
        log = (self.s1 / run / "nightly.log").read_text()
        return out.returncode, log, self.s1 / run

    def status(self):
        return json.loads((self.s1 / "data/status.json").read_text())

    def test_label_failure_stops_before_training(self):
        rc, log, run = self.run_night(STUB_LABEL_RC=7, STUB_NEW_LABELS=3)
        self.assertEqual(rc, 3, log)
        self.assertIn("LABELING FAILED (label.py rc 7)", log)
        self.assertFalse((self.s1 / "data/ran-build-sft").exists(), "build_sft must not run after a labeling failure")
        self.assertFalse((run / "adapter").exists(), "no training after a labeling failure")
        self.assertFalse(self.pointer.exists())
        self.assertIn("label.py rc 7", self.status()["last_labeling_failure"]["detail"])

    def test_label_config_error_stops(self):
        rc, log, run = self.run_night(STUB_LABEL_RC=2, STUB_NEW_LABELS=0)
        self.assertEqual(rc, 3, log)
        self.assertIn("LABELING FAILED (label.py rc 2)", log)
        self.assertFalse((run / "adapter").exists())

    def test_label_stopped_early_with_growth_trains(self):
        self.run_night(STUB_NEW_LABELS=3)
        rc, log, run = self.run_night(STUB_LABEL_RC=3, STUB_NEW_LABELS=2)
        self.assertEqual(rc, 0, log)
        self.assertIn("LABELING STOPPED EARLY", log)
        self.assertIn("training tonight", log)
        self.assertTrue((run / "report.json").exists(), log)
        self.assertIn("label.py rc 3", self.status()["last_labeling_stopped_early"]["detail"])

    def test_label_stopped_early_without_growth_skips(self):
        self.run_night(STUB_NEW_LABELS=3)
        rc, log, run = self.run_night(STUB_LABEL_RC=3, STUB_NEW_LABELS=0)
        self.assertEqual(rc, 0, log)
        self.assertIn("LABELING STOPPED EARLY", log)
        self.assertIn("skipped: no new labels", log)
        self.assertFalse((run / "adapter").exists())

    def test_stale_synced_router_db_is_reported(self):
        db = self.tmp / "router-air.db"; db.write_text("")
        os.utime(db, (1_700_000_000, 1_700_000_000))
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_ROUTER_DB_EXTRA=db)
        self.assertIn("WARNING: synced router db", log)
        self.assertIn("min old", self.status()["last_stale_router_db"]["detail"])

    def test_changed_sft_data_trains_even_with_unchanged_labels(self):
        self.run_night(STUB_NEW_LABELS=3)
        rc, log, run = self.run_night(STUB_NEW_LABELS=0, STUB_SFT_EXTRA='{"outcome": 1}\n')   # e.g. a new outcome-loop example
        self.assertEqual(rc, 0, log)
        self.assertIn("training tonight", log)
        self.assertTrue((run / "report.json").exists(), log)

    def test_corrupt_labels_stop_without_poisoning_the_marker(self):
        (self.s1 / "data/labels.jsonl").write_text('{"id": "r0"}\nnot json\n')
        rc, log, run = self.run_night(STUB_NEW_LABELS=0)
        self.assertEqual(rc, 1, log)
        self.assertIn("LABEL COUNT FAILED", log)
        self.assertFalse((self.s1 / "data/last-train-labels").exists())
        self.assertFalse((run / "adapter").exists())
        self.assertIn("last_label_count_failure", self.status())

    def test_failing_nights_count_toward_the_stall_alert(self):
        self.run_night(STUB_NEW_LABELS=3)
        _, log, _ = self.run_night(STUB_LABEL_RC=2)
        self.assertNotIn("ALERT", log)
        rc, log, _ = self.run_night(STUB_LABEL_RC=2)
        self.assertEqual(rc, 3, log)
        self.assertIn("ALERT: S1 labels have not grown for 2 consecutive nights", log)
        self.assertIn("alert", self.status())

    def test_evaluate_sees_the_serve_pointer(self):
        other = self.tmp / "serving" / "current.json"
        self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_SERVE_POINTER=other)
        self.assertEqual((self.s1 / "data/eval-pointer").read_text(), str(other))
        self.run_night(STUB_NEW_LABELS=1)
        self.assertEqual((self.s1 / "data/eval-pointer").read_text(), str(self.pointer))

    def test_log_keep_is_validated(self):
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_LOG_KEEP="lots")
        self.assertIn("is not a whole number; keeping 14", log)
        _, log, run = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_LOG_KEEP=0)
        self.assertIn("keeping 1", log)
        self.assertEqual([p.parent.name for p in self.s1.glob("runs/*/nightly.log")], [run.name])

    def test_router_db_path_with_spaces(self):
        d = self.tmp / "dir with space"; d.mkdir()
        fresh = d / "router-air.db"; fresh.write_text("")
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_ROUTER_DB_EXTRA=f"{fresh}:{self.tmp / 'gone.db'}")
        self.assertIn(f"synced router db {fresh} fresh", log)
        self.assertIn(f"WARNING: synced router db {self.tmp / 'gone.db'} is missing", log)

    def test_corrupt_status_is_kept_with_a_timestamped_name(self):
        (self.s1 / "data/status.json").write_text("{broken")
        self.run_night(STUB_NEW_LABELS=1)
        self.assertEqual(len(list((self.s1 / "data").glob("status.json.corrupt-*"))), 1)
        self.assertIn("last_train", self.status())

    def test_teacher_args_reach_label_py(self):
        self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_TEACHER_ARGS="--workers 1 --teacher provider",
                       CORTADOS_PROVIDER_URL="http://example.invalid", CORTADOS_PROVIDER_TOKEN_FILE="/nonexistent")
        self.assertEqual((self.s1 / "data/label-args").read_text(), "--workers 1 --teacher provider")

    def test_no_new_labels_skips_training(self):
        rc, log, run = self.run_night(STUB_NEW_LABELS=5)
        self.assertEqual(rc, 0, log)
        self.assertTrue((run / "report.json").exists(), log)
        rc, log, run = self.run_night(STUB_NEW_LABELS=0)
        self.assertEqual(rc, 0, log)
        self.assertIn("skipped: no new labels", log)
        self.assertFalse((run / "adapter").exists(), "identical data must not be retrained")
        rc, log, run = self.run_night(STUB_NEW_LABELS=2)
        self.assertIn("training tonight", log)
        self.assertTrue((run / "report.json").exists(), log)

    def test_promotion_writes_serving_pointer_with_prev(self):
        old = '{"adapter": "/old/adapter", "base": "b"}\n'
        self.pointer.write_text(old)
        rc, log, run = self.run_night(STUB_NEW_LABELS=4)
        self.assertEqual(rc, 0, log)
        self.assertIn(f"PROMOTED → {self.pointer}", log)
        cur = json.loads(self.pointer.read_text())
        self.assertEqual(cur["adapter"], str((run / "adapter").resolve()))
        self.assertEqual(cur["report"], str((run / "report.json").resolve()))
        self.assertEqual((self.s1 / "current.json.prev").read_text(), old)
        self.assertEqual([p.name for p in self.s1.glob(".current.json*")], [], "no temp file may be left beside the pointer")
        self.assertIn("promotion", " ".join(self.status()))
        # a configured pointer elsewhere (the scorer's checkout) receives the promotion instead
        other = self.tmp / "serving" / "current.json"
        rc, log, run = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_SERVE_POINTER=other)
        self.assertEqual(json.loads(other.read_text())["adapter"], str((run / "adapter").resolve()))

    def test_failed_promotion_is_retried_on_an_unchanged_data_night(self):
        ro = self.tmp / "ro"; ro.mkdir()
        pointer = ro / "current.json"
        ro.chmod(0o500)                                               # the scorer's directory is not writable tonight
        self.addCleanup(ro.chmod, 0o700)
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, CORTADOS_S1_SERVE_POINTER=pointer)
        self.assertEqual(rc, 1, log)
        self.assertIn("PROMOTION FAILED", log)
        self.assertFalse((self.s1 / "data/last-train-labels").exists(), "no marker until the outcome is published")
        ro.chmod(0o700)
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_SERVE_POINTER=pointer)
        self.assertEqual(rc, 0, log)
        self.assertIn("pending candidate runs/2026-01-01 (promotion_failed): re-evaluating", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertEqual(json.loads(pointer.read_text())["adapter"], str((run1 / "adapter").resolve()))
        self.assertFalse((run2 / "adapter").exists(), "publishing the approved run needs no retraining")
        self.assertFalse((self.s1 / "data/pending-candidate").exists())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_SERVE_POINTER=pointer)
        self.assertIn("skipped: no new labels", log)

    def test_deferred_promotion_is_dropped_when_it_no_longer_promotes(self):
        ro = self.tmp / "ro"; ro.mkdir(); pointer = ro / "current.json"
        ro.chmod(0o500); self.addCleanup(ro.chmod, 0o700)
        rc, log, _ = self.run_night(STUB_NEW_LABELS=3, CORTADOS_S1_SERVE_POINTER=pointer)
        self.assertIn("PROMOTION FAILED", log)
        ro.chmod(0o700)
        # same SFT data, but the holdout / incumbent changed: the re-evaluation no longer approves the run
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=0, STUB_PROMOTE=0, CORTADOS_S1_SERVE_POINTER=pointer)
        self.assertEqual(rc, 0, log)
        self.assertIn("pending runs/2026-01-01 no longer promotes", log)
        self.assertFalse(pointer.exists(), "a stale approval must not be published")
        self.assertFalse((self.s1 / "data/pending-candidate").exists())
        self.assertFalse((run2 / "adapter").exists())
        _, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_SERVE_POINTER=pointer)
        self.assertIn("skipped: no new labels", log)

    def test_train_config_values_are_exported_and_not_logged(self):
        (self.s1 / "data/train-config.env").write_text("# provider teacher\nCORTADOS_PROVIDER_URL=http://cfg.invalid:8432\n")
        _, log, _ = self.run_night(STUB_NEW_LABELS=1)
        self.assertEqual((self.s1 / "data/label-env").read_text(), "http://cfg.invalid:8432")
        self.assertIn("train-config sets: CORTADOS_PROVIDER_URL", log)
        self.assertNotIn("cfg.invalid", log)

    def test_holdout_blocked_candidate_waits_and_promotes_after_backfill(self):
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        self.assertEqual(rc, 0, log)
        self.assertIn("holdout too small", log)
        self.assertFalse((self.s1 / "data/last-train-labels").exists(), "a holdout-blocked result is not final")
        self.assertFalse(self.pointer.exists())
        evals = (self.s1 / "data/eval-count").read_text()
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=0, STUB_HOLDOUT_BLOCKED=1)
        self.assertIn("waits for the holdout backfill", log)
        self.assertEqual((self.s1 / "data/eval-count").read_text(), evals, "unchanged holdout: no re-evaluation")
        self.assertFalse((run2 / "adapter").exists())
        (self.s1 / "data/stub-holdout").write_text("h-backfilled")   # holdout labels never touch the SFT data
        rc, log, run3 = self.run_night(STUB_NEW_LABELS=0, STUB_HOLDOUT_BLOCKED=1)
        self.assertEqual(rc, 0, log)
        self.assertIn("pending candidate runs/2026-01-01 (holdout_blocked): re-evaluating", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertEqual(json.loads(self.pointer.read_text())["adapter"], str((run1 / "adapter").resolve()))
        self.assertFalse((run3 / "adapter").exists(), "promoted without retraining")
        self.assertFalse((self.s1 / "data/pending-candidate").exists())
        _, log, _ = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("skipped: no new labels", log)

    def test_lowering_the_holdout_floor_wakes_a_waiting_candidate(self):
        _, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        self.assertIn("holdout too small", log)
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=0, STUB_HOLDOUT_BLOCKED=1, CORTADOS_S1_MIN_HOLDOUT=40)   # only the floor changes
        self.assertEqual(rc, 0, log)
        self.assertIn("pending candidate runs/2026-01-01 (holdout_blocked): re-evaluating", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertFalse((run2 / "adapter").exists())

    def test_force_train_bypasses_a_waiting_candidate(self):
        self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        (self.s1 / "data/force-train").write_text("")
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=0, STUB_HOLDOUT_BLOCKED=1)
        self.assertIn("forced training: pending candidate runs/2026-01-01 (holdout_blocked) superseded", log)
        self.assertNotIn("waits for the holdout backfill", log)
        self.assertTrue((run2 / "adapter").exists(), log)

    def test_claude_backfill_of_codex_ids_counts_as_growth(self):
        codex = [{"id": f"r{i}", "teacher": "gpt-6.1-sol", "sample": 0} for i in range(3)]
        (self.s1 / "data/labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in codex))
        for _ in range(3):
            _, log, _ = self.run_night(STUB_LABEL_RC=2)
        self.assertIn("ALERT", log)
        self.assertEqual(self.status()["last_label_count"], 0, "Codex labels are not usable labels")
        with (self.s1 / "data/labels.jsonl").open("a") as f:          # Claude backfill on the SAME ids
            for r in codex:
                f.write(json.dumps({**r, "teacher": "claude-sonnet-5"}) + "\n")
        self.run_night(STUB_LABEL_RC=2)
        st = self.status()
        self.assertEqual(st["last_label_count"], 3)
        self.assertNotIn("alert", st)

    def test_label_count_is_distinct_claude_triples(self):
        rows = [{"id": "a", "teacher": "claude-sonnet-5", "sample": 0}]
        steps = [
            ([{"id": "a", "teacher": "claude-sonnet-5", "sample": 0}], 1),                       # raw duplicate row: no change
            ([{"id": "a", "teacher": "claude-sonnet-5", "sample": 1}], 2),                       # another sample, same id: +1
            ([{"id": "a", "teacher": "claude-opus-5", "sample": 0}], 3),                         # another Claude teacher, same id: +1
            ([{"id": "a", "teacher": "claude-opus-5", "sample": 0, "ts": 2}], 3),                # same triple, other fields: once
            ([{"id": "a", "teacher": "gpt-6.1-sol", "sample": 2}], 3),                           # Codex: not usable
        ]
        p = self.s1 / "data/labels.jsonl"
        def count():
            out = subprocess.run(["python3", "-B", str(self.s1 / "nightly_state.py"), "labels", str(p)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            return int(out.stdout)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(count(), 1)
        for add, want in steps:
            rows += add
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(count(), want, add)

    def test_unavailable_evaluation_stays_pending_and_recovers(self):
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_UNAVAILABLE=1)
        self.assertEqual(rc, 1, log)
        self.assertIn("EVALUATION UNAVAILABLE for runs/2026-01-01", log)
        self.assertFalse((self.s1 / "data/last-train-labels").exists(), "an unavailable evaluation is not a rejection")
        self.assertIn("evaluation_failed runs/2026-01-01", (self.s1 / "data/pending-candidate").read_text())
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=0)           # bar loads again; data and fingerprints unchanged
        self.assertEqual(rc, 0, log)
        self.assertIn("pending candidate runs/2026-01-01 (evaluation_failed): re-evaluating", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertFalse((run2 / "adapter").exists(), "recovered without retraining")
        _, log, _ = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("skipped: no new labels", log)

    def test_crashed_evaluation_stays_pending_and_recovers(self):
        rc, log, _ = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_RC=1)
        self.assertEqual(rc, 1, log)
        self.assertIn("EVALUATION FAILED for runs/2026-01-01", log)
        self.assertFalse((self.s1 / "data/last-train-labels").exists())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, STUB_EVAL_UNAVAILABLE=1)   # still unavailable: stays pending
        self.assertEqual(rc, 1, log)
        self.assertIn("EVALUATION UNAVAILABLE again for runs/2026-01-01", log)
        rc, log, run3 = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertFalse((run3 / "adapter").exists())

    def test_reruns_on_one_night_count_as_one_stalled_night(self):
        self.run_night(STUB_NEW_LABELS=3)
        same = (date(2026, 1, 1) + timedelta(days=99)).isoformat()
        for _ in range(3):
            _, log, _ = self.run_night(STUB_LABEL_RC=2, CORTADOS_S1_NIGHT_DATE=same)
        self.assertNotIn("ALERT", log, "three runs on one night are one stalled night")
        self.assertEqual(self.status()["nights_without_growth"], 1)
        _, log, _ = self.run_night(STUB_LABEL_RC=2)
        self.assertIn("ALERT: S1 labels have not grown for 2 consecutive nights", log)

    def test_same_night_growth_clears_the_stall(self):
        self.run_night(STUB_NEW_LABELS=3)
        self.run_night(STUB_LABEL_RC=2)
        night = (date(2026, 1, 1) + timedelta(days=50)).isoformat()
        _, log, _ = self.run_night(STUB_LABEL_RC=2, CORTADOS_S1_NIGHT_DATE=night)
        self.assertIn("ALERT", log)
        self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_NIGHT_DATE=night)          # a rerun the same night grows
        st = self.status()
        self.assertNotIn("alert", st)
        self.assertEqual(st["nights_without_growth"], 0)
        _, log, _ = self.run_night(STUB_LABEL_RC=2, CORTADOS_S1_NIGHT_DATE=night)  # that night grew: not a stalled night
        self.assertEqual(self.status()["nights_without_growth"], 0)

    def test_pending_candidate_adapter_survives_retention(self):
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_RC=1)          # crash: adapter, no report.json
        self.assertIn("EVALUATION FAILED for runs/2026-01-01", log)
        self.assertFalse((run1 / "report.json").exists())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, STUB_EVAL_RC=1)
        self.assertNotIn("retention: removed checkpoints of unfinished runs/2026-01-01", log)
        self.assertNotIn("superseded", log)
        self.assertTrue((run1 / "adapter").is_dir(), "the pending candidate's adapter must survive the retention pass")
        rc, log, run3 = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertFalse((run3 / "adapter").exists())

    def test_retention_still_prunes_other_unfinished_runs(self):
        stale = self.s1 / "runs/2025-12-01/adapter"; stale.mkdir(parents=True)
        (stale / "0000100_adapters.safetensors").write_text("w")
        _, log, _ = self.run_night(STUB_NEW_LABELS=1)
        self.assertIn("retention: removed checkpoints of unfinished runs/2025-12-01", log)

    def test_pending_candidate_with_a_missing_adapter_is_dropped_not_superseded(self):
        _, _, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_RC=1)
        shutil.rmtree(run1 / "adapter")
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=1)
        self.assertIn("pending candidate runs/2026-01-01 adapter missing; dropped", log)
        self.assertNotIn("superseded", log)
        self.assertTrue((run2 / "report.json").exists(), log)

    def test_same_early_stop_reason_two_nights_alerts(self):
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, STUB_LABEL_RC=3, STUB_STOP_REASON="unknown_headroom")
        self.assertIn("reason unknown_headroom", log)
        self.assertNotIn("ALERT", log)
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, STUB_LABEL_RC=3, STUB_STOP_REASON="unknown_headroom")
        self.assertIn("ALERT: label_early_stop unknown_headroom on 2 consecutive nights", log)
        self.assertNotIn("alert", self.status(), "labels grew: the growth stall is not what fires here")
        self.assertIn("label_early_stop", self.status()["alerts"])
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, STUB_LABEL_RC=3, STUB_STOP_REASON="low_headroom")   # new reason: restarts
        self.assertNotIn("ALERT", log)
        self.run_night(STUB_NEW_LABELS=1)                                           # a completed pass clears it
        self.assertNotIn("label_early_stop", self.status().get("alerts", {}))
        self.assertNotIn("label_early_stop", self.status().get("streaks", {}))

    def test_repeated_evaluation_failures_alert(self):
        _, log, _ = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_UNAVAILABLE=1)
        self.assertNotIn("ALERT", log)
        _, log, _ = self.run_night(STUB_NEW_LABELS=0, STUB_EVAL_UNAVAILABLE=1)
        self.assertIn("ALERT: evaluation_failed evaluation unavailable or crashed on 2 consecutive nights", log)
        self.assertIn("evaluation_failed", self.status()["alerts"])
        self.run_night(STUB_NEW_LABELS=0)                                           # evaluates again: cleared
        self.assertNotIn("evaluation_failed", self.status().get("alerts", {}))

    def test_holdout_blocked_holds_retraining_below_the_floor_and_alerts(self):
        self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=2, STUB_HOLDOUT_BLOCKED=1)      # new TRAIN labels, holdout still short
        self.assertEqual(rc, 0, log)
        self.assertIn("holdout still 50/200; not retraining", log)
        self.assertFalse((run2 / "adapter").exists(), "no GPU loop while the bar cannot judge any candidate")
        self.assertIn("holdout_blocked runs/2026-01-01", (self.s1 / "data/pending-candidate").read_text())
        self.assertIn("ALERT: holdout_blocked", log)
        self.assertIn("holdout_blocked", self.status()["alerts"])
        # the holdout backfill reaches the floor while train labels also changed: retrain normally, alert cleared on promotion
        (self.s1 / "data/stub-holdout").write_text("h-backfilled")
        rc, log, run3 = self.run_night(STUB_NEW_LABELS=1, STUB_HOLDOUT_BLOCKED=1)
        self.assertEqual(rc, 0, log)
        self.assertIn("superseded by new training data", log)
        self.assertTrue((run3 / "report.json").exists(), log)
        self.assertIn("PROMOTED →", log)
        self.assertNotIn("holdout_blocked", self.status().get("alerts", {}))

    def _count_failure_keeps_candidate(self, **env):
        self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        for night in (1, 2):
            rc, log, run = self.run_night(STUB_NEW_LABELS=2, STUB_HOLDOUT_BLOCKED=1, **env)
            self.assertEqual(rc, 1, log)
            self.assertIn("HOLDOUT COUNT FAILED", log)
            self.assertNotIn("superseded", log)
            self.assertFalse((run / "adapter").exists(), "no training on a failed holdout count")
            self.assertIn("holdout_blocked runs/2026-01-01", (self.s1 / "data/pending-candidate").read_text())
        self.assertIn("ALERT: holdout_count_failed", log)
        self.assertIn("holdout_count_failed", self.status()["alerts"])
        (self.s1 / "data/force-train").write_text("")                              # an explicit override still trains
        rc, log, run = self.run_night(STUB_NEW_LABELS=1, STUB_HOLDOUT_BLOCKED=1, **env)
        self.assertTrue((run / "adapter").exists(), log)

    def test_failed_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_RC=1)

    def test_malformed_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_GARBAGE=1)

    def test_holdout_blocked_force_train_retrains_below_the_floor(self):
        self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        (self.s1 / "data/force-train").write_text("")
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=2, STUB_HOLDOUT_BLOCKED=1)
        self.assertNotIn("not retraining", log)
        self.assertTrue((run2 / "adapter").exists(), log)

    def test_holdout_blocked_alert_clears_on_not_promoted(self):
        self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1)
        self.run_night(STUB_NEW_LABELS=0, STUB_HOLDOUT_BLOCKED=1)
        self.assertIn("holdout_blocked", self.status()["alerts"])
        (self.s1 / "data/stub-holdout").write_text("h-backfilled")
        _, log, _ = self.run_night(STUB_NEW_LABELS=0, STUB_PROMOTE=0)               # re-evaluated: a genuine rejection
        self.assertIn("no longer promotes", log)
        self.assertNotIn("holdout_blocked", self.status().get("alerts", {}))

    def test_malformed_report_takes_the_evaluation_failed_branch(self):
        rc, log, _ = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_GARBAGE=1)
        self.assertEqual(rc, 1, log)
        self.assertIn("EVALUATION UNAVAILABLE for runs/2026-01-01: report.json unreadable or malformed", log)
        self.assertIn("evaluation_failed", (self.s1 / "data/pending-candidate").read_text())
        self.assertFalse((self.s1 / "data/last-train-labels").exists())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)

    def test_not_promoted_leaves_pointer(self):
        self.pointer.write_text('{"adapter": "/old"}')
        rc, log, _ = self.run_night(STUB_NEW_LABELS=4, STUB_PROMOTE=0)
        self.assertEqual(rc, 0, log)
        self.assertEqual(self.pointer.read_text(), '{"adapter": "/old"}')
        self.assertFalse((self.s1 / "current.json.prev").exists())

    def test_stall_alert_after_two_nights(self):
        self.run_night(STUB_NEW_LABELS=3)
        _, log, _ = self.run_night(STUB_NEW_LABELS=0)
        self.assertNotIn("ALERT", log, "one quiet night is not a stall")
        self.assertNotIn("alert", self.status())
        _, log, _ = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("ALERT: S1 labels have not grown for 2 consecutive nights", log)
        st = self.status()
        self.assertIn("ALERT", st["alert"])
        self.assertIn("last_label_growth", st)
        self.assertIn("last_train", st)
        self.run_night(STUB_NEW_LABELS=1)
        self.assertNotIn("alert", self.status(), "growth clears the alert")

    def test_old_logs_rotate(self):
        for i in range(3):
            self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_LOG_KEEP=2)
            log = self.s1 / f"runs/2026-01-{i + 1:02d}/nightly.log"
            os.utime(log, (1_700_000_000 + i, 1_700_000_000 + i))
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_LOG_KEEP=2)
        self.assertIn("log rotation: removed", log)
        self.assertEqual(sorted(p.parent.name for p in self.s1.glob("runs/*/nightly.log")), ["2026-01-03", "2026-01-04"])
        self.assertTrue((self.s1 / "runs/2026-01-01/report.json").exists(), "rotation removes logs only")


class NightlyWithRealLabeler(NightlyEndToEnd):
    """The exit-code contract against the REAL s1/label.py talking to an in-thread fake provider, not a stub."""

    def setUp(self):
        super().setUp()
        from tests.test_s1_teacher import SPENT_503, FakeProvider, answers_for, err
        self.SPENT_503 = SPENT_503
        self.FakeProvider, self.answers_for, self.err = FakeProvider, answers_for, err
        shutil.copy(S1 / "label.py", self.s1 / "label.py")
        reqs = [{"id": f"r{i}", "split": "train", "unit_kind": "interactive", "request": f"task {i}"} for i in range(4)]
        (self.s1 / "data/requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reqs))
        (self.tmp / "tok").write_text("tok-test\n")
        self.env.update(CORTADOS_S1_TEACHER_ARGS="--k 2 --batch 2 --workers 1 --split train",
                        CORTADOS_PROVIDER_TOKEN_FILE=str(self.tmp / "tok"))
        self.fake = None
        self.addCleanup(lambda: self.fake and self.fake.close())

    def test_quota_refusal_takes_the_stopped_early_path(self):
        self.fake = self.FakeProvider([(200, {}, self.answers_for), (503, {}, self.err(self.SPENT_503))]
                                      + [(200, {}, self.answers_for)] * 2)
        rc, log, run = self.run_night(CORTADOS_PROVIDER_URL=self.fake.url)
        self.assertEqual(rc, 0, log)
        self.assertEqual(len(self.fake.seen), 2, "spent quota stops the pass at once")
        self.assertIn("LABELING STOPPED EARLY (label.py rc 3, reason quota)", log)
        self.assertIn("training tonight", log)                    # the first batch's labels are new data
        self.assertIn("last_labeling_stopped_early", self.status())
        self.assertEqual(self.status()["last_label_count"], 2)

    def test_missing_provider_url_takes_the_config_error_path(self):
        rc, log, run = self.run_night()
        self.assertEqual(rc, 3, log)
        self.assertIn("LABELING FAILED (label.py rc 2)", log)
        self.assertFalse((run / "adapter").exists())


# the stub-based scenarios already ran in NightlyEndToEnd; the subclass keeps only its own two tests
for _name in [n for n in vars(NightlyEndToEnd) if n.startswith("test_")]:
    setattr(NightlyWithRealLabeler, _name, None)


class EvaluatePointer(unittest.TestCase):
    def test_evaluate_reads_the_serve_pointer(self):
        import importlib.util, sys
        sys.path.insert(0, str(S1))
        spec = importlib.util.spec_from_file_location("s1_evaluate_ptr", S1 / "evaluate.py")
        ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
        with mock.patch.dict(os.environ, {"CORTADOS_S1_SERVE_POINTER": "/srv/s1/current.json"}):
            self.assertEqual(ev.current_pointer(), Path("/srv/s1/current.json"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_S1_SERVE_POINTER", None)
            self.assertEqual(ev.current_pointer(), S1 / "current.json")
        src = (S1 / "evaluate.py").read_text()
        self.assertNotIn('parent / "current.json").read_text()', src, "every incumbent read goes through current_pointer()")


class EvaluateHoldoutGuards(unittest.TestCase):
    def setUp(self):
        import importlib.util, sys
        sys.path.insert(0, str(S1))
        spec = importlib.util.spec_from_file_location("s1_evaluate_guard", S1 / "evaluate.py")
        self.ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.ev)
        import common
        self.common = common
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp, True)
        self._data = common.DATA; common.DATA = self.tmp
        self.addCleanup(setattr, common, "DATA", self._data)

    def answers(self, steps=1):
        return {"steps": steps, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
                "outward": False, "work_kind": "conversation", "urgency": "now", "needs_clarification": False, "parts": 0, "diversity": False}

    def test_partial_claude_backfill_leaves_too_small_a_holdout_to_promote(self):
        reqs = {f"t{i}": {"id": f"t{i}", "split": "train", "unit_kind": "interactive", "request": "x"} for i in range(30)}
        reqs.update({f"h{i}": {"id": f"h{i}", "split": "holdout", "unit_kind": "interactive", "request": "y"} for i in range(300)})
        rows = [{"id": f"t{i}", "teacher": "claude-sonnet-5", "sample": 0, "answers": self.answers()} for i in range(30)]
        rows += [{"id": f"h{i}", "teacher": "gpt-6.1-sol", "sample": 0, "answers": self.answers()} for i in range(300)]
        rows += [{"id": f"h{i}", "teacher": "claude-opus-5", "sample": 0, "answers": self.answers()} for i in range(50)]  # quota-stopped backfill
        (self.tmp / "labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        with mock.patch("sys.stderr"):
            holdout, valid = self.ev.build_eval_sets(reqs, self.common.read_labels())
        self.assertEqual(len(holdout), 50)
        c = {"n": 50, "band_accuracy": 1.0, "under_routed": 0.0, "outward_recall": 1.0}
        bar = {"band_accuracy": 1.0, "under_routed": 0.0, "outward_recall": 1.0}
        self.assertEqual(self.ev.promote_decision(c, bar, None, None, False, holdout_rows=len(holdout)), (False, False))
        self.assertEqual(self.ev.promote_decision(c, bar, None, None, False, holdout_rows=0)[0], False)   # zero rows: both "tie"
        self.assertTrue(self.ev.promote_decision(c, bar, None, None, False, holdout_rows=self.ev.MIN_TEACHER_HOLDOUT)[0])

    def test_cache_key_follows_the_teacher_labels_not_just_ids(self):
        req = {"id": "h0", "split": "holdout", "unit_kind": "interactive", "request": "y"}
        k1 = self.ev.holdout_key([(req, self.ev.teacher_view([self.answers(1)]))], 0.75, 0.25)
        k2 = self.ev.holdout_key([(req, self.ev.teacher_view([self.answers(3)]))], 0.75, 0.25)   # same id, Codex sample removed
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, self.ev.holdout_key([(req, self.ev.teacher_view([self.answers(1)]))], 0.5, 0.25))
        legacy = __import__("hashlib").sha1(("h0" + "|0.75|0.25").encode()).hexdigest()[:12]
        self.assertNotEqual(k1, legacy, "a legacy id-only cache must miss")
        src = (S1 / "evaluate.py").read_text()
        self.assertNotIn('"|".join(sorted(r["id"] for r, _ in holdout))', src, "every teacher-bar cache uses holdout_key")


class EvaluateHoldoutKeyCli(unittest.TestCase):
    def test_holdout_key_output_includes_the_floor(self):
        import importlib.util, io, sys
        from contextlib import redirect_stdout
        sys.path.insert(0, str(S1))
        spec = importlib.util.spec_from_file_location("s1_evaluate_cli", S1 / "evaluate.py")
        ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
        def key(floor):
            with mock.patch.object(ev, "read_jsonl", return_value=[]), mock.patch.object(ev, "read_labels", return_value=[]), \
                 mock.patch.object(ev, "MIN_TEACHER_HOLDOUT", floor), mock.patch.object(sys, "argv", ["evaluate.py", "--holdout-key"]), \
                 redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ev.main(), 0)
            return out.getvalue().strip()
        self.assertNotEqual(key(200), key(40))
        self.assertTrue(key(200).endswith("-min200"))


class EvaluateUnavailableStatus(unittest.TestCase):
    def run_eval(self, pointer_text):
        import importlib.util, io, sys
        from contextlib import redirect_stdout
        sys.path.insert(0, str(S1))
        spec = importlib.util.spec_from_file_location("s1_evaluate_status", S1 / "evaluate.py")
        ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, True)
        pointer = tmp / "current.json"
        if pointer_text is not None:
            pointer.write_text(pointer_text)
        g = {"n": 0, "band_accuracy": 1.0, "under_routed": 0.0, "over_routed": 0.0, "outward_recall": 1.0}
        with mock.patch.object(ev, "Scorer"), mock.patch.object(ev, "fit_temperatures", return_value={}), \
             mock.patch.object(ev, "grade", return_value=g), mock.patch.object(ev, "outcome_holdout", return_value=[]), \
             mock.patch.object(ev, "read_jsonl", return_value=[]), mock.patch.object(ev, "read_labels", return_value=[]), \
             mock.patch.dict(os.environ, {"CORTADOS_S1_SERVE_POINTER": str(pointer)}), \
             mock.patch.object(sys, "argv", ["evaluate.py", "--base", "b", "--adapter", str(tmp / "cand"), "--out", str(tmp / "out")]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(), 0)
        return json.loads((tmp / "out/report.json").read_text())

    def test_unreadable_incumbent_is_unavailable_not_a_rejection(self):
        r = self.run_eval("{not json")
        self.assertEqual(r["evaluation_status"], "unavailable")
        self.assertIn("bar_error", r["evaluation_unavailable"])
        self.assertFalse(r["promote"])

    def test_no_incumbent_yet_is_a_normal_evaluation(self):
        self.assertEqual(self.run_eval(None)["evaluation_status"], "ok")


class AtomicPointer(unittest.TestCase):
    def test_failed_write_leaves_old_pointer_and_no_temp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("nightly_state", S1 / "nightly_state.py")
        ns = importlib.util.module_from_spec(spec); spec.loader.exec_module(ns)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "current.json"; p.write_text("old")
            with mock.patch.object(ns.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ns.write_atomic(p, "new")
            self.assertEqual(p.read_text(), "old")
            self.assertEqual(sorted(x.name for x in Path(d).iterdir()), ["current.json"])


if __name__ == "__main__":
    unittest.main()
