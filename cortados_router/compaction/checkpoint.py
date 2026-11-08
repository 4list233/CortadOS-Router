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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import handoff, policy as policy_mod, transcript


class StatusError(ValueError):
    pass


def _write_private(path: str, text: str, force: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)  # an existing file overwritten with --force keeps its old mode otherwise


def cmd_build(args) -> int:
    pol = policy_mod.load(args.policy)
    session, stats = transcript.parse(args.transcript, args.format)
    md, manifest = handoff.build_handoff(session, pol)
    manifest["parse_stats"] = stats
    os.makedirs(args.out, mode=0o700, exist_ok=True)
    md_path = os.path.join(args.out, "handoff.md")
    man_path = os.path.join(args.out, "handoff.manifest.json")
    if not args.force:
        for p in (md_path, man_path):
            if os.path.exists(p):
                print("refusing to overwrite %s (use --force)" % p, file=sys.stderr)
                return 3
    _write_private(md_path, md, args.force)
    _write_private(man_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n", args.force)
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


def classify(pol: dict, pct: Optional[float], used: Optional[float], window: Optional[float],
             history: Sequence[float] = (), model: str = "", idle_s: Optional[float] = None,
             ttl_class: str = "", fresh: bool = False) -> Tuple[str, str]:
    """Level for one status sample. Thresholds are min(pct of W, cap) (and force <= price cliff - margin for an
    API-billed model in policy.price_cliff_tokens). `history` = used tokens at earlier turn ends, oldest first.
    Levels: ok, warn (checkpoint at the next turn boundary), checkpoint, force, escalate (circuit breaker: a fresh
    session already above X% of the checkpoint threshold; do not auto-restart)."""
    if used is None and pct is not None and window:
        used = pct * window / 100.0
    if used is None or not window:
        if pct is None:
            raise StatusError("status has neither context_window.used_percentage nor a token count")
        # No window size: only the percentage rule can be applied; say so in the reason.
        for name in ("force", "checkpoint", "warn"):
            if pct >= pol[name]["pct"]:
                return name, "used %.1f%% >= %s %s%% (window unknown: absolute caps not applied)" % (
                    pct, name, pol[name]["pct"])
        return "ok", "used %.1f%% (window unknown)" % pct
    th = policy_mod.thresholds(pol, window, model)
    cb = pol["circuit_breaker"]
    if fresh and used > th["checkpoint"] * cb["fresh_open_max_pct_of_checkpoint"] / 100.0:
        return "escalate", "fresh session opens at %d tokens, above %s%% of the %d checkpoint threshold: stop" % (
            used, cb["fresh_open_max_pct_of_checkpoint"], th["checkpoint"])
    if used >= th["force"]:
        return "force", "%d tokens >= force %d" % (used, th["force"])
    if used >= th["checkpoint"]:
        return "checkpoint", "%d tokens >= checkpoint %d" % (used, th["checkpoint"])
    v = pol["velocity"]
    samples = [x for x in history if x is not None] + [used]
    growths = [b - a for a, b in zip(samples, samples[1:])][-int(v["lookback_turns"]):]
    if growths:
        if growths[-1] >= window * v["single_turn_pct_of_window"] / 100.0:
            return "checkpoint", "velocity: last turn added %d tokens >= %s%% of the window" % (
                growths[-1], v["single_turn_pct_of_window"])
        mean = sum(growths) / len(growths)
        if mean > 0 and used + v["projection_turns"] * mean >= th["force"]:
            return "checkpoint", "velocity: %d + %s x mean growth %d >= force %d" % (
                used, v["projection_turns"], mean, th["force"])
    if used >= th["warn"]:
        ttl = pol["idle_cache_ttl_s"].get(ttl_class) if ttl_class else None
        if ttl_class and ttl is None:
            raise StatusError("unknown cache TTL class %r (policy idle_cache_ttl_s)" % ttl_class)
        if idle_s is not None and ttl is not None and idle_s > ttl:
            return "checkpoint", "idle %ds > cache TTL %ds at >= warn: checkpoint before resuming" % (idle_s, ttl)
        return "warn", "%d tokens >= warn %d: checkpoint at the next turn boundary" % (used, th["warn"])
    return "ok", "%d tokens of %d" % (used, window)


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
                                 args.idle_seconds, args.cache_ttl_class or "", args.fresh)
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
    c.add_argument("--policy")
    args = ap.parse_args(argv)
    return cmd_build(args) if args.cmd == "build" else cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
