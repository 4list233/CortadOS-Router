"""Route picker step 6 (docs/ORCHESTRATOR-ROUTE-PICKER.md §1.2, §7): the launcher's --route, --route last,
--no-route-prompt, the interactive menu on a TTY, route-pick.json, the offline menu, the widening lines and the E11
Haiku warning. Fakes only: injected stdin/stdout (a fake TTY), a fake route-options fetch, a throwaway HOME. No real
provider, no vendor CLI, no exec of claude."""
from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from cortados_router import session
from cortados_router.provider import route_options

SECRET = "tok-SECRET-123"
OPUS, SOL, ASTRA, FABLE = "claude-opus-5-5", "gpt-6.1-sol", "gpt-6-astra", "claude-fable-5-1"


class TTY(io.StringIO):
    def isatty(self):
        return True


def model(mid, name, general, routes, ceiling=False, short=()):
    return {"id": mid, "ids": [mid], "name": name, "index": {"general": general}, "ceiling": ceiling,
            "short_of": list(short), "below_face_floor": False, "routes": routes}


ROUTES_CLAUDE = [{"provider": "claude-cli", "lane": "claude", "account": "A", "state": "ok", "left_pct": 62.0},
                 {"provider": "claude-cli#b", "lane": "claude#b", "account": "B", "state": "ok", "left_pct": 40.0},
                 {"provider": "anthropic-api", "lane": "anthropic-api", "account": None, "state": "ok",
                  "left_usd": 142.0}]
ROUTES_CODEX = [{"provider": "codex-cli", "lane": "codex", "account": None, "state": "ok", "left_pct": 71.0}]
R3_VIEW = {"band": "R3", "errors": [], "cloud": {
    "reference": {"general": 79.6},
    "models": [model(ASTRA, "GPT-6 Astra", 100.0, ROUTES_CODEX, ceiling=True),
               model(OPUS, "Claude Opus 5.5", 99.8, ROUTES_CLAUDE, ceiling=True),
               model(FABLE, "Claude Fable 5.1", 91.2,
                     [dict(ROUTES_CLAUDE[0], left_pct=12.0), dict(ROUTES_CLAUDE[1], state="spent", left_pct=0.0)],
                     ceiling=True),
               model(SOL, "GPT-6.1 Sol", 90.1, ROUTES_CODEX, short=("code",)),
               model("claude-opus-5", "Claude Opus 5", 81.5,
                     [{"provider": "claude-cli", "lane": "claude", "account": "A", "state": "unread"},
                      {"provider": "claude-cli#b", "lane": "claude#b", "account": "B", "state": "unread"}]),
               model("gpt-5.6-sol", "GPT-5.6 Sol", 79.6, ROUTES_CODEX)],
    "below_band": [{"id": "gpt-6-sol", "general": 73.6}, {"id": "claude-sonnet-5", "general": 52.8},
                   {"id": "claude-haiku-4-5-20251001", "general": 13.3}]}}
LOCAL_VIEW = {"band": "local", "errors": [], "local": {
    "status": "ok", "note": route_options.LOCAL_NOTE, "enabled": False,
    "models": [{"id": "qwen-27b", "node": "pro", "node_label": "Pro", "state": "switch off"}]}}


class RouteBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tok = os.path.join(self.tmp.name, "token")
        with open(self.tok, "w") as f:
            f.write(SECRET + "\n")
        self.home = os.path.join(os.path.realpath(self.tmp.name), "home")
        os.mkdir(self.home, 0o700)
        self.sdir = os.path.join(self.home, ".config", "cortados", "sessions")
        self.managed = os.path.join(self.tmp.name, "managed")
        keep = session.SETTINGS_DIR
        session.SETTINGS_DIR = os.path.join(self.home, "default-sessions")
        self.addCleanup(setattr, session, "SETTINGS_DIR", keep)
        p = mock.patch.dict(os.environ, {"HOME": self.home})      # never the owner's ~/.config/cortados
        p.start(); self.addCleanup(p.stop)
        self.base = {"PATH": "/usr/bin"}
        self.fetched = []
        self.views = {"R3": R3_VIEW, "R0": R3_VIEW, "R4": {**R3_VIEW, "band": "R4"}, "local": LOCAL_VIEW}
        self.offline = False

    def fetch(self, band):
        self.fetched.append(band)
        if self.offline:
            raise session.RouteOptionsUnavailable("provider unreachable (ConnectionRefusedError)")
        return self.views[band]

    def prepare(self, *argv, answers=None, tty=True):
        out = TTY() if tty else io.StringIO()
        inp = (TTY if tty else io.StringIO)(answers or "")
        args, env, cmd = session.prepare(["--settings-dir", self.sdir, "--provider-url", "http://r.example",
                                          "--token-file", self.tok] + list(argv), self.base,
                                         managed_dirs=(self.managed,), home=self.home, stdin=inp, out=out,
                                         fetch=self.fetch)
        return cmd[cmd.index("--model") + 1], out.getvalue(), args

    def main(self, *argv, answers=""):
        out, err = TTY(), io.StringIO()
        with mock.patch.object(os, "execvpe") as ex:
            rc = session.main(["--settings-dir", self.sdir, "--provider-url", "http://r.example", "--token-file",
                               self.tok] + list(argv), base_env=self.base, out=out, err=err,
                              managed_dirs=(self.managed,), home=self.home, stdin=TTY(answers), fetch=self.fetch)
        return rc, out.getvalue(), err.getvalue(), ex

    def pick_path(self):
        return os.path.join(self.home, ".config", "cortados", "route-pick.json")


