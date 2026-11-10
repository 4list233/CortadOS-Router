"""s1/nightly.sh end to end on stubs: label failure stops, no new labels skips, promotion reaches the serving pointer
atomically with a .prev, a two-night label stall raises an ALERT, old logs rotate.

The real nightly.sh and nightly_state.py are copied into a temp "s1" next to stub dataset/label/build_sft/evaluate scripts and
a stub training python; HOME is the temp dir (stub launchctl, no ~/.cortados), so nothing real is read, trained or unloaded.
"""
from __future__ import annotations

import fcntl
import json
import os
import time as _time
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
        "if os.environ.get('STUB_LABEL_SLEEP'): __import__('time').sleep(float(os.environ['STUB_LABEL_SLEEP']))   # a long cycle\n"
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
        "if a.holdout_count and os.environ.get('STUB_COUNT_GARBAGE'): print('fifty', floor); raise SystemExit(0)\n"        "if a.holdout_count and os.environ.get('STUB_COUNT_MULTILINE'): print(300, floor); print('garbage'); raise SystemExit(0)\n"
        "if a.holdout_count: print('labels: kept N Claude-teacher rows', file=__import__('sys').stderr)   # like read_labels\n"
        "if a.holdout_count and os.environ.get('STUB_COUNT_NUL'):\n"
        "    __import__('sys').stdout.buffer.write({'1': b'300 200\\x00\\n', '2': b'30\\x000 200\\n'}[os.environ['STUB_COUNT_NUL']]); raise SystemExit(0)\n"
        "if a.holdout_count and os.environ.get('STUB_COUNT_TRAILING'): print(300, floor); print(); raise SystemExit(0)\n"
        "short = os.environ.get('STUB_HOLDOUT_BLOCKED') == '1' and hk == 'h0'   # the holdout is short only in the blocked scenarios\n"
        "if a.holdout_count: print(50 if short else 300, floor); raise SystemExit(0)   # 50 rows until the backfill\n"
        "if os.environ.get('STUB_EVAL_GARBAGE'): open(os.path.join(a.out, 'report.json'), 'w').write('{trunc'); raise SystemExit(0)\n"
        "open('data/eval-count', 'a').write('x')\n"
        "if os.environ.get('STUB_EVAL_TERM'): import signal, time; os.kill(int(open('data/nightly.pid').read()), signal.SIGTERM); time.sleep(1); raise SystemExit(143)\n"
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
# Stub studio gate: nightly tests do not test the gate (test_studio_capacity does), and the real gate would
# query the developer's Ollama. It runs the command, or refuses with 75 for the stage named in STUB_GATE_REFUSE.
if [ "$1 $2 $3 $4" = "-B -m cortados_router.studio_capacity gate" ]; then
  shift 4; [ "$1" = "--" ] && shift
  case "${STUB_GATE_REFUSE:-}:$*" in
    convert:*convert.py*|train:*train.sh*|eval:*evaluate.py*) echo "stub gate refused: $*" >&2; exit 75 ;;
  esac
  exec "$@"
