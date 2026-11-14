"""The client-side boundary hook (cortados_router/boundary_hook.py, docs/BOUNDARY-HOOK.md): a Claude Code PreToolUse hook
that asks the provider about a tool_use id and enforces the effect boundary on the CLIENT's own filesystem. Fakes only:
a loopback check server stands in for the provider (and one end-to-end test runs the provider test server); no vendor
CLI is opened, no real ~/.cortados is read."""
import io
import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import boundary_hook as hook
from cortados_router.provider import effect_boundary as eb

REPO = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)                    # an absolute interpreter, as the installers resolve one
REGISTER_TS = REPO / "integrations" / "claude-mod" / "cortados-routed-subagents" / "hooks" / "register.ts"
TOKEN = "test-boundary-token-0001"
MARKED = eb.MARK_PREFIX + "a" * 24


class FakeCheck(BaseHTTPRequestHandler):
    """POST /v1/cortados/boundary/check: answers[tool_use_id] (default {"known": false}); records what it was asked."""
    answers: dict = {}
    asked: list = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        FakeCheck.asked.append((self.path, self.headers.get("authorization"), body))
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            code, out = 401, {"error": "bad token"}
        else:
            code = FakeCheck.status
            out = FakeCheck.answers.get(body.get("tool_use_id"),
                                        {"known": False, "marked": str(body.get("tool_use_id")).startswith(
                                            eb.MARK_PREFIX)})
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def record(profile, project, tool="Write"):
    """What the provider answers for a known call (the real check_answer, on a throwaway ledger)."""
    led = eb.BoundaryLedger()
    return eb.check_answer(led.record("conv", tool, profile, project), led)


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"                     # register.ts shQuote


def mod_command(python: str) -> str:
    """register.ts boundaryHookCommand (test_the_mod_builds_its_command_as_mod_command keeps the two in step)."""
    return "cd / && exec " + sh_quote(python) + " -I -B -c " + sh_quote(Path(hook.__file__).read_text(encoding="utf-8"))


class HookBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCheck)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeCheck.answers, FakeCheck.asked, FakeCheck.status = {}, [], 200
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "repo")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(self.project, "src"))
        os.makedirs(self.outside)
        env = {"CORTADOS_BOUNDARY_URL": self.url, "CORTADOS_BOUNDARY_TOKEN": TOKEN}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def run_hook(self, tool, tool_input, tid=MARKED):
        err = io.StringIO()
        event = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                 "tool_use_id": tid, "cwd": self.project}
        code = hook.main(stdin=io.StringIO(json.dumps(event)), stderr=err)
        return code, err.getvalue()

    def assertDenied(self, result, why=""):
        code, err = result
        self.assertEqual(code, 2, err)
        self.assertIn("cortados boundary: denied", err)
        self.assertIn(why, err)
        self.assertNotIn(TOKEN, err)

    def assertAllowed(self, result):
        code, err = result
        self.assertEqual((code, err), (0, ""))


