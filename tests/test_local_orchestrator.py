"""Step 8 of the orchestrator route picker (docs/ORCHESTRATOR-ROUTE-PICKER.md §5, §7 step 8; E8, owner 2026-11-24):
a `local[:<models>]` route pick runs the session's main loop on a local model under the `orchestrator` boundary
profile, plan and delegate only, enforced by the provider's effect boundary AND the client hook; no local node
available is a clear refusal, never a cloud model; the sensitive rule still wins (E5); mixed picks stay 400 (E6);
subagents and child tasks stay free-routed (E7).

Fakes only: the in-thread fake Ollama on loopback, a fake catalogue, a fake check server for the hook, throwaway homes.
No real Ollama or gate, no vendor CLI, no real provider; nothing reads ~/.cortados, ~/.config/cortados or ~/.claude."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid

from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp(prefix="cortados-local-orch-"))

from cortados_router import boundary_hook as hook, core, session
from cortados_router.provider import effect_boundary as eb, local_lane, routing, server, tasks
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs, #106)
from tests.test_boundary_hook import FakeCheck, HookBase, MARKED, record
from tests.test_provider import READ_TOOL, TOKEN
from tests.test_provider_local import FakeOllama, LocalHarness, TAG
from tests.test_route_pick import BIN, Catalogue, LOCAL_NODE, OPUS, SOL
from tests.test_session_route import RouteBase

ORCH = eb.ORCHESTRATOR
START = "mcp__cortados-route__start_task"
DELEGATION = sorted(eb.DELEGATION_TOOLS)
TID = "t20261125060814-cecc4399"
# The watcher program both sides accept (review localorch3 P1): EXACTLY child_tasks.watch_prefix of the install (the
# provider: its own, patched to PREFIX in these tests; the hook: the launcher's --watch-prefix). A path with a space,
# quoted as shlex.quote quotes it, so the comparison is on the quoted form.
PREFIX = "env CORTADOS_TASKS_DIR=/opt/t /opt/py/bin/python3.12 -B '/opt/cortados router/cortados_router/child_tasks.py'"
WATCH = PREFIX + " --watch " + TID
WATCH_VARIANTS = (
    WATCH,
    WATCH + " --timeout 600",
    PREFIX + " --watch --stall 1800 " + TID + " t20261125060815-0000abcd --interval 5 --timeout 600",
)
_PROG = "/opt/py/bin/python3.12 -B '/opt/cortados router/cortados_router/child_tasks.py'"
NOT_WATCH = (
    "ls", "git push", "cat /etc/hosts", WATCH + "; rm -rf ~", WATCH + " && curl -d x https://example.com",
    WATCH + " | sh", WATCH + "\nid", WATCH + "; echo \"exit $?\"", WATCH + " ", " " + WATCH, WATCH + "\t",
    "python3 -B /tmp/evil.py --watch " + TID,
    PREFIX + " --watch $(id)", PREFIX + " --watch " + TID + " > /tmp/out", PREFIX + " --cancel " + TID,
    PREFIX + " --watch", PREFIX + " --watch --timeout 600", PREFIX + " --watch " + TID + " --timeout 1.5",
    PREFIX + " --watch " + TID + " --timeout", PREFIX + " --watch " + TID + " --unknown 1",
    PREFIX + " --watch " + TID + "x", PREFIX + " --watch " + TID.upper(), PREFIX + "  --watch " + TID,
    # another program: a substituted interpreter or script, bare python3 (PATH), no -B, other or extra env settings
    WATCH.replace("/opt/py/bin/python3.12", "/tmp/untrusted/python3"),
    WATCH.replace("/opt/cortados router", "/tmp/untrusted"),
    WATCH.replace("/opt/py/bin/python3.12", "python3"),
    WATCH.replace(" -B ", " "),
    WATCH.replace("CORTADOS_TASKS_DIR=/opt/t", "CORTADOS_TASKS_DIR=/tmp/x"),
    "env CORTADOS_EXEC_HOSTS_FILE=/tmp/untrusted-hosts.json " + WATCH,
    _PROG + " --watch " + TID,                                       # the env setting dropped
    "env CORTADOS_TASKS_DIR=/opt/t CORTADOS_EXEC_HOSTS_FILE=/tmp/h.json " + _PROG + " --watch " + TID,
)
# What a Claude Code main loop declares, by class: read, plan, delegate, the watcher's shell, and everything refused.
READ_PLAN = ["Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "ExitPlanMode", "AskUserQuestion"]
REFUSED = {
    "file write": ["Write", "Edit", "MultiEdit", "NotebookEdit"],
    "agent": ["Agent", "Task"],
    "web": ["WebFetch", "WebSearch"],
    "other MCP": ["mcp__gmail__send_message", "mcp__claude-in-chrome__navigate", "mcp__cortados-route-x__start_task",
                  "mcp__cortados_route__start_task", "mcp__bridge__mcp__cortados-route__start_task"],
    "skills and commands": ["Skill", "SlashCommand", "ToolSearch"],
    "unknown": ["Frobnicate", "bash", "read"],
}


def tool(name, **extra):
    return {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}, **extra}


def every_tool():
    names = READ_PLAN + DELEGATION + ["Bash", "BashOutput", "KillShell", "KillBash"] + [n for v in REFUSED.values()
                                                                                    for n in v]
    return [tool(n) for n in names]


def call(name, inp, cid="toolu_node_own"):
    return {"type": "tool_use", "id": cid, "name": name, "input": inp}

# ------------------------------------------------------------------------------------------- the profile (provider)
class ProfileTests(unittest.TestCase):

    def test_the_allowlist_is_exact(self):
        # Bash only as the watcher; no background-job tools (review localorch3 P2)
        self.assertEqual(eb.PROFILE_TOOLS[ORCH], frozenset(READ_PLAN) | frozenset(DELEGATION) | {"Bash"})
        self.assertEqual(eb.DELEGATION_TOOLS, frozenset(f"mcp__cortados-route__{t}" for t in (
            "start_task", "route_task", "task_status", "wait_task", "list_tasks", "cancel_task")))
        # E5: a sensitive turn of a local pick reads and plans only: no delegation (it leaves the machine), no shell
        self.assertEqual(eb.PROFILE_TOOLS[eb.ORCHESTRATOR_SENSITIVE], frozenset(READ_PLAN))
        self.assertEqual(eb.profile_for(eb.PICK_LOCAL_REASON), ORCH)
        self.assertEqual(eb.profile_for(eb.PICK_LOCAL_SENSITIVE_REASON), eb.ORCHESTRATOR_SENSITIVE)
        # the existing profiles are unchanged
        self.assertNotIn(START, eb.PROFILE_TOOLS[eb.SENSITIVE] | eb.PROFILE_TOOLS[eb.COST])
        self.assertIn("Write", eb.PROFILE_TOOLS[eb.SENSITIVE])

    def test_fenced_only_for_automatic_moves_and_local_picks(self):
        self.assertTrue(eb.fenced(False, eb.PICK_LOCAL_REASON))
        self.assertTrue(eb.fenced(False, eb.PICK_LOCAL_SENSITIVE_REASON))
        self.assertTrue(eb.fenced(True, None))
        self.assertFalse(eb.fenced(False, None))              # a plain sub/ pin: the owner's own choice, untouched
        self.assertFalse(eb.fenced(False, "keyword"))

    def test_every_refused_class_is_refused_and_unknown_fails_closed(self):
        b = eb.Boundary(eb.PICK_LOCAL_REASON, "/Users/someone/code/proj", hook=True)
        for cls, names in REFUSED.items():
            for name in names:
                with self.subTest(cls=cls, name=name):
                    self.assertEqual(b.blocked_call(name, {"file_path": "/Users/someone/code/proj/a.py"}),
                                     "tool-not-allowed")
        for bad in (None, 3, "", "mcp__cortados-route__"):
            self.assertEqual(b.blocked_call(bad, {}), "tool-not-allowed")

    def test_read_plan_and_delegation_are_allowed(self):
        b = eb.Boundary(eb.PICK_LOCAL_REASON, None, hook=True)     # no project folder: nothing writes anyway
        for name in READ_PLAN:
            self.assertIsNone(b.blocked_call(name, {"file_path": "/etc/hosts"}), name)
        for name in DELEGATION:
            self.assertIsNone(b.blocked_call(name, {"task": "fix the tests", "task_id": "t20261125060814-cecc4399"}),
                              name)
        self.assertIsNone(b.blocked_call(START, {"task": "x", "repo": "4list233/CortadOS-Router", "host": "here"}))

    def test_start_task_may_not_ask_for_chrome_or_connectors(self):
        b = eb.Boundary(eb.PICK_LOCAL_REASON, None, hook=True)
        self.assertEqual(b.blocked_call(START, {"task": "x", "chrome": True}), "delegate-chrome")
        self.assertEqual(b.blocked_call(START, {"task": "x", "connectors": ["gmail"]}), "delegate-connectors")
        self.assertEqual(b.blocked_call(START, "not an object"), "delegate-malformed")
        self.assertIsNone(b.blocked_call(START, {"task": "x", "chrome": False, "connectors": []}))

    def test_the_shell_is_the_task_watcher_only(self):
        b = eb.Boundary(eb.PICK_LOCAL_REASON, None, hook=True)
        with mock.patch.object(eb, "expected_watch_prefix", return_value=PREFIX):
            for cmd in WATCH_VARIANTS:
                self.assertIsNone(b.blocked_call("Bash", {"command": cmd, "run_in_background": True}), cmd)
            for cmd in NOT_WATCH + (None, 3):
                with self.subTest(cmd=cmd):
                    self.assertEqual(b.blocked_call("Bash", {"command": cmd}), "bash-not-watch")
            self.assertEqual(b.blocked_call("Bash", "ls"), "bash-not-watch")
        # review localorch3 P2: no background-job control, whatever the job id
        for name in ("BashOutput", "KillShell", "KillBash"):
            self.assertEqual(b.blocked_call(name, {"bash_id": "b1", "shell_id": "b1"}), "tool-not-allowed", name)

    def test_the_provider_accepts_exactly_its_own_watch_command(self):
        # review localorch3 P1: the program is this install's (child_tasks.watch_prefix), never one named by the call
        from cortados_router import child_tasks
        for env in ({}, {"CORTADOS_TASKS_DIR": "/opt/t", "CORTADOS_EXEC_HOSTS_FILE": "/opt/h b.json"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, {"CORTADOS_TASKS_DIR": "",
                                                                     "CORTADOS_EXEC_HOSTS_FILE": "", **env}):
                self.assertEqual(eb.expected_watch_prefix(), child_tasks.watch_prefix())
                cmd = child_tasks.watch_command(TID)
                self.assertTrue(eb.is_watch_command(cmd), cmd)
                self.assertTrue(eb.is_watch_command(cmd + " t20261125060815-0000abcd --stall 1800"))
                argv = cmd.split(" ")
                self.assertTrue(os.path.isabs(child_tasks.shlex.split(cmd)[-3]))
                for bad in (cmd.replace(argv[argv.index("-B") - 1], "/tmp/untrusted/python3"),
                            cmd.replace(argv[argv.index("-B") - 1], "python3"),
                            cmd.replace("cortados_router/child_tasks.py", "x/cortados_router/child_tasks.py"),
                            "env CORTADOS_EXEC_HOSTS_FILE=/tmp/h.json " + cmd, cmd + " --unknown 1"):
                    self.assertFalse(eb.is_watch_command(bad), bad)
        self.assertFalse(eb.is_watch_command(WATCH, ""))             # no prefix: nothing is the watcher

    def test_without_the_hook_there_is_no_shell(self):
        b = eb.Boundary(eb.PICK_LOCAL_REASON, None, hook=False)
        self.assertEqual(b.blocked_call("Bash", {"command": WATCH}), "tool-not-allowed")
        self.assertEqual(b.blocked_call("BashOutput", {}), "tool-not-allowed")
        self.assertEqual(b.blocked_call("KillShell", {}), "tool-not-allowed")
        self.assertIsNone(b.blocked_call(START, {"task": "x"}))     # delegation stays: it needs no client check
        out, dropped = b.tools_for_node({"tools": every_tool()})
        self.assertNotIn("Bash", [t["name"] for t in out["tools"]])
        self.assertIn("Bash", dropped)

    def test_the_sensitive_variant_neither_delegates_nor_runs_a_shell(self):
        b = eb.Boundary(eb.PICK_LOCAL_SENSITIVE_REASON, None, hook=True)
        for name in DELEGATION + ["Bash", "Write"]:
            self.assertEqual(b.blocked_call(name, {"command": WATCH, "task": "x"}), "tool-not-allowed", name)
        self.assertIsNone(b.blocked_call("Read", {"file_path": "/etc/hosts"}))

    def test_the_node_is_offered_the_profile_only(self):
        body = {"tools": every_tool() + [{"type": "web_search_20250305", "name": "Read"}],
                "tool_choice": {"type": "tool", "name": "Write"}}
        out, dropped = eb.Boundary(eb.PICK_LOCAL_REASON, None, hook=True).tools_for_node(body)
        self.assertEqual(sorted(t["name"] for t in out["tools"]),
                         sorted(READ_PLAN + DELEGATION + ["Bash"]))
        self.assertNotIn("tool_choice", out)                       # it named a dropped tool
        self.assertIn("Read", dropped)                             # the typed server tool, whatever its name
        for names in REFUSED.values():
            for n in names:
                self.assertIn(n, dropped)

    def test_the_check_answer_carries_the_profile(self):
        led = eb.BoundaryLedger()
        ans = eb.check_answer(led.record("c", START, ORCH, "/Users/someone/code/proj"), led)
        self.assertEqual((ans["profile"], ans["bash"], ans["bash_watch"], ans["write_tools"]), (ORCH, False, True, {}))
        self.assertEqual(ans["tools"], sorted(eb.PROFILE_TOOLS[ORCH]))
        self.assertEqual(ans["delegate_refused"], ["chrome", "connectors"])
        ans = eb.check_answer(led.record("c", "Read", eb.ORCHESTRATOR_SENSITIVE, None), led)
        self.assertEqual((ans["bash"], ans["bash_watch"], ans["tools"]), (False, False, sorted(READ_PLAN)))
        ans = eb.check_answer(led.record("c", "Bash", eb.COST, None), led)        # unchanged for cost
        self.assertEqual((ans["bash"], ans["bash_watch"], ans["delegate_refused"]), (True, False, []))

    def test_the_watch_check_is_one_on_both_sides(self):
        self.assertEqual(hook.WATCH_TAIL_RE.pattern, eb.WATCH_TAIL_RE.pattern)
        self.assertEqual(hook.START_TASK_TOOL, eb.START_TASK_TOOL)
        self.assertEqual(hook.JOB_TOOLS, eb.BASH_FAMILY - eb.ORCHESTRATOR_SHELL)
        for cmd in WATCH_VARIANTS:
            self.assertTrue(hook.is_watch_command(cmd, PREFIX), cmd)
            self.assertTrue(eb.is_watch_command(cmd, PREFIX), cmd)
        for cmd in NOT_WATCH + (None,):
            self.assertFalse(hook.is_watch_command(cmd, PREFIX), cmd)
            self.assertFalse(eb.is_watch_command(cmd, PREFIX), cmd)
        for none in (None, ""):
            self.assertFalse(hook.is_watch_command(WATCH, none))     # a hook without the launcher's prefix: never

    def test_the_blocked_message_says_plan_and_delegate(self):
        self.assertIn("plans and delegates only", eb.blocked_message(ORCH))
        self.assertIn("local orchestrator", eb.blocked_message(eb.ORCHESTRATOR_SENSITIVE))
        self.assertEqual(eb.blocked_message(eb.SENSITIVE), eb.BLOCKED_MESSAGE)


# --------------------------------------------------------------------------------------------- the client hook
class HookTests(HookBase):
    """The client half: the hook checks every tool of a provider-marked call against the record's profile."""

    def answer(self, name, profile=ORCH):
        FakeCheck.answers[MARKED] = record(profile, self.project, tool=name)

    def run_hook(self, tool, tool_input, tid=MARKED, argv=("--watch-prefix", PREFIX)):
        """The hook as a local-pick session runs it: with the launcher's --watch-prefix (session.boundary_hook_entry)."""
        err = io.StringIO()
        event = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                 "tool_use_id": tid, "cwd": self.project}
        code = hook.main(stdin=io.StringIO(json.dumps(event)), stderr=err, argv=list(argv))
        return code, err.getvalue()

    def test_every_refused_class_is_denied_on_the_client(self):
        for cls, names in REFUSED.items():
            for name in names:
                with self.subTest(cls=cls, name=name):
                    self.answer(name)        # even a record naming it (the provider never mints one) is refused
                    self.assertDenied(self.run_hook(name, {"file_path": os.path.join(self.project, "a.py")}),
                                      "not allowed on this local-routed turn's profile (orchestrator)")

    def test_an_unknown_tool_without_a_record_is_denied(self):
        self.assertDenied(self.run_hook("Frobnicate", {}), "no live record")
        with mock.patch.dict(os.environ, {"CORTADOS_BOUNDARY_URL": "http://127.0.0.1:9"}):
            self.assertDenied(self.run_hook("mcp__gmail__send_message", {}), "could not confirm")

    def test_a_record_of_another_tool_is_denied(self):
        self.answer("Read")
        self.assertDenied(self.run_hook(START, {"task": "x"}), "does not match")

    def test_read_plan_and_delegation_are_allowed(self):
        for name in READ_PLAN + DELEGATION:
            with self.subTest(name=name):
                self.answer(name)
                self.assertAllowed(self.run_hook(name, {"task": "fix the tests", "file_path": "/etc/hosts"}))

    def test_start_task_inputs_the_provider_refuses_are_denied(self):
        self.answer(START)
        self.assertDenied(self.run_hook(START, {"task": "x", "chrome": True}), "with chrome")
        self.assertDenied(self.run_hook(START, {"task": "x", "connectors": ["gmail"]}), "with connectors")
        self.assertDenied(self.run_hook(START, "x"), "cannot be checked")
        self.assertAllowed(self.run_hook(START, {"task": "x"}))

    def test_bash_only_as_the_watcher(self):
        self.answer("Bash")
        for cmd in WATCH_VARIANTS:
            self.assertAllowed(self.run_hook("Bash", {"command": cmd, "run_in_background": True}))
        for cmd in NOT_WATCH:
            with self.subTest(cmd=cmd):
                self.assertDenied(self.run_hook("Bash", {"command": cmd}), "exact watch_command")
        # review localorch3 P1: no launcher prefix (a session not launched with a local pick, the mod): no watcher
        for argv in ((), ("--watch-prefix", ""), ("--watch-prefix",)):
            self.assertDenied(self.run_hook("Bash", {"command": WATCH}, argv=argv), "not launched with a local pick")
        self.answer("Bash", eb.ORCHESTRATOR_SENSITIVE)
        self.assertDenied(self.run_hook("Bash", {"command": WATCH}), "no shell")

    def test_an_older_hook_answer_without_tools_still_denies_the_orchestrator_shell(self):
        ans = record(ORCH, self.project, tool="Bash")
        for key in ("tools", "bash_watch"):
            ans.pop(key)
        FakeCheck.answers[MARKED] = ans
        self.assertDenied(self.run_hook("Bash", {"command": WATCH}), "no shell")   # `bash` stays False

    def test_unmarked_calls_of_other_tools_are_not_asked(self):
        self.assertAllowed(self.run_hook(START, {"task": "x"}, tid="toolu_01CloudModelOwnId"))
        self.assertAllowed(self.run_hook("WebFetch", {"url": "https://example.com"}, tid="toolu_01CloudModelOwnId"))
        self.assertEqual(FakeCheck.asked, [])

    def test_background_job_tools_are_denied_whatever_the_id(self):
        # review localorch3 P2: even a record naming them (an older provider's profile listed them)
        for name in ("BashOutput", "KillShell", "KillBash"):
            ans = record(ORCH, self.project, tool=name)
            ans["tools"] = sorted(set(ans["tools"]) | {name})
            FakeCheck.answers[MARKED] = ans
            self.assertDenied(self.run_hook(name, {"bash_id": "b1", "shell_id": "b1"}), "background jobs")
            self.answer(name)
            self.assertDenied(self.run_hook(name, {"bash_id": "b1"}), "not allowed")
        ans = record(eb.COST, self.project, tool="KillShell")           # unchanged for the cost profile
        FakeCheck.answers[MARKED] = ans
        self.assertAllowed(self.run_hook("KillShell", {"shell_id": "b1"}))

    def test_the_hook_matcher_is_every_tool_only_for_a_local_pick(self):
        # review localorch3 (speed): `*` starts the hook on every Read; only a local-pick session needs it
        entry = session.boundary_hook_entry(None, "/usr/bin/python3")
        self.assertEqual(entry["matcher"], "Write|Edit|MultiEdit|NotebookEdit|Bash")
        self.assertNotIn("--watch-prefix", entry["hooks"][0]["command"])
        entry = session.boundary_hook_entry(None, "/usr/bin/python3", local_pick=True, watch_prefix=PREFIX)
        self.assertEqual(entry["matcher"], "*")
        cmd = entry["hooks"][0]["command"]
        inner = cmd[len(session.BOUNDARY_HOOK_GUARD_HEAD):-len(session.BOUNDARY_HOOK_GUARD_TAIL)]
        argv = __import__("shlex").split(inner)
        self.assertEqual(argv[-2:], ["--watch-prefix", PREFIX])
        self.assertEqual(hook.watch_prefix_from(argv[5:]), PREFIX)


