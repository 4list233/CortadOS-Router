"""Promote one setting of a finished S1 calibration sweep (s1/calibrate.py) to the serving pointer, by hand, with a dry run.

calibrate.py never promotes and writes no report.json, so nightly_state.promote cannot publish a sweep setting. This helper
builds the pointer s1/serve.py reads from the sweep's own files and refuses anything the serving path cannot reproduce.

  python3 -B promote_sweep.py plan  --sweep <root>/runs/<c>/sweep.json --setting <version>-t<tilt> --pointer <current.json> [--write]
  python3 -B promote_sweep.py check --sweep <root>/runs/<c>/sweep.json --expect <setting dir> --requests <root>/data/requests.jsonl

plan (a dry run unless --write): prints the pointer it would write and every check it made. Refused (exit 3) when
  - the setting's tilt is not 0: serving applies no tilt (serve.py loads base + adapter + temperatures; core.decide_s1 applies only
    fail-up and the outward threshold), so a tilted setting would be published as its untilted twin. The refusal names that twin
    and its own promotion report, so the owner can approve it explicitly;
  - the setting's promotion_report (evaluate.promotion_report, recorded in sweep.json) did not promote;
  - the live pointer is not the one the sweep graded against (fingerprint.inference.live_pointer: adapter, base, temperatures);
  - the candidate or live weights changed since the sweep (calibrate.file_id against the fingerprint);
  - the version's cache is incomplete or inconsistent (scores.done, scores.meta.json built from those weights, temps.json fitted
    on the same validation rows as the scores, a blend file written before its scoring began).
--write copies the version's adapter (adapters.safetensors + adapter_config.json) into <run>/promoted/<run>-<version>/adapter,
read-only: the sweep dir is rebuilt in place by a later sweep of the same candidate, and evaluate.py keys the incumbent bar cache
by the adapter's parent dir name, which must not be shared with any other adapter. Then it writes the pointer atomically and
keeps the previous one as <pointer>.prev (nightly_state.write_atomic, the same as a nightly promotion). serve.py reloads on the
pointer's mtime change; no restart.

--fs-prefix DIR (dry runs against copies): every absolute path read from sweep.json and the pointer (weights, the live
adapter) is opened under DIR, a mirror of the real tree; the pointer it prints keeps the real paths.

check: scores the holdout rows of one sweep setting dir (e.g. `live` before, `blend0.7-t0` after) through the LIVE scorer the way
the router does (core.decide_s1: CORTADOS_S1_URL / CORTADOS_S1_TOKEN from the environment, fail-up, outward threshold; usage
refresh forced off, so no vendor CLI is opened) and
compares each band with the band the sweep recorded for that row, by id. Up to --per-band rows per predicted band. Prints ids,
bands and latency only, never request text. Exit 0 when every band matches and the median latency is within --budget-s of the
sweep's live median; 1 otherwise; 2 when it cannot run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

EXIT_REFUSED = 3


class Refused(Exception):
    """The setting cannot be published as asked; the message says why."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise Refused(f"missing {path}")
    except ValueError as exc:
        raise Refused(f"{path} is not JSON ({exc})")


def version_of(setting: str) -> str:
    """'blend0.7-t0.3' -> 'blend0.7' (calibrate.run_sweep names settings f'{version}-t{tilt:g}')."""
    if "-t" not in setting:
        raise Refused(f"{setting!r} is not a sweep setting name (<version>-t<tilt>)")
    return setting.rsplit("-t", 1)[0]


def find_row(sweep: dict, setting: str) -> dict:
    for r in sweep.get("settings") or []:
        if r.get("setting") == setting:
            return r
    names = ", ".join(r.get("setting", "?") for r in sweep.get("settings") or [])
    raise Refused(f"no setting {setting!r} in the sweep (has: {names})")


