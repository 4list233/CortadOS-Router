import concurrent.futures
import io
import json
import os
from pathlib import Path
import socket
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

    def call(self, event=None, env=None, probe=None):
        out, err = io.StringIO(), io.StringIO()
        # Default probe: any real probe in these tests is a test failure (no network, no provider is ever opened).
        rc = gate.main(io.StringIO(json.dumps(self.event if event is None else event)),
                       out, err, {} if env is None else env, self.directory,
                       probe or (lambda url: self.fail(f"unexpected provider probe of {url}")))
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

    def test_agent_and_task_denied_in_main_loop_without_spending_budget(self):
        """The owner's rule: the top-level session never delegates with the Agent tool. Denied whatever the
        subagent type or input shape, with a reason that names start_task and the no-router fallback."""
        for tool in ("Agent", "Task"):
            for tool_input in ({"subagent_type": "general-purpose", "prompt": "x"}, {"subagent_type": "fork"},
                               {"prompt": "x"}, "not a dict", None):
                out, err = self.call(dict(self.event, tool_name=tool, tool_input=tool_input),
                                     {"CORTADOS_DELEGATE_BUDGET": "3"})
                self.assertEqual(err, "")
                self.assertEqual(json.loads(out), {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": gate.AGENT_DENY_REASON}})
        self.assertIn("mcp__cortados-route__start_task", gate.AGENT_DENY_REASON)
        self.assertIn("tell the owner in one line and do the work directly", gate.AGENT_DENY_REASON)
        self.assertFalse(self.directory.exists(), "a refused Agent call spends no budget")
        self.assertEqual(self.call(), ("", ""), "the budget is intact after the refusals")

    def test_agent_allowed_inside_subagent(self):
        for tool_input in ({"subagent_type": "fork"}, {"subagent_type": "general-purpose"}):
            self.assertEqual(self.call(dict(self.event, tool_name="Agent", tool_input=tool_input, agent_id="child"),
                                       {"CORTADOS_DELEGATE_BUDGET": "0"}), ("", ""))
        self.assertFalse(self.directory.exists())

    def test_cortados_route_tools_unlimited_and_exact_prefix(self):
        for tool in ("mcp__cortados-route__start_task", "mcp__cortados-route__route_task",
                     "mcp__cortados-route__wait_task", "mcp__cortados-route__task_status",
                     "mcp__cortados-route__list_tasks", "mcp__cortados-route__cancel_task"):
            for _ in range(5):
                self.assertEqual(self.call(dict(self.event, tool_name=tool),
                                           {"CORTADOS_DELEGATE_BUDGET": "0"}), ("", ""))
        self.assertFalse(self.directory.exists())
        # Another server's start_task (a workspace connector) is ordinary work and spends the budget.
        for tool in ("mcp__velox-workspace__start_task", "mcp__cortados-routex__start_task", "mcp__cortados__route_task"):
            out, err = self.call(dict(self.event, tool_name=tool), {"CORTADOS_DELEGATE_BUDGET": "0"})
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny", tool)

    def test_budget_then_exact_deny(self):
        for _ in range(3):
            self.assertEqual(self.call(), ("", ""))
        out, err = self.call()
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": gate.budget_deny_reason(3)}})
        reason = gate.budget_deny_reason(3)
        self.assertIn("used your 3 direct work calls", reason)
        self.assertIn("mcp__cortados-route__start_task", reason)
        self.assertNotIn("Agent tool", reason)
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
        for key in ("CORTADOS_PROVIDER_URL", "ANTHROPIC_BASE_URL"):     # no provider target: no real probe
            env.pop(key, None)
        script = str(Path(gate.__file__).resolve())

        def invoke(_):
            return subprocess.run([sys.executable, "-B", script], input=json.dumps(self.event),
                                  text=True, capture_output=True, cwd=str(self.home), env=env, timeout=10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(invoke, range(12)))
        self.assertTrue(all(r.returncode == 0 and not r.stderr for r in results))
        self.assertEqual(sum(not r.stdout for r in results), 3)
        self.assertEqual(sum(bool(r.stdout) for r in results), 9)

    def test_router_outage_releases_budget_but_agent_stays_refused(self):
        """Regression (review P2): with the budget spent and cortados-route unavailable, the session could neither
        delegate nor work directly. While the provider is unreachable the gate now allows direct work past the budget,
        visibly (stderr, exit 1), without counting it; Agent/Task stay refused; recovery restores the denial."""
        env = {"CORTADOS_DELEGATE_BUDGET": "3", "ANTHROPIC_BASE_URL": "http://user:secret@provider.example:8432/x"}
        probed = []
        down = lambda url: probed.append(url) or "cannot connect to provider.example:8432 (ConnectionRefusedError)"
        for _ in range(3):
            self.assertEqual(self.call(env=env, probe=down), ("", ""))
        self.assertEqual(probed, [], "no probe while the budget lasts")
        for _ in range(2):
            out, err = self.call(env=env, probe=down)
            self.assertEqual(out, "")
            self.assertIn("provider http://provider.example:8432 unreachable", err)
            self.assertIn("Agent stays refused", err)
            self.assertNotIn("secret", err)
            self.assertNotIn("fail open", err)
        self.assertEqual(len(probed), 2)
        self.assertEqual(json.loads(gate.state_path(self.directory, "session").read_text()), {"prompt": 3},
                         "outage calls do not spend the budget")
        for tool in ("Agent", "Task"):
            out, err = self.call(dict(self.event, tool_name=tool, tool_input={"prompt": "x"}), env, down)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"],
                             gate.AGENT_DENY_REASON)
            self.assertEqual(err, "")
        self.assertEqual(len(probed), 2, "an Agent refusal never probes")
        # Provider back: the budget denial returns, and its reason explains the recovery path.
        out, err = self.call(env=env, probe=lambda url: None)
        self.assertEqual(err, "")
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason, gate.budget_deny_reason(3, "http://provider.example:8432"))
        self.assertIn("unreachable this gate releases the budget", reason)
        self.assertIn("Agent and Task stay refused", reason)
        self.assertIn("--no-gate", reason)
        self.assertNotIn("secret", reason)

    def test_cortados_provider_url_preferred_and_no_target_no_release(self):
        seen = []
        env = {"CORTADOS_DELEGATE_BUDGET": "0", "CORTADOS_PROVIDER_URL": "https://route.example",
               "ANTHROPIC_BASE_URL": "http://other.example:1"}
        self.assertIn("unreachable", self.call(env=env, probe=lambda url: seen.append(url) or "down")[1])
        self.assertEqual(seen, ["https://route.example"])
        # No provider configured at all: nothing to check, so the budget holds (no silent release).
        out, err = self.call(env={"CORTADOS_DELEGATE_BUDGET": "0"})
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_slow_resolver_deadline_keeps_budget_and_does_not_hold_hook_open(self):
        # No real host: the resolver stays blocked beyond hook exit. This also catches
        # non-daemon workers that hold Python shutdown open.
        script = """
import io
import json
import socket
import sys
import time
from cortados_router import orchestrator_gate as gate

def slow_resolver(*args, **kwargs):
    time.sleep(10)
    raise socket.gaierror("fake resolver stalled")

socket.getaddrinfo = slow_resolver
event = {"session_id": "deadline", "prompt_id": "p", "tool_name": "Bash"}
env = {"CORTADOS_DELEGATE_BUDGET": "0", "CORTADOS_PROVIDER_URL": "http://fake.invalid"}
start = time.monotonic()
rc = gate.main(io.StringIO(json.dumps(event)), env=env, directory=sys.argv[1],
               probe=lambda url: gate.provider_unreachable(url, timeout=0.1))
assert time.monotonic() - start < 1.0, "probe exceeded wall-clock deadline"
sys.exit(rc)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.directory)],
            cwd=str(Path(gate.__file__).resolve().parent.parent),
            text=True, capture_output=True, timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_real_probe_against_local_sockets(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            self.assertIsNone(gate.provider_unreachable(f"http://127.0.0.1:{port}"))
        finally:
            listener.close()
        self.assertIn("cannot connect to 127.0.0.1", gate.provider_unreachable(f"http://127.0.0.1:{port}", 1.0))
        for url in ("not a url", "ftp://host", "http://", "http://h:99999"):
            self.assertEqual(gate.provider_unreachable(url), "provider URL is malformed", url)


if __name__ == "__main__":
    unittest.main()