# ------------------------------------------------------------------------------------------- selection (routing)
LOCAL_NODE_2 = {"node": "pc", "lane": "local-pc", "label": "PC", "base_url": "http://127.0.0.1:8",
                "models": [{"id": "qwen3.6-35b-a3b:q4", "alias": "qwen-35b", "name": "Qwen 35B",
                            "context_length": 65536}]}
TAGS = {"pro": {"qwen3.8-27b-heretic:q4"}, "pc": {"qwen3.6-35b-a3b:q4"}}


class SelectionTests(Catalogue):

    def setUp(self):
        super().setUp()
        self.local_nodes = [LOCAL_NODE, LOCAL_NODE_2]
        self.down: dict[str, str] = {}
        self.probed: list[str] = []

        def probe(node):
            self.probed.append(node["node"])
            if node["node"] in self.down:
                raise local_lane.LocalUnavailable(f"node {node['node']}: {self.down[node['node']]}")
            return set(TAGS[node["node"]])
        p = mock.patch.object(local_lane, "probe", probe)
        p.start(); self.addCleanup(p.stop)
        self.picks = []
        p = mock.patch.object(core, "log_route_pick", lambda rid, label: self.picks.append((rid, label)) or True)
        p.start(); self.addCleanup(p.stop)

        def no_s1(*a, **k):
            raise AssertionError("a local pick is not scored by S1")
        core.decide = no_s1
        p = mock.patch.object(routing.execution_routing, "select", side_effect=AssertionError("no cloud select"))
        p.start(); self.addCleanup(p.stop)

    def choose(self, text, body=None, **kw):
        pick, err = routing.route_pick(core, None, None, f"cortados@{text}")
        self.assertIsNone(err)
        body = body or {"model": f"cortados@{text}", "messages": [{"role": "user", "content": "plan the release"}]}
        return routing.choose(core, body, client="claude-cli", conversation_id=uuid.uuid4().hex, claude_bin=BIN,
                              codex_bin=BIN, route_pick=pick, **kw)

    def test_the_first_available_picked_model_takes_it_in_the_order_typed(self):
        r = self.choose("local:qwen-35b+qwen-27b")
        self.assertEqual((r["provider"], r["model"]), ("local-pc", "qwen3.6-35b-a3b:q4"))
        self.down["pc"] = "local gate: local disabled: GPU busy with another program"
        r = self.choose("local:qwen-35b+qwen-27b")
        self.assertEqual((r["provider"], r["model"]), ("local-pro", "qwen3.8-27b-heretic:q4"))
        rules = r["route"]["hard_rules"]
        self.assertIn("route pick local:qwen-27b+qwen-35b (orchestrator profile): qwen-27b on node pro", rules)
        self.assertIn("boundary profile: orchestrator", rules)
        self.assertTrue(any("qwen-35b on node pc skipped: node pc: local gate: local disabled: GPU busy" in x
                            for x in rules), rules)
        self.assertEqual((r["bind_reason"], r["pinned"], r[routing.PICK_LOCAL_KEY], r["band"]),
                         ("route_pick_local", True, "local:qwen-27b+qwen-35b", "pin"))
        self.assertEqual(self.picks[-1], (r["route_id"], "local:qwen-27b+qwen-35b"))
        (rid, meta, route) = self.logged[-1]
        self.assertEqual(rid, r["route_id"])

    def test_bare_local_takes_config_order(self):
        r = self.choose("local")
        self.assertEqual(r["model"], "qwen3.8-27b-heretic:q4")
        self.down["pro"] = "local gate: local switch off"
        self.assertEqual(self.choose("local")["model"], "qwen3.6-35b-a3b:q4")

    def test_no_node_available_is_a_clear_refusal_never_cloud(self):
        self.down = {"pro": "local gate: local disabled: on battery", "pc": "local gate: local switch off"}
        r = self.choose("local:qwen-27b+qwen-35b")
        self.assertTrue(r["pick_refused"])
        self.assertNotIn("provider", r)
        self.assertNotIn("client_error", r)                        # 503: a dependency (the nodes), not the client's
        for part in ("qwen-27b on node pro: node pro: local gate: local disabled: on battery",
                     "qwen-35b on node pc: node pc: local gate: local switch off",
                     "nothing ran on any other model, local or cloud", "/model cortados@R3:*", "/model cortados"):
            self.assertIn(part, r["error"])
        self.assertEqual(self.logged, [])                          # nothing ran: no routes row

    def test_a_model_the_node_does_not_list_or_a_busy_node_is_skipped(self):
        TAGS["pro"] = set()
        self.addCleanup(TAGS.__setitem__, "pro", {"qwen3.8-27b-heretic:q4"})
        r = self.choose("local:qwen-27b")
        self.assertIn("does not list", r["error"])
        TAGS["pro"] = {"qwen3.8-27b-heretic:q4"}
        with mock.patch.object(local_lane, "busy", lambda node: True):
            self.assertIn("node pro is busy", self.choose("local:qwen-27b")["error"])

    def test_too_large_everywhere_is_a_413(self):
        big = {"model": "cortados@local", "messages": [{"role": "user", "content": "x " * 400000}]}
        r = self.choose("local", body=big)
        self.assertTrue(r["fit_blocked"])
        self.assertIn("exceeds", r["error"])

    def test_an_excluded_model_is_skipped(self):
        r = self.choose("local:qwen-27b+qwen-35b", exclude_model="qwen-35b")
        self.assertEqual(r["model"], "qwen3.8-27b-heretic:q4")

    def test_a_broken_local_config_refuses(self):
        with mock.patch.object(routing, "local_pins", side_effect=local_lane.LocalConfigError("bad", "pro")):
            r = self.choose("local")
        self.assertTrue(r["pick_refused"])
        self.assertNotIn("provider", r)

    def test_the_sensitive_rule_still_wins(self):
        # E5: a sensitive-marked request keeps the local model and loses delegation and the shell.
        r = self.choose("local:qwen-27b", local="sensitive")
        self.assertEqual((r["model"], r["bind_reason"]), ("qwen3.8-27b-heretic:q4", "route_pick_local_sensitive"))
        self.assertIn("boundary profile: orchestrator-sensitive", r["route"]["hard_rules"])
        with mock.patch.object(routing, "request_sensitivity", lambda body, r, local: "keyword"):
            r = self.choose("local:qwen-27b")
        self.assertEqual(r["bind_reason"], "route_pick_local_sensitive")
        with mock.patch.object(routing, "request_sensitivity", lambda body, r, local: "unknown"):
            self.assertEqual(self.choose("local")["bind_reason"], "route_pick_local_sensitive")   # doubt: stricter
        self.assertEqual(self.choose("local", local="prefer")["bind_reason"], "route_pick_local")  # a cost word

    def test_a_mixed_pick_is_still_400(self):
        for text in (f"local:qwen-27b+{OPUS}", f"R3:{SOL}+qwen-27b"):
            with self.subTest(text=text):
                pick, err = routing.route_pick(core, None, None, f"cortados@{text}")
                self.assertIsNone(pick)
                self.assertIn("mixed picks are refused, E6", err)

    def test_the_tasks_view_labels_it(self):
        r = self.choose("local:qwen-27b", local="sensitive")
        labels = {tasks._rule_label(x) for x in r["route"]["hard_rules"]}
        self.assertIn("route pick local (orchestrator profile)", labels)
        self.assertIn("boundary profile orchestrator-sensitive", labels)
        self.assertIn("route pick local: sensitive, read and plan only", labels)
        self.down["pc"] = "x"
        r = self.choose("local:qwen-35b+qwen-27b")
        labels = {tasks._rule_label(x) for x in r["route"]["hard_rules"]}
        self.assertEqual(labels, {"route pick local (orchestrator profile)", "boundary profile orchestrator",
                                  "route pick local: model skipped"})