class ContainmentTests(HookBase):
    def test_a_client_only_symlink_escape_is_denied(self):
        # The provider judged /repo/escape/x as inside the project (its own filesystem has no such link); on this
        # machine escape -> /outside, so the write would land outside the project.
        os.symlink(self.outside, os.path.join(self.project, "escape"))
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project)
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                          ("NotebookEdit", "notebook_path")):
            with self.subTest(tool=tool):
                FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project, tool)
                self.assertDenied(self.run_hook(tool, {key: os.path.join(self.project, "escape", "x.py")}),
                                  "outside the project folder")
        self.assertEqual(os.listdir(self.outside), [])

    def test_a_relative_path_through_a_client_symlink_is_denied(self):
        os.symlink(self.outside, os.path.join(self.project, "escape"))
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        self.assertDenied(self.run_hook("Write", {"file_path": "escape/x.py"}), "outside the project folder")

    def test_a_dangling_client_symlink_to_outside_is_denied(self):
        os.symlink(os.path.join(self.outside, "new.txt"), os.path.join(self.project, "src", "new.txt"))
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.project, "src", "new.txt")}))

    def test_a_client_symlink_onto_a_protected_path_is_denied(self):
        os.makedirs(os.path.join(self.project, ".git", "hooks"))
        os.symlink(os.path.join(self.project, ".git"), os.path.join(self.project, "docs"))
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.project, "docs", "hooks", "pre-push")}),
                          "protected path")

    def test_an_in_project_path_is_allowed(self):
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project)
        self.assertAllowed(self.run_hook("Write", {"file_path": os.path.join(self.project, "src", "notes.py")}))
        self.assertAllowed(self.run_hook("Write", {"file_path": os.path.join(self.project, "new", "dir", "a.md")}))
        # an in-project symlink to another in-project folder stays inside
        os.symlink(os.path.join(self.project, "src"), os.path.join(self.project, "alias"))
        self.assertAllowed(self.run_hook("Write", {"file_path": os.path.join(self.project, "alias", "b.py")}))
        self.assertEqual(FakeCheck.asked[0][2], {"tool_use_id": MARKED})       # metadata only: the id

    def test_a_project_folder_that_is_itself_a_client_symlink_is_resolved(self):
        link = os.path.join(self.tmp, "repo-link")
        os.symlink(self.project, link)
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, link)
        self.assertAllowed(self.run_hook("Write", {"file_path": os.path.join(link, "src", "a.py")}))
        self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.outside, "a.py")}))

    def test_build_files_follow_the_profile(self):
        target = {"file_path": os.path.join(self.project, "package.json")}
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project)
        self.assertDenied(self.run_hook("Write", target), "protected path")
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        self.assertAllowed(self.run_hook("Write", target))

    def test_odd_paths_and_records_are_denied(self):
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        for bad in ("", "~/x", "$HOME/x", None, 3):
            with self.subTest(path=bad):
                self.assertDenied(self.run_hook("Write", {"file_path": bad}))
        self.assertDenied(self.run_hook("Write", "not an object"))
        FakeCheck.answers[MARKED] = record(eb.COST, None)                          # no project folder
        self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.project, "a")}), "no project folder")
        FakeCheck.answers[MARKED] = record(eb.COST, self.project, tool="Read")      # not the recorded call
        self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.project, "a")}), "does not match")


class ProfileTests(HookBase):
    def test_bash_is_denied_on_sensitive_and_allowed_on_cost(self):
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project, tool="Bash")
        self.assertDenied(self.run_hook("Bash", {"command": "ls"}), "no shell")
        FakeCheck.answers[MARKED] = record(eb.COST, self.project, tool="Bash")
        self.assertAllowed(self.run_hook("Bash", {"command": "ls"}))

    def test_other_tools_are_not_the_hooks(self):
        self.assertAllowed(self.run_hook("Read", {"file_path": "/etc/hosts"}))
        self.assertEqual(FakeCheck.asked, [])


