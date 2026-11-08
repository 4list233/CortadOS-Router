"""Checkpoint CLI (phase 0, stdlib only, no model calls).

  python3 -B -m cortados_router.compaction.checkpoint build --transcript PATH [--format claude-code|codex|auto] \
      --out DIR [--force] [--policy FILE]
      Writes DIR/handoff.md and DIR/handoff.manifest.json (mode 0600: transcripts hold private data). Refuses to
      overwrite either file unless --force.
  python3 -B -m cortados_router.compaction.checkpoint check --status-file PATH [--history-file PATH] [--model ID]
      [--idle-seconds N --cache-ttl-class CLASS] [--fresh]
      Prints ok / warn / checkpoint / force / escalate, then the reason, from a Claude Code status-line JSON and the
      policy (thresholds min(pct of W, cap), price cliffs, velocity, idle-vs-cache-TTL, fresh-session breaker).

Status-line fields read (Claude Code passes this JSON to the statusLine command on stdin; the reader is tolerant and
these are the assumed names): `context_window.used_percentage`, `context_window.context_window_size`,
`context_window.current_usage.{input_tokens, cache_creation_input_tokens, cache_read_input_tokens}` (the tokens in
the window now), falling back to `context_window.total_input_tokens`. Missing everything -> exit 2 with an error,
never a silent "ok".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import handoff, policy as policy_mod, transcript


class StatusError(ValueError):
    pass


class WriteRefused(OSError):
    pass


def _write_private(path: str, text: str, force: bool) -> None:
    """Write `text` to `path` with mode 0600, atomically: a 0600 temp file in the same directory, fsync, then
    os.replace (with --force) or os.link (without: fails if the destination exists). The content never sits in a
    file with wider permissions. A destination that is a symlink is refused even with --force (no following it)."""
    if os.path.islink(path):
        raise WriteRefused("refusing to write through symlink %s" % path)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".handoff-", suffix=".tmp")  # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if force:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise WriteRefused("refusing to overwrite %s (use --force)" % path)
            os.unlink(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_build(args) -> int:
    pol = policy_mod.load(args.policy)
    session, stats = transcript.parse(args.transcript, args.format)
    md, manifest, extra = handoff.build_handoff_files(session, pol)
    manifest["parse_stats"] = stats
    os.makedirs(args.out, mode=0o700, exist_ok=True)
    md_path = os.path.join(args.out, "handoff.md")
    man_path = os.path.join(args.out, "handoff.manifest.json")
    extra_paths = {os.path.join(args.out, name): text for name, text in sorted(extra.items())}
    # A stale overflow file from an earlier build would point a reader at the wrong text: refuse/replace it too.
    stale = [os.path.join(args.out, n) for n in handoff.OVERFLOW_FILES.values()
             if os.path.join(args.out, n) not in extra_paths and os.path.lexists(os.path.join(args.out, n))]
    for p in [md_path, man_path] + list(extra_paths) + stale:  # check all first so a refusal leaves neither file half-updated
        if os.path.islink(p) or (os.path.exists(p) and not args.force):
            print("refusing to overwrite %s (%s)" % (p, "symlink" if os.path.islink(p) else "use --force"),
                  file=sys.stderr)
            return 3
    try:
        for p, text in extra_paths.items():  # sibling files first: handoff.md must never point at a missing file
            _write_private(p, text, args.force)
        for p in stale:
            os.unlink(p)
        _write_private(md_path, md, args.force)
        _write_private(man_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n", args.force)
    except WriteRefused as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print("handoff-id: %s  %s  (~%d tokens, %d skipped lines)"
          % (manifest["handoff_id"], md_path, manifest["total_tokens"], sum(stats["skipped"].values())))
    return 0


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def read_status(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (used_pct, used_tokens, window_tokens); any may be None."""
    cw = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
    window = _num(cw.get("context_window_size"))
    used = None
    cu = cw.get("current_usage")
    if isinstance(cu, dict):
        parts = [_num(cu.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(p is not None for p in parts):
            used = sum(p for p in parts if p is not None)
    if used is None:
        used = _num(cw.get("total_input_tokens"))
    pct = _num(cw.get("used_percentage"))
    if pct is None and used is not None and window:
        pct = 100.0 * used / window
    return pct, used, window


def forced_restarts_in_window(pol: dict, restarts: Sequence[dict], now: float) -> int:
    window = pol["circuit_breaker"]["window_s"]
    return sum(1 for r in restarts if r.get("forced") and now - r["ts"] <= window)


def classify(pol: dict, pct: Optional[float], used: Optional[float], window: Optional[float],
             history: Sequence[float] = (), model: str = "", idle_s: Optional[float] = None,
             ttl_class: str = "", fresh: bool = False, restarts: Sequence[dict] = (),
             now: Optional[float] = None) -> Tuple[str, str]:
    """Level for one status sample. Thresholds are min(pct of W, cap) (and force <= price cliff - margin for an
    API-billed model in policy.price_cliff_tokens); with no window size the absolute caps and cliffs still apply to a
    known token count. `history` = used tokens at earlier turn ends, oldest first; `restarts` = restart events
    ({"ts", "forced"}). Levels: ok, warn (checkpoint at the next turn boundary), checkpoint, force, escalate (circuit
    breaker: stop, tell the owner, no automatic restart)."""
    level, reason = _level(pol, pct, used, window, history, model, idle_s, ttl_class, fresh)
    if level in ("checkpoint", "force"):
        n = forced_restarts_in_window(pol, restarts, time.time() if now is None else now)
        cb = pol["circuit_breaker"]
        if n >= cb["max_forced_restarts"]:
            return "escalate", "%s wanted (%s) but %d forced restarts in the last %ds reached the circuit breaker" % (
                level, reason, n, cb["window_s"])
    return level, reason


def _level(pol, pct, used, window, history, model, idle_s, ttl_class, fresh) -> Tuple[str, str]:
    if used is None and pct is not None and window:
        used = pct * window / 100.0
    if used is None and pct is None:
        raise StatusError("status has neither context_window.used_percentage nor a token count")
    if used is None:
        # Percentage only, no window size: only the percentage rule can be applied; say so.
        for name in ("force", "checkpoint", "warn"):
            if pct >= pol[name]["pct"]:
                return name, "used %.1f%% >= %s %s%% (window unknown: absolute caps not applied)" % (
                    pct, name, pol[name]["pct"])
        return "ok", "used %.1f%% (window unknown)" % pct
    th = policy_mod.thresholds(pol, window, model)
    note = "" if window else " (window unknown: absolute caps only)"
    cb = pol["circuit_breaker"]
    if fresh and used > th["checkpoint"] * cb["fresh_open_max_pct_of_checkpoint"] / 100.0:
        return "escalate", "fresh session opens at %d tokens, above %s%% of the %d checkpoint threshold: stop" % (
            used, cb["fresh_open_max_pct_of_checkpoint"], th["checkpoint"])
    pct_level = "ok"
    if pct is not None and not window:
        pct_level = next((n for n in ("force", "checkpoint", "warn") if pct >= pol[n]["pct"]), "ok")
    for name in ("force", "checkpoint"):
        if used >= th[name] or pct_level == name:
            return name, "%d tokens >= %s %d%s" % (used, name, th[name], note) if used >= th[name] else \
                "used %.1f%% >= %s %s%%" % (pct, name, pol[name]["pct"])
    v = pol["velocity"]
    samples = [x for x in history if x is not None] + [used]
    growths = [b - a for a, b in zip(samples, samples[1:])][-v["lookback_turns"]:]
    if growths:
        if window and growths[-1] >= window * v["single_turn_pct_of_window"] / 100.0:
            return "checkpoint", "velocity: last turn added %d tokens >= %s%% of the window" % (
                growths[-1], v["single_turn_pct_of_window"])
        mean = sum(growths) / len(growths)
        if mean > 0 and used + v["projection_turns"] * mean >= th["force"]:
            return "checkpoint", "velocity: %d + %s x mean growth %d >= force %d" % (
                used, v["projection_turns"], mean, th["force"])
    if used >= th["warn"] or pct_level == "warn":
        ttl = pol["idle_cache_ttl_s"].get(ttl_class) if ttl_class else None
        if ttl_class and ttl is None:
            raise StatusError("unknown cache TTL class %r (policy idle_cache_ttl_s)" % ttl_class)
        if idle_s is not None and ttl is not None and idle_s > ttl:
            return "checkpoint", "idle %ds > cache TTL %ds at >= warn: checkpoint before resuming" % (idle_s, ttl)
        return "warn", "%d tokens >= warn %d%s: checkpoint at the next turn boundary" % (used, th["warn"], note)
    return "ok", "%d tokens%s" % (used, " of %d" % window if window else note)


def read_restarts(path: Optional[str]) -> List[dict]:
    """Restart log: one JSON object per line, {"ts": epoch seconds, "forced": bool}."""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                r = json.loads(ln)
                if not isinstance(r, dict) or not isinstance(r.get("ts"), (int, float)):
                    raise StatusError("bad restart log line: %r" % ln.strip())
                out.append(r)
    return out


def cmd_record_restart(args) -> int:
    d = os.path.dirname(os.path.abspath(args.restart_log))
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(args.restart_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time() if args.now is None else args.now, "forced": args.forced,
                             "handoff_id": args.handoff_id or ""}, sort_keys=True) + "\n")
    return 0