class CloudSensitiveRuleUnchangedTests(Catalogue):
    """E5 for a cloud pick is steps 1-3's: sensitive_local still moves it, to its own (sensitive) profile."""

    def test_a_cloud_pick_still_goes_through_sensitive_local(self):
        seen = []

        def moved(core_, body, r, **kw):
            seen.append(kw.get("local"))
            return {**r, "local": {"node": LOCAL_NODE, "model": LOCAL_NODE["models"][0]}, "bind_reason": "sensitive_flag"}
        core.decide = lambda *a, **k: dict(__import__("tests.test_routing_fixes", fromlist=["dec"]).dec())
        with mock.patch.object(routing, "sensitive_local", moved), \
                mock.patch.object(routing, "choose_local_pick", side_effect=AssertionError("not a local pick")):
            pick = routing.resolve_pick(core, f"R3:{SOL}")
            r = routing.choose(core, {"model": f"cortados@R3:{SOL}", "messages": [{"role": "user", "content": "x"}]},
                               client="c", conversation_id="c1", claude_bin=BIN, codex_bin=BIN,
                               band_window=pick.window, route_pick=pick, local="sensitive")
        self.assertEqual((seen, r["bind_reason"], eb.profile_for(r["bind_reason"])),
                         (["sensitive"], "sensitive_flag", eb.SENSITIVE))


