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
    from cortados_router import session
    return (session.BOUNDARY_HOOK_GUARD_HEAD + sh_quote(python) + " -I -S -B -c "
            + sh_quote(Path(hook.__file__).read_text(encoding="utf-8")) + session.BOUNDARY_HOOK_GUARD_TAIL)


def ad5ab4c_mod_command(python: str) -> str:
    """The mod's command before the -S and guard fix (ad5ab4c): the regression tests show it fails."""
    return "cd / && exec " + sh_quote(python) + " -I -B -c " + sh_quote(Path(hook.__file__).read_text(encoding="utf-8"))


def child_command(python: str) -> str:
    """session.py's child-task hook command on the given interpreter."""
    from cortados_router import session
    return session.boundary_hook_entry(python=python)["hooks"][0]["command"]


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

    def test_other_tools_are_not_the_hooks_unless_marked(self):
        # An unmarked call of a tool outside HOOK_TOOLS returns at once: the provider is not asked.
        self.assertAllowed(self.run_hook("Read", {"file_path": "/etc/hosts"}, tid="toolu_01CloudModelOwnId"))
        self.assertEqual(FakeCheck.asked, [])
        # Since step 8 (matcher "*"), a provider-marked call of ANY tool is checked, and fails closed without a record.
        self.assertDenied(self.run_hook("Read", {"file_path": "/etc/hosts"}), "no live record")
        FakeCheck.answers[MARKED] = record(eb.SENSITIVE, self.project, tool="Read")
        self.assertAllowed(self.run_hook("Read", {"file_path": "/etc/hosts"}))


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


class IsolationBase(HookBase):
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


class IsolationTests(IsolationBase):
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
        command = session.boundary_hook_entry()["hooks"][0]["command"]
        head, tail = session.BOUNDARY_HOOK_GUARD_HEAD, session.BOUNDARY_HOOK_GUARD_TAIL
        self.assertTrue(command.startswith(head) and command.endswith(tail), command)
        self.assertTrue(head.endswith("(cd / && exec "))
        argv = shlex.split(command[len(head):-len(tail)])
        self.assertTrue(os.path.isabs(argv[0]), argv)
        self.assertEqual(argv[1:4], ["-I", "-S", "-B"])
        self.assertEqual(argv[4:], [str(Path(hook.__file__).resolve())])
        # Every helper of the guard is named by absolute path: no PATH lookup there either.
        for tool in ("cat", "tr", "grep", "sed"):
            self.assertNotRegex(head + tail, r"(?<![/\w])" + tool + r" ")

    def test_a_copy_next_to_planted_modules_ignores_them(self):
        # The script in a writable folder (here outside the project) with planted modules beside it.
        side = os.path.join(self.tmp, "writable")
        os.makedirs(side)
        copy = os.path.join(side, "boundary_hook.py")
        shutil.copy(hook.__file__, copy)
        self.plant(side)
        self.plant(self.project)
        self.assertBlockedAndClean(self.run_command(["sh", "-c", "cd / && exec %s -I -S -B %s" % (
            shlex.quote(PY), shlex.quote(copy))]))
        # With -I but without -S (an ad5ab4c install) a marked call is denied: site may have run project code.
        self.assertBlockedAndClean(self.run_command(["sh", "-c", "cd / && exec %s -I -B %s" % (
            shlex.quote(PY), shlex.quote(copy))]), "without python -S")
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
        r = subprocess.run(["sh", "-c", "cd / && exec %s -I -S -B %s" % (shlex.quote(PY), shlex.quote(copy))],
                           input=ev, capture_output=True, text=True, cwd=self.project, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "CORTADOS_BOUNDARY_URL": self.url,
                                "CORTADOS_BOUNDARY_TOKEN": TOKEN})
        self.assertBlockedAndClean(r, "hook file lies inside the project")


