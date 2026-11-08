"""Evaluation-only calibration sweep for an S1 candidate adapter: can the candidate be rebalanced toward fewer under-routes
WITHOUT retraining? Two knobs, graded on both holdouts with evaluate.py's own grading and promotion rule (never duplicated here):

  (A) tilt t: a post-hoc bias toward the higher level of each difficulty feature on close calls (see tilt_answers).
  (B) blend alpha: the candidate's LoRA delta interpolated with the live adapter's (see blend_adapters); also candidate-only
      LoRA scale s (alpha on the candidate alone, the live side's share is zero).

  ../.venv-mlx/bin/python calibrate.py --candidate runs/<c>/adapter --live runs/<l>/adapter --out runs/<c>/sweep.json \
      [--tilts 0 0.05 0.1] [--alphas 1.0 0.85] [--scales 0.75 0.5]

Selection discipline: the chosen setting is picked on the TEACHER holdout only (select_on_teacher); the outcome holdout is
then used only to confirm it with evaluate.promotion_report, unchanged. This script NEVER promotes and never writes the
serving pointer: it writes sweep.json, sweep.md beside it, and blended adapters under <run>/sweep/<setting>/ only."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Grids. Reasons: tilt 0 is the untouched candidate (the reference row); 0.3 is the largest tilt that still needs the higher level
# to hold a fair share of the mass (p_hi >= p_top - 0.3); beyond it the tilt stops being a "close call" bias and becomes a floor.
DEFAULT_TILTS = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]
# alpha 1.0 is the candidate itself; 0.5 is the midpoint. Below 0.5 the result is mostly the live adapter, which is already graded.
DEFAULT_ALPHAS = [1.0, 0.85, 0.7, 0.5]
# Candidate-only LoRA scale: the same delta shrunk toward the base model; two points are enough to see the direction (owner ask).
DEFAULT_SCALES = [0.75, 0.5]


# --- (A) tilt -----------------------------------------------------------------------------------------------------------
def tilt_answers(p: dict, t: float, features: list[str]) -> dict:
    """Applied to one scored request AFTER evaluate.failed_up (the production rounding), BEFORE the band is computed.

    Where and why: the band is a monotone function of the difficulty features (core.resolve sums them into D and floors
    blast_radius), so biasing each feature's chosen level upward is the one place a "lean to the higher band" bias can act
    without touching the table or the hard rules. Per feature, the answer becomes the HIGHEST level whose probability is
    within t of the top probability (p[i] >= max(p) - t), never lower than the answer already chosen. Monotone by
    construction: a larger t admits a superset of levels, so the chosen level (and therefore D and the band) never falls.
    t = 0 is a no-op. Only difficulty features are tilted; outward keeps evaluate's OUTWARD_AT rule (a hard-rule input).
    Limitation: Scorer.score conditions later questions on earlier argmax answers; the tilt does not re-run that chain."""
    if t < 0:
        raise ValueError(f"tilt must be >= 0, got {t}")
    if not t:
        return p
    probs = p.get("probs") or {}
    ans = dict(p["answers"])
    for f in features:
        pr = probs.get(f) or []
        if not pr:
            continue
        top = max(pr)
        hi = max(i for i, x in enumerate(pr) if x >= top - t)
        ans[f] = max(int(ans[f]), hi)
    return {**p, "answers": ans}


# --- (B) blend ----------------------------------------------------------------------------------------------------------
class BlendError(ValueError):
    """The two adapters cannot be blended (different base, layers, keys or LoRA layout). Raised, never papered over."""


def blend_plan(cand_cfg: dict, live_cfg: dict) -> dict:
    """Checks that both adapters were trained on the same base with the same layer layout; returns the merged config.
    Ranks MAY differ: the blend concatenates along the rank axis (see blend_tensors), so the output rank is r_c + r_l."""
    for k in ("model", "num_layers", "fine_tune_type"):
        if cand_cfg.get(k) != live_cfg.get(k):
            raise BlendError(f"adapters differ on {k}: candidate {cand_cfg.get(k)!r} vs live {live_cfg.get(k)!r}")
    lc, ll = cand_cfg.get("lora_parameters") or {}, live_cfg.get("lora_parameters") or {}
    if not (lc.get("rank") and ll.get("rank") and lc.get("scale") and ll.get("scale")):
        raise BlendError("adapter_config.json lacks lora_parameters.rank/scale on one side")
    if (lc.get("keys") or None) != (ll.get("keys") or None):
        raise BlendError(f"adapters target different modules: {lc.get('keys')!r} vs {ll.get('keys')!r}")
    out = json.loads(json.dumps(cand_cfg))
    out["lora_parameters"]["rank"] = int(lc["rank"]) + int(ll["rank"])
    return out


def blend_tensors(cand: dict, live: dict | None, alpha: float, cand_scale: float, live_scale: float, concat) -> dict:
    """Exact interpolation of the effective LoRA delta: delta = alpha*delta_c + (1-alpha)*delta_l, delta = scale * A@B
    (mlx_lm: lora_a is (in, r), lora_b is (r, out)). Averaging A and B separately would NOT give that (A@B is bilinear: it
    adds cross terms A_c@B_l), so the ranks are stacked instead: A' = [A_c | A_l], B' = [w_c*B_c ; w_l*B_l], giving
    A'@B' = w_c*A_c@B_c + w_l*A_l@B_l exactly, at rank r_c + r_l, which also handles different ranks. The output uses the
    candidate's scale, so each side's weight folds in its scale ratio. live=None is candidate-only scaling (B_c * alpha).
    `concat(a, b, axis)` is the array library's concatenate (mx.concatenate in production; a list version in tests)."""
    if live is None:
        return {k: (v * alpha if k.endswith("lora_b") else v) for k, v in cand.items()}
    if set(cand) != set(live):
        diff = sorted(set(cand) ^ set(live))
        raise BlendError(f"adapters have different tensor keys ({len(diff)} differ, e.g. {diff[:3]})")
    w_c, w_l = alpha, (1.0 - alpha) * live_scale / cand_scale
    out = {}
    for k in cand:
        if k.endswith("lora_a"):
            out[k] = concat(cand[k], live[k], -1)
        elif k.endswith("lora_b"):
            out[k] = concat(cand[k] * w_c, live[k] * w_l, 0)
        else:
            raise BlendError(f"unexpected tensor {k!r}: only lora_a / lora_b are blended")
    return out


