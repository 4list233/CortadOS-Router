"""Evaluation-only calibration sweep for an S1 candidate adapter: can the candidate be rebalanced toward fewer under-routes
WITHOUT retraining? Two knobs, graded on both holdouts with evaluate.py's own grading and promotion rule (never duplicated here):

  (A) tilt t: a post-hoc bias toward the higher level of each difficulty feature on close calls (see tilt_answers).
  (B) blend alpha: the candidate's LoRA delta interpolated with the live adapter's (see blend_adapters); also candidate-only
      LoRA scale s (alpha on the candidate alone, the live side's share is zero).

  ../.venv-mlx/bin/python calibrate.py --candidate runs/<c>/adapter --live runs/<l>/adapter --out runs/<c>/sweep.json \
      [--tilts 0 0.05 0.1] [--alphas 1.0 0.85] [--scales 0.75 0.5]

Selection discipline: the chosen setting is picked on the TEACHER holdout only (select_on_teacher); the outcome holdout is
then used only to confirm it with evaluate.promotion_report, unchanged. This script NEVER promotes and never writes the
serving pointer: it writes sweep.json, sweep.md beside it, and blended adapters under <run>/sweep/<setting>/ only.

Candidate weights (resolve_candidate): <candidate>/adapters.safetensors when mlx_lm wrote its final file; otherwise the
highest-numbered step checkpoint NNNNNNN_adapters.safetensors in the same dir (never 0000000, the warm-start copy of the live
adapter), logged with the run's segment-target. The candidate dir is never written: a checkpoint is loaded through
<run>/sweep/cand-weights/, two symlinks (adapter_config.json, adapters.safetensors -> the checkpoint). Neither file -> exit 4.
The chosen weights must resolve (realpath) to a file inside the candidate adapter dir that is not the 0000000 warm-start copy:
a symlink leading out of the run, or onto 0000000, is refused (exit 4), never loaded.

Resumable, one model at a time (#74): each adapter version (live, cand, blend<a>, scale<s>) is scored once per holdout row and
every scored row is appended to <run>/sweep/<version>/scores.jsonl as it completes (scores.done when the version is complete,
temps.json once its temperatures are fitted). A rerun reuses every cached row whose version identity still matches and scores
only the missing ones. The identity: the weights files (resolved path, size, mtime, hash of the first and last MiB) and each
adapter's adapter_config.json hash, the blend/scale parameters, base, fail-up/outward thresholds, live temperatures, and for
every candidate version (whose temperatures are fitted) the hash of the validation rows. temps.json carries that validation
hash too and is refitted when it differs. A changed identity moves the version's cache (scores, done marker, temps) aside
into <version>/stale-<stamp>/ and starts that version over. A torn last line (a kill mid-write) is cut off on load, so the
next append starts on a fresh line. Each model is released (gc + MLX cache cleared) before the next is loaded, so only one
is ever resident. The tilts reuse the cached scores. sweep.md then sweep.json are written atomically (tmp + rename) only when
every version is complete, so an existing sweep.json always describes a finished sweep.

sweep.json carries "fingerprint", every input the result depends on (fingerprint()): the request (candidate, live, tilts,
alphas, scales), the candidate weights and both adapter configs, the teacher/outcome holdouts, the validation rows, the
inference thresholds and live pointer, and CALIBRATE_VERSION. `--check-done` (same arguments, reads files only, never
loads a model or writes anything) exits 10 when <out> exists with the fingerprint the arguments give now, else 0 (the
sweep must run; the cache decides what is reused). nightly.sh marks a request done without a run only on 10.
Exit codes: 0 done, 2 arguments rejected (argparse, or a grid outside sweep_request.check_grid), 4 refused (bad candidate),
10 --check-done: already complete, else failed."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# Grids. Reasons: tilt 0 is the untouched candidate (the reference row); 0.3 is the largest tilt that still needs the higher level
# to hold a fair share of the mass (p_hi >= p_top - 0.3); beyond it the tilt stops being a "close call" bias and becomes a floor.
DEFAULT_TILTS = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]
# alpha 1.0 is the candidate itself; 0.5 is the midpoint. Below 0.5 the result is mostly the live adapter, which is already graded.
DEFAULT_ALPHAS = [1.0, 0.85, 0.7, 0.5]
# Candidate-only LoRA scale: the same delta shrunk toward the base model; two points are enough to see the direction (owner ask).
# Suggested points only: --scales defaults to none (#74: every scale is a whole extra model pass, ~1 h on the Studio), so a sweep
# request lists the scales it wants.
DEFAULT_SCALES = [0.75, 0.5]
EXIT_REFUSED = 4          # the request cannot be run as given (no candidate weights); nightly.sh sets it aside, never retries it
EXIT_ALREADY_DONE = 10    # --check-done: <out> is a finished sweep of exactly these inputs
# Bumped whenever what calibrate.py computes for the same inputs changes (grading, selection, sweep.json layout): a sweep.json
# from an older version is then not accepted as complete. 2: fingerprint + keyed caches (#74 review).
CALIBRATE_VERSION = 2


# --- candidate weights --------------------------------------------------------------------------------------------------
class CandidateError(ValueError):
    """The candidate adapter dir has neither a final adapters.safetensors nor a step checkpoint (or lacks its config)."""


CHECKPOINT_RE = re.compile(r"^(\d{7})_adapters\.safetensors$")


def resolve_candidate(cand_dir: Path) -> tuple[Path, str]:
    """(weights file, log note) for the candidate. The final adapters.safetensors when present; else the highest-numbered
    NNNNNNN_adapters.safetensors step checkpoint (0000000 is the warm-start copy of the promoted adapter, not a trained step,
    so it never counts). The note names the file, its step and the run's segment-target / iters-base when recorded."""
    cand_dir = Path(cand_dir)
    if not (cand_dir / "adapter_config.json").is_file():
        raise CandidateError(f"candidate {cand_dir} has no adapter_config.json")
    final = cand_dir / "adapters.safetensors"
    if final.is_file():
        check_weights(cand_dir, final)
        return final, f"candidate weights: {final} (final adapter)"
    steps = sorted((int(m[1]), p) for p in cand_dir.iterdir() if (m := CHECKPOINT_RE.match(p.name)) and int(m[1]) > 0 and p.is_file())
    if not steps:
        raise CandidateError(f"candidate {cand_dir} has no adapters.safetensors and no NNNNNNN_adapters.safetensors step "
                             "checkpoint (0000000 is the warm-start copy and is not used)")
    step, path = steps[-1]
    check_weights(cand_dir, path)
    run = cand_dir.parent
    target = (run / "segment-target").read_text().strip() if (run / "segment-target").is_file() else ""
    base = (run / "iters-base").read_text().strip() if (run / "iters-base").is_file() else ""
    rec = f"run segment-target {target or 'not recorded'}"
    if base.isdigit() and int(base):
        rec += f", iters-base {int(base)} (cumulative step {int(base) + step})"
    if target.isdigit() and int(base or 0) + step < int(target):
        rec += "; the segment did NOT reach its target"
    return path, f"candidate {cand_dir} has no adapters.safetensors; using checkpoint {path.name} (step {step}; {rec})"


