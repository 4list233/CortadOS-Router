"""Small state helpers for s1/nightly.sh (stdlib only, so the nightly never depends on the training venv for bookkeeping).

  labels <labels.jsonl>                       print the number of usable Claude (id, teacher, sample) labels (0 when missing); a corrupt
                                              line exits non-zero so the nightly stops instead of recording a bogus count
  fingerprint <file>...                       print one sha256 over the files (the SFT data training reads); a missing file exits 1
  growth <status.json> <count> <stall-nights> [<night YYYY-MM-DD>]
                                              record tonight's label count; print ALERT line when labels have not grown for
                                              <stall-nights> consecutive nights (distinct dates; the dashboard reads the same file)
  streak <status.json> <name> hit|clear <night> <threshold> [<detail>]
                                              consecutive-night tracker (label early stops, evaluation failures); prints an ALERT
                                              line at <threshold> nights and keeps it in status["alerts"][<name>]
  event <status.json> <key> [<detail>]        stamp last_<key> (train, promotion, labeling_failure, skip) with now + detail
  promote <report.json> <adapter dir> <pointer>
                                              write the promoted pointer the scorer reads (s1/serve.py reloads on mtime change),
                                              atomically (temp + rename), keeping the previous one as <pointer>.prev for rollback

Every write is temp + os.replace in the target directory: a reader (serve.py, the dashboard) sees the old file or the new
one, never a half-written one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_status(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:   # a corrupt status must be visible, not silently reset; keep the bad copy
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%dT%H%M%S')}")   # never overwrite an earlier one
        os.replace(path, backup)
        print(f"nightly_state: {path} was not JSON ({exc}); moved to {backup.name} and started fresh", file=sys.stderr)
        return {}


def save_status(path: Path, st: dict) -> None:
    write_atomic(path, json.dumps(st, indent=1, sort_keys=True) + "\n")


def labels(path: Path) -> int:
    """Usable teacher labels: distinct Claude (id, teacher, sample) triples, the same filter training applies
    (common.is_claude_teacher, which common.read_labels uses). Counting ids would miss a Claude backfill of ids a Codex
    teacher labelled before, and show it as a stall."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import is_claude_teacher       # one definition of "usable teacher"; loads the router's core (stdlib)
    if not path.exists():
        return 0
    keys = set()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n}: not JSON ({exc})") from None
        if not isinstance(row, dict) or not row.get("id"):
            raise ValueError(f"{path}:{n}: no id")
        if is_claude_teacher(row.get("teacher")):
            keys.add((row["id"], row["teacher"], row.get("sample")))
    return len(keys)


def fingerprint(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")   # FileNotFoundError → non-zero exit
    return h.hexdigest()[:16]


def growth(path: Path, count: int, stall_nights: int, night: str | None = None) -> str:
    """Track label growth per NIGHT (a calendar date), not per invocation: a manual rerun on the same night must not count
    as a second stalled night, and a night that grew at any of its runs is not a stalled night."""
    st = load_status(path)
    prev = st.get("last_label_count")
    stamp = now()
    night = night or datetime.now().date().isoformat()
    if prev is None or count > prev:
        st.update(last_label_growth=stamp, nights_without_growth=0, last_growth_night=night)
        st.pop("last_stall_night", None)
    elif night not in (st.get("last_growth_night"), st.get("last_stall_night")):
        st["nights_without_growth"] = int(st.get("nights_without_growth", 0)) + 1
        st["last_stall_night"] = night
    st.update(last_label_count=count, last_label_check=stamp)
    n = int(st.get("nights_without_growth", 0))
    if n >= stall_nights:
        st["alert"] = (f"ALERT: S1 labels have not grown for {n} consecutive nights "
                       f"(count {count}, last growth {st.get('last_label_growth', 'never')}); check the teacher/label.py")
    else:
        st.pop("alert", None)
    save_status(path, st)
    return st.get("alert", "")


def streak(path: Path, name: str, outcome: str, night: str, threshold: int, detail: str = "") -> str:
    """Consecutive NIGHTS (distinct dates) a condition held: outcome "hit" extends the streak (a new detail, e.g. a different
    stop reason, restarts it at 1), "clear" ends it. At `threshold` nights an ALERT goes into status["alerts"][name] and is
    returned; clearing removes it. Used for label early stops (same reason) and evaluation failures."""
    st = load_status(path)
    streaks, alerts = st.setdefault("streaks", {}), st.setdefault("alerts", {})
    cur = streaks.get(name) or {}
    if outcome == "clear":
        streaks.pop(name, None); alerts.pop(name, None)
    else:
        if cur.get("detail") != detail:
            cur = {"detail": detail, "nights": 0}
        if cur.get("last_night") != night:
            cur["nights"] = int(cur.get("nights", 0)) + 1
            cur["last_night"] = night
        streaks[name] = cur
        if cur["nights"] >= threshold:
            alerts[name] = f"ALERT: {name} {detail} on {cur['nights']} consecutive nights (last {night})".replace("  ", " ")
        else:
            alerts.pop(name, None)
    save_status(path, st)
    return alerts.get(name, "")


def event(path: Path, key: str, detail: str) -> None:
    st = load_status(path)
    st[f"last_{key}"] = {"when": now(), "detail": detail}
    save_status(path, st)


def promote(report: Path, adapter: Path, pointer: Path) -> None:
    r = json.loads(report.read_text())
    cfg = {"base": r["base"], "adapter": str(adapter), "temperatures": r["temperatures"], "promoted": r["when"],
           "report": str(report),
           "inference": {"fail_up_below": r.get("fail_up_below"), "outward_at": r.get("outward_at")},
           "holdout": {k: r["candidate"][k] for k in ("band_accuracy", "under_routed", "over_routed", "outward_recall")}}
    if pointer.exists():
        write_atomic(pointer.with_name(pointer.name + ".prev"), pointer.read_text())
    # mkstemp creates the temp file 0600 and the rename keeps that mode, so the pointer ends up 0600: fine because serve.py
    # (launchd com.cortados.s1-scorer) runs as the same user as the nightly; a scorer under another user would need a chmod here.
    write_atomic(pointer, json.dumps(cfg, indent=1) + "\n")


def main(argv: list[str]) -> int:
    cmd, args = argv[0], argv[1:]
    if cmd == "labels":
        try:
            print(labels(Path(args[0])))
        except ValueError as exc:
            print(f"nightly_state: {exc}", file=sys.stderr)
            return 1
    elif cmd == "fingerprint":
        try:
            print(fingerprint([Path(a) for a in args]))
        except OSError as exc:
            print(f"nightly_state: {exc}", file=sys.stderr)
            return 1
    elif cmd == "growth":
        alert = growth(Path(args[0]), int(args[1]), int(args[2]), args[3] if len(args) > 3 else None)
        if alert:
            print(alert)
    elif cmd == "streak":
        alert = streak(Path(args[0]), args[1], args[2], args[3], int(args[4]), args[5] if len(args) > 5 else "")
        if alert:
            print(alert)
    elif cmd == "event":
        event(Path(args[0]), args[1], args[2] if len(args) > 2 else "")
    elif cmd == "promote":
        promote(Path(args[0]).resolve(), Path(args[1]).resolve(), Path(args[2]))
    else:
        print(f"nightly_state: unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
