"""Checkpoint CLI (phase 0, stdlib only, no model calls).

  python3 -B -m cortados_router.compaction.checkpoint build --transcript PATH [--format claude-code|codex|auto] \
      --out DIR [--force] [--policy FILE]
      Writes DIR/handoff.md and DIR/handoff.manifest.json (mode 0600: transcripts hold private data). Refuses to
      overwrite either file unless --force.
  python3 -B -m cortados_router.compaction.checkpoint check --status-file PATH [--previous-status-file PATH]
      Prints one of ok / warn / checkpoint / force, then the reason, from a Claude Code status-line JSON.

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
from typing import Any, Dict, Optional, Tuple

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
             prev_used: Optional[float] = None) -> Tuple[str, str]:
    if pct is None and used is None:
        raise StatusError("status has neither context_window.used_percentage nor a token count")
    cls = None
    if window:
        cls = pol["ceiling_200k_class"] if window <= pol["ceiling_200k_class"]["max_window_tokens"] \
            else pol["ceiling_1m_class"]
    if pct is not None and pct >= pol["force_pct"]:
        return "force", "used %.1f%% >= force %s%%" % (pct, pol["force_pct"])
    if pct is not None and pct >= pol["checkpoint_pct"]:
        return "checkpoint", "used %.1f%% >= checkpoint %s%%" % (pct, pol["checkpoint_pct"])
    if cls and used is not None and used >= cls["checkpoint_tokens"]:
        return "checkpoint", "%d tokens >= absolute ceiling %d for a %d-token window" % (
            used, cls["checkpoint_tokens"], window)
    if prev_used is not None and used is not None and window and used > prev_used:
        projected = used + (used - prev_used) * pol["velocity_turns"]
        if 100.0 * projected / window >= pol["force_pct"]:
            return "checkpoint", "velocity: +%d tokens last turn would cross force %s%% within %d turns" % (
                used - prev_used, pol["force_pct"], pol["velocity_turns"])
    if pct is not None and pct >= pol["warn_pct"]:
        return "warn", "used %.1f%% >= warn %s%%" % (pct, pol["warn_pct"])
    return "ok", "used %s%%" % ("?" if pct is None else "%.1f" % pct)


def cmd_check(args) -> int:
    pol = policy_mod.load(args.policy)
    with open(args.status_file, encoding="utf-8") as fh:
        pct, used, window = read_status(json.load(fh))
    prev_used = None
    if args.previous_status_file and os.path.exists(args.previous_status_file):
        with open(args.previous_status_file, encoding="utf-8") as fh:
            prev_used = read_status(json.load(fh))[1]
    try:
        level, reason = classify(pol, pct, used, window, prev_used)
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
    c.add_argument("--previous-status-file")
    c.add_argument("--policy")
    args = ap.parse_args(argv)
    return cmd_build(args) if args.cmd == "build" else cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