def headline(r: dict) -> str:
    t, o, rep = r.get("teacher") or {}, r.get("outcome") or {}, r.get("report") or {}
    return (f"{r['setting']}: teacher acc {t.get('band_accuracy')} under {t.get('under_routed')} over {t.get('over_routed')} "
            f"outward {t.get('outward_recall')}; outcome acc {o.get('band_accuracy', '-')} under {o.get('under_routed', '-')} "
            f"over {o.get('over_routed', '-')}; would promote: {rep.get('promote')}")


def same_weights(now: dict, want: dict) -> bool:
    """calibrate.weights_id equality on everything but the path (the path is compared by the caller, before any mirror)."""
    return not now.get("missing") and {k: v for k, v in now.items() if k != "path"} == {k: v for k, v in want.items() if k != "path"}


def plan(sweep_path: Path, setting: str, pointer_path: Path, dest: Path | None = None, now: str | None = None,
         fs_prefix: Path | None = None) -> dict:
    """Everything --write would do, checked, as {"pointer", "source", "dest", "checks", "notes"}. Writes nothing.
    fs_prefix: open the absolute paths named inside sweep.json / the pointer under this mirror (dry runs against copies)."""
    import calibrate as cal

    def to_fs(p) -> Path:
        return Path(str(fs_prefix) + str(p)) if fs_prefix else Path(p)

    def real(p: Path) -> str:
        s = str(p)
        return s[len(str(fs_prefix)):] if fs_prefix and s.startswith(str(fs_prefix)) else s
    sweep_path = Path(sweep_path).resolve()
    run_dir = sweep_path.parent                       # <root>/runs/<run>
    root = run_dir.parent.parent                      # paths in sweep.json are relative to the train root (nightly.sh's cwd)
    sweep = _read(sweep_path)
    fp = sweep.get("fingerprint")
    if not isinstance(fp, dict):
        raise Refused(f"{sweep_path} has no input fingerprint (a sweep before CALIBRATE_VERSION 2): rerun the sweep")
    checks, notes = [], []
    row = find_row(sweep, setting)
    version = version_of(setting)
    if float(row.get("tilt") or 0) != 0:
        twin = next((r for r in sweep["settings"] if r.get("setting") == f"{version}-t0"), None)
        msg = (f"{setting} has tilt {row['tilt']:g}, and serving applies no tilt: s1/serve.py loads base + adapter + temperatures "
               f"only, and core.decide_s1 applies only fail-up and the outward threshold. Published, it would serve as {version}-t0.")
        if twin:
            msg += f" That setting's own grade: {headline(twin)}. Approve it by name to publish it."
        raise Refused(msg)
    checks.append(f"tilt 0: serving reproduces this setting ({headline(row)})")
    rep = row.get("report") or {}
    if rep.get("promote") is not True:
        failing = ", ".join(c["check"] for c in rep.get("checks", []) if not c.get("ok")) or rep.get("reason")
        raise Refused(f"{setting} does not pass evaluate.promotion_report ({failing})")
    checks.append(f"promotion_report: promote=True under rule {rep.get('rule')!r} ({rep.get('reason')})")

    cur = _read(pointer_path)
    graded = (fp.get("inference") or {}).get("live_pointer") or {}
    for k in ("adapter", "base", "temperatures"):
        if cur.get(k) != graded.get(k):
            raise Refused(f"the live pointer's {k} is not the one the sweep graded against "
                          f"(now {cur.get(k)!r}, sweep {graded.get(k)!r}): the sweep's comparison no longer holds")
    checks.append(f"live pointer unchanged since the sweep (adapter {cur['adapter']})")
    if cur.get("task_context_contract"):
        notes.append("the current pointer declares task_context_contract; the new one does not (a nightly promotion drops it too)")

    cand_w = Path(fp["candidate_weights"]["path"])
    live_w = Path(fp["live_weights"]["path"])
    for name, path, want in (("candidate", cand_w, fp["candidate_weights"]), ("live", live_w, fp["live_weights"])):
        if str(path) != want.get("path") or not same_weights(cal.weights_id(to_fs(path)), want):
            raise Refused(f"the {name} weights {path} changed since the sweep (size/mtime/first-last MiB hash differ)")
    checks.append("candidate and live weights identical to the sweep's fingerprint")

    vdir = run_dir / "sweep" / version
    meta = _read(vdir / "scores.meta.json")
    if not (vdir / "scores.done").is_file():
        raise Refused(f"{vdir}/scores.done is missing: the {version} scores are incomplete")
    if meta.get("candidate") != fp["candidate_weights"]:
        raise Refused(f"{vdir}/scores.meta.json was scored from other candidate weights than the sweep's fingerprint")
    blend = row.get("blend")
    if blend and blend[0] == "blend" and meta.get("live") != fp["live_weights"]:
        raise Refused(f"{vdir}/scores.meta.json was blended with other live weights than the sweep's fingerprint")
    temps = _read(vdir / "temps.json")
    if not (isinstance(temps.get("T"), dict) and temps["T"]):
        raise Refused(f"{vdir}/temps.json has no fitted temperatures")
    if temps.get("valid") != meta.get("valid") or temps.get("valid") != fp.get("valid"):
        raise Refused(f"{vdir}/temps.json was fitted on other validation rows than the scores and the sweep")
    checks.append(f"{version}: scores complete, built from the fingerprinted weights, temperatures fitted on the same valid rows")

    source = (root / row["adapter"]) if not Path(row["adapter"]).is_absolute() else Path(row["adapter"])
    src_w, src_c = source / "adapters.safetensors", source / "adapter_config.json"
    for p in (src_w, src_c):
        if not p.is_file():
            raise Refused(f"missing {p}")
    if blend and src_w.stat().st_mtime_ns > (vdir / "scores.meta.json").stat().st_mtime_ns:
        raise Refused(f"{src_w} was rewritten after its scoring began: it is not the adapter the sweep graded")
    cfg_src = _read(src_c)
    if cfg_src.get("model") != fp["inference"]["base"]:
        raise Refused(f"{src_c} names base {cfg_src.get('model')!r}, the sweep graded {fp['inference']['base']!r}")
    rank = (cfg_src.get("lora_parameters") or {}).get("rank")
    w_sha, c_sha = sha256(src_w), sha256(src_c)
    checks.append(f"source adapter {source} (LoRA rank {rank}; adapters.safetensors sha256 {w_sha[:16]}...)")

    dest = Path(dest) if dest else run_dir / "promoted" / f"{run_dir.name}-{version}" / "adapter"
    if dest.exists():
        same = all((dest / n).is_file() and sha256(dest / n) == s for n, s in (("adapters.safetensors", w_sha), ("adapter_config.json", c_sha)))
        if not same:
            raise Refused(f"{dest} exists with other content; move it aside or pass another --dest")
        notes.append(f"{dest} already holds this adapter (byte-identical); it is reused")
    bar_glob = sorted((root / "data").glob(f"bar-{dest.parent.name}-*.json"))
    if bar_glob:
        notes.append(f"incumbent bar caches already named {dest.parent.name}: {[p.name for p in bar_glob]} (evaluate.py reuses them)")
    live_cfg = _read(to_fs(live_w.parent / "adapter_config.json"))
    live_rank = (live_cfg.get("lora_parameters") or {}).get("rank")
    if rank != live_rank:
        notes.append(f"LoRA rank {rank} differs from the live adapter's {live_rank}: nightly.sh warm-starts the next new run from the "
                     f"promoted adapter (CORTADOS_S1_INIT) while training at mlx_lm's default rank; mlx_lm resumes with "
                     f"load_weights(strict=False), which does not check shapes. See the runbook before training resumes.")

    t = row["teacher"]
    pointer = {
        "base": fp["inference"]["base"],
        "adapter": real(dest.resolve() if dest.exists() else dest.absolute()),
        "temperatures": temps["T"],
        "promoted": now or time.strftime("%Y-%m-%d %H:%M"),
        "report": real(sweep_path),
        "inference": {"fail_up_below": sweep.get("fail_up"), "outward_at": fp["inference"].get("outward_at")},
        "holdout": {k: t[k] for k in ("band_accuracy", "under_routed", "over_routed", "outward_recall")},
        "holdout_rows": sweep.get("holdout_rows"),
        "sweep": {"setting": setting, "version": version, "tilt": row.get("tilt"), "blend": blend,
                  "source": real(source), "adapter_sha256": w_sha, "adapter_config_sha256": c_sha,
                  "outcome": row.get("outcome"), "promotion_rule": rep.get("rule"), "chosen_by_sweep": sweep.get("chosen")},
    }
    return {"pointer": pointer, "source": str(source), "dest": str(dest), "checks": checks, "notes": notes,
            "sha": {"adapters.safetensors": w_sha, "adapter_config.json": c_sha}}


