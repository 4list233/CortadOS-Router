"""s1/promote_sweep.py: publishing a calibration-sweep setting by hand. Fakes only: tiny files stand in for adapters; no MLX,
no scorer, no real ~/.cortados, no Studio."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

S1 = Path(__file__).resolve().parents[1] / "s1"
sys.path.insert(0, str(S1))

import calibrate as cal  # noqa: E402
import promote_sweep as ps  # noqa: E402

BASE = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
LIVE_T = {"steps": 1.5, "outward": 1.2}


def ok_report(promote=True):
    return {"promote": promote, "rule": "new", "reason": "outcome-led",
            "checks": [{"check": "teacher under-routing", "ok": promote}]}


def teacher(acc, under):
    return {"n": 935, "band_accuracy": acc, "under_routed": under, "over_routed": 0.2, "outward_recall": 0.918, "latency_ms_median": 2423}


class Tree:
    """<root>/runs/{live,cand}/adapter, <root>/runs/cand/sweep.json + sweep/<version>/ like calibrate.run_sweep leaves them."""

    def __init__(self, base: Path):
        self.root = base / "train"
        self.live = self.root / "runs" / "live" / "adapter"
        self.cand = self.root / "runs" / "cand" / "adapter"
        self.run = self.root / "runs" / "cand"
        for d, rank in ((self.live, 8), (self.cand, 8)):
            d.mkdir(parents=True)
            (d / "adapters.safetensors").write_bytes(os.urandom(4096))
            (d / "adapter_config.json").write_text(json.dumps({"model": BASE, "lora_parameters": {"rank": rank, "scale": 20.0}}))
        (self.root / "data").mkdir()
        self.pointer = base / "current.json"
        self.pointer.write_text(json.dumps({"base": BASE, "adapter": str(self.live.resolve()), "temperatures": LIVE_T,
                                            "promoted": "2026-10-16 05:40"}))
        cand_id, live_id = cal.file_id(self.cand / "adapters.safetensors"), cal.file_id(self.live / "adapters.safetensors")
        self.fp = {"calibrate_version": 3, "candidate_weights": cand_id, "live_weights": live_id, "valid": "V",
                   "inference": {"base": BASE, "fail_up": 0.75, "outward_at": 0.25,
                                 "live_pointer": {"adapter": str(self.live.resolve()), "base": BASE, "temperatures": LIVE_T}}}
        v = self.run / "sweep" / "blend0.7"
        v.mkdir(parents=True)
        (v / "adapters.safetensors").write_bytes(os.urandom(8192))
        (v / "adapter_config.json").write_text(json.dumps({"model": BASE, "lora_parameters": {"rank": 16, "scale": 20.0}}))
        time.sleep(0.01)
        (v / "scores.meta.json").write_text(json.dumps({"version": "blend0.7", "candidate": cand_id, "live": live_id, "valid": "V"}))
        (v / "scores.done").write_text("{}")
        (v / "temps.json").write_text(json.dumps({"valid": "V", "T": {"steps": 1.2, "outward": 1.2}}))
        self.vdir = v
        self.prov = cal.provenance(str(v), type("S", (), {"T": {"steps": 1.2, "outward": 1.2}})())
        self.sweep = {
            "versions": {"blend0.7": self.prov},
            "fingerprint": self.fp, "fail_up": 0.75, "holdout_rows": 935, "chosen": "blend0.7-t0.3",
            "live_teacher": teacher(0.576, 0.059),
            "settings": [
                {"setting": "blend0.7-t0", "adapter": "runs/cand/sweep/blend0.7", "tilt": 0.0, "blend": ["blend", 0.7],
                 "teacher": teacher(0.68, 0.107), "outcome": {"n": 83, "band_accuracy": 0.554}, "report": ok_report()},
                {"setting": "blend0.7-t0.3", "adapter": "runs/cand/sweep/blend0.7", "tilt": 0.3, "blend": ["blend", 0.7],
                 "teacher": teacher(0.681, 0.099), "outcome": {"n": 83, "band_accuracy": 0.554}, "report": ok_report()},
                {"setting": "cand-t0", "adapter": "runs/cand/adapter", "tilt": 0.0, "blend": None,
                 "teacher": teacher(0.661, 0.133), "outcome": {"n": 83}, "report": ok_report(False)},
            ]}
        self.write_sweep()

    def write_sweep(self):
        (self.run / "sweep.json").write_text(json.dumps(self.sweep))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Tree(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, setting="blend0.7-t0", **kw):
        return ps.plan(self.t.run / "sweep.json", setting, self.t.pointer, now="2026-11-11 12:00", **kw)

    def refused(self, setting="blend0.7-t0", **kw):
        with self.assertRaises(ps.Refused) as cm:
            self.plan(setting, **kw)
        return str(cm.exception)

    def test_dry_run_builds_the_pointer_and_writes_nothing(self):
        before = self.t.pointer.read_text()
        p = self.plan()
        ptr = p["pointer"]
        self.assertEqual(ptr["temperatures"], {"steps": 1.2, "outward": 1.2})          # the version's fitted temps, not live's
        self.assertEqual(ptr["base"], BASE)
        self.assertEqual(ptr["inference"], {"fail_up_below": 0.75, "outward_at": 0.25})
        self.assertEqual(ptr["holdout"]["under_routed"], 0.107)
        self.assertTrue(ptr["adapter"].endswith("runs/cand/promoted/cand-blend0.7/adapter"))
        self.assertEqual(self.t.pointer.read_text(), before)
        self.assertFalse((self.t.run / "promoted").exists())
        self.assertTrue(any("LoRA rank 16 differs" in n and "trains at rank 16" in n for n in p["notes"]))
        self.assertEqual(ptr["lora"], {"rank": 16, "scale": 20.0, "num_layers": None})   # provenance, as nightly promotions record it

    def test_tilted_setting_is_refused_and_names_its_untilted_twin(self):
        msg = self.refused("blend0.7-t0.3")
        self.assertIn("serving applies no tilt", msg)
        self.assertIn("blend0.7-t0: teacher acc 0.68 under 0.107", msg)

    def test_setting_that_does_not_promote_is_refused(self):
        self.assertIn("does not pass evaluate.promotion_report", self.refused("cand-t0"))

    def test_unknown_setting_is_refused(self):
        self.assertIn("no setting", self.refused("blend0.5-t0"))

    def test_live_pointer_changed_since_the_sweep_is_refused(self):
        cur = json.loads(self.t.pointer.read_text())
        cur["temperatures"] = {"steps": 2.0}
        self.t.pointer.write_text(json.dumps(cur))
        self.assertIn("temperatures is not the one the sweep graded", self.refused())

    def test_changed_candidate_weights_are_refused(self):
        (self.t.cand / "adapters.safetensors").write_bytes(os.urandom(4096))
        self.assertIn("candidate weights", self.refused())

    def test_temps_fitted_on_other_rows_are_refused(self):
        (self.t.vdir / "temps.json").write_text(json.dumps({"valid": "OTHER", "T": {"steps": 1.0}}))
        self.assertIn("other validation rows", self.refused())

    def test_incomplete_scores_are_refused(self):
        (self.t.vdir / "scores.done").unlink()
        self.assertIn("incomplete", self.refused())

    def test_sweep_without_provenance_is_refused(self):
        del self.t.sweep["versions"]
        self.t.write_sweep()
        self.assertIn("recorded no provenance for blend0.7", self.refused())
        self.t.sweep["versions"] = {"blend0.7": None}          # fully cached before provenance was recorded
        self.t.write_sweep()
        self.assertIn("Move", self.refused())

    def test_temps_other_than_the_ones_the_scores_used_are_refused(self):
        # Same validation rows (the old check passes), other temperatures: the graded scores did not use these.
        (self.t.vdir / "temps.json").write_text(json.dumps({"valid": "V", "T": {"steps": 1.2, "outward": 1.3}}))
        self.assertIn("other temperatures than the ones blend0.7 was scored with", self.refused())

    def test_blend_weights_other_than_the_graded_ones_are_refused_even_with_the_old_mtime(self):
        w = self.t.vdir / "adapters.safetensors"
        st = w.stat()
        w.write_bytes(os.urandom(8192))
        os.utime(w, ns=(st.st_atime_ns, st.st_mtime_ns))          # mtime restored: only the content says it changed
        self.assertIn("is not the adapter blend0.7 was scored with", self.refused())

    def test_blend_config_other_than_the_graded_one_is_refused(self):
        for cfg in ({"model": BASE, "lora_parameters": {"rank": 16, "scale": 10.0}},      # other scale
                    {"model": BASE, "lora_parameters": {"rank": 8, "scale": 20.0}},       # other rank
                    {"model": BASE, "lora_parameters": {"rank": 16, "scale": 20.0}, "num_layers": 4}):   # any other field
            (self.t.vdir / "adapter_config.json").write_text(json.dumps(cfg))
            self.assertIn("is not the config blend0.7 was scored with", self.refused())

    def test_recorded_config_content_must_match_not_only_its_hash(self):
        self.t.sweep["versions"]["blend0.7"] = dict(self.t.prov, adapter_config={"model": BASE, "lora_parameters": {"rank": 8}})
        self.t.write_sweep()
        self.assertIn("is not the config blend0.7 was scored with", self.refused())

    def test_sweep_without_fingerprint_is_refused(self):
        del self.t.sweep["fingerprint"]
        self.t.write_sweep()
        self.assertIn("no input fingerprint", self.refused())

    def test_write_copies_read_only_keeps_prev_and_is_idempotent(self):
        before = self.t.pointer.read_text()
        p = self.plan()
        ps.write(p, self.t.pointer)
        dest = Path(p["dest"])
        self.assertEqual(ps.sha256(dest / "adapters.safetensors"), ps.sha256(self.t.vdir / "adapters.safetensors"))
        self.assertEqual(os.stat(dest / "adapters.safetensors").st_mode & 0o777, 0o444)
        self.assertEqual(Path(str(self.t.pointer) + ".prev").read_text(), before)
        ptr = json.loads(self.t.pointer.read_text())
        self.assertEqual(ptr["adapter"], str(dest.resolve()))
        self.assertEqual(ptr["sweep"]["setting"], "blend0.7-t0")
        # Re-planning now fails the live-pointer check (the pointer moved), which is the guard against a double publish.
        self.assertIn("is not the one the sweep graded", self.refused())

    def test_existing_destination_with_other_content_is_refused(self):
        dest = self.t.run / "promoted" / "cand-blend0.7" / "adapter"
        dest.mkdir(parents=True)
        (dest / "adapters.safetensors").write_bytes(b"other")
        (dest / "adapter_config.json").write_text("{}")
        self.assertIn("exists with other content", self.refused())

    def test_fs_prefix_mirror_reads_copies_and_prints_real_paths(self):
        mirror = Path(self.tmp.name) / "mirror"
        real_root = Path("/Users/velox/cortados/train")
        # Rebuild the tree as if it lived at /Users/velox/cortados/train, mirrored under <mirror>.
        import shutil
        shutil.copytree(self.t.root, Path(str(mirror) + str(real_root)), copy_function=shutil.copy2)
        m_root = Path(str(mirror) + str(real_root))
        fp = json.loads(json.dumps(self.t.fp))
        for k, rel in (("candidate_weights", "runs/cand/adapter/adapters.safetensors"), ("live_weights", "runs/live/adapter/adapters.safetensors")):
            fp[k]["path"] = str(real_root / rel)
        live_real = str(real_root / "runs/live/adapter")
        fp["inference"]["live_pointer"]["adapter"] = live_real
        sweep = dict(self.t.sweep, fingerprint=fp)
        (m_root / "runs/cand/sweep.json").write_text(json.dumps(sweep))
        # file_id keeps mtime: copy2 preserved it; the mirror's files must still match the fingerprint's other fields
        for k, rel in (("candidate_weights", "runs/cand/adapter/adapters.safetensors"), ("live_weights", "runs/live/adapter/adapters.safetensors")):
            self.assertTrue(ps.same_weights(cal.file_id(m_root / rel), fp[k]))
        meta = json.loads((m_root / "runs/cand/sweep/blend0.7/scores.meta.json").read_text())
        meta["candidate"], meta["live"] = fp["candidate_weights"], fp["live_weights"]
        (m_root / "runs/cand/sweep/blend0.7/scores.meta.json").write_text(json.dumps(meta))
        os.utime(m_root / "runs/cand/sweep/blend0.7/adapters.safetensors", ns=(1, 1))
        ptr = Path(self.tmp.name) / "copy-current.json"
        ptr.write_text(json.dumps({"base": BASE, "adapter": live_real, "temperatures": LIVE_T}))
        p = ps.plan(m_root / "runs/cand/sweep.json", "blend0.7-t0", ptr, fs_prefix=mirror.resolve(), now="x")
        self.assertEqual(p["pointer"]["adapter"], str(real_root / "runs/cand/promoted/cand-blend0.7/adapter"))
        self.assertEqual(p["pointer"]["report"], str(real_root / "runs/cand/sweep.json"))

    def test_cli_refuses_write_with_fs_prefix(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ps.main(["plan", "--sweep", str(self.t.run / "sweep.json"), "--setting", "blend0.7-t0",
                          "--pointer", str(self.t.pointer), "--write", "--fs-prefix", self.tmp.name])
        self.assertEqual(rc, ps.EXIT_REFUSED)
        self.assertIn("--fs-prefix is for dry runs", buf.getvalue())

    def test_cli_dry_run_exit_codes(self):
        for setting, want in (("blend0.7-t0", 0), ("blend0.7-t0.3", ps.EXIT_REFUSED)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ps.main(["plan", "--sweep", str(self.t.run / "sweep.json"), "--setting", setting, "--pointer", str(self.t.pointer)])
            self.assertEqual(rc, want, buf.getvalue())
        self.assertFalse((self.t.run / "promoted").exists())


class CheckTest(unittest.TestCase):
    rows = [{"id": f"r{i}", "pred_band": b, "unit_kind": "interactive", "pred_answers": {"steps": i}}
            for i, b in enumerate(["R0", "R0", "R0", "R1", "R2", "R2"])]
    reqs = {f"r{i}": {"id": f"r{i}", "request": f"secret text {i}"} for i in range(6)}

    def test_pick_rows_spreads_across_bands(self):
        self.assertEqual([r["id"] for r in ps.pick_rows(self.rows, 2)], ["r0", "r1", "r3", "r4", "r5"])

    def run_check(self, band_for, ms=2400, live=2409.0):
        lines = []
        decide = lambda text, uk: {"answers": {"steps": int(text.split()[-1])}, "degraded": False, "engine_ms": ms}
        rc = ps.run_check(ps.pick_rows(self.rows, 2), self.reqs, decide, lambda a: band_for(a["steps"]),
                          live_median_ms=live, budget_s=1.0, out=lines.append)
        return rc, "\n".join(lines)

    def test_all_bands_match(self):
        bands = {r["pred_answers"]["steps"]: r["pred_band"] for r in self.rows}
        rc, out = self.run_check(bands.get)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("secret", out)

    def test_band_mismatch_fails(self):
        rc, out = self.run_check(lambda s: "R4")
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH", out)

    def test_latency_over_budget_fails(self):
        bands = {r["pred_answers"]["steps"]: r["pred_band"] for r in self.rows}
        rc, out = self.run_check(bands.get, ms=3500)
        self.assertEqual(rc, 1)
        self.assertIn("OVER BUDGET", out)

    def test_rows_missing_from_requests_fail(self):
        bands = {r["pred_answers"]["steps"]: r["pred_band"] for r in self.rows}
        lines = []
        decide = lambda text, uk: {"answers": {"steps": int(text.split()[-1])}, "degraded": False, "engine_ms": 2400}
        reqs = {k: v for k, v in self.reqs.items() if k != "r4"}      # the only selected R2 rows: r4 missing, r5 present
        rc = ps.run_check(ps.pick_rows(self.rows, 2), reqs, decide, lambda a: bands[a["steps"]],
                          live_median_ms=2409.0, budget_s=1.0, out=lines.append)
        self.assertEqual(rc, 1)
        out = "\n".join(lines)
        self.assertIn("r4: expected R2, NOT CHECKED", out)
        self.assertIn("4/5 bands match; 1 not checked", out)

    def test_every_row_missing_from_requests_fails(self):
        lines = []
        rc = ps.run_check(self.rows[:2], {}, lambda t, u: self.fail("must not score"), lambda a: "R0",
                          live_median_ms=None, budget_s=1.0, out=lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("2 not checked", "\n".join(lines))

    def test_no_selected_rows_cannot_run(self):
        self.assertEqual(ps.run_check([], self.reqs, None, None, live_median_ms=None, budget_s=1.0, out=lambda m: None), 2)

    def test_degraded_scorer_fails(self):
        lines = []
        rc = ps.run_check(self.rows[:1], self.reqs, lambda t, u: {"answers": None, "degraded": True, "error": "down"},
                          lambda a: "R0", live_median_ms=None, budget_s=1.0, out=lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("DEGRADED", "\n".join(lines))


class CheckCliTest(unittest.TestCase):
    """The check subcommand end to end through the real core.resolve, with core.decide_s1 replaced (no scorer, no network)."""

    def test_check_cli_matches_bands_through_core_resolve(self):
        from unittest import mock
        sys.path.insert(0, str(S1.parent))
        from cortados_router import core
        low = {f: 0 for f in core.FEATURES}
        low.update(outward=False, work_kind=core.WORK_KINDS[0], urgency=core.URGENCY[0], needs_clarification=False, parts=0, diversity=False)
        high = dict(low, **{f: 3 for f in core.FEATURES})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "lane_health", lambda **_: {}), \
                mock.patch.dict(os.environ, {"CORTADOS_HOME": d, "CORTADOS_SCARCITY_REFRESH": "off"}):
            band = lambda a: core.resolve({"answers": a, "degraded": False, "engine": "t", "engine_ms": 0})["band"]
            run = Path(d) / "runs" / "c"
            (run / "sweep" / "blend0.7-t0").mkdir(parents=True)
            (run / "sweep.json").write_text(json.dumps({"live_teacher": {"latency_ms_median": 2400}}))
            rows = [{"id": "a", "unit_kind": "interactive", "pred_band": band(low), "pred_answers": low},
                    {"id": "b", "unit_kind": "interactive", "pred_band": band(high), "pred_answers": high}]
            (run / "sweep" / "blend0.7-t0" / "holdout-rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            reqs = Path(d) / "requests.jsonl"
            reqs.write_text(json.dumps({"id": "a", "request": "LOW"}) + "\n" + json.dumps({"id": "b", "request": "HIGH"}) + "\n")
            fake = lambda text, uk, **_: {"answers": low if text == "LOW" else high, "degraded": False, "engine_ms": 2300}
            buf = io.StringIO()
            with mock.patch.object(core, "decide_s1", fake), redirect_stdout(buf):
                rc = ps.main(["check", "--sweep", str(run / "sweep.json"), "--expect", "blend0.7-t0", "--requests", str(reqs)])
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertIn("2/2 bands match", buf.getvalue())
            self.assertNotIn("LOW", buf.getvalue().replace("ok", ""))
            self.assertEqual(os.environ.get("CORTADOS_SCARCITY_REFRESH"), "off")      # main() forces it before touching core


if __name__ == "__main__":
    unittest.main()
