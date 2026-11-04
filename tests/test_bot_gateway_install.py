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
FAKE_IP = "100.100.100.100"   # inside 100.64.0.0/10 but Tailscale's fixed service address (MagicDNS): never a node
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

    def test_design_a_runs_the_data_launcher(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\nOTHER=1\n")
        r, tok = self.install()
        doc = self.check_common(r, tok)
        args = doc["ProgramArguments"]
        self.assertEqual(args[:3], ["/usr/bin/python3", "-I", str(REPO / "integrations" / "bot-gateway" / "launch.py")])
        self.assertEqual(args[3:8], ["--provider-env", str(self.home / ".cortados" / "provider.env"),
                                     "--python", "/usr/bin/python3", "--"])
        self.assertNotIn("/bin/zsh", args)                    # provider.env is never executed as shell code
        self.gateway_args(" ".join(args[8:]) + " -m cortados_router.bot_gateway")

    def run_plist_launcher(self, doc, extra_env=None):
        """Run the plist's real ProgramArguments with the gateway interpreter swapped for a fake that records its
        argv and environment. Returns (CompletedProcess, argv list or None, env dict or None)."""
        rec = Path(self.tmp.name) / "rec"
        rec.mkdir(exist_ok=True)
        fake = self.bin / "fake-python"
        # A python fake, not sh: a shell adds PWD/SHLVL/_ of its own, which would hide what the launcher passed.
        fake.write_text("#!/usr/bin/python3 -I\nimport os, sys\n"
                        f"open({str(rec / 'argv')!r}, 'w').write('\\n'.join(sys.argv) + '\\n')\n"
                        f"open({str(rec / 'env')!r}, 'w').write(''.join(f'{{k}}={{v}}\\n' for k, v in os.environ.items()))\n")
        fake.chmod(0o755)
        args = list(doc["ProgramArguments"])
        args[args.index("--python") + 1] = str(fake)
        env = dict(doc["EnvironmentVariables"], HOME=str(self.home), **(extra_env or {}))
        p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30, cwd=doc["WorkingDirectory"])
        argv = (rec / "argv").read_text().splitlines() if (rec / "argv").exists() else None
        got = (dict(l.split("=", 1) for l in (rec / "env").read_text().splitlines() if "=" in l)
               if (rec / "env").exists() else None)
        return p, argv, got

    def test_design_a_child_gets_only_the_allowlist(self):
        self.provider_env(f"# provider settings\n\nexport CORTADOS_PROVIDER_TOKEN='{PROVIDER_SECRET}'\n"
                          "OTHER=leak\nexport OTHER_SECRET=exported-secret-value\n"
                          "export HTTPS_PROXY=http://proxy.invalid:3128\n"
                          "export t=scratch-collision\n"
                          "export CORTADOS_PROVIDER_URL=http://elsewhere:1\n")
        r, tok = self.install()
        doc = self.check_common(r, tok)
        p, argv, got = self.run_plist_launcher(doc, {"PYTHONPATH": "/nowhere", "http_proxy": "http://proxy.invalid:1",
                                                     "t": "inherited", "OTHER_SECRET": "inherited-secret"})
        self.assertEqual(p.returncode, 0, p.stderr)
        # The fake runs under /usr/bin/python3, Apple's xcrun shim, which adds its developer-tools variables, and
        # Python's C-locale coercion adds LC_CTYPE. Those come from the interpreter, not from the launcher, and none
        # of them carries a value from provider.env or the launching environment (checked below).
        for k in ("SDKROOT", "CPATH", "LIBRARY_PATH", "MANPATH", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"):
            got.pop(k, None)
        self.assertEqual(set(got), {"HOME", "PATH", "CORTADOS_HOME", "CORTADOS_PROVIDER_URL",
                                    "CORTADOS_PROVIDER_TOKEN"})
        self.assertEqual(got["CORTADOS_PROVIDER_TOKEN"], PROVIDER_SECRET)
        self.assertEqual(got["CORTADOS_PROVIDER_URL"], f"http://{FAKE_IP}:8432")
        self.assertEqual(got["HOME"], str(self.home))
        self.assertEqual(got["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(argv[1:4], ["-B", "-m", "cortados_router.bot_gateway"])
        self.assertNotIn("--enable-start-task", argv)
        self.assertFalse(any(PROVIDER_SECRET in a for a in argv))
        self.assertNotIn(PROVIDER_SECRET, p.stdout + p.stderr)
        dump = (Path(self.tmp.name) / "rec" / "env").read_text()
        for leaked in ("proxy.invalid", "exported-secret-value", "inherited", "scratch-collision", "/nowhere", "leak"):
            self.assertNotIn(leaked, dump)

    def test_design_a_shell_code_refused_with_78_and_nothing_runs(self):
        cases = {
            "set -x": "set -x\n",
            "trap": "trap 'echo $CORTADOS_PROVIDER_TOKEN' EXIT\n",
            "debug trap": "trap 'export HTTPS_PROXY=x' DEBUG\n",
            "command substitution": "OTHER=$(id -u)\n",
            "backticks": "OTHER=`id -u`\n",
            "expansion in double quotes": 'OTHER="$HOME"\n',
            "readonly": "export LOCKED=1\nreadonly LOCKED\n",
            "command": "echo hi\n",
            "chained": "OTHER=1; echo hi\n",
            "function": "f() { :; }\n",
            "set -a": "set -a\n",
        }
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        r, tok = self.install()
        doc = self.check_common(r, tok)
        for name, bad in cases.items():
            with self.subTest(name):
                self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n{bad}")
                rec = Path(self.tmp.name) / "rec"
                for f in ("argv", "env"):
                    (rec / f).unlink(missing_ok=True) if rec.exists() else None
                p, argv, got = self.run_plist_launcher(doc)
                self.assertEqual(p.returncode, 78, p.stderr)
                self.assertIsNone(argv)
                self.assertRegex(p.stderr, r"line [23]:")       # the first refused line (line 1 is the token)
                self.assertNotIn(PROVIDER_SECRET, p.stdout + p.stderr)
                self.assertNotIn(bad.strip(), p.stderr)          # the line number is named, never the content

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

    def test_bad_tailnet_ip_refused(self):
        cases = {
            "nonzero exit": f"echo {FAKE_IP}; exit 1",
            "stopped": "echo 'Tailscale is stopped.' >&2; exit 1",
            "empty": "exit 0",
            "two lines": f"echo {FAKE_IP}; echo 100.64.0.2",
            "bad octet": "echo 100.64.0.300",
            "wildcard": "echo 0.0.0.0",
            "not CGNAT": "echo 192.0.2.10",
            "loopback": "echo 127.0.0.1",
            "padded": f"echo ' {FAKE_IP}'",
            "leading zero": "echo 100.064.0.1",
            "IPv6": "echo fd7a:115c:a1e0::1",
        }
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.assertEqual(self.run_sh("token").returncode, 0)
        for name, body in cases.items():
            with self.subTest(name):
                self._fake("tailscale", body + "\n")
                r = self.run_sh("plist")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertFalse(self.plist().exists())

    def test_cgnat_edges_accepted(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.assertEqual(self.run_sh("token").returncode, 0)
        for ip in ("100.64.0.1", "100.127.255.254"):
            with self.subTest(ip):
                self._fake("tailscale", f"echo {ip}\n")
                r = self.run_sh("plist")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"--host {ip} ", " ".join(plistlib.loads(self.plist().read_bytes())["ProgramArguments"]))

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

    def test_status_never_prints_log_contents(self):
        log = self.home / ".cortados" / "bot-gateway.out"
        log.parent.mkdir(parents=True)
        leaked = "Bearer-synthetic-credential-ZZ9-in-a-path"
        log.write_text(f"bot-gateway: listening\nbot-gateway: POST /{leaked}\nbot-gateway: POST /mcp\n")
        s = self.run_sh("status")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertNotIn(leaked, s.stdout + s.stderr)
        self.assertNotIn("listening", s.stdout + s.stderr)
        self.assertIn(f"{log} (3 lines; not shown)", s.stdout)

    def test_status_fails_without_401(self):
        self._fake("curl", "printf 000\n")
        s = self.run_sh("status")
        self.assertNotEqual(s.returncode, 0)
        self.assertIn("HTTP 000", s.stdout)


if __name__ == "__main__":
    unittest.main()


class LauncherTest(unittest.TestCase):
    """launch.py directly, with a fake execve: what it would exec, and exit 78 for every config failure."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_gateway_launch", SCRIPT.parent / "launch.py")
        cls.launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.launch)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.envfile = Path(self.tmp.name) / "provider.env"
        self.environ = {"HOME": self.tmp.name, "CORTADOS_HOME": self.tmp.name + "/.cortados",
                        "CORTADOS_PROVIDER_URL": f"http://{FAKE_IP}:8432", "PYTHONPATH": "/nowhere",
                        "HTTPS_PROXY": "http://proxy.invalid:1", "t": "inherited"}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, mode=0o600):
        self.envfile.write_text(text)
        os.chmod(self.envfile, mode)

    def run_main(self, flags=("--host", FAKE_IP, "--port", "8434")):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.launch.main(["--provider-env", str(self.envfile), "--python", "/usr/bin/python3", "--", *flags],
                                  environ=self.environ, execve=lambda *a: self.calls.append(a))
        return rc, err.getvalue()

    def test_quoted_values(self):
        p = self.launch.parse_env("A=plain-1.2:x@y\nexport B='single # kept'\nC=\"double 'q' #\"\n"
                                  "  # indented comment\nD=\nE=''\n")
        self.assertEqual(p, {"A": "plain-1.2:x@y", "B": "single # kept", "C": "double 'q' #", "D": "", "E": ""})

    def test_execve_args_env_and_no_token_in_argv(self):
        self.write(f'export CORTADOS_PROVIDER_TOKEN="{PROVIDER_SECRET}"\n')
        rc, err = self.run_main()
        self.assertEqual(rc, 0, err)
        (prog, argv, env), = self.calls
        self.assertEqual(prog, "/usr/bin/python3")
        self.assertEqual(argv, ["/usr/bin/python3", "-B", "-m", "cortados_router.bot_gateway",
                                "--host", FAKE_IP, "--port", "8434"])
        self.assertFalse(any(PROVIDER_SECRET in a for a in argv))
        self.assertEqual(env, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CORTADOS_PROVIDER_TOKEN": PROVIDER_SECRET,
                               "HOME": self.tmp.name, "CORTADOS_HOME": self.tmp.name + "/.cortados",
                               "CORTADOS_PROVIDER_URL": f"http://{FAKE_IP}:8432"})
        self.assertNotIn(PROVIDER_SECRET, err)

    def test_every_config_failure_exits_78(self):
        good = f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n"
        cases = {
            "no token": ("OTHER=1\n", {}, None),
            "empty token": ("CORTADOS_PROVIDER_TOKEN=\n", {}, None),
            "empty quoted token": ("CORTADOS_PROVIDER_TOKEN=''\n", {}, None),
            "token with space": ("CORTADOS_PROVIDER_TOKEN='a b c d e f g h'\n", {}, None),
            "token twice": (good + good, {}, None),
            "unquoted shell syntax": ("CORTADOS_PROVIDER_TOKEN=a;b\n", {}, None),
            "unbalanced quote": ("CORTADOS_PROVIDER_TOKEN='abc\n", {}, None),
            "backslash": ("CORTADOS_PROVIDER_TOKEN=a\\b\n", {}, None),
            "dollar in single quotes": ("CORTADOS_PROVIDER_TOKEN='a$b'\n", {}, None),
            "group writable": (good, {"mode": 0o620}, None),
            "missing HOME": (good, {"unset": "HOME"}, None),
            "missing URL": (good, {"unset": "CORTADOS_PROVIDER_URL"}, None),
            "https URL": (good, {"url": "https://x:1"}, None),
            "start_task flag": (good, {}, ("--enable-start-task",)),
            "not UTF-8": (None, {}, None),
        }
        for name, (text, opt, flags) in cases.items():
            with self.subTest(name):
                self.calls.clear()
                env_backup = dict(self.environ)
                if text is None:
                    self.envfile.write_bytes(b"CORTADOS_PROVIDER_TOKEN=\xff\xfe\n")
                    os.chmod(self.envfile, 0o600)
                else:
                    self.write(text, opt.get("mode", 0o600))
                if "unset" in opt:
                    self.environ.pop(opt["unset"])
                if "url" in opt:
                    self.environ["CORTADOS_PROVIDER_URL"] = opt["url"]
                rc, err = self.run_main(flags) if flags else self.run_main()
                self.environ = env_backup
                self.assertEqual(rc, 78, err)
                self.assertEqual(self.calls, [])
                self.assertNotIn(PROVIDER_SECRET, err)

    def test_symlink_and_missing_file_exit_78(self):
        real = Path(self.tmp.name) / "real.env"
        real.write_text(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        os.chmod(real, 0o600)
        self.envfile.symlink_to(real)
        self.assertEqual(self.run_main()[0], 78)
        self.envfile.unlink()
        self.assertEqual(self.run_main()[0], 78)
        self.assertEqual(self.calls, [])
