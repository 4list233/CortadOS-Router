"""Grade a CortadOS Router S1 candidate on the HOLDOUT (15% of requests, never trained on, labelled by a different and stronger teacher: Opus ×3),
calibrate it on the valid split, compare with the incumbent engine, and decide promotion. No hand gold set yet (Forest 2026-09-19), so the
bar is the stronger teacher; when Forest corrects routes in Discord those rows outrank every teacher label.

  python evaluate.py --base <mlx model> --adapter <dir> --out runs/<name> [--baseline]
  ../.venv-laya/bin/python evaluate.py --engine laya --checkpoint laya/runs/<date> --out laya/runs/<date>     (the laya candidate, same holdout, same grading)"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from build_sft import OUTCOME_UNIT_KIND                # the one reading outcome tasks are graded under (interactive)
from common import DATA, QUESTIONS, core, read_jsonl, read_labels, valid_answers
from student import N_OPTIONS, Scorer, encode

BANDS = ["R0", "R1", "R2", "R3", "R4"]


def outcome_holdout() -> list[tuple[dict, str]]:
    """Held-out tasks the loop actually ran, as (request_row, outcome_band). 'above' (no band passed) is not a label,
    so it is dropped. Empty until the loop has judged holdout tasks."""
    from outcome import store as outcome_store
    return [({"id": o["id"], "request": o["request"], "unit_kind": OUTCOME_UNIT_KIND}, o["outcome_band"])
            for o in outcome_store.read("outcomes")
            if o.get("split") == "holdout" and o.get("outcome_band") in BANDS and isinstance(o.get("request"), str)]


def band_of(a: dict) -> str:
    core.lane_health = lambda: {}
    return core.resolve({"answers": a, "degraded": False, "engine": "eval", "engine_ms": 0})["band"]


def teacher_view(samples: list[dict]) -> dict:
    dist = {q: [sum(encode(q, s[q]) == d for s in samples) / len(samples) for d in range(N_OPTIONS[q])] for q in QUESTIONS}
    major = {q: max(range(N_OPTIONS[q]), key=dist[q].__getitem__) for q in QUESTIONS}
    return {"dist": dist, "major": major, "band": Counter(band_of(s) for s in samples).most_common(1)[0][0]}


def fit_temperatures(scorer: Scorer, rows: list[tuple[dict, list[dict]]]) -> dict[str, float]:
    """One temperature per question, chosen on the valid split to minimise NLL against the teacher's samples."""
    scorer.T = {}
    raw = [(scorer.score(r["request"], r["unit_kind"])["probs"], samples) for r, samples in rows]
    out = {}
    for q in QUESTIONS:
        best = (1e9, 1.0)
        for T in [0.5, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]:
            nll = 0.0
            for probs, samples in raw:
                z = [math.log(max(p, 1e-9)) / T for p in probs[q]]
                lse = math.log(sum(math.exp(x) for x in z))
                nll -= sum(z[encode(q, s[q])] - lse for s in samples) / len(samples)
            best = min(best, (nll, T))
        out[q] = best[1]
    return out


OUTWARD_AT = float(__import__("os").environ.get("CORTADOS_S1_OUTWARD_AT", "0.5"))


def failed_up(p: dict, below: float) -> dict:
    """Same rule as core.decide_s1: uncertain difficulty features round UP (never down)."""
    if not below:
        return p
    ans = dict(p["answers"])
    pr_out = (p.get("probs") or {}).get("outward") or []
    if len(pr_out) == 2 and pr_out[1] >= OUTWARD_AT:            # destructive question: stricter, call it outward at a lower probability
        ans["outward"] = True
    for f in core.FEATURES:
        pr = (p.get("probs") or {}).get(f) or []
        if pr and max(pr) < below:
            top2 = sorted(range(len(pr)), key=lambda i: -pr[i])[:2]
            ans[f] = max(top2)
    return {**p, "answers": ans}


