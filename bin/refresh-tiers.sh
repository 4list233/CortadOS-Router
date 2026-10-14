#!/bin/bash
# Weekly: what can we reach (inventory) → what do public benchmarks say (scores.csv) → which band is each model in (tiers.generated.json).
# The router hot-reads tiers.generated.json; if any step fails the previous file stays in place.
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
bin/refresh-inventory.sh
( cd tables/benchmarks && cp scores.csv scores.prev.csv 2>/dev/null; python3 fetch_benchmarks.py ) || echo "fetch failed: keeping previous scores"
[ "$(wc -l < tables/benchmarks/scores.csv)" -gt 1000 ] || { cp tables/benchmarks/scores.prev.csv tables/benchmarks/scores.csv; echo "scores.csv looked truncated: restored previous"; }
cp tables/tiers.generated.json tables/tiers.prev.json 2>/dev/null
python3 tables/benchmarks/build_tiers.py > tables/benchmarks/last-build.txt || { cp tables/tiers.prev.json tables/tiers.generated.json; echo "tier build failed: restored previous"; exit 1; }
python3 - <<'PY'
import json
a, b = (json.load(open(f"tables/{n}.json")) for n in ("tiers.prev", "tiers.generated"))
band = lambda d: {x["family"]: x["band"] for x in d["families"]}
moves = {f: (band(a).get(f), v) for f, v in band(b).items() if band(a).get(f) != v}
print("band changes:", moves or "none")
PY
