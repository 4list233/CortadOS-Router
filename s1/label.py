"""Teacher labelling for CortadOS Router S1. Every request is answered on all twelve routing questions k times, independently, by a strong model,
stateless (no tools, no session). The k samples are the soft label; their disagreement is the uncertainty.

  train split   → claude-sonnet-5 × 3        holdout split → claude-opus-5 × 3   (a different, stronger teacher grades the exam)

Backend (Forest 2026-11-02): the teacher calls go THROUGH THE ROUTER — the `cortados` provider's /v1/messages with a `sub/<model>` pin — so
the provider picks the subscription account by scarcity and refuses on its own quota/fit gates; no CLI or account is hard-coded here, and a
new provider needs no recoding. `--backend cli` keeps the old local `claude -p` path for an explicit manual pass only (never a fallback).
Provider config: CORTADOS_PROVIDER_URL (required), token from CORTADOS_PROVIDER_TOKEN_FILE or ~/.config/cortados/provider-token.
Headroom: every call asks the provider to reveal the routed account's "% left"; the pass stops once it drops below --min-left-pct (default
30), because interactive sessions, reviews and routed subagents share these accounts and the provider itself refuses only at spent.
--max-scarcity is the cli backend's local gate; given on the provider backend it is a config error (exit 2), never silently ignored.

Only Claude teachers: Codex-labelled rows rated tasks much easier and caused under-routing (Forest 2026-11-02), so a non-Claude teacher is refused
here and common.read_labels drops any already on disk.

Resume-safe: finished (id, teacher, sample) triples are skipped.
Exit codes (nightly branches on these, not on log text):
  0  pass completed (every scheduled call made; individual batches may still have been dropped, e.g. a 413, and are logged)
  2  config error before any call (no CORTADOS_PROVIDER_URL, unreadable/empty token file, a non-Claude teacher, --max-scarcity on
     the provider backend, --min-left-pct outside [0, 100))
  3  stopped early: quota refusal, auth/other 4xx, x-should-retry: false, retries exhausted, the routed account below
     --min-left-pct, or (cli backend) the scarcity gate;
     the labels written before the stop are kept and the next run resumes from them
  python3 label.py [--limit N] [--k 3] [--batch 12] [--workers 3] [--split train|holdout|all] [--train-teacher M] [--holdout-teacher M]"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import DATA, LABELER_MARKER, QUESTIONS, REPO, core, is_claude_teacher, read_jsonl, read_labels, valid_answers

STATE = Path.home() / ".cortados"
LABELS = DATA / "labels.jsonl"
DEFAULTS = {"steps": 0, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0, "outward": False, "work_kind": "conversation",
            "urgency": "now", "needs_clarification": False, "parts": 0, "diversity": False}
TEACHER = {"train": "claude-sonnet-5", "holdout": "claude-opus-5"}
BACKEND = "provider"
EXIT_OK, EXIT_CONFIG, EXIT_STOPPED = 0, 2, 3          # see the module docstring
SCARCITY_STOP = 0.7
_write = threading.Lock()
_stop = threading.Event()

SYSTEM = (core.SYSTEM + "\n\nYou are the TEACHER producing training labels. You receive a JSON array of requests Forest (a founder running a fleet of AI "
          "agents for his business and personal life) sent to his agents; `unit_kind` says whether a person was waiting (interactive) or a scheduled job "
          "fired it (background). Rate each request on its own. Be decisive and use the whole scale: most chat is 0-1 on everything; reserve 3 for what the "
          "anchor literally says. blast_radius and outward are about what CARRYING OUT the request would touch, not about the topic mentioned. "
          "Reply with ONLY a JSON array, one object per request, in the same order, each: {\"i\": <the request's i>, "
          + ", ".join(f'"{q}": …' for q in QUESTIONS) + "}. No prose, no code fence.")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for line in (STATE / "env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip().strip('"')
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    return env


def _scarce(model: str) -> float:
    sys.path.insert(0, str(REPO / "cortados_router"))
    import scarcity as sc
    lane = (sc.current().get("lanes") or {}).get("claude") or {}
    pool = sc.pool_for("claude", model, list(lane.get("pools") or {}))
    return float(((lane.get("pools") or {}).get(pool) or lane).get("scarcity") or 0.0)


ONLY: list[str] = []      # --only q1,q2: relabel just these questions; rows are written with "override": true and merged over the full labels


def system_prompt() -> str:
    return _system_core() + TAG_NOTE


def _system_core() -> str:
    if not ONLY:
        return SYSTEM
    return (core.SYSTEM + "\n\nYou are the TEACHER producing training labels for ONLY these questions: " + ", ".join(ONLY) + ". Read each definition "
            "again and apply it literally. Reply with ONLY a JSON array, one object per request, in the same order, each: {\"i\": <the request's i>, "
            + ", ".join(f'"{q}": …' for q in ONLY) + "}. No prose, no code fence.")


# ------------------------------------------------------------------ provider backend (the router picks the account)
TOKEN_FILE_DEFAULT = Path.home() / ".config/cortados/provider-token"
# Seconds per HTTP call: the CLI path's 600 s bound, kept — a 12-request batch on Opus can take minutes; longer means a stalled provider.
PROVIDER_TIMEOUT_S = 600
# Attempts per batch on a retryable answer (429/529/5xx without x-should-retry: false, or a dropped connection). Three covers a provider
# restart or a busy session; more would keep hammering a provider that is down while every other batch does the same.
PROVIDER_ATTEMPTS = 3
PROVIDER_BACKOFF_S = (5.0, 20.0)          # seconds before attempt 2 and 3: past a provider restart, short of stalling the nightly
# The labeler's own traffic lands in the router db like any request; s1/dataset.py must not feed it back as training requests (it is a JSON
# array of OTHER requests). The provider does not persist x-cortados-run-id in the routes table (only in memory for /v1/cortados/runs), but it
# logs the user message text, so the marker rides at the head of the message, and the run id (prefix RUN_ID_PREFIX) rides in the header too.
RUN_ID_PREFIX = "s1-label-"
# The tag line every teacher message starts with (both backends): marker + this pass's run id + the call's sequence number.
TAG_NOTE = f"\n\nThe user message starts with a bookkeeping tag line ({LABELER_MARKER} …); ignore it."
# A provider refusal for SPENT capacity: stop the whole pass (every later batch would get the same answer and the subscriptions need the
# headroom); rerun after the reset — the pass is resume-safe. The provider wraps every sub/ refusal in "no … subscription account can take
# this request (…)" (routing.choose_pinned) and answers 503, so the wrapper itself proves nothing: only the spent reasons inside it stop.
# "usage reading came from a different login, awaiting a fresh sample" is transient (the next usage sample clears it) and is retried.
QUOTA_RE = re.compile(r"no capacity left|usage limit|quota|% used, resets", re.I)
# Output budget per call: a 12-request batch answers in ~3-4k tokens of JSON; 8192 leaves room for a --batch of ~24 without truncating the array.
MAX_TOKENS = 8192
# Headroom the labeler leaves in the account it was routed to (% left, from the provider's x-cortados-reveal answer). The provider's pin
# path only refuses at SPENT; interactive sessions, reviews and routed subagents share these Claude accounts, so a nightly pass must stop
# while they still have room. 30% ~ the old --max-scarcity 0.7 gate. Override with --min-left-pct.
MIN_LEFT_PCT_DEFAULT = 30.0
MIN_LEFT_PCT = MIN_LEFT_PCT_DEFAULT
# Unknown headroom (the provider sent no "% left": its usage reading is stale, failed, or of another login) stops the pass by
# default, after keeping the batch that came back: --min-left-pct cannot be honoured blind, and a whole backlog run blind could
# drain the accounts interactive work shares. --allow-unknown-headroom (or CORTADOS_S1_ALLOW_UNKNOWN_HEADROOM=1, e.g. in
# s1/data/train-config.env) opts into continuing on the provider's own spent gate.
ALLOW_UNKNOWN_HEADROOM = False
REVEAL_RE = re.compile(r"·\s*(\d+(?:\.\d+)?)%\s*left\s*$")
PASS_RUN_ID: str | None = None                           # one x-cortados-run-id per pass: the provider keeps a bounded LRU of run records
_call_seq = iter(range(1, 10**9))
_seq_lock = threading.Lock()
_reveal_missing_logged = threading.Event()


class TeacherStop(Exception):
    """Stop the whole pass (quota refused, auth/config rejected, provider unreachable after retries). Logged, never a crash."""


class TeacherSkip(Exception):
    """Drop this batch only (e.g. 413 too large for every account, or an answer with no JSON array: retrying unchanged cannot succeed)."""


def _config_exit(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(EXIT_CONFIG)


def provider_config() -> tuple[str, str]:
    url = (os.environ.get("CORTADOS_PROVIDER_URL") or "").strip().rstrip("/")
    if not url:
        _config_exit("label: CORTADOS_PROVIDER_URL is not set; the provider backend needs the cortados endpoint (no default: a wrong "
                     "host would only show up as a connection error per batch)")
    tf = Path(os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or TOKEN_FILE_DEFAULT).expanduser()
    try:
        token = tf.read_text().strip()
    except OSError as exc:
        _config_exit(f"label: provider token file {tf} unreadable: {type(exc).__name__}")
    if not token:
        _config_exit(f"label: provider token file {tf} is empty")
    return url, token


def pass_run_id() -> str:
    global PASS_RUN_ID
    with _seq_lock:
        if PASS_RUN_ID is None:
            PASS_RUN_ID = f"{RUN_ID_PREFIX}{time.strftime('%Y%m%d')}-{os.getpid()}"   # matches the provider's [A-Za-z0-9_-]{8,64}
        return PASS_RUN_ID


def _tag() -> str:
    with _seq_lock:
        n = next(_call_seq)
    return f"{LABELER_MARKER} run={pass_run_id()} call={n}"


def _error_message(raw: bytes) -> str:
    try:
        e = json.loads(raw.decode("utf-8", "replace")).get("error")
        return str(e.get("message") if isinstance(e, dict) else e)[:300]
    except (ValueError, AttributeError):
        return raw[:300].decode("utf-8", "replace")


def classify(status: int, should_retry: str | None, message: str) -> str:
    """'retry' | 'stop' | 'skip' for a non-200 provider answer."""
    if QUOTA_RE.search(message or ""):
        return "stop"                                       # spent quota: nothing later in this pass will fare better
    if status == 413:
        return "skip"                                       # too large for every account: this batch only
    if status in (429, 529) or 500 <= status < 600:
        return "stop" if (should_retry or "").strip().lower() == "false" else "retry"
    return "stop"                                           # 400/401/403/404: a bad token or an unserved pin; every batch would fail the same


def left_pct(revealed_model) -> float | None:
    """The `· <pct>% left` the provider appends to the response model for a revealing client (server.reveal_model), or None."""
    m = REVEAL_RE.search(revealed_model or "") if isinstance(revealed_model, str) else None
    return float(m.group(1)) if m else None


def call_provider(model: str, content: str, system: str, *, url: str, token: str, sleep=time.sleep) -> tuple[str, dict]:
    run_id = pass_run_id()
    tag = content.split("\n", 1)[0]
    body = json.dumps({"model": f"sub/{model}", "max_tokens": MAX_TOKENS, "system": system,
                       "messages": [{"role": "user", "content": content}]}).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": token, "x-cortados-run-id": run_id,
               "x-cortados-reveal": "1"}
    last = ""
    for attempt in range(PROVIDER_ATTEMPTS):
        if attempt:
            if _stop.is_set():                              # another worker already stopped the pass: do not spend another call
                raise TeacherStop(f"{tag}: pass stopping; retry abandoned ({last})")
            sleep(PROVIDER_BACKOFF_S[min(attempt - 1, len(PROVIDER_BACKOFF_S) - 1)])
            if _stop.is_set():
                raise TeacherStop(f"{tag}: pass stopping; retry abandoned ({last})")
        req = urllib.request.Request(url + "/v1/messages", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=PROVIDER_TIMEOUT_S) as r:
                out = json.loads(r.read().decode("utf-8"))
            text = "".join(b.get("text", "") for b in out.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
            return text, {"run_id": run_id, "tag": tag, "usage": out.get("usage") or {}, "model": out.get("model"),
                          "stop_reason": out.get("stop_reason"), "left_pct": left_pct(out.get("model"))}
        except urllib.error.HTTPError as exc:
            msg = _error_message(exc.read())
            verdict = classify(exc.code, exc.headers.get("x-should-retry"), msg)
            last = f"HTTP {exc.code}: {msg}"
            if verdict == "stop":
                raise TeacherStop(f"{tag}: {last}")
            if verdict == "skip":
                raise TeacherSkip(f"{tag}: {last}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:     # unreachable / reset / timed out: retryable
            last = f"{type(exc).__name__}: {str(getattr(exc, 'reason', exc))[:160]}"
        except ValueError as exc:                                                         # a 200 whose body is not JSON: a provider bug
            raise TeacherSkip(f"{tag}: unparseable response: {exc}")
        print(f"  ~ {model} {tag}: attempt {attempt + 1}/{PROVIDER_ATTEMPTS} failed ({last})"
              + ("; retrying" if attempt + 1 < PROVIDER_ATTEMPTS else ""), flush=True)
    raise TeacherStop(f"{tag}: provider still failing after {PROVIDER_ATTEMPTS} attempts ({last})")


PROVIDER: tuple[str, str] | None = None                  # (url, token), set by main() for the provider backend


def _payload(batch: list[dict]) -> str:
    """The teacher's user message, both backends: the tag line (so dataset.py can drop the labeler's router rows) + the JSON batch."""
    return _tag() + "\n" + json.dumps([{"i": n, "unit_kind": r["unit_kind"], "request": r["request"]} for n, r in enumerate(batch)],
                                      ensure_ascii=False)


def call_teacher(model: str, batch: list[dict]) -> tuple[list[dict], dict]:
    if BACKEND == "cli":
        text, meta = call_teacher_cli(model, batch)
    else:
        if PROVIDER is None:
            raise TeacherStop("provider backend not configured")
        text, meta = call_provider(model, _payload(batch), system_prompt(), url=PROVIDER[0], token=PROVIDER[1])
        # Headroom first: the response metadata says how much of the account is left whatever the answer text looks like, so a
        # prose / truncated / invalid answer (TeacherSkip below) must not hide a 12%-left reading and let later calls spend more.
        check_headroom(model, meta)
    where = meta.get("tag") or "cli"
    if meta.get("stop_reason") == "max_tokens":         # a truncated array would parse partly or not at all: never trust it
        raise TeacherSkip(f"{where}: answer truncated (stop_reason max_tokens, {len(text or '')} chars)")
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        raise TeacherSkip(f"{where}: no JSON array in the answer (stop_reason {meta.get('stop_reason')}, {len(text or '')} chars)")
    try:
        return json.loads(m.group(0)), meta
    except ValueError as exc:
        raise TeacherSkip(f"{where}: answer array is not valid JSON ({exc}; {len(text or '')} chars)")


def check_headroom(model: str, meta: dict) -> None:
    """Stop the pass once the account this call ran on is below MIN_LEFT_PCT (provider backend)."""
    left = meta.get("left_pct")
    if left is None:
        # The provider reveals % left only for a fresh reading of the login the route ran on (server.reveal_model).
        if not ALLOW_UNKNOWN_HEADROOM:
            print(f"  ! {model}: provider sent no '% left' (model {meta.get('model')!r}); headroom unknown: stopping the pass after "
                  "this batch (--allow-unknown-headroom / CORTADOS_S1_ALLOW_UNKNOWN_HEADROOM=1 continues on the provider's own "
                  "quota gate)", flush=True)
            _stop.set()
            return
        if not _reveal_missing_logged.is_set():          # opted in: logged once per pass, not per call
            _reveal_missing_logged.set()
            print(f"  ? {model}: provider sent no '% left' (model {meta.get('model')!r}); headroom unknown, continuing on the provider's "
                  "own quota gate (--allow-unknown-headroom)", flush=True)
        return
    if left < MIN_LEFT_PCT:
        print(f"  ! {model}: account at {left:g}% left < --min-left-pct {MIN_LEFT_PCT:g}: stopping the pass to leave headroom for "
              "interactive use (rerun after the reset; resume-safe)", flush=True)
        _stop.set()


def call_teacher_cli(model: str, batch: list[dict]) -> tuple[str, dict]:
    cwd = STATE / "teacher-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    payload = _payload(batch)
    argv = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--max-turns", "1", "--tools", "", "--disable-slash-commands",
            "--strict-mcp-config", "--setting-sources", "project", "--model", model, "--system-prompt", system_prompt()]
    p = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=600, cwd=cwd, env=_env())
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:300])
    return out.get("result") or "", out


def work(job: tuple[str, int, list[dict]]) -> int:
    model, sample, batch = job
    if _stop.is_set():
        return 0
    try:
        answers, meta = call_teacher(model, batch)
    except TeacherStop as exc:
        print(f"  ! {model} s{sample}: stopping the pass: {exc}", flush=True)
        _stop.set()
        return 0
    except TeacherSkip as exc:
        print(f"  ! {model} s{sample}: batch dropped: {exc}", flush=True)
        return 0
    except Exception as exc:
        msg = str(exc)
        print(f"  ! {model} s{sample}: {type(exc).__name__}: {msg[:160]}", flush=True)
        if re.search(r"limit|429|quota|overloaded", msg, re.I):
            _stop.set()
        return 0
    good = 0
    with _write, LABELS.open("a") as f:
        for a in answers:
            if not (isinstance(a, dict) and isinstance(a.get("i"), int) and 0 <= a["i"] < len(batch)):
                continue
            if ONLY:
                if all(q in a for q in ONLY) and valid_answers({**DEFAULTS, **{q: a[q] for q in ONLY}}):
                    f.write(json.dumps({"id": batch[a["i"]]["id"], "teacher": model, "sample": sample, "ts": int(time.time()), "override": True,
                                        "answers": {q: a[q] for q in ONLY}}, ensure_ascii=False) + "\n")
                    good += 1
            elif valid_answers(a):
                f.write(json.dumps({"id": batch[a["i"]]["id"], "teacher": model, "sample": sample, "ts": int(time.time()),
                                    "answers": {q: a[q] for q in QUESTIONS}}, ensure_ascii=False) + "\n")
                good += 1
    if BACKEND != "cli":                                   # (headroom was already checked in call_teacher, before parsing)                                   # the provider meters its own routed requests per account; recording here would double-count
        return good
    try:                                                   # the subscription ledger sees teacher work like any other lane use
        # The usage ledger is CortadOS's PTY plugin ($CORTADOS_PTY_PLUGIN, else <CORTADOS_REPO>/plugins/cortados-pty).
        sys.path.insert(0, str(os.environ.get("CORTADOS_PTY_PLUGIN") or Path(core.REPO) / "plugins/cortados-pty"))
        import usage
        u = meta.get("usage") or {}
        usage.record({"job_id": f"teacher-{meta.get('session_id', time.time())}", "runtime": "claude", "account": "subscription", "model": model, "session_id": meta.get("session_id"),
                      "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"), "cache_read_tokens": u.get("cache_read_input_tokens"),
                      "cache_write_tokens": u.get("cache_creation_input_tokens"), "source": "router-teacher", "label": "router-teacher",
                      "dedup_key": f"teacher:{meta.get('session_id')}"})
    except Exception as exc:
        print(f"  ! teacher usage not recorded in the ledger: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    return good


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--k", type=int, default=3); ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--train-teacher", default=os.environ.get("CORTADOS_S1_TEACHER_TRAIN") or TEACHER["train"])
    ap.add_argument("--holdout-teacher", default=os.environ.get("CORTADOS_S1_TEACHER_HOLDOUT") or TEACHER["holdout"])
    ap.add_argument("--backend", choices=("provider", "cli"), default="provider", help="cli: the old local `claude -p` path, explicit manual passes only")
    ap.add_argument("--workers", type=int, default=3); ap.add_argument("--split", default="all"); ap.add_argument("--only", default=""); ap.add_argument("--max-scarcity", type=float, default=None, help="cli backend only: the local pool gate (default %s)" % SCARCITY_STOP)
    ap.add_argument("--min-left-pct", type=float, default=MIN_LEFT_PCT_DEFAULT, help="provider backend: stop once the routed account has less "
                    "than this %% left (default %(default)s; interactive sessions, reviews and routed subagents share these accounts)")
    ap.add_argument("--allow-unknown-headroom", action="store_true",
                    default=os.environ.get("CORTADOS_S1_ALLOW_UNKNOWN_HEADROOM") == "1",
                    help="provider backend: keep labelling when the provider reveals no %% left (default: stop the pass with exit 3)")
    a = ap.parse_args()
    global BACKEND, PROVIDER, ALLOW_UNKNOWN_HEADROOM
    ALLOW_UNKNOWN_HEADROOM = a.allow_unknown_headroom
    for split, model in (("train", a.train_teacher), ("holdout", a.holdout_teacher)):
        if not is_claude_teacher(model):
            print(f"label: refusing {split} teacher {model!r}: only Claude teachers label (Codex labels under-rated tasks; training drops them)",
                  file=sys.stderr)
            return EXIT_CONFIG
        TEACHER[split] = model
    BACKEND = a.backend
    global MIN_LEFT_PCT
    if BACKEND == "provider":
        if a.max_scarcity is not None:                  # never silently ignored: it gates the local pool reading the provider path does not use
            print("label: --max-scarcity applies to --backend cli only; on the provider backend use --min-left-pct", file=sys.stderr)
            return EXIT_CONFIG
        if not 0 <= a.min_left_pct < 100:
            print(f"label: --min-left-pct must be in [0, 100), got {a.min_left_pct}", file=sys.stderr)
            return EXIT_CONFIG
        MIN_LEFT_PCT = a.min_left_pct
        PROVIDER = provider_config()
    elif a.max_scarcity is None:
        a.max_scarcity = SCARCITY_STOP
    global ONLY
    ONLY = [q for q in a.only.split(",") if q]
    global LABELS
    if ONLY:
        LABELS = DATA / f"labels-override-{'-'.join(ONLY)}.jsonl"
    rows = [r for r in read_jsonl(DATA / "requests.jsonl") if a.split in ("all", r["split"])]
    done = {(l["id"], l["teacher"], l["sample"]) for l in read_jsonl(LABELS)}
    if ONLY:                                             # override rows only make sense where a full label exists
        full = {(l["id"], l["teacher"], l["sample"]) for l in read_labels()}      # Claude-only, as training sees them
        rows = [r for r in rows if any(k[0] == r["id"] for k in full)]
    jobs = []
    for split, model in TEACHER.items():
        for s in range(a.k):
            todo = [r for r in rows if r["split"] == split and (r["id"], model, s) not in done]
            random.Random(f"{split}-{s}").shuffle(todo)      # each sample sees different neighbours: no batch-context echo between samples
            jobs += [(model, s, todo[i:i + a.batch]) for i in range(0, len(todo), a.batch)]
    random.Random(7).shuffle(jobs)
    if a.limit:
        jobs = jobs[:a.limit]
    print(f"{len(jobs)} teacher calls to make ({sum(len(j[2]) for j in jobs)} request-labels); already done {len(done)}", flush=True)
    total, t0 = 0, time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        for n, got in enumerate(ex.map(work, jobs), 1):
            total += got
            if BACKEND == "provider":
                # No local scarcity gate: the provider chooses the account by scarcity and refuses on its own quota gates (TeacherStop).
                if n % 5 == 0 or n == len(jobs):
                    print(f"  {n}/{len(jobs)} calls · {total} labels · {int(time.time() - t0)} s", flush=True)
                continue
            if n % 5 == 0 or n == len(jobs):
                models = {j[0] for j in jobs}
                worst = max(_scarce(m) for m in models)
                print(f"  {n}/{len(jobs)} calls · {total} labels · {int(time.time() - t0)} s · claude scarcity {worst}", flush=True)
                if worst >= a.max_scarcity:
                    print(f"  claude pool scarcity {worst} ≥ {a.max_scarcity}: stopping; rerun after the reset (resume-safe)", flush=True)
                    _stop.set()
    print(f"done: {total} labels written" + (" (stopped early)" if _stop.is_set() else ""), flush=True)
    return EXIT_STOPPED if _stop.is_set() else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
