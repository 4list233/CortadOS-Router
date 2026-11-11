"""#74: a requested S1 calibration sweep completes in one night. calibrate.py (checkpoint fallback, resumable per-version score
cache, one model resident at a time, atomic sweep.json), nightly.sh (sweep first, the rest of the night off, scales from the
request, retention keeps the candidate) and overnight.py (no quota cooldown past the window start while a sweep is pending).
Fakes only: no MLX, no model, no real ~/cortados or ~/.cortados, no Studio."""
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import weakref
from datetime import datetime
from pathlib import Path
from unittest import mock

S1 = Path(__file__).resolve().parents[1] / "s1"
sys.path.insert(0, str(S1))

import calibrate as cal  # noqa: E402
import common  # noqa: E402
import sweep_request as sr  # noqa: E402
import evaluate as ev  # noqa: E402
from common import QUESTIONS, core  # noqa: E402
from student import N_OPTIONS, decode  # noqa: E402
from tests import test_s1_nightly as tn  # noqa: E402  (module only: its TestCases are not re-collected here)

_spec = importlib.util.spec_from_file_location("overnight_sweep_first", S1 / "overnight.py")
night = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(night)


# --- calibrate.py ---------------------------------------------------------------------------------------------------------
def _probs(close: bool):
    """Confident answers, or (close=True) a close call on every difficulty feature so tilts change the answer."""
    p = {q: [1.0 if i == 0 else 0.0 for i in range(N_OPTIONS[q])] for q in QUESTIONS}
    if close:
        for f in core.FEATURES:
            p[f] = [0.55, 0.45] + [0.0] * (N_OPTIONS[f] - 2)
    return p


def _scored(p):
    return {"answers": {q: decode(q, max(range(len(v)), key=v.__getitem__)) for q, v in p.items()}, "probs": p, "ms": 1}


class Interrupted(Exception):
    """Stands in for the window-end TERM landing mid-pass."""


class FakeModel:
    def __init__(self, fleet, adapter):
        self.fleet, self.adapter = fleet, adapter

    def score(self, request, unit_kind):
        f = self.fleet
        if f.fail_after is not None and len(f.calls) >= f.fail_after:
            raise Interrupted()
        f.calls.append((Path(self.adapter).name, request))
        return _scored(_probs(close=request.endswith("1")))


class Fleet:
    """make_scorer stand-in: records each load and score call and, at every load, how many earlier models are still alive
    (weakrefs only, so the fleet itself never keeps one resident)."""

    def __init__(self, fail_after=None):
        self.fail_after, self.loads, self.calls, self.refs, self.alive_at_load, self.releases = fail_after, [], [], [], [], 0

    def __call__(self, adapter, temps=None):
        import gc
        gc.collect()
        self.alive_at_load.append(sum(r() is not None for r in self.refs))
        m = FakeModel(self, adapter)
        self.refs.append(weakref.ref(m))
        self.loads.append(Path(adapter).name)
        return m

    def release(self):
        self.releases += 1


def fake_blend(cand_dir, live_dir, alpha, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "adapters.safetensors").write_text(f"blend {alpha} {live_dir is not None}")
    (out_dir / "adapter_config.json").write_text("{}")
    return out_dir


class SweepFixture:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s1-sweep74-"))
        tv = ev.teacher_view([_scored(_probs(False))["answers"]])
        self.holdout = [({"id": f"r{i}", "request": f"req{i}", "unit_kind": "interactive"}, tv) for i in range(3)]
        self.o_holdout = [({"id": "o1", "request": "out1", "unit_kind": "interactive"}, "R2")]
        self.run = self.tmp / "runs/2026-11-08"
        self.cand = self.run / "adapter"
        self.cand.mkdir(parents=True)
        (self.cand / "adapter_config.json").write_text("{}")
        (self.cand / "0001500_adapters.safetensors").write_text("w")

    def sweep(self, fleet, run=None, **kw):
        run = run or self.run
        args = dict(candidate=str(self.cand), live="live", tilts=[0, 0.1], alphas=[1.0, 0.7], scales=[0.5],
                    out=run / "sweep.json", holdout=self.holdout, o_holdout=self.o_holdout, make_scorer=fleet, fail_up=0.6,
                    candidate_file=str(self.cand / "0001500_adapters.safetensors"), release=fleet.release, log=lambda m: None)
        args.update(kw)
        with mock.patch.object(cal, "blend_adapters", fake_blend):
            return cal.run_sweep(**args)