def _read_history(path: Optional[str]) -> List[float]:
    """History file: one JSON number per line (used tokens at earlier turn ends, oldest first)."""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                v = json.loads(ln)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise StatusError("history line is not a number: %r" % ln)
                out.append(float(v))
    return out


def cmd_check(args) -> int:
    pol = policy_mod.load(args.policy)
    try:
        with open(args.status_file, encoding="utf-8") as fh:
            pct, used, window = read_status(json.load(fh))
        level, reason = classify(pol, pct, used, window, _read_history(args.history_file), args.model or "",
                                 args.idle_seconds, args.cache_ttl_class or "", args.fresh,
                                 read_restarts(args.restart_log), args.now)
    except StatusError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print("%s %s" % (level, reason))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="checkpoint")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--transcript", required=True)
    b.add_argument("--format", default="auto", choices=("auto", "claude-code", "codex"))
    b.add_argument("--out", required=True)
    b.add_argument("--force", action="store_true")
    b.add_argument("--policy")
    c = sub.add_parser("check")
    c.add_argument("--status-file", required=True)
    c.add_argument("--history-file", help="used tokens at earlier turn ends, one number per line, oldest first")
    c.add_argument("--model", help="routed model id, for policy.price_cliff_tokens (API-billed only)")
    c.add_argument("--idle-seconds", type=float, help="seconds since the last turn ended")
    c.add_argument("--cache-ttl-class", help="key of policy.idle_cache_ttl_s, e.g. claude-subscription")
    c.add_argument("--fresh", action="store_true", help="this is the first sample of a restarted session")
    c.add_argument("--restart-log", help="restart events (record-restart), for the circuit breaker")
    c.add_argument("--now", type=float, help=argparse.SUPPRESS)
    r = sub.add_parser("record-restart")
    r.add_argument("--restart-log", required=True)
    r.add_argument("--forced", action="store_true")
    r.add_argument("--handoff-id")
    r.add_argument("--now", type=float, help=argparse.SUPPRESS)
    c.add_argument("--policy")
    args = ap.parse_args(argv)
    return {"build": cmd_build, "check": cmd_check, "record-restart": cmd_record_restart}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
