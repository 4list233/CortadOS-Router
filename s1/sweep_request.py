"""The pending calibration sweep request, data/sweep-request.json (#74): validation and the failure bound, for nightly.sh.
Stdlib only, no model: nightly.sh runs it with plain python3, and calibrate.py reuses check_grid so both refuse the same grids.

  python3 sweep_request.py validate <request>                   calibrate.py argv, one per line (exit 0); invalid: exit 2,
                                                                the reason on stderr (nightly.sh sets the request aside)
  python3 sweep_request.py failed <request> <night> <reason> <limit>   record a failed sweep night; exit 10 once <limit>
                                                                distinct nights have failed for this request (set it aside)
  python3 sweep_request.py mark <set-aside file> <kind> <when> <note>  record <kind>_at / <kind> (and the failed nights)
                                                                inside a request just moved aside; forget the failure record

Request: {"candidate": "runs/<c>/adapter", "live": "runs/<l>/adapter", "tilts": [...], "alphas": [...], "scales": [...]}.
Limits (reasons): tilts in [0, MAX_TILT] (a tilt compares probabilities, so a difference above 1 admits every level: it is a
floor, not a bias); alphas and scales in (0, 1] (0 would score the live adapter or the base model, which are not candidates;
above 1 extrapolates past the candidate); every value finite; tilts and alphas non-empty (scales may be empty: none unless
asked); at most MAX_GRID values per list and no duplicates (each alpha/scale is a whole model pass, ~1 h on the Studio);
paths non-empty strings of at most MAX_PATH characters without a newline (nightly.sh splits the argv on newlines)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

MAX_TILT = 1.0
MAX_GRID = 12
MAX_PATH = 4096
FAILURES = "sweep-request.failures.json"   # beside the request: {"request": sha256 of its bytes, "nights": {night: reason}}


def check_grid(tilts, alphas, scales) -> None:
    """Raises ValueError naming the first value outside the limits in the module doc."""
    for name, vals, lo_open, hi in (("tilts", tilts, False, MAX_TILT), ("alphas", alphas, True, 1.0), ("scales", scales, True, 1.0)):
        if not isinstance(vals, list) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals):
            raise ValueError(f"{name} must be a list of numbers")
        if name != "scales" and not vals:
            raise ValueError(f"{name} must not be empty")
        if len(vals) > MAX_GRID:
            raise ValueError(f"{name} has {len(vals)} values (at most {MAX_GRID})")
        if len({float(x) for x in vals}) != len(vals):
            raise ValueError(f"{name} has duplicate values")
        for x in vals:
            if not math.isfinite(x) or (x <= 0 if lo_open else x < 0) or x > hi:
                raise ValueError(f"{name} value {x} outside {'(0' if lo_open else '[0'}, {hi:g}]")


def validate(r) -> list[str]:
    """The calibrate.py argv for a request dict; ValueError when it is not runnable as given."""
    if not isinstance(r, dict):
        raise ValueError("request must be a JSON object")
    for k in ("candidate", "live"):
        v = r.get(k)
        if not isinstance(v, str) or not v.strip() or len(v) > MAX_PATH or "\n" in v or "\r" in v:
            raise ValueError(f"{k} must be a non-empty one-line path of at most {MAX_PATH} characters")
    want = {"tilts": r.get("tilts", [0]), "alphas": r.get("alphas", [1.0]), "scales": r.get("scales", [])}  # scales: none unless asked
    check_grid(want["tilts"], want["alphas"], want["scales"])
    out = Path(r["candidate"]).parent / "sweep.json"
    return ["--candidate", r["candidate"], "--live", r["live"], "--out", str(out),
            "--tilts", *map(str, want["tilts"]), "--alphas", *map(str, want["alphas"]), "--scales", *map(str, want["scales"])]


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def failed(req: Path, night: str, reason: str, limit: int) -> int:
    """Records one failed night for this request (keyed on its bytes: an edited or new request starts over). Several failed
    cycles in one night count once; a stopped night (window end) is not a failure and does not count. 10 = limit reached."""
    rec_path = req.with_name(FAILURES)
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    try:
        rec = json.loads(rec_path.read_text())
    except (OSError, ValueError):
        rec = None
    if not isinstance(rec, dict) or rec.get("request") != digest or not isinstance(rec.get("nights"), dict):
        rec = {"request": digest, "nights": {}}
    rec["nights"][night] = reason[:300]
    _write_atomic(rec_path, json.dumps(rec, indent=1) + "\n")
    print(f"calibration sweep failed on {len(rec['nights'])} of {limit} nights for this request")
    return 10 if len(rec["nights"]) >= limit else 0


def mark(dest: Path, kind: str, when: str, note: str) -> None:
    """Annotates a request just moved aside: <kind>_at, <kind> = note, and for "failed" the failed nights with their reasons.
    The failure record is removed for every kind (the request it counted is no longer pending)."""
    rec_path = dest.with_name(FAILURES)
    try:
        r = json.loads(dest.read_text())
    except ValueError:      # an unparseable request is kept byte for byte; the file name already says why it was set aside
        r = None
    if r is not None:
        if not isinstance(r, dict):
            r = {"request": r}
        r.update({f"{kind}_at": when, kind: note})
        if kind == "failed":
            try:
                r["failed_nights"] = json.loads(rec_path.read_text())["nights"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        _write_atomic(dest, json.dumps(r, indent=1) + "\n")
    rec_path.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "validate" and len(args) == 1:
        try:
            print("\n".join(validate(json.loads(Path(args[0]).read_text()))))
        except (OSError, ValueError) as e:
            print(f"sweep request invalid: {e}", file=sys.stderr)
            return 2
        return 0
    if cmd == "failed" and len(args) == 4:
        return failed(Path(args[0]), args[1], args[2], int(args[3]))
    if cmd == "mark" and len(args) == 4:
        mark(Path(args[0]), args[1], args[2], args[3])
        return 0
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
