"""Measured local compute scarcity. Standalone node collector and fleet sampler.

No subscription CLI, model generation, RPC health connection, or remote write is
performed. Missing telemetry is unknown, never unused capacity. Memory values
are bytes; Apple GPU allocations already share host RAM and are not added to it.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time

GIB = 2 ** 30


def command(argv, timeout=5):
    result = subprocess.run(argv, capture_output=True, timeout=timeout, check=True)
    return result.stdout


def mac_memory(text):
    page = re.search(r"page size of (\d+) bytes", text)
    if not page:
        raise ValueError("vm_stat did not report page size")
    pages = {key.strip(): int(value) for key, value in re.findall(r"^([^:\n]+):\s+(\d+)\.", text, re.M)}
    # Reclaimability is an estimate; inactive pages are not guaranteed free.
    return sum(pages.get(key, 0) for key in ("Pages free", "Pages inactive", "Pages speculative")) * int(page[1])


def collect_node(metal_probe=None):
    now = time.time()
    out = {"sampled_at": now, "platform": platform.system(), "hostname": platform.node(),
           "cpu_count": os.cpu_count(), "host_memory_total_bytes": None,
           "host_available_estimate_bytes": None, "gpu_budget_bytes": None,
           "gpu_headroom_before_reserve_bytes": None, "gpu_utilization_pct": None,
           "gpus": [], "warnings": []}
    try:
        out["load_average_1m"] = os.getloadavg()[0]
    except OSError:
        out["load_average_1m"] = None
    if sys.platform == "darwin":
        out["host_memory_scope"] = "unified"
        try:
            out["host_memory_total_bytes"] = int(command(["/usr/sbin/sysctl", "-n", "hw.memsize"]))
            out["host_available_estimate_bytes"] = mac_memory(command(["/usr/bin/vm_stat"]).decode())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            out["warnings"].append("Host memory unavailable: " + type(exc).__name__)
        try:
            devices = plistlib.loads(command(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOAccelerator", "-d", "1"]))
            stats = next((device["PerformanceStatistics"] for device in devices if "PerformanceStatistics" in device), {})
            out["gpu_driver_allocated_bytes"] = stats.get("Alloc system memory")
            out["gpu_driver_in_use_bytes"] = stats.get("In use system memory")
            out["gpu_utilization_pct"] = stats.get("Device Utilization %")
            out["gpu_utilization_source"] = "IOAccelerator driver sample; not a benchmark"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            out["warnings"].append("GPU counters unavailable: " + type(exc).__name__)
        if metal_probe:
            try:
                budget = json.loads(command([metal_probe]))
                out["gpu_budget_bytes"] = budget["recommended_working_set_bytes"]
                out["gpu_name"] = budget["name"]
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                out["warnings"].append("Metal budget unavailable: " + type(exc).__name__)
        budget, allocated = out["gpu_budget_bytes"], out.get("gpu_driver_allocated_bytes")
        host = out["host_available_estimate_bytes"]
        if all(isinstance(value, (float, int)) for value in (budget, allocated, host)):
            out["gpu_headroom_before_reserve_bytes"] = max(0, min(budget - allocated, host))
    else:
        try:
            info = Path("/proc/meminfo").read_text()
            memory = {key: int(kib) * 1024 for key, kib in re.findall(r"^(\w+):\s+(\d+) kB", info, re.M)}
            out["host_memory_total_bytes"] = memory.get("MemTotal")
            out["host_available_estimate_bytes"] = memory.get("MemAvailable")
            out["host_memory_scope"] = "wsl_vm" if "microsoft" in platform.release().lower() else "system"
        except OSError:
            out["warnings"].append("Host memory unavailable")
        smi = shutil.which("nvidia-smi")
        if not smi and Path("/usr/lib/wsl/lib/nvidia-smi").is_file():
            smi = "/usr/lib/wsl/lib/nvidia-smi"
        if smi:
            try:
                raw = command([smi, "--query-gpu=name,memory.total,memory.free,utilization.gpu", "--format=csv,noheader,nounits"]).decode()
                for name, total, free, utilization in csv.reader(io.StringIO(raw), skipinitialspace=True):
                    out["gpus"].append({"name": name, "total_bytes": int(float(total) * 2**20),
                                        "free_bytes": int(float(free) * 2**20),
                                        "utilization_pct": float(utilization) if utilization.strip().isdigit() else None})
                if out["gpus"]:
                    out["gpu_budget_bytes"] = sum(gpu["total_bytes"] for gpu in out["gpus"])
                    out["gpu_headroom_before_reserve_bytes"] = sum(gpu["free_bytes"] for gpu in out["gpus"])
                    used = [gpu["utilization_pct"] for gpu in out["gpus"] if gpu["utilization_pct"] is not None]
                    out["gpu_utilization_pct"] = max(used) if used else None
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                out["warnings"].append("NVIDIA counters unavailable: " + type(exc).__name__)
    return out


def sample_node(node, collector_source):
    base = {"id": node["id"], "role": node.get("role"), "pool_eligible": node.get("pool_eligible", False),
            "inventory": node.get("inventory", {}), "status": "unconfigured"}
    target = node.get("target")
    if not target:
        return base
    try:
        if target == "local":
            data = collect_node(node.get("metal_probe"))
        else:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", target):
                raise ValueError("invalid SSH target")
            remote = ["python3", "-", "--collect"]
            if node.get("metal_probe"):
                remote += ["--metal-probe", node["metal_probe"]]
            proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=yes",
                                   target, shlex.join(remote)], input=collector_source, capture_output=True, timeout=15, check=True)
            data = json.loads(proc.stdout)
        if not isinstance(data, dict) or "sampled_at" not in data:
            raise ValueError("invalid node sample")
        sample_time = float(data["sampled_at"])
        if not math.isfinite(sample_time):
            raise ValueError("invalid timestamp")
        headroom = data.get("gpu_headroom_before_reserve_bytes")
        reserve = max(0, float(node.get("reserve_gib", 2))) * GIB
        data["gpu_headroom_estimate_bytes"] = max(0, headroom - reserve) if isinstance(headroom, (int, float)) else None
        data["reserve_bytes"] = int(reserve)
        return {**data, **base, "status": "online"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {**base, "status": "unreachable", "error": type(exc).__name__}


def deployment_health(deployment, nodes, now, max_age_s):
    reasons, pressure = [], []
    if deployment.get("ready") is not True:
        reasons.append("deployment has not passed an inference readiness check")
    members = deployment.get("nodes") or []
    if not members:
        reasons.append("no worker membership configured")
    context = deployment.get("max_context_tokens")
    if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        reasons.append("no verified context limit")
    requirements = deployment.get("additional_memory_gib_by_node") or {}
    for member in members:
        node = nodes.get(member) or {}
        if not node.get("pool_eligible"):
            reasons.append(member + " is excluded from the stable pool")
        age = now - float(node.get("sampled_at") or 0)
        if node.get("status") != "online" or not -5 <= age <= max_age_s:
            reasons.append(member + " is offline or its telemetry is stale")
            continue
        headroom, budget = node.get("gpu_headroom_estimate_bytes"), node.get("gpu_budget_bytes")
        required = requirements.get(member)
        if not isinstance(required, (int, float)) or not math.isfinite(required) or required < 0:
            reasons.append(member + " has no measured additional-memory requirement")
        elif not isinstance(headroom, (int, float)) or not math.isfinite(headroom) or headroom < required * GIB:
            reasons.append(member + " has insufficient or unknown GPU headroom")
        utilization = node.get("gpu_utilization_pct")
        if not isinstance(utilization, (int, float)) or not math.isfinite(utilization):
            reasons.append(member + " has unknown GPU utilization")
        else:
            pressure.append(min(0.95, max(0, utilization / 100)))
        if isinstance(headroom, (int, float)) and budget and math.isfinite(headroom):
            pressure.append(min(0.95, max(0, 1 - headroom / budget)))
    return {"ok": not reasons, "why": "; ".join(reasons) or None,
            "scarcity": 1.0 if reasons else round(max(pressure, default=0), 2),
            "spent": bool(reasons), "nodes": members, "endpoint": deployment.get("endpoint"),
            "max_context_tokens": deployment.get("max_context_tokens"),
            "measured_tokens_per_second": deployment.get("measured_tokens_per_second")}


def sample(config_path):
    config = json.loads(Path(config_path).read_text())
    source = Path(__file__).read_bytes()
    nodes_config = config.get("nodes", [])
    ids = [node["id"] for node in nodes_config]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate node IDs")
    with ThreadPoolExecutor(max_workers=4) as executor:
        readings = list(executor.map(lambda node: sample_node(node, source), nodes_config))
    nodes = {node["id"]: node for node in readings}
    now = time.time()
    max_age_s = min(120, max(1, float(config.get("max_age_s", 120))))
    deployments = {deployment["model"]: deployment_health(deployment, nodes, now, max_age_s)
                   for deployment in config.get("deployments", [])}
    healthy = [deployment for deployment in deployments.values() if deployment["ok"]]
    eligible = [node for node in readings if node["pool_eligible"] and node["status"] == "online"
                and -5 <= now - float(node.get("sampled_at") or 0) <= max_age_s]
    known = [node for node in eligible if node.get("gpu_headroom_estimate_bytes") is not None]
    # The reading is only as fresh as the oldest node a healthy deployment stands on; the router stops admitting
    # local routes at expires_at (scarcity.lane_scarcity), so expired capacity never admits a request.
    members = {member for deployment in healthy for member in deployment["nodes"]}
    expires_at = min([float(nodes[member].get("sampled_at") or now) + max_age_s for member in members if member in nodes]
                     + [now + max_age_s])
    return {"source": "local_resources: node telemetry; no quota reset", "windows": [], "fetched_at": now,
            "max_age_s": max_age_s, "expires_at": expires_at,
            "scarcity": min((deployment["scarcity"] for deployment in healthy), default=1.0),
            "refusing": not healthy, "resource_managed": True, "pools": {},
            "why": None if healthy else "no verified local deployment currently available",
            "nodes": readings, "deployments": deployments,
            "known_pool_headroom_estimate_bytes": sum(node["gpu_headroom_estimate_bytes"] for node in known),
            "unknown_pool_nodes": [node["id"] for node in readings if node["pool_eligible"] and node not in known],
            "note": "Sum is inventory headroom, not proof one model fits. Host RAM is separate; no automatic failover."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true", help="collect this node only; writes JSON to stdout")
    parser.add_argument("--metal-probe")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.collect:
        result = collect_node(args.metal_probe)
    elif args.config:
        result = sample(args.config)
    else:
        parser.error("choose --collect or --config")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
