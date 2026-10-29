"""Tests for the anthropic-api credit lane's pin + admission-controlled executor hookup in the provider server.
No live API and no real ~/.cortados state: api_credits and anthropic_proxy.serve are stubbed."""
import os
import unittest
from unittest import mock

from cortados_router import api_credits
from cortados_router.provider.server import Handler

# A realistic reservation id for the test key "sk": its org fingerprint, so key_for_reservation() resolves to "sk".
RID = api_credits._org_id("sk") + "|2026-11:abc"


class TestApiPin(unittest.TestCase):
    def test_pin_detection(self):
        self.assertEqual(Handler._api_pin({"model": "api/claude-opus-5-5"}), "claude-opus-5-5")
        self.assertEqual(Handler._api_pin({"model": "api/claude-haiku-5-5"}), "claude-haiku-5-5")
        self.assertEqual(Handler._api_pin({"model": "api"}), "claude-opus-5-5")
        self.assertIsNone(Handler._api_pin({"model": "cortados"}))
        self.assertIsNone(Handler._api_pin({"model": ""}))


class TestServeApiLane(unittest.TestCase):
    def _handler(self, headers=None):
        h = Handler.__new__(Handler)
        if headers is not None:
            h.headers = headers
        h.errors = []
        h._error = lambda code, kind, msg, w: h.errors.append((code, msg))
        return h

    def test_refuses_without_a_key(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY", None)
            h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
        self.assertEqual(h.errors[0][0], 503)

    def test_refuses_when_reservation_rejected(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=None), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve") as served:
            h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
            served.assert_not_called()
        self.assertEqual(h.errors[0][0], 429)

    def test_fails_closed_when_accounting_unavailable(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", side_effect=api_credits.LedgerError("down")), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve") as served:
            h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
            served.assert_not_called()                 # never spend when accounting is unavailable
        self.assertEqual(h.errors[0][0], 503)

    def test_refuses_an_unbounded_server_tool_before_dispatch(self):
        h = self._handler()
        body = {"model": "api", "tools": [{"type": "code_execution_20250522", "name": "code_execution"}]}
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve") as reserve, \
             mock.patch("cortados_router.provider.anthropic_proxy.serve") as served:
            h._serve_api_lane(body, "claude-opus-5-5", None)
            reserve.assert_not_called()                # refused before any reservation or dispatch
            served.assert_not_called()
        self.assertEqual(h.errors[0][0], 400)

    def test_unknown_outcome_never_settles_below_observed_cost(self):
        h = self._handler()
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}   # opus-5-5 input -> $4 already observed on the wire
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.api_credits.estimate_max_cost", return_value=1.0), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=(usage, "unknown")), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            self.assertAlmostEqual(settle.call_args[0][1], 4.0, places=4)   # max(reservation 1.0, observed 4.0)

    def test_completed_turn_settles_the_actual_cost(self):
        h = self._handler()
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}  # opus-5-5 input -> $4
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=(usage, "completed")), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            settle.assert_called_once()
            self.assertAlmostEqual(settle.call_args[0][1], 4.0, places=4)
        self.assertEqual(h.errors, [])

    def test_unknown_outcome_settles_the_full_reservation(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.api_credits.estimate_max_cost", return_value=12.34), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=({"input_tokens": 1000}, "unknown")), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            self.assertAlmostEqual(settle.call_args[0][1], 12.34, places=4)   # full reservation, conservative

    def test_rejected_turn_settles_zero(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=({}, "rejected")), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            self.assertEqual(settle.call_args[0][1], 0.0)   # the API rejected it; nothing billed

    def test_reserved_org_deconfigured_releases_and_refuses(self):
        # reserve succeeded against an org whose key is no longer configured (res_id org != any configured key):
        # release the reservation (settle 0) and refuse, never dispatch with a different org's key.
        h = self._handler()
        orphan = api_credits._org_id("sk-GONE") + "|2026-11:abc"
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=orphan), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve") as served, \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            served.assert_not_called()                      # never dispatched with the wrong key
            self.assertEqual(settle.call_args[0][1], 0.0)   # reservation released
        self.assertEqual(h.errors[0][0], 503)



class TestApiLaneReveal(unittest.TestCase):
    """x-cortados-reveal on an api/-pinned request adds the credit window's % left to the upstream's real model."""

    def _serve(self, headers):
        h = TestServeApiLane._handler(None, headers)
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.api_credits.window_numbers", lambda now: (40.4, now + 1, 2.0)), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=({}, "rejected")) as served, \
             mock.patch("cortados_router.api_credits.settle"):
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
        return served.call_args

    def test_reveal_passes_the_percent_suffix(self):
        self.assertEqual(self._serve({"x-cortados-reveal": "1"}).kwargs.get("model_suffix"), " · 60% left")

    def test_without_the_header_the_proxy_call_is_unchanged(self):
        self.assertNotIn("model_suffix", self._serve({}).kwargs)
        self.assertNotIn("model_suffix", self._serve(None).kwargs)      # no headers at all: never raises


if __name__ == "__main__":
    unittest.main()
