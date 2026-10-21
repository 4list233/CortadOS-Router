import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cortados_router import core, local_resources as resources, scarcity


class LocalResources(unittest.TestCase):
    def node(self, **changes):
        return {"id": "studio", "status": "online", "pool_eligible": True,
                "sampled_at": 1000, "gpu_budget_bytes": 24 * resources.GIB,
                "gpu_headroom_estimate_bytes": 8 * resources.GIB,
                "host_available_estimate_bytes": 100 * resources.GIB,
                "gpu_utilization_pct": 10, **changes}

    def deployment(self):
        return {"model": "model", "ready": True, "nodes": ["studio"],
                "additional_memory_gib_by_node": {"studio": 2}, "max_context_tokens": 4096}

    def test_vm_stat_uses_reported_page_size(self):
        text = "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 10.\nPages inactive: 20.\nPages speculative: 3."
        self.assertEqual(resources.mac_memory(text), 33 * 4096)

    def test_missing_page_size_is_unknown(self):
        with self.assertRaises(ValueError):
            resources.mac_memory("Pages free: 10.")

    def test_offline_node_denies_ready_deployment(self):
        row = resources.deployment_health(self.deployment(), {"studio": self.node(status="unreachable")}, 1000, 120)
        self.assertFalse(row["ok"])
        self.assertIn("offline", row["why"])

    def test_stale_and_future_clock_samples_are_not_capacity(self):
        for sampled_at in (1, 2000):
            with self.subTest(sampled_at=sampled_at):
                row = resources.deployment_health(self.deployment(), {"studio": self.node(sampled_at=sampled_at)}, 1000, 120)
                self.assertFalse(row["ok"])

    def test_travel_client_is_not_a_home_worker(self):
        row = resources.deployment_health(self.deployment(), {"studio": self.node(pool_eligible=False)}, 1000, 120)
        self.assertFalse(row["ok"])
        self.assertIn("excluded", row["why"])

    def test_host_ram_does_not_rescue_gpu_memory_fit(self):
        row = resources.deployment_health(self.deployment(), {"studio": self.node(gpu_headroom_estimate_bytes=resources.GIB)}, 1000, 120)
        self.assertFalse(row["ok"])
        self.assertIn("headroom", row["why"])

    def test_every_shard_must_fit_on_its_assigned_worker(self):
        deployment = self.deployment()
        deployment["nodes"].append("pc")
        deployment["additional_memory_gib_by_node"]["pc"] = 2
        nodes = {"studio": self.node(), "pc": self.node(id="pc", gpu_headroom_estimate_bytes=resources.GIB)}
        self.assertFalse(resources.deployment_health(deployment, nodes, 1000, 120)["ok"])

    def test_unverified_model_is_not_made_ready_by_free_memory(self):
        deployment = self.deployment()
        deployment["ready"] = False
        self.assertFalse(resources.deployment_health(deployment, {"studio": self.node()}, 1000, 120)["ok"])

    def test_unknown_compute_is_not_zero_usage(self):
        row = resources.deployment_health(self.deployment(), {"studio": self.node(gpu_utilization_pct=None)}, 1000, 120)
        self.assertFalse(row["ok"])
        self.assertIn("unknown GPU utilization", row["why"])

    def test_unknown_context_limit_is_not_an_unbounded_model(self):
        deployment = self.deployment()
        deployment.pop("max_context_tokens")
        self.assertFalse(resources.deployment_health(deployment, {"studio": self.node()}, 1000, 120)["ok"])

    def test_local_summary_has_no_subscription_reset_clock(self):
        with patch.object(scarcity, "change_since", return_value={}):
            lines = scarcity.summary_lines({"lanes": {"local": {"scarcity": 1, "refusing": True, "resource_managed": True}}})
        self.assertIn("UNAVAILABLE", lines[0])
        self.assertNotIn("until the reset", " ".join(lines))

    def test_busy_compute_is_ranking_signal_not_spent_quota(self):
        row = resources.deployment_health(self.deployment(), {"studio": self.node(gpu_utilization_pct=100)}, 1000, 120)
        self.assertTrue(row["ok"])
        self.assertEqual(row["scarcity"], 0.95)
        self.assertFalse(row["spent"])

    def test_inventory_without_target_is_unconfigured(self):
        row = resources.sample_node({"id": "pc", "inventory": {"vram_gib": 12, "host_ram_gib": 32}}, b"")
        self.assertEqual(row["status"], "unconfigured")
        self.assertNotIn("gpu_headroom_estimate_bytes", row)

    def test_fleet_does_not_count_traveling_pro_or_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "resources.json"
            config.write_text(json.dumps({"nodes": [{"id": "studio"}, {"id": "pro"}]}))
            now = time.time()
            nodes = [self.node(sampled_at=now), self.node(id="pro", sampled_at=now, pool_eligible=False)]
            with patch.object(resources, "sample_node", side_effect=nodes):
                row = resources.sample(config)
            self.assertEqual(row["known_pool_headroom_estimate_bytes"], 8 * resources.GIB)
            self.assertTrue(row["refusing"])

    def test_ssh_failures_have_no_fresh_capacity(self):
        with patch.object(resources.subprocess, "run", side_effect=OSError()):
            row = resources.sample_node({"id": "studio", "target": "owner@studio"}, b"")
        self.assertEqual(row["status"], "unreachable")
        self.assertNotIn("sampled_at", row)

    def test_local_candidate_requires_exact_verified_model(self):
        health = {"local": {"resource_managed": True, "deployments": {"ready-model": {"ok": True, "scarcity": 0.2, "max_context_tokens": 4096}}}}
        self.assertTrue(core.candidate_health({"lane": "local", "model": "ready-model"}, health)["ok"])
        self.assertFalse(core.candidate_health({"lane": "local", "model": "other-model"}, health)["ok"])

    def test_rejected_local_deployment_is_never_the_fallback_choice(self):
        # Shadow mode shows the first candidate when every pool is spent; unverified local capacity is unroutable, not spent.
        ms = {"use_cases": {"code": ""}, "reference_index": {b: {"general": v, "code": v} for b, v in (("R1", 10), ("R2", 50), ("R3", 80), ("R4", 95))},
              "models": [{"family": "loc", "ceiling": False, "proxy": False, "index": {"general": 60, "code": 60}, "price_1m_blended_usd": None,
                          "reach": [{"lane": "local", "provider": "custom", "model": "loc"}]}]}
        answers = {"steps": 1, "ambiguity": 1, "blast_radius": 0, "context_size": 0, "novelty": 1, "parallelizability": 0, "outward": False,
                   "work_kind": "code", "urgency": "now", "needs_clarification": False, "parts": 0, "diversity": False}
        decision = {"answers": answers, "degraded": False, "engine": "test", "engine_ms": 1}
        verified = {"local": {"resource_managed": True, "deployments": {"loc": {"ok": True, "scarcity": 0.2, "max_context_tokens": 131072}}, "scarcity": 0.2}}
        rejected = {"local": {"resource_managed": True, "deployments": {}, "scarcity": 1.0, "refusing": True, "why": "no verified local deployment currently available"}}
        with patch.object(core, "model_scores", lambda: ms), patch.object(core, "lane_health", lambda: verified):
            self.assertEqual(core.resolve(decision)["chosen"]["lane"], "local")
        with patch.object(core, "model_scores", lambda: ms), patch.object(core, "lane_health", lambda: rejected):
            r = core.resolve(decision)
        self.assertIsNone(r["chosen"])
        self.assertEqual([(s["lane"], s["why"]) for s in r["skipped"]], [("local", "no verified local deployment currently available")])

    def test_change_readout_takes_each_lane_baseline_from_its_own_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / "scarcity-history.jsonl"
            target = time.time() - 24 * 3600
            history.write_text(json.dumps({"ts": target - 600, "lanes": {"codex": {"scarcity": 0.2, "windows": [["week", 30.0, 40.0]]}}}) + "\n"
                               + json.dumps({"ts": target - 5, "lanes": {"local": {"scarcity": 0, "windows": []}}}) + "\n")
            with patch.object(scarcity, "HISTORY", history):
                self.assertEqual(scarcity.change_since(24), {("codex", "week"): 30.0})

    def test_managed_local_lane_missing_or_stale_is_unavailable(self):
        for data in ({"lanes": {}}, {"lanes": {"local": {"fetched_at": time.time() - 121, "resource_managed": True, "scarcity": 0}}}):
            with self.subTest(data=data), patch.dict(os.environ, {"CORTADOS_LOCAL_RESOURCES_CONFIG": "fleet.json"}), patch.object(scarcity, "current", return_value=data):
                row = scarcity.lane_scarcity()["local"]
                self.assertEqual(row["scarcity"], 1)
                self.assertTrue(row["refusing"])

    def test_legacy_local_reading_cannot_bypass_fleet_checks(self):
        data = {"lanes": {"local": {"fetched_at": time.time(), "scarcity": 0}}}
        with patch.dict(os.environ, {"CORTADOS_LOCAL_RESOURCES_CONFIG": "fleet.json"}), patch.object(scarcity, "current", return_value=data):
            self.assertTrue(scarcity.lane_scarcity()["local"]["refusing"])

    def test_managed_local_reading_expires_with_its_node_telemetry(self):
        now = time.time()
        for expires_at, usable in ((now - 1, False), (now + 30, True)):
            data = {"lanes": {"local": {"fetched_at": now, "resource_managed": True, "scarcity": 0.2, "refusing": False,
                                        "expires_at": expires_at, "deployments": {"model": {"ok": True, "scarcity": 0.2}}}}}
            with self.subTest(usable=usable), patch.dict(os.environ, {"CORTADOS_LOCAL_RESOURCES_CONFIG": "fleet.json"}), patch.object(scarcity, "current", return_value=data):
                row = scarcity.lane_scarcity()["local"]
                self.assertEqual(row["refusing"], not usable)
                self.assertEqual(row["deployments"], {"model": {"ok": True, "scarcity": 0.2}} if usable else {})

    def test_sample_reports_the_earliest_node_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "resources.json"
            deployment = {**self.deployment(), "nodes": ["studio", "pc"], "additional_memory_gib_by_node": {"studio": 2, "pc": 2}}
            config.write_text(json.dumps({"max_age_s": 10, "nodes": [{"id": "studio"}, {"id": "pc"}], "deployments": [deployment]}))
            now = time.time()
            nodes = [self.node(sampled_at=now - 3), self.node(id="pc", sampled_at=now - 1)]
            with patch.object(resources, "sample_node", side_effect=nodes):
                row = resources.sample(config)
            self.assertFalse(row["refusing"])
            self.assertEqual(row["max_age_s"], 10)
            self.assertAlmostEqual(row["expires_at"], now - 3 + 10, delta=1)

    def test_local_only_sample_keeps_the_full_sample_stamp(self):
        # A local-only refresh must not look like a full sample, or the subscription probes would never come due again.
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "scarcity.json").write_text(json.dumps({"fetched_at": 1000, "learned_at": time.time(), "lanes": {}}))
            local = {"resource_managed": True, "scarcity": 0, "refusing": False, "windows": [], "pools": {}, "deployments": {}}
            with patch.object(scarcity, "STATE", home), patch.object(scarcity, "CACHE", home / "scarcity.json"), \
                 patch.object(scarcity, "HISTORY", home / "scarcity-history.jsonl"), patch.object(scarcity, "PROBE_DIR", home / "usage-probe"), \
                 patch.dict(scarcity.SAMPLERS, {"local": lambda: dict(local)}), patch.object(scarcity, "_plans", return_value={}), \
                 patch.object(scarcity._reaper, "sweep_stale", return_value=None):
                out = scarcity._sample_locked(only=["local"])
            self.assertEqual(out["fetched_at"], 1000)
            self.assertTrue(out["lanes"]["local"]["resource_managed"])
            self.assertEqual(json.loads((home / "scarcity.json").read_text())["fetched_at"], 1000)

    def test_sampler_write_keeps_lanes_published_meanwhile(self):
        # A run merges only the lanes it sampled onto the disk copy, so a reading published during its probes survives.
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "scarcity.json").write_text(json.dumps({"fetched_at": 1000, "lanes": {"local": {"scarcity": 0.1, "resource_managed": True}}}))
            with patch.object(scarcity, "STATE", home), patch.object(scarcity, "CACHE", home / "scarcity.json"):
                out = scarcity._write_cache({"codex": {"scarcity": 0.5}}, 2000)
            self.assertEqual((out["fetched_at"], out["lanes"]["local"]["scarcity"], out["lanes"]["codex"]["scarcity"]), (2000, 0.1, 0.5))

    def test_managed_local_refresh_does_not_wait_for_subscription_probes(self):
        # The vendor probes hold the sampler lock for up to 240 s; the local reading must publish without it.
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "scarcity.json").write_text(json.dumps({"fetched_at": 1000, "lanes": {"codex": {"scarcity": 0.4}}}))
            local = {"resource_managed": True, "scarcity": 0, "refusing": False, "windows": [], "expires_at": time.time() + 60}
            with patch.object(scarcity, "STATE", home), patch.object(scarcity, "CACHE", home / "scarcity.json"), \
                 patch.object(scarcity, "HISTORY", home / "scarcity-history.jsonl"), patch.object(scarcity, "_local", return_value=dict(local)), \
                 patch.object(scarcity, "_plans", return_value={}):
                self.assertTrue(scarcity._lock.acquire(timeout=1))
                try:
                    done = threading.Event()
                    threading.Thread(target=lambda: (scarcity._sample_local_managed(), done.set()), daemon=True).start()
                    self.assertTrue(done.wait(5), "the local refresh waited on the sampler lock")
                finally:
                    scarcity._lock.release()
            on_disk = json.loads((home / "scarcity.json").read_text())
            self.assertEqual((on_disk["fetched_at"], on_disk["lanes"]["codex"]), (1000, {"scarcity": 0.4}))
            self.assertTrue(on_disk["lanes"]["local"]["resource_managed"])

    def test_managed_local_refresh_is_scheduled_apart_from_the_vendor_probes(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(scarcity, "CACHE", Path(folder) / "scarcity.json"), \
             patch.object(scarcity, "sample") as sampler, patch.object(scarcity, "_sample_local_managed") as local, \
             patch.dict(os.environ, {"CORTADOS_LOCAL_RESOURCES_CONFIG": "fleet.json", "CORTADOS_SCARCITY_REFRESH": "on"}):
            full, part = threading.Event(), threading.Event()
            sampler.side_effect = lambda **kw: full.set()
            local.side_effect = lambda: part.set()
            scarcity.current()
            self.assertTrue(full.wait(5) and part.wait(5))
            self.assertNotIn("local", sampler.call_args.kwargs["only"])

    def test_refresh_switch_never_schedules_a_usage_sample(self):
        # A fresh throwaway home is always "due"; with the switch, verification and `decide` runs open no vendor CLI.
        with tempfile.TemporaryDirectory() as folder, patch.object(scarcity, "CACHE", Path(folder) / "scarcity.json"), \
             patch.object(scarcity, "sample") as sampler:
            with patch.dict(os.environ, {"CORTADOS_SCARCITY_REFRESH": "off"}):
                self.assertEqual(scarcity.current()["lanes"], {})
                self.assertFalse(scarcity._refreshing.is_set())
            sampler.assert_not_called()
            started = threading.Event()
            sampler.side_effect = lambda **kw: started.set()
            with patch.dict(os.environ, {"CORTADOS_SCARCITY_REFRESH": "on"}):
                scarcity.current()
            self.assertTrue(started.wait(5), "without the switch the refresh is scheduled")


if __name__ == "__main__":
    unittest.main()
