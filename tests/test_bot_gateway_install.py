"""integrations/bot-gateway/install.sh in a throwaway HOME with fake tailscale / launchctl / curl on PATH.
Nothing real is touched: no LaunchAgent is loaded, no network call is made, no real provider.env is read."""
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "integrations" / "bot-gateway" / "install.sh"
FAKE_IP = "192.0.2.77"                        # RFC 5737 documentation address: never a real machine
PROVIDER_SECRET = "provider-secret-value-0123456789abcdef"


class InstallerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.calls = Path(self.tmp.name) / "launchctl.calls"
        self._fake("tailscale", f'[ "$1 $2" = "ip -4" ] && echo {FAKE_IP}\n')
        self._fake("launchctl", f'echo "$*" >> {self.calls}\n'
                                'case "$1" in print) echo "    state = running"; echo "    pid = 4242";; esac\n')
        self._fake("curl", "printf 401\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    def run_sh(self, *args):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin"}
        return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def provider_env(self, text):
        d = self.home / ".cortados"
        d.mkdir(exist_ok=True)
        (d / "provider.env").write_text(text)
        os.chmod(d / "provider.env", 0o600)

    def token_path(self):
        return self.home / ".config" / "cortados" / "bot-gateway-token"

    def plist(self):
        return self.home / "Library" / "LaunchAgents" / "com.cortados.bot-gateway.plist"


class TokenTest(InstallerHarness):
    def test_token_created_0600_and_never_printed(self):
        r = self.run_sh("token")
        self.assertEqual(r.returncode, 0, r.stderr)
        tok = self.token_path().read_text()
        self.assertGreaterEqual(len(tok), 43)                       # 32 bytes, base64url
        self.assertRegex(tok, r"^[A-Za-z0-9_-]+$")
        st = self.token_path().lstat()
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertNotIn(tok, r.stdout + r.stderr)
        self.assertIn(f"cat {self.token_path()}", r.stdout)
        # Rerun keeps the token and still prints nothing of it.
        r2 = self.run_sh("token")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.token_path().read_text(), tok)
        self.assertNotIn(tok, r2.stdout + r2.stderr)

    def test_existing_loose_token_refused(self):
        self.token_path().parent.mkdir(parents=True)
        self.token_path().write_text("x" * 43)
        os.chmod(self.token_path(), 0o644)
        r = self.run_sh("token")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("x" * 43, r.stdout + r.stderr)