class UnknownAndFailClosedTests(HookBase):
    def test_an_unknown_tool_use_passes_unchanged(self):
        out = self.run_hook("Write", {"file_path": os.path.join(self.outside, "a.py")}, tid="toolu_01CloudModelOwnId")
        self.assertAllowed(out)
        self.assertEqual(len(FakeCheck.asked), 1)                                   # the provider was asked
        self.assertAllowed(self.run_hook("Bash", {"command": "git push"}, tid="toolu_01CloudModelOwnId"))

    def test_an_unreachable_provider_fails_closed_for_marked_calls(self):
        with mock.patch.dict(os.environ, {"CORTADOS_BOUNDARY_URL": f"http://127.0.0.1:{closed_port()}"}):
            self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.project, "a.py")}),
                              "could not confirm")
            self.assertDenied(self.run_hook("Bash", {"command": "ls"}), "could not confirm")
            # an unmarked call is not the boundary's: the provider's silence changes nothing
            self.assertAllowed(self.run_hook("Write", {"file_path": os.path.join(self.project, "a.py")},
                                             tid="toolu_01CloudModelOwnId"))

    def test_every_other_failure_fails_closed_for_marked_calls(self):
        target = {"file_path": os.path.join(self.project, "a.py")}
        self.assertDenied(self.run_hook("Write", target), "no live record")          # expired or unknown
        FakeCheck.status = 500
        self.assertDenied(self.run_hook("Write", target), "could not confirm")
        FakeCheck.status = 200
        FakeCheck.answers[MARKED] = ["not", "a", "record"]
        self.assertDenied(self.run_hook("Write", target), "could not confirm")
        with mock.patch.dict(os.environ, {"CORTADOS_BOUNDARY_TOKEN": "wrong"}):      # 401
            self.assertDenied(self.run_hook("Write", target), "could not confirm")
        with mock.patch.dict(os.environ, {"CORTADOS_BOUNDARY_URL": "", "ANTHROPIC_BASE_URL": ""}):
            self.assertDenied(self.run_hook("Write", target), "could not confirm")   # nowhere to ask
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        with mock.patch.object(hook, "check_write", side_effect=ZeroDivisionError):  # a crash in the script
            self.assertDenied(self.run_hook("Write", target), "ZeroDivisionError")
            self.assertAllowed(self.run_hook("Write", target, tid="toolu_01CloudModelOwnId"))

    def test_the_session_provider_is_the_default(self):
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        with mock.patch.dict(os.environ, {"CORTADOS_BOUNDARY_URL": "", "CORTADOS_BOUNDARY_TOKEN": "",
                                          "ANTHROPIC_BASE_URL": self.url + "/", "ANTHROPIC_AUTH_TOKEN": TOKEN}):
            self.assertAllowed(self.run_hook("Write", {"file_path": os.path.join(self.project, "a.py")}))
        self.assertEqual(FakeCheck.asked[-1][0], "/v1/cortados/boundary/check")

    def test_unreadable_input_is_not_the_hooks(self):
        self.assertEqual(hook.main(stdin=io.StringIO("not json"), stderr=io.StringIO()), 0)


class SelfLocationTests(HookBase):
    """The hook denies a known call whose project folder holds its interpreter, its file or the file's folder."""

    def test_a_hook_file_inside_the_project_fails_closed(self):
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        target = {"file_path": os.path.join(self.project, "src", "a.py")}
        with mock.patch.dict(hook.__dict__, {"__file__": os.path.join(self.project, "tools", "boundary_hook.py")}):
            self.assertDenied(self.run_hook("Write", target), "hook file lies inside the project")
        with mock.patch.dict(hook.__dict__, {"__file__": os.path.join(self.project, "boundary_hook.py")}):
            self.assertDenied(self.run_hook("Write", target), "inside the project")
        FakeCheck.answers[MARKED] = record(eb.COST, self.project, tool="Bash")
        with mock.patch.dict(hook.__dict__, {"__file__": os.path.join(self.project, "boundary_hook.py")}):
            self.assertDenied(self.run_hook("Bash", {"command": "ls"}), "inside the project")

    def test_a_hook_file_linked_into_the_project_fails_closed(self):
        os.makedirs(os.path.join(self.outside, "real"))
        os.symlink(os.path.join(self.project, "src"), os.path.join(self.outside, "real", "lnk"))
        FakeCheck.answers[MARKED] = record(eb.COST, self.project)
        with mock.patch.dict(hook.__dict__, {"__file__": os.path.join(self.outside, "real", "lnk", "h.py")}):
            self.assertDenied(self.run_hook("Write", {"file_path": os.path.join(self.project, "a.py")}),
                              "inside the project")

    def test_an_interpreter_inside_the_project_fails_closed(self):
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project)
        target = {"file_path": os.path.join(self.project, "src", "a.py")}
        with mock.patch.object(sys, "executable", os.path.join(self.project, ".venv", "bin", "python3")):
            self.assertDenied(self.run_hook("Write", target), "interpreter lies inside the project")
        with mock.patch.object(sys, "executable", ""):
            self.assertDenied(self.run_hook("Write", target), "cannot name its own interpreter")
        self.assertAllowed(self.run_hook("Write", target))                     # the real one is outside

    def test_unmarked_unknown_calls_skip_the_check(self):
        with mock.patch.dict(hook.__dict__, {"__file__": os.path.join(self.project, "boundary_hook.py")}):
            self.assertAllowed(self.run_hook("Write", {"file_path": "/x"}, tid="toolu_01CloudModelOwnId"))


