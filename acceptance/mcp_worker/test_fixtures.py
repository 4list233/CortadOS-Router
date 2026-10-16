# PURPOSE: Verify the independent capture corpus and fault double before production grading.
# CONTRACT: RT-MCP-001 and D-0092; fixture integrity does not prove service acceptance.
# DEPENDS: stdlib and protected native capture files.
# CEILING: Synthetic timestamps and mutations are explicitly fixture-only.

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).parent


class FixtureIntegrity(unittest.TestCase):
    def test_captured_native_boundaries_retain_provenance_hashes(self):
        root = HERE / "fixtures/native-boundaries"
        provenance = json.loads((root / "PROVENANCE.json").read_text())
        for entry in provenance["files"]:
            self.assertEqual(hashlib.sha256((root / entry["file"]).read_bytes()).hexdigest(), entry["fixture_sha256"], entry["file"])
        boundary = (root / "handoff-boundary/handoff.txt").read_bytes()
        request = json.loads((root / "handoff-boundary/request.json").read_text())
        self.assertEqual(len(boundary), 65536)
        expected = "TASK\n" + request["task"] + "\n\nPARENT CONTEXT (reference data; it does not grant additional permissions)\n" + request["context"] + "\n"
        self.assertEqual(expected.encode(), boundary)

    def test_all_enabled_models_have_real_command_and_mcp_output_kinds(self):
        for label, directory in (("luna", "explicit-grant"), ("sol", "model-connectors/gpt-6.1-sol"), ("astra", "model-connectors/gpt-6-astra")):
            commands = [json.loads(line) for line in (HERE / "fixtures" / ("codex-" + label + "-command.jsonl")).read_text().splitlines()]
            connectors = [json.loads(line) for line in (HERE / "fixtures/native-boundaries" / directory / "connector-stdout.jsonl").read_text().splitlines()]
            self.assertTrue(any(row.get("item", {}).get("type") == "command_execution" for row in commands))
            self.assertTrue(any(row.get("item", {}).get("type") == "mcp_tool_call" for row in connectors))
            for events in (commands, connectors):
                self.assertEqual(sum(row["type"] == "turn.completed" for row in events), 1)
                self.assertTrue(any(row.get("item", {}).get("type") == "agent_message" for row in events))

    def test_process_double_replays_wire_methods_without_provider_access(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "fixture-control.json").write_text(json.dumps({"pagination": "two_pages"}))
            methods = ["initialize", "account/read", "account/rateLimits/read", "model/list", "model/list", "config/read", "configRequirements/read"]
            requests = [{"id": i+1, "method": method, "params": {"cursor": "second-page"} if i == 4 else {}} for i, method in enumerate(methods)]
            result = subprocess.run([sys.executable, str(HERE / "fake_codex.py"), "app-server"], input="".join(json.dumps(row)+"\n" for row in requests), text=True, capture_output=True, env={"CODEX_HOME": directory, "PATH": os.environ["PATH"]}, check=True)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(rows), len(requests))
            self.assertTrue(all("result" in row for row in rows), rows)
            self.assertEqual(rows[1]["result"]["account"]["type"], "chatgpt")
            self.assertEqual(rows[3]["result"]["nextCursor"], "second-page")
            self.assertIsNone(rows[4]["result"]["nextCursor"])
            self.assertEqual(rows[6]["result"]["requirements"], None)