def blend_adapters(cand_dir: Path, live_dir: Path | None, alpha: float, out_dir: Path) -> Path:
    """Writes a blended adapter (adapters.safetensors + adapter_config.json) into out_dir; MLX only, imported here."""
    import mlx.core as mx
    cand_cfg = json.loads((cand_dir / "adapter_config.json").read_text())
    cand = mx.load(str(cand_dir / "adapters.safetensors"))
    if live_dir is None:
        cfg, live, ls = cand_cfg, None, 1.0
    else:
        live_cfg = json.loads((live_dir / "adapter_config.json").read_text())
        cfg, live, ls = blend_plan(cand_cfg, live_cfg), mx.load(str(live_dir / "adapters.safetensors")), live_cfg["lora_parameters"]["scale"]
    t = blend_tensors(cand, live, alpha, cand_cfg["lora_parameters"]["scale"], ls, lambda a, b, ax: mx.concatenate([a, b], axis=ax))
    out_dir.mkdir(parents=True, exist_ok=True)
    mx.save_safetensors(str(out_dir / "adapters.safetensors"), t)
    (out_dir / "adapter_config.json").write_text(json.dumps(cfg, indent=1))
    return out_dir


# --- selection ----------------------------------------------------------------------------------------------------------
def select_on_teacher(rows: list[dict], live_teacher: dict, max_under_rise: float, outward_floor: float) -> str | None:
    """The setting chosen on the TEACHER holdout ONLY (outcome numbers are never read here): among settings whose teacher
    under-routing rises at most max_under_rise over live and whose outward recall holds the floor, the highest teacher band
    accuracy; ties go to the lower under-routing, then the name. None when no setting qualifies."""
    ok = [r for r in rows if r["teacher"]["under_routed"] - live_teacher["under_routed"] <= max_under_rise + 1e-9
          and r["teacher"]["outward_recall"] >= outward_floor - 1e-9]
    if not ok:
        return None
    return min(ok, key=lambda r: (-r["teacher"]["band_accuracy"], r["teacher"]["under_routed"], r["setting"]))["setting"]