class NoPromptTests(RouteBase):

    def test_no_prompt_without_a_tty(self):
        m, out, _ = self.prepare(tty=False)
        self.assertEqual((m, out, self.fetched), ("sub/" + FABLE, "", []))   # today's default, no menu, no fetch

    def test_no_prompt_under_print_env(self):
        rc, out, err, ex = self.main("--print-env")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CortadOS: route", out)
        self.assertIn("--model sub/" + FABLE, out)
        self.assertEqual(self.fetched, [])
        ex.assert_not_called()

    def test_no_prompt_for_child_tasks(self):
        m, out, _ = self.prepare("--orchestrator", "cortados", "--orchestrator-band", "any",
                                 "--run-id", "t20261125000000-0000abcd")
        self.assertEqual((m, out, self.fetched), ("cortados", "", []))

    def test_no_prompt_with_no_route_prompt_or_an_orchestrator_flag(self):
        self.assertEqual(self.prepare("--no-route-prompt")[:2], ("sub/" + FABLE, ""))
        self.assertEqual(self.prepare("--orchestrator", "opus")[:2], ("sub/" + OPUS, ""))
        self.assertEqual(self.fetched, [])


class RouteFlagTests(RouteBase):

    def test_friendly_names_resolve_to_table_ids(self):
        self.assertEqual(self.prepare("--route", "R3:opus+sol")[0], f"cortados@R3:{OPUS}+{SOL}")
        self.assertEqual(self.prepare("--route", "r4:Astra+fable")[0], f"cortados@R4:{FABLE}+{ASTRA}")
        self.assertEqual(self.prepare("--route", "R3:gpt 6.1".replace(" ", "-"))[0], f"cortados@R3:{SOL}")

    def test_window_star_and_any(self):
        self.assertEqual(self.prepare("--route", "R3")[0], "cortados@R3")
        self.assertEqual(self.prepare("--route", "R4:*")[0], "cortados@R4:*")
        self.assertEqual(self.prepare("--route", "any")[0], "cortados")
        self.assertEqual(self.fetched, [])                  # nothing to resolve: the provider is not asked

    def test_offline_route_uses_aliases_and_ids_as_typed(self):
        self.offline = True
        self.assertEqual(self.prepare("--route", f"R3:opus+sol+{ASTRA}")[0], f"cortados@R3:{OPUS}+{ASTRA}+{SOL}")

    def test_ambiguous_and_unknown_names_refused(self):
        with self.assertRaisesRegex(session.SessionError, "ambiguous"):
            self.prepare("--route", "R3:claude-opus")
        with self.assertRaisesRegex(session.SessionError, "not a model the provider serves"):
            self.prepare("--route", "R3:nonesuch")

    def test_conflicts_with_orchestrator_flags_refused(self):
        for extra in (["--orchestrator-band", "R3"], ["--orchestrator", "opus"], ["--orchestrator", "cortados"]):
            with self.subTest(extra=extra), self.assertRaisesRegex(session.SessionError, "cannot be combined"):
                self.prepare("--route", "R3:opus", *extra)
        self.assertFalse(os.path.exists(self.sdir) and os.listdir(self.sdir), "no settings file for a refused launch")

    def test_local_picks_refused_with_the_note(self):
        with self.assertRaisesRegex(session.SessionError, "coming in the next step"):
            self.prepare("--route", "local:qwen-27b")

    def test_route_last(self):
        with self.assertRaisesRegex(session.SessionError, "no route pick has been saved"):
            self.prepare("--route", "last")
        session.save_pick(f"R3:{OPUS}+{SOL}", self.home)
        self.assertEqual(self.prepare("--route", "last")[0], f"cortados@R3:{OPUS}+{SOL}")
        session.save_pick("sub/" + OPUS, self.home)
        self.assertEqual(self.prepare("--route", "last")[0], "sub/" + OPUS)