# ------------------------------------------------------------------------------------------ end to end (provider)
class EndToEndTests(LocalHarness):
    """The real endpoint over the fake Ollama: a `cortados@local:qwen-27b` main loop."""
    MODEL = "cortados@local:qwen-27b"
    SYSTEM = ("You are Claude Code.\n - Primary working directory: /Users/someone/code/proj\n")
    HOOKED = {eb.HOOK_HEADER: "1", "X-Claude-Code-Session-Id": "6a1d0c3e-local-orch"}

    def setUp(self):
        super().setUp()
        self.reg.local_recheck = lambda b, route: (_ for _ in ()).throw(AssertionError("a pick is not rescored"))
        self._select = routing.execution_routing.select
        self.selects = []

        def select(core_, **kw):
            self.selects.append(kw)
            return {"router": "x", "error": "no cloud route in this test"}
        routing.execution_routing.select = select
        self.addCleanup(setattr, routing.execution_routing, "select", self._select)

    def body(self, text="plan the release", stream=False, model=None, tools=None, messages=None):
        return {"model": model or self.MODEL, "max_tokens": 256, "stream": stream, "system": self.SYSTEM,
                "tools": every_tool() if tools is None else tools,
                "messages": messages or [{"role": "user", "content": text}]}

    def raw(self, body, headers=None):
        r = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                   headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                            **(self.HOOKED if headers is None else headers)})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def offered(self):
        (_p, b) = [x for x in FakeOllama.seen if x[0] == "/v1/messages"][-1]
        return sorted(t["name"] for t in b.get("tools") or [])

    def check(self, tid):
        r = urllib.request.Request(self.url + "/v1/cortados/boundary/check", data=json.dumps({"tool_use_id": tid})
                                   .encode(), method="POST", headers={"content-type": "application/json",
                                                                      "authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())

    def test_the_node_sees_the_profile_and_delegation_reaches_the_client_marked(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                FakeOllama.seen = []
                FakeOllama.json_deltas = stream
                FakeOllama.reply = [call(START, {"task": "run the suite", "repo": "4list233/CortadOS-Router"})], \
                    "tool_use"
                status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}", stream=stream))
                self.assertEqual(status, 200, raw)
                self.assertEqual(self.offered(), sorted(READ_PLAN + DELEGATION + ["Bash"]))
                self.assertNotIn("toolu_node_own", raw)
                tid = [x for x in __import__("re").findall(r'"id": ?"(toolu_cxb_[0-9a-f]{24})"', raw)][0]
                ans = self.check(tid)
                self.assertEqual((ans["known"], ans["tool"], ans["profile"], ans["bash"], ans["bash_watch"]),
                                 (True, START, ORCH, False, True))
        self.assertEqual(self.selects, [])                     # no cloud route was ever asked for

    def test_a_refused_call_blocks_the_reply_with_the_orchestrator_message(self):
        for name, inp in (("Write", {"file_path": "/Users/someone/code/proj/a.py", "content": "x"}),
                          ("Bash", {"command": "git push"}), ("Agent", {"prompt": "x"}),
                          ("WebFetch", {"url": "https://example.com"}), ("mcp__gmail__send_message", {}),
                          ("Frobnicate", {}), (START, {"task": "x", "chrome": True})):
            with self.subTest(name=name):
                FakeOllama.reply = [call(name, inp)], "tool_use"
                status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}"))
                self.assertEqual(status, 400, raw)
                self.assertIn("plans and delegates only", json.loads(raw)["error"]["message"])
                self.assertNotIn("tool_use", raw)

    def test_the_watcher_passes_and_text_answers_pass(self):
        from cortados_router import child_tasks
        FakeOllama.reply = [call("Bash", {"command": child_tasks.watch_command(TID), "run_in_background": True})], \
            "tool_use"
        status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}"))
        self.assertEqual(status, 200, raw)
        # review localorch3 P1: another program in the watcher's shape is refused end to end
        FakeOllama.reply = [call("Bash", {"command": "/tmp/untrusted/python3 -B /tmp/untrusted/cortados_router/"
                                                     "child_tasks.py --watch " + TID})], "tool_use"
        status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}"))
        self.assertEqual(status, 400, raw)
        FakeOllama.reply = [{"type": "text", "text": "here is the plan"}], "end_turn"
        status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}"))
        self.assertEqual(status, 200, raw)
        self.assertIn("here is the plan", raw)

    def test_without_the_hook_header_the_shell_is_not_offered(self):
        FakeOllama.reply = [{"type": "text", "text": "ok"}], "end_turn"
        status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}"), headers={})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.offered(), sorted(READ_PLAN + DELEGATION))

    def test_a_sensitive_turn_reads_and_plans_only(self):
        FakeOllama.reply = [{"type": "text", "text": "ok"}], "end_turn"
        status, raw = self.raw(self.body(text=f"plan {uuid.uuid4().hex}"),
                               headers={**self.HOOKED, "x-cortados-local": "sensitive"})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.offered(), sorted(READ_PLAN))

    def test_a_later_turn_marked_sensitive_loses_delegation(self):
        FakeOllama.reply = [call("Read", {"file_path": "/Users/someone/code/proj/README.md"})], "tool_use"
        first = self.body(text=f"plan {uuid.uuid4().hex}")
        status, raw = self.raw(first)
        self.assertEqual(status, 200, raw)
        got = json.loads(raw)
        nxt = {**first, "messages": first["messages"] + [
            {"role": "assistant", "content": got["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": got["content"][0]["id"],
                                          "content": "notes"}]}]}
        FakeOllama.reply = [{"type": "text", "text": "done"}], "end_turn"
        status, raw = self.raw(nxt, headers={**self.HOOKED, "x-cortados-local": "sensitive"})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.offered(), sorted(READ_PLAN))
        (s,) = [x for x in self.reg.sessions.values() if isinstance(x, server.LocalSession)]
        self.assertEqual(s.route["bind_reason"], "route_pick_local")    # the conversation's own reason is unchanged

    def test_no_local_node_is_a_503_naming_the_node_never_cloud(self):
        from tests.test_boundary_hook import closed_port
        # the node's URL (write_config's base_url_env) now names a port nothing listens on
        with mock.patch.dict(os.environ, {"TEST_LOCAL_PRO_URL": f"http://127.0.0.1:{closed_port()}"}):
            status, raw = self.raw(self.body())
        self.assertEqual(status, 503, raw)
        msg = json.loads(raw)["error"]["message"]
        self.assertIn("qwen-27b on node pro", msg)
        self.assertIn("nothing ran on any other model, local or cloud", msg)
        self.assertEqual(self.selects, [])

    def test_the_pick_keeps_its_identity(self):
        # A local pick's route is pinned, but unlike a sub/ pin it is not in every window: a cloud pick with the same
        # history forks (routing runs again) instead of continuing the local conversation.
        FakeOllama.reply = [{"type": "text", "text": "plan"}], "end_turn"
        first = self.body(text=f"plan {uuid.uuid4().hex}")
        status, raw = self.raw(first)
        self.assertEqual(status, 200, raw)
        (s,) = [x for x in self.reg.sessions.values() if isinstance(x, server.LocalSession)]
        self.assertFalse(server._in_window(s, "R3"))
        self.assertTrue(server._in_window(s, "local:qwen-27b"))
        pin = type("S", (), {"route": {"pinned": True}})()
        self.assertTrue(server._in_window(pin, "R3"))                    # a sub/ pin: in every window, as before
        nxt = {**first, "model": "cortados@R3", "messages": first["messages"] + [
            {"role": "assistant", "content": json.loads(raw)["content"]}, {"role": "user", "content": "go on"}]}
        status, raw = self.raw(nxt)
        self.assertEqual(len(self.selects), 1, raw)                       # routed again, for the cloud window

    def test_subagents_stay_free_routed(self):
        # E7: a subagent of the local-orchestrated session sends plain `cortados`, which S1 routes as always.
        FakeOllama.reply = [{"type": "text", "text": "plan"}], "end_turn"
        self.assertEqual(self.raw(self.body(text=f"plan {uuid.uuid4().hex}"))[0], 200)
        self.raw(self.body(text=f"sub {uuid.uuid4().hex}", model="cortados"))
        self.assertEqual(len(self.selects), 1)
        self.assertNotIn("route_pick", self.selects[0].get("constraints") or {})


