"""s1/nightly.sh run selection and checkpoint retention (CortadOS Router S1 scorer training).

Regression for 2026-10-13: data/train-config.env still pinned CORTADOS_S1_RUN to the finished 10-01 run, so every night
opened a new folder, trained from step 0, stopped at the 07:00 deadline around 2,400 of 3,600 steps and never reached
evaluation; ten abandoned runs held ~6.5 GB of checkpoints. Only the run-selection block is executed here, against a
fake runs/ tree: nothing trains, no service is touched.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

NIGHTLY = Path(__file__).resolve().parents[1] / "s1" / "nightly.sh"


def selection_block() -> str:
    text = NIGHTLY.read_text()
    m = re.search(r"^newest_unfinished_run\(\) \{.*?^mkdir -p \"\$RUN\" data$", text, re.S | re.M)
    assert m, "test setup: nightly.sh run-selection block not found"
    return m.group(0).replace('mkdir -p "$RUN" data', 'mkdir -p "$RUN"') + '\nprune_unresumable_runs\necho "RUN=$RUN"\n'


class NightlyRunSelection(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nightly-runs-"))
        (self.root / "current.json").write_text(json.dumps({"adapter": str(self.root / "runs/2026-09-28/adapter")}))

    def run_dir(self, name, *, finished=False, checkpoints=0, age_s=0):
        d = self.root / "runs" / name
        (d / "adapter").mkdir(parents=True)
        for i in range(1, checkpoints + 1):
            (d / "adapter" / f"{i * 100:07d}_adapters.safetensors").write_bytes(b"x")
        if checkpoints:
            (d / "segment-target").write_text("3600")
        if finished:
            (d / "report.json").write_text("{}")
        t = 1_700_000_000 + age_s
        os.utime(d, (t, t))
        return d

    def select(self, pin):
        env = {**os.environ, "CORTADOS_S1_RUN": pin} if pin else {k: v for k, v in os.environ.items() if k != "CORTADOS_S1_RUN"}
        out = subprocess.run(["bash", "-c", selection_block()], cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return re.search(r"^RUN=(.*)$", out.stdout, re.M).group(1), out.stdout

    def test_finished_pin_resumes_newest_unfinished_run(self):
        self.run_dir("2026-09-28", finished=True, checkpoints=30)
        self.run_dir("2026-10-01-0130", finished=True, checkpoints=36, age_s=10)
        old = self.run_dir("2026-10-12-0130", checkpoints=24, age_s=20)
        new = self.run_dir("2026-10-13-0130", checkpoints=22, age_s=30)
        run, _ = self.select("runs/2026-10-01-0130")
        self.assertEqual(run, "runs/2026-10-13-0130",
                         "nightly.sh: a finished pinned run must resume the newest unfinished run, never restart from step 0")
        self.assertTrue((new / "adapter").is_dir())
        self.assertFalse((old / "adapter").exists(), "nightly.sh retention: an unresumable unfinished run's checkpoints must be removed")
        self.assertTrue((old / "segment-target").exists(), "nightly.sh retention: logs and run metadata are kept")
        self.assertTrue((self.root / "runs/2026-10-01-0130/adapter").is_dir(), "nightly.sh retention: finished runs are never pruned")
        self.assertTrue((self.root / "runs/2026-09-28/adapter").is_dir(), "nightly.sh retention: the promoted adapter is never pruned")

    def test_finished_pin_without_unfinished_run_starts_new_folder(self):
        self.run_dir("2026-10-01-0130", finished=True, checkpoints=36)
        run, _ = self.select("runs/2026-10-01-0130")
        self.assertRegex(run, r"^runs/\d{4}-\d{2}-\d{2}-\d{4}$", "nightly.sh: with nothing to resume, a new dated folder is started")

    def test_unfinished_pin_is_kept_and_promoted_unfinished_run_is_spared(self):
        self.run_dir("2026-09-28", checkpoints=5)                      # promoted adapter's run, even without a report
        pinned = self.run_dir("2026-10-13-0130", checkpoints=22, age_s=30)
        run, _ = self.select("runs/2026-10-13-0130")
        self.assertEqual(run, "runs/2026-10-13-0130")
        self.assertTrue((pinned / "adapter").is_dir())
        self.assertTrue((self.root / "runs/2026-09-28/adapter").is_dir(),
                        "nightly.sh retention: the run holding current.json's adapter is never pruned")


def warm_start_block() -> str:
    text = NIGHTLY.read_text()
    m = re.search(r'^MAX_ITERS="\$\{CORTADOS_S1_MAX_ITERS:-3600\}".*?^fi$', text, re.S | re.M)
    assert m, "test setup: nightly.sh warm-start block not found"
    return 'LR="${CORTADOS_S1_LR:-1e-4}"\n' + m.group(0) + '\necho "INIT=${CORTADOS_S1_INIT:-} MAX=$MAX_ITERS LR=$LR"\n'


class NightlyRetrainsEveryNight(unittest.TestCase):
    """Forest 2026-10-13: retrain every night, warm-starting from the promoted adapter so a candidate is evaluated nightly."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nightly-warm-"))
        (self.root / "current.json").write_text(json.dumps({"adapter": "/promoted/run/adapter"}))

    def run_block(self, pending, init=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CORTADOS_S1_")}
        if init:
            env["CORTADOS_S1_INIT"] = init
        out = subprocess.run(["bash", "-c", f"pending={pending}\n" + warm_start_block()], cwd=self.root, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return dict(kv.split("=", 1) for kv in out.stdout.split())

    def test_new_run_warm_starts_from_promoted_adapter_with_a_night_sized_budget(self):
        got = self.run_block(pending=0)
        self.assertEqual(got["INIT"], "/promoted/run/adapter/adapters.safetensors",
                         "nightly.sh: a new run must warm-start from current.json's promoted adapter")
        self.assertEqual(got["MAX"], "1500", "nightly.sh: a warm-start night must fit training and evaluation before 07:00")
        self.assertEqual(got["LR"], "5e-5")

    def test_pending_run_resumes_without_warm_start(self):
        got = self.run_block(pending=1)
        self.assertEqual(got["INIT"], "", "nightly.sh: a run being resumed must not be re-initialised from the promoted adapter")
        self.assertEqual(got["MAX"], "3600")

    def test_explicit_init_wins(self):
        got = self.run_block(pending=0, init="/explicit/adapters.safetensors")
        self.assertEqual(got["INIT"], "/explicit/adapters.safetensors")

    def test_ordinary_nights_are_not_labelling_only(self):
        self.assertNotIn("labelling only tonight", NIGHTLY.read_text(),
                         "nightly.sh: Forest 2026-10-13 ruled training every night; the cadence skip must stay removed")


if __name__ == "__main__":
    unittest.main()