class ResumableSweep(SweepFixture, unittest.TestCase):
    ROWS = 4          # 3 teacher + 1 outcome rows per adapter version
    VERSIONS = 4      # live, cand, blend0.7, scale0.5

    def test_resume_after_interruption_recomputes_only_missing_rows_and_versions(self):
        first = Fleet(fail_after=self.ROWS + 2)          # live completes, the candidate pass stops after 2 rows
        with self.assertRaises(Interrupted):
            self.sweep(first)
        self.assertFalse((self.run / "sweep.json").exists(), "an interrupted sweep never leaves a sweep.json")
        self.assertFalse((self.run / "sweep.md").exists())
        self.assertTrue((self.run / "sweep/live/scores.done").exists())
        self.assertFalse((self.run / "sweep/cand/scores.done").exists())
        second = Fleet()
        sweep = self.sweep(second)
        self.assertNotIn("live", second.loads, "a completed version is reused, its model never loaded")
        self.assertEqual(len(second.calls), self.ROWS * self.VERSIONS - (self.ROWS + 2))
        self.assertEqual([a for a, _ in second.calls].count(Path(self.cand).name), 2)   # only the 2 missing candidate rows
        third = Fleet()
        again = self.sweep(third)
        self.assertEqual((third.loads, third.calls), ([], []))    # all cached: a rerun loads nothing
        # the resumed result is the uninterrupted one
        fresh = self.sweep(Fleet(), run=self.tmp / "runs/fresh")
        strip = lambda s: [{k: v for k, v in r.items() if k != "adapter"} for r in s["settings"]]
        self.assertEqual(strip(sweep), strip(fresh))
        self.assertEqual(strip(again), strip(fresh))
        self.assertEqual(len(sweep["settings"]), 3 * 2)           # cand, blend0.7, scale0.5 x 2 tilts
        self.assertEqual(sweep["request"], {"tilts": [0, 0.1], "alphas": [1.0, 0.7], "scales": [0.5]})

    def test_a_truncated_cache_line_is_rescored_not_trusted(self):
        with self.assertRaises(Interrupted):
            self.sweep(Fleet(fail_after=self.ROWS + 2))
        scores = self.run / "sweep/cand/scores.jsonl"
        scores.write_text(scores.read_text() + '{"k": "h:r2:abc", "p": {"answ')   # a kill mid-write
        f = Fleet()
        self.sweep(f)
        self.assertEqual([a for a, _ in f.calls].count(Path(self.cand).name), 2)

    def test_a_torn_last_line_survives_two_restarts(self):
        with self.assertRaises(Interrupted):
            self.sweep(Fleet(fail_after=self.ROWS + 2))           # live done, 2 candidate rows
        scores = self.run / "sweep/cand/scores.jsonl"
        scores.write_text(scores.read_text() + '{"k": "h:r2:abc", "p": {"answ')   # a kill mid-write
        with self.assertRaises(Interrupted):
            self.sweep(Fleet(fail_after=1))                      # restart 1: one more candidate row, then stopped again
        self.assertTrue(scores.read_text().endswith("\n"))
        self.assertNotIn('"p": {"answ{', scores.read_text(), "the next record was not glued onto the fragment")
        f = Fleet()
        self.sweep(f)                                            # restart 2: the row scored on restart 1 is still cached
        self.assertEqual([a for a, _ in f.calls].count(Path(self.cand).name), 1)

    def _live(self, cfg='{"lora_parameters": {"scale": 20}}'):
        live = self.tmp / "live"
        live.mkdir(exist_ok=True)
        (live / "adapter_config.json").write_text(cfg)
        (live / "adapters.safetensors").write_text("L")
        return str(live)

    def test_adapter_config_change_invalidates_exactly_the_versions_built_from_it(self):
        live = self._live()
        self.sweep(Fleet(), live=live)
        (self.cand / "adapter_config.json").write_text('{"lora_parameters": {"scale": 10}}')   # other LoRA scale, same weights
        f = Fleet()
        self.sweep(f, live=live)
        self.assertNotIn("live", f.loads)
        self.assertEqual(len(f.calls), self.ROWS * (self.VERSIONS - 1))
        stale = list((self.run / "sweep/cand").glob("stale-*"))
        self.assertEqual(len(stale), 1)
        self.assertEqual(sorted(p.name for p in stale[0].iterdir()), ["scores.done", "scores.jsonl", "scores.meta.json",
                                                                         "scores.provenance.json"])
        self._live('{"lora_parameters": {"scale": 16}}')
        f = Fleet()
        self.sweep(f, live=live)
        self.assertEqual(sorted(set(f.loads)), ["blend0.7", "live"])   # the blend reads the live adapter; cand / scale do not

    def test_sweep_records_each_versions_provenance(self):
        live = self._live()
        sweep = self.sweep(Fleet(), live=live)
        blend = sweep["versions"]["blend0.7"]
        bdir = self.run / "sweep/blend0.7"
        self.assertEqual(blend["adapter_sha256"], cal.file_sha256(bdir / "adapters.safetensors"))
        self.assertEqual(blend["adapter_config_sha256"], cal.file_hash(bdir / "adapter_config.json"))
        self.assertEqual(blend["adapter_config"], {})
        self.assertEqual(sweep["versions"]["live"]["adapter_config"], {"lora_parameters": {"scale": 20}})
        self.assertEqual(json.loads((bdir / "scores.provenance.json").read_text()), blend)
        again = self.sweep(Fleet(), live=live)                      # all cached: provenance read back, nothing loaded
        self.assertEqual(again["versions"], sweep["versions"])

    def test_resumed_rows_scored_with_another_artifact_are_rescored(self):
        with self.assertRaises(Interrupted):
            self.sweep(Fleet(fail_after=self.ROWS + 2))           # live done, 2 candidate rows cached
        prov = self.run / "sweep/cand/scores.provenance.json"
        rec = json.loads(prov.read_text())
        prov.write_text(json.dumps(dict(rec, T={"steps": 9.0})))  # those rows were scored with other temperatures
        f = Fleet()
        self.sweep(f)
        self.assertEqual([a for a, _ in f.calls].count(Path(self.cand).name), self.ROWS, "all candidate rows rescored")
        self.assertEqual(json.loads(prov.read_text()), rec)
        self.assertTrue(list((self.run / "sweep/cand").glob("stale-*/scores.provenance.json")), "moved, not deleted")

    def test_cached_rows_without_provenance_are_rescored_when_scoring_resumes(self):
        with self.assertRaises(Interrupted):
            self.sweep(Fleet(fail_after=self.ROWS + 2))
        (self.run / "sweep/cand/scores.provenance.json").unlink()   # rows cached by a version that recorded none
        f = Fleet()
        self.sweep(f)
        self.assertEqual([a for a, _ in f.calls].count(Path(self.cand).name), self.ROWS)

    def test_a_complete_version_without_provenance_is_reused_but_recorded_as_null(self):
        self.sweep(Fleet())
        (self.run / "sweep/blend0.7/scores.provenance.json").unlink()
        f = Fleet()
        sweep = self.sweep(f)
        self.assertEqual(f.loads, [])
        self.assertIsNone(sweep["versions"]["blend0.7"])

    def test_validation_change_refits_the_candidate_versions_and_moves_their_temps_aside(self):
        self.sweep(Fleet(), fit_identity={"valid": "A"})
        temps = self.run / "sweep/cand/temps.json"
        temps.write_text(json.dumps({"valid": "A", "T": {"all": 2.0}}))
        f = Fleet()
        self.sweep(f)
        self.assertEqual(len(f.calls), self.ROWS * (self.VERSIONS - 1))   # dropping / changing the key also invalidates
        f = Fleet()
        self.sweep(f, fit_identity={"valid": "B"})
        self.assertNotIn("live", f.loads, "the live adapter's temperatures are the pointer's: the validation rows do not touch it")
        self.assertEqual(len(f.calls), self.ROWS * (self.VERSIONS - 1))
        self.assertFalse(temps.exists())
        self.assertTrue(any((d / "temps.json").exists() for d in (self.run / "sweep/cand").glob("stale-*")), "moved, not deleted")

    def test_changed_identity_starts_the_version_over(self):
        self.sweep(Fleet())
        f = Fleet()
        self.sweep(f, fail_up=0.7)       # other failed-up threshold: every cached score is stale
        self.assertEqual(len(f.calls), self.ROWS * self.VERSIONS)
        (self.cand / "0001500_adapters.safetensors").write_text("retrained, other size")
        f = Fleet()
        self.sweep(f, fail_up=0.7)
        self.assertNotIn("live", f.loads)                        # the live adapter did not change
        self.assertEqual(len(f.calls), self.ROWS * (self.VERSIONS - 1))

    def test_new_holdout_rows_are_scored_without_rescoring_the_rest(self):
        self.sweep(Fleet())
        self.holdout.append(({"id": "r9", "request": "req9", "unit_kind": "interactive"}, self.holdout[0][1]))
        f = Fleet()
        self.sweep(f)
        self.assertEqual(len(f.calls), self.VERSIONS)              # one new row per version

    def test_one_model_resident_at_a_time(self):
        f = Fleet()
        self.sweep(f)
        self.assertEqual(len(f.loads), self.VERSIONS)
        self.assertEqual(f.alive_at_load, [0] * self.VERSIONS, "every earlier model was freed before the next loaded")
        self.assertEqual(f.releases, self.VERSIONS)                # gc + MLX cache cleared after each pass

    def test_sweep_json_is_atomic_and_written_last(self):
        order = []
        real = cal.write_atomic

        def spy(path, text):
            order.append(Path(path).name)
            real(path, text)
        with mock.patch.object(cal, "write_atomic", spy):
            self.sweep(Fleet())
        self.assertEqual(order[-2:], ["sweep.md", "sweep.json"])
        self.assertEqual([p.name for p in self.run.iterdir() if p.name.startswith(".")], [], "no temp file left")
        json.loads((self.run / "sweep.json").read_text())

    def test_write_atomic_failure_keeps_the_old_file(self):
        p = self.tmp / "sweep.json"
        p.write_text("old")
        with mock.patch.object(cal.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cal.write_atomic(p, "new")
        self.assertEqual(p.read_text(), "old")

    def test_fitted_temperatures_are_cached_for_a_resume(self):
        fits = []

        class S:
            def __init__(self, base, adapter=None, temperatures=None):
                self.T = temperatures or {}
        live = self.tmp / "live"
        with mock.patch.object(ev, "Scorer", S), mock.patch.object(ev, "fit_temperatures", lambda s, v: fits.append(1) or {"all": 2.0}):
            make = cal.scorer_factory(live=str(live), base="b", valid=[], pointer={"adapter": str(live), "base": "b"})
            temps = self.tmp / "v/temps.json"
            self.assertEqual(make(str(self.cand), temps).T, {"all": 2.0})
            self.assertEqual(make(str(self.cand), temps).T, {"all": 2.0})
            self.assertEqual(len(fits), 1)
            self.assertEqual(json.loads(temps.read_text()), {"valid": cal.data_hash([]), "T": {"all": 2.0}})
            other = cal.scorer_factory(live=str(live), base="b", valid=[({"id": "v1"}, [])],
                                       pointer={"adapter": str(live), "base": "b"})
            other(str(self.cand), temps)                           # other validation rows: refitted, not reused
            self.assertEqual(len(fits), 2)
            temps.write_text(json.dumps({"all": 3.0}))             # the unkeyed format of the first version: refitted
            make(str(self.cand), temps)
            self.assertEqual(len(fits), 3)


class CandidateResolution(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix="s1-cand-")) / "runs/2026-11-08"
        self.dir = self.run / "adapter"
        self.dir.mkdir(parents=True)
        (self.dir / "adapter_config.json").write_text("{}")

    def touch(self, *names):
        for n in names:
            (self.dir / n).write_text("w")

    def test_highest_numbered_checkpoint_when_the_final_file_is_missing(self):
        self.touch("0000000_adapters.safetensors", "0000100_adapters.safetensors", "0000900_adapters.safetensors",
                   "0001500_adapters.safetensors", "0002000_adapters.safetensors.old", "resume_100_adapters.safetensors")
        (self.run / "segment-target").write_text("1500\n")
        path, note = cal.resolve_candidate(self.dir)
        self.assertEqual(path, self.dir / "0001500_adapters.safetensors")
        self.assertIn("using checkpoint 0001500_adapters.safetensors (step 1500; run segment-target 1500)", note)
        self.assertNotIn("did NOT reach", note)

    def test_short_checkpoint_says_the_segment_did_not_finish(self):
        self.touch("0000900_adapters.safetensors")
        (self.run / "segment-target").write_text("1500\n")
        self.assertIn("did NOT reach its target", cal.resolve_candidate(self.dir)[1])

    def test_final_adapter_wins(self):
        self.touch("0001500_adapters.safetensors", "adapters.safetensors")
        self.assertEqual(cal.resolve_candidate(self.dir)[0], self.dir / "adapters.safetensors")

    def test_refuses_without_weights_or_with_only_the_warm_start_copy_or_without_config(self):
        with self.assertRaisesRegex(cal.CandidateError, "no adapters.safetensors and no NNNNNNN"):
            cal.resolve_candidate(self.dir)
        self.touch("0000000_adapters.safetensors")
        with self.assertRaises(cal.CandidateError):
            cal.resolve_candidate(self.dir)
        (self.dir / "adapter_config.json").unlink()
        self.touch("0000100_adapters.safetensors")
        with self.assertRaisesRegex(cal.CandidateError, "adapter_config.json"):
            cal.resolve_candidate(self.dir)

    def test_checkpoint_is_loaded_in_place_and_the_adapter_dir_is_never_written(self):
        self.touch("0001500_adapters.safetensors")
        before = sorted((p.name, p.stat().st_mtime_ns) for p in self.dir.iterdir())
        path, _ = cal.resolve_candidate(self.dir)
        view = cal.candidate_view(self.dir, path, self.run / "sweep/cand-weights")
        self.assertEqual(Path(os.readlink(view / "adapters.safetensors")), path.resolve())
        self.assertEqual(Path(os.readlink(view / "adapter_config.json")), (self.dir / "adapter_config.json").resolve())
        self.assertEqual(cal.candidate_view(self.dir, path, self.run / "sweep/cand-weights"), view)   # idempotent
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in self.dir.iterdir()), before)
        self.touch("adapters.safetensors")
        self.assertEqual(cal.candidate_view(self.dir, self.dir / "adapters.safetensors", self.run / "x"), self.dir)

    def test_checkpoint_symlink_leaving_the_run_is_refused(self):
        outside = self.run.parent.parent / "elsewhere_adapters.safetensors"
        outside.write_text("someone else's weights")
        (self.dir / "0001500_adapters.safetensors").symlink_to(outside)
        with self.assertRaisesRegex(cal.CandidateError, "outside the candidate adapter dir"):
            cal.resolve_candidate(self.dir)
        with self.assertRaises(cal.CandidateError):
            cal.candidate_view(self.dir, self.dir / "0001500_adapters.safetensors", self.run / "sweep/cand-weights")
        self.assertFalse((self.run / "sweep").exists())
        with mock.patch("builtins.print", lambda *a, **k: None):
            rc = cal.main(["--candidate", str(self.dir), "--live", "x", "--out", str(self.run / "sweep.json")])
        self.assertEqual(rc, cal.EXIT_REFUSED)
        self.assertFalse((self.run / "sweep").exists())

    def test_checkpoint_symlink_onto_the_warm_start_copy_is_refused(self):
        self.touch("0000000_adapters.safetensors")
        (self.dir / "0001500_adapters.safetensors").symlink_to(self.dir / "0000000_adapters.safetensors")
        with self.assertRaisesRegex(cal.CandidateError, "warm-start copy"):
            cal.resolve_candidate(self.dir)
        (self.dir / "adapters.safetensors").symlink_to(self.dir / "0000000_adapters.safetensors")
        with self.assertRaisesRegex(cal.CandidateError, "warm-start copy"):
            cal.resolve_candidate(self.dir)

    def test_main_refuses_with_exit_4_before_loading_anything(self):
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            rc = cal.main(["--candidate", str(self.dir), "--live", "x", "--out", str(self.run / "sweep.json")])
        self.assertEqual(rc, cal.EXIT_REFUSED)
        self.assertTrue(any(line.startswith("sweep refused:") for line in out), out)
        self.assertFalse((self.run / "sweep").exists())

    def test_cli_default_has_no_extra_scales(self):
        self.assertEqual([n for n, _ in cal.variants_for([1.0, 0.85, 0.7, 0.5], [])],
                         ["live", "cand", "blend0.85", "blend0.7", "blend0.5"])    # the request's 24 configs: 5 passes