PLANTS = {
    "json.py": "json",
    os.path.join("urllib", "__init__.py"): "urllib",
    os.path.join("http", "__init__.py"): "http",
    "sitecustomize.py": "sitecustomize",
    "usercustomize.py": "usercustomize",
}


class IsolationTests(HookBase):
    """The hook runs as the installers run it, with the project (model-writable) as cwd, holding planted modules that
    would allow the call and record that they ran. Every planted module must stay unexecuted and the marked write
    outside the project must still be denied (exit 2)."""

    def setUp(self):
        super().setUp()
        self.pwned = os.path.join(self.tmp, "pwned")
        os.makedirs(self.pwned)
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project)

    def plant(self, folder):
        for rel, name in PLANTS.items():
            path = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("import os\nopen(os.path.join(%r, %r), 'w').close()\nos._exit(0)\n" % (self.pwned, name))

    def planted_python(self, folder):
        """A python3 in folder/bin that records it ran and allows the call."""
        os.makedirs(os.path.join(folder, "bin"), exist_ok=True)
        fake = os.path.join(folder, "bin", "python3")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n: > %s/python3\nexit 0\n" % shlex.quote(self.pwned))
        os.chmod(fake, 0o755)
        return os.path.join(folder, "bin")

    def env(self):
        planted_bin = self.planted_python(self.project)
        return {"PATH": planted_bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": self.project, "PYTHONSTARTUP": os.path.join(self.project, "json.py"),
                "PYTHONHOME": "", "HOME": self.tmp,
                "CORTADOS_BOUNDARY_URL": self.url, "CORTADOS_BOUNDARY_TOKEN": TOKEN}

    def run_command(self, argv, env=None, cwd=None):
        ev = json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Write",
                         "tool_input": {"file_path": os.path.join(self.outside, "x.py")}, "tool_use_id": MARKED,
                         "cwd": self.project})
        env = {k: v for k, v in (env or self.env()).items() if v != ""}
        return subprocess.run(argv, input=ev, capture_output=True, text=True, env=env, cwd=cwd or self.project,
                              timeout=60)

    def assertBlockedAndClean(self, r, why="outside the project folder"):
        self.assertEqual(os.listdir(self.pwned), [], "a planted module or interpreter ran: " + r.stderr)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("cortados boundary: denied", r.stderr)
        self.assertIn(why, r.stderr)
        self.assertEqual(os.listdir(self.outside), [])

    def test_the_child_task_command_ignores_planted_modules_and_python(self):
        from cortados_router import session
        self.plant(self.project)
        command = session.boundary_hook_entry()["hooks"][0]["command"]
        self.assertBlockedAndClean(self.run_command(["sh", "-c", command]))

    def test_the_mod_command_ignores_planted_modules_and_python(self):
        self.plant(self.project)
        self.assertBlockedAndClean(self.run_command(["sh", "-c", mod_command(PY)]))

    def test_the_installers_never_look_python3_up_on_path(self):
        from cortados_router import session
        argv = shlex.split(session.boundary_hook_entry()["hooks"][0]["command"])
        self.assertEqual(argv[:3], ["cd", "/", "&&"])
        self.assertEqual(argv[3], "exec")
        self.assertTrue(os.path.isabs(argv[4]), argv)
        self.assertEqual(argv[5:7], ["-I", "-B"])
        self.assertEqual(argv[7], str(Path(hook.__file__).resolve()))

    def test_a_copy_next_to_planted_modules_ignores_them(self):
        # The script in a writable folder (here outside the project) with planted modules beside it.
        side = os.path.join(self.tmp, "writable")
        os.makedirs(side)
        copy = os.path.join(side, "boundary_hook.py")
        shutil.copy(hook.__file__, copy)
        self.plant(side)
        self.plant(self.project)
        self.assertBlockedAndClean(self.run_command(["sh", "-c", "cd / && exec %s -I -B %s" % (
            shlex.quote(PY), shlex.quote(copy))]))
        # Even run WITHOUT -I (an older install), its own folder and the cwd are dropped before any import, and a
        # marked call is denied.
        self.assertBlockedAndClean(self.run_command([PY, "-B", copy], env={
            "PATH": os.environ.get("PATH", ""), "CORTADOS_BOUNDARY_URL": self.url,
            "CORTADOS_BOUNDARY_TOKEN": TOKEN}), "without python -I")
        self.assertBlockedAndClean(self.run_command([PY, "-B", "-c", Path(hook.__file__).read_text()], env={
            "PATH": os.environ.get("PATH", ""), "CORTADOS_BOUNDARY_URL": self.url,
            "CORTADOS_BOUNDARY_TOKEN": TOKEN}), "without python -I")

    def test_a_copy_inside_the_project_fails_closed_even_for_an_inside_write(self):
        copy = os.path.join(self.project, "tools", "boundary_hook.py")
        os.makedirs(os.path.dirname(copy))
        shutil.copy(hook.__file__, copy)
        self.plant(os.path.dirname(copy))
        ev = json.dumps({"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.project, "src", "a.py")},
                         "tool_use_id": MARKED})
        r = subprocess.run(["sh", "-c", "cd / && exec %s -I -B %s" % (shlex.quote(PY), shlex.quote(copy))],
                           input=ev, capture_output=True, text=True, cwd=self.project, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "CORTADOS_BOUNDARY_URL": self.url,
                                "CORTADOS_BOUNDARY_TOKEN": TOKEN})
        self.assertBlockedAndClean(r, "hook file lies inside the project")