# ---------------------------------------------------------------------------------------------------- the launcher
LOCAL_TWO = {"band": "local", "errors": [], "local": {
    "status": "ok", "note": "n", "enabled": True,
    "models": [{"id": "qwen-27b", "tag": "qwen3.8-27b-heretic:q4", "node": "pro", "node_label": "Pro",
                "state": "available"},
               {"id": "qwen-35b", "tag": "qwen3.6-35b-a3b:q4", "node": "pc", "node_label": "PC",
                "state": "switch off"}]}}


class LauncherTests(RouteBase):

    def setUp(self):
        super().setUp()
        self.views["local"] = LOCAL_TWO

    def argv(self, *argv, answers=None):
        from tests.test_session_route import TTY
        out = TTY()
        args, env, cmd = session.prepare(["--settings-dir", self.sdir, "--provider-url", "http://r.example",
                                          "--token-file", self.tok] + list(argv), self.base,
                                         managed_dirs=(self.managed,), home=self.home, stdin=TTY(answers or ""),
                                         out=out, fetch=self.fetch)
        return args, env, cmd, out.getvalue()

    def test_route_local_launches_with_the_note_and_only_the_delegation_server(self):
        for flag, model in (("local", "cortados@local"), ("local:QWEN-27B", "cortados@local:qwen-27b"),
                            ("local:qwen3.6-35b-a3b:q4".replace(":q4", ""), None),
                            # review localorch3 P3: the typed order is the preference order, kept (not sorted)
                            ("local:qwen-35b+qwen-27b", "cortados@local:qwen-35b+qwen-27b"),
                            ("local:qwen-27b+QWEN-35B+qwen-27b", "cortados@local:qwen-27b+qwen-35b")):
            with self.subTest(flag=flag):
                if model is None:
                    with self.assertRaisesRegex(session.SessionError, "not a local model"):
                        self.argv("--route", flag)
                    continue
                args, env, cmd, _ = self.argv("--route", flag)
                self.assertEqual(cmd[cmd.index("--model") + 1], model)
                self.assertIn(session.LOCAL_PICK_NOTE, args.route_notes)
                self.assertIn("plan and delegate only", session.LOCAL_PICK_NOTE)
                self.assertIn("--strict-mcp-config", cmd)
                cfg_path = cmd[cmd.index("--mcp-config") + 1]
                self.assertEqual(oct(os.stat(cfg_path).st_mode & 0o777), "0o600")
                with open(cfg_path) as f:
                    cfg = json.load(f)
                (name, srv), = cfg["mcpServers"].items()
                self.assertEqual(name, "cortados-route")
                self.assertTrue(srv["args"][-1].endswith("cortados_router/subagent_mcp.py"))
                self.assertTrue(os.path.isabs(srv["command"]))
                self.assertEqual(srv["env"]["CORTADOS_PROVIDER_TOKEN_FILE"], os.path.abspath(self.tok))
                self.assertNotIn("SECRET", json.dumps(cfg))
                from tests.test_session_route import SECRET
                self.assertNotIn(SECRET, json.dumps(cfg))                 # the token file's path, never the token
                self.assertEqual(env.get("ENABLE_TOOL_SEARCH"), "false")  # delegation tools declared up front

    def hook_entry(self, cmd):
        with open(cmd[cmd.index("--settings") + 1]) as f:
            return json.load(f)["hooks"]["PreToolUse"][0]

    def test_the_generated_hook_per_case(self):
        # review localorch3: matcher `*` and the watcher prefix only for a local pick; cloud picks, the default
        # orchestrator and child tasks keep the file-writer + Bash matcher, without a watcher prefix
        import shlex
        from cortados_router import child_tasks
        for env in ({}, {"CORTADOS_TASKS_DIR": "/opt/t b", "CORTADOS_EXEC_HOSTS_FILE": "/opt/h.json"}):
            with self.subTest(env=env):
                self.base = {k: v for k, v in self.base.items() if k not in child_tasks.WATCH_ENV_VARS}
                self.base.update(env)
                _a, _e, cmd, _ = self.argv("--route", "local:qwen-27b")
                entry = self.hook_entry(cmd)
                self.assertEqual(entry["matcher"], "*")
                full = entry["hooks"][0]["command"]
                argv = shlex.split(full[len(session.BOUNDARY_HOOK_GUARD_HEAD):-len(session.BOUNDARY_HOOK_GUARD_TAIL)])
                prefix = hook.watch_prefix_from(argv)
                # the prefix is what the configured delegation server's start_task will hand back
                with open(cmd[cmd.index("--mcp-config") + 1]) as f:
                    srv = json.load(f)["mcpServers"]["cortados-route"]
                self.assertEqual(srv["command"], os.path.realpath(__import__("sys").executable))
                self.assertEqual({k: srv["env"][k] for k in child_tasks.WATCH_ENV_VARS if k in srv["env"]}, env)
                with mock.patch.dict(os.environ, {k: env.get(k, "") for k in child_tasks.WATCH_ENV_VARS}):
                    self.assertEqual(prefix, child_tasks.watch_prefix())
                    self.assertTrue(hook.is_watch_command(child_tasks.watch_command(TID), prefix))
                self.assertEqual(os.path.dirname(shlex.split(prefix)[-1]), os.path.dirname(srv["args"][-1]))
        for argv in (("--route", "R3"), ("--orchestrator", "cortados", "--orchestrator-band", "any",
                                         "--run-id", TID, "--no-delegate-directive", "--no-gate"), ()):
            with self.subTest(argv=argv):
                _a, _e, cmd, _ = self.argv(*argv, answers="6\n")
                entry = self.hook_entry(cmd)
                self.assertEqual(entry["matcher"], "Write|Edit|MultiEdit|NotebookEdit|Bash")
                self.assertNotIn("--watch-prefix", entry["hooks"][0]["command"])

    def test_the_owners_own_mcp_set_is_kept(self):
        _a, env, cmd, _ = self.argv("--route", "local", "--", "--strict-mcp-config", "--mcp-config", "/x.json")
        self.assertEqual(cmd.count("--strict-mcp-config"), 1)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "/x.json")

    def test_a_cloud_route_adds_no_mcp_config(self):
        _a, env, cmd, _ = self.argv("--route", "R3")
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("ENABLE_TOOL_SEARCH", env)

    def test_the_menu_picks_local_models(self):
        _a, _e, cmd, out = self.argv(answers="5\n1\n2\n\n")
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:qwen-27b+qwen-35b")
        self.assertIn("  [ ] 1 qwen-27b on Pro: available", out)
        self.assertIn("  [ ] 2 qwen-35b on PC: switch off", out)
        self.assertIn(session.LOCAL_PICK_NOTE, out)
        self.assertEqual(session.load_pick(self.home), "local:qwen-27b+qwen-35b")
        # review localorch3 P3: the order the models are selected in is the preference order
        os.remove(session.pick_file(self.home))                          # nothing pre-selected
        _a, _e, cmd, out = self.argv(answers="5\n2\n1\n\n")
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:qwen-35b+qwen-27b")
        self.assertEqual(session.load_pick(self.home), "local:qwen-35b+qwen-27b")
        _a, _e, cmd, out = self.argv(answers="5\n\n")                 # the saved order comes back pre-selected
        self.assertIn("  [x] 2 qwen-35b on PC: switch off", out)
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:qwen-35b+qwen-27b")
        _a, _e, cmd, out = self.argv(answers="5\na\n")
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local")
        _a, _e, cmd, out = self.argv(answers="5\n\nb\n6\n")
        self.assertIn("select at least one model, or a / b", out)
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados")

    def test_the_menu_reprompts_an_oversized_local_pick(self):
        # review localorch4 P2: the local menu checks the pick like the cloud menu (check_route) and asks again,
        # never returning a pick the provider refuses (it used to crash launch with an uncaught PickError)
        nine = [{"id": f"m{i}", "tag": f"m{i}:q4", "node": "pro", "node_label": "Pro", "state": "available"}
                for i in range(1, 10)]
        self.views["local"] = {"local": {"status": "ok", "models": nine}}
        _a, _e, cmd, out = self.argv(answers="5\n1 2 3 4 5 6 7 8 9\n\n9\n\n")
        self.assertIn("route pick: a route pick names at most 8 models (9 given)", out)
        self.assertIn("deselect some models, or a / b", out)
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:m1+m2+m3+m4+m5+m6+m7+m8")
        long_ids = ["a" * 64, "b" * 64]
        self.views["local"] = {"local": {"status": "ok", "models": [
            {"id": i, "tag": i, "node": "pro", "node_label": "Pro", "state": "available"} for i in long_ids]}}
        os.remove(session.pick_file(self.home))
        _a, _e, cmd, out = self.argv(answers="5\n1 2\n\n2\n\n")
        self.assertIn("deselect some models, or a / b", out)
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:" + "a" * 64)

    def test_route_local_oversized_exits_2(self):
        from tests.test_session_route import TTY
        self.views["local"] = {"local": {"status": "ok", "models": [
            {"id": i, "tag": i, "node": "pro", "node_label": "Pro", "state": "available"} for i in ("a" * 64, "b" * 64)]}}
        for route in ("local:" + "a" * 64 + "+" + "b" * 64, "local:" + "+".join(f"m{i}" for i in range(1, 10))):
            with self.subTest(route=route[:20]):
                err = io.StringIO()
                with mock.patch.object(os, "execvpe") as ex:
                    rc = session.main(["--settings-dir", self.sdir, "--provider-url", "http://r.example",
                                       "--token-file", self.tok, "--route", route], base_env=self.base, out=TTY(),
                                      err=err, managed_dirs=(self.managed,), home=self.home, stdin=TTY(""),
                                      fetch=self.fetch)
                self.assertEqual(rc, 2, err.getvalue())
                self.assertIn("cortados session:", err.getvalue())
                ex.assert_not_called()

    def test_the_menu_offline_takes_typed_names(self):
        self.offline = True
        _a, _e, cmd, _ = self.argv(answers="5\nqwen-27b\n")
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:qwen-27b")

    def test_route_last_keeps_a_saved_local_pick(self):
        session.save_pick("local:qwen-27b", self.home)
        _a, _e, cmd, _ = self.argv("--route", "last")
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados@local:qwen-27b")
        self.assertIn("--strict-mcp-config", cmd)

    def test_child_tasks_stay_free_routed(self):
        # E7: a child task's launcher args (child_tasks._prepare) give it a free-routed main loop and no menu,
        # whatever pick the owner saved for his own sessions.
        session.save_pick("local:qwen-27b", self.home)
        args, env, cmd, out = self.argv("--orchestrator", "cortados", "--orchestrator-band", "any",
                                        "--run-id", "t20261125060814-cecc4399", "--no-delegate-directive", "--no-gate")
        self.assertEqual(cmd[cmd.index("--model") + 1], "cortados")
        self.assertNotIn("--mcp-config", cmd)
        self.assertEqual((out, self.fetched), ("", []))
        from cortados_router import child_tasks
        src = open(child_tasks.__file__, encoding="utf-8").read()
        self.assertIn('launch = ["--orchestrator", orchestrator, "--orchestrator-band", "any"', src)


if __name__ == "__main__":
    unittest.main()