def grade(predict, rows: list[tuple[dict, dict]]) -> dict:
    per_q, conf_bins, band_hits, under, over, lat = defaultdict(list), defaultdict(list), [], 0, 0, []
    outward_tp = outward_fn = misses = 0
    for r, tv in rows:
        p = predict(r)
        if p is None:
            misses += 1
            continue
        lat.append(p.get("ms", 0))
        for q in QUESTIONS:
            d = encode(q, p["answers"][q])
            ok = d == tv["major"][q]
            per_q[q].append(ok)
            if p.get("probs"):
                conf_bins[q].append((max(p["probs"][q]), ok, sum((p["probs"][q][i] - tv["dist"][q][i]) ** 2 for i in range(N_OPTIONS[q]))))
        b = band_of(p["answers"])
        band_hits.append(b == tv["band"])
        under += BANDS.index(b) < BANDS.index(tv["band"])
        over += BANDS.index(b) > BANDS.index(tv["band"])
        if tv["major"]["outward"] == 1:
            outward_tp += p["answers"]["outward"] is True
            outward_fn += p["answers"]["outward"] is not True
    n = max(len(band_hits), 1)

    def ece(items):
        bins = defaultdict(list)
        for c, ok, _ in items:
            bins[min(int(c * 10), 9)].append((c, ok))
        return sum(len(v) / len(items) * abs(sum(c for c, _ in v) / len(v) - sum(o for _, o in v) / len(v)) for v in bins.values()) if items else None
    return {"n": len(band_hits), "no_answer": misses, "band_accuracy": round(sum(band_hits) / n, 3), "under_routed": round(under / n, 3), "over_routed": round(over / n, 3),
            "outward_recall": round(outward_tp / max(outward_tp + outward_fn, 1), 3), "outward_positives": outward_tp + outward_fn,
            "question_accuracy": {q: round(sum(v) / len(v), 3) for q, v in per_q.items()},
            "ece": {q: round(ece(v), 3) for q, v in conf_bins.items()} or None,
            "brier": {q: round(sum(x[2] for x in v) / len(v), 3) for q, v in conf_bins.items()} or None,
            "latency_ms_median": int(statistics.median(lat)) if lat else None}


def grade_outcome(predict, rows: list[tuple[dict, str]]) -> dict:
    """Band accuracy against the band each held-out task ACTUALLY needed (its outcome band), not a teacher's reading.
    Rows are (request_row, outcome_band). Under-routing here is the costly error the whole loop exists to drive down."""
    hits = under = over = misses = n = 0
    for r, truth in rows:
        p = predict(r)
        if p is None:
            misses += 1
            continue
        n += 1
        b = band_of(p["answers"])
        hits += b == truth
        under += BANDS.index(b) < BANDS.index(truth)
        over += BANDS.index(b) > BANDS.index(truth)
    nn = max(n, 1)
    return {"n": n, "no_answer": misses, "band_accuracy": round(hits / nn, 3),
            "under_routed": round(under / nn, 3), "over_routed": round(over / nn, 3)}


def promote_decision(c: dict, bar: dict | None, obar: dict | None, co: dict | None, has_outcome_holdout: bool) -> tuple[bool, bool]:
    """(promote, outcome_regression_checked). No bar → never promote (nothing beats nothing). Band accuracy may give up
    to 3 points when under-routing improves, else holds within one request of noise; under-routing and outward recall
    never regress. When held-out outcomes exist a valid outcome bar is REQUIRED, and on the SAME outcome holdout the
    candidate may regress on neither outcome band accuracy nor outcome under-routing (strict, no slack — these are the
    actual-task ground truth, so no degradation is allowed; Forest/Codex review 2026-10-25). A missing/failed outcome
    baseline blocks promotion rather than defaulting to OK."""
    if not bar:
        return False, False
    slack = 1.0 / max(c["n"], 1)
    better_under = c["under_routed"] < bar["under_routed"]
    acc_ok = c["band_accuracy"] >= bar["band_accuracy"] - (0.03 if better_under else slack)
    if has_outcome_holdout:
        outcome_ok = (bool(obar and co and co.get("n"))
                      and co["band_accuracy"] >= obar["band_accuracy"]
                      and co["under_routed"] <= obar["under_routed"])
    else:
        outcome_ok = True
    promote = acc_ok and c["under_routed"] <= bar["under_routed"] + slack and c["outward_recall"] >= bar["outward_recall"] and outcome_ok
    return promote, bool(has_outcome_holdout and obar and co and co.get("n"))