class CheckDone(SweepFixture, unittest.TestCase):
    """calibrate.py --check-done (nightly.sh's "already complete"): 10 only for a sweep.json whose input fingerprint equals
    what the same arguments give now; reads files only."""

    def setUp(self):
        super().setUp()
        self.valid = [({"id": "v1", "request": "val1", "unit_kind": "interactive", "split": "train"}, [{"q": 1}])]
        self.live = self.tmp / "live"
        self.live.mkdir()
        (self.live / "adapter_config.json").write_text("{}")
        (self.live / "adapters.safetensors").write_text("L")
        self.pointer = self.tmp / "current.json"
        self.pointer.write_text(json.dumps({"adapter": str(self.live), "base": "b", "temperatures": {"all": 1.0}}))

    def main(self, *extra):
        argv = ["--candidate", str(self.cand), "--live", str(self.live), "--out", str(self.run / "sweep.json"), "--base", "b",
                "--tilts", "0", "0.1", "--alphas", "1.0", "0.7", "--fail-up", "0.6", *extra]
        with mock.patch.object(common, "read_jsonl", lambda p: []), mock.patch.object(common, "read_labels", lambda: []), \
                mock.patch.object(ev, "build_eval_sets", lambda reqs, labels: (self.holdout, self.valid)), \
                mock.patch.object(ev, "outcome_holdout", lambda: self.o_holdout), \
                mock.patch.object(ev, "current_pointer", lambda: self.pointer), \
                mock.patch.object(cal, "scorer_factory", lambda **kw: Fleet()), \
                mock.patch.object(cal, "blend_adapters", fake_blend), mock.patch("builtins.print", lambda *a, **k: None):
            return cal.main(list(argv))

    def check(self, *extra):
        return self.main("--check-done", *extra)

    def test_only_a_sweep_of_exactly_these_inputs_is_complete(self):
        self.assertEqual(self.check(), 0, "no sweep.json yet")
        self.assertEqual(self.main(), 0)
        self.assertIsInstance(json.loads((self.run / "sweep.json").read_text())["fingerprint"], dict)
        snapshot = sorted(str(p) for p in self.tmp.rglob("*"))
        self.assertEqual(self.check(), cal.EXIT_ALREADY_DONE)
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), snapshot, "--check-done writes nothing")

        def stale(change, undo, why):
            change()
            with self.subTest(why):
                self.assertEqual(self.check(), 0, why)
            undo()
            self.assertEqual(self.check(), cal.EXIT_ALREADY_DONE, f"undone: {why}")

        w = self.cand / "0001500_adapters.safetensors"
        st = w.stat()

        def rewrite(text):          # same size and the old mtime: only the content hash can tell
            w.write_text(text)
            os.utime(w, ns=(st.st_atime_ns, st.st_mtime_ns))
        stale(lambda: rewrite("v"), lambda: rewrite("w"), "candidate weights rewritten (same size, same mtime)")
        new = self.cand / "0002000_adapters.safetensors"
        stale(lambda: new.write_text("w"), new.unlink, "the candidate run advanced to a later checkpoint")
        cfg = self.cand / "adapter_config.json"
        stale(lambda: cfg.write_text('{"x": 1}'), lambda: cfg.write_text("{}"), "candidate adapter_config.json changed")
        lcfg = self.live / "adapter_config.json"
        stale(lambda: lcfg.write_text('{"x": 1}'), lambda: lcfg.write_text("{}"), "live adapter_config.json changed")
        row = ({"id": "r9", "request": "req9", "unit_kind": "interactive"}, self.holdout[0][1])
        stale(lambda: self.holdout.append(row), self.holdout.pop, "holdout changed")
        orow = ({"id": "o2", "request": "out2", "unit_kind": "interactive"}, "R1")
        stale(lambda: self.o_holdout.append(orow), self.o_holdout.pop, "outcome holdout changed")
        vrow = ({"id": "v2", "request": "val2", "unit_kind": "interactive", "split": "train"}, [{"q": 0}])
        stale(lambda: self.valid.append(vrow), self.valid.pop, "validation rows changed")
        ptr = self.pointer.read_text()
        stale(lambda: self.pointer.write_text(ptr.replace("1.0", "1.5")), lambda: self.pointer.write_text(ptr),
              "live pointer temperatures changed")
        p = mock.patch.object(ev, "OUTWARD_AT", ev.OUTWARD_AT + 0.1)
        stale(p.start, p.stop, "outward threshold changed")
        for name, bump in (("MIN_TEACHER_HOLDOUT", 1), ("MIN_OUTCOME_HOLDOUT", 1), ("OUTCOME_OVER_MIN_DROP", 0.01),
                           ("TEACHER_UNDER_MAX_RISE", 0.01), ("OUTWARD_RECALL_FLOOR", 0.01), ("_EPS", 1e-9)):
            p = mock.patch.object(ev, name, getattr(ev, name) + bump)
            stale(p.start, p.stop, f"promotion constant {name} changed")
        with self.subTest("fail-up threshold changed"):
            self.assertEqual(self.check("--fail-up", "0.7"), 0)
        with self.subTest("request changed"):
            self.assertEqual(self.check("--scales", "0.5"), 0)
        v = mock.patch.object(cal, "CALIBRATE_VERSION", cal.CALIBRATE_VERSION + 1)
        stale(v.start, v.stop, "calibrate.py version changed")

    def test_raising_the_teacher_holdout_minimum_after_a_finished_sweep_runs_it_again(self):
        """#74 review: CORTADOS_S1_MIN_HOLDOUT is read at import; a sweep whose reports were computed under the old minimum is
        not complete once the minimum changes."""
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.check(), cal.EXIT_ALREADY_DONE)
        old = ev.MIN_TEACHER_HOLDOUT
        try:
            with mock.patch.dict(os.environ, {"CORTADOS_S1_MIN_HOLDOUT": str(old + 50)}):
                importlib.reload(ev)       # the environment is read once, as a fresh calibrate.py process would
                self.assertEqual(ev.MIN_TEACHER_HOLDOUT, old + 50)
                self.assertEqual(self.check(), 0)
        finally:
            importlib.reload(ev)
        self.assertEqual(ev.MIN_TEACHER_HOLDOUT, old)
        self.assertEqual(self.check(), cal.EXIT_ALREADY_DONE)

    def test_a_sweep_json_without_a_fingerprint_is_not_complete(self):
        self.main()
        out = self.run / "sweep.json"
        old = json.loads(out.read_text())
        old.pop("fingerprint")
        out.write_text(json.dumps(old))                     # written before fingerprints existed; request and live match
        self.assertEqual(self.check(), 0)


