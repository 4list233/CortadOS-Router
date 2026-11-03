"""cursor_isolation: canned stream-json events modeled on the C0/C1 transcripts in docs/CURSOR-LANE.md."""
import json
import os
import shutil
import stat
import unittest

from cortados_router.provider import cursor_isolation as ci

W = "/tmp/ws"


def ev(kind, args, result=None, subtype="completed"):
    body = {"args": args}
    if result is not None:
        body["result"] = result
    return {"type": "tool_call", "subtype": subtype, "call_id": "t1",
            "tool_call": {kind: body, "hookAdditionalContexts": [], "toolCallId": "t1"}}


class CheckEventTest(unittest.TestCase):
    def test_denied_shell_ok(self):
        ci.check_event(ev("shellToolCall", {"command": "ls"}, {"permissionDenied": {"command": "ls"}}))

    def test_denied_read_ok(self):
        ci.check_event(ev("readToolCall", {"path": W + "/c"}, {"error": {"errorMessage": "Permission denied"}}))

    def test_executed_glob_hard_fails(self):
        with self.assertRaisesRegex(ci.CursorIsolationError, "globToolCall"):
            ci.check_event(ev("globToolCall", {"targetDirectory": W, "globPattern": "*"},
                              {"success": {"files": [], "totalFiles": 0}}))

    def test_executed_read_hard_fails(self):
        with self.assertRaisesRegex(ci.CursorIsolationError, "readToolCall"):
            ci.check_event(ev("readToolCall", {"path": W + "/c"}, {"success": {"content": "x"}}))

    def test_other_error_is_not_a_denial(self):
        with self.assertRaises(ci.CursorIsolationError):
            ci.check_event(ev("readToolCall", {"path": W + "/c"}, {"error": {"errorMessage": "ENOENT"}}))

    def test_mcp_call_to_shim_ok(self):
        ci.check_event(ev("mcpToolCall", {"name": "cortados-echo_marker", "args": {"text": "c1"}},
                          {"success": {"content": [{"text": {"text": "MARKER:c1"}}]}}))

    def test_mcp_call_to_other_server_hard_fails(self):
        with self.assertRaises(ci.CursorIsolationError):
            ci.check_event(ev("mcpToolCall", {"name": "github-create_issue", "args": {}}, {"success": {}}))

    def test_schema_lookup_for_shim_ok(self):
        ci.check_event(ev("getMcpToolsToolCall", {"server": "cortados", "toolName": "x"}, {"success": {}}))
        with self.assertRaises(ci.CursorIsolationError):
            ci.check_event(ev("getMcpToolsToolCall", {"server": "other", "toolName": "x"}, {"success": {}}))

    def test_unknown_tool_kind_hard_fails(self):
        with self.assertRaisesRegex(ci.CursorIsolationError, "webFetchToolCall"):
            ci.check_event(ev("webFetchToolCall", {"url": "http://x"}, {"success": {}}))

    def test_tool_like_event_of_unknown_shape_fails_closed(self):
        with self.assertRaises(ci.CursorIsolationError):
            ci.check_event({"type": "tool_use", "tool": "shell"})
        with self.assertRaises(ci.CursorIsolationError):
            ci.check_event({"type": "tool_call", "subtype": "completed", "tool_call": {"hookAdditionalContexts": []}})

    def test_started_event_waits_for_completion(self):
        ci.check_event(ev("shellToolCall", {"command": "ls"}, subtype="started"))

    def test_non_tool_events_pass(self):
        ci.check_event({"type": "system", "subtype": "init", "cwd": W, "model": "Composer 2.5"})
        ci.check_event({"type": "result", "subtype": "success", "result": "ok"})


class WorkspaceTest(unittest.TestCase):
    def test_workspace_and_argv(self):
        ws = ci.build_workspace(["python3", "-I", "/x/shim.py"])
        try:
            self.assertEqual(stat.S_IMODE(os.stat(ws).st_mode), 0o700)
            self.assertEqual(sorted(os.listdir(ws)), [".cursor"])
            cli = json.load(open(os.path.join(ws, ".cursor", "cli.json")))
            self.assertEqual(cli["permissions"]["allow"], ["Mcp(cortados:*)"])
            self.assertIn("Shell(*)", cli["permissions"]["deny"])
            mcp = json.load(open(os.path.join(ws, ".cursor", "mcp.json")))
            self.assertEqual(list(mcp["mcpServers"]), ["cortados"])
            argv = ci.build_argv("cursor-agent", "composer-2.5", ws, "hi")
            self.assertEqual(argv[argv.index("--sandbox") + 1], "enabled")
            self.assertIn("--approve-mcps", argv)
        finally:
            shutil.rmtree(ws)

    def test_forbidden_flags(self):
        for bad in (["-p", "--sandbox", "enabled", "--force"], ["-p", "--sandbox", "disabled"], ["-p", "--yolo"]):
            with self.assertRaises(ci.CursorIsolationError):
                ci.check_command_line(bad)


if __name__ == "__main__":
    unittest.main()
