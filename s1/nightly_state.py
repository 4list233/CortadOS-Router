"""Small state helpers for s1/nightly.sh (stdlib only, so the nightly never depends on the training venv for bookkeeping).

  labels <labels.jsonl>                       print "<distinct ids> <sha256 of the file>" (0 and "none" when the file is missing)
  growth <status.json> <count> <stall-nights> record tonight's label count; print ALERT line when labels have not grown for
                                              <stall-nights> consecutive nights (the router dashboard reads the same file)
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
        os.replace(path, path.with_suffix(".corrupt"))
        print(f"nightly_state: {path} was not JSON ({exc}); moved to .corrupt and started fresh", file=sys.stderr)
        return {}


def save_status(path: Path, st: dict) -> None:
    write_atomic(path, json.dumps(st, indent=1, sort_keys=True) + "\n")


def labels(path: Path) -> str:
    if not path.exists():
        return "0 none"
    raw = path.read_bytes()
    ids = set()
    for line in raw.decode().splitlines():
        if line.strip():
            ids.add(json.loads(line)["id"])
    return f"{len(ids)} {hashlib.sha256(raw).hexdigest()[:16]}"


def growth(path: Path, count: int, stall_nights: int) -> str:
    st = load_status(path)
    prev = st.get("last_label_count")
    stamp = now()
    if prev is None or count > prev:
        st.update(last_label_growth=stamp, nights_without_growth=0)
    else:
        st["nights_without_growth"] = int(st.get("nights_without_growth", 0)) + 1
    st.update(last_label_count=count, last_label_check=stamp)
    n = st["nights_without_growth"]
    if n >= stall_nights:
        st["alert"] = (f"ALERT: S1 labels have not grown for {n} consecutive nights "
                       f"(count {count}, last growth {st.get('last_label_growth', 'never')}); check the teacher/label.py")
    else:
        st.pop("alert", None)
    save_status(path, st)
    return st.get("alert", "")


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
    write_atomic(pointer, json.dumps(cfg, indent=1) + "\n")


def main(argv: list[str]) -> int:
    cmd, args = argv[0], argv[1:]
    if cmd == "labels":
        print(labels(Path(args[0])))
    elif cmd == "growth":
        alert = growth(Path(args[0]), int(args[1]), int(args[2]))
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
