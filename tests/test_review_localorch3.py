"""Independent review negatives (gpt-6.1-sol, review localorch3, 2026-11-25), adopted by fix round 1: never execute the
proposed shell strings. Adapted: the client's check is boundary_hook.is_watch_command against the launcher's exact
prefix (WATCH_COMMAND_RE is gone); the accepted exact forms are in tests/test_local_orchestrator.py."""
import unittest
from unittest import mock
from cortados_router import boundary_hook as hook, session
from cortados_router.provider import effect_boundary as eb

TASK = "t20261125060814-cecc4399"
WATCH = "/usr/bin/python3 -B /trusted/cortados_router/child_tasks.py --watch " + TASK

class WatcherReviewTests(unittest.TestCase):
    def test_router_tool_names_are_exact_on_both_sides(self):
        boundary = eb.Boundary(eb.PICK_LOCAL_REASON, "/review-project", hook=True)
        for name in ("mcp__Cortados-route__start_task", "mcp__cortados-route__START_TASK",
                     "mcp__cortados-route-extra__start_task", "mcp__other__start_task",
                     "mcp__bridge__mcp__cortados-route__start_task"):
            with self.subTest(tool=name):
                self.assertEqual(boundary.blocked_call(name, {"task": "x"}), "tool-not-allowed")
                answer = {"known": True, "tool": name, "profile": eb.ORCHESTRATOR,
                          "tools": sorted(eb.PROFILE_TOOLS[eb.ORCHESTRATOR])}
                with mock.patch.object(hook, "ask", return_value=answer), self.assertRaises(hook.Denied):
                    hook.decide({"tool_name": name, "tool_use_id": "toolu_cxb_review", "tool_input": {}})

    def test_launcher_preserves_local_preference_order(self):
        self.assertEqual(session.canonical_route("local:qwen-35b+qwen-27b",
                         local=[("qwen-35b", "tag35"), ("qwen-27b", "tag27")]),
                         "local:qwen-35b+qwen-27b")

    def test_reject_control_of_a_non_watcher_shell(self):
        boundary = eb.Boundary(eb.PICK_LOCAL_REASON, "/review-project", hook=True)
        for name in ("KillShell", "KillBash", "BashOutput"):
            inp = {"shell_id": "unrelated-cloud-job", "bash_id": "unrelated-cloud-job"}
            with self.subTest(side="provider", tool=name):
                self.assertIsNotNone(boundary.blocked_call(name, inp))
            answer = {"known": True, "tool": name, "profile": eb.ORCHESTRATOR,
                      "tools": sorted(eb.PROFILE_TOOLS[eb.ORCHESTRATOR])}
            with self.subTest(side="client", tool=name), mock.patch.object(hook, "ask", return_value=answer):
                with self.assertRaises(hook.Denied):
                    hook.decide({"tool_name": name, "tool_use_id": "toolu_cxb_review", "tool_input": inp})

    def test_reject_shell_syntax_and_bad_ids(self):
        for command in (WATCH + "; id", WATCH + " && id", WATCH + " | sh",
                        WATCH + " > /tmp/out", WATCH + "\nid",
                        WATCH.replace(TASK, "$(id)"), WATCH + " --unknown 1",
                        WATCH.replace(TASK, TASK + "x"), WATCH.replace(TASK, TASK.upper())):
            with self.subTest(command=command):
                self.assertFalse(eb.is_watch_command(command))
                self.assertFalse(hook.is_watch_command(command, WATCH.split(" --watch")[0]))

    def test_reject_substituted_executable_and_script_on_both_sides(self):
        boundary = eb.Boundary(eb.PICK_LOCAL_REASON, "/review-project", hook=True)
        answer = {"known": True, "tool": "Bash", "profile": eb.ORCHESTRATOR,
                  "tools": sorted(eb.PROFILE_TOOLS[eb.ORCHESTRATOR]), "bash": False,
                  "bash_watch": True, "project": "/review-project"}
        commands = (
            WATCH.replace("/usr/bin/python3", "/tmp/untrusted/python3"),
            WATCH.replace("/trusted/cortados_router", "/tmp/untrusted/cortados_router"),
            WATCH.replace("/usr/bin/python3", "python3"),
            "env CORTADOS_EXEC_HOSTS_FILE=/tmp/untrusted-hosts.json " + WATCH,
        )
        for command in commands:
            with self.subTest(side="provider", command=command):
                self.assertIsNotNone(boundary.blocked_call("Bash", {"command": command}))
            with self.subTest(side="client", command=command):
                with mock.patch.object(hook, "ask", return_value=answer):
                    with self.assertRaises(hook.Denied):
                        hook.decide({"tool_name": "Bash", "tool_use_id": "toolu_cxb_review",
                                     "tool_input": {"command": command}})

if __name__ == "__main__":
    unittest.main()