def write(p: dict, pointer_path: Path) -> None:
    """Copy the adapter (verified, read-only) and publish the pointer, keeping <pointer>.prev. Called only after plan()."""
    from nightly_state import write_atomic
    src, dest = Path(p["source"]), Path(p["dest"])
    if not dest.exists():
        tmp = dest.parent / f".{dest.name}.tmp-{os.getpid()}"
        tmp.mkdir(parents=True)
        for n in ("adapters.safetensors", "adapter_config.json"):
            shutil.copy2(src / n, tmp / n)
            if sha256(tmp / n) != p["sha"][n]:
                shutil.rmtree(tmp)
                raise Refused(f"copy of {n} does not match its source; nothing published")
            os.chmod(tmp / n, 0o444)
        os.replace(tmp, dest)
    pointer = dict(p["pointer"], adapter=str(dest.resolve()))
    pointer_path = Path(pointer_path)
    if pointer_path.exists():
        write_atomic(pointer_path.with_name(pointer_path.name + ".prev"), pointer_path.read_text())
    write_atomic(pointer_path, json.dumps(pointer, indent=1) + "\n")


# --- check -------------------------------------------------------------------------------------------------------------
def pick_rows(rows: list[dict], per_band: int) -> list[dict]:
    """Up to per_band rows per predicted band, lowest ids first (deterministic, spread across bands)."""
    out, seen = [], {}
    for r in sorted(rows, key=lambda x: x["id"]):
        b = r.get("pred_band")
        if b and seen.get(b, 0) < per_band:
            seen[b] = seen.get(b, 0) + 1
            out.append(r)
    return out


