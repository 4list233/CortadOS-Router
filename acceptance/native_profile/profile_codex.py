#!/usr/bin/env python3
# PURPOSE: Add observed-version sequencing to the existing protected native double.
# CONTRACT: RT-MCP-NATIVE-002 exam only; synthetic version changes are fault inputs.
# DEPENDS: unchanged mcp_worker/fake_codex.py for all metadata/execution mechanics.
import json
import os
from pathlib import Path
import runpy
import sys

if "--version" in sys.argv:
    home = Path(os.environ["CODEX_HOME"])
    control = json.loads((home / "fixture-control.json").read_text())
    observations = home / "fixture-version-checks.jsonl"
    previous = observations.read_text().splitlines() if observations.exists() else []
    versions = control.get("version_sequence")
    version = (versions[min(len(previous), len(versions) - 1)] if versions
               else control.get("version", "0.162.1"))
    with observations.open("a") as stream:
        stream.write(json.dumps({"version": version, "pid": os.getpid()}) + "\n")
    print("codex-cli " + version)
else:
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "mcp_worker/fake_codex.py"), run_name="__main__")
