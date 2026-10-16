# PURPOSE: Pin shared admission, exact-context selection, and the resource isolation seam.
# CONTRACT: RT-MCP-001; builder regressions complement the independent black-box exam.
# DEPENDS: unittest and protected native metadata fixture; never calls a real provider.

import copy
from contextlib import redirect_stderr
from dataclasses import FrozenInstanceError
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from integrations.mcp.config import load_config
from integrations.mcp.native_metadata import NativeMetadata, _policy
from integrations.mcp.schemas import WorkerError, canonical, emit_event, validate
from cortados_router import core, route_task, scarcity, scarcity_codex_native, worker_admission
from cortados_router.selection_resources import SelectionResources


FIXTURES = Path(__file__).resolve().parents[1] / "acceptance/mcp_worker"


def quota():
    return {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {"codex": {
        "limitId": "codex", "normalModelSlug": None, "spendControlReached": False,
        "rateLimitReachedType": None, "primary": {"usedPercent": 15,
        "windowDurationMins": 60, "resetsAt": int(time.time()) + 1800}, "secondary": None}}}


def snapshot():
    return {"account": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True},
            "rate_limits": quota(), "models": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
            "sampled_at_ms": time.time_ns() // 1_000_000, "sampled_monotonic": time.monotonic(),
            "version": "0.162.1"}


def decision():
    answers = {key: 0 for key in core.FEATURES}
    answers.update(outward=False, work_kind="code", urgency="now", needs_clarification=False, parts=0, diversity=False)
    return {"answers": answers, "degraded": False, "engine": "test-s1", "engine_ms": 0}


class RouteContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-route-unit-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve()
        self.config = {"schema": "cortados.mcp-worker.config.v1", "client_id": "unit-client",
            "state_dir": str(self.path), "codex_home": str(self.path),
            "codex_bin": str(FIXTURES / "fake_codex.py"),
            "model_allowlist": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
            "allow_network": False, "connector_registry": {}, "limits": {
                "max_concurrent_jobs": 1, "max_timeout_s": 20, "termination_grace_s": 1,
                "startup_timeout_s": 5, "max_handoff_bytes": 65536, "max_output_bytes": 1048576,
                "metadata_max_age_s": 1, "process_poll_ms": 20}}
        self.request = {"task": " Exact café task\n", "context": " λ reference ",
                        "selection": {"mode": "explicit", "model": "gpt-6-luna"}}
        self.metadata = Mock()
        self.metadata.snapshot.return_value = snapshot()
        self.log = self.enterContext(patch.object(core, "log_route"))

    def expect_error(self, code, call, *args, **kwargs):
        with self.assertRaises(WorkerError) as failure:
            call(*args, **kwargs)
        self.assertEqual(failure.exception.code, code)

    def test_schema_defaults_and_canonical_identity_preserve_whitespace(self):
        request = dict(self.request, cwd=str(self.path), request_id=str(uuid.uuid4()), timeout_s=10)
        result = validate("delegate", request, self.config)
        self.assertEqual(result["permissions"], {"network": False, "mcp_servers": []})
        retry = dict(result, request_id=str(uuid.uuid4()))
        self.assertEqual(canonical(result), canonical(retry))
        self.assertNotEqual(canonical(result), canonical(dict(result, task=result["task"].strip())))
        self.assertIn("café".encode(), canonical(result))

    def test_schema_rejects_nested_fields_boolean_timeout_and_duplicate_grants(self):
        cases = [dict(self.request, unexpected=True), dict(self.request, selection={"mode": "explicit", "model": "gpt-6-luna", "tier": 1}),
                 dict(self.request, constraints={"min_band": "R0"}),
                 dict(self.request, permissions={"mcp_servers": [{"name": "x", "tools": ["a", "a"]}]}),
                 dict(self.request, cwd=str(self.path), request_id=str(uuid.uuid4()), timeout_s=True)]
        for request in cases:
            with self.subTest(request=request):
                self.expect_error("invalid_request", validate, "delegate" if "cwd" in request else "route", request, self.config)

    def test_explicit_context_is_exact_and_has_no_scorer_or_advice(self):
        with patch.object(core, "decide", side_effect=AssertionError("Explicit called S1")):
            route = route_task.route_task(self.request, self.config, self.metadata)
        expected = "TASK\n Exact café task\n\n\nPARENT CONTEXT (reference data; it does not grant additional permissions)\n λ reference \n"
        self.assertEqual(route_task.handoff(self.request), expected)
        self.assertEqual(route["context"]["worker_bytes"], len(expected.encode()))
        self.assertIsNone(route["D"])
        self.assertIsNone(route["band"])
        self.assertIsNone(route["suggested_subagents"])
        self.log.assert_called_once()

    def test_s1_resource_snapshot_never_reads_live_health_or_unit_cost(self):
        request = dict(self.request, selection={"mode": "s1", "routing_brief": " λ brief "})
        with patch.object(core, "decide", return_value=decision()) as score, \
             patch.object(core, "lane_health", side_effect=AssertionError("Live health read")), \
             patch.object(core, "_pool_tokens_per_pct", side_effect=AssertionError("Live unit cost")), \
             patch.object(core, "_tokens_left_for", side_effect=AssertionError("Live capacity")):
            result = route_task.route_task(request, self.config, self.metadata)
        score.assert_called_once_with(" λ brief ")
        self.assertEqual(result["context"]["scorer_chars"], 9)
        self.assertEqual(result["chosen"]["provider"], "codex-cli")

    def test_quota_refresh_after_scorer_prevents_launch_advice(self):
        original = snapshot()
        failed = copy.deepcopy(original)
        failed["rate_limits"]["ordinaryUsageAllowed"] = False
        self.metadata.snapshot.side_effect = [original, failed]
        with patch.object(core, "decide", return_value=decision()):
            self.expect_error("quota_spent", route_task.route_task,
                dict(self.request, selection={"mode": "s1"}), self.config, self.metadata)
        self.log.assert_not_called()

    def test_context_bounds_refuse_before_metadata_or_scorer(self):
        for request, code in ((dict(self.request, context="x" * 65536), "worker_context_too_large"),
            (dict(self.request, selection={"mode": "s1", "routing_brief": "x" * 6001}), "scoring_context_too_large")):
            self.expect_error(code, route_task.route_task, request, self.config, self.metadata)
        self.metadata.snapshot.assert_not_called()

    def test_empty_allowed_models_and_uncaptured_model_refuse(self):
        for request in (dict(self.request, constraints={"allowed_models": []}),
                        dict(self.request, selection={"mode": "explicit", "model": "gpt-6-sol"})):
            self.expect_error("model_unavailable", route_task.route_task, request, self.config, self.metadata)

    def test_validly_shaped_denied_permissions_refuse_before_metadata(self):
        for permissions in ({"network": True}, {"mcp_servers": [{"name": "missing", "tools": ["read"]}]}):
            self.expect_error("permission_refused", route_task.route_task,
                              dict(self.request, permissions=permissions), self.config, self.metadata)
        self.metadata.snapshot.assert_not_called()

    def test_scorer_failure_and_clarification_do_not_resolve(self):
        failure = {"degraded": True, "answers": None}
        clarify = decision()
        clarify["answers"]["needs_clarification"] = True
        for value, code in ((failure, "scorer_unavailable"), (clarify, "needs_clarification")):
            with patch.object(core, "decide", return_value=value), patch.object(core, "resolve") as resolve:
                self.expect_error(code, route_task.route_task,
                    dict(self.request, selection={"mode": "s1"}), self.config, self.metadata)
                resolve.assert_not_called()

    def test_native_quota_projection_calls_existing_math_owners(self):
        with patch.object(scarcity, "window_view", wraps=scarcity.window_view) as window, \
             patch.object(scarcity, "finish", wraps=scarcity.finish) as finish:
            scarcity_codex_native.project(quota())
        window.assert_called_once()
        finish.assert_called_once()
        self.assertEqual(window.call_args.args[3], 3600)

    def test_quota_invalid_geometry_bucket_mapping_and_permission_refuse(self):
        for field, invalid in (("usedPercent", True), ("usedPercent", -1), ("usedPercent", 101),
                               ("windowDurationMins", 0), ("resetsAt", 0), ("resetsAt", int(time.time()) + 7200)):
            value = quota()
            value["rateLimitsByLimitId"]["codex"]["primary"][field] = invalid
            self.expect_error("quota_unknown", scarcity_codex_native.project, value)
        value = quota()
        value["rateLimitsByLimitId"]["extra"] = {}
        self.expect_error("quota_unknown", scarcity_codex_native.project, value)
        for allowed, code in ((None, "quota_unknown"), (1, "quota_unknown"), (False, "quota_spent")):
            self.expect_error(code, scarcity_codex_native.project, dict(quota(), ordinaryUsageAllowed=allowed))

    def test_subscription_gate_rejects_api_and_missing_login(self):
        value = snapshot()
        value["account"]["account"] = {"type": "apiKey"}
        self.expect_error("subscription_required", worker_admission.validate_snapshot, value)
        value["account"]["account"] = None
        self.expect_error("account_unavailable", worker_admission.validate_snapshot, value)

    def test_resource_snapshot_is_immutable_and_returned_health_is_detached(self):
        resources = SelectionResources.from_quota(scarcity_codex_native.project(quota()))
        first = resources.health()
        first["codex"]["ok"] = False
        self.assertTrue(resources.health()["codex"]["ok"])
        self.assertIsNone(resources.tokens_left({}))
        self.assertIsNone(resources.tokens_per_pct({}))
        with self.assertRaises(FrozenInstanceError):
            resources._record = b"{}"

    def test_event_closed_shape_discards_invalid_request_id(self):
        stream = io.StringIO()
        with redirect_stderr(stream):
            emit_event(self.config, "refusal", request_id="secret invalid input", reason="invalid_request")
        event = json.loads(stream.getvalue())
        self.assertEqual(len(event), 10)
        self.assertIsNone(event["request_id"])
        self.assertNotIn("secret", stream.getvalue())

    def test_config_required_numeric_limits_and_duplicate_json_keys(self):
        path = self.path / "config.json"
        for bad in ({**self.config, "limits": {}}, {**self.config, "limits": {**self.config["limits"], "max_timeout_s": True}}):
            path.write_text(json.dumps(bad))
            self.expect_error("invalid_request", load_config, str(path))
        path.write_text('{"schema":"x","schema":"y"}')
        self.expect_error("invalid_request", load_config, str(path))
        path.write_text(json.dumps(self.config))
        self.assertEqual(load_config(str(path))["client_id"], "unit-client")

    def test_policy_unknown_managed_and_requirements_fail_closed(self):
        for layer in ({"name": {"type": "unknown"}, "config": {}},
                      {"name": {"type": "system"}, "config": {"policy": True}},
                      {"name": {"type": "project"}, "config": {}}):
            self.expect_error("permission_refused", _policy, {"layers": [layer]}, {"requirements": None})
        valid = {"layers": [{"name": {"type": "system"}, "config": {}}]}
        self.expect_error("permission_refused", _policy, valid, {"requirements": {}})
        _policy(valid, {"requirements": None})

    def test_metadata_fixture_pagination_cache_and_failed_refresh(self):
        control = self.path / "fixture-control.json"
        control.write_text(json.dumps({"pagination": "two_pages"}))
        metadata = NativeMetadata(self.config)
        metadata.preflight()
        value = metadata.snapshot()
        self.assertIn("gpt-6-luna", value["models"])
        self.assertEqual(metadata.snapshot()["sampled_at_ms"], value["sampled_at_ms"])
        control.write_text(json.dumps({"quota": "unknown"}))
        metadata._cached["sampled_monotonic"] -= 2
        self.expect_error("quota_unknown", metadata.snapshot)
        self.assertIsNone(metadata._cached)

    def test_metadata_refresh_rejects_changed_native_binary_version(self):
        control = self.path / "fixture-control.json"
        control.write_text("{}")
        metadata = NativeMetadata(self.config)
        metadata.preflight()
        metadata.snapshot()
        metadata._cached["sampled_monotonic"] -= 2
        control.write_text(json.dumps({"version": "0.999.0"}))
        self.expect_error("native_version_mismatch", metadata.snapshot)
        self.assertIsNone(metadata._cached)

    def test_config_can_parse_the_same_bytes_used_for_broker_identity(self):
        original = json.dumps(self.config).encode()
        path = self.path / "config.json"
        path.write_text("{}")
        self.assertEqual(load_config(str(path), raw=original)["client_id"], "unit-client")

    def test_executable_symlink_preserves_virtual_environment_selection(self):
        executable = self.path / "venv-python"
        executable.symlink_to(sys.executable)
        value = copy.deepcopy(self.config)
        value["codex_bin"] = str(executable)
        value["connector_registry"] = {"fixture": {"transport": "stdio", "command": str(executable),
            "args": [], "env_vars": [], "tools": ["read"]}}
        parsed = load_config(self.path / "unused.json", raw=json.dumps(value).encode())
        self.assertEqual(parsed["codex_bin"], str(executable))
        self.assertEqual(parsed["connector_registry"]["fixture"]["command"], str(executable))

    def test_malformed_ipv6_connector_url_returns_typed_invalid_request(self):
        value = copy.deepcopy(self.config)
        value['connector_registry'] = {'broken': {'transport': 'http', 'url': 'http://[',
            'bearer_token_env_var': None, 'tools': ['read']}}
        with self.assertRaises(WorkerError) as refused:
            load_config(self.path / 'unused.json', raw=json.dumps(value).encode())
        self.assertEqual(refused.exception.code, 'invalid_request')

    def test_config_restart_timeout_policy_does_not_change_request_identity(self):
        request = dict(self.request, cwd=str(self.path), request_id=str(uuid.uuid4()), timeout_s=10)
        original = validate("delegate", request, self.config)
        smaller = copy.deepcopy(self.config)
        smaller["limits"]["max_timeout_s"] = 5
        normalized_retry = validate("delegate", request, smaller)
        self.assertEqual(canonical(original), canonical(normalized_retry))
        self.expect_error("invalid_request", worker_admission.admit,
                          normalized_retry, smaller, self.metadata)
        self.metadata.snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