def check_weights(cand_dir: Path, weights: Path) -> Path:
    """The weights file resolved (realpath, every symlink followed); CandidateError unless it lies inside the candidate adapter
    dir (also resolved) and is not the 0000000 warm-start copy. A checkpoint symlinked out of the run, or onto the warm-start
    copy, would sweep some other adapter under this candidate's name."""
    real, root = Path(os.path.realpath(weights)), Path(os.path.realpath(cand_dir))
    if root not in real.parents:
        raise CandidateError(f"candidate weights {weights} resolve to {real}, outside the candidate adapter dir {root}")
    m = CHECKPOINT_RE.match(real.name)
    if m and int(m[1]) == 0:
        raise CandidateError(f"candidate weights {weights} resolve to the warm-start copy {real.name}")
    if not real.is_file():
        raise CandidateError(f"candidate weights {weights} resolve to {real}, which is not a file")
    return real


def candidate_view(cand_dir: Path, weights: Path, view: Path) -> Path:
    """The adapter dir to load: cand_dir itself for its final file, else `view` holding two symlinks (config + the checkpoint)
    so mlx_lm reads the checkpoint in place. Nothing is ever written into cand_dir. The weights are checked (check_weights)
    first; the view links to the resolved file."""
    real = check_weights(cand_dir, weights)
    if weights == Path(cand_dir) / "adapters.safetensors":
        return Path(cand_dir)
    view.mkdir(parents=True, exist_ok=True)
    for name, target in (("adapter_config.json", Path(cand_dir).resolve() / "adapter_config.json"),
                         ("adapters.safetensors", real)):
        link = view / name
        if link.is_symlink() or link.exists():
            if link.is_symlink() and Path(os.readlink(link)) == target:
                continue
            link.unlink()
        link.symlink_to(target)
    return view


