"""Opt-in deploy check for S1's system-prompt prefix cache (student.PrefixCache). NOT part of the test suite: it needs
MLX, the base model and the promoted adapter, so it runs on the Studio at deploy time, before the scorer is restarted.

  python3 -B s1/check_prefix_cache.py [--n 200] [--current s1/current.json] [--requests file.jsonl]
                                      [--max-growth-mb 256] [--max-mismatch 0]

It loads the promoted scorer once (a separate process from the running service; it does not touch it), then
  1. scores N requests with the cache on, sampling this process's current RSS after a short warm-up and at the end;
  2. scores the same N requests with the cache off but the prefill split at the prefix (use_prefix=False,
     prefill_split=len(prefix)): the cached path's exact computation without the stored state or its copy;
  3. compares the two paths' answers per question and the largest probability difference: they must be identical;
  4. scores them once more in one full prefill pass (the pre-cache path) and reports that difference apart
     (`full_pass_*`, not gated). Splitting a bf16 / 4-bit prefill anywhere moves the logits by about one bf16 step and
     flips near-ties, cache or no cache (2026-11-23 on the Studio), so the full pass is no exact reference.
Prints one JSON report (counts and numbers only, never request text) and exits 0 when RSS growth over the cached run is
within --max-growth-mb and answer mismatches are within --max-mismatch; 1 otherwise; 2 when it could not run.

The earlier prefix cache grew ~50 MB per call: over 200 calls that is ~10 GB, far past the default 256 MB bound.
Requests come from --requests (JSONL rows with a "request" field, e.g. a private local sample) or, by default, from a
synthetic set generated here, so no conversation data is needed."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

VERBS = ["rename", "fix", "refactor", "explain", "plan", "delete", "summarize", "migrate", "write tests for", "review"]
OBJECTS = ["the variable in utils.py", "a failing CI job", "the payments service", "this README", "a SQL schema",
           "the production bucket", "a flaky integration test", "the auth middleware", "a 40-file monorepo change",
           "a haiku about routers"]


def synthetic(n: int) -> list[str]:
    out = []
    for i in range(n):
        v, o = VERBS[i % len(VERBS)], OBJECTS[(i // len(VERBS)) % len(OBJECTS)]
        pad = " Keep the change small." * (i % 5)        # varied lengths
        out.append(f"{v} {o} (case {i}).{pad}")
    return out


def load_requests(path: str | None, n: int) -> list[str]:
    if not path:
        return synthetic(n)
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f.read().split("\n"):
            line = line.strip()
            if line:
                r = json.loads(line).get("request")
                if r:
                    rows.append(str(r)[:6000])
            if len(rows) >= n:
                break
    return rows


def current_rss_bytes() -> int:
    """This process's CURRENT resident set (not the peak): `ps` reports KiB on macOS and Linux."""
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True)
    return int(out.stdout.strip()) * 1024


def scorer_system() -> str:
    from student import messages
    return messages("x", "interactive")[0]["content"]


def compare(a: list, b: list) -> tuple:
    """(requests with any differing answer, differing answers, largest probability difference) of two score lists."""
    requests, answers, max_diff = 0, 0, 0.0
    for c, u in zip(a, b):
        diff = [q for q in u["answers"] if c["answers"].get(q) != u["answers"][q]]
        answers += len(diff)
        requests += bool(diff)
        for q, pu in u["probs"].items():
            for x, y in zip(c["probs"].get(q, []), pu):
                max_diff = max(max_diff, abs(x - y))
    return requests, answers, max_diff


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--current", default=str(HERE / "current.json"))
    ap.add_argument("--requests", default=None)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--max-growth-mb", type=float, default=256.0)
    ap.add_argument("--max-mismatch", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        from student import Scorer
        cfg = json.loads(Path(a.current).read_text())
        reqs = load_requests(a.requests, a.n)
        t0 = time.time()
        scorer = Scorer(cfg["base"], cfg["adapter"], cfg.get("temperatures"), prefix_cache=True)   # the check: on regardless of the env
        load_s = time.time() - t0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"could not run: {type(exc).__name__}: {str(exc)[:300]}"}))
        return 2
    if not reqs:
        print(json.dumps({"ok": False, "error": "no requests"}))
        return 2

    for r in reqs[:a.warmup]:                              # build the prefix, settle MLX's buffer cache
        scorer.score(r)
    rss_start = current_rss_bytes()
    cached, samples, t0 = [], [], time.time()
    for i, r in enumerate(reqs):
        cached.append(scorer.score(r))
        if (i + 1) % 50 == 0:
            samples.append(round(current_rss_bytes() / 2**20))
    cached_s = time.time() - t0
    rss_end = current_rss_bytes()
    stats = scorer.prefixes.stats()
    reused = scorer.last_stats.get("prefix_tokens", 0)

    split = len(scorer._system_prefix(scorer_system()))
    t0 = time.time()
    uncached = [scorer.score(r, use_prefix=False, prefill_split=split) for r in reqs]
    uncached_s = time.time() - t0
    t0 = time.time()
    full = [scorer.score(r, use_prefix=False) for r in reqs]
    full_s = time.time() - t0

    mismatched_requests, mismatched_answers, max_prob_diff = compare(cached, uncached)
    full_requests, full_answers, full_diff = compare(cached, full)
    growth_mb = (rss_end - rss_start) / 2**20
    ok = growth_mb <= a.max_growth_mb and mismatched_requests <= a.max_mismatch and reused > 0
    print(json.dumps({
        "ok": ok, "n": len(reqs), "load_s": round(load_s, 1),
        "prefix_reused_tokens": reused, "prefix_slots": stats["prefix_slots"],
        "prefix_mb": round(stats["prefix_bytes"] / 2**20, 1),
        "rss_start_mb": round(rss_start / 2**20), "rss_end_mb": round(rss_end / 2**20),
        "rss_growth_mb": round(growth_mb, 1), "rss_every_50_mb": samples, "max_growth_mb": a.max_growth_mb,
        "cached_mean_ms": round(cached_s / len(reqs) * 1000), "uncached_mean_ms": round(uncached_s / len(reqs) * 1000),
        "mismatched_requests": mismatched_requests, "mismatched_answers": mismatched_answers,
        "max_prob_diff": round(max_prob_diff, 4),
        "full_pass_mean_ms": round(full_s / len(reqs) * 1000), "full_pass_mismatched_requests": full_requests,
        "full_pass_mismatched_answers": full_answers, "full_pass_max_prob_diff": round(full_diff, 4),
    }))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
