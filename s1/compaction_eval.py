"""Evaluate the compaction keep heads (and, side by side, the phase-1 deterministic rules) on labelled test rows.

scorer(row, head) -> P(yes) for head in {"call", "result"} is injected (the real one wraps a Scorer on the compaction
adapter; tests pass a fake). Rows are test-rows.jsonl from compaction_dataset.py.

Per head: AUC, Brier score, a 5-bin calibration table. At each threshold t in THRESHOLDS (keep when p >= t):
chars_saved (result chars of dropped results + input chars of dropped calls), wrong_drop_rate (dropped results that
were needed / dropped results), missed_needed (needed results dropped / needed results) and the decision mix
(keep call+result, call only, drop both). The rules get the same decision metrics from their KeepDecisions.
`switch_on` lists thresholds where neither head drops more needed items than the rules (results AND calls, counted
separately) and at least as many chars are saved; it is empty with "insufficient evidence" below MIN_ROWS rows or
MIN_NEEDED needed results. The heads replace the rules only where they are better, never on AUC alone.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

THRESHOLDS = (0.3, 0.5, 0.7)
BINS = 5
HEADS = {"call": "needed_call", "result": "needed_result"}
# Switch-on needs enough evidence: below MIN_ROWS one event moves a rate by more than 2 points, and with fewer than
# MIN_NEEDED needed results a "0 wrong drops" says nothing (an empty eval would otherwise approve every threshold).
MIN_ROWS = 50
MIN_NEEDED = 5


def sufficient(rows: list[dict]) -> bool:
    return len(rows) >= MIN_ROWS and sum(bool(r["needed_result"]) for r in rows) >= MIN_NEEDED


def auc(scores: list[float], ys: list[bool]) -> Optional[float]:
    """Mann-Whitney AUC with ties counted half; None when a class is absent."""
    pos = [s for s, y in zip(scores, ys) if y]
    neg = [s for s, y in zip(scores, ys) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def brier(scores, ys) -> Optional[float]:
    return sum((s - y) ** 2 for s, y in zip(scores, ys)) / len(ys) if ys else None


def calibration(scores, ys, bins: int = BINS) -> list[dict[str, Any]]:
    table = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores) if lo <= s < hi or (b == bins - 1 and s == 1.0)]
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                      "mean_p": round(sum(scores[i] for i in idx) / len(idx), 4) if idx else None,
                      "frac_yes": round(sum(ys[i] for i in idx) / len(idx), 4) if idx else None})
    return table


def decision_metrics(rows: list[dict], keep: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Savings and the safety metric of EACH head: a dropped needed result (wrong_drops) and a dropped needed call
    (wrong_call_drops, e.g. forgetting that an edit or a commit happened) are counted separately, so cheap call drops
    cannot hide behind a clean result head."""
    saved = dropped = wrong = needed = 0
    cdropped = cwrong = cneeded = 0
    mix = {"keep_call_and_result": 0, "keep_call_only": 0, "drop_both": 0}
    for r, (kc, kr) in zip(rows, keep):
        kc = kc or kr          # a kept result implies the call is kept
        needed += r["needed_result"]
        cneeded += r["needed_call"]
        if not kr:
            dropped += 1
            saved += r.get("result_chars", 0)
            wrong += r["needed_result"]
        if not kc:
            cdropped += 1
            saved += r.get("input_chars", 0)
            cwrong += r["needed_call"]
        mix["keep_call_and_result" if kr else "keep_call_only" if kc else "drop_both"] += 1
    return {"chars_saved": saved, "dropped_results": dropped, "wrong_drops": wrong,
            "wrong_drop_rate": round(wrong / dropped, 4) if dropped else 0.0,
            "missed_needed": round(wrong / needed, 4) if needed else 0.0,
            "dropped_calls": cdropped, "wrong_call_drops": cwrong,
            "wrong_call_drop_rate": round(cwrong / cdropped, 4) if cdropped else 0.0,
            "missed_needed_call": round(cwrong / cneeded, 4) if cneeded else 0.0, "mix": mix}


def _key(d) -> tuple[str, int]:
    if isinstance(d, Mapping):
        return d["session"], int(d["event_index"])
    return d.session, int(d.event_index)


def _keeps(d) -> tuple[bool, bool]:
    if isinstance(d, Mapping):
        return bool(d["keep_call"]), bool(d["keep_result"])
    return bool(d.keep_call), bool(d.keep_result)