ENDS = 1 << 20   # file_id hashes this many bytes at each end: a rewrite with the same size and a restored mtime still changes it


def file_id(path: Path) -> dict:
    """Identity of a weights file: resolved path, size, mtime and a sha256 of its first and last MiB (the whole file for
    small ones). A retrained / replaced file changes it; reading 2 MiB keeps it cheap for multi-GB adapters."""
    p = Path(path).resolve()
    st = p.stat()
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(ENDS))
        if st.st_size > ENDS:
            f.seek(max(ENDS, st.st_size - ENDS))
            h.update(f.read(ENDS))
    return {"path": str(p), "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ends_sha256": h.hexdigest()}


def file_hash(path: Path) -> str | None:
    """sha256 of a small file's content (adapter_config.json), None when it is missing."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def data_hash(obj) -> str:
    """sha256 of rows (holdouts, validation set) in a canonical JSON form; order-independent at the top level."""
    rows = sorted(json.dumps(x, sort_keys=True, default=str) for x in obj)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def weights_id(path: Path) -> dict:
    """file_id, or {"path", "missing": True} when there is no such file (the run then fails or refuses on its own)."""
    return file_id(path) if Path(path).is_file() else {"path": str(path), "missing": True}


def release_model() -> None:
    """Free the model just dropped: collect it, then MLX's buffer cache (so the next model does not stack on top)."""
    gc.collect()
    try:
        import mlx.core as mx
    except ImportError:
        return
    for clear in (getattr(mx, "clear_cache", None), getattr(getattr(mx, "metal", None), "clear_cache", None)):
        if clear:
            clear()
            return


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


def scorer_factory(*, live: str, base: str, valid: list, pointer: dict):
    """make_scorer for run_sweep. The LIVE adapter is built exactly as evaluate.py grades the incumbent
    (evaluate.incumbent_scorer: the pointer's base and FROZEN serving temperatures); refitting it here could cross the
    fail-up / outward thresholds, change the live bands and so corrupt selection and chosen_would_promote. Only candidate
    variants (candidate, blends, scales) get temperatures fitted on the valid split, as evaluate.py does for a candidate.
    The pointer must name the same adapter as --live: a sweep against some other baseline is refused, not guessed."""
    import evaluate as ev
    if not pointer.get("adapter") or Path(pointer["adapter"]).resolve() != Path(live).resolve():
        raise ValueError(f"--live {live!r} is not the promoted adapter {pointer.get('adapter')!r} in {ev.current_pointer()}")

    fit = data_hash(valid)

    def make_scorer(adapter: str, temps: Path | None = None):
        """temps: the version's temps.json, {"valid": <validation rows hash>, "T": {...}}. Fitted temperatures are saved there
        (atomic) and reused on a resumed sweep only while the validation rows are the same, so a restart does not pay the
        ~22 min fit again. The live adapter never has any: its temperatures are the pointer's."""
        if Path(adapter).resolve() == Path(live).resolve():
            return ev.incumbent_scorer(pointer)
        s = ev.Scorer(base, adapter)
        cached = _read_json(temps) if temps else None
        if isinstance(cached, dict) and cached.get("valid") == fit and isinstance(cached.get("T"), dict) and cached["T"]:
            s.T = cached["T"]
        else:
            s.T = ev.fit_temperatures(s, valid)          # what evaluate.py does for the candidate it grades
            if temps:
                write_atomic(temps, json.dumps({"valid": fit, "T": s.T}))
        return s
    return make_scorer


