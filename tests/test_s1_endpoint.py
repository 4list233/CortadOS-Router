"""The S1 endpoint resolver: explicit URL wins, then a named device from the endpoints file, then loopback; the server
bind follows CORTADOS_S1_BIND. No network and no real config file: every case passes its own environ and temp file."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from cortados_router import s1_endpoint as ep


def env(**kw):
    return {k: v for k, v in kw.items() if v is not None}


class ResolveUrlTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / "s1-endpoints.json"

    def write(self, data):
        self.file.write_text(json.dumps(data))
        return str(self.file)

    def test_loopback_when_nothing_configured(self):
        self.assertEqual(ep.resolve_url(environ={}), ep.DEFAULT_URL)

    def test_explicit_url_wins_over_everything(self):
        path = self.write({"default": "studio", "endpoints": {"studio": "http://10.0.0.5:8431"}})
        e = env(CORTADOS_S1_URL="http://127.0.0.1:9999/", CORTADOS_S1_ENDPOINTS=path, CORTADOS_S1_ENDPOINT="studio")
        self.assertEqual(ep.resolve_url(environ=e), "http://127.0.0.1:9999")   # trailing slash stripped

    def test_named_endpoint_selected(self):
        path = self.write({"default": "local", "endpoints": {"local": "http://127.0.0.1:8431", "studio": "http://10.0.0.5:8431"}})
        e = env(CORTADOS_S1_ENDPOINTS=path, CORTADOS_S1_ENDPOINT="studio")
        self.assertEqual(ep.resolve_url(environ=e), "http://10.0.0.5:8431")

    def test_default_endpoint_used_without_selection(self):
        path = self.write({"default": "studio", "endpoints": {"studio": "http://10.0.0.5:8431"}})
        self.assertEqual(ep.resolve_url(environ=env(CORTADOS_S1_ENDPOINTS=path)), "http://10.0.0.5:8431")

    def test_file_without_default_falls_back_to_loopback(self):
        path = self.write({"endpoints": {"studio": "http://10.0.0.5:8431"}})
        self.assertEqual(ep.resolve_url(environ=env(CORTADOS_S1_ENDPOINTS=path)), ep.DEFAULT_URL)

    def test_unknown_name_is_an_error_listing_known(self):
        path = self.write({"endpoints": {"studio": "http://10.0.0.5:8431"}})
        e = env(CORTADOS_S1_ENDPOINTS=path, CORTADOS_S1_ENDPOINT="air")
        with self.assertRaises(ValueError) as cm:
            ep.resolve_url(environ=e)
        self.assertIn("studio", str(cm.exception))      # the error names the known devices

    def test_named_endpoint_without_file_is_an_error(self):
        e = env(CORTADOS_S1_ENDPOINTS=str(self.dir / "absent.json"), CORTADOS_S1_ENDPOINT="studio")
        with self.assertRaises(ValueError):
            ep.resolve_url(environ=e)

    def test_explicit_missing_file_raises_even_without_a_name(self):
        # an operator who set CORTADOS_S1_ENDPOINTS meant that file to exist: its absence is an error, not loopback
        e = env(CORTADOS_S1_ENDPOINTS=str(self.dir / "absent.json"))
        with self.assertRaises(ValueError):
            ep.resolve_url(environ=e)

    def test_non_string_default_raises(self):
        for bad in (False, [], 0, {}):
            path = self.write({"default": bad, "endpoints": {"studio": "http://10.0.0.5:8431"}})
            with self.assertRaises(ValueError):
                ep.resolve_url(environ=env(CORTADOS_S1_ENDPOINTS=path))

    def test_malformed_file_raises_not_silently_loopback(self):
        self.file.write_text("{not json")
        with self.assertRaises(ValueError):
            ep.resolve_url(environ=env(CORTADOS_S1_ENDPOINTS=str(self.file)))

    def test_non_http_url_in_file_rejected(self):
        path = self.write({"default": "studio", "endpoints": {"studio": "10.0.0.5:8431"}})
        with self.assertRaises(ValueError):
            ep.resolve_url(environ=env(CORTADOS_S1_ENDPOINTS=path))


class ClientAuthTests(unittest.TestCase):
    """core.decide_s1 must present the bearer token (when configured) and its own timeout, or a token-guarded scorer
    rejects every routing decision with 401."""

    def _capture(self, token):
        from cortados_router import core
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
            raise urllib.error.URLError("captured")          # abort after the headers are built

        with patch.object(core, "S1_TOKEN", token), patch("urllib.request.urlopen", fake_urlopen):
            core.decide_s1("hello")
        return seen["headers"]

    def test_sends_bearer_and_timeout_when_token_set(self):
        h = self._capture("abc")
        self.assertEqual(h.get("authorization"), "Bearer abc")
        self.assertIn("x-request-timeout-s", h)

    def test_no_authorization_header_without_token(self):
        h = self._capture(None)
        self.assertNotIn("authorization", h)
        self.assertIn("x-request-timeout-s", h)              # timeout is always propagated


class BindAddressTests(unittest.TestCase):
    def test_loopback_default(self):
        self.assertEqual(ep.bind_address(environ={}), ("127.0.0.1", 8431))

    def test_bind_and_port_overrides(self):
        e = {"CORTADOS_S1_BIND": "100.100.100.100", "CORTADOS_S1_PORT": "8500"}
        self.assertEqual(ep.bind_address(environ=e), ("100.100.100.100", 8500))


if __name__ == "__main__":
    unittest.main()