fi
[ "$1" = "-c" ] && exit 0
if [ "$1" = "-m" ]; then
  echo "$*" >> "$HOME/lora.log"
  while [ $# -gt 0 ]; do [ "$1" = "--adapter-path" ] && out="$2"; shift; done
  mkdir -p "$out"; echo w > "$out/0000100_adapters.safetensors"; echo w > "$out/adapters.safetensors"; exit 0
fi
exec python3 "$@"
"""


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class DeadlineCleanup(unittest.TestCase):
    def run_cleanup(self, setup):
        # Exercise the production helper with real Bash jobs; log signaling instead of
        # relying on the OS to recycle a PID (or ever risking an unrelated process).
        source = (S1 / "nightly.sh").read_text()
        helper = source[source.index('deadline=""\nstop_deadline()'):source.index("\ncleanup()")]
        script = helper + """
kill() { printf 'kill:%s\\n' "$*"; }
wait() { printf 'wait:%s\\n' "$*"; builtin wait "$@" 2>/dev/null; }
""" + setup + """
saved=$deadline
stop_deadline
[ -z "$deadline" ] || exit 10
stop_deadline
printf 'saved:%s\\n' "$saved"
"""
        out = subprocess.run(["/bin/bash", "-u", "-c", script],
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.splitlines()

    def test_completed_watcher_is_reaped_without_signaling(self):
        for status in (0, 7):
            with self.subTest(status=status):
                lines = self.run_cleanup(
                    f"(exit {status}) & deadline=$!\n"
                    # A foreground command lets Bash reap the completed watcher,
                    # just as training can continue after the clock watcher exits.
                    "/bin/sleep 0.1\n")
                pid = lines[-1].split(":")[1]
                self.assertEqual(lines, [f"wait:{pid}", f"saved:{pid}"])

    def test_nonchild_pid_is_not_signaled(self):
        lines = self.run_cleanup("deadline=$$\n")
        pid = lines[-1].split(":")[1]
        self.assertEqual(lines, [f"wait:{pid}", f"saved:{pid}"])

    def test_running_watcher_is_signaled_and_reaped(self):
        # The kill spy forwards only in this case, to a known active child.
        lines = self.run_cleanup(
            "kill() { printf 'kill:%s\\n' \"$*\"; builtin kill \"$@\"; }\n"
            "/bin/sleep 30 & deadline=$!\n")
        pid = lines[-1].split(":")[1]
        self.assertEqual(lines, [f"kill:-TERM {pid}", f"wait:{pid}", f"saved:{pid}"])


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
                    "CORTADOS_HOME": str(self.tmp / "cortados-home"),
                    "CORTADOS_S1_EVAL_MINUTES": "0"}   # no wall-clock dependence; the deadline tests set it
        self.night = 0

    def run_night(self, **env):
        self.night += 1
        run = f"runs/2026-01-{self.night:02d}"   # production-style: the runs/20*/ globs (retention, resume) run here
        out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh")], cwd=self.tmp,
                             env={**self.env, "CORTADOS_S1_RUN": run,
                                  # Pin every deadline clock; permit explicit matrix runs without consulting local time.
                                  "CORTADOS_S1_NOW": os.environ.get("CORTADOS_S1_NOW", "0300"),
                                  "CORTADOS_S1_NIGHT_DATE": (date(2026, 1, 1) + timedelta(days=self.night)).isoformat(),
                                  **{k: str(v) for k, v in env.items()}},
                             capture_output=True, text=True, timeout=60)
        log = (self.s1 / run / "nightly.log").read_text()
        return out.returncode, log, self.s1 / run

    def test_laya_looping_clock_returns_promptly_and_reaps_watcher(self):
        (self.s1 / "laya").mkdir()
        (self.s1 / ".venv-laya/bin").mkdir(parents=True)
        _exe(self.s1 / ".venv-laya/bin/python", "#!/bin/bash\nexit 0\n")
        # Skip only Laya's unrelated 20-second GPU settle; keep the real watcher sleep to expose leaked timers.
        _exe(Path(self.env["HOME"]) / ".local/bin/sleep",
             '#!/bin/bash\n[ "$1" = 20 ] && exit 0\n'
             '[ "$1" != 60 ] || echo "$$ $PPID" > "$HOME/watcher-pids"\nexec /bin/sleep "$@"\n')
        _exe(self.s1 / "laya/train.sh", """#!/bin/bash
python3 - <<'PY'
import os, signal, time
from pathlib import Path
marker = Path(os.environ["HOME"]) / "watcher-pids"
end = time.monotonic() + 2
while not marker.exists() and time.monotonic() < end:
    time.sleep(0.01)
assert marker.exists(), "watcher must enter its sleeping branch"
if os.environ.get("STUB_LAYA_TERM") == "1":
    os.kill(int(Path("../data/nightly.pid").read_text()), signal.SIGTERM)
PY
""")
        marker = Path(self.env["HOME"]) / "watcher-pids"

        def remove_timers():
            # Clean up even a regressed implementation, so a failed assertion cannot leak a 60-second sleeper.
            if marker.exists():
                for pid in map(int, marker.read_text().split()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # Already reaped is the expected successful path.
        self.addCleanup(remove_timers)
        # Both sides of midnight loop; normal completion and TERM must both own the watcher's lifetime.
        for now in ("1300", "0300"):
            for terminate in ("0", "1"):
                with self.subTest(now=now, terminate=terminate):
                    remove_timers()  # Subtests continue after failure; clear the previous timer before replacing its PID record.
                    marker.unlink(missing_ok=True)
                    (self.s1 / "data/laya-tonight").touch()
                    start = time.monotonic()
                    rc, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_NOW=now,
                                                STUB_LAYA_TERM=terminate)
                    self.assertLess(time.monotonic() - start, 5, "watcher must not delay pipe EOF or EXIT cleanup")
                    self.assertEqual(rc, 130 if terminate == "1" else 0, log)
                    self.assertTrue(marker.exists(), log)
                    for pid in map(int, marker.read_text().split()):
                        with self.assertRaises(ProcessLookupError, msg=f"timer {pid} survived nightly exit"):
                            os.kill(pid, 0)

    def test_laya_gate_refusal_at_each_stage_fails_the_night(self):
        (self.s1 / "laya").mkdir()
        (self.s1 / ".venv-laya/bin").mkdir(parents=True)
        _exe(self.s1 / ".venv-laya/bin/python", "#!/bin/bash\necho \"venv $*\" >> \"$HOME/laya.log\"\nexit 0\n")
        _exe(Path(self.env["HOME"]) / ".local/bin/sleep", '#!/bin/bash\n[ "$1" = 20 ] && exit 0\nexec /bin/sleep "$@"\n')
        _exe(self.s1 / "laya/train.sh", '#!/bin/bash\necho train >> "$HOME/laya.log"\n'
                                        'mkdir -p "$LAYA_RUN" && touch "$LAYA_RUN/model.safetensors"\n')
        log_file = Path(self.env["HOME"]) / "laya.log"
        for stage, ran, kept in (("convert", [], True), ("train", ["venv convert.py"], True),
                                 ("eval", ["venv convert.py", "train"], False)):
            with self.subTest(stage=stage):
                log_file.unlink(missing_ok=True)
                (self.s1 / "data/laya-tonight").touch()
                (self.s1 / "data/laya-init").write_text("laya/runs/old/checkpoint_latest\n")
                rc, log, _ = self.run_night(STUB_NEW_LABELS=1, STUB_GATE_REFUSE=stage, CORTADOS_S1_NOW="0700")
                self.assertEqual(rc, 1, log)
                self.assertIn("studio gate refused laya %s" % stage, log)
                lines = log_file.read_text().split("\n")[:-1] if log_file.exists() else []
                self.assertEqual(lines, ran, log)
                self.assertEqual((self.s1 / "data/laya-tonight").exists(), kept, log)
                self.assertEqual((self.s1 / "data/laya-init").exists(), kept, log)
                (self.s1 / "data/laya-tonight").unlink(missing_ok=True)
                (self.s1 / "data/laya-init").unlink(missing_ok=True)

    def test_laya_refusal_flags_survive_term_during_settle(self):
        # Review r3: the refused stage's one-shot flags must be back on disk before the post-refusal settle delay,
        # because a supervisor TERM there runs the exit trap, which does not restore them.
        (self.s1 / "laya").mkdir()
        (self.s1 / ".venv-laya/bin").mkdir(parents=True)
        _exe(self.s1 / ".venv-laya/bin/python", "#!/bin/bash\nexit 0\n")
        _exe(Path(self.env["HOME"]) / ".local/bin/sleep",
             '#!/bin/bash\nif [ "$1" = 20 ]; then kill -TERM "$(cat "$PWD/data/nightly.pid")"; exit 0; fi\nexec /bin/sleep "$@"\n')
        _exe(self.s1 / "laya/train.sh", '#!/bin/bash\nmkdir -p "$LAYA_RUN" && touch "$LAYA_RUN/model.safetensors"\n')
        for stage in ("convert", "train"):
            with self.subTest(stage=stage):
                (self.s1 / "data/laya-tonight").touch()
                (self.s1 / "data/laya-init").write_text("laya/runs/old/checkpoint_latest\n")
                rc, log, _ = self.run_night(STUB_NEW_LABELS=1, STUB_GATE_REFUSE=stage, CORTADOS_S1_NOW="0700")
                self.assertEqual(rc, 130, log)
                self.assertIn("stop requested", log)
                self.assertTrue((self.s1 / "data/laya-tonight").exists(), log)
                self.assertEqual((self.s1 / "data/laya-init").read_text(), "laya/runs/old/checkpoint_latest\n", log)
                (self.s1 / "data/laya-tonight").unlink(missing_ok=True)
                (self.s1 / "data/laya-init").unlink(missing_ok=True)
                (self.s1 / "data/nightly.stop").unlink(missing_ok=True)

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
        self.assertEqual((self.s1 / "data/label-args").read_text(), "--min-left-pct 15 --workers 1 --teacher provider")

    # Continuous mode (overnight.py --continuous): "worked" in CORTADOS_S1_CYCLE_RESULT only for real work; absence = idle.
    def _cycle(self, **env):
        rf = self.tmp / "cycle-result"
        rf.unlink(missing_ok=True)
        rc, log, run = self.run_night(CORTADOS_S1_CONTINUOUS=1, CORTADOS_S1_CYCLE_RESULT=rf, **env)
        return rc, log, run, (rf.read_text().strip() if rf.exists() else None)

    def test_cycle_result_worked_on_training_and_absent_when_idle(self):
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=5)
        self.assertEqual((rc, res), (0, "worked"), log)
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=0)
        self.assertIn("skipped: no new labels", log)
        self.assertEqual((rc, res), (0, None), log)

    def test_outside_night_window_labels_but_skips_every_gated_step(self):
        # Forest 2026-11-08: overnight.py sets CORTADOS_S1_TRAIN_ALLOWED=0 outside the window; labeling still counts as work.
        reason = "training skipped: outside night window 01:00-07:00; next window starts 2026-11-09 01:00 EST"
        rc, log, run, res = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=0, CORTADOS_S1_TRAIN_SKIP_REASON=reason)
        self.assertEqual((rc, res), (0, "worked"), log)
        self.assertIn(reason, log)
        self.assertNotIn("training tonight", log)
        self.assertNotIn("studio gate", log)
        self.assertFalse((self.s1 / run / "adapter").exists(), log)
        rc, log, run, res = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=1)
        self.assertEqual(rc, 0, log)
        self.assertIn("training tonight", log)

    def _stopped_mid_lora(self, name, age_s=0):
        """A run as overnight.py leaves it when the night window ends mid-LoRA: checkpoint 0000100, segment-target, no final
        adapters.safetensors, no report.json."""
        run = self.s1 / "runs" / name
        (run / "adapter").mkdir(parents=True)
        (run / "adapter/0000100_adapters.safetensors").write_text("w")
        (run / "segment-target").write_text("300\n")
        if age_s:
            old = _time.time() - age_s
            for p in (run / "adapter/0000100_adapters.safetensors", run / "adapter", run):
                os.utime(p, (old, old))
        return run

    def test_continuous_cross_night_resume_keeps_and_resumes_the_stopped_run(self):
        run = self._stopped_mid_lora("2026-01-01")   # night 1; the cycle below runs on the real (later) date
        out = self._bare(CORTADOS_S1_CONTINUOUS=1, STUB_NEW_LABELS=5)
        log = (run / "nightly.log").read_text()
        self.assertEqual(out.returncode, 0, log)
        self.assertIn("cross-night resume", log)
        self.assertNotIn("retention: removed", log)
        lora = (Path(self.env["HOME"]) / "lora.log").read_text()
        self.assertIn(f"--resume-adapter-file runs/2026-01-01/adapter/resume_100_adapters.safetensors", lora)
        self.assertIn("--iters 200", lora)   # 300 target - 100 checkpointed
        self.assertTrue((run / "adapter/resume_100_adapters.safetensors").exists())

    def test_cross_night_resume_takes_only_the_newest_unfinished_run(self):
        # Rule (unchanged retention): only the newest unfinished run is resumed, with no age limit; any older unfinished,
        # unpromoted, non-pending run loses its checkpoints (logs kept) in prune_unresumable_runs.
        old = self._stopped_mid_lora("2025-06-01", age_s=200 * 86400)
        new = self._stopped_mid_lora("2026-01-01")
        out = self._bare(CORTADOS_S1_CONTINUOUS=1, STUB_NEW_LABELS=5)
        log = (new / "nightly.log").read_text()
        self.assertEqual(out.returncode, 0, log)
        self.assertIn("retention: removed checkpoints of unfinished runs/2025-06-01", log)
        self.assertFalse((old / "adapter").exists())
        self.assertIn("--resume-adapter-file runs/2026-01-01/", (Path(self.env["HOME"]) / "lora.log").read_text())

    def _completed_unevaluated(self, name):
        """A run whose LoRA finished (final adapters.safetensors) but was never evaluated: the recovery path's, not resumable."""
        run = self._stopped_mid_lora(name)
        (run / "adapter/adapters.safetensors").write_text("final")
        return run

    def test_completed_unevaluated_run_does_not_hide_an_older_interrupted_run(self):
        # Review P2-b: newest_unfinished_run used to return the newer completed run, the carry rejected it, the older interrupted
        # run was not carried, and retention deleted its checkpoints. Both must survive and the older one is resumed.
        old = self._stopped_mid_lora("2025-12-30", age_s=2 * 86400)
        done = self._completed_unevaluated("2025-12-31")
        out = self._bare(CORTADOS_S1_CONTINUOUS=1, STUB_NEW_LABELS=5)
        log = (old / "nightly.log").read_text() if (old / "nightly.log").exists() else out.stdout + out.stderr
        self.assertIn("cross-night resume", log)
        self.assertIn("continuing the unfinished run runs/2025-12-30", log)
        self.assertNotIn("retention: removed checkpoints of unfinished runs/2025-12-30", log)
        self.assertNotIn("retention: removed checkpoints of unfinished runs/2025-12-31", log)
        self.assertTrue((old / "adapter/0000100_adapters.safetensors").exists(), log)
        self.assertTrue((done / "adapter/adapters.safetensors").exists(), log)

    def test_daytime_label_only_cycle_keeps_the_interrupted_runs_checkpoints(self):
        old = self._stopped_mid_lora("2025-12-30", age_s=2 * 86400)
        done = self._completed_unevaluated("2025-12-31")
        reason = "training skipped: outside night window 01:00-07:00"
        out = self._bare(CORTADOS_S1_CONTINUOUS=1, STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=0,
                         CORTADOS_S1_TRAIN_SKIP_REASON=reason)
        log = (old / "nightly.log").read_text() if (old / "nightly.log").exists() else out.stdout + out.stderr
        self.assertNotIn("retention: removed", log)
        self.assertTrue((old / "adapter/0000100_adapters.safetensors").exists(), log)
        self.assertTrue((done / "adapter/adapters.safetensors").exists(), log)

    def test_window_mode_does_not_carry_runs_across_nights(self):
        run = self._stopped_mid_lora("2026-01-01")
        self._bare(STUB_NEW_LABELS=5)
        self.assertFalse((run / "adapter").exists(), "window mode keeps its old rule: only tonight's run resumes")

    def test_cycle_result_absent_on_label_failure(self):
        rc, log, _, res = self._cycle(STUB_LABEL_RC=1)
        self.assertNotEqual(rc, 0, log)
        self.assertIsNone(res, log)

    def test_continuous_gpu_alone_keeps_the_scorer_loaded(self):
        rc, log, run, _ = self._cycle(STUB_NEW_LABELS=5)
        self.assertEqual(rc, 0, log)
        self.assertIn("continuous mode: scorer stays up during training", log)
        self.assertTrue((run / "adapter").exists(), log)
        calls = self.tmp / "home/launchctl.log"
        self.assertNotIn("unload", calls.read_text() if calls.exists() else "")

    def test_cycle_result_quota_wins_over_worked(self):
        for reason in ("low_headroom", "unknown_headroom"):
            rc, log, run, res = self._cycle(STUB_LABEL_RC=3, STUB_STOP_REASON=reason, STUB_NEW_LABELS=3)
            self.assertEqual((rc, res), (0, "quota"), log)   # labels were added and training ran, but the floor was hit
            self.assertTrue((run / "adapter").exists(), log)
        rc, log, _, res = self._cycle(STUB_LABEL_RC=3, STUB_STOP_REASON="retries", STUB_NEW_LABELS=3)
        self.assertEqual((rc, res), (0, "worked"), "a non-headroom early stop is ordinary work")

    def _bare(self, *args, **env):
        return subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh"), *args], cwd=self.tmp,
                              env={**self.env, "CORTADOS_S1_NOW": os.environ.get("CORTADOS_S1_NOW", "0300"),
                                   **{k: str(v) for k, v in env.items()}}, capture_output=True, text=True, timeout=60)

    def test_continuous_stop_flag_is_kept_and_refuses_the_cycle(self):
        flag = self.s1 / "data/nightly.stop"; flag.touch()
        out = self._bare(CORTADOS_S1_CONTINUOUS=1, CORTADOS_S1_RUN="runs/2026-01-01", CORTADOS_S1_NIGHT_DATE="2026-01-02")
        self.assertEqual(out.returncode, 130, out.stdout + out.stderr)
        self.assertTrue(flag.exists(), "continuous mode never clears the stop flag")
        self.assertFalse((self.s1 / "data/ran-dataset").exists())
        self.assertFalse((self.s1 / "data/nightly.pid").exists())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0)   # window mode: a new night clears yesterday's stop
        self.assertEqual(rc, 0, log)
        self.assertFalse(flag.exists())

    def test_stop_continues_a_sigstopped_run_so_it_gets_its_grace_period(self):
        mark = self.tmp / "termed"
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal, sys, time\n"
                                  f"signal.signal(signal.SIGTERM, lambda *a: (open({str(mark)!r}, 'w').write('x'), sys.exit(0)))\n"
                                  "time.sleep(120)\n"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        time.sleep(0.5)
        (self.s1 / "data/nightly.pid").write_text(f"{child.pid}\n")
        os.kill(child.pid, signal.SIGSTOP)
        out = self._bare("--stop")
        self.assertIn("stopped nightly", out.stdout)
        child.wait(timeout=10)
        self.assertTrue(mark.exists(), "the stopped run handled TERM (it was continued), not SIGKILLed")
        self.assertTrue((self.s1 / "data/nightly.stop").exists())

    def test_stop_continues_a_stopped_grandchild_through_the_group(self):
        # The supervisor's layout: the run (nightly.sh) leads its own group (start_new_session), a wrapper is its child, and
        # the SIGSTOPped trainer is the wrapper's child. `pkill -P <run>` reaches only the wrapper; only the group CONT
        # reaches the trainer. The trainer marks "early" if it resumed while the run was still handling TERM (--stop
        # continued it) and "late" if it resumed only after the run exited (the kernel CONTs an orphaned stopped group).
        mark, ready, exiting = self.tmp / "trainer-resumed", self.tmp / "trainer-ready", self.tmp / "run-exiting"
        trainer = ("import os, signal\n"
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                   f"open({str(ready)!r}, 'w').write(str(os.getpid()))\n"
                   "os.kill(os.getpid(), signal.SIGSTOP)\n"
                   f"open({str(mark)!r}, 'w').write('late' if os.path.exists({str(exiting)!r}) else 'early')\n")
        wrapper = ("import signal, subprocess, sys\n"
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                   f"subprocess.Popen([sys.executable, '-c', {trainer!r}]).wait()\n")
        run = ("import os, signal, subprocess, sys, time\n"
               f"subprocess.Popen([sys.executable, '-c', {wrapper!r}])\n"
               "def term(*a):\n"
               "    t = time.monotonic() + 5\n"   # give the trainer time to act, as nightly.sh's trap waits for its children
               f"    while not os.path.exists({str(mark)!r}) and time.monotonic() < t: time.sleep(0.05)\n"
               f"    open({str(exiting)!r}, 'w').close(); sys.exit(0)\n"
               "signal.signal(signal.SIGTERM, term)\n"
               "time.sleep(120)\n")
        child = subprocess.Popen([sys.executable, "-c", run], start_new_session=True)
        threading.Thread(target=child.wait, daemon=True).start()   # reap at once: a zombie run would hold --stop's 20 s wait
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.addCleanup(lambda: subprocess.run(["kill", "-KILL", "--", f"-{child.pid}"], capture_output=True))
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "trainer started")
        tpid = ready.read_text().strip()
        st = ""
        deadline = time.monotonic() + 5
        while not st.startswith("T") and time.monotonic() < deadline:
            st = subprocess.run(["ps", "-o", "stat=", "-p", tpid], capture_output=True, text=True).stdout.strip()
            time.sleep(0.05)
        self.assertTrue(st.startswith("T"), f"trainer is stopped: {st!r}")
        (self.s1 / "data/nightly.pid").write_text(f"{child.pid}\n")
        out = self._bare("--stop")
        self.assertIn("stopped nightly", out.stdout)
        deadline = time.monotonic() + 5
        while not mark.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(mark.read_text() if mark.exists() else "never", "early",
                         "--stop's group CONT resumed the stopped grandchild while the run was still alive")

    def _supervisor(self, scorer_port, **env):
        """overnight.py --continuous over the real nightly.sh in the temp s1; its probe hits the test's stub scorer, never :8431."""
        shutil.copy(S1 / "overnight.py", self.s1 / "overnight.py")
        out = open(self.tmp / f"sup-{time.monotonic_ns()}.log", "w+")
        self.addCleanup(out.close)
        e = {k: v for k, v in self.env.items() if k != "CORTADOS_NIGHTLY_SUPERVISED"}
        e.update({"CORTADOS_S1_URL": f"http://127.0.0.1:{scorer_port}", "CORTADOS_S1_MIN_FREE_GB": "0",
                  "CORTADOS_S1_CYCLE_IDLE_S": "60", **{k: str(v) for k, v in env.items()}})
        p = subprocess.Popen([sys.executable, "-B", str(self.s1 / "overnight.py"), "--continuous", "--", "/bin/bash",
                              str(self.s1 / "nightly.sh")], cwd=self.tmp, env=e, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        return p, out

    def _wait_for(self, what, cond, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def test_supervisor_term_leaves_no_stop_flag_and_stop_still_prevents_relaunch(self):
        import http.server, threading
        class Scorer(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Scorer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        port, flag, args = srv.server_port, self.s1 / "data/nightly.stop", self.s1 / "data/label-args"
        text = lambda f: (f.flush(), f.seek(0), f.read())[2]
        # 1. TERM the supervisor mid-cycle (launchctl unload / kickstart -k / logout / shutdown): no stop flag is left behind
        sup, out = self._supervisor(port, STUB_LABEL_SLEEP=60)
        self._wait_for("the first cycle to start labelling", args.exists)
        sup.send_signal(signal.SIGTERM); sup.wait(timeout=40)
        self.assertFalse(flag.exists(), "a supervisor TERM must not look like nightly.sh --stop: " + text(out))
        self.assertFalse((self.s1 / "data/nightly.pid").exists(), text(out))
        # 2. restarted (launchd KeepAlive / the next boot), it runs a cycle
        args.unlink()
        sup, out = self._supervisor(port, STUB_NEW_LABELS=0)
        self._wait_for("a cycle after the restart", lambda: "cycle 1 ended rc 0" in text(out))
        self.assertTrue(args.exists(), text(out))
        sup.send_signal(signal.SIGTERM); sup.wait(timeout=40)
        self.assertFalse(flag.exists(), text(out))
        # 3. nightly.sh --stop during a cycle still flags the stop, and a restarted supervisor does not relaunch
        args.unlink()
        sup, out = self._supervisor(port, STUB_LABEL_SLEEP=60)
        self._wait_for("the cycle to start labelling", args.exists)
        stop = self._bare("--stop")
        self.assertIn("stopped nightly", stop.stdout, stop.stdout + stop.stderr)
        self.assertEqual(sup.wait(timeout=40), 130, text(out))
        self.assertTrue(flag.exists())
        args.unlink()
        sup, out = self._supervisor(port)
        self.assertEqual(sup.wait(timeout=40), 0, text(out))
        self.assertIn("stop flag", text(out))
        self.assertFalse(args.exists(), "no cycle runs while the stop flag is there")

    def test_continuous_cleanup_does_not_reload_the_scorer(self):
        rc, log, _, _ = self._cycle(STUB_NEW_LABELS=0)
        self.assertEqual(rc, 0, log)
        self.assertNotIn("scorer back", log)
        calls = self.tmp / "home/launchctl.log"
        self.assertNotIn("load", calls.read_text() if calls.exists() else "")
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0)   # window mode still restores the scorer on exit
        self.assertIn("launchctl load", calls.read_text())

    def test_min_left_pct_is_validated(self):
        for value, want, note in (("40", "40", None), ("101", "15", "above 100"), ("abc", "15", "not a whole number"),
                                  ("007", "7", None)):
            rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_MIN_LEFT_PCT=value)
            self.assertEqual(rc, 0, log)
            self.assertTrue((self.s1 / "data/label-args").read_text().startswith(f"--min-left-pct {want} "), value)
            if note:
                self.assertIn("MIN_LEFT_PCT", log); self.assertIn(note, log)

    def test_future_dated_bench_stamp_is_due_and_a_failed_stamp_is_logged(self):
        repo = self.tmp / "router"; (repo / "bin").mkdir(parents=True)
        _exe(repo / "bin/refresh-tiers.sh", "#!/bin/bash\necho run >> \"$HOME/refresh.log\"\n")
        calls = Path(self.env["HOME"]) / "refresh.log"
        cont = {"CORTADOS_ROUTER_REPO": str(repo), "CORTADOS_S1_CONTINUOUS": "1"}
        (self.s1 / "data/last-bench-refresh").write_text(f"{int(time.time()) + 10**6}\n")
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, **cont)
        self.assertEqual(calls.read_text().count("run"), 1, log)
        (self.s1 / "data/last-bench-refresh").unlink()
        (self.s1 / "data/last-bench-refresh").mkdir()   # the stamp cannot be written
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, **cont)
        self.assertEqual(rc, 0, log)
        self.assertIn("WARNING: could not write data/last-bench-refresh", log)

    def test_continuous_eval_time_ok_always_evaluates(self):
        rc, log, run, _ = self._cycle(STUB_NEW_LABELS=3, CORTADOS_S1_EVAL_MINUTES=1440)   # window mode: never enough time
        self.assertEqual(rc, 0, log)
        self.assertNotIn("not evaluating", log)
        self.assertTrue((run / "report.json").exists(), log)

    # --- calibration sweep request (s1/calibrate.py): FIRST in an in-window cycle (#74; more in tests/test_s1_sweep_first.py) ---
    CALIBRATE_STUB = (
        "import sys\n"
        "open('data/sweep-count', 'a').write('x')\n"
        "open('data/sweep-args', 'w').write(' '.join(sys.argv[1:]))\n"
        "print('sweep: 4 settings; chosen on teacher: cand-t0.1; would promote: False')\n"
    )

    def _sweep_request(self):
        (self.s1 / "calibrate.py").write_text(self.CALIBRATE_STUB)
        req = self.s1 / "data/sweep-request.json"
        req.write_text(json.dumps({"candidate": "runs/2026-11-08/adapter", "live": "runs/2026-10-16-0130/adapter",
                                   "tilts": [0, 0.1], "alphas": [1.0, 0.7]}))
        return req

    def test_sweep_runs_once_first_inside_window(self):
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5)                  # first night trains; no request yet
        self.assertEqual(rc, 0, log)
        req = self._sweep_request()
        (self.s1 / "data/label-args").unlink()
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_TRAIN_ALLOWED=1)
        self.assertEqual(rc, 0, log)
        self.assertIn("the sweep runs first", log)
        self.assertFalse((self.s1 / "data/label-args").exists(), "the sweep cycle makes no labeling calls: " + log)
        self.assertEqual((self.s1 / "data/sweep-count").read_text(), "x", log)
        self.assertIn("--out runs/2026-11-08/sweep.json --tilts 0 0.1 --alphas 1.0 0.7 --scales", (self.s1 / "data/sweep-args").read_text())
        self.assertIn("sweep: 4 settings", log)
        self.assertFalse(req.exists())
        self.assertEqual(len(list((self.s1 / "data").glob("sweep-request.done-*.json"))), 1)
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0)                  # request consumed: no second sweep
        self.assertEqual((self.s1 / "data/sweep-count").read_text(), "x", log)
        self.assertIn("skipped: no new labels", log)

    def test_sweep_never_runs_when_training_not_allowed(self):
        req = self._sweep_request()
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=0)
        self.assertEqual(rc, 0, log)
        self.assertFalse((self.s1 / "data/sweep-count").exists(), log)
        self.assertTrue(req.exists())
        self.assertTrue((self.s1 / "data/label-args").exists(), "outside the window the cycle labels as usual")

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

    def test_term_during_evaluation_keeps_the_run_pending_and_next_night_evaluates_it(self):
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_TERM=1)    # 07:00 TERM mid-evaluation
        self.assertEqual(rc, 130, log)
        self.assertIn("stop requested", log)
        self.assertFalse((run1 / "report.json").exists())
        self.assertIn("evaluation_pending runs/2026-01-01", (self.s1 / "data/pending-candidate").read_text())
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=2)                      # new labels do NOT supersede it
        self.assertEqual(rc, 0, log)
        self.assertNotIn("retention: removed checkpoints of unfinished runs/2026-01-01", log)
        self.assertNotIn("superseded", log)
        self.assertIn("pending candidate runs/2026-01-01 (evaluation_pending): re-evaluating", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertFalse((run2 / "adapter").exists(), "evaluated, not retrained")
        self.assertFalse((self.s1 / "data/last-train-labels").exists(), "the newer labels were never trained on")
        _, log, run3 = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("training tonight", log)

    def test_marker_is_written_before_evaluation_starts(self):
        evaluate = self.s1 / "evaluate.py"
        evaluate.write_text("import os, sys\nif '--holdout-key' not in sys.argv and '--holdout-count' not in sys.argv:\n"
                            "    open('data/marker-at-eval', 'w').write(open('data/pending-candidate').read())\n" + evaluate.read_text())
        self.run_night(STUB_NEW_LABELS=3)
        self.assertIn("evaluation_pending runs/2026-01-01", (self.s1 / "data/marker-at-eval").read_text())
        self.assertFalse((self.s1 / "data/pending-candidate").exists(), "a final verdict clears the marker")

    def test_not_enough_time_skips_evaluation_and_leaves_it_pending(self):
        late = {"CORTADOS_S1_EVAL_MINUTES": 110, "CORTADOS_S1_NOW": "0659"}   # 1 min to 07:00: never enough time
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, **late)
        self.assertEqual(rc, 0, log)
        self.assertIn("not evaluating runs/2026-01-01: 1 min to the 0700 deadline", log)
        self.assertIn("it stays pending (evaluation_pending)", log)
        self.assertNotIn("ALERT", log)
        self.assertFalse((self.s1 / "data/eval-count").exists(), "evaluation not started")
        self.assertIn("evaluation_pending runs/2026-01-01", (self.s1 / "data/pending-candidate").read_text())
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=1, **late)
        self.assertIn("not evaluating runs/2026-01-01", log)
        self.assertIn("ALERT", log)
        self.assertIn("evaluation deferred: not enough time before the deadline", log)
        self.assertFalse((run2 / "adapter").exists(), "no new training while the candidate waits")
        self.assertTrue((run1 / "adapter").is_dir())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertNotIn("ALERT", log)

    def test_minutes_left_table(self):
        cases = {("0130", "0700"): 330, ("0245", "0700"): 255, ("0619", "0700"): 41, ("0659", "0700"): 1,
                 ("2200", "0700"): 540, ("0000", "0700"): 420, ("0830", "0700"): 1350, ("0959", "0700"): 1261,
                 ("0130", "0030"): 1380, ("0245", "0030"): 1305, ("0619", "0030"): 1091, ("0659", "0030"): 1051,
                 ("2200", "0030"): 150, ("0000", "0030"): 30, ("0830", "0030"): 960, ("0959", "0030"): 871}
        for (now, dl), want in cases.items():
            out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh"), "--minutes-left", now], cwd=self.tmp,
                                 env={**self.env, "CORTADOS_S1_DEADLINE": dl}, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.split("\n")[0], str(want), (now, dl, out.stdout, out.stderr))
        for bad in ("2400", "9z9z"):
            out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh"), "--minutes-left", "0130"], cwd=self.tmp,
                                 env={**self.env, "CORTADOS_S1_DEADLINE": bad}, capture_output=True, text=True, timeout=30)
            self.assertIn(f"CORTADOS_S1_DEADLINE={bad} is not HHMM; using 0700", out.stdout)
            self.assertTrue(out.stdout.startswith("330\n"), out.stdout)
        out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh"), "--minutes-left", "0130"], cwd=self.tmp,
                             env={**self.env, "CORTADOS_S1_EVAL_MINUTES": "300"}, capture_output=True, text=True, timeout=30)
        self.assertIn("EVAL_MINUTES=300 can never fit", out.stdout)

    def minutes_left(self, *args, **env):
        return subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh"), "--minutes-left", *args], cwd=self.tmp,
                              env={**self.env, **{k: str(v) for k, v in env.items()}}, capture_output=True, text=True, timeout=30)

    def test_eval_minutes_ceiling_follows_the_deadline(self):
        out = self.minutes_left("0130", CORTADOS_S1_DEADLINE="1200", CORTADOS_S1_EVAL_MINUTES="400")
        self.assertEqual(out.stdout, "630\n", out.stdout + out.stderr)     # 400 fits a 01:30-12:00 window: no note
        out = self.minutes_left("0130", CORTADOS_S1_DEADLINE="1200", CORTADOS_S1_EVAL_MINUTES="600")
        self.assertIn("EVAL_MINUTES=600 can never fit the 01:30-1200 window (>= 600); using 110", out.stdout)

    def test_window_too_short_for_the_fallback_disables_the_deadline_check(self):
        # DEADLINE=0300: the 01:30-03:00 window is 90 min, too short for the configured value AND the 110 fallback
        out = self.minutes_left("0130", CORTADOS_S1_DEADLINE="0300", CORTADOS_S1_EVAL_MINUTES="200")
        self.assertTrue(out.stdout.startswith("90\n"), out.stdout + out.stderr)
        self.assertIn("using 110", out.stdout)
        self.assertIn("too short even for EVAL_MINUTES=110; deadline check disabled (0)", out.stdout)
        out = self.minutes_left("0130", CORTADOS_S1_DEADLINE="0300", CORTADOS_S1_EVAL_MINUTES="110")
        self.assertIn("deadline check disabled (0)", out.stdout)
        out = self.minutes_left("0130", CORTADOS_S1_DEADLINE="0700", CORTADOS_S1_EVAL_MINUTES="110")
        self.assertEqual(out.stdout, "330\n", out.stdout)                    # a window that fits 110: no note

    def test_valid_now_override_is_always_logged(self):
        out = self.minutes_left(CORTADOS_S1_NOW="0619")
        self.assertTrue(out.stdout.startswith("41\n"), out.stdout)
        self.assertIn("CORTADOS_S1_NOW=0619 replaces the clock for the deadline check (test hook)", out.stdout)
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_NOW="0619")
        self.assertIn("CORTADOS_S1_NOW=0619 replaces the clock", log)
        out = self.minutes_left("0130")
        self.assertNotIn("CORTADOS_S1_NOW", out.stdout)

    def test_continuous_mode_refreshes_benchmarks_at_most_once_per_interval(self):
        repo = self.tmp / "router"; (repo / "bin").mkdir(parents=True)
        _exe(repo / "bin/refresh-tiers.sh", "#!/bin/bash\necho run >> \"$HOME/refresh.log\"\n")
        calls = Path(self.env["HOME"]) / "refresh.log"
        cont = {"CORTADOS_ROUTER_REPO": str(repo), "CORTADOS_S1_CONTINUOUS": "1"}
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, **cont)
        self.assertEqual(calls.read_text().count("run"), 1, log)
        self.assertTrue((self.s1 / "data/last-bench-refresh").read_text().strip().isdigit())
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, **cont)
        self.assertEqual(calls.read_text().count("run"), 1, "a fresh refresh is not repeated")
        self.assertIn("benchmark/tiers refresh skipped", log)
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_BENCH_REFRESH_S="0", **cont)
        self.assertEqual(calls.read_text().count("run"), 2, "an expired interval refreshes again")
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_ROUTER_REPO=str(repo))   # window mode: every night
        self.assertEqual(calls.read_text().count("run"), 3, log)
        _exe(repo / "bin/refresh-tiers.sh", "#!/bin/bash\nexit 4\n")
        (self.s1 / "data/last-bench-refresh").write_text("5\n")
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, **cont)
        self.assertIn("refresh-tiers.sh failed", log)
        self.assertEqual((self.s1 / "data/last-bench-refresh").read_text(), "5\n", "a failed refresh is not stamped")

    def test_invalid_now_is_refused_or_falls_back_to_the_clock(self):
        for bad in ("730", "2500", "7", "abc"):
            out = self.minutes_left(bad)
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn(f"--minutes-left {bad} is not HHMM", out.stdout)
            self.assertEqual(out.stderr, "", bad)
            out = self.minutes_left(CORTADOS_S1_NOW=bad)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            first = out.stdout.split("\n")[0]
            self.assertTrue(first.isdigit() and int(first) < 1440, (bad, out.stdout))
            self.assertIn(f"CORTADOS_S1_NOW={bad} is not HHMM; using the clock", out.stdout)
            self.assertEqual(out.stderr, "", bad)

    def test_minutes_left_has_no_side_effects(self):
        stop, pid = self.s1 / "data/nightly.stop", self.s1 / "data/nightly.pid"
        stop.write_text("keep"); pid.write_text("999999\n")
        env = {k: v for k, v in self.env.items() if k != "CORTADOS_NIGHTLY_SUPERVISED"}   # would re-exec under overnight.py
        out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh"), "--minutes-left", "0130"], cwd=self.tmp,
                             env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout, "330\n", out.stdout + out.stderr)
        self.assertEqual(stop.read_text(), "keep")
        self.assertEqual(pid.read_text(), "999999\n")

    def test_reevaluation_keeps_the_candidates_own_training_fingerprint(self):
        # night 1: TERM mid-evaluation; night 2: +2 labels, re-evaluated and holdout-blocked; night 3: quiet, holdout backfilled.
        # The candidate never saw night 2's labels: they must never be marked trained without a training run. (Before the fix the
        # marker took night 2's hash, night 3 said "PROMOTED (retry) ...; no new data" and wrote it, and night 4 skipped them.)
        # With the candidate's own fingerprint, night 3 sees data newer than the candidate and trains it (a ready holdout supersedes).
        self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_TERM=1, STUB_HOLDOUT_BLOCKED=1)
        h1 = (self.s1 / "data/pending-candidate").read_text().split()[0]
        rc, log, _ = self.run_night(STUB_NEW_LABELS=2, STUB_HOLDOUT_BLOCKED=1)
        self.assertIn("pending runs/2026-01-01 still blocked", log)
        self.assertFalse((self.s1 / "data/last-train-labels").exists(), "night 2's labels were never trained on")
        marker = (self.s1 / "data/pending-candidate").read_text().split()
        self.assertEqual((marker[0], marker[2]), (h1, "holdout_blocked"), "the candidate keeps its own training fingerprint")
        (self.s1 / "data/stub-holdout").write_text("h-backfilled")
        rc, log, run3 = self.run_night(STUB_NEW_LABELS=0, STUB_HOLDOUT_BLOCKED=1)
        self.assertNotIn("no new data", log)
        self.assertNotIn("PROMOTED (retry)", log)
        self.assertIn("superseded by new training data", log)
        self.assertIn("training tonight", log)
        self.assertTrue((run3 / "report.json").exists(), "night 2's labels are trained, not skipped")

    def test_invalid_marker_falls_through_to_recovery_and_never_lets_prune_delete(self):
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_TERM=1)
        (self.s1 / "data/pending-candidate").write_text("garbage\n")          # truncated / garbled marker
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=2)
        self.assertIn("data/pending-candidate is invalid", log)
        self.assertIn("recovered runs/2026-01-01", log)
        self.assertNotIn("retention: removed checkpoints of unfinished runs/2026-01-01", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertIn("the newer labels are trained next night", log)

    def test_trained_unevaluated_run_without_marker_is_recovered(self):
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_EVAL_TERM=1)
        (self.s1 / "data/pending-candidate").unlink()                          # the 2026-11-03 state: no marker
        rc, log, run2 = self.run_night(STUB_NEW_LABELS=2)
        self.assertIn("recovered runs/2026-01-01", log)
        self.assertNotIn("retention: removed checkpoints of unfinished runs/2026-01-01", log)
        self.assertIn("PROMOTED (retry) runs/2026-01-01", log)
        self.assertFalse((run2 / "adapter").exists())

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
        (self.s1 / "data/stub-holdout").write_text("h-backfilled")                 # ... and its candidate promotes
        rc, log, run = self.run_night(STUB_NEW_LABELS=1, STUB_HOLDOUT_BLOCKED=1, **env)
        self.assertTrue((run / "adapter").exists(), log)
        self.assertIn("PROMOTED →", log)
        st = self.status()
        self.assertNotIn("holdout_count_failed", st.get("alerts", {}), "resolving the candidate clears its failed-count alert")
        self.assertNotIn("holdout_count_failed", st.get("streaks", {}))

    def test_failed_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_RC=1)

    def test_malformed_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_GARBAGE=1)

    def test_multiline_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_MULTILINE=1)

    def test_huge_holdout_floor_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(CORTADOS_S1_MIN_HOLDOUT="9" * 25)

    def test_bad_numeric_settings_fall_back_to_defaults(self):
        db = self.tmp / "router-air.db"; db.write_text(""); os.utime(db, (1_700_000_000, 1_700_000_000))
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_ROUTER_DB_EXTRA=db, CORTADOS_S1_DB_MAX_AGE_MIN="9" * 30,
                                   CORTADOS_S1_LOG_KEEP="9" * 30, CORTADOS_S1_MAX_ITERS="x")
        self.assertNotIn("integer expression expected", log)
        self.assertIn("DB_MAX_AGE_MIN=" + "9" * 30 + " is not a whole number up to 9 digits; using 60", log)
        self.assertIn("WARNING: synced router db", log, "a huge age limit must not make a stale db look fresh")
        self.assertIn("CORTADOS_S1_LOG_KEEP=" + "9" * 30 + " is not a whole number; keeping 14", log)
        self.assertIn("MAX_ITERS=x is not a whole number up to 9 digits; using 3600", log)

    def test_nul_bytes_in_holdout_count_keep_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_NUL="1")

    def test_embedded_nul_in_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_NUL="2")

    def test_invalid_warm_start_cap_uses_its_own_default(self):
        self.run_night(STUB_NEW_LABELS=3)                                          # promotes: a serving pointer exists
        self.assertTrue(self.pointer.exists())
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_NIGHT_ITERS="bogus")
        self.assertIn("warm start from", log)
        self.assertIn("MAX_ITERS=bogus is not a whole number up to 9 digits; using 1500", log)

    def _pending_run(self, segment_target, checkpoint="0000100_adapters.safetensors"):
        run = self.s1 / "runs/2026-01-01"
        (run / "adapter").mkdir(parents=True)
        (run / "adapter" / checkpoint).write_text("w")
        (run / "segment-target").write_text(segment_target)
        (run / "nightly.log").write_text("")
        return run

    def test_unremovable_bad_segment_target_fails_promptly(self):
        run = self._pending_run("garbage")
        run.chmod(0o555); self.addCleanup(run.chmod, 0o755)                         # segment-target cannot be removed
        t0 = time.time()
        rc, log, _ = self.run_night(STUB_NEW_LABELS=1)
        self.assertLess(time.time() - t0, 30, "must not spin")
        self.assertNotEqual(rc, 0, log)
        self.assertIn("ERROR: cannot remove runs/2026-01-01/segment-target; stopping", log)
        calls = (self.tmp / "home/launchctl.log").read_text().split("\n")
        self.assertTrue(calls[-2].startswith("launchctl load"), f"the scorer is restarted on the error exit: {calls}")

    def test_unreadable_checkpoint_count_stops_the_resume(self):
        self._pending_run("100", checkpoint="0abc_adapters.safetensors")
        rc, log, _ = self.run_night(STUB_NEW_LABELS=1)
        self.assertNotEqual(rc, 0, log)
        self.assertIn("ERROR: checkpoint runs/2026-01-01/adapter/0abc_adapters.safetensors has no readable step count; not resuming", log)
        self.assertNotIn("resuming from", log)

    def test_trailing_blank_line_in_holdout_count_keeps_the_candidate(self):
        self._count_failure_keeps_candidate(STUB_COUNT_TRAILING=1)

    def test_evaluation_failed_candidate_below_the_floor_is_not_superseded(self):
        # the holdout is short AND the bar is unavailable: the candidate's last reason is evaluation_failed, not holdout_blocked
        rc, log, run1 = self.run_night(STUB_NEW_LABELS=3, STUB_HOLDOUT_BLOCKED=1, STUB_EVAL_UNAVAILABLE=1)
        self.assertIn("EVALUATION UNAVAILABLE for runs/2026-01-01", log)
        for _ in range(2):
            rc, log, run = self.run_night(STUB_NEW_LABELS=2, STUB_HOLDOUT_BLOCKED=1)      # new train labels every night
            self.assertEqual(rc, 0, log)
            self.assertIn("holdout still 50/200; not retraining (pending runs/2026-01-01 (evaluation_failed) waits", log)
            self.assertNotIn("superseded", log)
            self.assertFalse((run / "adapter").exists(), "no retraining while the holdout is below the floor")
            self.assertIn("evaluation_failed runs/2026-01-01", (self.s1 / "data/pending-candidate").read_text())
        self.assertTrue((run1 / "adapter").is_dir())
        self.assertIn("holdout_blocked", self.status()["alerts"])

    def test_leading_zero_settings_are_decimal(self):
        rc, log, run = self.run_night(STUB_NEW_LABELS=30, CORTADOS_S1_MAX_ITERS="010", CORTADOS_S1_LOG_KEEP="08",
                                      CORTADOS_S1_STALL_NIGHTS="09", CORTADOS_S1_DB_MAX_AGE_MIN="08")
        self.assertEqual(rc, 0, log)
        self.assertNotIn("value too great for base", log)
        self.assertNotIn("integer expression expected", log)
        self.assertEqual((run / "segment-target").read_text().strip(), "10", "010 is ten, not octal eight")
        rc, log, run = self.run_night(STUB_NEW_LABELS=30, CORTADOS_S1_MAX_ITERS="08")
        self.assertNotIn("value too great for base", log)
        self.assertIn("segment target", log)

    def test_bad_thresholds_and_batch_fall_back_with_a_log(self):
        for bad in ("lots", "9" * 30, "0"):
            _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_STALL_NIGHTS=bad, CORTADOS_S1_EVAL_FAIL_NIGHTS=bad,
                                       CORTADOS_S1_EARLY_STOP_NIGHTS=bad, CORTADOS_S1_HOLDOUT_BLOCKED_NIGHTS=bad,
                                       CORTADOS_S1_BATCH="0" if bad == "0" else bad)
            for name in ("STALL_NIGHTS", "EVAL_FAIL_NIGHTS", "EARLY_STOP_NIGHTS", "HOLDOUT_BLOCKED_NIGHTS", "BATCH"):
                self.assertRegex(log, rf"{name}={bad} is (not a whole number up to 9 digits|below 1); using 2", (name, bad))
            self.assertNotIn("integer expression expected", log)
            self.assertNotIn("Traceback", log)

    def test_failing_streak_record_is_logged(self):
        blocker = self.tmp / "not-a-dir"; blocker.write_text("")
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, STUB_LABEL_RC=3, STUB_STOP_REASON="quota",
                                   CORTADOS_S1_STATUS=blocker / "status.json")
        self.assertIn("WARNING: recording the label_early_stop streak failed", log)
        self.assertIn("WARNING: recording label growth failed", log)

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