class RequestValidation(unittest.TestCase):
    def argv(self, **r):
        return sr.validate({"candidate": "runs/c/adapter", "live": "runs/l/adapter", **r})

    def test_valid_request_gives_the_calibrate_argv(self):
        self.assertEqual(self.argv(tilts=[0, 0.1], alphas=[1.0, 0.5]),
                         ["--candidate", "runs/c/adapter", "--live", "runs/l/adapter", "--out", "runs/c/sweep.json",
                          "--tilts", "0", "0.1", "--alphas", "1.0", "0.5", "--scales"])
        self.argv(tilts=[sr.MAX_TILT], alphas=[1], scales=[0.5, 1.0])

    def test_out_of_range_non_finite_empty_and_oversized_grids_are_invalid(self):
        bad = [dict(alphas=[2]), dict(alphas=[0]), dict(alphas=[-0.5]), dict(tilts=[-0.1]), dict(tilts=[sr.MAX_TILT + 0.5]),
               dict(alphas=[float("nan")]), dict(tilts=[float("inf")]), dict(scales=[0]), dict(scales=[1.5]),
               dict(alphas=[]), dict(tilts=[]), dict(tilts=[i / 100 for i in range(sr.MAX_GRID + 1)]),
               dict(alphas=[1.0, 1]), dict(alphas=[True]), dict(tilts="0.1"), dict(candidate="a\nb"), dict(live=""),
               dict(candidate="x" * (sr.MAX_PATH + 1))]
        for b in bad:
            with self.subTest(b), self.assertRaises(ValueError):
                self.argv(**b)

    def test_calibrate_rejects_the_same_grids_with_exit_2(self):
        for extra in (["--alphas", "2"], ["--tilts", "nan"], ["--tilts", "-0.1"], ["--scales", "0"], ["--alphas"]):
            with self.subTest(extra), mock.patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
                cal.main(["--candidate", "c", "--live", "l", "--out", "o/sweep.json", *extra])
            self.assertEqual(cm.exception.code, 2)


