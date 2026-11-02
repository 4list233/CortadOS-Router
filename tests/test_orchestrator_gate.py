import concurrent.futures
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from cortados_router import orchestrator_gate as gate


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        self.directory = self.home / ".config" / "cortados" / "gate"
        self.event = {"session_id": "session", "prompt_id": "prompt", "tool_name": "Bash",
                      "tool_input": {"command": "true"}, "hook_event_name": "PreToolUse"}

    def call(self, event=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        rc = gate.main(io.StringIO(json.dumps(self.event if event is None else event)),
                       out, err, {} if env is None else env, self.directory)
        # Fail-open exits 1 so Claude Code shows the stderr line yet still allows the tool; normal decisions exit 0.
        self.assertEqual(rc, 1 if err.getvalue() else 0)
        return out.getvalue(), err.getvalue()

    def test_subagent_exempt_even_with_empty_agent_id(self):
        for agent_id in ("child", ""):
            self.assertEqual(self.call(dict(self.event, agent_id=agent_id),
                                       {"CORTADOS_DELEGATE_BUDGET": "0"}), ("", ""))
        self.assertFalse(self.directory.exists())

    def test_planning_unlimited(self):
        for tool in gate.PLANNING_TOOLS:
            for _ in range(5):
                self.assertEqual(self.call(dict(self.event, tool_name=tool),
                                           {"CORTADOS_DELEGATE_BUDGET": "0"}), ("", ""))
        self.assertFalse(self.directory.exists())

    def test_fork_denied_in_main_loop_without_budget(self):
        for tool in ("Agent", "Task"):
            for kind in ("fork", " Fork ", "FORK", "f_ork", "f-ork", "f ork", "F.o.r.k", "f2ork"):
                out, err = self.call(dict(self.event, tool_name=tool,
                                          tool_input={"subagent_type": kind, "prompt": "x"}))
                self.assertEqual(err, "")
                self.assertEqual(json.loads(out), {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": gate.FORK_DENY_REASON}})
        self.assertFalse(self.directory.exists())

    def test_non_ascii_agent_type_denied_without_budget(self):
        for kind in ("f\u2014ork", "f\u2e5dork", "\U0001CCDBork", "\uff46\uff4f\uff52\uff4b", "g\u00e9n\u00e9ral"):
            for tool in ("Agent", "Task"):
                out, err = self.call(dict(self.event, tool_name=tool, tool_input={"subagent_type": kind}),
                                     {"CORTADOS_DELEGATE_BUDGET": "0"})
                self.assertEqual(err, "")
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"],
                                 gate.NON_ASCII_DENY_REASON)
        self.assertFalse(self.directory.exists())

    def test_fork_allowed_inside_subagent_and_other_agents_unbudgeted(self):
        fork = dict(self.event, tool_name="Agent", tool_input={"subagent_type": "fork"})
        self.assertEqual(self.call(dict(fork, agent_id="child")), ("", ""))
        for tool_input in ({"subagent_type": "general-purpose"}, {"subagent_type": "forked"}, {"prompt": "x"}, "not a dict", None):
            for _ in range(5):
                self.assertEqual(self.call(dict(self.event, tool_name="Agent", tool_input=tool_input),
                                           {"CORTADOS_DELEGATE_BUDGET": "0"}), ("", ""))
        self.assertFalse(self.directory.exists())

    def test_budget_then_exact_deny(self):
        for _ in range(3):
            self.assertEqual(self.call(), ("", ""))
        out, err = self.call()
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "orchestrator: you have used your 3 direct work calls for this prompt; "
            "delegate this to a subagent with the Agent tool (subagents have every tool, MCP included)"}})
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        path = gate.state_path(self.directory, "session")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {"prompt": 3})

    def test_prompt_and_session_budgets_independent(self):
        env = {"CORTADOS_DELEGATE_BUDGET": "1"}
        self.assertEqual(self.call(env=env), ("", ""))
        self.assertTrue(self.call(env=env)[0])
        self.assertEqual(self.call(dict(self.event, prompt_id="next"), env), ("", ""))
        self.assertEqual(self.call(dict(self.event, session_id="next"), env), ("", ""))
        missing = dict(self.event)
        del missing["prompt_id"]
        self.assertEqual(self.call(missing, env), ("", ""))
        self.assertTrue(self.call(missing, env)[0])
        self.assertEqual(json.loads(gate.state_path(self.directory, "session").read_text())["none"], 1)

    def test_zero_budget_denies_all_work_including_unknown(self):
        for tool in ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
                     "mcp__server__tool", "FutureTool"):
            out, err = self.call(dict(self.event, tool_name=tool), {"CORTADOS_DELEGATE_BUDGET": "0"})
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(err, "")

    def test_unknown_tool_consumes_budget(self):
        self.assertEqual(self.call(dict(self.event, tool_name="FutureTool"),
                                   {"CORTADOS_DELEGATE_BUDGET": "1"}), ("", ""))
        self.assertTrue(self.call(env={"CORTADOS_DELEGATE_BUDGET": "1"})[0])

    def test_mcp_allowlist_is_exact_and_mcp_only(self):
        env = {"CORTADOS_DELEGATE_BUDGET": "0",
               "CORTADOS_DELEGATE_ALLOW_MCP": " mcp__server__read ,Bash,mcp__other__*"}
        self.assertEqual(self.call(dict(self.event, tool_name="mcp__server__read"), env), ("", ""))
        for tool in ("Bash", "mcp__server__write", "mcp__other__read"):
            self.assertTrue(self.call(dict(self.event, tool_name=tool), env)[0])

    def test_corrupt_state_fails_open_visibly_without_overwrite(self):
        self.call()
        path = gate.state_path(self.directory, "session")
        path.write_text("{broken")
        out, err = self.call()
        self.assertEqual(out, "")
        self.assertIn("fail open", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(path.read_text(), "{broken")

    def test_invalid_input_budget_and_state_fail_open(self):
        for budget in ("-1", "invalid"):
            out, err = self.call(env={"CORTADOS_DELEGATE_BUDGET": budget})
            self.assertEqual(out, "")
            self.assertIn("fail open", err)
        self.call()
        path = gate.state_path(self.directory, "session")
        for value in ([], {"prompt": -1}, {"prompt": True}, {"prompt": "2"}):
            path.write_text(json.dumps(value))
            out, err = self.call()
            self.assertEqual(out, "")
            self.assertIn("fail open", err)

    def test_prune_only_old_regular_json_files(self):
        self.call()
        old = self.directory / "old.json"
        old.write_text("{}")
        stale = time.time() - gate.PRUNE_AGE - 60
        os.utime(old, (stale, stale))
        other = self.directory / "keep.txt"
        other.write_text("keep")
        os.utime(other, (stale, stale))
        self.call()
        self.assertFalse(old.exists())
        self.assertTrue(other.exists())

    def test_symlink_and_loose_directory_fail_open(self):
        target = self.home / "target"
        target.mkdir(mode=0o700)
        self.directory.parent.mkdir(parents=True)
        self.directory.symlink_to(target, target_is_directory=True)
        out, err = self.call()
        self.assertEqual(out, "")
        self.assertIn("fail open", err)
        self.assertEqual(list(target.iterdir()), [])
        self.directory.unlink()
        self.directory.mkdir(mode=0o755)
        self.assertIn("fail open", self.call()[1])

    def test_absolute_script_unknown_cwd_and_parallel_budget(self):
        env = dict(os.environ, HOME=str(self.home), CORTADOS_DELEGATE_BUDGET="3")
        script = str(Path(gate.__file__).resolve())

        def invoke(_):
            return subprocess.run([sys.executable, "-B", script], input=json.dumps(self.event),
                                  text=True, capture_output=True, cwd=str(self.home), env=env, timeout=10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(invoke, range(12)))
        self.assertTrue(all(r.returncode == 0 and not r.stderr for r in results))
        self.assertEqual(sum(not r.stdout for r in results), 3)
        self.assertEqual(sum(bool(r.stdout) for r in results), 9)


if __name__ == "__main__":
    unittest.main()
