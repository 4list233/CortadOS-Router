"""Benchmarks → tiers. No human tier list: a model's band comes from public benchmark scores (scores.csv, fetched by fetch_benchmarks.py).

Method (all knobs live in routing-table.json → "tiering"):
 1. Names: every published model name is normalised to a FAMILY (effort/thinking/date suffixes stripped; mini/nano/lite/pro/instant kept:
    they are different models). A family's score on a benchmark = its best published configuration.
 2. Ability: benchmarks differ in scale and difficulty and no model is tested everywhere, so each score is min-max scaled inside its benchmark,
    put on a logit scale, and one ability θ per family + one difficulty per benchmark are fitted over ALL ~1,100 published models
    (y = θ_family − d_benchmark, ridge-shrunk so a family seen on 2 benchmarks cannot outrank one seen on 40 by luck). One θ overall and one per
    skill (code, agentic, reasoning, chat).
 3. Bands: the floor of a band is the midpoint between the abilities of the reference models of that band and the band below. The references
    are the models the 2.0 table already ruled into each band (haiku / sonnet / opus / fable class), so "R3" keeps meaning "Opus-class or better"
    while WHICH models are Opus-class is decided by the numbers, and changes when the numbers change.
 4. Reach: a ranked family becomes candidates only through subscriptions that can actually run it (tables/inventory/*).
 Families with no public score stay unranked (never routed by this file); Cursor-branded Grok borrows plain Grok's score, flagged proxy."""
from __future__ import annotations

import csv
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUTER = HERE.parent
EFFORT = r"(?:low|medium|med|high|xhigh|max|promax|none|minimal|unknown|effort|thinking|reasoning|preview|fast|nothink|latest|\d+k)"
WORK_KIND_SKILLS = {"code": {"code": 0.6, "agentic": 0.4}, "ops": {"agentic": 0.7, "code": 0.3}, "research": {"reasoning": 0.7, "chat": 0.3},
                    "planning": {"reasoning": 0.6, "agentic": 0.4}, "writing": {"chat": 0.8, "reasoning": 0.2}, "conversation": {"chat": 0.7, "reasoning": 0.3},
                    "capture/log": {"chat": 0.5, "agentic": 0.5}}


def family(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\([^)]*\)", " ", n)                       # "(Max)", "(codex-harness)"
    n = n.split("/")[-1]                                   # "chutes/Qwen3-8B"
    n = re.sub(r"[ _.:]+", "-", n)
    n = re.sub(r"-20\d{2}-?\d{2}-?\d{2}", "", n)           # date stamps
    n = re.sub(r"^hf-co-[a-z0-9]+-", "", n).replace("-gguf", "").replace("-128k", "").replace("-q4-k-m", "")   # ollama/hf tags
    prev = None
    while prev != n:
        prev = n
        n = re.sub(rf"-{EFFORT}$", "", n).strip("-")
    return n


def load_scores() -> dict[tuple[str, str], dict[str, float]]:
    """{(benchmark, skill): {family: best unit-interval score}}"""
    raw = defaultdict(lambda: defaultdict(list))
    for r in csv.DictReader((HERE / "scores.csv").open()):
        if r["skill"] in ("speed", "price"):
            continue
        try:
            raw[(r["source_id"] + ":" + r["benchmark"], r["skill"])][family(r["model_name_as_published"])].append(float(r["score"]))
        except ValueError:
            pass
    out = {}
    for key, fams in raw.items():
        best = {f: max(v) for f, v in fams.items()}
        lo, hi = min(best.values()), max(best.values())
        if len(best) >= 8 and hi > lo:
            out[key] = {f: min(0.98, max(0.02, (s - lo) / (hi - lo))) for f, s in best.items()}
    return out