class SitePthTests(IsolationBase):
    """Review finding 2026-11-14 (HIGH): -I does not disable `site`. A .pth file in the interpreter's site-packages (an
    editable install pointing at the project root) puts the project on sys.path, and site then imports a project
    sitecustomize.py the model may write, before the hook runs. -S (no site import) closes it."""

    def setUp(self):
        super().setUp()
        venv = os.path.join(self.tmp, "venv")                  # outside the project, as an owner's venv would be
        subprocess.run([PY, "-I", "-m", "venv", "--without-pip", venv], check=True, capture_output=True, timeout=120)
        self.venv_py = os.path.join(venv, "bin", "python3")
        site = subprocess.run([self.venv_py, "-I", "-c", "import site; print(site.getsitepackages()[0])"],
                              capture_output=True, text=True, check=True, cwd="/").stdout.strip()
        with open(os.path.join(site, "editable_project.pth"), "w", encoding="utf-8") as f:
            f.write(self.project + "\n")
        with open(os.path.join(self.project, "sitecustomize.py"), "w", encoding="utf-8") as f:
            f.write("import os\nopen(os.path.join(%r, 'sitecustomize'), 'w').close()\nos._exit(0)\n" % self.pwned)

    def clean_env(self):
        return {"PATH": "/usr/bin:/bin", "CORTADOS_BOUNDARY_URL": self.url, "CORTADOS_BOUNDARY_TOKEN": TOKEN}

    def test_the_old_commands_ran_the_project_sitecustomize(self):
        # The control: ad5ab4c's -I -B command runs the payload, which allows the marked write (exit 0).
        r = self.run_command(["sh", "-c", ad5ab4c_mod_command(self.venv_py)], env=self.clean_env())
        self.assertEqual(os.listdir(self.pwned), ["sitecustomize"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_child_task_command_skips_site(self):
        self.assertBlockedAndClean(self.run_command(["sh", "-c", child_command(self.venv_py)], env=self.clean_env()))

    def test_the_mod_command_skips_site(self):
        self.assertBlockedAndClean(self.run_command(["sh", "-c", mod_command(self.venv_py)], env=self.clean_env()))


class GuardTests(unittest.TestCase):
    """Review finding 2026-11-14 (HIGH): an interpreter that cannot start failed OPEN. Claude Code blocks only on exit 2;
    a missing hookPython (126/127) or a broken /usr/bin/python3 shim (bad DEVELOPER_DIR) is a non-blocking error and
    the call ran. The shell guard decides those from the event's tool_use_id field. Both builders, through sh."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {"PATH": "/usr/bin:/bin",                  # no provider: the real hook fails closed on its own
                    "CORTADOS_BOUNDARY_URL": f"http://127.0.0.1:{closed_port()}", "CORTADOS_BOUNDARY_TOKEN": TOKEN}

    def fake(self, name, body):
        """A fake interpreter: a shell script standing in for python3."""
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def run_guarded(self, python, event):
        raw = event if isinstance(event, str) else json.dumps(event)
        out = []
        for build in (child_command, mod_command):
            r = subprocess.run(["sh", "-c", build(python)], input=raw, capture_output=True, text=True, env=self.env,
                               cwd=self.tmp, timeout=60)
            self.assertNotIn(TOKEN, r.stdout + r.stderr)
            out.append((r.returncode, r.stderr))
        self.assertEqual(out[0][0], out[1][0], out)
        return out[0]

    def ev(self, tid, tool_input=None):
        return {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Write",
                "tool_input": tool_input or {"file_path": "/x/y"}, "tool_use_id": tid}

    def assertGuardDenied(self, result):
        self.assertEqual(result[0], 2, result[1])
        self.assertIn("cortados boundary: denied", result[1])

    def test_a_missing_interpreter_denies_a_marked_call(self):
        missing = os.path.join(self.tmp, "no-such", "python3")
        self.assertGuardDenied(self.run_guarded(missing, self.ev(MARKED)))
        self.assertGuardDenied(self.run_guarded(self.tmp, self.ev(MARKED)))     # a folder: 126

    def test_a_missing_interpreter_allows_an_unmarked_call(self):
        missing = os.path.join(self.tmp, "no-such", "python3")
        self.assertEqual(self.run_guarded(missing, self.ev("toolu_01CloudModelOwnId"))[0], 0)
        self.assertEqual(self.run_guarded(missing, {"tool_name": "Write", "tool_input": {}})[0], 0)   # no id

    def test_the_id_field_decides_not_a_marked_id_inside_tool_input(self):
        missing = os.path.join(self.tmp, "no-such", "python3")
        ev = self.ev("toolu_01CloudModelOwnId", {"file_path": "/x/" + MARKED, "content": '"%s" %s' % (MARKED, MARKED)})
        self.assertIn('"' + MARKED, json.dumps(ev).replace('\\"', '"'))
        self.assertEqual(self.run_guarded(missing, ev)[0], 0)
        self.assertEqual(self.run_guarded(missing, json.dumps(ev, indent=2))[0], 0)

    def test_whitespace_and_key_order(self):
        missing = os.path.join(self.tmp, "no-such", "python3")
        pretty = '{\n  "tool_use_id"  :\n\t"%s",\r\n  "tool_name": "Bash",\n  "tool_input": {"command": "ls"}\n}' % MARKED
        self.assertGuardDenied(self.run_guarded(missing, pretty))
        self.assertEqual(self.run_guarded(missing, pretty.replace(MARKED, "toolu_01Own"))[0], 0)

    def test_an_id_it_cannot_read_fails_closed_only_with_the_prefix_present(self):
        missing = os.path.join(self.tmp, "no-such", "python3")
        # A second tool_use_id key (inside tool_input): which one is the event's is not read; the prefix is present.
        nested = self.ev("toolu_01CloudModelOwnId", {"file_path": "/x", "tool_use_id": MARKED})
        self.assertGuardDenied(self.run_guarded(missing, nested))
        self.assertGuardDenied(self.run_guarded(missing, 'not json "%s' % MARKED))
        self.assertEqual(self.run_guarded(missing, "not json")[0], 0)
        self.assertEqual(self.run_guarded(missing, self.ev(None))[0], 0)

    def test_a_genuine_exit_2_is_kept(self):
        two = self.fake("exits2", "cat >/dev/null; echo 'cortados boundary: denied: test' >&2; exit 2")
        self.assertGuardDenied(self.run_guarded(two, self.ev("toolu_01CloudModelOwnId")))
        self.assertGuardDenied(self.run_guarded(two, self.ev(MARKED)))

    def test_exit_0_allows(self):
        zero = self.fake("exits0", "cat >/dev/null; exit 0")
        self.assertEqual(self.run_guarded(zero, self.ev(MARKED)), (0, ""))

    def test_a_crashing_hook_denies_marked_and_allows_unmarked(self):
        for body in ("cat >/dev/null; exit 1", "cat >/dev/null; kill -SEGV $$", "kill -KILL $$", "exit 3"):
            crash = self.fake("crash", body)
            self.assertGuardDenied(self.run_guarded(crash, self.ev(MARKED)))
            self.assertEqual(self.run_guarded(crash, self.ev("toolu_01CloudModelOwnId"))[0], 0, body)

    def test_a_broken_shim_that_exits_126(self):
        # /usr/bin/python3 on macOS is an xcrun shim; with an invalid DEVELOPER_DIR it fails before Python starts.
        shim = self.fake("python3", "echo 'xcrun: error: invalid DEVELOPER_DIR path' >&2; exit 126")
        self.assertGuardDenied(self.run_guarded(shim, self.ev(MARKED)))
        self.assertEqual(self.run_guarded(shim, self.ev("toolu_01CloudModelOwnId"))[0], 0)

    def test_the_real_hook_still_decides(self):
        # The guard leaves the hook's own answers alone: unmarked unknown 0, marked with no provider 2 (its reason).
        self.assertEqual(self.run_guarded(PY, self.ev("toolu_01CloudModelOwnId")), (0, ""))
        code, err = self.run_guarded(PY, self.ev(MARKED))
        self.assertEqual(code, 2, err)
        self.assertIn("cortados boundary: denied", err)
        self.assertNotIn("the hook did not run", err)


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
        """mod_command mirrors register.ts boundaryHookCommand: the shell guard (the same two strings as session.py),
        from /, absolute interpreter, -I -S, the source inline."""
        from cortados_router import session
        src = REGISTER_TS.read_text(encoding="utf-8")
        for name in ("BOUNDARY_HOOK_GUARD_HEAD", "BOUNDARY_HOOK_GUARD_TAIL"):
            m = re.search(r"export const %s = String\.raw`([^`]*)`" % name, src)
            self.assertIsNotNone(m, "register.ts must export " + name)
            self.assertEqual(m.group(1), getattr(session, name))
        self.assertIn("return BOUNDARY_HOOK_GUARD_HEAD + shQuote(python) + ' -I -S -B -c ' + shQuote(BOUNDARY_HOOK_PY)"
                      " + BOUNDARY_HOOK_GUARD_TAIL", src)
        self.assertIn("hookPython: '/usr/bin/python3',", src)
        self.assertIn("command: boundaryHookCommand(python)", src)

    def test_constants_match_the_provider(self):
        from cortados_router import session
        self.assertEqual(hook.MARK_PREFIX, eb.MARK_PREFIX)
        self.assertIn("toolu_cxb_*)", session.BOUNDARY_HOOK_GUARD_TAIL)
        self.assertEqual(session.BOUNDARY_HOOK_GUARD_TAIL.count(eb.MARK_PREFIX), 2)   # the id case, the raw check
        self.assertEqual(hook.WRITE_KEYS, eb.WRITE_TOOLS)
        self.assertEqual(hook.HOOK_TOOLS, eb.HOOK_TOOLS)
        self.assertEqual(session.BOUNDARY_HOOK_HEADER, eb.HOOK_HEADER)
        self.assertEqual(session.BOUNDARY_HOOK_MATCHER, "*")         # every tool (step 8); decide() narrows it
        from cortados_router.provider import server
        self.assertEqual(hook.CHECK_PATH, server.BOUNDARY_CHECK_PATH)


if __name__ == "__main__":
    unittest.main()
