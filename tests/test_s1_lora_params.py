"""s1/lora_params.py: the LoRA rank/scale a nightly training segment runs at (#74, 2026-11-13: the live adapter became the
rank-16 blend while nightly.sh trained every warm start at mlx_lm's default rank 8). Stdlib; fakes only, no MLX."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_s1_nightly import fake_adapter

S1 = Path(__file__).resolve().parents[1] / "s1"
_spec = importlib.util.spec_from_file_location("lora_params", S1 / "lora_params.py")
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)
BASE = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("CORTADOS_S1_LORA_")}


class LoraParams(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s1-lora-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        env = mock.patch.dict(os.environ, CLEAN, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def plan(self, start=None, config=None, layers=16):
        return lp.plan(self.run_dir, layers, BASE, start, config)

    def test_rank_is_read_from_the_tensors(self):
        w = fake_adapter(self.tmp / "a", rank=16)
        self.assertEqual(lp.tensor_rank(w), (16, 16))
        self.assertEqual(lp.main(["rank", str(w)]), 0)

    def test_fresh_run_uses_the_configured_values_or_mlx_defaults(self):
        self.assertEqual(self.plan()[0], {"rank": 8, "dropout": 0.0, "scale": 20.0})
        with mock.patch.dict(os.environ, {"CORTADOS_S1_LORA_RANK": "16", "CORTADOS_S1_LORA_SCALE": "10"}):
            self.assertEqual(self.plan()[0], {"rank": 16, "dropout": 0.0, "scale": 10.0})

    def test_warm_start_keeps_the_start_weights_rank_and_scale(self):
        w = fake_adapter(self.tmp / "a", rank=16, scale=12.5)
        params, why = self.plan(w, self.tmp / "a/adapter_config.json")
        self.assertEqual(params, {"rank": 16, "dropout": 0.0, "scale": 12.5})
        self.assertIn("rank 16", why)

    def assertRefused(self, fragment, **kw):
        with self.assertRaises(lp.Refused) as cm:
            self.plan(**kw)
        self.assertIn(fragment, str(cm.exception))

    def test_config_that_does_not_describe_the_tensors_is_refused(self):
        w = fake_adapter(self.tmp / "a", rank=16, config_rank=8)
        self.assertRefused("says LoRA rank 8 but the tensors", start=w, config=self.tmp / "a/adapter_config.json")

    def test_missing_config_is_refused(self):
        w = fake_adapter(self.tmp / "a", rank=16)
        (self.tmp / "a/adapter_config.json").unlink()
        self.assertRefused("no adapter_config.json", start=w, config=self.tmp / "a/adapter_config.json")

    def test_explicit_rank_or_scale_that_disagrees_is_refused_not_applied(self):
        w = fake_adapter(self.tmp / "a", rank=16)
        cfg = self.tmp / "a/adapter_config.json"
        with mock.patch.dict(os.environ, {"CORTADOS_S1_LORA_RANK": "8"}):
            self.assertRefused("CORTADOS_S1_LORA_RANK=8", start=w, config=cfg)
        with mock.patch.dict(os.environ, {"CORTADOS_S1_LORA_SCALE": "10"}):
            self.assertRefused("CORTADOS_S1_LORA_SCALE=10", start=w, config=cfg)
        with mock.patch.dict(os.environ, {"CORTADOS_S1_LORA_RANK": "16", "CORTADOS_S1_LORA_SCALE": "20"}):
            self.assertEqual(self.plan(w, cfg)[0]["rank"], 16)        # agreeing values are fine

    def test_bad_configured_values_are_refused(self):
        for v in ("eight", "0", "-4", "nan"):
            with self.subTest(v=v), mock.patch.dict(os.environ, {"CORTADOS_S1_LORA_RANK" if v != "nan" else "CORTADOS_S1_LORA_SCALE": v}):
                with self.assertRaises(lp.Refused):
                    self.plan()

    def test_rank_above_the_cap_is_refused(self):
        w = fake_adapter(self.tmp / "a", rank=128)
        self.assertRefused("above CORTADOS_S1_LORA_MAX_RANK=64", start=w, config=self.tmp / "a/adapter_config.json")
        with mock.patch.dict(os.environ, {"CORTADOS_S1_LORA_MAX_RANK": "128"}):
            self.assertEqual(self.plan(w, self.tmp / "a/adapter_config.json")[0]["rank"], 128)

    def test_layer_count_and_base_must_match(self):
        w = fake_adapter(self.tmp / "a", rank=16, layers=8)
        self.assertRefused("trained on 8 layers", start=w, config=self.tmp / "a/adapter_config.json")
        w = fake_adapter(self.tmp / "b", rank=16)
        cfg = json.loads((self.tmp / "b/adapter_config.json").read_text())
        (self.tmp / "b/adapter_config.json").write_text(json.dumps({**cfg, "model": "other/base"}))
        self.assertRefused("trained on base 'other/base'", start=w, config=self.tmp / "b/adapter_config.json")

    def test_mixed_ranks_and_non_adapters_are_refused(self):
        w = fake_adapter(self.tmp / "a", rank=16)
        raw = w.read_bytes()
        hdr = json.loads(raw[8:])
        hdr["model.layers.0.self_attn.q_proj.lora_a"]["shape"] = [4, 8]
        body = json.dumps(hdr).encode()
        w.write_bytes(len(body).to_bytes(8, "little") + body)
        with self.assertRaisesRegex(lp.Refused, "mixes LoRA ranks"):
            lp.tensor_rank(w)
        (self.tmp / "junk").write_text("w")
        with self.assertRaisesRegex(lp.Refused, "not a safetensors"):
            lp.tensor_rank(self.tmp / "junk")

    def test_a_run_never_changes_rank_half_way(self):
        out = self.run_dir / "lora-train.yaml"
        self.assertEqual(lp.main(["plan", "--run", str(self.run_dir), "--num-layers", "16", "--base", BASE]), 0)
        self.assertEqual(json.loads(out.read_text()), {"lora_parameters": {"rank": 8, "dropout": 0.0, "scale": 20.0}})
        w = fake_adapter(self.tmp / "a", rank=16)
        rc = lp.main(["plan", "--run", str(self.run_dir), "--num-layers", "16", "--base", BASE,
                      "--start", str(w), "--config", str(self.tmp / "a/adapter_config.json")])
        self.assertEqual(rc, lp.EXIT_REFUSED)
        self.assertEqual(json.loads((self.run_dir / "lora-params.json").read_text())["rank"], 8)


class Provenance(unittest.TestCase):
    def test_nightly_promotion_records_the_adapters_lora_shape(self):
        spec = importlib.util.spec_from_file_location("nightly_state_lp", S1 / "nightly_state.py")
        ns = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ns)
        tmp = Path(tempfile.mkdtemp(prefix="s1-lora-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        fake_adapter(tmp / "adapter", rank=16)
        self.assertEqual(ns.lora_of(tmp / "adapter"), {"rank": 16, "scale": 20.0, "num_layers": 16})
        self.assertIsNone(ns.lora_of(tmp / "missing"))


class BandsOnly(unittest.TestCase):
    def test_band_only_tools_turn_the_vendor_usage_probe_off(self):
        import sys
        sys.path.insert(0, str(S1))
        self.addCleanup(sys.path.remove, str(S1))
        import common
        with mock.patch.dict(os.environ, {"CORTADOS_SCARCITY_REFRESH": "on"}), \
                mock.patch.object(common.core, "lane_health", common.core.lane_health):
            common.bands_only()
            self.assertEqual(os.environ["CORTADOS_SCARCITY_REFRESH"], "off")
            self.assertEqual(common.core.lane_health(refresh=True), {})


if __name__ == "__main__":
    unittest.main()
