"""The verify-router skill's helpers (#112): where verification finds S1 and what its live-home check compares.
Fakes only: no network, no real provider.env, no live ~/.cortados, no child process."""
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("verify_router_skill", REPO / ".claude" / "skills" / "verify-router" / "verify.py")
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

TOKEN = "s1-secret-value-xyz"


class S1ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.envfile = Path(self.tmp.name) / "provider.env"

    def write(self, text):
        self.envfile.write_text(text)
        os.chmod(self.envfile, 0o600)

    def test_environment_wins(self):
        self.write(f"CORTADOS_S1_URL=http://192.0.2.5:8431\nCORTADOS_S1_TOKEN={TOKEN}\n")
        settings, source = verify.s1_config({"CORTADOS_S1_URL": "http://127.0.0.1:9999"}, self.envfile)
        self.assertEqual(settings, {})
        self.assertIn("environment", source)

    def test_provider_env_gives_only_s1_names(self):
        self.write("# live provider\nexport CORTADOS_PROVIDER_TOKEN=prov\n"
                   f"CORTADOS_S1_URL='http://192.0.2.5:8431'\nCORTADOS_S1_TOKEN=\"{TOKEN}\"\nCODEX_CA_CERTIFICATE=/x\n")
        settings, source = verify.s1_config({}, self.envfile)
        self.assertEqual(settings, {"CORTADOS_S1_URL": "http://192.0.2.5:8431", "CORTADOS_S1_TOKEN": TOKEN})
        self.assertIn("provider.env", source)
        self.assertNotIn(TOKEN, source)
        # the children's environment then resolves the same scorer the live provider does
        self.assertEqual(verify.resolve_url(environ={**settings}), "http://192.0.2.5:8431")

    def test_provider_env_is_data_never_shell(self):
        self.write(f"CORTADOS_S1_TOKEN={TOKEN}\nCORTADOS_S1_URL=$(echo http://evil)\n")
        settings, source = verify.s1_config({}, self.envfile)
        self.assertEqual(settings, {})
        self.assertIn("line 2", source)
        self.assertNotIn(TOKEN, source)
        self.assertNotIn("evil", source)

    def test_missing_or_s1_free_file_falls_back_to_router_default(self):
        settings, source = verify.s1_config({}, self.envfile)
        self.assertEqual((settings, "router default" in source), ({}, True))
        self.write("CORTADOS_PROVIDER_TOKEN=prov\n")
        settings, source = verify.s1_config({}, self.envfile)
        self.assertEqual(settings, {})
        self.assertIn("sets no CORTADOS_S1_*", source)

    def test_runner_hands_settings_and_sandbox_to_children(self):
        r = verify.Runner({"CORTADOS_S1_URL": "http://192.0.2.5:8431", "CORTADOS_S1_TOKEN": TOKEN})
        env = r.env("verify-test-")
        self.addCleanup(lambda: [__import__("shutil").rmtree(s, ignore_errors=True) for s in r.sandboxes])
        sandbox = r.sandboxes[0]
        self.assertEqual(env["HOME"], str(sandbox / "home"))
        self.assertEqual(env["CORTADOS_HOME"], str(sandbox / "state"))
        self.assertEqual(env["CORTADOS_SCARCITY_REFRESH"], "off")
        self.assertEqual((env["CORTADOS_S1_URL"], env["CORTADOS_S1_TOKEN"]), ("http://192.0.2.5:8431", TOKEN))
        self.assertEqual(verify.escaped_homes(r.sandboxes), [])
        (sandbox / "home" / ".cortados").mkdir()
        self.assertEqual(verify.escaped_homes(r.sandboxes), [sandbox])


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class S1ProbeTest(unittest.TestCase):
    def test_health_with_bearer(self):
        seen = {}

        def urlopen(req, timeout):
            seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
            return _Resp(b"{}")
        self.assertEqual(verify.s1_probe("http://192.0.2.5:8431", TOKEN, urlopen), (True, "/health 200"))
        self.assertEqual(seen, {"url": "http://192.0.2.5:8431/health", "auth": f"Bearer {TOKEN}"})

    def test_http_error_is_listening_and_refused_is_down(self):
        def denied(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        self.assertEqual(verify.s1_probe("http://192.0.2.5:8431", TOKEN, denied), (True, "/health HTTP 401"))

        def refused(req, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        up, detail = verify.s1_probe("http://127.0.0.1:8431", TOKEN, refused)
        self.assertFalse(up)
        self.assertIn("URLError", detail)
        self.assertIn("Connection refused", detail)
        self.assertNotIn(TOKEN, detail)

    def test_shown_url_masks_private_hosts(self):
        self.assertEqual(verify.shown_url("http://127.0.0.1:8431"), "http://127.0.0.1:8431")
        self.assertEqual(verify.shown_url("http://192.0.2.5:8431"), "http://<private host>:8431")
        self.assertEqual(verify.shown_url("https://studio.example.ts.net"), "https://<private host>")


class LiveHomeCheckTest(unittest.TestCase):
    LIVE = {"router.db": (1, 10), "provider.out": (1, 5), "child-tasks.jsonl": (1, 7), "codex-home/log": (1, 1),
            "unit-cost.json": (1, 3)}

    def test_live_service_writes_do_not_fail(self):
        after = {**self.LIVE, "router.db": (2, 11), "provider.out": (2, 6), "child-tasks.jsonl": (2, 8),
                 "codex-home/log": (2, 2), "codex-home/new": (2, 1)}
        verdict, evidence = verify.live_home_check(self.LIVE, after, {"router.db", "router.db-journal"}, 4, 4)
        self.assertEqual(verdict, "PASS", evidence)
        self.assertIn("5 file(s) changed by the live services", evidence)

    def test_a_path_verification_writes_changed(self):
        after = {**self.LIVE, "unit-cost.json": (2, 3)}
        verdict, evidence = verify.live_home_check(self.LIVE, after, {"router.db", "unit-cost.json"}, 0, 0)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("unit-cost.json", evidence)
        # created in the live home during the run counts as changed too
        verdict, _ = verify.live_home_check(self.LIVE, {**self.LIVE, "usage-probe/x": (2, 1)}, {"usage-probe/x"}, 0, 0)
        self.assertEqual(verdict, "FAIL")

    def test_new_verification_rows_fail_and_unreadable_db_blocks(self):
        verdict, evidence = verify.live_home_check(self.LIVE, self.LIVE, {"router.db"}, 3, 5)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("2 row(s)", evidence)
        self.assertEqual(verify.live_home_check(self.LIVE, self.LIVE, {"router.db"}, None, 0)[0], "BLOCKED")

    def test_verify_rows_counts_only_verification_platform(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "router.db"
            self.assertEqual(verify.verify_rows(db), 0)                      # missing file
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE other (x)")
            c.commit()
            self.assertEqual(verify.verify_rows(db), 0)                      # no routes table yet
            c.execute("CREATE TABLE routes (turn_id TEXT PRIMARY KEY, platform TEXT)")
            c.executemany("INSERT INTO routes VALUES (?, ?)",
                          [("a", "provider:verify"), ("b", "provider:claude-code"), ("c", "provider:verify"), ("d", None)])
            c.commit()
            c.close()
            self.assertEqual(verify.verify_rows(db), 2)

    def test_file_stamps_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "sub").mkdir()
            (Path(d) / "sub" / "f").write_text("abc")
            (Path(d) / "router.db").write_text("x")
            stamps = verify.file_stamps(d)
            self.assertEqual(set(stamps), {"sub/f", "router.db"})
            self.assertEqual(stamps["sub/f"][1], 3)
            self.assertEqual(verify.file_stamps(Path(d) / "missing"), {})


if __name__ == "__main__":
    unittest.main()
