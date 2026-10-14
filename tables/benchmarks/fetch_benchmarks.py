#!/usr/bin/env python3
"""
fetch_benchmarks.py

Pulls PUBLIC, unauthenticated, machine-readable benchmark data for LLM router
ranking purposes. Python 3 stdlib only (urllib, json, csv, re).

For every source:
  - plain HTTP GET, User-Agent "cortados-router-benchmarks/1.0"
  - no API keys, no logins, no POSTs
  - writes the untouched download to raw/<source_id>.<ext>
  - appends normalized rows to scores.csv:
        source_id, benchmark, skill, model_name_as_published, score, unit, date_fetched
  - a failing source prints an error and does NOT stop the others
  - never invents a number: every row here traces back to a file in raw/

Run: python3 fetch_benchmarks.py
"""

import csv
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
SCORES_CSV = HERE / "scores.csv"
USER_AGENT = "cortados-router-benchmarks/1.0"
TIMEOUT = 30
TODAY = datetime.date.today().isoformat()

SCORES_HEADER = [
    "source_id",
    "benchmark",
    "skill",
    "model_name_as_published",
    "score",
    "unit",
    "date_fetched",
]


def http_get(url, extra_headers=None):
    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def write_raw(source_id, ext, data_bytes):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{source_id}.{ext}"
    path.write_bytes(data_bytes)
    return path


def to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Source: Epoch AI ECI benchmarking hub CSV
# ---------------------------------------------------------------------------

EPOCH_BENCHMARK_SKILL = {
    # code
    "Aider polyglot": "code",
    "GSO-Bench": "code",
    "MirrorCode": "code",
    "CadEval": "code",
    "FrontierCode": "code",
    "Surface Evolver Bench": "code",
    # agentic (tool-use / multi-step / environment benchmarks)
    "SWE-Bench verified": "agentic",
    "Terminal Bench": "agentic",
    "OSWorld": "agentic",
    "OSWorld 2.0": "agentic",
    "The Agent Company": "agentic",
    "Cybench": "agentic",
    "ExploitBench": "agentic",
    "APEX-Agents": "agentic",
    "GDPval": "agentic",
    "Remote Labor Index": "agentic",
    "METR Time Horizons": "agentic",
    "DTBench": "agentic",
    "EBR-bench": "agentic",
    "Balrog": "agentic",
    "DeepResearch Bench": "agentic",
    "DeepSWE": "agentic",
    "PostTrainBench": "agentic",
    # chat / writing preference
    "Lech Mazur Writing": "chat",
    "Fiction.LiveBench": "chat",
    # everything else defaults to reasoning/knowledge (see DEFAULT below)
}
EPOCH_SKILL_DEFAULT = "reasoning"


def fetch_epoch_eci(rows_out):
    source_id = "epoch_eci"
    url = "https://epoch.ai/data/eci_benchmarks.csv"
    raw_bytes = http_get(url)
    path = write_raw(source_id, "csv", raw_bytes)
    text = raw_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(text.splitlines())
    count = 0
    for row in reader:
        model_name = (row.get("model_version") or "").strip()
        if not model_name or model_name.lower() == "unknown":
            model_name = (row.get("model") or row.get("Model") or "").strip()
        if not model_name:
            continue
        bench = (row.get("benchmark") or "").strip()
        score = to_float(row.get("performance"))
        if score is None or not bench:
            continue
        skill = EPOCH_BENCHMARK_SKILL.get(bench, EPOCH_SKILL_DEFAULT)
        rows_out.append(
            [source_id, bench, skill, model_name, score, "epoch_performance_0_1", TODAY]
        )
        count += 1
    return count, path


# ---------------------------------------------------------------------------
# Source: LiveBench per-subtask CSV (discover latest date via GitHub API)
# ---------------------------------------------------------------------------

