"""Tests for the anthropic-api credit lane's pin + admission-controlled executor hookup in the provider server.
No live API and no real ~/.cortados state: api_credits and anthropic_proxy.serve are stubbed."""
import os
import unittest
from unittest import mock

from cortados_router import api_credits
from cortados_router.provider.server import Handler

RID = "2026-11-22:abc"     # a reservation id as api_credits.reserve mints them (UTC day : random)


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
        h._error = lambda code, kind, msg, w, **kw: h.errors.append((code, msg))
        return h

    def test_refuses_without_a_key(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY", None)
            h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
        self.assertEqual(h.errors[0][0], 503)
        self.assertIn("no API key", h.errors[0][1])

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
        usage = {"input_tokens": 100_000, "output_tokens": 0}   # opus-5-5 input -> $0.40 already observed on the wire
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.api_credits.estimate_max_cost", return_value=0.1), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=(usage, "unknown")), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            self.assertAlmostEqual(settle.call_args[0][1], 0.4, places=4)   # max(reservation 0.1, observed 0.4)

    def test_completed_turn_settles_the_actual_cost(self):
        h = self._handler()
        usage = {"input_tokens": 100_000, "output_tokens": 0}  # opus-5-5 input -> $0.40
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=(usage, "completed")), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            settle.assert_called_once()
            self.assertAlmostEqual(settle.call_args[0][1], 0.4, places=4)
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

    def test_an_unbounded_request_is_refused_before_any_reservation(self):
        h = self._handler()
        img = {"type": "image", "source": {"type": "url", "url": "https://example.com/x.png"}}
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve") as reserve, \
             mock.patch("cortados_router.provider.anthropic_proxy.serve") as served:
            h._serve_api_lane({"model": "api", "messages": [{"role": "user", "content": [img]}]}, "claude-opus-5-5",
                              None)
            reserve.assert_not_called()
            served.assert_not_called()
        self.assertEqual(h.errors[0][0], 400)
        self.assertIn("cannot bound", h.errors[0][1])

    def test_a_second_key_turns_the_lane_off(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk", "CORTADOS_ANTHROPIC_API_KEY_2": "sk2"}), \
             mock.patch("cortados_router.api_credits.reserve") as reserve, \
             mock.patch("cortados_router.provider.anthropic_proxy.serve") as served:
            h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
            reserve.assert_not_called()
            served.assert_not_called()
        self.assertEqual(h.errors[0][0], 503)
        self.assertNotIn("sk2", h.errors[0][1])

    def test_the_proxy_gets_the_key_and_never_the_error_text(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk-credit"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value=RID), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=({}, "rejected")) as served, \
             mock.patch("cortados_router.api_credits.settle"):
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            self.assertEqual(served.call_args.kwargs["key"], "sk-credit")


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