def run_check(rows: list[dict], requests: dict, decide, band_of, *, live_median_ms: float | None, budget_s: float,
              out=print) -> int:
    """decide(request, unit_kind) -> core.decide_s1-shaped dict; band_of(answers) -> band. Prints no request text."""
    lat, bad, n = [], 0, 0
    for r in rows:
        q = requests.get(r["id"])
        if not q:
            out(f"  {r['id']}: not in requests.jsonl; skipped")
            continue
        d = decide(q["request"], r.get("unit_kind") or q.get("unit_kind") or "interactive")
        n += 1
        if not d or d.get("degraded") or not d.get("answers"):
            bad += 1
            out(f"  {r['id']}: expected {r['pred_band']}, scorer DEGRADED ({(d or {}).get('error')})")
            continue
        band = band_of(d["answers"])
        lat.append(d.get("engine_ms") or 0)
        same_answers = all(d["answers"].get(k) == v for k, v in (r.get("pred_answers") or {}).items())
        ok = band == r["pred_band"]
        bad += 0 if ok else 1
        out(f"  {r['id']}: expected {r['pred_band']}, got {band} {'ok' if ok else 'MISMATCH'}"
            f"{'' if same_answers else ' (answers differ from the sweep row)'}; {d.get('engine_ms')} ms")
    if not n:
        out("check: no row could be scored")
        return 2
    med = statistics.median(lat) if lat else None
    lat_ok = med is not None and (live_median_ms is None or med <= live_median_ms + budget_s * 1000)
    out(f"check: {n - bad}/{n} bands match; median latency {med} ms "
        f"(sweep live median {live_median_ms} ms, budget +{budget_s:g} s): {'ok' if lat_ok else 'OVER BUDGET'}")
    return 0 if bad == 0 and lat_ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--sweep", required=True); p.add_argument("--setting", required=True); p.add_argument("--pointer", required=True)
    p.add_argument("--dest"); p.add_argument("--write", action="store_true")
    p.add_argument("--fs-prefix", help="dry runs against copies: open absolute paths from sweep.json/the pointer under this mirror")
    c = sub.add_parser("check")
    c.add_argument("--sweep", required=True); c.add_argument("--expect", required=True)
    c.add_argument("--requests", required=True); c.add_argument("--per-band", type=int, default=2)
    c.add_argument("--budget-s", type=float, default=float(os.environ.get("CORTADOS_S1_LATENCY_BUDGET_S", "1.0")))
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        try:
            if a.write and a.fs_prefix:
                print("REFUSED: --fs-prefix is for dry runs against copies; --write publishes only real paths")
                return EXIT_REFUSED
            pl = plan(Path(a.sweep), a.setting, Path(a.pointer), Path(a.dest) if a.dest else None,
                      fs_prefix=Path(a.fs_prefix).resolve() if a.fs_prefix else None)
            print("checks:"); [print(f"  ok  {x}") for x in pl["checks"]]
            for x in pl["notes"]:
                print(f"  NOTE {x}")
            print(f"copy: {pl['source']} -> {pl['dest']}")
            print(f"pointer {'written to' if a.write else 'that WOULD be written to'} {a.pointer}:")
            print(json.dumps(pl["pointer"], indent=1))
            if a.write:
                write(pl, Path(a.pointer))
                print(f"WROTE {a.pointer} (previous kept as {a.pointer}.prev); serve.py reloads on its next request")
            else:
                print("dry run: nothing written")
        except Refused as exc:
            print(f"REFUSED: {exc}")
            return EXIT_REFUSED
        return 0
    # core.resolve may sample vendor usage (opens the claude/codex/cursor/agy CLIs) when a sample is due; the band never needs it.
    os.environ["CORTADOS_SCARCITY_REFRESH"] = "off"
    sys.path.insert(0, str(HERE.parent))
    from cortados_router import core
    sweep_path = Path(a.sweep).resolve()
    sweep = json.loads(sweep_path.read_text())
    rows_p = sweep_path.parent / "sweep" / a.expect / "holdout-rows.jsonl"
    try:
        rows = [json.loads(x) for x in rows_p.read_text().splitlines() if x.strip()]
        reqs = {}
        for x in Path(a.requests).read_text().splitlines():
            if x.strip():
                o = json.loads(x)
                reqs[o["id"]] = o
    except (OSError, ValueError) as exc:
        print(f"check: cannot read inputs ({exc})")
        return 2
    core.lane_health = lambda **_: {}

    def band_of(ans):
        return core.resolve({"answers": ans, "degraded": False, "engine": "check", "engine_ms": 0})["band"]
    print(f"check: {a.expect} rows from {rows_p} against the scorer at {'CORTADOS_S1_URL' if os.environ.get('CORTADOS_S1_URL') else 'the default URL'}")
    return run_check(pick_rows(rows, a.per_band), reqs, core.decide_s1, band_of,
                     live_median_ms=(sweep.get("live_teacher") or {}).get("latency_ms_median"), budget_s=a.budget_s)


if __name__ == "__main__":
    sys.exit(main())