LIVEBENCH_SKILL_MAP = {
    "code_completion": "code",
    "code_generation": "code",
    "javascript": "code",
    "python": "code",
    "typescript": "code",
    "AMPS_Hard": "reasoning",
    "connections": "reasoning",
    "integrals_with_game": "reasoning",
    "logic_with_navigation": "reasoning",
    "math_comp": "reasoning",
    "olympiad": "reasoning",
    "spatial": "reasoning",
    "theory_of_mind": "reasoning",
    "zebra_puzzle": "reasoning",
    "paraphrase": "chat",
    "story_generation": "chat",
    "summarize": "chat",
    "typos": "chat",
    "consecutive_events": "agentic",
    "plot_unscrambling": "agentic",
    "tablejoin": "agentic",
    "tablereformat": "agentic",
}


def fetch_livebench(rows_out):
    source_id = "livebench"
    listing_url = "https://api.github.com/repos/LiveBench/new-livebench/contents/public"
    listing_bytes = http_get(listing_url, extra_headers={"Accept": "application/vnd.github+json"})
    listing = json.loads(listing_bytes.decode("utf-8"))
    if isinstance(listing, dict):
        raise RuntimeError(f"GitHub API error: {listing.get('message')}")
    table_re = re.compile(r"^table_(\d{4})_(\d{2})_(\d{2})\.csv$")
    best = None
    for entry in listing:
        m = table_re.match(entry.get("name", ""))
        if m:
            date_key = tuple(int(g) for g in m.groups())
            if best is None or date_key > best[0]:
                best = (date_key, entry["name"])
    if best is None:
        raise RuntimeError("no table_YYYY_MM_DD.csv found in LiveBench/new-livebench public/")
    latest_name = best[1]
    raw_url = f"https://raw.githubusercontent.com/LiveBench/new-livebench/main/public/{latest_name}"
    raw_bytes = http_get(raw_url)
    path = write_raw(source_id, "csv", raw_bytes)
    text = raw_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(text.splitlines())
    count = 0
    for row in reader:
        model_name = (row.get("model") or "").strip()
        if not model_name:
            continue
        for col, skill in LIVEBENCH_SKILL_MAP.items():
            if col not in row:
                continue
            score = to_float(row.get(col))
            if score is None:
                continue
            rows_out.append(
                [source_id, f"livebench_{col}", skill, model_name, score, "percent", TODAY]
            )
            count += 1
    return count, path


# ---------------------------------------------------------------------------
# Source: Aider polyglot leaderboard YAML (hand-parsed, no PyYAML in stdlib)
# ---------------------------------------------------------------------------

def parse_flat_yaml_list(text):
    """Parse the specific flat structure used by aider's polyglot_leaderboard.yml:
    a top-level list of flat dicts, one dict per '- key: value' block, with
    2-space-indented 'key: value' continuation lines. No nested lists/maps,
    no block scalars - verified by inspection of the fetched file.
    """
    records = []
    current = None
    item_re = re.compile(r"^- ([A-Za-z0-9_]+):\s?(.*)$")
    cont_re = re.compile(r"^  ([A-Za-z0-9_]+):\s?(.*)$")

    def clean(v):
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        return v

    for line in text.splitlines():
        if not line.strip():
            continue
        m = item_re.match(line)
        if m:
            if current is not None:
                records.append(current)
            current = {m.group(1): clean(m.group(2))}
            continue
        m = cont_re.match(line)
        if m and current is not None:
            current[m.group(1)] = clean(m.group(2))
            continue
        # unrecognized line shape (shouldn't happen for this file) - ignore
    if current is not None:
        records.append(current)
    return records


def fetch_aider_polyglot(rows_out):
    source_id = "aider_polyglot"
    url = "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml"
    raw_bytes = http_get(url)
    path = write_raw(source_id, "yml", raw_bytes)
    text = raw_bytes.decode("utf-8", errors="replace")
    records = parse_flat_yaml_list(text)
    count = 0
    for rec in records:
        model_name = (rec.get("model") or "").strip()
        if not model_name:
            continue
        pass_rate_2 = to_float(rec.get("pass_rate_2"))
        if pass_rate_2 is not None:
            rows_out.append(
                [source_id, "aider_polyglot_pass_rate_2", "code", model_name, pass_rate_2, "percent", TODAY]
            )
            count += 1
        seconds = to_float(rec.get("seconds_per_case"))
        if seconds is not None:
            rows_out.append(
                [source_id, "aider_polyglot_seconds_per_case", "speed", model_name, seconds, "seconds_per_case", TODAY]
            )
            count += 1
    return count, path