def markdown(sweep: dict) -> str:
    lines = [f"# S1 calibration sweep {sweep['when']}", "",
             f"candidate `{sweep['candidate']}` vs live `{sweep['live']}`. Selection used the TEACHER columns only "
             f"(marked *); outcome columns confirm. Chosen: **{sweep['chosen'] or 'none qualified'}**.", "",
             "| setting | teacher acc* | teacher under* | teacher over* | outward recall* | outcome acc | outcome under | outcome over | would promote | failing check |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in [{"setting": "LIVE", "teacher": sweep["live_teacher"], "outcome": sweep["live_outcome"], "report": None}] + sweep["settings"]:
        t, o, rep = r["teacher"], r.get("outcome") or {}, r.get("report")
        fail = ", ".join(c["check"] for c in (rep or {}).get("checks", []) if not c["ok"]) or ((rep or {}).get("reason") if rep and not rep["promote"] else "")
        lines.append(f"| {r['setting']}{' (chosen)' if r['setting'] == sweep['chosen'] else ''} | {t['band_accuracy']} | {t['under_routed']} | "
                     f"{t['over_routed']} | {t['outward_recall']} | {o.get('band_accuracy', '-')} | {o.get('under_routed', '-')} | "
                     f"{o.get('over_routed', '-')} | {'-' if rep is None else ('yes' if rep['promote'] else 'no')} | {fail} |")
    return "\n".join(lines) + "\n"


def run_sweep(*, candidate: str, live: str, tilts: list[float], alphas: list[float], scales: list[float], out: Path,
              holdout: list, o_holdout: list, make_scorer, fail_up: float) -> dict:
    """make_scorer(adapter_dir) -> object with .score(request, unit_kind) (temperatures already fitted). Each adapter is
    scored once per holdout row; the tilts reuse those scores. Grading and the promotion rule are evaluate.py's."""
    import evaluate as ev
    from common import core
    run_dir = out.parent
    memo: dict[str, dict] = {}

    def scored(adapter: str, r: dict) -> dict:
        key = f"{adapter}|{r['id']}"
        if key not in memo:
            memo[key] = ev.failed_up(scorers[adapter].score(r["request"], r["unit_kind"]), fail_up)
        return memo[key]

    scorers = {live: make_scorer(live)}
    rows_dir = run_dir / "sweep"     # per-setting row files: <run>/sweep/<setting>/{holdout,outcome}-rows.jsonl, joined by id
    live_teacher = ev.grade(lambda r: scored(live, r), holdout, rows_out=rows_dir / "live" / "holdout-rows.jsonl")
    live_outcome = (ev.grade_outcome(lambda r: scored(live, r), o_holdout, rows_out=rows_dir / "live" / "outcome-rows.jsonl")
                    if o_holdout else None)
    variants = [("cand", candidate, None)]
    for a in alphas:
        if a != 1.0:
            variants.append((f"blend{a:g}", None, ("blend", a)))
    for s in scales:
        variants.append((f"scale{s:g}", None, ("scale", s)))
    rows = []
    for name, adapter, how in variants:
        if how:
            dest = run_dir / "sweep" / name
            adapter = str(blend_adapters(Path(candidate), Path(live) if how[0] == "blend" else None, how[1], dest))
        scorers[adapter] = make_scorer(adapter)
        for t in tilts:
            pred = (lambda ad, tt: lambda r: tilt_answers(scored(ad, r), tt, core.FEATURES))(adapter, t)
            setting = f"{name}-t{t:g}"
            rows.append({"setting": setting, "adapter": adapter, "tilt": t, "blend": how,
                         "teacher": ev.grade(pred, holdout, rows_out=rows_dir / setting / "holdout-rows.jsonl"),
                         "outcome": (ev.grade_outcome(pred, o_holdout, rows_out=rows_dir / setting / "outcome-rows.jsonl")
                                     if o_holdout else None)})
    chosen = select_on_teacher(rows, live_teacher, ev.TEACHER_UNDER_MAX_RISE, ev.OUTWARD_RECALL_FLOOR)
    for r in rows:   # confirmation only: the rule is evaluate.promotion_report, unchanged; nothing is promoted
        r["report"] = ev.promotion_report(r["teacher"], live_teacher, live_outcome, r["outcome"], bool(o_holdout), holdout_rows=len(holdout))
    sweep = {"when": time.strftime("%Y-%m-%d %H:%M"), "candidate": candidate, "live": live, "fail_up": fail_up,
             "selection": "teacher holdout only (select_on_teacher); outcome holdout used only to confirm via promotion_report",
             "holdout_rows": len(holdout), "outcome_holdout_rows": len(o_holdout), "live_teacher": live_teacher,
             "live_outcome": live_outcome, "chosen": chosen, "settings": rows,
             "chosen_would_promote": next((r["report"]["promote"] for r in rows if r["setting"] == chosen), None)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sweep, indent=1))
    out.with_suffix(".md").write_text(markdown(sweep))
    return sweep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", required=True); ap.add_argument("--live", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=os.environ.get("CORTADOS_S1_BASE", "mlx-community/Qwen3-4B-Instruct-2507-4bit"))
    ap.add_argument("--tilts", type=float, nargs="*", default=DEFAULT_TILTS)
    ap.add_argument("--alphas", type=float, nargs="*", default=DEFAULT_ALPHAS)
    ap.add_argument("--scales", type=float, nargs="*", default=DEFAULT_SCALES)
    ap.add_argument("--fail-up", type=float, default=float(os.environ.get("CORTADOS_S1_FAIL_UP", "0.6")))
    a = ap.parse_args(argv)
    for x in a.alphas + a.scales:
        if not 0.0 <= x <= 1.0:
            ap.error(f"alpha/scale {x} outside [0, 1]")
    import evaluate as ev
    from common import DATA, read_jsonl, read_labels
    from student import Scorer
    reqs = {r["id"]: r for r in read_jsonl(DATA / "requests.jsonl")}
    holdout, valid = ev.build_eval_sets(reqs, read_labels())

    def make_scorer(adapter: str):
        s = Scorer(a.base, adapter)
        s.T = ev.fit_temperatures(s, valid)          # what evaluate.py does for every adapter it grades
        return s
    sweep = run_sweep(candidate=a.candidate, live=a.live, tilts=a.tilts, alphas=a.alphas, scales=a.scales, out=Path(a.out),
                      holdout=holdout, o_holdout=ev.outcome_holdout(), make_scorer=make_scorer, fail_up=a.fail_up)
    print(f"sweep: {len(sweep['settings'])} settings; chosen on teacher: {sweep['chosen']}; would promote: {sweep['chosen_would_promote']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