def evaluate(rows: list[dict], scorer: Callable[[dict, str], float],
             rule_decisions: Optional[Iterable[Any]] = None) -> dict[str, Any]:
    p = {h: [float(scorer(r, h)) for r in rows] for h in HEADS}
    rep: dict[str, Any] = {"n": len(rows), "heads": {}, "thresholds": {}}
    for h, key in HEADS.items():
        ys = [bool(r[key]) for r in rows]
        rep["heads"][h] = {"auc": auc(p[h], ys), "brier": brier(p[h], ys), "positives": sum(ys),
                           "calibration": calibration(p[h], ys)}
    for t in THRESHOLDS:
        rep["thresholds"][str(t)] = decision_metrics(rows, [(pc >= t, pr >= t) for pc, pr in zip(p["call"], p["result"])])
    if rule_decisions is not None:
        by = {_key(d): _keeps(d) for d in rule_decisions}
        covered = [(r, by[(r["session"], r["event_index"])]) for r in rows if (r["session"], r["event_index"]) in by]
        rules = decision_metrics([r for r, _ in covered], [k for _, k in covered])
        rules["covered"] = len(covered)
        rep["rules"] = rules
        if not sufficient(rows):
            rep["switch_on"] = []
            rep["switch_on_note"] = (f"insufficient evidence: {len(rows)} rows, {sum(r['needed_result'] for r in rows)} "
                                     f"needed results (need >= {MIN_ROWS} and >= {MIN_NEEDED})")
        elif len(covered) == len(rows):
            # Same rows, same denominators: comparing counts compares the heads' miss rates. Neither head may drop
            # more needed items than the rules, and the heads must save at least as many chars.
            rep["switch_on"] = [t for t, m in rep["thresholds"].items()
                                if m["wrong_drops"] <= rules["wrong_drops"]
                                and m["wrong_call_drops"] <= rules["wrong_call_drops"]
                                and m["chars_saved"] >= rules["chars_saved"]]
        else:
            rep["switch_on"] = []
            rep["switch_on_note"] = "rules did not decide every test row; no comparison"
    return rep


def markdown(rep: dict[str, Any]) -> str:
    L = [f"# Compaction keep heads: eval ({rep['n']} test events)", ""]
    for h, m in rep["heads"].items():
        a = "n/a" if m["auc"] is None else f"{m['auc']:.3f}"
        b = "n/a" if m["brier"] is None else f"{m['brier']:.3f}"
        L += [f"## head `{h}`: AUC {a}, Brier {b}, positives {m['positives']}", "", "| bin | n | mean p | frac yes |",
              "|---|---|---|---|"]
        L += [f"| {c['bin']} | {c['n']} | {c['mean_p']} | {c['frac_yes']} |" for c in m["calibration"]]
        L.append("")
    L += ["## Decisions", "", "| who | chars saved | wrong result drops (rate) | wrong call drops (rate) | "
          "keep both / call only / drop |", "|---|---|---|---|---|"]
    rowsm = [(f"S1 t={t}", m) for t, m in rep["thresholds"].items()] + ([("rules", rep["rules"])] if "rules" in rep else [])
    for who, m in rowsm:
        x = m["mix"]
        L.append(f"| {who} | {m['chars_saved']} | {m['wrong_drops']} ({m['wrong_drop_rate']}) | "
                 f"{m['wrong_call_drops']} ({m['wrong_call_drop_rate']}) | "
                 f"{x['keep_call_and_result']} / {x['keep_call_only']} / {x['drop_both']} |")
    if "switch_on" in rep:
        L += ["", f"Heads beat the rules at thresholds: {', '.join(rep['switch_on']) or 'none'}"]
        if rep.get("switch_on_note"):
            L += ["", rep["switch_on_note"]]
    return "\n".join(L) + "\n"


def write(rep: dict[str, Any], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n")
    (out / "eval.md").write_text(markdown(rep))


def main(argv=None) -> int:   # pragma: no cover - needs mlx and an adapter
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rows", type=Path, required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--rules", type=Path, help="JSONL of KeepDecision dicts from the phase-1 rules")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from compaction_dataset import example
    from mlx_lm import load, generate  # noqa: F401  (imported here: mlx is only on the Studio)
    raise SystemExit("live scoring wrapper not wired yet: score rows with example(row['state'], head) on the adapter")


if __name__ == "__main__":
    sys.exit(main())