# ---------------------------------------------------------------------------
# Source: SWE-bench Verified leaderboard JSON
# ---------------------------------------------------------------------------

def fetch_swebench_verified(rows_out):
    source_id = "swebench_verified"
    url = "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json"
    raw_bytes = http_get(url)
    path = write_raw(source_id, "json", raw_bytes)
    data = json.loads(raw_bytes.decode("utf-8"))
    count = 0
    for lb in data.get("leaderboards", []):
        if lb.get("name") != "Verified":
            continue
        for r in lb.get("results", []):
            model_name = (r.get("model_display") or "").strip()
            score = to_float(r.get("resolved"))
            if not model_name or score is None:
                continue
            rows_out.append(
                [source_id, "swe_bench_verified_resolved", "agentic", model_name, score, "percent_resolved", TODAY]
            )
            count += 1
    return count, path


# ---------------------------------------------------------------------------
# Source: LMArena leaderboard-dataset on Hugging Face (datasets-server rows API)
# ---------------------------------------------------------------------------

def fetch_hf_arena_config(source_id, config, skill, benchmark_label, unit, score_field,
                           model_field="model_name", max_pages=20, page_size=100):
    base = "https://datasets-server.huggingface.co/rows"
    pages = []
    overall_rows = []
    offset = 0
    for _ in range(max_pages):
        url = (
            f"{base}?dataset=lmarena-ai%2Fleaderboard-dataset&config={config}"
            f"&split=latest&offset={offset}&length={page_size}"
        )
        raw_bytes = http_get(url)
        page = json.loads(raw_bytes.decode("utf-8"))
        pages.append(page)
        rows = page.get("rows", [])
        page_overall = [r["row"] for r in rows if r["row"].get("category") == "overall"]
        overall_rows.extend(page_overall)
        num_rows_total = page.get("num_rows_total", 0)
        if not page_overall and overall_rows:
            break  # ran off the end of the contiguous 'overall' block
        offset += page_size
        if offset >= num_rows_total or not rows:
            break

    raw_bytes_combined = json.dumps(pages, indent=2).encode("utf-8")
    path = write_raw(source_id, "json", raw_bytes_combined)

    count = 0
    rows_out_local = []
    for row in overall_rows:
        model_name = (row.get(model_field) or "").strip()
        score = to_float(row.get(score_field))
        if not model_name or score is None:
            continue
        rows_out_local.append(
            [source_id, benchmark_label, skill, model_name, score, unit, TODAY]
        )
        count += 1
    return count, path, rows_out_local


def fetch_lmarena_text(rows_out):
    count, path, local = fetch_hf_arena_config(
        "lmarena_text", "text", "chat", "lmarena_text_arena_overall", "arena_elo", "rating"
    )
    rows_out.extend(local)
    return count, path


def fetch_lmarena_text_style_control(rows_out):
    count, path, local = fetch_hf_arena_config(
        "lmarena_text_style_control", "text_style_control", "chat",
        "lmarena_text_arena_style_controlled_overall", "arena_elo_style_controlled", "rating"
    )
    rows_out.extend(local)
    return count, path


def fetch_lmarena_webdev(rows_out):
    count, path, local = fetch_hf_arena_config(
        "lmarena_webdev", "webdev", "code", "lmarena_webdev_arena_overall", "arena_elo_webdev", "rating"
    )
    rows_out.extend(local)
    return count, path


