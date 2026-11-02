import io
import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from cortados_router import session

SECRET = "tok-SECRET-123"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tok = os.path.join(self.tmp.name, "token")
        with open(self.tok, "w") as f:
            f.write(SECRET + "\n")
        self.base = {"PATH": "/usr/bin", "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": "console-key"}
        # A fake HOME under the real temp path (no /var symlink), so the ancestor checks run on real directories.
        self.home = os.path.join(os.path.realpath(self.tmp.name), "home")
        os.mkdir(self.home, 0o700)
        self.sdir = os.path.join(self.home, ".config", "cortados", "sessions")
        self.managed = os.path.join(self.tmp.name, "managed")     # empty: no managed settings unless a test writes one
        # Belt and braces: even a call that misses --settings-dir must never write the real ~/.config directory.
        keep = session.SETTINGS_DIR
        session.SETTINGS_DIR = os.path.join(self.home, "default-sessions")
        self.addCleanup(setattr, session, "SETTINGS_DIR", keep)

    def prepare(self, argv, base=None):
        return session.prepare(["--settings-dir", self.sdir] + list(argv), self.base if base is None else base,
                               managed_dirs=(self.managed,), home=self.home)

    def run_main(self, *argv, base=None):
        out, err = io.StringIO(), io.StringIO()
        rc = session.main(["--settings-dir", self.sdir] + list(argv), base_env=self.base if base is None else base,
                          out=out, err=err, managed_dirs=(self.managed,), home=self.home)
        return rc, out.getvalue(), err.getvalue()

    def settings_files(self):
        return sorted(os.listdir(self.sdir)) if os.path.isdir(self.sdir) else []

    def test_model_mapping(self):
        self.assertEqual(session.resolve_model("fable"), "sub/claude-fable-5-1")
        self.assertEqual(session.resolve_model("opus"), "sub/claude-opus-5-5")
        self.assertEqual(session.resolve_model("sonnet"), "sub/claude-sonnet-5")
        self.assertEqual(session.resolve_model("haiku"), "sub/claude-haiku-4-5-20251001")
        self.assertEqual(session.resolve_model("claude-opus-5-5"), "sub/claude-opus-5-5")
        self.assertEqual(session.resolve_model("sub/claude-x"), "sub/claude-x")
        self.assertEqual(session.resolve_model("cortados"), "cortados")

    def test_env(self):
        _, env, cmd = self.prepare(["--provider-url", "http://router.example:8787", "--token-file", self.tok])
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://router.example:8787")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], SECRET)
        self.assertEqual(env["ANTHROPIC_API_KEY"], SECRET)
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "cortados")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"], "1")
        self.assertEqual(env["CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL"], "cortados")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "sub/claude-haiku-4-5-20251001")
        self.assertIn("x-cortados-reveal: 1", env["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertEqual(env["CORTADOS_WHOLE_SESSION"], "1")
        self.assertEqual(env["CLAUDE_CODE_ENTRYPOINT"], "cli")
        for k in session.BLANKED:
            self.assertEqual(env[k], "")
        self.assertEqual(cmd[:3], ["claude", "--model", "sub/claude-fable-5-1"])
        self.assertIn("--settings", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn("MCP", cmd[cmd.index("--append-system-prompt") + 1])

    def test_url_from_env_and_extra_args(self):
        base = dict(self.base, CORTADOS_PROVIDER_URL="http://r.example")
        _, env, cmd = self.prepare(
            ["--orchestrator", "opus", "--token-file", self.tok, "--no-delegate-directive", "--", "-c", "--verbose"], base
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://r.example")
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertEqual(cmd[2], "sub/claude-opus-5-5")
        self.assertEqual(cmd[-2:], ["-c", "--verbose"])

    def test_settings_file_holds_env_at_flag_precedence(self):
        _, env, cmd = self.prepare(["--provider-url", "http://router.example:8787", "--token-file", self.tok])
        path = cmd[cmd.index("--settings") + 1]
        self.assertEqual(os.path.dirname(path), self.sdir)
        self.assertTrue(os.path.basename(path).startswith("session-") and path.endswith(".json"))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["enabledPlugins"], {session.PLUGIN_ID: False})
        senv = data["env"]
        self.assertEqual(senv["ANTHROPIC_AUTH_TOKEN"], SECRET)
        self.assertEqual(senv["ANTHROPIC_API_KEY"], SECRET)
        self.assertEqual(senv["ANTHROPIC_BASE_URL"], "http://router.example:8787")
        self.assertEqual(senv["CLAUDE_CODE_SUBAGENT_MODEL"], "cortados")
        self.assertEqual(senv["ANTHROPIC_SMALL_FAST_MODEL"], session.SMALL_FAST_MODEL)
        for k in session.BLANKED:
            self.assertEqual(senv[k], "")
        # Every launcher variable is in both the settings file and the process env, with the same value.
        self.assertEqual(senv, {k: env[k] for k in senv})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.sdir).st_mode), 0o700)
        self.assertFalse(any(SECRET in a for a in cmd), "token must never be in argv")

    def launch(self, url, token):
        with open(self.tok, "w") as f:
            f.write(token + "\n")
        _, _, cmd = self.prepare(["--provider-url", url, "--token-file", self.tok])
        return cmd[cmd.index("--settings") + 1]

    def test_interleaved_launches_get_their_own_files(self):
        a = self.launch("http://a.example", "tok-A")          # launch A writes, but its claude has not read yet
        b = self.launch("http://b.example", "tok-B")          # launch B must not touch A's file
        self.assertNotEqual(a, b)
        for path, url, token in ((a, "http://a.example", "tok-A"), (b, "http://b.example", "tok-B")):
            with open(path, encoding="utf-8") as f:
                senv = json.load(f)["env"]
            self.assertEqual((senv["ANTHROPIC_BASE_URL"], senv["ANTHROPIC_API_KEY"], senv["ANTHROPIC_AUTH_TOKEN"]),
                             (url, token, token))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_prune_removes_only_old_matching_files(self):
        os.makedirs(self.sdir, mode=0o700)
        old = time.time() - session.PRUNE_AGE - 60
        names = {"session-old.json": old, "session-new.json": None, "other-old.json": old, "session-old.txt": old}
        for n, mtime in names.items():
            with open(os.path.join(self.sdir, n), "w") as f:
                f.write("{}")
            if mtime:
                os.utime(os.path.join(self.sdir, n), (mtime, mtime))
        os.mkdir(os.path.join(self.sdir, "session-dir.json"))      # not a regular file: kept
        os.utime(os.path.join(self.sdir, "session-dir.json"), (old, old))
        target = os.path.join(self.tmp.name, "outside.json")      # an old symlink is never followed or removed
        with open(target, "w") as f:
            f.write("{}")
        os.symlink(target, os.path.join(self.sdir, "session-link.json"))
        os.utime(os.path.join(self.sdir, "session-link.json"), (old, old), follow_symlinks=False)
        new = os.path.basename(self.launch("http://a.example", "tok-A"))
        self.assertEqual(self.settings_files(), sorted(
            [new, "session-new.json", "other-old.json", "session-old.txt", "session-dir.json", "session-link.json"]))
        self.assertTrue(os.path.exists(target))
        with mock.patch("os.getuid", return_value=os.getuid() + 1):   # another uid's old file is never removed
            os.utime(os.path.join(self.sdir, "session-new.json"), (old, old))
            session.prune_settings(self.sdir)
        self.assertIn("session-new.json", self.settings_files())

    def test_symlinked_private_dir_refused(self):
        real = os.path.join(self.home, "elsewhere")
        os.mkdir(real, 0o700)
        os.makedirs(os.path.dirname(self.sdir), mode=0o700)
        os.symlink(real, self.sdir)
        with self.assertRaises(session.SessionError) as cm:
            self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual(os.listdir(real), [])

    def test_symlinked_ancestor_refused(self):
        real = os.path.join(self.home, "realcfg")
        os.mkdir(real, 0o700)
        os.symlink(real, os.path.join(self.home, ".config"))
        with self.assertRaises(session.SessionError):
            self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])

    def test_loose_private_dir_refused(self):
        for mode in (0o770, 0o707, 0o755):
            with self.subTest(mode=oct(mode)):
                os.makedirs(self.sdir, mode=0o700, exist_ok=True)
                os.chmod(self.sdir, mode)
                with self.assertRaises(session.SessionError) as cm:
                    self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
                self.assertIn("mode", str(cm.exception))
                self.assertEqual(self.settings_files(), [])

    def test_foreign_owner_refused(self):
        real_lstat = os.lstat

        def fake(p, *a, **k):
            st = real_lstat(p, *a, **k)
            if p == self.home:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, os.getuid() + 1) + tuple(st)[5:])
            return st
        with mock.patch("os.lstat", fake):
            with self.assertRaises(session.SessionError) as cm:
                self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn("owned by", str(cm.exception))

    def test_happy_path_private_dir(self):
        path = self.launch("http://r.example", "tok-A")
        self.assertEqual(stat.S_IMODE(os.lstat(self.sdir).st_mode), 0o700)
        self.assertEqual(os.lstat(path).st_uid, os.getuid())

    def test_refused_passthrough_args(self):
        for extra in (["--settings", "other.json"], ["--settings=other.json"], ["--setting-sources", "user"],
                      ["--setting-sources=user"], ["--model", "opus"], ["--model=opus"], ["-c", "--settings", "x"]):
            with self.subTest(extra=extra):
                with self.assertRaises(session.SessionError):
                    self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok, "--"] + extra)
                self.assertEqual(self.settings_files(), [])
        _, _, cmd = self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok, "--",
                                  "--mcp-config", "mcp.json", "--settingsx"])
        self.assertEqual(cmd[-3:], ["--mcp-config", "mcp.json", "--settingsx"])

    def write_managed(self, rel, content):
        path = os.path.join(self.managed, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(content, f)

    def test_managed_settings_conflict_refuses(self):
        self.write_managed("managed-settings.json", {"env": {"ANTHROPIC_API_KEY": "corp-key", "OTHER": "x"}})
        with self.assertRaises(session.SessionError) as cm:
            self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))
        self.assertNotIn("corp-key", str(cm.exception))
        self.assertEqual(self.settings_files(), [], "no token file is written for a refused launch")
        rc, out, err = self.run_main("--provider-url", "http://r.example", "--token-file", self.tok, "--print-env")
        self.assertEqual(rc, 2)
        self.assertIn("managed settings", err)

    def test_managed_dropin_conflict_refuses(self):
        self.write_managed("managed-settings.d/10-corp.json", {"env": {"ANTHROPIC_BASE_URL": "https://corp.example"}})
        with self.assertRaises(session.SessionError) as cm:
            self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn("ANTHROPIC_BASE_URL", str(cm.exception))

    def test_managed_settings_without_conflict_ok(self):
        self.write_managed("managed-settings.json", {"env": {"OTHER": "x"}, "permissions": {}})
        self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertEqual(len(self.settings_files()), 1)

    def test_managed_dropin_dir_errors_fail_closed(self):
        from pathlib import Path
        os.makedirs(os.path.join(self.managed, "managed-settings.d"))
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(session.SessionError) as cm:
                self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn(os.path.join(self.managed, "managed-settings.d"), str(cm.exception))
        self.assertEqual(self.settings_files(), [])
        for exc in (FileNotFoundError(2, "x"), NotADirectoryError(20, "x")):       # absent dir: no drop-ins
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(Path, "iterdir", side_effect=exc):
                    self.assertEqual(session.managed_settings_files((self.managed,)),
                                     [Path(self.managed) / "managed-settings.json"])

    def test_managed_unreadable_or_invalid_refuses(self):
        from pathlib import Path
        self.write_managed("managed-settings.d/20-bad.json", {})
        bad = os.path.join(self.managed, "managed-settings.d", "20-bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        with self.assertRaises(session.SessionError) as cm:
            self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn(bad, str(cm.exception))
        os.unlink(bad)
        self.write_managed("managed-settings.json", {"env": {}})
        real = Path.read_text

        def unreadable(p, *a, **k):
            if str(p).startswith(self.managed):
                raise PermissionError(13, "Permission denied", str(p))
            return real(p, *a, **k)
        with mock.patch.object(Path, "read_text", unreadable):
            with self.assertRaises(session.SessionError) as cm:
                self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok])
        self.assertIn("managed-settings.json", str(cm.exception))
        self.assertEqual(self.settings_files(), [])

    def test_launcher_models_are_served(self):
        """Every launcher default and short name goes through the provider's real normalize_model and choose (real
        tables and core on throwaway state, lane health stubbed, S1 forbidden) and is served, never a client error."""
        import tempfile as _tf
        from pathlib import Path
        from cortados_router import core
        from cortados_router.provider import routing
        keep = (core.STATE, core.lane_health, core.decide)
        self.addCleanup(lambda: setattr(core, "STATE", keep[0]) or setattr(core, "lane_health", keep[1])
                        or setattr(core, "decide", keep[2]))
        core.STATE = Path(_tf.mkdtemp(prefix="cortados-session-served-"))
        core.lane_health = lambda: {}

        def no_s1(text, ctx=None):
            raise AssertionError("a sub/ pin must not be scored")
        core.decide = no_s1
        served = {m for p, m in routing.served_routes(core) if p.split("#")[0] == "claude-cli"}
        names = {n: session.resolve_model(n) for n in session.ORCHESTRATORS}
        names["default"] = session.resolve_model(session.DEFAULT_ORCHESTRATOR)
        names["small-fast"] = session.SMALL_FAST_MODEL
        for label, model in names.items():
            with self.subTest(name=label, model=model):
                self.assertTrue(model.startswith("sub/"))
                self.assertIn(routing.normalize_model(model), served)
                body = {"model": model, "messages": [{"role": "user", "content": "plan the work"}]}
                r = routing.choose(core, body, client="claude-cli", conversation_id="c-" + label, claude_bin="python3",
                                   codex_bin="python3")
                self.assertFalse(r.get("client_error"), r)
                self.assertNotIn("error", r)
                self.assertEqual(r["model"], routing.normalize_model(model))

if __name__ == "__main__":
    unittest.main()