class PickFileTests(RouteBase):

    def test_pick_file_is_0600_in_a_0700_dir(self):
        path = session.save_pick("R3:*", self.home)
        self.assertEqual(path, self.pick_path())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["pick"], "R3:*")
        self.assertIsInstance(data["saved_at"], float)
        self.assertEqual(session.load_pick(self.home), "R3:*")
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".route-pick-")], [])

    def test_directory_rules(self):
        real = os.path.join(self.home, "elsewhere")
        os.makedirs(real, 0o700)
        os.makedirs(os.path.join(self.home, ".config"), 0o700)
        os.symlink(real, os.path.join(self.home, ".config", "cortados"))
        with self.assertRaisesRegex(session.SessionError, "symlink"):
            session.save_pick("R3", self.home)

    def test_loose_mode_or_bad_content_not_read(self):
        session.save_pick("R3", self.home)
        os.chmod(self.pick_path(), 0o644)
        with self.assertRaisesRegex(session.SessionError, "not a private 0600 file"):
            session.load_pick(self.home)
        for bad in ('{"pick": "R3:<script>"}', '{"pick": 3}', "not json", '{"pick": "sub/../../x"}'):
            with self.subTest(bad=bad):
                with open(self.pick_path(), "w") as f:
                    f.write(bad)
                os.chmod(self.pick_path(), 0o600)
                with self.assertRaisesRegex(session.SessionError, "unreadable"):
                    session.load_pick(self.home)

    def test_a_bad_saved_pick_is_said_and_the_menu_still_runs(self):
        session.save_pick("R3", self.home)
        os.chmod(self.pick_path(), 0o644)
        m, out, _ = self.prepare(answers="6\n")
        self.assertEqual(m, "cortados")
        self.assertIn("not a private 0600 file", out)


class MenuTests(RouteBase):

    def test_first_time_enter_is_todays_behaviour(self):
        m, out, _ = self.prepare(answers="\n")
        self.assertEqual(m, "sub/" + FABLE)
        self.assertIn(f"[Enter = today's default: sub/{FABLE}]", out)
        self.assertFalse(os.path.exists(self.pick_path()))   # Enter is no pick: nothing saved

    def test_menu_rendered_and_pick_saved(self):
        m, out, args = self.prepare(answers="3\n2 4\n\n")
        self.assertEqual(m, f"cortados@R3:{OPUS}+{SOL}")
        self.assertEqual(session.load_pick(self.home), f"R3:{OPUS}+{SOL}")
        want = [
            f"CortadOS: route for this session's main loop        [Enter = today's default: sub/{FABLE}]",
            "  1 R1   2 R2   3 R3   4 R4   5 local   6 any (S1 routes freely)   7 pin one model",
            "> R3 models (general index ≥ 79.6; state from cached readings, no vendor CLI opened)",
            "  [ ] 1 GPT-6 Astra       codex 71% left   frontier",
            "  [ ] 2 Claude Opus 5.5   claude A 62% left · claude B 40% left · API credit $142 left   frontier",
            "  [ ] 3 Claude Fable 5.1  claude A 12% left · claude B spent   frontier",
            "  [ ] 4 GPT-6.1 Sol       codex 71% left   short of code",
            "  [ ] 5 Claude Opus 5     claude A · claude B",
            "  [ ] 6 GPT-5.6 Sol       codex 71% left",
            "  a = any capable R3 model     b = band only (the R3 window)",
            "  numbers toggle, Enter accepts",
            "> R3 models (general index ≥ 79.6; state from cached readings, no vendor CLI opened)",
            "  [ ] 1 GPT-6 Astra       codex 71% left   frontier",
            "  [x] 2 Claude Opus 5.5   claude A 62% left · claude B 40% left · API credit $142 left   frontier",
        ]
        self.assertEqual(out.splitlines()[:len(want)], want)
        self.assertTrue(out.rstrip().endswith(f"→ claude --model cortados@R3:{OPUS}+{SOL}"))
        self.assertEqual(self.fetched, ["R3"])
        # the next launch: Enter keeps it, and the saved models are pre-selected
        m, out, _ = self.prepare(answers="3\n\n")
        self.assertEqual(m, f"cortados@R3:{OPUS}+{SOL}")
        self.assertIn(f"[Enter = last: R3:{OPUS}+{SOL}]", out)
        self.assertIn("  [x] 4 GPT-6.1 Sol", out)
        self.assertEqual(self.prepare(answers="\n")[0], f"cortados@R3:{OPUS}+{SOL}")

    def test_any_capable_band_only_any_and_pin(self):
        self.assertEqual(self.prepare(answers="3\na\n")[0], "cortados@R3:*")
        self.assertEqual(self.prepare(answers="4\nb\n")[0], "cortados@R4")
        self.assertEqual(self.prepare(answers="6\n")[0], "cortados")
        self.assertEqual(self.prepare(answers="7\nopus\n")[0], "sub/" + OPUS)
        self.assertEqual(session.load_pick(self.home), "sub/" + OPUS)

    def test_enter_with_nothing_selected_asks_again(self):
        m, out, _ = self.prepare(answers="3\n\n9\n1\n\n")
        self.assertEqual(m, f"cortados@R3:{ASTRA}")
        self.assertIn("select at least one model, or a / b", out)
        self.assertIn("numbers 1-6, a, b, or Enter", out)

    def test_local_is_shown_with_the_note_and_not_picked(self):
        m, out, _ = self.prepare(answers="5\n6\n")
        self.assertEqual(m, "cortados")
        self.assertIn("  qwen-27b on Pro: switch off", out)
        self.assertIn("local orchestrator: coming in the next step (plan-and-delegate profile)", out)

    def test_offline_menu_when_the_provider_is_unreachable(self):
        self.offline = True
        m, out, _ = self.prepare(answers="3\nopus+sol\n")
        self.assertEqual(m, f"cortados@R3:{OPUS}+{SOL}")
        self.assertIn("provider unreachable (ConnectionRefusedError); offline menu", out)
        self.assertIn("R3: type model names joined by +", out)

    def test_end_of_input_launches_nothing(self):
        rc, out, err, ex = self.main(answers="3\n")
        self.assertEqual(rc, 2)
        self.assertIn("route prompt closed", err)
        ex.assert_not_called()

    def test_endless_wrong_answers_end(self):
        with self.assertRaisesRegex(session.SessionError, "no route picked"):
            self.prepare(answers="x\n" * 20)


