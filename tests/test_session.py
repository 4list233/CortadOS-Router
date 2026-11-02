import io
import json
import os
import stat
import tempfile
import unittest

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
        self.settings = os.path.join(self.tmp.name, "cfg", "session-settings.json")
        self.managed = os.path.join(self.tmp.name, "managed")     # empty: no managed settings unless a test writes one
        # Belt and braces: even a call that misses --settings-file must never write the real ~/.config file.
        keep = session.SETTINGS_FILE
        session.SETTINGS_FILE = os.path.join(self.tmp.name, "default", "session-settings.json")
        self.addCleanup(setattr, session, "SETTINGS_FILE", keep)

    def prepare(self, argv, base=None):
        return session.prepare(["--settings-file", self.settings] + list(argv), self.base if base is None else base,
                               managed_dirs=(self.managed,))

    def run_main(self, *argv, base=None):
        out, err = io.StringIO(), io.StringIO()
        rc = session.main(["--settings-file", self.settings] + list(argv), base_env=self.base if base is None else base,
                          out=out, err=err, managed_dirs=(self.managed,))
        return rc, out.getvalue(), err.getvalue()

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
        self.assertEqual(cmd[cmd.index("--settings") + 1], self.settings)
        with open(self.settings, encoding="utf-8") as f:
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
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.settings)).st_mode), 0o700)
        self.assertFalse(any(SECRET in a for a in cmd), "token must never be in argv")

    def test_settings_file_replaced_each_launch(self):
        self.prepare(["--provider-url", "http://a.example", "--token-file", self.tok])
        os.chmod(self.settings, 0o644)                      # a loosened old file is replaced, not reused
        with open(self.tok, "w") as f:
            f.write("tok-OTHER\n")
        self.prepare(["--provider-url", "http://b.example", "--token-file", self.tok])
        with open(self.settings, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "http://b.example")
        self.assertEqual(data["env"]["ANTHROPIC_API_KEY"], "tok-OTHER")
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.settings)) if n.endswith(".tmp")], [])

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
        self.assertFalse(os.path.exists(self.settings), "no token file is written for a refused launch")
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
        self.assertTrue(os.path.exists(self.settings))

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
