"""exec_hosts: the hosts file, local detection, and the ssh call shape — with a fake transport (ssh never runs)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortados_router import exec_hosts as eh  # noqa: E402

AIR = {"ssh": "air", "hostnames": ["Forests-MacBook-Air"], "root": "/Users/x/CortadOS", "router": "/Users/x/cortados-router",
       "python": "/usr/bin/python3", "claude": "/Users/x/.local/bin/claude", "path": "/usr/bin:/bin", "gui": True,
       "provider_url": "http://127.0.0.1:8432"}


class HostsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "exec-hosts.json")

    def write(self, cfg):
        with open(self.path, "w") as f:
            json.dump(cfg, f)
        return self.path

    def test_missing_file_names_the_path_and_the_example_and_never_falls_back(self):
        with self.assertRaises(eh.HostError) as cm:
            eh.load(self.path)
        msg = str(cm.exception)
        self.assertIn(self.path, msg)
        self.assertIn(str(eh.EXAMPLE_FILE), msg)
        self.assertIn("never silently on this machine", msg)

    def test_example_file_loads_and_names_the_air_as_default(self):
        hosts = eh.load(str(eh.EXAMPLE_FILE))
        self.assertEqual(hosts.default, "air")
        air = hosts.resolve()
        self.assertEqual(air.ssh, "air")
        self.assertTrue(air.gui)
        self.assertTrue(air.root and air.router and air.claude and air.path and air.provider_url)
        with mock.patch.object(eh, "short_hostname", return_value="forests-macbook-pro"):
            self.assertFalse(air.is_local())
        with mock.patch.object(eh, "short_hostname", return_value="forests-macbook-air"):
            self.assertTrue(air.is_local())

    def test_env_and_arg_choose_the_host(self):
        self.write({"default": "air", "hosts": {"air": AIR, "here": {"root": "/tmp/w"}}})
        hosts = eh.load(self.path)
        self.assertEqual(hosts.resolve().name, "air")
        self.assertEqual(hosts.resolve("here").name, "here")
        self.assertEqual(hosts.resolve(env={"CORTADOS_EXEC_HOST": "here"}).name, "here")
        self.assertTrue(hosts.resolve("here").is_local())          # no ssh: this machine
        with self.assertRaises(eh.HostError) as cm:
            hosts.resolve("studio")
        self.assertIn("studio", str(cm.exception))
        self.assertIn("air, here", str(cm.exception))

    def test_local_entry_prefers_a_hostname_match(self):
        self.write({"default": "air", "hosts": {"air": AIR, "here": {"root": "/tmp/w"}}})
        hosts = eh.load(self.path)
        with mock.patch.object(eh, "short_hostname", return_value="forests-macbook-air"):
            self.assertEqual(hosts.local_entry().name, "air")
        with mock.patch.object(eh, "short_hostname", return_value="elsewhere"):
            self.assertEqual(hosts.local_entry().name, "here")

    def test_invalid_entries_are_refused_with_the_field_named(self):
        bad = [({"default": "a", "hosts": {"a": {"ssh": "a"}}}, "router"),
               ({"default": "a", "hosts": {"a": {"root": "relative"}}}, "absolute path"),
               ({"default": "a", "hosts": {"a": {"root": "/w", "nope": 1}}}, "unknown field"),
               ({"default": "a", "hosts": {"a": {"root": "/w", "gui": "yes"}}}, "gui"),
               ({"default": "a", "hosts": {"a": {"root": "/w", "provider_url": "ftp://x"}}}, "provider_url"),
               ({"default": "a", "hosts": {"a": {"root": "/w", "hostnames": "air"}}}, "hostnames"),
               ({"default": "zz", "hosts": {"a": {"root": "/w"}}}, "default"),
               ({"default": "A", "hosts": {"A": {"root": "/w"}}}, "slug"),
               ({"default": "a", "hosts": {"a": {"ssh": "-oProxyCommand=x", "router": "/r"}}}, "options"),
               ({"hosts": {}}, "at least one host"),
               ([], "expected")]
        for cfg, needle in bad:
            self.write(cfg)
            with self.assertRaises(eh.HostError, msg=cfg) as cm:
                eh.load(self.path)
            self.assertIn(needle, str(cm.exception), cfg)

    def test_config_path_from_env(self):
        self.assertEqual(eh.config_path({"CORTADOS_EXEC_HOSTS_FILE": "/a/b.json"}), "/a/b.json")
        self.assertEqual(eh.config_path({"HOME": "/Users/u"}), "/Users/u/.config/cortados/exec-hosts.json")


class CallTests(unittest.TestCase):
    def setUp(self):
        self.host = eh.Host("air", AIR)
        self.calls = []
        self.reply = (0, json.dumps({"v": eh.PROTOCOL, "ok": True, "result": {"task_id": "t1"}}).encode(), b"")

    def transport(self, argv, stdin, timeout):
        self.calls.append((argv, json.loads(stdin), timeout))
        r = self.reply
        if isinstance(r, Exception):
            raise r
        return r

    def test_ssh_argv_and_request_shape(self):
        with mock.patch.object(eh, "short_hostname", return_value="pro"):
            out = eh.call(self.host, "status", {"task_id": "t1"}, transport=self.transport)
        self.assertEqual(out, {"task_id": "t1"})
        argv, req, timeout = self.calls[0]
        self.assertEqual(argv[:1], ["ssh"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn(f"ConnectTimeout={eh.SSH_CONNECT_TIMEOUT_S}", argv)
        self.assertEqual(argv[-2:], ["air", "/usr/bin/python3 -B /Users/x/cortados-router/cortados_router/child_tasks.py --remote"])
        self.assertEqual(argv[argv.index("--") + 1], "air")   # the destination can never be read as an option
        self.assertEqual(req, {"v": eh.PROTOCOL, "op": "status", "host": "air", "args": {"task_id": "t1"}})
        self.assertEqual(timeout, eh.SSH_RPC_TIMEOUT_S)

    def test_unreachable_timeout_and_garbage_are_host_unreachable(self):
        with mock.patch.object(eh, "short_hostname", return_value="pro"):
            self.reply = (255, b"", b"ssh: connect to host air port 22: No route to host")
            with self.assertRaises(eh.HostUnreachable) as cm:
                eh.call(self.host, "start", {}, transport=self.transport)
            self.assertIn("unreachable", str(cm.exception))
            self.assertIn("'air' (ssh air)", str(cm.exception))
            self.assertIn("No route to host", str(cm.exception))
            self.assertFalse(cm.exception.ambiguous)            # nothing was sent: a definite failure
            self.reply = subprocess.TimeoutExpired("ssh", 30)
            with self.assertRaises(eh.HostUnreachable) as cm:
                eh.call(self.host, "start", {}, transport=self.transport)
            self.assertIn("did not answer", str(cm.exception))
            self.assertTrue(cm.exception.ambiguous)             # sent, then no answer: the host may have acted
            self.reply = (1, b"Traceback...\n", b"python3: No such file")
            with self.assertRaises(eh.HostUnreachable) as cm:
                eh.call(self.host, "start", {}, transport=self.transport)
            self.assertIn("no reply", str(cm.exception))
            self.assertIn("No such file", str(cm.exception))
            self.assertTrue(cm.exception.ambiguous)             # the remote command ran
            self.reply = OSError("ssh missing")
            with self.assertRaises(eh.HostUnreachable) as cm:
                eh.call(self.host, "start", {}, transport=self.transport)
            self.assertFalse(cm.exception.ambiguous)

    def test_remote_error_protocol_mismatch_and_banner(self):
        with mock.patch.object(eh, "short_hostname", return_value="pro"):
            self.reply = (1, json.dumps({"v": eh.PROTOCOL, "ok": False, "error": "cwd '/x' does not exist on execution host 'air'"}).encode(), b"")
            with self.assertRaises(eh.HostError) as cm:
                eh.call(self.host, "start", {}, transport=self.transport)
            self.assertNotIsInstance(cm.exception, eh.HostUnreachable)
            self.assertIn("does not exist on execution host", str(cm.exception))
            self.reply = (0, json.dumps({"v": 99, "ok": True, "result": {}}).encode(), b"")
            with self.assertRaises(eh.HostError) as cm:
                eh.call(self.host, "start", {}, transport=self.transport)
            self.assertIn("protocol", str(cm.exception))
            self.assertIn("/Users/x/cortados-router", str(cm.exception))
            self.reply = (0, b"Welcome to the Air\n" + json.dumps({"v": eh.PROTOCOL, "ok": True, "result": [1]}).encode() + b"\n", b"")
            self.assertEqual(eh.call(self.host, "list", {}, transport=self.transport), [1])

    def test_calling_a_local_host_is_refused(self):
        with mock.patch.object(eh, "short_hostname", return_value="forests-macbook-air"):
            with self.assertRaises(eh.HostError):
                eh.call(self.host, "status", {}, transport=self.transport)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