# Stub gate for the sweep-stop tests: like studio_capacity's gate it takes the exclusive lock, starts the command in its own
# session with the lock fd inherited, and on SIGTERM stops that group (bounded) and exits 143. Records its and the child's pids.
SWEEP_GATE = r'''#!/usr/bin/env python3
import fcntl, os, signal, subprocess, sys, time
if sys.argv[1:5] != ["-B", "-m", "cortados_router.studio_capacity", "gate"]:
    os.execv(os.environ["SWEEP_REAL_PY"], [os.environ["SWEEP_REAL_PY"]] + sys.argv[1:])
argv = sys.argv[5:]; argv = argv[1:] if argv[:1] == ["--"] else argv
fd = os.open(os.environ["SWEEP_LOCK"], os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
child = subprocess.Popen(argv, pass_fds=(fd,), start_new_session=True)
if any(a.endswith("calibrate.py") for a in argv):   # only the sweep's gate; the base-model probe also runs gated
    open(os.environ["SWEEP_PIDS"], "w").write("%d %d\n" % (os.getpid(), child.pid))
while child.poll() is None and not stop:
    time.sleep(0.05)
if stop:
    os.killpg(child.pid, signal.SIGTERM)
    end = time.time() + 5          # the real gate's grace is 30 s; 5 s keeps the test short
    while child.poll() is None and time.time() < end:
        time.sleep(0.05)
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL); child.wait()
    sys.exit(143)
sys.exit(child.returncode)
'''