class PlistTest(InstallerHarness):
    def install(self):
        r = self.run_sh("token")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_sh("plist")
        return r, self.token_path().read_text()

    def check_common(self, r, tok):
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = self.plist().read_bytes().decode()
        doc = plistlib.loads(raw.encode())
        for secret in (tok, PROVIDER_SECRET):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, r.stdout + r.stderr)
        self.assertNotIn("--enable-start-task", raw)
        self.assertTrue(doc["RunAtLoad"])
        self.assertTrue(doc["KeepAlive"])
        log = str(self.home / ".cortados" / "bot-gateway.out")
        self.assertEqual(doc["StandardOutPath"], log)
        self.assertEqual(doc["StandardErrorPath"], log)
        self.assertEqual(doc["EnvironmentVariables"]["CORTADOS_PROVIDER_URL"], f"http://{FAKE_IP}:8432")
        self.assertNotIn("CORTADOS_PROVIDER_TOKEN", doc["EnvironmentVariables"])
        self.assertTrue((self.home / "cortados-bot-workspace").is_dir())
        self.assertEqual(stat.S_IMODE(self.token_path().stat().st_mode), 0o600)
        return doc

    def gateway_args(self, cmdline):
        self.assertIn(f"--host {FAKE_IP} --port 8434", cmdline)
        self.assertIn(f"--token-file {self.token_path()}", cmdline)
        self.assertIn(f"--workspace-root {self.home / 'cortados-bot-workspace'}", cmdline)
        self.assertIn("-m cortados_router.bot_gateway", cmdline)

    def test_design_a_sources_provider_env(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\nOTHER=1\n")
        r, tok = self.install()
        doc = self.check_common(r, tok)
        args = doc["ProgramArguments"]
        self.assertEqual(args[:3], ["/bin/zsh", "-f", "-c"])
        script = args[3]
        self.assertIn(f". {self.home / '.cortados' / 'provider.env'}", script)
        self.assertIn("export CORTADOS_PROVIDER_TOKEN", script)
        self.assertNotIn("set -a", script)                   # only the token is exported, not the whole file
        self.gateway_args(script)

    def test_design_a_script_runs_with_only_the_token_exported(self):
        """Run the plist's zsh line with the gateway swapped for `env`: the token arrives, OTHER does not."""
        if not os.path.exists("/bin/zsh"):
            self.skipTest("no /bin/zsh")
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\nOTHER=leak\n"
                          "CORTADOS_PROVIDER_URL=http://elsewhere:1\n")
        r, tok = self.install()
        doc = self.check_common(r, tok)
        script = doc["ProgramArguments"][3].split("exec ")[0] + "exec /usr/bin/env"
        out = subprocess.run(["/bin/zsh", "-f", "-c", script], env=dict(doc["EnvironmentVariables"]),
                             capture_output=True, text=True, timeout=30).stdout
        self.assertIn(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}", out)
        self.assertIn(f"CORTADOS_PROVIDER_URL=http://{FAKE_IP}:8432", out)
        self.assertNotIn("OTHER=", out)

    def test_design_b_provider_token_file(self):
        self.provider_env("CORTADOS_PROVIDER_CLAUDE_BIN=claude\n")       # no token variable
        r = self.run_sh("token")
        self.assertEqual(r.returncode, 0, r.stderr)
        ptf = self.token_path().parent / "bot-gateway-provider-token"
        ptf.write_text(PROVIDER_SECRET)
        os.chmod(ptf, 0o600)
        r = self.run_sh("plist")
        doc = self.check_common(r, self.token_path().read_text())
        self.assertEqual(doc["EnvironmentVariables"]["CORTADOS_PROVIDER_TOKEN_FILE"], str(ptf))
        self.assertEqual(doc["ProgramArguments"][0], "/usr/bin/python3")
        self.gateway_args(" ".join(doc["ProgramArguments"]))

    def test_no_provider_token_refused(self):
        self.provider_env("CORTADOS_PROVIDER_CLAUDE_BIN=claude\n")
        self.run_sh("token")
        r = self.run_sh("plist")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.plist().exists())

    def test_no_tailnet_ip_refused(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self._fake("tailscale", "echo 'Tailscale is stopped.' >&2; exit 1\n")
        self.run_sh("token")
        r = self.run_sh("plist")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.plist().exists())

    def test_plist_without_token_refused(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        r = self.run_sh("plist")
        self.assertNotEqual(r.returncode, 0)


class LaunchctlTest(InstallerHarness):
    def test_load_status_unload(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.run_sh("token")
        self.assertEqual(self.run_sh("plist").returncode, 0)
        tok = self.token_path().read_text()
        r = self.run_sh("load")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.run_sh("status")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertIn("state = running", s.stdout)
        self.assertIn("HTTP 401", s.stdout)
        self.assertEqual(self.run_sh("unload").returncode, 0)
        for out in (r.stdout + r.stderr, s.stdout + s.stderr):
            self.assertNotIn(tok, out)
            self.assertNotIn(PROVIDER_SECRET, out)
        uid = os.getuid()
        calls = self.calls.read_text().splitlines()
        self.assertIn(f"bootstrap gui/{uid} {self.plist()}", calls)
        self.assertIn(f"print gui/{uid}/com.cortados.bot-gateway", calls)
        self.assertEqual(calls.count(f"bootout gui/{uid}/com.cortados.bot-gateway"), 2)   # load's reset + unload

    def test_status_fails_without_401(self):
        self._fake("curl", "printf 000\n")
        s = self.run_sh("status")
        self.assertNotEqual(s.returncode, 0)
        self.assertIn("HTTP 000", s.stdout)


if __name__ == "__main__":
    unittest.main()
