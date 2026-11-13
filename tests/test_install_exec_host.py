"""scripts/install_exec_host.sh in a throwaway HOME with a fake launchctl and a fake claude (never the real ones)."""
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "scripts" / "install_exec_host.sh"
SECRET = "tok-must-never-be-printed-0001"
# `print` answers from a marker file, so "loaded" is whatever the last bootstrap left.
FAKE_LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  print) [ -f "$FAKE_LOG.loaded" ] && exit 0; exit 113 ;;
  bootstrap) touch "$FAKE_LOG.loaded" ;;
  bootout) rm -f "$FAKE_LOG.loaded" ;;
esac
exit 0
"""
FAKE_CLAUDE = """#!/bin/sh
echo "$*" >> "$FAKE_CLAUDE_LOG"
[ "$1 $2" = "auth status" ] || exit 9
printf '{"loggedIn": %s, "authMethod": "claude.ai"}\\n' "$FAKE_LOGGED_IN"
"""


@unittest.skipUnless(shutil.which("bash") and os.path.exists("/usr/bin/python3"), "needs bash and /usr/bin/python3")
class InstallExecHostTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.fake = self.home / "fake-launchctl"
        self.fake.write_text(FAKE_LAUNCHCTL)
        os.chmod(self.fake, 0o755)
        self.log = self.home / "launchctl.log"
        self.claude = self.home / ".local" / "bin" / "claude"
        self.claude_log = self.home / "claude.log"
        self.conf = self.home / ".config" / "cortados"
        self.plist = self.home / "Library" / "LaunchAgents" / "com.cortados.archive-cleanup.plist"

    def add_claude(self):
        self.claude.parent.mkdir(parents=True, exist_ok=True)
        self.claude.write_text(FAKE_CLAUDE)
        os.chmod(self.claude, 0o755)

    def add_provider_files(self):
        self.conf.mkdir(parents=True, exist_ok=True)
        (self.conf / "provider-token").write_text(SECRET + "\n")
        (self.conf / "provider-url").write_text("https://air.example.ts.net:8444\n")

    def add_hosts_file(self, hostnames):
        self.conf.mkdir(parents=True, exist_ok=True)
        (self.conf / "exec-hosts.json").write_text(json.dumps({"default": "air", "hosts": {
            "air": {"ssh": "air", "router": "/r", "hostnames": ["forests-macbook-air"], "root": "/w"},
            "pro": {"hostnames": hostnames, "root": str(self.home / "CortadOS")}}}))

    def install(self, *args, logged_in="true", cwd=None, home=None):
        env = {"HOME": home or str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "CORTADOS_LAUNCHCTL": str(self.fake),
               "FAKE_LOG": str(self.log), "FAKE_CLAUDE_LOG": str(self.claude_log), "FAKE_LOGGED_IN": logged_in,
               "CORTADOS_INSTALL_HOSTNAME": "Forests-MacBook-Pro"}
        p = subprocess.run(["/bin/bash", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60,
                           cwd=cwd)
        self.assertNotIn(SECRET, p.stdout + p.stderr)
        return p

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def entry(self, out):
        start = out.index("{\n")
        return json.loads(out[start:out.index("\n}\n", start) + 2])

    def test_dry_run_changes_nothing_and_prints_the_entry(self):
        self.add_claude()
        self.add_provider_files()
        p = self.install("pro", "--dry-run")
        self.assertEqual(p.returncode, 1, p.stderr)               # exec-hosts.json is still missing: a TODO
        self.assertFalse((self.home / "CortadOS").exists())
        self.assertFalse(self.plist.exists())
        self.assertFalse(any(c.startswith(("bootstrap", "bootout")) for c in self.calls()))
        self.assertIn("would create", p.stdout)
        self.assertIn("--cleanup-archive", p.stdout)
        entry = self.entry(p.stdout[p.stdout.index("this machine's entry"):])
        self.assertEqual(list(entry), ["pro"])
        pro = entry["pro"]
        self.assertNotIn("ssh", pro)
        self.assertEqual(pro["hostnames"], ["Forests-MacBook-Pro"])
        self.assertEqual(pro["root"], str(self.home / "CortadOS"))
        self.assertEqual(pro["router"], str(REPO))
        self.assertEqual(pro["claude"], str(self.claude))
        self.assertEqual(pro["provider_url"], "https://air.example.ts.net:8444")
        self.assertTrue(pro["gui"])
        self.assertIn("TODO: create", p.stdout)
        self.assertEqual(self.claude_log.read_text().split(), ["auth", "status", "--json"])   # read-only check only

    def test_install_creates_the_root_and_the_cleanup_job_and_is_idempotent(self):
        self.add_claude()
        self.add_provider_files()
        self.add_hosts_file(["Forests-MacBook-Pro"])
        p = self.install("pro")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ready", p.stdout)
        for sub in ("active", "archive", "held"):
            self.assertTrue((self.home / "CortadOS" / sub).is_dir())
        with open(self.plist, "rb") as f:
            pl = plistlib.load(f)
        self.assertEqual(pl["Label"], "com.cortados.archive-cleanup")
        self.assertEqual(pl["ProgramArguments"],
                         ["/usr/bin/python3", "-B", str(REPO / "cortados_router" / "child_tasks.py"), "--cleanup-archive"])
        self.assertEqual(pl["StartCalendarInterval"], {"Hour": 4, "Minute": 30})
        self.assertEqual(pl["EnvironmentVariables"]["HOME"], str(self.home))
        self.assertFalse(pl["RunAtLoad"])
        uid = os.getuid()
        self.assertIn(f"bootstrap gui/{uid} {self.plist}", self.calls())
        # a second run finds it installed and loaded: nothing is rewritten or reloaded
        before = len([c for c in self.calls() if c.startswith("bootstrap")])
        p = self.install("pro")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("unchanged", p.stdout)
        self.assertEqual(len([c for c in self.calls() if c.startswith("bootstrap")]), before)

    def test_missing_pieces_are_listed_as_todos(self):
        p = self.install("studio", "--ssh", "studio", "--no-gui", "--dry-run")
        self.assertEqual(p.returncode, 1)
        self.assertIn("TODO: install the claude CLI", p.stdout)
        self.assertIn("TODO: copy the provider token", p.stdout)
        self.assertIn("TODO: write the provider URL", p.stdout)
        entry = self.entry(p.stdout[p.stdout.index("this machine's entry"):])["studio"]
        self.assertEqual(entry["ssh"], "studio")
        self.assertFalse(entry["gui"])
        self.add_claude()
        p = self.install("pro", "--dry-run", logged_in="false")
        self.assertIn("TODO: log the claude CLI in", p.stdout)

    def test_a_hosts_file_that_does_not_name_this_machine_is_reported(self):
        self.add_claude()
        self.add_provider_files()
        self.add_hosts_file(["some-other-laptop"])
        p = self.install("pro", "--dry-run")
        self.assertEqual(p.returncode, 1)
        self.assertIn('no entry names Forests-MacBook-Pro in hostnames, so host "here" is refused', p.stdout)

    def test_a_relative_claude_path_is_printed_absolute_and_the_entry_loads(self):
        # Review finding: `--claude ./bin/claude` passed the checks but printed "claude": "./bin/claude", which
        # exec_hosts.Host refuses (absolute paths only), so the copied entry broke config loading.
        from cortados_router import exec_hosts as eh
        self.add_claude()
        self.add_provider_files()
        p = self.install("pro", "--claude", "./.local/bin/claude", "--dry-run", cwd=str(self.home))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)    # only exec-hosts.json is missing
        self.assertIn("logged in", p.stdout)                       # the resolved path was the one probed
        entry = self.entry(p.stdout[p.stdout.index("this machine's entry"):])["pro"]
        for key in ("claude", "root", "router"):
            self.assertTrue(os.path.isabs(entry[key]), (key, entry[key]))
        self.assertNotIn("./", entry["claude"])
        self.assertEqual(Path(entry["claude"]).resolve(), self.claude.resolve())
        host = eh.Host("pro", entry)                               # the printed entry is accepted as it is
        self.assertEqual(host.claude, entry["claude"])

    def test_bad_arguments_are_refused(self):
        for args in (["Pro"], ["here"], ["pro", "--ssh", "-oProxyCommand=x"], ["pro", "--nope"]):
            p = self.install(*args)
            self.assertEqual(p.returncode, 2, args)
        self.assertFalse((self.home / "CortadOS").exists())
        p = self.install("pro", "--dry-run", home="relative-home", cwd=str(self.home))
        self.assertEqual(p.returncode, 2)
        self.assertIn("HOME must be an absolute path", p.stderr)


if __name__ == "__main__":
    unittest.main()