class StartLinesTests(RouteBase):

    def test_widening_lines_at_start(self):
        rc, _out, err, ex = self.main("--route", "R3:opus+sol")
        self.assertEqual(rc, 1)                                  # execvpe mocked: main returns past it
        argv = ex.call_args[0][1]
        self.assertEqual(argv[argv.index("--model") + 1], f"cortados@R3:{OPUS}+{SOL}")
        self.assertIn(f"cortados session: main loop: cortados@R3:{OPUS}+{SOL}", err)
        self.assertIn("cortados session: widen mid-session: /model cortados@R3:* (any capable R3 model) or "
                      "/model cortados@R3 (the band window)", err)
        self.assertNotIn("Haiku", err)
        _rc, _o, err, _ex = self.main("--route", "R4:*")
        self.assertIn("widen mid-session: /model cortados@R4 (the band window)", err)
        _rc, _o, err, _ex = self.main("--route", "R3")
        self.assertIn("widen mid-session: /model cortados@R3:* (only the models clearing R3's reference) or "
                      "/model cortados (free routing)", err)

    def test_e11_haiku_warning(self):
        _rc, _o, err, _ex = self.main("--route", "R2:haiku+sol")
        self.assertIn("warning: this pick names a Haiku-family model, so Claude Code drives the main loop like a "
                      "Haiku client", err)
        self.assertEqual(sum("Haiku-family" in line for line in err.splitlines()), 1)

    def test_claude_only_note(self):
        _rc, _o, err, _ex = self.main("--route", "R3:opus+fable")
        self.assertIn("note: a Claude-only pick has no route while S1 is degraded", err)


class FetchTests(unittest.TestCase):

    def test_path_matches_the_provider(self):
        self.assertEqual(session.ROUTE_OPTIONS_PATH, route_options.PATH)

    def test_timeout_and_token(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout):
            seen.update(url=req.full_url, auth=req.get_header("Authorization"), timeout=timeout)
            return Resp(b'{"band": "R3"}')
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(session.fetch_route_options("http://p.example/", "tok", "R3"), {"band": "R3"})
        self.assertEqual(seen, {"url": "http://p.example/v1/cortados/route-options?band=R3", "auth": "Bearer tok",
                                "timeout": 2.0})

    def test_unreachable_is_offline_not_a_crash(self):
        with self.assertRaises(session.RouteOptionsUnavailable):
            session.fetch_route_options("http://127.0.0.1:9", "tok", "R3", timeout=0.5)


if __name__ == "__main__":
    unittest.main()
