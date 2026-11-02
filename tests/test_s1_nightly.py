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
from pathlib import Path
from unittest import mock

S1 = Path(__file__).resolve().parents[1] / "s1"

STUBS = {
    "dataset.py": "open('data/ran-dataset', 'a').write('x')\n",
    "label.py": (
        "import json, os, sys\n"
        "open('data/label-args', 'w').write(' '.join(sys.argv[1:]))\n"
        "rc = int(os.environ.get('STUB_LABEL_RC', '0'))\n"
        "if rc: sys.exit(rc)\n"
        "p = 'data/labels.jsonl'\n"
        "have = sum(1 for _ in open(p)) if os.path.exists(p) else 0\n"
        "with open(p, 'a') as f:\n"
        "    for i in range(int(os.environ.get('STUB_NEW_LABELS', '0'))):\n"
        "        f.write(json.dumps({'id': f'r{have + i}'}) + '\\n')\n"
    ),
    "build_sft.py": (
        "import os\n"
        "os.makedirs('data/sft', exist_ok=True)\n"
        "open('data/sft/train.jsonl', 'w').write('{}\\n' * 4)\n"
        "open('data/ran-build-sft', 'a').write('x')\n"
    ),
    "evaluate.py": (
        "import argparse, json, os\n"
        "ap = argparse.ArgumentParser(); ap.add_argument('--base'); ap.add_argument('--adapter'); ap.add_argument('--out')\n"
        "ap.add_argument('--baseline', action='store_true'); a = ap.parse_args()\n"
        "c = {'band_accuracy': 0.9, 'under_routed': 0.01, 'over_routed': 0.1, 'outward_recall': 1.0}\n"
        "json.dump({'base': a.base, 'temperatures': {}, 'when': '2026-11-02', 'candidate': c,\n"
        "           'promote': os.environ.get('STUB_PROMOTE', '1') == '1'}, open(os.path.join(a.out, 'report.json'), 'w'))\n"
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
        for name in ("nightly.sh", "nightly_state.py"):
            shutil.copy(S1 / name, self.s1 / name)
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
        run = f"runs/n{self.night}"
        out = subprocess.run(["/bin/bash", str(self.s1 / "nightly.sh")], cwd=self.tmp,
                             env={**self.env, "CORTADOS_S1_RUN": run, **{k: str(v) for k, v in env.items()}},
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
            log = self.s1 / f"runs/n{i + 1}/nightly.log"
            os.utime(log, (1_700_000_000 + i, 1_700_000_000 + i))
        _, log, _ = self.run_night(STUB_NEW_LABELS=1, CORTADOS_S1_LOG_KEEP=2)
        self.assertIn("log rotation: removed", log)
        self.assertEqual(sorted(p.parent.name for p in self.s1.glob("runs/*/nightly.log")), ["n3", "n4"])
        self.assertTrue((self.s1 / "runs/n1/report.json").exists(), "rotation removes logs only")


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