# --- resumable score cache ----------------------------------------------------------------------------------------------
def write_atomic(path: Path, text: str) -> None:
    """tmp + fsync + rename in the same dir: a reader sees the old file or the whole new one, never a torn write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def row_key(kind: str, r: dict) -> str:
    """Cache key of one scored row: holdout kind ('h' teacher / 'o' outcome), id, and a hash of what the model reads (request
    text + unit kind), so an edited request is rescored. Teacher labels are not in it: they change the grade, not the score."""
    h = hashlib.sha1(f"{r.get('unit_kind')}\0{r['request']}".encode()).hexdigest()[:16]
    return f"{kind}:{r['id']}:{h}"


class ScoreCache:
    """<run>/sweep/<version>/: scores.meta.json (the version identity), scores.jsonl (one {"k", "p"} line per scored row,
    appended and fsynced as each row finishes), scores.done (written once every requested row is cached) and temps.json.
    A different identity moves the version's cache (meta, scores, done, temps) aside into stale-<stamp>/ before anything is
    reused: the dependents go with the scores, so no file of the old identity is read again."""

    def __init__(self, vdir: Path, identity: dict, log=print):
        self.dir, self.identity, self.log = Path(vdir), identity, log
        self.scores, self.meta, self.done = self.dir / "scores.jsonl", self.dir / "scores.meta.json", self.dir / "scores.done"
        self.temps = self.dir / "temps.json"
        self.rows: dict[str, dict] = {}
        if _read_json(self.meta) == identity:
            self._load()
        elif any(p.exists() for p in (self.meta, self.scores, self.done, self.temps)):
            stale = self.dir / time.strftime("stale-%Y%m%d-%H%M%S")
            n = 0
            while stale.exists():
                n += 1
                stale = self.dir / (time.strftime("stale-%Y%m%d-%H%M%S") + f"-{n}")
            stale.mkdir(parents=True)
            for p in (self.meta, self.scores, self.done, self.temps):
                if p.exists():
                    os.replace(p, stale / p.name)
            self.log(f"sweep: {self.dir.name}: cached scores are for another adapter/settings/data; moved aside to "
                     f"{stale.name}, starting this version over")

    def _load(self) -> None:
        try:
            data = self.scores.read_bytes()
        except FileNotFoundError:
            return
        end = data.rfind(b"\n") + 1
        if end < len(data):    # a kill mid-write left a torn last line: cut it off so the next append starts a fresh line
            with open(self.scores, "r+b") as f:
                f.truncate(end)
                f.flush()
                os.fsync(f.fileno())
            data = data[:end]
        for line in data.decode(errors="replace").splitlines():     # a damaged line is dropped (rescored), never trusted
            try:
                o = json.loads(line)
                self.rows[o["k"]] = o["p"]
            except (ValueError, KeyError, TypeError):
                continue

    def complete(self, keys: list[str]) -> bool:
        return all(k in self.rows for k in keys)

    def begin(self) -> None:
        write_atomic(self.meta, json.dumps(self.identity, sort_keys=True))
        self.done.unlink(missing_ok=True)
        self._fh = open(self.scores, "a")

    def add(self, key: str, p: dict) -> None:
        self.rows[key] = p
        self._fh.write(json.dumps({"k": key, "p": p}, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def finish(self, n: int) -> None:
        fh = getattr(self, "_fh", None)
        if fh:
            fh.close()
            self._fh = None
        write_atomic(self.done, json.dumps({"rows": n, "when": time.strftime("%Y-%m-%d %H:%M:%S")}))

    def close(self) -> None:
        fh = getattr(self, "_fh", None)
        if fh:
            fh.close()
            self._fh = None


def variants_for(alphas: list[float], scales: list[float]) -> list[tuple[str, tuple | None]]:
    """The adapter versions a sweep scores, in order: (name, how). The live and candidate come first."""
    out: list[tuple[str, tuple | None]] = [("live", None), ("cand", None)]
    out += [(f"blend{a:g}", ("blend", a)) for a in alphas if a != 1.0]
    out += [(f"scale{s:g}", ("scale", s)) for s in scales]
    return out


def run_sweep(*, candidate: str, live: str, tilts: list[float], alphas: list[float], scales: list[float], out: Path,
              holdout: list, o_holdout: list, make_scorer, fail_up: float, identity: dict | None = None,
              fit_identity: dict | None = None, fingerprint: dict | None = None,
              candidate_file: str | None = None, release=release_model, log=lambda m: None) -> dict:
    """make_scorer(adapter_dir, temps_path) -> object with .score(request, unit_kind) (temperatures already fitted, or read
    from temps_path). Each adapter version is scored once per holdout row into its ScoreCache; the model is then dropped and
    `release` frees it before the next version loads, so one model is resident at a time. A version whose cache already holds
    every row is not loaded at all. The tilts and all grading read the cached scores. Grading and the promotion rule are
    evaluate.py's. `identity` (base, thresholds, live temperatures) joins each version's cache identity; `fit_identity` (the
    validation rows hash) joins only the candidate versions', whose temperatures are fitted. `fingerprint` (see fingerprint())
    is written into sweep.json for nightly.sh's --check-done."""
    import evaluate as ev
    from common import core
    run_dir = out.parent
    rows_dir = run_dir / "sweep"     # per-setting row files: <run>/sweep/<setting>/{holdout,outcome}-rows.jsonl, joined by id
    common_id = {"fail_up": fail_up, "outward_at": ev.OUTWARD_AT, **(identity or {})}
    live_id = {"live": weights_id(Path(live) / "adapters.safetensors"), "live_config": file_hash(Path(live) / "adapter_config.json")}
    cand_w = Path(candidate_file) if candidate_file else Path(candidate) / "adapters.safetensors"
    cand_id = {"candidate": weights_id(cand_w), "candidate_config": file_hash(Path(candidate) / "adapter_config.json"),
               **(fit_identity or {})}
    wanted = [("h", r) for r, _ in holdout] + [("o", r) for r, _ in o_holdout]
    keys = [row_key(kind, r) for kind, r in wanted]
    scores: dict[str, ScoreCache] = {}
    adapters: dict[str, str] = {}
    for name, how in variants_for(alphas, scales):
        vdir = rows_dir / name
        weights = dict(live_id) if name == "live" else dict(cand_id)
        if how and how[0] == "blend":
            weights.update(live_id)
        cache = ScoreCache(vdir, {"version": name, "how": list(how) if how else None, **weights, **common_id}, log=log)
        scores[name] = cache
        adapter = live if name == "live" else (candidate if how is None else str(vdir))
        adapters[name] = adapter
        missing = [(k, r) for k, (_, r) in zip(keys, wanted) if k not in cache.rows]
        if not missing and cache.done.exists():
            log(f"sweep: {name}: all {len(keys)} rows cached; not loaded")
            continue
        if missing:
            log(f"sweep: {name}: {len(keys) - len(missing)} of {len(keys)} rows cached; scoring {len(missing)}")
            if how:   # blends are rebuilt (seconds) before scoring: a write cut by a stop must not be loaded
                blend_adapters(Path(candidate), Path(live) if how[0] == "blend" else None, how[1], vdir)
            cache.begin()
            scorer = None
            try:
                scorer = make_scorer(adapter, None if name == "live" else cache.temps)
                for k, r in missing:
                    cache.add(k, ev.failed_up(scorer.score(r["request"], r["unit_kind"]), fail_up))
            finally:
                cache.close()
                scorer = None           # the one reference: the model goes before the next version loads
                release()
        else:
            cache.begin()
        cache.finish(len(keys))

    def pred_for(name: str, kind: str, t: float):
        rows = scores[name].rows
        return lambda r: tilt_answers(rows[row_key(kind, r)], t, core.FEATURES)

    live_teacher = ev.grade(pred_for("live", "h", 0), holdout, rows_out=rows_dir / "live" / "holdout-rows.jsonl")
    live_outcome = (ev.grade_outcome(pred_for("live", "o", 0), o_holdout, rows_out=rows_dir / "live" / "outcome-rows.jsonl")
                    if o_holdout else None)
    rows = []
    for name, how in variants_for(alphas, scales)[1:]:
        for t in tilts:
            setting = f"{name}-t{t:g}"
            rows.append({"setting": setting, "adapter": adapters[name], "tilt": t, "blend": how,
                         "teacher": ev.grade(pred_for(name, "h", t), holdout, rows_out=rows_dir / setting / "holdout-rows.jsonl"),
                         "outcome": (ev.grade_outcome(pred_for(name, "o", t), o_holdout, rows_out=rows_dir / setting / "outcome-rows.jsonl")
                                     if o_holdout else None)})
    chosen = select_on_teacher(rows, live_teacher, ev.TEACHER_UNDER_MAX_RISE, ev.OUTWARD_RECALL_FLOOR)
    for r in rows:   # confirmation only: the rule is evaluate.promotion_report, unchanged; nothing is promoted
        r["report"] = ev.promotion_report(r["teacher"], live_teacher, live_outcome, r["outcome"], bool(o_holdout), holdout_rows=len(holdout))
    sweep = {"when": time.strftime("%Y-%m-%d %H:%M"), "candidate": candidate, "candidate_file": str(cand_w), "live": live,
             "fail_up": fail_up, "request": {"tilts": tilts, "alphas": alphas, "scales": scales},
             "selection": "teacher holdout only (select_on_teacher); outcome holdout used only to confirm via promotion_report",
             "holdout_rows": len(holdout), "outcome_holdout_rows": len(o_holdout), "live_teacher": live_teacher,
             "live_outcome": live_outcome, "chosen": chosen, "settings": rows,
             "chosen_would_promote": next((r["report"]["promote"] for r in rows if r["setting"] == chosen), None),
             "fingerprint": fingerprint}
    write_atomic(out.with_suffix(".md"), markdown(sweep))
    write_atomic(out, json.dumps(sweep, indent=1))      # last: an existing sweep.json means the sweep finished
    return sweep