class SharedSourceTests(unittest.TestCase):
    def test_the_mod_embeds_this_file_verbatim(self):
        """The routed-subagents mod runs the same hook in each routed child (python3 -c <this source>)."""
        src = REGISTER_TS.read_text(encoding="utf-8")
        m = re.search(r"export const BOUNDARY_HOOK_PY = String\.raw`([^`]*)`", src)
        self.assertIsNotNone(m, "register.ts must export BOUNDARY_HOOK_PY")
        self.assertEqual(m.group(1), Path(hook.__file__).read_text(encoding="utf-8"))

    def test_the_source_can_be_embedded(self):
        text = Path(hook.__file__).read_text(encoding="utf-8")
        self.assertNotIn("`", text)
        self.assertNotIn("${", text)

    def test_the_mods_command_line_runs_the_hook(self):
        """The mod's hook command, built as register.ts builds it (mod_command), run by sh."""
        env = {"PATH": os.environ.get("PATH", ""),
               "CORTADOS_BOUNDARY_URL": f"http://127.0.0.1:{closed_port()}", "CORTADOS_BOUNDARY_TOKEN": TOKEN}
        for tid, code in (("toolu_01CloudModelOwnId", 0), (MARKED, 2)):
            ev = json.dumps({"tool_name": "Write", "tool_use_id": tid, "tool_input": {"file_path": "/x/y"}})
            r = subprocess.run(["sh", "-c", mod_command(PY)], input=ev, capture_output=True, text=True, env=env,
                               timeout=60)
            self.assertEqual(r.returncode, code, r.stderr)
            self.assertNotIn(TOKEN, r.stderr + r.stdout)

    def test_the_mod_builds_its_command_as_mod_command(self):
        """mod_command mirrors register.ts boundaryHookCommand: from /, absolute interpreter, -I, the source inline."""
        src = REGISTER_TS.read_text(encoding="utf-8")
        self.assertIn("return 'cd / && exec ' + shQuote(python) + ' -I -B -c ' + shQuote(BOUNDARY_HOOK_PY)", src)
        self.assertIn("hookPython: '/usr/bin/python3',", src)
        self.assertIn("command: boundaryHookCommand(python)", src)

    def test_constants_match_the_provider(self):
        from cortados_router import session
        self.assertEqual(hook.MARK_PREFIX, eb.MARK_PREFIX)
        self.assertEqual(hook.WRITE_KEYS, eb.WRITE_TOOLS)
        self.assertEqual(hook.HOOK_TOOLS, eb.HOOK_TOOLS)
        self.assertEqual(session.BOUNDARY_HOOK_HEADER, eb.HOOK_HEADER)
        self.assertEqual(set(session.BOUNDARY_HOOK_MATCHER.split("|")), set(eb.HOOK_TOOLS))
        from cortados_router.provider import server
        self.assertEqual(hook.CHECK_PATH, server.BOUNDARY_CHECK_PATH)


if __name__ == "__main__":
    unittest.main()
