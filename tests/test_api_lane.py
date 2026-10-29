"""Tests for the anthropic-api credit lane's pin + admission-controlled executor hookup in the provider server.
No live API and no real ~/.cortados state: api_credits and anthropic_proxy.serve are stubbed."""
import os
import unittest
from unittest import mock

from cortados_router import api_credits
from cortados_router.provider.server import Handler


class TestApiPin(unittest.TestCase):
    def test_pin_detection(self):
        self.assertEqual(Handler._api_pin({"model": "api/claude-opus-5-5"}), "claude-opus-5-5")
        self.assertEqual(Handler._api_pin({"model": "api/claude-haiku-5-5"}), "claude-haiku-5-5")
        self.assertEqual(Handler._api_pin({"model": "api"}), "claude-opus-5-5")
        self.assertIsNone(Handler._api_pin({"model": "cortados"}))
        self.assertIsNone(Handler._api_pin({"model": ""}))


class TestServeApiLane(unittest.TestCase):
    def _handler(self):
        h = Handler.__new__(Handler)
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

    def test_happy_path_reserves_serves_and_settles_actual(self):
        h = self._handler()
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}  # opus-5-5 -> $4, has output_tokens -> complete
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value="2026-11:abc"), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=usage), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            settle.assert_called_once()
            # settled with the actual cost ($4), not the full reservation
            self.assertAlmostEqual(settle.call_args[0][1], 4.0, places=4)
        self.assertEqual(h.errors, [])

    def test_disconnect_settles_the_full_reservation(self):
        h = self._handler()
        incomplete = {"input_tokens": 1000}  # no output_tokens -> treated as incomplete
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
             mock.patch("cortados_router.api_credits.reserve", return_value="2026-11:abc"), \
             mock.patch("cortados_router.api_credits.estimate_max_cost", return_value=12.34), \
             mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=incomplete), \
             mock.patch("cortados_router.api_credits.settle") as settle:
            h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
            self.assertAlmostEqual(settle.call_args[0][1], 12.34, places=4)   # full reservation, conservative


if __name__ == "__main__":
    unittest.main()