def fingerprint(*, candidate: str, weights: Path, live: str, base: str, fail_up: float, tilts: list[float], alphas: list[float],
                scales: list[float], holdout: list, o_holdout: list, valid: list, pointer: dict) -> dict:
    """Every input a finished sweep.json depends on. A completed sweep is accepted (--check-done) only when this is equal now:
    the request as given; the resolved candidate weights (file_id: path, size, mtime, first/last MiB hash) and both adapter
    configs; the live weights; the teacher holdout (rows + teacher truth), the outcome holdout and the validation rows the
    candidate temperatures are fitted on; the inference thresholds, the grading/promotion constants and the live pointer
    (base, frozen temperatures); and CALIBRATE_VERSION."""
    import evaluate as ev
    return {
        "calibrate_version": CALIBRATE_VERSION,
        "request": {"candidate": candidate, "live": live, "tilts": [float(x) for x in tilts],
                    "alphas": [float(x) for x in alphas], "scales": [float(x) for x in scales]},
        "candidate_weights": weights_id(weights),
        "candidate_config": file_hash(Path(candidate) / "adapter_config.json"),
        "live_weights": weights_id(Path(live) / "adapters.safetensors"),
        "live_config": file_hash(Path(live) / "adapter_config.json"),
        "holdout": data_hash([[r, tv] for r, tv in holdout]),
        "outcome_holdout": data_hash([[r, b] for r, b in o_holdout]),
        "valid": data_hash(valid),
        "inference": {"base": base, "fail_up": fail_up, "outward_at": ev.OUTWARD_AT,
                      "teacher_under_max_rise": ev.TEACHER_UNDER_MAX_RISE, "outward_recall_floor": ev.OUTWARD_RECALL_FLOOR,
                      "min_outcome_holdout": ev.MIN_OUTCOME_HOLDOUT, "live_pointer": {
                          "adapter": pointer.get("adapter"), "base": pointer.get("base"),
                          "temperatures": pointer.get("temperatures")}},
    }