# calibrate.py stand-in: long sleep when STUB_CAL_SLEEP is set (the sweep a stop interrupts), exit STUB_CAL_RC when set (a failed
# sweep), else a finished sweep line
CALIBRATE_STUB = ("import os, sys, time\n"
                  "if os.environ.get('STUB_CAL_SLEEP'): time.sleep(float(os.environ['STUB_CAL_SLEEP']))\n"
                  "if os.environ.get('STUB_CAL_RC'): print('calibrate failed'); sys.exit(int(os.environ['STUB_CAL_RC']))\n"
                  "print('sweep: 2 settings')\n")

# python3 wrapper put first on PATH: when nightly.sh parses the sweep request (`python3 - data/sweep-request.json`) it marks
# the moment and holds, so the test can deliver TERM during parsing, before the launch.
PARSE_HOOK = """#!/bin/bash
if [ "$1" = - ] && [ "${2:-}" = data/sweep-request.json ] && [ -n "${STUB_PARSE_MARK:-}" ]; then
  touch "$STUB_PARSE_MARK"; /bin/sleep 2
fi
exec "$SWEEP_REAL_PY" "$@"
"""


def _alive(pid: int) -> bool:
    """True while pid exists and is not a zombie (ps shows Z for those)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.returncode == 0 and "Z" not in out.stdout


class NightlySweepStop(unittest.TestCase):
    setUp = NightlyEndToEnd.setUp

    def _prepare(self, **env):
        real_py = shutil.which("python3")
        self.assertIsNotNone(real_py)
        _exe(self.tmp / "sweep-py", SWEEP_GATE)
        (self.s1 / "calibrate.py").write_text(CALIBRATE_STUB)
        _exe(Path(self.env["HOME"]) / ".local/bin/python3", PARSE_HOOK)
        (self.s1 / "data/sweep-request.json").write_text(json.dumps(
            {"candidate": "runs/c/adapter", "live": "runs/l/adapter", "tilts": [0], "alphas": [1.0]}))
        self.lock = self.tmp / "train.lock"; self.pids = self.tmp / "gate-pids"
        self.run = "runs/2026-01-01"
        return {**self.env, "CORTADOS_S1_PY": str(self.tmp / "sweep-py"), "SWEEP_REAL_PY": real_py,
                "SWEEP_LOCK": str(self.lock), "SWEEP_PIDS": str(self.pids), "CORTADOS_S1_RUN": self.run,
                "CORTADOS_S1_NOW": "0300", "CORTADOS_S1_NIGHT_DATE": "2026-01-02",
                **{k: str(v) for k, v in env.items()}}

    def _start(self, env):
        proc = subprocess.Popen(["/bin/bash", str(self.s1 / "nightly.sh")], cwd=self.tmp, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        def reap():   # bounded teardown: never leave the shell or a stub gate/child running after a failure
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait(5)
            if self.pids.exists():
                gate, child = map(int, self.pids.read_text().split())
                for kill, target in ((os.kill, gate), (os.killpg, child)):   # the child leads its own group
                    try:
                        kill(target, signal.SIGKILL)
                    except ProcessLookupError:
                        pass      # already gone: the expected case when the test passed
        self.addCleanup(reap)
        return proc

    def _wait_for(self, cond, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out waiting; log:\n" + self._log())

    def _log(self):
        p = self.s1 / self.run / "nightly.log"
        return p.read_text() if p.exists() else "<no log>"

    def _stop_cycle(self, proc):
        """Like overnight.py's stop_group_drained: TERM the cycle's WHOLE process group (nightly.sh leads it), then wait."""
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(30)

    def test_group_term_during_sweep_stops_gate_and_child_and_frees_the_lock(self):
        env = self._prepare(STUB_CAL_SLEEP=300)
        proc = self._start(env)
        self._wait_for(lambda: self.pids.exists() and len(self.pids.read_text().split()) == 2)
        gate, child = map(int, self.pids.read_text().split())
        self._stop_cycle(proc)
        for pid in (gate, child):
            self.assertFalse(_alive(pid), "pid %d survived nightly.sh; log:\n%s" % (pid, self._log()))
        fd = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises BlockingIOError if anything still holds the lock
        finally:
            os.close(fd)
        self.assertTrue((self.s1 / "data/sweep-request.json").exists(), self._log())
        self.assertIn("stop requested", self._log())     # the cycle's TERM trap (exit 130) after the gate returned 143
        self.assertEqual(proc.returncode, 130)
        self.assertFalse(list((self.s1 / "data").glob("sweep-request.done-*.json")))

    def test_group_term_during_request_parsing_never_starts_the_sweep(self):
        mark = self.tmp / "parsing"
        env = self._prepare(STUB_CAL_SLEEP=300, STUB_PARSE_MARK=mark)
        proc = self._start(env)
        self._wait_for(mark.exists)
        self._stop_cycle(proc)
        self.assertFalse(self.pids.exists(), "the gate started after a stop; log:\n" + self._log())
        self.assertTrue((self.s1 / "data/sweep-request.json").exists())
        self.assertIn("stop requested", self._log())

    def test_finished_sweep_renames_the_request_and_logs_the_summary(self):
        proc = self._start(self._prepare())
        proc.wait(30)
        self.assertFalse((self.s1 / "data/sweep-request.json").exists(), self._log())
        self.assertEqual(len(list((self.s1 / "data").glob("sweep-request.done-*.json"))), 1)
        self.assertIn("sweep: 2 settings", self._log())

    def test_failed_gate_keeps_the_request(self):
        proc = self._start(self._prepare(STUB_CAL_RC=3))
        proc.wait(30)
        self.assertTrue((self.s1 / "data/sweep-request.json").exists(), self._log())
        self.assertFalse(list((self.s1 / "data").glob("sweep-request.done-*.json")))
        self.assertIn("SWEEP FAILED or interrupted rc=3", self._log())

    def test_repeated_group_term_during_cleanup_tail_still_finishes_cleanup(self):
        # overnight.py re-sends TERM to the cycle group every 5 s while draining. Once sweep_on_exit has returned, cleanup
        # ignores TERM/INT, so its tail (stop_deadline, pkill, gpu_share, rm pidfile) runs to the end. The stub launchctl
        # holds gpu_share's `launchctl load` for ~2 s while the test keeps TERMing the whole group.
        mark = self.tmp / "loading"
        _exe(Path(self.env["HOME"]) / ".local/bin/launchctl",
             '#!/bin/bash\necho "launchctl $*" >> "$HOME/launchctl.log"\n'
             'if [ "$1" = load ] && [ -n "${STUB_LOAD_MARK:-}" ]; then touch "$STUB_LOAD_MARK"; '
             'for i in 1 2 3 4 5 6 7 8 9 10; do /bin/sleep 0.2; done; echo loaded >> "$HOME/launchctl.log"; fi\n')
        env = self._prepare(STUB_LOAD_MARK=mark)
        (self.s1 / "data/sweep-request.json").unlink()    # no sweep: sweep_on_exit returns at once, the cycle's exit trap is live
        proc = self._start(env)
        self._wait_for(lambda: mark.exists() or proc.poll() is not None)
        self.assertIsNone(proc.poll(), "cycle ended before gpu_share; log:\n" + self._log())
        self.assertTrue((self.s1 / "data/nightly.pid").exists(), self._log())   # we are inside cleanup's tail
        sent, end = 0, time.time() + 20
        while proc.poll() is None and time.time() < end:
            try:
                os.killpg(proc.pid, signal.SIGTERM); sent += 1
            except ProcessLookupError:
                break                                     # the group is gone: cleanup finished between checks
            time.sleep(0.1)
        proc.wait(10)
        self.assertGreaterEqual(sent, 5)
        self.assertFalse((self.s1 / "data/nightly.pid").exists(), "pidfile left behind; log:\n" + self._log())
        calls = (self.tmp / "home/launchctl.log").read_text()
        self.assertIn("loaded", calls)                    # the held launchctl load ran to the end (TERM ignored)
        self.assertIn("scorer back", self._log())         # gpu_share's own tail ran
        self.assertNotIn("stop requested", self._log())   # no trap fired during cleanup


if __name__ == "__main__":
    unittest.main()
