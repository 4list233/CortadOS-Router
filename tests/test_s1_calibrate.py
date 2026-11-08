"""s1/calibrate.py (evaluation-only tilt/blend sweep) and nightly.sh's sweep_request step. Fakes only: no MLX, no model,
no real ~/.cortados, no Studio."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

S1 = Path(__file__).resolve().parents[1] / "s1"
sys.path.insert(0, str(S1))

import calibrate as cal  # noqa: E402
import evaluate as ev  # noqa: E402
from common import QUESTIONS, core  # noqa: E402
from student import N_OPTIONS  # noqa: E402


class Mat:
    """A tiny 2-D matrix with the three operations blend_tensors uses (scalar *, concatenate) plus @ for checking."""
    def __init__(self, rows):
        self.r = [list(map(float, x)) for x in rows]

    def __mul__(self, k):
        return Mat([[v * k for v in row] for row in self.r])

    def __matmul__(self, o):
        return Mat([[sum(a * o.r[k][j] for k, a in enumerate(row)) for j in range(len(o.r[0]))] for row in self.r])

    def close(self, o):
        return all(abs(a - b) < 1e-9 for ra, rb in zip(self.r, o.r) for a, b in zip(ra, rb))


def mconcat(a, b, axis):
    return Mat(a.r + b.r) if axis == 0 else Mat([x + y for x, y in zip(a.r, b.r)])


def delta(t, scale):
    return (t["l.lora_a"] @ t["l.lora_b"]) * scale


def lin(a, b, alpha):
    return Mat([[alpha * x + (1 - alpha) * y for x, y in zip(ra, rb)] for ra, rb in zip(a.r, b.r)])


CAND = {"l.lora_a": Mat([[1, 2], [3, 4], [5, 6]]), "l.lora_b": Mat([[1, 0, 2, 1], [0, 1, 1, 3]])}       # rank 2
LIVE_R1 = {"l.lora_a": Mat([[2], [1], [0]]), "l.lora_b": Mat([[4, 3, 2, 1]])}                       # rank 1


class Blend(unittest.TestCase):
    def test_alpha_one_is_candidate_and_zero_is_live(self):
        for alpha, want in ((1.0, delta(CAND, 2.0)), (0.0, delta(LIVE_R1, 0.5))):
            out = cal.blend_tensors(CAND, LIVE_R1, alpha, 2.0, 0.5, mconcat)
            self.assertTrue(delta(out, 2.0).close(want), alpha)   # output carries the candidate's scale

    def test_midpoint_is_exact_delta_interpolation_across_ranks(self):
        out = cal.blend_tensors(CAND, LIVE_R1, 0.7, 2.0, 0.5, mconcat)
        self.assertEqual(len(out["l.lora_a"].r[0]), 3)                       # rank 2 + rank 1
        self.assertTrue(delta(out, 2.0).close(lin(delta(CAND, 2.0), delta(LIVE_R1, 0.5), 0.7)))

    def test_candidate_only_scale(self):
        out = cal.blend_tensors(CAND, None, 0.5, 2.0, 1.0, mconcat)
        self.assertTrue(delta(out, 2.0).close(delta(CAND, 1.0)))

    def test_refuses_mismatched_keys_and_layout(self):
        with self.assertRaises(cal.BlendError):
            cal.blend_tensors(CAND, {"other.lora_a": Mat([[1]]), "l.lora_b": Mat([[1]])}, 0.5, 1, 1, mconcat)
        cfg = {"model": "base", "num_layers": 16, "fine_tune_type": "lora", "lora_parameters": {"rank": 8, "scale": 20}}
        self.assertEqual(cal.blend_plan(cfg, {**cfg, "lora_parameters": {"rank": 4, "scale": 10}})["lora_parameters"]["rank"], 12)
        for k, v in (("model", "other"), ("num_layers", 8)):
            with self.assertRaises(cal.BlendError):
                cal.blend_plan(cfg, {**cfg, k: v})


def scored(probs):
    answers = {q: core_decode(q, max(range(len(p)), key=p.__getitem__)) for q, p in probs.items()}
    return {"answers": answers, "probs": probs}


def core_decode(q, d):
    from student import decode
    return decode(q, d)


def flat_probs(top=0):
    return {q: [1.0 if i == top else 0.0 for i in range(N_OPTIONS[q])] for q in QUESTIONS}


class Tilt(unittest.TestCase):
    def test_higher_tilt_never_lowers_answers_or_band(self):
        probs = flat_probs()
        for i, f in enumerate(core.FEATURES):    # close calls of varying margins on every difficulty feature
            n = N_OPTIONS[f]
            probs[f] = [0.5 - 0.04 * i, 0.0] + [0.0] * (n - 3) + [0.5 + 0.04 * i - 0.0] if n >= 3 else [0.55, 0.45]
            probs[f] = [x / sum(probs[f]) for x in probs[f]]
        p = scored(probs)
        last_band, last_ans = -1, None
        for t in cal.DEFAULT_TILTS + [0.5, 1.0]:
            q = cal.tilt_answers(p, t, core.FEATURES)
            band = ev.BANDS.index(ev.band_of(q["answers"]))
            self.assertGreaterEqual(band, last_band, t)
            if last_ans:
                for f in core.FEATURES:
                    self.assertGreaterEqual(int(q["answers"][f]), int(last_ans[f]))
            last_band, last_ans = band, q["answers"]
        self.assertIs(cal.tilt_answers(p, 0.0, core.FEATURES), p)
        with self.assertRaises(ValueError):
            cal.tilt_answers(p, -0.1, core.FEATURES)


def grade(acc, under, over=0.1, recall=0.9):
    return {"n": 300, "band_accuracy": acc, "under_routed": under, "over_routed": over, "outward_recall": recall}


class Selection(unittest.TestCase):
    def test_selection_reads_teacher_only(self):
        live = grade(0.6, 0.065)
        rows = [{"setting": "a", "teacher": grade(0.70, 0.145), "outcome": {"band_accuracy": 0.99}},   # under rise .08 > .07
                {"setting": "b", "teacher": grade(0.66, 0.12), "outcome": {"band_accuracy": 0.10}},
                {"setting": "c", "teacher": grade(0.64, 0.10), "outcome": {"band_accuracy": 0.95}},
                {"setting": "d", "teacher": grade(0.69, 0.08, recall=0.5), "outcome": None}]            # below outward floor
        self.assertEqual(cal.select_on_teacher(rows, live, 0.07, 0.85), "b")
        for r in rows:          # outcome numbers cannot change the choice
            r["outcome"] = {"band_accuracy": 0.0}
        self.assertEqual(cal.select_on_teacher(rows, live, 0.07, 0.85), "b")


class FakeScorer:
    def __init__(self, adapter):
        self.adapter = adapter

    def score(self, request, unit_kind):
        return {**scored(flat_probs()), "ms": 1}


class SweepNeverPromotes(unittest.TestCase):
    def test_sweep_writes_only_sweep_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="s1-cal-"))
        pointer = tmp / "current.json"
        pointer.write_text('{"adapter": "live"}')
        before = pointer.read_text(), pointer.stat().st_mtime_ns
        tv = ev.teacher_view([scored(flat_probs())["answers"]])
        holdout = [({"id": f"r{i}", "request": "x", "unit_kind": "interactive"}, tv) for i in range(3)]
        o_holdout = [({"id": "o1", "request": "y", "unit_kind": "interactive"}, "R2")]
        run = tmp / "runs/2026-11-08"
        old = os.environ.get("CORTADOS_S1_SERVE_POINTER")
        os.environ["CORTADOS_S1_SERVE_POINTER"] = str(pointer)
        try:
            sweep = cal.run_sweep(candidate="cand", live="live", tilts=[0, 0.1], alphas=[1.0], scales=[], out=run / "sweep.json",
                                  holdout=holdout, o_holdout=o_holdout, make_scorer=FakeScorer, fail_up=0.6)
        finally:
            if old is None:
                os.environ.pop("CORTADOS_S1_SERVE_POINTER")
            else:
                os.environ["CORTADOS_S1_SERVE_POINTER"] = old
        self.assertEqual((pointer.read_text(), pointer.stat().st_mtime_ns), before)
        self.assertEqual([r["setting"] for r in sweep["settings"]], ["cand-t0", "cand-t0.1"])
        self.assertTrue((run / "sweep.json").exists() and (run / "sweep.md").exists())
        self.assertEqual(sorted(p.name for p in run.iterdir()), ["sweep", "sweep.json", "sweep.md"])
        for setting in ("live", "cand-t0", "cand-t0.1"):    # per-row files for the morning join, nothing else under sweep/
            for f in ("holdout-rows.jsonl", "outcome-rows.jsonl"):
                self.assertTrue((run / "sweep" / setting / f).exists(), (setting, f))
        rows = [json.loads(x) for x in (run / "sweep/cand-t0/holdout-rows.jsonl").read_text().splitlines()]
        self.assertEqual([r["id"] for r in rows], ["r0", "r1", "r2"])
        self.assertNotIn("request", rows[0])
        self.assertNotIn("promote\": true", (run / "sweep.json").read_text().replace(" ", ""))


class RowFiles(unittest.TestCase):
    def test_grade_writes_one_row_per_request_without_text(self):
        tmp = Path(tempfile.mkdtemp(prefix="s1-rows-"))
        tv = ev.teacher_view([scored(flat_probs())["answers"]])
        holdout = [({"id": "a", "request": "x" * 400, "unit_kind": "interactive", "source": "loop"}, tv)]
        probs = flat_probs()
        probs["steps"] = [0.0, 0.45, 0.55] + [0.0] * (N_OPTIONS["steps"] - 3)
        p = scored(probs)
        g = ev.grade(lambda r: ev.failed_up(p, 0.6), holdout, rows_out=tmp / "h.jsonl")
        row = json.loads((tmp / "h.jsonl").read_text())
        self.assertEqual((row["id"], row["source"], row["chars"], row["approx_tokens"]), ("a", "loop", 400, 100))
        self.assertEqual(row["pred_answers_raw"]["steps"], 2)               # before failed_up: the model's top answer
        self.assertEqual(row["teacher_major"], tv["major"])
        self.assertEqual(row["verdict"], "hit" if g["band_accuracy"] == 1 else ("under" if g["under_routed"] else "over"))
        self.assertNotIn("request", row)
        ev.grade_outcome(lambda r: p, [(holdout[0][0], "R4")], rows_out=tmp / "o.jsonl")
        o = json.loads((tmp / "o.jsonl").read_text())
        self.assertEqual(o["true_band"], "R4")
        self.assertNotIn("teacher_major", o)


class RowsReport(unittest.TestCase):
    def test_join_finds_candidate_only_under_routes(self):
        import rows_report
        tmp = Path(tempfile.mkdtemp(prefix="s1-join-"))

        def row(i, verdict, steps, tokens=10, src="loop", band="R2"):
            return {"id": i, "verdict": verdict, "pred_answers": {"steps": steps, "novelty": 0}, "approx_tokens": tokens,
                    "source": src, "true_band": band}
        cand = [row("a", "under", 1), row("b", "under", 1), row("c", "hit", 2), row("d", "under", 0, 300, "teacher", "R3"), row("x", "hit", 1)]
        live = [row("a", "hit", 2), row("b", "under", 1), row("c", "hit", 2), row("d", "over", 0, 300), row("y", "hit", 1)]
        (tmp / "c.jsonl").write_text("".join(json.dumps(r) + "\n" for r in cand))
        (tmp / "l.jsonl").write_text("".join(json.dumps(r) + "\n" for r in live))
        rep = rows_report.regressions(rows_report.load(tmp / "c.jsonl"), rows_report.load(tmp / "l.jsonl"))
        self.assertEqual(rep["ids"], ["a", "d"])
        self.assertEqual((rep["joined"], rep["only_candidate"], rep["only_live"]), (4, 1, 1))
        self.assertEqual(rep["by_source"], {"loop": 1, "teacher": 1})
        self.assertEqual(rep["by_true_band"], {"R2": 1, "R3": 1})
        self.assertEqual(rep["by_differing_question"], {"steps": 1})
        self.assertEqual(rep["by_length"], {"<50": 1, "200-799": 1})


def sweep_fn() -> str:
    src = (S1 / "nightly.sh").read_text()
    start = src.index("gated() {")   # sweep_request runs its step through gated(), so extract both
    end = src.index("\n}\n", src.index("sweep_request() {")) + 3
    gated = src[start:src.index("\n}\n", start) + 3]
    return gated + src[src.index("sweep_request() {"):end]


class NightlySweepStep(unittest.TestCase):
    def run_step(self, allowed, with_request=True):
        tmp = Path(tempfile.mkdtemp(prefix="s1-sweep-"))
        (tmp / "data").mkdir()
        if with_request:
            (tmp / "data/sweep-request.json").write_text(json.dumps(
                {"candidate": "runs/2026-11-08/adapter", "live": "runs/2026-10-16-0130/adapter", "tilts": [0, 0.1], "alphas": [1.0, 0.7]}))
        # stand-in for the gate interpreter: sweep_request runs "$GATE_PY" -B -m cortados_router.studio_capacity gate -- ...
        (tmp / "fake-gate").write_text("#!/bin/bash\necho \"$*\" > data/gated.log; echo 'sweep: 4 settings'\n")
        (tmp / "fake-gate").chmod(0o755)
        script = ("GATE_PY=./fake-gate; GATE_ROOT=.; SWEEP_CANCELLED=0\n"
                  "PY=fake-py; BASE=base\n" + sweep_fn() + "sweep_request\n")
        out = subprocess.run(["/bin/bash", "-c", script], cwd=tmp, capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ["PATH"], "CORTADOS_S1_TRAIN_ALLOWED": allowed})
        return tmp, out

    def test_runs_with_request_and_renames_it(self):
        tmp, out = self.run_step("1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("sweep: 4 settings", out.stdout)
        call = (tmp / "data/gated.log").read_text()
        self.assertIn("calibrate.py --base base --candidate runs/2026-11-08/adapter", call)
        self.assertIn("--out runs/2026-11-08/sweep.json --tilts 0 0.1 --alphas 1.0 0.7", call)
        self.assertFalse((tmp / "data/sweep-request.json").exists())
        self.assertEqual(len(list((tmp / "data").glob("sweep-request.done-*.json"))), 1)

    def test_not_allowed_outside_night_window(self):
        tmp, out = self.run_step("0")
        self.assertFalse((tmp / "data/gated.log").exists())
        self.assertTrue((tmp / "data/sweep-request.json").exists())

    def test_no_request_no_run(self):
        tmp, out = self.run_step("1", with_request=False)
        self.assertEqual(out.returncode, 0)
        self.assertFalse((tmp / "data/gated.log").exists())

    def test_runs_from_the_exit_trap_before_teardown(self):
        src = (S1 / "nightly.sh").read_text()
        self.assertIn('cleanup() { local rc=$?; sweep_on_exit "$rc"; stop_deadline;', src)


if __name__ == "__main__":
    unittest.main()