def check_done(out: Path, fp: dict) -> tuple[bool, str]:
    """(True, note) when <out> is a finished sweep with exactly this fingerprint; else (False, which inputs differ)."""
    done = _read_json(out)
    if not isinstance(done, dict):
        return False, f"no finished sweep at {out}"
    old = done.get("fingerprint")
    if not isinstance(old, dict):
        return False, f"{out} has no input fingerprint (written before it was recorded)"
    diff = sorted(k for k in set(fp) | set(old) if fp.get(k) != old.get(k))
    if diff:
        return False, f"{out} was computed from other inputs ({', '.join(diff)} changed)"
    return True, f"{out} is a finished sweep of exactly these inputs"


def main(argv: list[str] | None = None) -> int:
    from sweep_request import check_grid
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", required=True); ap.add_argument("--live", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=os.environ.get("CORTADOS_S1_BASE", "mlx-community/Qwen3-4B-Instruct-2507-4bit"))
    ap.add_argument("--tilts", type=float, nargs="*", default=DEFAULT_TILTS)
    ap.add_argument("--alphas", type=float, nargs="*", default=DEFAULT_ALPHAS)
    ap.add_argument("--scales", type=float, nargs="*", default=[])   # none unless asked (#74); DEFAULT_SCALES are suggestions
    ap.add_argument("--fail-up", type=float, default=float(os.environ.get("CORTADOS_S1_FAIL_UP", "0.6")))
    ap.add_argument("--check-done", action="store_true",
                    help="exit 10 when --out is a finished sweep of exactly these inputs, else 0; loads no model, writes nothing")
    a = ap.parse_args(argv)
    try:     # the same limits nightly.sh's request validation applies (NaN, out-of-range, empty, oversized grids)
        check_grid(a.tilts, a.alphas, a.scales)
    except ValueError as e:
        ap.error(str(e))
    out = Path(a.out)
    try:
        weights, note = resolve_candidate(Path(a.candidate))
        candidate = a.candidate if a.check_done else str(candidate_view(Path(a.candidate), weights, out.parent / "sweep" / "cand-weights"))
    except (CandidateError, OSError) as e:
        print(f"sweep refused: {e}", flush=True)
        return EXIT_REFUSED
    print(note, flush=True)
    import evaluate as ev
    from common import DATA, read_jsonl, read_labels
    reqs = {r["id"]: r for r in read_jsonl(DATA / "requests.jsonl")}
    holdout, valid = ev.build_eval_sets(reqs, read_labels())
    o_holdout = ev.outcome_holdout()

    cur = json.loads(ev.current_pointer().read_text())   # unreadable or missing pointer: let it raise; there is no live baseline without it
    fp = fingerprint(candidate=a.candidate, weights=weights, live=a.live, base=a.base, fail_up=a.fail_up, tilts=a.tilts,
                     alphas=a.alphas, scales=a.scales, holdout=holdout, o_holdout=o_holdout, valid=valid, pointer=cur)
    if a.check_done:
        ok, why = check_done(out, fp)
        print(f"sweep check: {why}", flush=True)
        return EXIT_ALREADY_DONE if ok else 0
    make_scorer = scorer_factory(live=a.live, base=a.base, valid=valid, pointer=cur)
    identity = {"base": a.base, "live_base": cur.get("base"), "live_temperatures": cur.get("temperatures")}
    sweep = run_sweep(candidate=candidate, live=a.live, tilts=a.tilts, alphas=a.alphas, scales=a.scales, out=out,
                      holdout=holdout, o_holdout=o_holdout, make_scorer=make_scorer, fail_up=a.fail_up,
                      identity=identity, fit_identity={"valid": data_hash(valid)}, fingerprint=fp,
                      candidate_file=str(weights), log=lambda m: print(m, flush=True))
    print(f"sweep: {len(sweep['settings'])} settings; chosen on teacher: {sweep['chosen']}; would promote: {sweep['chosen_would_promote']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