def promoted_bar_for_laya(holdout: list, fail_up: float) -> tuple[dict | None, str | None]:
    """The bar for a non-MLX candidate (--engine laya): the promoted Qwen adapter's grade on THIS holdout, read from the cache the qwen engine
    writes (bar-<run>-<hkey>.json) or, failing that, from the promoted run's own report when it graded the same number of holdout rows with the
    same inference settings. Never loads MLX here (the laya venv has none; only the 8B may sit beside the trainer). Returns (bar, source)."""
    cur = json.loads((Path(__file__).resolve().parent / "current.json").read_text())
    if not cur.get("adapter"):
        return None, None
    hkey = hashlib.sha1(("|".join(sorted(r["id"] for r, _ in holdout)) + f"|{fail_up}|{OUTWARD_AT}").encode()).hexdigest()[:12]
    cache_p = DATA / f"bar-{Path(cur['adapter']).parent.name}-{hkey}.json"
    if cache_p.exists():
        return json.loads(cache_p.read_text()), f"promoted adapter {cur['adapter']} re-graded on this holdout (cached {cache_p.name})"
    rep_p = Path(cur.get("report") or "")
    if rep_p.exists():
        rep = json.loads(rep_p.read_text())
        if rep.get("holdout_rows") == len(holdout) and rep.get("fail_up_below") == fail_up and rep.get("outward_at") == OUTWARD_AT and rep.get("candidate"):
            return rep["candidate"], f"promoted adapter's own report {rep_p} (same {len(holdout)} holdout rows, same fail-up/outward settings; not re-graded)"
    raise RuntimeError(f"no cached bar for {cur['adapter']} on this holdout ({cache_p.name}); grade it once with the qwen engine to create the cache")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=False); ap.add_argument("--adapter"); ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", action="store_true", help="also grade the incumbent engine (ollama 14B) on the holdout; slow, cached")
    ap.add_argument("--fail-up", type=float, default=float(__import__("os").environ.get("CORTADOS_S1_FAIL_UP", "0.6")),
                    help="grade what production runs: a difficulty feature whose top probability is below this takes the higher of its two most likely levels (0 = off)")
    ap.add_argument("--engine", choices=["qwen", "laya"], default="qwen", help="qwen (default: --base/--adapter, MLX) or laya (--checkpoint, torch/MPS; run from s1/.venv-laya; the laya candidate code is not published)")
    ap.add_argument("--checkpoint", help="--engine laya: a directory s1/laya/train.py wrote (or s1/laya/base for the zero-shot upstream checkpoint)")
    a = ap.parse_args()
    if a.engine == "qwen" and not a.base:
        ap.error("the following arguments are required: --base")
    if a.engine == "laya" and not a.checkpoint:
        ap.error("--engine laya requires --checkpoint <dir>")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    reqs = {r["id"]: r for r in read_jsonl(DATA / "requests.jsonl")}
    samples = defaultdict(list)
    for l in read_labels():
        samples[l["id"]].append(l["answers"])
    holdout = [(reqs[i], teacher_view(s)) for i, s in samples.items() if i in reqs and reqs[i]["split"] == "holdout"]
    valid = [(reqs[i], s) for i, s in samples.items() if i in reqs and reqs[i]["split"] == "train" and int(hashlib.sha1(("v" + i).encode()).hexdigest(), 16) % 10 == 0]
    if a.engine == "laya":
        sys.path.insert(0, str(Path(__file__).resolve().parent / "laya"))
        from predict import LayaScorer                    # duck-types Scorer: .T and .score(request, unit_kind) -> answers/probs/confidence/ms
        scorer = LayaScorer(a.checkpoint)
    else:
        scorer = Scorer(a.base, a.adapter)
    T = fit_temperatures(scorer, valid)
    scorer.T = T
    report = {"when": time.strftime("%Y-%m-%d %H:%M"), "base": a.base, "adapter": a.adapter, "engine": a.engine, "checkpoint": a.checkpoint,
              "temperatures": T, "holdout_rows": len(holdout), "valid_rows": len(valid),
              "teacher_band_mix": dict(Counter(tv["band"] for _, tv in holdout)),
              "fail_up_below": a.fail_up, "outward_at": OUTWARD_AT, "candidate": grade(lambda r: failed_up(scorer.score(r["request"], r["unit_kind"]), a.fail_up), holdout)}
    o_holdout = outcome_holdout()                              # the loop's held-out tasks; truth = the band they needed
    if o_holdout:
        report["outcome_holdout_rows"] = len(o_holdout)
        report["outcome"] = grade_outcome(lambda r: failed_up(scorer.score(r["request"], r["unit_kind"]), a.fail_up), o_holdout)
    if a.baseline:
        cache_p = DATA / "baseline-incumbent.jsonl"
        cache = {x["id"]: x for x in read_jsonl(cache_p)}

        def incumbent(r):
            if r["id"] not in cache:
                t0 = time.time()
                d = core.decide(r["request"], {"unit_kind": r["unit_kind"]})
                cache[r["id"]] = {"id": r["id"], "answers": d.get("answers"), "degraded": d.get("degraded"), "ms": int((time.time() - t0) * 1000)}
                with cache_p.open("a") as f:
                    f.write(json.dumps(cache[r["id"]]) + "\n")
            c = cache[r["id"]]
            if c.get("degraded") or not valid_answers(c.get("answers") or {}):
                return None                                   # the incumbent sometimes returns off-schema values (e.g. work_kind="interactive"): a miss
            return {"answers": c["answers"], "ms": c["ms"]}
        report["incumbent"] = {"engine": core.ENGINE_MODEL, **grade(incumbent, holdout)}
    c, i = report["candidate"], report.get("incumbent")
    # 2.0's promotion rule, carried over: better routing, and NO regression where mistakes are expensive (under-routing, outward actions)
    # the bar is the model currently PROMOTED (s1/current.json), not the old 14B: no regression on band accuracy (±1 request of noise allowed),
    # under-routing or outward recall. Falls back to the 14B when nothing has been promoted yet.
    # The bar is the promoted adapter graded on THIS holdout with THESE inference settings (cached per adapter+holdout+settings), never a
    # stored number from an older holdout: on 2026-09-26 stale numbers rejected a candidate that was better on the same rows.
    bar = None
    if a.engine == "laya":
        try:
            bar, src = promoted_bar_for_laya(holdout, a.fail_up)
            if src:
                report["bar_source"] = src
        except Exception as exc:
            report["bar_error"] = f"{type(exc).__name__}: {exc}"
    else:
        cur, inc = {}, None
        try:
            cur = json.loads((Path(__file__).resolve().parent / "current.json").read_text())
        except Exception as exc:
            report["bar_error"] = f"{type(exc).__name__}: {exc}"
        if cur.get("adapter") and Path(cur["adapter"]).exists() and cur["adapter"] != str(Path(a.adapter).resolve()) and cur["adapter"] != a.adapter:
            try:
                hkey = hashlib.sha1(("|".join(sorted(r["id"] for r, _ in holdout)) + f"|{a.fail_up}|{OUTWARD_AT}").encode()).hexdigest()[:12]
                cache_p = DATA / f"bar-{Path(cur['adapter']).parent.name}-{hkey}.json"
                if cache_p.exists():
                    bar = json.loads(cache_p.read_text())
                else:
                    inc = Scorer(cur["base"], cur["adapter"], cur.get("temperatures"))
                    bar = grade(lambda r: failed_up(inc.score(r["request"], r["unit_kind"]), a.fail_up), holdout)
                    cache_p.write_text(json.dumps(bar))
                report["bar_source"] = f"promoted adapter {cur['adapter']} re-graded on this holdout"
            except Exception as exc:
                report["bar_error"] = f"{type(exc).__name__}: {exc}"
            # The outcome bar is graded INDEPENDENTLY of the teacher-bar cache: a cached teacher bar must never let a
            # missing outcome bar skip the outcome no-regression check (Codex review 2026-10-25). A grading failure here
            # leaves no outcome bar, and promotion below is then blocked whenever outcome holdouts exist.
            if o_holdout:
                try:
                    ohkey = hashlib.sha1(("|".join(sorted(r["id"] for r, _ in o_holdout)) + f"|{a.fail_up}|{OUTWARD_AT}").encode()).hexdigest()[:12]
                    obar_p = DATA / f"outcome-bar-{Path(cur['adapter']).parent.name}-{ohkey}.json"
                    if obar_p.exists():
                        report["outcome_bar"] = json.loads(obar_p.read_text())
                    else:
                        inc = inc or Scorer(cur["base"], cur["adapter"], cur.get("temperatures"))
                        obar = grade_outcome(lambda r: failed_up(inc.score(r["request"], r["unit_kind"]), a.fail_up), o_holdout)
                        obar_p.write_text(json.dumps(obar))
                        report["outcome_bar"] = obar
                except Exception as exc:
                    report["outcome_bar_error"] = f"{type(exc).__name__}: {exc}"
    bar = bar or i
    report["bar"] = bar
    # Forest 2026-09-24: the right answer over cost (the rule lives in promote_decision, exercised directly in tests).
    # Auto-promote purely on the no-regression bar (Forest 2026-10-25): a candidate goes live only if it does not regress
    # against the CURRENTLY PROMOTED model on under-routing, outward recall, band accuracy AND the outcome band, so the
    # live scorer can never get worse. No hold-first/arm ceremony — the bar alone guards every promotion.
    report["promote"], report["outcome_regression_checked"] = promote_decision(
        c, bar, report.get("outcome_bar"), report.get("outcome"), bool(o_holdout))
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("holdout_rows", "outcome_holdout_rows", "teacher_band_mix", "candidate",
                                              "outcome", "outcome_bar", "incumbent", "promote") if k in report}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
