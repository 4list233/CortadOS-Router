"""Tests for the anthropic-api credit lane's pin + executor hookup in the provider server. No live API and no real
~/.cortados state: anthropic_proxy.serve is stubbed and the meter is redirected to a temp CORTADOS_HOME."""
import os
import tempfile
import unittest
from unittest import mock

from cortados_router import api_credits
from cortados_router.provider.server import Handler


class TestApiPin(unittest.TestCase):
    def test_pin_detection(self):
        self.assertEqual(Handler._api_pin({"model": "api/claude-opus-5-5"}), "claude-opus-5-5")
        self.assertEqual(Handler._api_pin({"model": "api/claude-haiku-5-5"}), "claude-haiku-5-5")
        self.assertEqual(Handler._api_pin({"model": "api"}), "claude-opus-5-5")  # bare -> default
        self.assertIsNone(Handler._api_pin({"model": "cortados"}))               # the routed model is not a pin
        self.assertIsNone(Handler._api_pin({"model": ""}))
        self.assertIsNone(Handler._api_pin({}))


class TestServeApiLane(unittest.TestCase):
    def _handler(self):
        h = Handler.__new__(Handler)  # no socket; we only exercise _serve_api_lane
        h.errors = []
        h._error = lambda code, kind, msg, w: h.errors.append((code, msg))
        return h

    def test_refuses_without_a_key(self):
        h = self._handler()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY", None)
            h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
        self.assertEqual(h.errors[0][0], 503)
        self.assertIn("anthropic-api", h.errors[0][1])

    def test_refuses_when_over_budget(self):
        home = tempfile.mkdtemp()
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_HOME": home, "CORTADOS_ANTHROPIC_API_KEY": "sk-x",
                                          "CORTADOS_API_CREDIT_BUDGET_USD": "200"}):
            # spend the whole budget this month, then the lane must refuse (and never call serve)
            api_credits.record("claude-opus-5-5", {"output_tokens": 10_000_000}, 1793000000.0)  # $200
            with mock.patch("cortados_router.provider.anthropic_proxy.serve") as served, \
                 mock.patch("cortados_router.api_credits.spent", return_value=200.0), \
                 mock.patch("cortados_router.api_credits.over_budget", return_value=True):
                h._serve_api_lane({"model": "api"}, "claude-opus-5-5", None)
                served.assert_not_called()
        self.assertEqual(h.errors[0][0], 429)

    def test_proxies_when_configured_and_under_budget(self):
        home = tempfile.mkdtemp()
        h = self._handler()
        with mock.patch.dict(os.environ, {"CORTADOS_HOME": home, "CORTADOS_ANTHROPIC_API_KEY": "sk-x"}):
            with mock.patch("cortados_router.provider.anthropic_proxy.serve") as served:
                h._serve_api_lane({"model": "api/claude-opus-5-5"}, "claude-opus-5-5", None)
                served.assert_called_once()
                # the Claude model is what gets executed on the lane
                self.assertEqual(served.call_args[0][2], "claude-opus-5-5")
        self.assertEqual(h.errors, [])


if __name__ == "__main__":
    unittest.main()
