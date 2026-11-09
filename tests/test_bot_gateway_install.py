"""integrations/bot-gateway/install.sh in a throwaway HOME with fake tailscale / launchctl / curl on PATH.
Nothing real is touched: no LaunchAgent is loaded, no network call is made, no real provider.env is read."""
import json
import os
import plistlib
import stat
import subprocess
import sys
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

    def run_sh(self, *args, extra_env=None):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", **(extra_env or {})}
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
        self.assertEqual(doc["EnvironmentVariables"]["CORTADOS_PROVIDER_URL"], "http://127.0.0.1:8432")
        self.assertNotIn("CORTADOS_PROVIDER_TOKEN", doc["EnvironmentVariables"])
        self.assertFalse((self.home / "cortados-bot-workspace").exists())   # #86: no confined workspace root
        self.assertEqual(stat.S_IMODE(self.token_path().stat().st_mode), 0o600)
        return doc

    def gateway_args(self, cmdline):
        self.assertIn("--host 127.0.0.1 --port 8434", cmdline)
        self.assertIn(f"--token-file {self.token_path()}", cmdline)
        self.assertNotIn("--workspace-root", cmdline)

    def test_design_a_runs_the_data_launcher(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\nOTHER=1\n")
        r, tok = self.install()
        doc = self.check_common(r, tok)
        args = doc["ProgramArguments"]
        self.assertEqual(args[:3], ["/usr/bin/python3", "-I", str(REPO / "integrations" / "bot-gateway" / "launch.py")])
        self.assertEqual(args[3:8], ["--provider-env", str(self.home / ".cortados" / "provider.env"),
                                     "--python", "/usr/bin/python3", "--"])
        self.assertNotIn("/bin/zsh", args)                    # provider.env is never executed as shell code
        self.gateway_args(" ".join(args[8:]))

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
        self.assertEqual(got["CORTADOS_PROVIDER_URL"], "http://127.0.0.1:8432")
        self.assertEqual(got["HOME"], str(self.home))
        self.assertEqual(got["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(argv[1:5], ["-I", "-S", "-B", "-c"])
        self.assertIn("runpy.run_module('cortados_router.bot_gateway'", argv[5])
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

    def test_selector_accepts_leading_blanks_like_the_launcher(self):
        """install.sh picks design (a) for every line form launch.py accepts, so an indented assignment never
        silently selects the fallback file."""
        self.assertEqual(self.run_sh("token").returncode, 0)
        ptf = self.token_path().parent / "bot-gateway-provider-token"
        ptf.write_text(PROVIDER_SECRET)
        os.chmod(ptf, 0o600)                     # present, so a selector miss would quietly pick design (b)
        for line in (f"  export CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}", f"\tCORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}",
                     f" \texport\tCORTADOS_PROVIDER_TOKEN='{PROVIDER_SECRET}'"):
            with self.subTest(repr(line[:12])):
                self.provider_env(line + "\n")
                r = self.run_sh("plist")
                self.assertEqual(r.returncode, 0, r.stderr)
                args = plistlib.loads(self.plist().read_bytes())["ProgramArguments"]
                self.assertEqual(args[3], "--provider-env")
                self.assertEqual(self.launcher_parses(args[4]), PROVIDER_SECRET)

    def launcher_parses(self, path):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_gateway_launch_sel", SCRIPT.parent / "launch.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.parse_env(Path(path).read_text())["CORTADOS_PROVIDER_TOKEN"]

    def design_b(self):
        self.provider_env("CORTADOS_PROVIDER_CLAUDE_BIN=claude\n")       # no token variable
        r = self.run_sh("token")
        self.assertEqual(r.returncode, 0, r.stderr)
        ptf = self.token_path().parent / "bot-gateway-provider-token"
        ptf.write_text(PROVIDER_SECRET + "\n")
        os.chmod(ptf, 0o600)
        r = self.run_sh("plist")
        return ptf, self.check_common(r, self.token_path().read_text())

    def test_design_b_provider_token_file(self):
        ptf, doc = self.design_b()
        args = doc["ProgramArguments"]
        self.assertEqual(args[:3], ["/usr/bin/python3", "-I", str(REPO / "integrations" / "bot-gateway" / "launch.py")])
        self.assertEqual(args[3:8], ["--provider-token-file", str(ptf), "--python", "/usr/bin/python3", "--"])
        self.assertNotIn("CORTADOS_PROVIDER_TOKEN_FILE", doc["EnvironmentVariables"])
        self.gateway_args(" ".join(args[8:]))

    def test_design_b_child_environment_not_contaminated(self):
        """Fallback mode goes through the same launcher: an inherited PYTHONPATH (with a sitecustomize.py), proxies
        and an ambient CORTADOS_PROVIDER_TOKEN reach neither the launcher nor the gateway; the file's token wins."""
        ptf, doc = self.design_b()
        site = Path(self.tmp.name) / "site"
        site.mkdir()
        marker = Path(self.tmp.name) / "sitecustomize-ran"
        (site / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
        p, argv, got = self.run_plist_launcher(doc, {"PYTHONPATH": str(site), "HTTPS_PROXY": "http://proxy.invalid:1",
                                                     "CORTADOS_PROVIDER_TOKEN": "ambient-token-0123456789",
                                                     "CORTADOS_PROVIDER_TOKEN_FILE": "/nowhere"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(marker.exists())
        for k in ("SDKROOT", "CPATH", "LIBRARY_PATH", "MANPATH", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"):
            got.pop(k, None)                      # added by Apple's /usr/bin/python3 shim and Python itself
        self.assertEqual(set(got), {"HOME", "PATH", "CORTADOS_HOME", "CORTADOS_PROVIDER_URL",
                                    "CORTADOS_PROVIDER_TOKEN"})
        self.assertEqual(got["CORTADOS_PROVIDER_TOKEN"], PROVIDER_SECRET)
        self.assertFalse(any(PROVIDER_SECRET in a for a in argv))
        self.assertNotIn(PROVIDER_SECRET, p.stdout + p.stderr)
        dump = (Path(self.tmp.name) / "rec" / "env").read_text()
        for leaked in ("proxy.invalid", "ambient-token", str(site), "/nowhere"):
            self.assertNotIn(leaked, dump)

    def test_no_provider_token_refused(self):
        self.provider_env("CORTADOS_PROVIDER_CLAUDE_BIN=claude\n")
        self.run_sh("token")
        r = self.run_sh("plist")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.plist().exists())

    def test_bad_provider_url_refused(self):
        cases = ["ftp://127.0.0.1:8432", "http://user:pw@127.0.0.1:8432", "http://tok@127.0.0.1:8432",
                 "http://127.0.0.1:8432/?token=x", "http://127.0.0.1:8432/#x", "http://127.0.0.1:8432/?",
                 "http://127.0.0.1:8432?", "http://127.0.0.1:8432/#", "http://127.0.0.1:8432#",
                 "HTTP://127.0.0.1:8432", "Https://provider.example.test", "http://", "127.0.0.1:8432",
                 "http://127.0.0.1:99999", "http://127.0.0.1:8432/ x", "http://127.0.0.1:8432\nX=1", ""]
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.assertEqual(self.run_sh("token").returncode, 0)
        for url in cases:
            with self.subTest(url):
                r = self.run_sh("plist", extra_env={"CORTADOS_BOT_PROVIDER_URL": url})
                if url == "":
                    # Empty means unset (${VAR:-default}): the loopback default is used.
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.plist().unlink()
                    continue
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertFalse(self.plist().exists())

    def test_provider_url_override_and_no_tailscale_needed(self):
        # The plist no longer asks Tailscale for anything: a failing tailscale CLI does not matter.
        self._fake("tailscale", "exit 1\n")
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.assertEqual(self.run_sh("token").returncode, 0)
        r = self.run_sh("plist")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("provider http://127.0.0.1:8432,", r.stdout)
        for url in ("https://provider.example.test", "http://127.0.0.1:9000/"):
            with self.subTest(url):
                r = self.run_sh("plist", extra_env={"CORTADOS_BOT_PROVIDER_URL": url})
                self.assertEqual(r.returncode, 0, r.stderr)
                doc = plistlib.loads(self.plist().read_bytes())
                self.assertIn("--host 127.0.0.1 ", " ".join(doc["ProgramArguments"]))
                self.assertEqual(doc["EnvironmentVariables"]["CORTADOS_PROVIDER_URL"], url)
                # The written value must survive the real launcher's resolution (review r1: an accepted
                # "HTTP://" would otherwise exit 78 in a crash loop).
                from cortados_router.subagent_mcp import resolve_provider_url
                self.assertEqual(resolve_provider_url(dict(doc["EnvironmentVariables"])), url.rstrip("/"))

    def test_allowed_host_is_read_from_environment_into_plist(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.assertEqual(self.run_sh("token").returncode, 0)
        r = self.run_sh("plist", extra_env={"CORTADOS_BOT_ALLOWED_HOST": "gateway.example.test"})
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = plistlib.loads(self.plist().read_bytes())
        self.assertEqual(doc["EnvironmentVariables"]["CORTADOS_BOT_ALLOWED_HOST"], "gateway.example.test")

    def test_funnel_443_preserves_private_serve_on_other_port_and_verifies_owner(self):
        calls = Path(self.tmp.name) / "tailscale.calls"
        state = Path(self.tmp.name) / "funnel.json"
        # Private tailnet-only Serve lives on its own port (8444 here) and is never touched or published.
        unrelated = ('{"TCP":{"8444":{"HTTPS":true}},"Web":{"public.example:8444":{"Handlers":'
                     '{"/other":{"Proxy":"http://127.0.0.1:9000"}}}}}')
        ours = ('{"TCP":{"8444":{"HTTPS":true},"443":{"HTTPS":true}},"Web":'
                '{"public.example:8444":{"Handlers":{"/other":{"Proxy":"http://127.0.0.1:9000"}}},'
                '"public.example:443":{"Handlers":{"/mcp":{"Proxy":"http://127.0.0.1:8434/mcp"}}}},'
                '"AllowFunnel":{"public.example:443":true}}')
        state.write_text(unrelated)
        self._fake("tailscale", f'''echo "$*" >> {calls}
if [ "$1 $2 $3" = "funnel status --json" ]; then /bin/cat {state}; exit 0; fi
case "$*" in
  "funnel --bg --https=443 --set-path /mcp http://127.0.0.1:8434/mcp") printf '%s' '{ours}' > {state};;
  "funnel --https=443 --set-path /mcp off") printf '%s' '{unrelated}' > {state};;
esac
''')
        self.assertEqual(self.run_sh("funnel-on").returncode, 0)
        owner = self.home / ".cortados" / "bot-gateway-funnel-owner"
        self.assertTrue(owner.exists())
        self.assertEqual(self.run_sh("funnel-off").returncode, 0)
        self.assertFalse(owner.exists())
        self.assertEqual(state.read_text(), unrelated)
        commands = [line for line in calls.read_text().splitlines() if "status --json" not in line]
        self.assertEqual(commands, [
            "funnel --bg --https=443 --set-path /mcp http://127.0.0.1:8434/mcp",
            "funnel --https=443 --set-path /mcp off",
        ])

    def test_funnel_refuses_reserved_port_conflict_and_unowned_removal(self):
        state = Path(self.tmp.name) / "funnel.json"
        calls = Path(self.tmp.name) / "tailscale.calls"
        self._fake("tailscale", f'''if [ "$1 $2 $3" = "funnel status --json" ]; then /bin/cat {state}; exit 0; fi
echo "$*" >> {calls}
''')
        # Any non-owned handler or path on 443 (private Serve or public, alone or beside our /mcp) is a conflict:
        # publishing or removing there could expose or break unrelated paths.
        other = {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"}}}
        mixed = {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"},
                              "/mcp": {"Proxy": "http://127.0.0.1:8434/mcp"}}}
        for web, allow in ((other, {"public.example:443": True}), (other, {}), (mixed, {"public.example:443": True})):
            with self.subTest(web=web, allow=allow):
                state.write_text(json.dumps({"TCP": {"443": {"HTTPS": True}},
                                             "Web": {"public.example:443": web}, "AllowFunnel": allow}))
                self.assertNotEqual(self.run_sh("funnel-on").returncode, 0)
                self.assertNotEqual(self.run_sh("funnel-off").returncode, 0)
                self.assertFalse(calls.exists())

    def test_plist_without_token_refused(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        r = self.run_sh("plist")
        self.assertNotEqual(r.returncode, 0)


class FunnelReviewTest(InstallerHarness):
    def setUp(self):
        super().setUp()
        self.state = Path(self.tmp.name) / "funnel.json"
        self.ts_calls = Path(self.tmp.name) / "tailscale.calls"
        self.owner = self.home / ".cortados" / "bot-gateway-funnel-owner"
        self.expected = "443 /mcp http://127.0.0.1:8434/mcp\n"
        self.ours = {"TCP": {"443": {"HTTPS": True}},
                     "Web": {"public.example:443": {"Handlers": {
                         "/mcp": {"Proxy": "http://127.0.0.1:8434/mcp"}}}},
                     "AllowFunnel": {"public.example:443": True}}
        self.state.write_text("{}")
        self._fake("tailscale", f"""if [ "$1 $2 $3" = "funnel status --json" ]; then
  /bin/cat {self.state}; exit 0
fi
echo "$*" >> {self.ts_calls}
case "$*" in
  "funnel --bg --https=443 --set-path /mcp http://127.0.0.1:8434/mcp")
    [ "$(/bin/cat {self.owner})" = "pending {self.expected.strip()}" ] || exit 9
    printf '%s' '{json.dumps(self.ours)}' > {self.state};;
  "funnel --https=443 --set-path /mcp off") printf '{{}}' > {self.state};;
esac
""")

    def marker(self, pending=False):
        self.owner.parent.mkdir(exist_ok=True)
        self.owner.write_text(("pending " if pending else "") + self.expected)
        self.owner.chmod(0o600)

    def fail_record(self, action):
        # Fail only the record operation, not status parsing; never invoke a real tailscale.
        self._fake("python3", f'if [ "$4" = "{action}" ]; then exit 1; fi\n'
                              'exec /usr/bin/python3 "$@"\n')

    def test_exact_config_rejects_missing_conflicting_and_extended_tcp(self):
        for listener in (None, {"TCPForward": "127.0.0.1:9000"},
                         {"HTTPS": True, "TCPForward": "127.0.0.1:9000"},
                         {"HTTPS": 1}, {"HTTPS": False}):
            with self.subTest(listener=listener):
                cfg = dict(self.ours, TCP={} if listener is None else {"443": listener})
                self.state.write_text(json.dumps(cfg))
                self.marker()
                self.assertNotEqual(self.run_sh("funnel-off").returncode, 0)
                self.assertFalse(self.ts_calls.exists())
                self.assertTrue(self.owner.exists())

    def test_exact_config_rejects_unknown_web_and_top_level_fields(self):
        for cfg in (dict(self.ours, Unknown={}),
                    dict(self.ours, Web={"public.example:443": dict(
                        self.ours["Web"]["public.example:443"], Unknown=True)})):
            with self.subTest(cfg=cfg):
                self.state.write_text(json.dumps(cfg))
                self.marker()
                self.assertNotEqual(self.run_sh("funnel-off").returncode, 0)
                self.assertFalse(self.ts_calls.exists())

    def test_intent_is_present_before_activation_and_finalized(self):
        r = self.run_sh("funnel-on")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.owner.read_text(), self.expected)
        self.assertEqual(stat.S_IMODE(self.owner.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.state.read_text()), self.ours)

    def test_intent_write_failure_never_activates(self):
        self.fail_record("create")
        r = self.run_sh("funnel-on")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.ts_calls.exists())
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertFalse(self.owner.exists())

    def test_intent_fsync_failure_never_activates_and_can_be_cleaned(self):
        # Execute the actual record helper, but fail its durability barrier after writing.
        self._fake("python3", """if [ "$4" = create ]; then
  exec /usr/bin/python3 -I -c '
import os, sys
code = sys.stdin.read()
sys.argv = ["-", *sys.argv[1:]]
def fail(fd):
    raise OSError("injected fsync failure")
os.fsync = fail
exec(compile(code, "<record>", "exec"))
' "$3" "$4" "$5"
fi
exec /usr/bin/python3 "$@"
""")
        r = self.run_sh("funnel-on")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("injected fsync failure", r.stderr)
        self.assertFalse(self.ts_calls.exists())
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertEqual(self.owner.read_text(), "pending " + self.expected)
        r = self.run_sh("funnel-off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.owner.exists())
        self.assertFalse(self.ts_calls.exists())

    def test_lock_prevents_pending_cleanup_during_activation(self):
        self.marker(pending=True)
        Path(str(self.owner) + ".lock").mkdir()
        for command in ("funnel-on", "funnel-off"):
            with self.subTest(command=command):
                r = self.run_sh(command)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("operation locked", r.stderr)
                self.assertTrue(self.owner.exists())
                self.assertFalse(self.ts_calls.exists())

    def test_exclusive_intent_refuses_existing_record(self):
        self.marker(pending=True)
        r = self.run_sh("funnel-on")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.ts_calls.exists())
        self.assertEqual(self.owner.read_text(), "pending " + self.expected)

    def test_finalize_failure_retains_recoverable_durable_intent(self):
        self.fail_record("finalize")
        r = self.run_sh("funnel-on")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(self.state.read_text()), self.ours)
        self.assertEqual(self.owner.read_text(), "pending " + self.expected)
        r = self.run_sh("funnel-off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.owner.exists())
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_empty_pending_record_cleanup_does_not_mutate_tailscale(self):
        self.marker(pending=True)
        r = self.run_sh("funnel-off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.owner.exists())
        self.assertFalse(self.ts_calls.exists())

    def test_conflicting_pending_record_is_retained_without_mutation(self):
        self.marker(pending=True)
        self.state.write_text(json.dumps(dict(self.ours, TCP={})))
        r = self.run_sh("funnel-off")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.owner.exists())
        self.assertFalse(self.ts_calls.exists())


    def test_owned_443_record_with_conflicting_listener_refused_without_mutation(self):
        # Valid 443 ownership, so funnel-off reaches configuration validation and must refuse there.
        other = {"/other": {"Proxy": "http://127.0.0.1:9000"}}
        mine = {"/mcp": {"Proxy": "http://127.0.0.1:8434/mcp"}}
        for handlers, allow in ((other, True), (other, None), (dict(other, **mine), True)):
            for pending in (False, True):
                with self.subTest(handlers=handlers, allow=allow, pending=pending):
                    cfg = {"TCP": {"443": {"HTTPS": True}},
                           "Web": {"public.example:443": {"Handlers": handlers}}}
                    if allow:
                        cfg["AllowFunnel"] = {"public.example:443": True}
                    self.state.write_text(json.dumps(cfg))
                    self.marker(pending=pending)
                    r = self.run_sh("funnel-off")
                    self.assertNotEqual(r.returncode, 0)
                    self.assertIn("not exactly ours", r.stderr)
                    self.assertFalse(self.ts_calls.exists())
                    self.assertTrue(self.owner.exists())

    def legacy(self, pending=False):
        legacy = self.expected.replace("443", "8443", 1)
        self.owner.parent.mkdir(exist_ok=True)
        self.owner.write_text(("pending " if pending else "") + legacy)
        self.owner.chmod(0o600)
        self.legacy_ours = {"TCP": {"8443": {"HTTPS": True}},
                            "Web": {"public.example:8443": {"Handlers": {
                                "/mcp": {"Proxy": "http://127.0.0.1:8434/mcp"}}}},
                            "AllowFunnel": {"public.example:8443": True}}
        self._fake("tailscale", f"""if [ "$1 $2 $3" = "funnel status --json" ]; then
  /bin/cat {self.state}; exit 0
fi
echo "$*" >> {self.ts_calls}
case "$*" in
  "funnel --https=8443 --set-path /mcp off") printf '{{}}' > {self.state};;
esac
""")
        return legacy

    def test_legacy_8443_record_blocks_funnel_on_with_migration_hint(self):
        legacy = self.legacy()
        self.state.write_text(json.dumps(self.legacy_ours))
        r = self.run_sh("funnel-on")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("legacy Funnel ownership record", r.stderr)
        self.assertIn("funnel-off", r.stderr)
        self.assertFalse(self.ts_calls.exists())
        self.assertEqual(self.owner.read_text(), legacy)

    def test_legacy_8443_record_funnel_off_removes_exactly_owned_handler(self):
        self.legacy()
        self.state.write_text(json.dumps(self.legacy_ours))
        r = self.run_sh("funnel-off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ts_calls.read_text().splitlines(), ["funnel --https=8443 --set-path /mcp off"])
        self.assertFalse(self.owner.exists())
        self.assertEqual(json.loads(self.state.read_text()), {})
        # After migration, funnel-on proceeds on 443.
        self.fake_tailscale_443()
        r = self.run_sh("funnel-on")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.owner.read_text(), self.expected)

    def test_legacy_8443_pending_record_with_empty_listener_cleans_without_mutation(self):
        self.legacy(pending=True)
        r = self.run_sh("funnel-off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.owner.exists())
        self.assertFalse(self.ts_calls.exists())

    def test_legacy_8443_record_with_conflicting_listener_refused_without_mutation(self):
        mixed = {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"},
                              "/mcp": {"Proxy": "http://127.0.0.1:8434/mcp"}}}
        for pending in (False, True):
            with self.subTest(pending=pending):
                legacy = self.legacy(pending=pending)
                self.state.write_text(json.dumps(dict(self.legacy_ours, Web={"public.example:8443": mixed})))
                r = self.run_sh("funnel-off")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("not exactly ours", r.stderr)
                self.assertFalse(self.ts_calls.exists())
                self.assertEqual(self.owner.read_text(), ("pending " if pending else "") + legacy)

    def fake_tailscale_443(self):
        if self.ts_calls.exists():
            self.ts_calls.unlink()
        self._fake("tailscale", f"""if [ "$1 $2 $3" = "funnel status --json" ]; then
  /bin/cat {self.state}; exit 0
fi
echo "$*" >> {self.ts_calls}
case "$*" in
  "funnel --bg --https=443 --set-path /mcp http://127.0.0.1:8434/mcp")
    printf '%s' '{json.dumps(self.ours)}' > {self.state};;
esac
""")

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

    def test_status_uses_allowed_host_from_installed_plist(self):
        self.provider_env(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        self.run_sh("token")
        self.assertEqual(self.run_sh("plist", extra_env={"CORTADOS_BOT_ALLOWED_HOST": "gateway.example.test"}).returncode, 0)
        curl_calls = Path(self.tmp.name) / "curl.calls"
        self._fake("curl", f'echo "$*" >> {curl_calls}\nprintf 401\n')
        s = self.run_sh("status")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertIn("-H Host: gateway.example.test", curl_calls.read_text())

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


class LauncherBase(unittest.TestCase):
    """launch.py loaded as a module, with a fake execve recording what it would exec."""

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


class LauncherTest(LauncherBase):
    """launch.py directly: what it would exec, and exit 78 for every config failure."""

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
        self.assertEqual(argv, ["/usr/bin/python3", "-I", "-S", "-B", "-c", self.launch.bootstrap(),
                                "--host", FAKE_IP, "--port", "8434"])
        self.assertIn(repr(str(REPO)), self.launch.bootstrap())
        self.assertFalse(any(PROVIDER_SECRET in a for a in argv))
        self.assertEqual(env, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CORTADOS_PROVIDER_TOKEN": PROVIDER_SECRET,
                               "HOME": self.tmp.name, "CORTADOS_HOME": self.tmp.name + "/.cortados",
                               "CORTADOS_PROVIDER_URL": f"http://{FAKE_IP}:8432"})
        self.assertNotIn(PROVIDER_SECRET, err)

    def test_https_file_fallback(self):
        self.write(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        endpoint = Path(self.tmp.name) / ".config/cortados/provider-url"
        endpoint.parent.mkdir(parents=True)
        endpoint.write_text(" https://provider.example:8443/\n")
        self.environ.pop("CORTADOS_PROVIDER_URL")
        rc, err = self.run_main()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls[0][2]["CORTADOS_PROVIDER_URL"], "https://provider.example:8443")

    def test_invalid_ambient_route_timeout_does_not_break_launcher_import(self):
        self.write(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        launch = str(SCRIPT.parent / "launch.py")
        argv = ["--provider-env", str(self.envfile), "--python", "/usr/bin/python3", "--",
                "--host", FAKE_IP, "--port", "8434"]
        code = ("import importlib.util; "
                "s=importlib.util.spec_from_file_location('isolated_launch', %r); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "raise SystemExit(m.main(%r, environ=%r, execve=lambda *a: None))"
                % (launch, argv, self.environ))
        env = dict(os.environ, HOME=self.tmp.name, CORTADOS_HOME=self.tmp.name + "/.cortados",
                   CORTADOS_SCARCITY_REFRESH="off", CORTADOS_ROUTE_TIMEOUT_S="invalid-ambient-setting")
        run = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

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
            "invalid URL": (good, {"url": "ftp://invalid.example"}, None),
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


class LauncherRound3Test(LauncherBase):
    """Review r3: no names in errors, exact flag allowlist, no Unicode normalization, token file source."""

    def test_errors_name_no_assignment_or_token(self):
        cases = [f"{PROVIDER_SECRET}=1\n{PROVIDER_SECRET}=2\n",                    # token used as a name, twice
                 f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\nCORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n",
                 f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n{PROVIDER_SECRET} junk\n",
                 f"CORTADOS_PROVIDER_TOKEN='{PROVIDER_SECRET}\n",
                 f"SOMENAME=a;{PROVIDER_SECRET}\n"]
        for text in cases:
            with self.subTest(text[:20]):
                self.write(text)
                rc, err = self.run_main()
                self.assertEqual(rc, 78, err)
                self.assertNotIn(PROVIDER_SECRET, err)
                self.assertNotIn("SOMENAME", err)
        name_token = "TokenShapedLikeAName_0123456789abcdef"     # a token that is also a valid assignment name
        self.write(f"{name_token}=1\n{name_token}=2\n")
        rc, err = self.run_main()
        self.assertEqual(rc, 78)
        self.assertIn("line 2: duplicate assignment", err)
        self.assertNotIn(name_token, err)

    def test_flag_allowlist_refuses_abbreviations_and_unknowns(self):
        self.write(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        bad = [("--enable-start-task",), ("--enable-start-t",), ("--enable",), ("--en",), ("--children-run-as-m",),
               ("--hos", FAKE_IP), ("--host=" + FAKE_IP,), ("--host",), ("--host", "--port"),
               ("--max-concurrent", "9"), ("-h",), ("positional",)]
        for flags in bad:
            with self.subTest(flags):
                self.calls.clear()
                rc, err = self.run_main(flags)
                self.assertEqual(rc, 78, err)
                self.assertEqual(self.calls, [])
        rc, err = self.run_main(("--host", FAKE_IP, "--port", "8434", "--token-file", "/t", "--workspace-root", "/w",
                                 "--children-run-as-me"))
        self.assertEqual(rc, 0, err)

    def test_gateway_parser_takes_no_abbreviations(self):
        import contextlib, io
        from cortados_router import bot_gateway
        for flag in ("--enable-start-t", "--enable", "--children-run"):
            with self.subTest(flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    bot_gateway.main([flag])
                self.assertEqual(cm.exception.code, 2)

    def test_unicode_and_control_characters_refused(self):
        cases = {
            "U+001C splits nothing": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\x1cOTHER=1\n",
            "trailing NBSP": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\u00a0\n",
            "NBSP in quotes": f"CORTADOS_PROVIDER_TOKEN='{PROVIDER_SECRET}\u00a0'\n",
            "NUL in other value": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\nOTHER=a\x00b\n",
            "lone CR": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\rOTHER=1\n",
            "line separator": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\u2028OTHER=1\n",
            "vertical tab": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\x0b\n",
            "DEL": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\x7f\n",
            "C1": f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\x85\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.calls.clear()
                self.write(text)
                rc, err = self.run_main()
                self.assertEqual(rc, 78, err)
                self.assertEqual(self.calls, [])
                self.assertNotIn(PROVIDER_SECRET, err)

    def test_crlf_and_space_tab_trim_accepted(self):
        self.write(f"# c\r\n \tCORTADOS_PROVIDER_TOKEN='{PROVIDER_SECRET}'\t \r\nOTHER=1\r\n")
        rc, err = self.run_main()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls[0][2]["CORTADOS_PROVIDER_TOKEN"], PROVIDER_SECRET)

    def run_file(self, content, mode=0o600):
        f = Path(self.tmp.name) / "ptoken"
        f.write_bytes(content)
        os.chmod(f, mode)
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.launch.main(["--provider-token-file", str(f), "--", "--host", FAKE_IP],
                                  environ=dict(self.environ, CORTADOS_PROVIDER_TOKEN="ambient-token-0123456789"),
                                  execve=lambda *a: self.calls.append(a))
        return rc, err.getvalue()

    def test_token_file_source(self):
        for content in (PROVIDER_SECRET.encode(), (PROVIDER_SECRET + "\n").encode(),
                        (PROVIDER_SECRET + "\r\n").encode()):
            with self.subTest(content[-2:]):
                self.calls.clear()
                rc, err = self.run_file(content)
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.calls[0][2]["CORTADOS_PROVIDER_TOKEN"], PROVIDER_SECRET)   # not the ambient one
                self.assertFalse(any(PROVIDER_SECRET in a for a in self.calls[0][1]))
        bad = {"two newlines": ((PROVIDER_SECRET + "\n\n").encode(), 0o600),
               "leading space": ((" " + PROVIDER_SECRET).encode(), 0o600),
               "NBSP": ((PROVIDER_SECRET + "\u00a0").encode(), 0o600),
               "NUL": ((PROVIDER_SECRET + "\x00").encode(), 0o600),
               "empty": (b"", 0o600),
               "group readable": (PROVIDER_SECRET.encode(), 0o640),
               "not UTF-8": (b"\xff\xfe", 0o600)}
        for name, (content, mode) in bad.items():
            with self.subTest(name):
                self.calls.clear()
                rc, err = self.run_file(content, mode)
                self.assertEqual(rc, 78, err)
                self.assertEqual(self.calls, [])
                self.assertNotIn(PROVIDER_SECRET, err)

    def test_both_or_neither_source_refused(self):
        import contextlib, io
        for argv in (["--", "--host", FAKE_IP],
                     ["--provider-env", "/a", "--provider-token-file", "/b", "--", "--host", FAKE_IP]):
            with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.launch.main(argv, environ=self.environ, execve=lambda *a: self.calls.append(a))
        self.assertEqual(self.calls, [])


class ChildIsolationTest(unittest.TestCase):
    """Review r4: the gateway child itself runs isolated. The REAL /usr/bin/python3 runs the child, with marker
    sitecustomize/usercustomize files in the throwaway HOME's user site and in PYTHONPATH; none may run, and argv must
    still reach the gateway's argparse."""

    PY = "/usr/bin/python3"

    def setUp(self):
        if not os.path.exists(self.PY):
            self.skipTest("no /usr/bin/python3")
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.home.mkdir()
        self.marker = t / "marker"
        hook = f"open({str(self.marker)!r}, 'a').write(__name__ + chr(10))\n"
        user_site = subprocess.run([self.PY, "-c", "import site; print(site.getusersitepackages())"],
                                   env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, capture_output=True,
                                   text=True, timeout=60).stdout.strip()
        self.assertTrue(user_site.startswith(str(self.home)), user_site)
        self.pypath = t / "pypath"
        for d in (Path(user_site), self.pypath):
            d.mkdir(parents=True, exist_ok=True)
            (d / "sitecustomize.py").write_text(hook)
            (d / "usercustomize.py").write_text(hook)
        self.envfile = t / "provider.env"
        self.envfile.write_text(f"CORTADOS_PROVIDER_TOKEN={PROVIDER_SECRET}\n")
        os.chmod(self.envfile, 0o600)
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "CORTADOS_HOME": str(t / "state"),
                    "CORTADOS_PROVIDER_URL": f"http://{FAKE_IP}:8432", "PYTHONPATH": str(self.pypath)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_markers_would_run_without_isolation(self):
        """The setup is live: a plain python with this HOME and PYTHONPATH does import the hooks."""
        subprocess.run([self.PY, "-c", "pass"], env=self.env, timeout=60, check=True)
        self.assertTrue(self.marker.exists())

    def test_gateway_child_loads_no_site_hooks_and_gets_its_argv(self):
        # Loopback with a missing token file: the real gateway parses its flags, then refuses to start (exit 2)
        # without binding anything; its message proves argparse saw --token-file.
        missing = Path(self.tmp.name) / "no-such-token"
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        p = subprocess.run([self.PY, "-I", str(SCRIPT.parent / "launch.py"), "--provider-env", str(self.envfile),
                            "--python", self.PY, "--", "--host", "127.0.0.1", "--port", "1",
                            "--token-file", str(missing), "--workspace-root", str(ws)],
                           env=self.env, capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("bot-gateway: refusing to start: cannot open the token file", p.stderr)
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")
        self.assertNotIn(PROVIDER_SECRET, p.stdout + p.stderr)
