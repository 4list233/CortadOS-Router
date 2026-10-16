# PURPOSE: Measure reference storage/native costs and candidate MCP overhead without provider calls.
# CONTRACT: RT-MCP-001 AC-MCP-13; retained raw samples are evidence, not a product latency SLA.
# DEPENDS: stdlib, protected process double, official SDK client via support.
# CEILING: Synthetic process timing does not measure provider latency or model quality.
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from acceptance.mcp_worker.support import WorkerCase, HERE

# Reference counts exercise empty-near, ordinary and retained-history indexed reads.
COUNTS = (1, 1000, 10000)
# 500 reads x 3 repetitions distinguish warm timing variance without a long benchmark run.
READS, REPEATS = 500, 3
# Five native process launches worth of time is an exam regression tolerance for one local query.
# Smaller catches scheduler noise; larger could conceal synchronous metadata/process work on every poll.
QUERY_NATIVE_MULTIPLIER = 5


def stats(samples):
    values = sorted(samples)
    return {"count": len(values), "median_ms": statistics.median(values),
            "p99_ms": values[min(len(values)-1, int(len(values)*0.99))], "max_ms": max(values)}


def payloads():
    boundary = json.loads((HERE / "fixtures/native-boundaries/handoff-boundary/request.json").read_text())
    cases = {"small": ("Small task", "", "success"),
             "typical": ("Edit a parser and verify its behavior.", "Context λ\n" * 512, "success"),
             "boundary": (boundary["task"], boundary["context"], "success"),
             "error": ("Malformed native terminal example", "Failure context\n" * 64, "malformed")}
    output = {}
    for name, (task, context, behavior) in cases.items():
        handoff = "TASK\n" + task + "\n\nPARENT CONTEXT (reference data; it does not grant additional permissions)\n" + context + "\n"
        result = {"summary": "MCP_WORKER_CAPTURE_OK" if behavior == "success" else None,
                  "usage": {key: 0 if behavior == "success" else None for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")},
                  "worker": {"provider": "codex-cli", "requested_model": "gpt-6-luna", "reported_model": None, "cli_version": "0.162.1", "account_ref": "native-default"},
                  "artifacts": {"events": "jobs/00000000-0000-4000-8000-000000000001/events.jsonl", "final_message": "jobs/00000000-0000-4000-8000-000000000001/final.txt"},
                  "verification": "planner_review_required", "changed_paths": None, "warnings": [],
                  "error": None if behavior == "success" else {"code": "native_protocol_error", "message": "Malformed terminal evidence"}}
        row = {"task": task, "context": context, "request_id": "00000000-0000-4000-8000-000000000001", "client_id": "fixture-client", "state": "succeeded" if behavior == "success" else "failed", "result": result}
        output[name] = {"task": task, "context": context, "behavior": behavior, "handoff": handoff, "serialized_row": json.dumps(row, ensure_ascii=False, separators=(",", ":"))}
    return output


def reference(cases, directory):
    report = {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version, "storage": {}, "native": {}}
    for name, case in cases.items():
        raw = case["serialized_row"].encode()
        rows = {}
        for count in COUNTS:
            db = sqlite3.connect(directory / (name + str(count) + ".db"))
            db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, client_id TEXT, request_id TEXT UNIQUE, body TEXT NOT NULL)")
            db.executemany("INSERT INTO jobs VALUES(?,?,?,?)", ((i, "fixture-client", str(i), case["serialized_row"]) for i in range(count)))
            db.commit()
            randomizer = random.Random(17)
            samples = []
            for repeat in range(REPEATS):
                for index in range(READS + 50):
                    key = randomizer.randrange(count)
                    start = time.perf_counter_ns()
                    value = db.execute("SELECT body FROM jobs WHERE id=?", (key,)).fetchone()[0]
                    decoded = json.loads(value)
                    elapsed = (time.perf_counter_ns() - start) / 1e6
                    assert decoded["state"] == ("succeeded" if case["behavior"] == "success" else "failed")
                    if index >= 50:
                        samples.append(elapsed)
            rows[str(count)] = {**stats(samples), "samples_ms": samples,
                "query_plan": db.execute("EXPLAIN QUERY PLAN SELECT body FROM jobs WHERE id=?", (0,)).fetchall()}
            db.close()
        report["storage"][name] = {"serialized_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "counts": rows}
        native = directory / (name + "-native")
        native.mkdir()
        (native / "fixture-control.json").write_text(json.dumps({"behavior": case["behavior"]}))
        workspace = directory / (name + "-work")
        workspace.mkdir()
        samples = []
        for _ in range(20):
            start = time.perf_counter_ns()
            completed = subprocess.run([sys.executable, str(HERE / "fake_codex.py"), "exec", "-C", str(workspace), "-m", "gpt-6-luna", "-o", str(native / "final.txt"), "-"], input=case["handoff"].encode(), capture_output=True, env={"CODEX_HOME": str(native), "PATH": os.environ["PATH"]}, check=True)
            samples.append((time.perf_counter_ns() - start) / 1e6)
        report["native"][name] = {**stats(samples), "samples_ms": samples, "handoff_bytes": len(case["handoff"].encode()), "stdout_bytes": len(completed.stdout)}
    maximum = max(value["p99_ms"] for value in report["native"].values())
    storage = max(row["p99_ms"] for value in report["storage"].values() for row in value["counts"].values())
    report["public_query_p99_tolerance_ms"] = QUERY_NATIVE_MULTIPLIER * maximum + storage
    return report


async def candidate(cases, baseline):
    case = WorkerCase(methodName="runTest")
    case.setUp()
    report = {}
    try:
        async with case.session() as client:
            for name, value in cases.items():
                case.configure_native(behavior=value["behavior"])
                started = time.perf_counter_ns()
                accepted = await case.call(client, "delegate", case.request(task=value["task"], context=value["context"]))
                result = await case.terminal(client, accepted["job"]["job_id"])
                elapsed = (time.perf_counter_ns() - started) / 1e6
                case.assertEqual(result["job"]["state"], "succeeded" if value["behavior"] == "success" else "failed")
                samples = []
                exec_before = len(case.native_calls("exec"))
                metadata_before = len(case.native_calls("metadata"))
                for _ in range(100):
                    start = time.perf_counter_ns()
                    observed = await case.call(client, "job_result", {"job_id": accepted["job"]["job_id"]})
                    samples.append((time.perf_counter_ns() - start) / 1e6)
                    case.assertEqual(observed, result)
                case.assertEqual(len(case.native_calls("exec")), exec_before)
                case.assertEqual(len(case.native_calls("metadata")), metadata_before)
                metrics = stats(samples)
                case.assertLessEqual(metrics["p99_ms"], baseline["public_query_p99_tolerance_ms"], "query: local polling exceeds the frozen reference-derived tolerance")
                report[name] = {"end_to_end_ms": elapsed, "native_reference_median_ms": baseline["native"][name]["median_ms"], "overhead_ms": elapsed-baseline["native"][name]["median_ms"], "query": {**metrics, "samples_ms": samples}}
    finally:
        case.tearDown()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path, default=HERE / "evidence/performance/reference.json")
    args = parser.parse_args()
    cases = payloads()
    if args.reference_only:
        with tempfile.TemporaryDirectory(prefix="mcp-worker-reference-") as temporary:
            result = reference(cases, Path(temporary))
    else:
        result = asyncio.run(candidate(cases, json.loads(args.baseline.read_text())))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