# --- overnight.py -----------------------------------------------------------------------------------------------------------
class SweepCooldown(unittest.TestCase):
    W = dict(window_raw="01:00-07:00", switch_raw="1")

    def test_evening_quota_stop_waits_only_until_the_window_start(self):
        d, note = night.sweep_delay(18000, 0, "quota", datetime(2026, 11, 8, 22, 0), **self.W)
        self.assertEqual(d, 3 * 3600 + 1)
        self.assertIn("night window start 01:00", note)

    def test_inside_the_window_a_sweep_only_cycle_needs_no_cooldown(self):
        self.assertEqual(night.sweep_delay(18000, 0, "quota", datetime(2026, 11, 9, 3, 0), **self.W)[0], 0.0)
        self.assertEqual(night.sweep_delay(18000, 0, "quota", datetime(2026, 11, 9, 14, 0), window_raw="off",
                                           switch_raw="0")[0], 0.0)    # training allowed any time: same

    def test_failure_backoff_inside_the_window_and_short_delays_are_kept(self):
        self.assertEqual(night.sweep_delay(900, 1, "idle", datetime(2026, 11, 9, 3, 0), **self.W), (900, None))
        self.assertEqual(night.sweep_delay(900, 0, "idle", datetime(2026, 11, 8, 14, 0), **self.W), (900, None))
        self.assertEqual(night.sweep_delay(4 * 3600, 3, "idle", datetime(2026, 11, 9, 0, 30), **self.W)[0], 1801)

    def test_aware_clock_counts_absolute_seconds_across_dst(self):
        # 2026-11-01: Toronto falls back at 02:00; from 23:00 EDT on Oct 31 to 03:00 EST on Nov 1 is 5 h, not the 4 h on the wall
        now = datetime(2026, 10, 31, 23, 0, tzinfo=night.TORONTO)
        self.assertEqual(night.sweep_delay(6 * 3600, 0, "quota", now, zone=night.TORONTO, window_raw="03:00-07:00",
                                           switch_raw="1")[0], 5 * 3600 + 1)

    def _quota_cycle(self, tmp, with_request, wall):
        cmd = [sys.executable, "-c", "import os; open(os.environ['CORTADOS_S1_CYCLE_RESULT'], 'w').write('quota')"]
        g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, max_pause_s=1800,
                        signaller=lambda pid, s: None, log=lambda m: None, alerter=lambda n, d: None, wall=lambda: wall,
                        night_window=night.parse_night_window("01:00-07:00"), night_budget_s=6.0)
        req = Path(tmp) / "sweep-request.json"
        if with_request:
            req.write_text("{}")
        sleeps = []
        night.run_continuous(cmd, g, result_file=Path(tmp) / "r", idle_s=900, quota_s=18000, sleep=sleeps.append, cycles=1,
                             poll_s=0, sweep_request=req)
        return sleeps[-1]

    def test_run_continuous_caps_the_quota_cooldown_only_while_a_sweep_is_pending(self):
        env = {"CORTADOS_S1_TRAIN_ONLY_IN_WINDOW": "1", "CORTADOS_S1_NIGHT_WINDOW": "01:00-07:00"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            self.assertEqual(self._quota_cycle(tmp, True, datetime(2026, 11, 8, 22, 0)), 3 * 3600 + 1)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            self.assertEqual(self._quota_cycle(tmp, False, datetime(2026, 11, 8, 22, 0)), 18000)

    def test_cycle_is_told_the_window_end(self):
        env = {"CORTADOS_S1_TRAIN_ONLY_IN_WINDOW": "1", "CORTADOS_S1_NIGHT_WINDOW": "01:00-07:00"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            out = Path(tmp) / "until"
            cmd = [sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.environ['CORTADOS_S1_TRAIN_UNTIL'])"]
            for wall, want in ((datetime(2026, 11, 9, 3, 0), str(int(datetime(2026, 11, 9, 7, 0).timestamp()))),
                               (datetime(2026, 11, 9, 14, 0), "")):
                g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0,
                                max_pause_s=1800, signaller=lambda pid, s: None, log=lambda m: None,
                                alerter=lambda n, d: None, wall=lambda: wall,
                                night_window=night.parse_night_window("01:00-07:00"), night_budget_s=6.0)
                night.run_cycle(cmd, g, result_file=Path(tmp) / "r", poll_s=0, sleep=lambda s: None)
                self.assertEqual(out.read_text(), want)


# --- nightly.sh -------------------------------------------------------------------------------------------------------------
CAL_STUB = (
    "import json, os, sys\n"
    "a = sys.argv[1:]\n"
    "if '--check-done' in a:\n"           # stands in for the fingerprint comparison (tested above against the real calibrate.py)
    "    open('data/check-count', 'a').write('x')\n"
    "    try:\n"
    "        fp = json.load(open(a[a.index('--out') + 1])).get('fingerprint')\n"
    "    except (OSError, ValueError):\n"
    "        fp = None\n"
    "    if fp == 'now':\n"
    "        print('sweep check: a finished sweep of exactly these inputs'); sys.exit(10)\n"
    "    print('sweep check: ' + ('no finished sweep' if fp is None else 'computed from other inputs'))\n"
    "    sys.exit(int(os.environ.get('STUB_CHECK_RC', '0')))\n"
    "open('data/sweep-count', 'a').write('x')\n"
    "open('data/sweep-args', 'w').write(' '.join(sys.argv[1:]))\n"
    "rc = int(os.environ.get('STUB_CAL_RC', '0'))\n"
    "if rc == 4: print('sweep refused: candidate runs/2026-11-08/adapter has no adapters.safetensors and no checkpoint'); sys.exit(4)\n"
    "print('candidate runs/2026-11-08/adapter has no adapters.safetensors; using checkpoint 0001500_adapters.safetensors "
    "(step 1500; run segment-target 1500)')\n"
    "if rc: print('Traceback: boom'); sys.exit(rc)\n"
    "print('sweep: 24 settings; chosen on teacher: cand-t0.1; would promote: False')\n"
)


class SweepFirstNightly(unittest.TestCase):
    setUp = tn.NightlyEndToEnd.setUp
    run_night = tn.NightlyEndToEnd.run_night
    _cycle = tn.NightlyEndToEnd._cycle

    def request(self, **extra):
        (self.s1 / "calibrate.py").write_text(CAL_STUB)
        req = self.s1 / "data/sweep-request.json"
        req.write_text(json.dumps({"candidate": "runs/2026-11-08/adapter", "live": "runs/2026-10-16-0130/adapter",
                                   "tilts": [0, 0.05, 0.1, 0.15, 0.2, 0.3], "alphas": [1.0, 0.85, 0.7, 0.5], **extra}))
        return req

    def lora(self):
        p = Path(self.env["HOME"]) / "lora.log"
        return p.read_text() if p.exists() else ""

    def test_pending_sweep_runs_first_and_takes_the_rest_of_the_night(self):
        req = self.request()
        until = int(time.time()) + 3600
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=1, CORTADOS_S1_TRAIN_UNTIL=until)
        self.assertEqual((rc, res), (0, "worked"), log)
        self.assertLess(log.index("the sweep runs first"), log.index("sweep: 24 settings"))
        for marker in ("label-args", "ran-dataset", "ran-build-sft", "eval-count"):
            self.assertFalse((self.s1 / "data" / marker).exists(), f"{marker}: nothing but the sweep runs\n{log}")
        self.assertEqual(self.lora(), "", log)
        self.assertIn("using checkpoint 0001500_adapters.safetensors", log)      # which weights the sweep used
        self.assertFalse(req.exists())
        done = list((self.s1 / "data").glob("sweep-request.done-*.json"))
        self.assertEqual(len(done), 1, log)
        rec = json.loads(done[0].read_text())
        self.assertRegex(rec["done_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertEqual(rec["candidate"], "runs/2026-11-08/adapter")
        self.assertEqual((self.s1 / "data/sweep-night-until").read_text().strip(), str(until))
        # the next cycle the same night labels but trains nothing
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=1, CORTADOS_S1_TRAIN_UNTIL=until)
        self.assertEqual(rc, 0, log)
        self.assertTrue((self.s1 / "data/label-args").exists(), log)
        self.assertIn("tonight's window went to the calibration sweep", log)
        self.assertEqual(self.lora(), "", log)
        self.assertEqual((self.s1 / "data/sweep-count").read_text(), "x")
        # the next night: normal cycles resume
        (self.s1 / "data/sweep-night-until").write_text(f"{int(time.time()) - 10}\n")
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=1)
        self.assertEqual(rc, 0, log)
        self.assertIn("training tonight", log)
        self.assertIn("--train", self.lora())
        self.assertFalse((self.s1 / "data/sweep-night-until").exists())

    def test_scales_come_from_the_request(self):
        self.request(scales=[0.5])
        self.run_night(STUB_NEW_LABELS=0)
        self.assertTrue((self.s1 / "data/sweep-args").read_text().endswith("--alphas 1.0 0.85 0.7 0.5 --scales 0.5"))
        self.request()
        self.run_night(STUB_NEW_LABELS=0)
        self.assertTrue((self.s1 / "data/sweep-args").read_text().endswith("--alphas 1.0 0.85 0.7 0.5 --scales"))

    def test_a_completed_sweep_json_marks_the_request_done_without_a_run(self):
        req = self.request()
        r = json.loads(req.read_text())
        (self.s1 / "runs/2026-11-08").mkdir(parents=True)
        (self.s1 / "runs/2026-11-08/sweep.json").write_text(json.dumps(
            {"live": r["live"], "request": {"tilts": r["tilts"], "alphas": [1, 0.85, 0.7, 0.5], "scales": []},
             "fingerprint": "now"}))
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5)
        self.assertEqual(rc, 0, log)
        self.assertFalse((self.s1 / "data/sweep-count").exists(), log)
        self.assertEqual(len(list((self.s1 / "data").glob("sweep-request.done-*.json"))), 1, log)
        self.assertIn("training tonight", log)                 # nothing was swept: the night trains

    def test_a_sweep_json_of_other_inputs_for_the_same_request_still_runs(self):
        # same live path and grid (what the old shortcut compared), but the fingerprint says the inputs changed, or is absent
        for fp in ("before-the-candidate-advanced", None):
            with self.subTest(fp=fp):
                req = self.request()
                r = json.loads(req.read_text())
                (self.s1 / "runs/2026-11-08").mkdir(parents=True, exist_ok=True)
                done = {"live": r["live"], "request": {"tilts": r["tilts"], "alphas": r["alphas"], "scales": []}}
                if fp:
                    done["fingerprint"] = fp
                (self.s1 / "runs/2026-11-08/sweep.json").write_text(json.dumps(done))
                (self.s1 / "data/sweep-count").unlink(missing_ok=True)
                rc, log, _ = self.run_night(STUB_NEW_LABELS=0)
                self.assertEqual(rc, 0, log)
                self.assertEqual((self.s1 / "data/sweep-count").read_text(), "x", log)
                self.assertIn("sweep check: ", log)
                self.assertIn("sweep finished", log)

    def test_a_different_request_for_the_same_candidate_still_runs(self):
        self.request(scales=[0.5])
        (self.s1 / "runs/2026-11-08").mkdir(parents=True)
        (self.s1 / "runs/2026-11-08/sweep.json").write_text(json.dumps(
            {"live": "runs/2026-10-16-0130/adapter", "request": {"tilts": [0], "alphas": [1.0], "scales": []}}))
        self.run_night(STUB_NEW_LABELS=0)
        self.assertEqual((self.s1 / "data/sweep-count").read_text(), "x")

    def test_refused_request_is_set_aside_and_the_night_trains(self):
        self.request()
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5, STUB_CAL_RC=4)
        self.assertEqual(rc, 0, log)
        self.assertIn("SWEEP REFUSED", log)
        self.assertIn("sweep refused: candidate", log)
        self.assertEqual(len(list((self.s1 / "data").glob("sweep-request.refused-*.json"))), 1, log)
        self.assertFalse((self.s1 / "data/sweep-request.json").exists())
        self.assertIn("training tonight", log)

    def test_invalid_request_is_set_aside_and_the_night_trains(self):
        (self.s1 / "calibrate.py").write_text(CAL_STUB)
        (self.s1 / "data/sweep-request.json").write_text(json.dumps({"candidate": 5, "live": "x"}))
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5)
        self.assertEqual(rc, 0, log)
        self.assertIn("SWEEP REQUEST INVALID", log)
        self.assertEqual(len(list((self.s1 / "data").glob("sweep-request.invalid-*.json"))), 1, log)
        self.assertFalse((self.s1 / "data/sweep-count").exists())
        self.assertIn("training tonight", log)

    def test_numerically_invalid_requests_are_set_aside_before_calibrate_and_the_night_trains(self):
        for i, bad in enumerate((dict(alphas=[2]), dict(tilts=[-0.1]), dict(alphas=[float("nan")]), dict(alphas=[]),
                                 dict(scales=[0]))):
            with self.subTest(bad=bad):
                for f in (self.s1 / "data").glob("sweep-request.invalid-*.json"):
                    f.unlink()
                self.request(**bad)
                rc, log, _ = self.run_night(STUB_NEW_LABELS=5 if i == 0 else 0)
                self.assertEqual(rc, 0, log)
                self.assertIn("SWEEP REQUEST INVALID: sweep request invalid:", log)
                aside = list((self.s1 / "data").glob("sweep-request.invalid-*.json"))
                self.assertEqual(len(aside), 1, log)
                self.assertFalse((self.s1 / "data/sweep-request.json").exists())
                self.assertFalse((self.s1 / "data/sweep-count").exists(), log)
                self.assertFalse((self.s1 / "data/check-count").exists(), "calibrate.py is never started for it")
                if i == 0:
                    self.assertIn("training tonight", log)
                    self.assertIn("--train", self.lora())
                    self.assertIn("invalid_at", json.loads(aside[0].read_text()))

    def test_calibrate_exit_2_sets_the_request_aside_instead_of_retrying(self):
        for env, ran in ((dict(STUB_CHECK_RC=2), False), (dict(STUB_CAL_RC=2), True)):
            with self.subTest(env=env):
                for f in (self.s1 / "data").glob("sweep-request.invalid-*.json"):
                    f.unlink()
                (self.s1 / "data/sweep-count").unlink(missing_ok=True)
                self.request()
                rc, log, _ = self.run_night(STUB_NEW_LABELS=5, **env)
                self.assertEqual(rc, 0, log)
                self.assertIn("calibrate.py rejected the arguments (exit 2)", log)
                self.assertEqual(len(list((self.s1 / "data").glob("sweep-request.invalid-*.json"))), 1, log)
                self.assertEqual((self.s1 / "data/sweep-count").exists(), ran, log)
                self.assertIn("training tonight", log)

    def test_missing_or_unreadable_calibrate_is_a_failed_night_not_an_invalid_request(self):
        """#74 review: python exits 2 when it cannot open calibrate.py, the same code as a rejected request. A broken install
        is retried and counted toward SWEEP_FAIL_NIGHTS; the valid request is never set aside as invalid."""
        cal_py = self.s1 / "calibrate.py"

        def missing():
            cal_py.unlink()

        def unreadable():
            cal_py.chmod(0)
        for night, (why, breaks) in zip(("2026-11-09", "2026-11-10", "2026-11-11"),
                                        (("missing", missing), ("unreadable", unreadable), ("missing", missing))):
            req = self.request()
            breaks()
            try:
                rc, log, _ = self.run_night(STUB_NEW_LABELS=5, CORTADOS_S1_NIGHT_DATE=night)
            finally:
                if cal_py.exists():
                    cal_py.chmod(0o644)
            with self.subTest(night=night, why=why):
                self.assertNotIn("SWEEP REQUEST INVALID", log)
                self.assertNotIn("rejected the arguments", log)
                self.assertEqual(list((self.s1 / "data").glob("sweep-request.invalid-*.json")), [], log)
                self.assertIn("calibrate.py missing or unreadable", log)
                self.assertFalse((self.s1 / "data/check-count").exists(), log)
                self.assertFalse((self.s1 / "data/sweep-count").exists(), log)
                if night != "2026-11-11":
                    self.assertEqual(rc, 1, log)
                    self.assertTrue(req.exists(), log)
                    self.assertEqual(self.lora(), "", log)
        self.assertEqual(rc, 0, log)                              # third failed night: set aside as failed, the night trains
        self.assertFalse(req.exists(), log)
        aside = list((self.s1 / "data").glob("sweep-request.failed-*.json"))
        self.assertEqual(len(aside), 1, log)
        rec = json.loads(aside[0].read_text())
        self.assertEqual(sorted(rec["failed_nights"]), ["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertIn("calibrate.py missing or unreadable", rec["failed_nights"]["2026-11-10"])
        self.assertIn("ALERT: calibration sweep failed on 3 nights", log)
        self.assertIn("--train", self.lora())

    def test_three_failed_nights_set_the_request_aside_with_an_alert_and_the_night_trains(self):
        req = self.request()
        for night, n in (("2026-11-09", 1), ("2026-11-09", 2), ("2026-11-10", 3)):   # two failed cycles in one night count once
            rc, log, _ = self.run_night(STUB_NEW_LABELS=5, STUB_CAL_RC=1, CORTADOS_S1_NIGHT_DATE=night)
            self.assertEqual(rc, 1, log)
            self.assertTrue(req.exists(), log)
            self.assertEqual(self.lora(), "", log)
        rec = json.loads((self.s1 / "data/sweep-request.failures.json").read_text())
        self.assertEqual(sorted(rec["nights"]), ["2026-11-09", "2026-11-10"])
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5, STUB_CAL_RC=1, CORTADOS_S1_NIGHT_DATE="2026-11-11")
        self.assertEqual(rc, 0, log)
        self.assertFalse(req.exists(), log)
        aside = list((self.s1 / "data").glob("sweep-request.failed-*.json"))
        self.assertEqual(len(aside), 1, log)
        rec = json.loads(aside[0].read_text())
        self.assertEqual(sorted(rec["failed_nights"]), ["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertIn("rc=1: Traceback: boom", rec["failed_nights"]["2026-11-11"])
        self.assertIn("failed_at", rec)
        self.assertFalse((self.s1 / "data/sweep-request.failures.json").exists())
        self.assertIn("ALERT: calibration sweep failed on 3 nights", log)
        self.assertIn("training tonight", log)
        self.assertIn("--train", self.lora())
        status = json.loads((self.s1 / "data/status.json").read_text())
        self.assertIn("sweep_set_aside", status.get("alerts", {}), status)

    def test_a_new_request_starts_its_failure_count_over(self):
        self.request()
        self.run_night(STUB_NEW_LABELS=0, STUB_CAL_RC=1, CORTADOS_S1_NIGHT_DATE="2026-11-09")
        self.run_night(STUB_NEW_LABELS=0, STUB_CAL_RC=1, CORTADOS_S1_NIGHT_DATE="2026-11-10")
        self.request(scales=[0.5])                              # replaced by the operator
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, STUB_CAL_RC=1, CORTADOS_S1_NIGHT_DATE="2026-11-11")
        self.assertEqual(rc, 1, log)
        self.assertTrue((self.s1 / "data/sweep-request.json").exists(), log)
        self.assertIn("failed on 1 of 3 nights", log)

    def test_far_future_sweep_night_marker_is_capped_at_the_window_end(self):
        marker = self.s1 / "data/sweep-night-until"
        until = int(time.time()) + 3600
        marker.write_text(f"{int(time.time()) + 10 * 86400}\n")
        rc, log, _, _ = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=1, CORTADOS_S1_TRAIN_UNTIL=until)
        self.assertEqual(rc, 0, log)
        self.assertIn("past this window's end", log)
        self.assertIn("tonight's window went to the calibration sweep", log)     # tonight still belongs to the sweep
        self.assertEqual(marker.read_text().strip(), str(until))
        # without a window end to compare with, a marker more than a day ahead is ignored and removed
        marker.write_text(f"{int(time.time()) + 10 * 86400}\n")
        rc, log, _ = self.run_night(STUB_NEW_LABELS=5)
        self.assertEqual(rc, 0, log)
        self.assertIn("more than a day ahead: ignored", log)
        self.assertIn("training tonight", log)
        self.assertFalse(marker.exists())

    def test_a_finished_sweep_never_writes_a_marker_past_a_day(self):
        self.request()
        rc, log, _, _ = self._cycle(STUB_NEW_LABELS=0, CORTADOS_S1_TRAIN_ALLOWED=1,
                                    CORTADOS_S1_TRAIN_UNTIL=int(time.time()) + 10 * 86400)
        self.assertEqual(rc, 0, log)
        self.assertIn("sweep finished", log)
        self.assertFalse((self.s1 / "data/sweep-night-until").exists(), log)

    def test_failed_sweep_keeps_the_request_and_fails_the_cycle_without_labeling(self):
        req = self.request()
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=5, STUB_CAL_RC=1)
        self.assertEqual((rc, res), (1, None), log)            # overnight.py backs off; the next try resumes the cache
        self.assertTrue(req.exists())
        self.assertIn("SWEEP FAILED or interrupted rc=1", log)
        self.assertFalse((self.s1 / "data/label-args").exists(), log)
        self.assertFalse((self.s1 / "data/sweep-night-until").exists())

    def test_outside_the_window_the_request_waits_and_the_cycle_labels(self):
        req = self.request()
        rc, log, _, res = self._cycle(STUB_NEW_LABELS=5, CORTADOS_S1_TRAIN_ALLOWED=0)
        self.assertEqual((rc, res), (0, "worked"), log)
        self.assertTrue(req.exists())
        self.assertFalse((self.s1 / "data/sweep-count").exists())

    def test_retention_keeps_the_pending_sweeps_candidate(self):
        cand = self.s1 / "runs/2026-11-08"
        (cand / "adapter").mkdir(parents=True)
        (cand / "adapter/0001500_adapters.safetensors").write_text("w")
        (cand / "segment-target").write_text("1500\n")
        req = self.request()
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_TRAIN_ALLOWED=0)   # window mode: no carry; prune runs
        self.assertIn("retention: keeping runs/2026-11-08 (candidate of the pending sweep request)", log)
        self.assertTrue((cand / "adapter/0001500_adapters.safetensors").exists(), log)
        req.unlink()
        rc, log, _ = self.run_night(STUB_NEW_LABELS=0, CORTADOS_S1_TRAIN_ALLOWED=0)
        self.assertFalse((cand / "adapter").exists(), "without a request the unfinished run is pruned as before: " + log)


if __name__ == "__main__":
    unittest.main()
