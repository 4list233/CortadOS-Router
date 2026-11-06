"""Endpoint resolution only: no network, vendor CLI or real machine configuration."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cortados_router import subagent_mcp as m


class ProviderURLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.file = self.home / ".config/cortados/provider-url"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("  https://provider.example:8443/\n")
        self.env = {"HOME": str(self.home)}
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_default_https_and_gateway_shared_resolver(self):
        from cortados_router import bot_gateway
        self.assertEqual(m._resolve_url(), "https://provider.example:8443")
        self.assertIs(bot_gateway.subagent_mcp.resolve_provider_url, m.resolve_provider_url)

    def test_precedence_and_explicit_file(self):
        self.env["CORTADOS_PROVIDER_URL_FILE"] = str(self.home / "other")
        Path(self.env["CORTADOS_PROVIDER_URL_FILE"]).write_text("https://other.example///")
        self.assertEqual(m.resolve_provider_url(self.env), "https://other.example")
        self.env["CORTADOS_PROVIDER_URL"] = " https://env.example/ "
        self.assertEqual(m.resolve_provider_url(self.env), "https://env.example")
        self.env["CORTADOS_PROVIDER_URL"] = ""
        self.assertEqual(m.resolve_provider_url(self.env), "https://other.example")

    def test_invalid_sources_do_not_fall_back_or_echo_contents(self):
        for bad in ("", "ftp://secret.example", "https://", "https:///path", "${URL}",
                    "https://host\nhttps://other", "https://host:bad", "https://[bad"):
            with self.subTest(bad=bad):
                self.file.write_text(bad)
                with self.assertRaisesRegex(m.ProviderURLError, "provider URL file"):
                    m.resolve_provider_url(self.env)
        self.env["CORTADOS_PROVIDER_URL"] = "secret-invalid"
        with self.assertRaises(m.ProviderURLError) as raised:
            m.resolve_provider_url(self.env)
        self.assertIn("CORTADOS_PROVIDER_URL", str(raised.exception))
        self.assertNotIn("secret-invalid", str(raised.exception))

    def test_missing_unreadable_and_invalid_encoding(self):
        self.file.unlink()
        self.assertEqual(m.resolve_provider_url(self.env, required=False), "")
        with self.assertRaisesRegex(m.ProviderURLError, "not found"):
            m.resolve_provider_url(self.env)
        self.env["CORTADOS_PROVIDER_URL_FILE"] = str(self.file)
        with self.assertRaises(m.ProviderURLError):
            m.resolve_provider_url(self.env, required=False)
        self.file.mkdir()
        with self.assertRaisesRegex(m.ProviderURLError, "cannot read"):
            m.resolve_provider_url(self.env)
        self.file.rmdir()
        self.file.write_bytes(b"\xff")
        with self.assertRaisesRegex(m.ProviderURLError, "cannot read"):
            m.resolve_provider_url(self.env)

    def test_standalone_import(self):
        spec = importlib.util.spec_from_file_location("standalone_mcp", m.__file__)
        standalone = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standalone)
        self.assertEqual(standalone.resolve_provider_url(self.env), m.resolve_provider_url(self.env))

    def test_https_route_request(self):
        seen = []
        def open_(req, timeout):
            seen.append(req.full_url)
            raise OSError("test transport")
        m.route_task(m.resolve_provider_url(self.env), "test-token-12345", "task", urlopen=open_)
        self.assertEqual(seen, ["https://provider.example:8443/v1/messages"])

    def test_https_child_run_request(self):
        from cortados_router import child_tasks
        with mock.patch.object(child_tasks, "_urlopen", side_effect=OSError("test transport")) as opened:
            child_tasks.fetch_run(m.resolve_provider_url(self.env), "test-token-12345", "test-run-id")
        self.assertEqual(opened.call_args[0][0].full_url,
                         "https://provider.example:8443/v1/cortados/runs/test-run-id")