def price_and_speed() -> dict[str, dict[str, float]]:
    """Per family, from Artificial Analysis: blended $/1M tokens (3:1) and output tokens/s. Several published variants → the median."""
    import statistics
    acc = defaultdict(lambda: defaultdict(list))
    for r in csv.DictReader((HERE / "scores.csv").open()):
        if r["source_id"] == "artificial_analysis" and r["benchmark"] in ("price_1m_blended_3_to_1_usd", "output_tokens_per_second", "time_to_first_token_s"):
            try:
                v = float(r["score"])
            except ValueError:
                continue
            if v > 0:
                acc[family(r["model_name_as_published"])][r["benchmark"]].append(v)
    return {f: {k: round(statistics.median(v), 3) for k, v in d.items()} for f, d in acc.items()}


def fit(benchmarks: dict, ridge: float = 1.5, with_difficulty: bool = False):
    y = {b: {f: math.log(s / (1 - s)) for f, s in fams.items()} for b, fams in benchmarks.items()}
    theta, diff = defaultdict(float), defaultdict(float)
    by_fam = defaultdict(list)
    for b, fams in y.items():
        for f in fams:
            by_fam[f].append(b)
    for _ in range(60):
        for b, fams in y.items():
            diff[b] = sum(theta[f] - v for f, v in fams.items()) / len(fams)
        for f, bs in by_fam.items():
            theta[f] = sum(y[b][f] + diff[b] for b in bs) / (len(bs) + ridge)      # ridge: pulled toward 0 (the average model) when evidence is thin
    if with_difficulty:
        return dict(theta), {f: len(bs) for f, bs in by_fam.items()}, dict(diff), y
    return dict(theta), {f: len(bs) for f, bs in by_fam.items()}


def reach() -> dict[str, list[dict]]:
    """family → the ways we can actually run it, native subscription first."""
    out = defaultdict(list)
    inv = ROUTER / "inventory"
    for m in ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"):   # Claude Code ≥ 2.1.280 for opus-5-5
        out[family(m)].append({"lane": "claude", "provider": "claude-cli", "model": m})
    for m in (inv / "codex.txt").read_text().split():
        if m not in ("codex-auto-review", "gpt-reserve"):
            out[family(m)].append({"lane": "codex", "provider": "openai-codex", "model": m})
    best: dict[str, tuple[int, str]] = {}
    for line in (inv / "cursor.txt").read_text().splitlines():
        mid = line.split(" - ")[0].strip()
        if " - " not in line or "fable" in mid or mid == "auto" or mid.endswith("-fast"):      # Cursor's Fable ids are NO-ZDR; -fast is a price tier, same model
            continue
        rank = next((i for i, suf in enumerate(("-thinking-high", "-high", "-medium", "")) if mid.endswith(suf)), 9)
        f = family(mid)
        if f not in best or rank < best[f][0]:
            best[f] = (rank, mid)
    for f, (_, mid) in best.items():
        pool = "cursor-models" if re.search(r"grok|composer", mid) else "other-models"
        out[re.sub(r"^cursor-", "", f) if f.startswith("cursor-grok") else f].append(
            {"lane": "cursor", "provider": "cursor-cli", "model": mid, "pool": pool, **({"proxy_for": f} if f.startswith("cursor-grok") else {})})
    seen = set()
    for line in (inv / "agy.txt").read_text().splitlines():
        if "\t" in line:
            mid, name = line.split("\t")[:2]
            f = family(mid)
            if f not in seen and (mid.endswith("-medium") or not re.search(r"-(low|high)$", mid) or f + "-medium" not in (inv / "agy.txt").read_text()):
                seen.add(f)
                out[f].append({"lane": "antigravity", "provider": "agy-cli", "model": mid.strip(), "display": name.strip(), "pool": "gemini" if "gemini" in mid else "claude-gpt"})
    # Forest 2026-09-28: local models are OUT of routing (if a route cannot use the local models, leave them out); the 8B serves only
    # as the context engine's extractor. ollama.txt is still listed in the inventory for the record, but no local route is offered.
    return out


