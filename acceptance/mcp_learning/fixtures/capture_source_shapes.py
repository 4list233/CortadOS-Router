# PURPOSE: Capture privacy-preserving observed integration shapes for the independent exam.
# CONTRACT: RT-MCP-LEARNING-003 AC-L11; reads sources, never writes production data.
# DEPENDS: Python stdlib; SQLite read-only transactions including committed WAL contents.
# CEILING: Redacted samples prove layout only, never request meaning or model quality.
import hashlib
import json
from pathlib import Path
import sqlite3
import time

HERE = Path(__file__).resolve().parent / "source-shapes"
TRAIN = Path("/Users/velox/cortados/train/data")
STATE = Path("/Users/velox/.local/state/cortados-workers")
ENUMS = {"state", "mode", "unit_kind", "split", "schema", "provider", "account_ref",
         "verification", "engine", "band", "work_kind", "urgency", "cli_version"}


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return type(value).__name__


def redact(value, key=""):
    if isinstance(value, dict):
        return {name: redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        return value if key in ENUMS else "[redacted string]"
    return value


def save(name, raw, value, metadata):
    sample = redact(value)
    assert shape(sample) == shape(value)
    result = {"captured_at_unix": time.time(), "source_record_sha256": hashlib.sha256(raw).hexdigest(),
              "redaction": "Every string value except closed protocol/category enums replaced; keys, arrays, scalars and nulls unchanged.",
              "metadata": metadata, "shape": shape(value), "sample": sample}
    (HERE / name).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def main():
    HERE.mkdir(parents=True, exist_ok=True)
    for name in ("requests", "labels"):
        with (TRAIN / (name + ".jsonl")).open("rb") as stream:
            raw = next(line for line in stream if line.strip())
        save(name + ".json", raw, json.loads(raw), {"source": "live train/data/" + name + ".jsonl", "record": "first nonempty JSON row"})
    for name, path, table in (("jobs", STATE / "jobs.db", "jobs"), ("routes", STATE / "router/router.db", "routes")):
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("BEGIN")
            columns = db.execute("PRAGMA table_info(" + table + ")").fetchall()
            row = db.execute("SELECT * FROM " + table + " ORDER BY rowid LIMIT 1").fetchone()
            value = dict(zip((col[1] for col in columns), row))
            for key in ("arguments", "record", "context", "answers", "resolved", "usage"):
                if isinstance(value.get(key), (str, bytes)):
                    try:
                        value[key] = json.loads(value[key])
                    except ValueError:
                        pass  # Non-JSON scalar text remains redacted; the observed column shape records it.
            raw = json.dumps(value, sort_keys=True, default=str).encode()
            metadata = {"source": "live worker state/" + str(path.relative_to(STATE)),
                        "user_version": db.execute("PRAGMA user_version").fetchone()[0], "columns": columns}
            save(name + ".json", raw, value, metadata)


if __name__ == "__main__":
    main()
