"""claims.jsonl → community-sentiment.json: bounded per-(family, use case) adjustments. See SPEC.md for the rules."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
EVIDENCE = {"anecdote": 1.0, "comparison": 2.0, "measured": 3.0}
KIND = {"vendor": 0.5, "social": 0.7, "forum": 1.0, "blog": 1.0, "benchmark-thread": 1.5}


def fold(max_points: float = 8.0, min_sources: int = 3, window_days: int = 30) -> dict:
    claims = []
    p = HERE / "claims.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                c = json.loads(line)
                if time.time() - float(c.get("ts") or 0) <= window_days * 86400 and c.get("family") and c.get("use_case"):
                    claims.append(c)
            except Exception:
                pass
    groups = defaultdict(list)
    for c in claims:
        groups[(c["family"], c["use_case"])].append(c)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M"), "claims_seen": len(claims), "max_points": max_points, "adjustments": {}}
    for (fam, uc), cs in groups.items():
        domains = {urlparse(c.get("source_url") or "").netloc for c in cs} - {""}
        kinds = {c.get("source_kind") for c in cs}
        if len(domains) < min_sources or kinds == {"vendor"}:
            continue
        num = den = 0.0
        for c in cs:
            w = float(c.get("specificity") or 1) * EVIDENCE.get(c.get("evidence"), 1.0) * KIND.get(c.get("source_kind"), 1.0)
            num += (1 if str(c.get("direction")) in ("+1", "1") else -1) * w
            den += w
        net = num / den if den else 0.0
        out["adjustments"].setdefault(fam, {})[uc] = {"points": round(max_points * net, 1), "net": round(net, 2), "sources": len(domains), "claims": len(cs),
                                                       "examples": [c["claim"] for c in cs[:3]]}
    (HERE.parent / "community-sentiment.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    o = fold()
    print(f"{o['claims_seen']} claims → {sum(len(v) for v in o['adjustments'].values())} adjustments")