def main() -> int:
    table = json.loads((ROUTER / "routing-table.json").read_text())
    cfg = table.get("tiering") or {}
    refs = cfg.get("band_references") or {}
    bm = load_scores()
    overall, n_obs, diff, y = fit(bm, cfg.get("ridge", 1.5), with_difficulty=True)
    # ---- per-use-case ability: weighted mean of (result + benchmark difficulty) on the benchmarks that resemble OUR work, shrunk toward general ability
    uc = json.loads((ROUTER / "use-cases.json").read_text())
    by_name = {k[0]: k for k in bm}
    unknown = sorted({b for u in uc["use_cases"].values() for b in u["benchmarks"] if b not in by_name})
    ability = {"general": dict(overall)}
    evidence = defaultdict(dict)
    for u, spec in uc["use_cases"].items():
        ability[u] = {}
        for f, gen in overall.items():
            num, den, n = uc["shrink"] * gen, uc["shrink"], 0
            for b, w in spec["benchmarks"].items():
                k = by_name.get(b)
                if k and f in y[k]:
                    num += w * (y[k][f] + diff[k]); den += w; n += 1
            ability[u][f] = num / den
            evidence[u][f] = n
    top = {u: max(v.values()) for u, v in ability.items()}
    # community sentiment (tables/community/SPEC.md): a bounded nudge per (family, use case), applied to the use-case index only
    try:
        import importlib.util as _ilu
        _sp = _ilu.spec_from_file_location("fold_sentiment", str(ROUTER / "community/fold_sentiment.py")); _fs = _ilu.module_from_spec(_sp); _sp.loader.exec_module(_fs)
        sentiment = _fs.fold().get("adjustments") or {}
    except Exception as exc:
        print("community sentiment unavailable:", exc, file=sys.stderr); sentiment = {}
    def index(u, f):
        raw = 100.0 * ability[u][f] / top[u]
        nudge = ((sentiment.get(f) or {}).get(u) or {}).get("points", 0.0) if u != "general" else 0.0
        return round(min(100.0, max(0.0, raw + nudge)), 1)                     # 100 = the best model anyone has published results for, on this use case
    skills = {s: fit({k: v for k, v in bm.items() if k[1] == s}, cfg.get("ridge", 1.5))[0] for s in ("code", "agentic", "reasoning", "chat")}
    missing = [m for ms in refs.values() for m in ms if family(m) not in overall]
    if missing:
        print("reference models without benchmark data:", missing, file=sys.stderr)
        return 1
    order = ["R1", "R2", "R3", "R4"]
    anchor = {b: min(overall[family(m)] for m in refs[b]) for b in order}
    floors = {"R1": -1e9, **{b: round((anchor[order[i - 1]] + anchor[b]) / 2, 3) for i, b in enumerate(order) if i}}
    ways = reach()
    fams = []
    for f, routes in ways.items():
        if f not in overall or n_obs[f] < cfg.get("min_benchmarks", 3):
            fams.append({"family": f, "band": None, "why": "no or too few public benchmark scores: unranked, never routed by benchmarks", "reach": routes})
            continue
        th = overall[f]
        band = [b for b in order if th >= floors[b]][-1]
        fams.append({"family": f, "theta": round(th, 3), "by_skill": {s: round(v[f], 3) for s, v in skills.items() if f in v}, "benchmarks": n_obs[f], "band": band,
                     "proxy": any("proxy_for" in r for r in routes), "reach": routes})

    def kind_score(x: dict, kind: str | None) -> float:
        w = WORK_KIND_SKILLS.get(kind or "", {})
        have = {s: wt for s, wt in w.items() if s in x.get("by_skill", {})}
        return sum(x["by_skill"][s] * wt for s, wt in have.items()) / sum(have.values()) if have else x["theta"]
    cap = int(cfg.get("max_candidates_per_band", 6))

    def pick(band: str, kind: str | None) -> list[dict]:
        """Own-band families ranked for the work kind. First the best model of EVERY subscription pool (so no subscription sits idle in a band and
        scarcity always has somewhere to move work), then the best of the rest; then the band above as the fail-up tail (never the ceiling)."""
        def entries(bb: str) -> list[dict]:
            members = sorted([x for x in fams if x["band"] == bb], key=lambda x: -kind_score(x, kind))
            return [{"family": x["family"], "score": round(kind_score(x, kind), 3), "tier_band": bb, **r} for x in members for r in x["reach"]]
        own, chosen, pools = entries(band), [], set()
        for e in own:
            key = (e["lane"], e.get("pool"))
            if key not in pools:
                pools.add(key); chosen.append(e)
        chosen += [e for e in own if e not in chosen][:max(0, cap - len(chosen))]
        chosen.sort(key=lambda e: -e["score"])
        up = order[order.index(band) + 1] if band != "R4" and order[order.index(band) + 1] != "R4" else None
        tail, seen = [], set()
        for e in (entries(up) if up else []):
            if (e["lane"], e.get("pool")) not in seen:
                seen.add((e["lane"], e.get("pool"))); tail.append({**e, "fail_up": True})
        return chosen + tail[:3]
    bands = {b: {k or "default": pick(b, k) for k in [None, *WORK_KIND_SKILLS]} for b in order}
    ceiling_floor = min(overall[family(m)] for m in refs["R4"])
    ps = price_and_speed()
    scored = []
    for x in fams:
        f = x["family"]
        if x.get("band") is None:
            continue
        pf = ps.get(f) or {}
        scored.append({"family": f, "benchmarks": n_obs[f], "proxy": x.get("proxy", False), "ceiling": overall[f] >= ceiling_floor,
                       "price_1m_blended_usd": pf.get("price_1m_blended_3_to_1_usd"), "tokens_per_s": pf.get("output_tokens_per_second"), "ttft_s": pf.get("time_to_first_token_s"),
                       "index": {u: index(u, f) for u in ability}, "use_case_benchmarks": {u: evidence[u][f] for u in evidence}, "reach": x["reach"]})
    scored.sort(key=lambda m: -m["index"]["general"])
    refs_index = {b: {u: min(index(u, family(m)) for m in ms) for u in ability} for b, ms in refs.items()}
    (ROUTER / "model-scores.generated.json").write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%d %H:%M"), "scale": "index 100 = best published model on that use case (any vendor, reachable or not); from public benchmarks only",
        "use_cases": {u: v["what"] for u, v in uc["use_cases"].items()}, "use_case_benchmarks_missing_from_data": unknown,
        "reference_index": refs_index, "models": scored, "unranked": [{"family": x["family"], "reach": x["reach"]} for x in fams if x.get("band") is None]}, indent=1))
    cols = ["general", *uc["use_cases"]]
    print("index per use case (100 = best published model) · price $/1M blended · tok/s:\n  " + "family".ljust(24) + "".join(c[:8].rjust(9) for c in cols) + "    $/1M   tok/s   lanes")
    for m in scored:
        print("  " + m["family"].ljust(24) + "".join(str(m["index"][c]).rjust(9) for c in cols) + f"  {m['price_1m_blended_usd'] if m['price_1m_blended_usd'] is not None else '?':>6} {m['tokens_per_s'] if m['tokens_per_s'] is not None else '?':>6}   "
              + ",".join(sorted({r["lane"] for r in m["reach"]})) + ("  ceiling" if m["ceiling"] else "") + ("  proxy" if m["proxy"] else ""))
    if unknown:
        print("use-case benchmarks not in the data:", unknown)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M"), "method": "per-benchmark min-max → logit → additive ability/difficulty fit over all published models, ridge-shrunk; floors = midpoints between band reference abilities",
           "benchmarks_used": len(bm), "families_fitted": len(overall), "band_references": refs, "reference_abilities": {b: round(a, 3) for b, a in anchor.items()}, "floors": floors,
           "families": sorted(fams, key=lambda x: -(x.get("theta") or -9)), "bands": bands}
    (ROUTER / "tiers.generated.json").write_text(json.dumps(out, indent=1))
    print(f"{len(bm)} benchmarks, {len(overall)} families fitted; floors {floors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