def fetch_lmarena_agent(rows_out):
    count, path, local = fetch_hf_arena_config(
        "lmarena_agent", "agent", "agentic", "lmarena_agent_arena_overall", "arena_agent_score_0_1", "score"
    )
    rows_out.extend(local)
    return count, path


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _env_key(name):
    """Runtime secrets live in ~/.cortados/env (0600, never in the repo)."""
    import os
    if os.environ.get(name):
        return os.environ[name]
    p = os.path.expanduser("~/.cortados/env")
    if os.path.exists(p):
        for line in open(p):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def fetch_artificial_analysis(rows_out):
    """Artificial Analysis LLM models endpoint (needs the free API key Forest created 2026-09-23). Gives, per model: intelligence and coding
    indices, output tokens/s, time-to-first-token, and price per 1M input/output tokens. Nothing else we fetch has speed or price."""
    key = _env_key("ARTIFICIAL_ANALYSIS_API_KEY")
    if not key:
        raise RuntimeError("no ARTIFICIAL_ANALYSIS_API_KEY in ~/.cortados/env: skipped")
    url = "https://artificialanalysis.ai/api/v2/data/llms/models"
    req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    path = RAW_DIR / "artificial_analysis.json"
    path.write_bytes(raw)
    data = json.loads(raw)
    models = data.get("data") if isinstance(data, dict) else data
    today = TODAY
    n = 0
    for m in models or []:
        name = m.get("slug") or m.get("name") or m.get("id")
        ev = m.get("evaluations") or {}
        fields = [("artificial_analysis_intelligence_index", "reasoning", ev.get("artificial_analysis_intelligence_index"), "index"),
                  ("artificial_analysis_coding_index", "code", ev.get("artificial_analysis_coding_index"), "index"),
                  ("terminalbench_hard", "agentic", ev.get("terminalbench_hard"), "fraction"),
                  ("livecodebench", "code", ev.get("livecodebench"), "fraction"),
                  ("ifbench", "chat", ev.get("ifbench"), "fraction"),
                  ("lcr", "reasoning", ev.get("lcr"), "fraction"),
                  ("output_tokens_per_second", "speed", m.get("median_output_tokens_per_second"), "tokens_per_s"),
                  ("time_to_first_token_s", "speed", m.get("median_time_to_first_token_seconds"), "seconds_lower_better"),
                  ("price_1m_input_usd", "price", (m.get("pricing") or {}).get("price_1m_input_tokens"), "usd_lower_better"),
                  ("price_1m_output_usd", "price", (m.get("pricing") or {}).get("price_1m_output_tokens"), "usd_lower_better"),
                  ("price_1m_blended_3_to_1_usd", "price", (m.get("pricing") or {}).get("price_1m_blended_3_to_1"), "usd_lower_better")]
        for bench, skill, val, unit in fields:
            if isinstance(val, (int, float)):
                rows_out.append(["artificial_analysis", bench, skill, name, val, unit, today]); n += 1
    return n, path


SOURCES = [
    ("artificial_analysis", fetch_artificial_analysis),
    ("epoch_eci", fetch_epoch_eci),
    ("livebench", fetch_livebench),
    ("aider_polyglot", fetch_aider_polyglot),
    ("swebench_verified", fetch_swebench_verified),
    ("lmarena_text", fetch_lmarena_text),
    ("lmarena_text_style_control", fetch_lmarena_text_style_control),
    ("lmarena_webdev", fetch_lmarena_webdev),
    ("lmarena_agent", fetch_lmarena_agent),
]


def main():
    all_rows = []
    summary = []

    for source_id, fn in SOURCES:
        try:
            count, path = fn(all_rows)
            summary.append((source_id, "OK", count, str(path)))
            print(f"[{source_id}] OK: {count} rows -> {path}")
        except urllib.error.URLError as e:
            summary.append((source_id, "FAILED", 0, f"network error: {e}"))
            print(f"[{source_id}] FAILED (network): {e}")
        except Exception as e:  # noqa: BLE001 - a bad source must not kill the run
            summary.append((source_id, "FAILED", 0, f"{type(e).__name__}: {e}"))
            print(f"[{source_id}] FAILED ({type(e).__name__}): {e}")

    with SCORES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(SCORES_HEADER)
        for row in all_rows:
            writer.writerow(row)

    distinct_models = {r[3] for r in all_rows}
    print("\n=== Summary ===")
    for source_id, status, count, note in summary:
        print(f"  {source_id:32s} {status:8s} rows={count:6d}  {note}")
    print(f"\nTotal rows written to scores.csv: {len(all_rows)}")
    print(f"Distinct model_name_as_published values: {len(distinct_models)}")
    print(f"scores.csv: {SCORES_CSV}")

    ok_count = sum(1 for _, status, _, _ in summary if status == "OK")
    if ok_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
