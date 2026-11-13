"""The live view's heartbeat sender: one POST /v1/cortados/live/heartbeat for THIS host, run every ~60 s by a LaunchAgent
(scripts/install_live_heartbeat.sh). Stdlib only; the provider endpoint is provider/live.py (#73).

What it reports, all metadata:
  - jobs      for each configured launchd label, `launchctl print gui/<uid>/<label>`: running / ok (last exit 0) / idle
              (never exited) / failed (last exit non-zero) / stopped (not loaded), and a summary
              "pid up, 3 runs, last exit 0" (the pid number is not sent);
  - hardware  cortados_router.hardware.sample() (cpu/gpu busy %, memory, load, chip) plus the disk's free % (statvfs);
  - s1        on the S1 host: the pipeline's flags by their fixed names (data/no-promote, data/no-train-tonight,
              data/nightly.stop, data/sweep-request.json: present or not, never read), the pending candidate's reason (the
              third field of data/pending-candidate, sent only when it is a plain slug like `promotion_held`), and the
              scorer's /health (up/down, its model name, load time).
  - orchestrator  on hosts that run the owner's main Claude Code sessions: their own tokens per window, by Office session
              tag (cortados_router/orchestrator_usage.py; never a raw session id, path or text). The transcript scan
              runs at most every `every_s` (default 300 s); between scans the cached summary is sent as it was, with
              its own sampled_at, so the provider shows its age.
Never a token, environment, path, command line or file contents beyond those fixed names and the validated reason slug.
The body is checked against the server's own validator (provider/live.validate_heartbeat) and its byte cap before it is
sent, so a sender bug shows here, not as a 400 on the provider.

Config, ~/.config/cortados/heartbeat.json (CORTADOS_HEARTBEAT_CONFIG; written by the install helper):
    {"version": 1, "host": "studio",
     "provider_url": "http://air.example.ts.net:8432",          (default: ~/.config/cortados/provider-url, else loopback)
     "token_file": "~/.config/cortados/live-heartbeat-token",   (0600; the host-bound live:heartbeat client token)
     "ttl_s": 180,
     "jobs": [{"label": "com.cortados.s1-serve", "name": "s1-scorer"}, "com.cortados.s1-nightly"],
     "hardware": true, "disk": true,
     "s1": {"data_dir": "~/cortados/train/data", "health_url": "http://127.0.0.1:8431"},
     "orchestrator": {"projects": "~/.claude/projects", "task_roots": ["~/CortadOS"], "every_s": 300,
                      "cache": "~/.cache/cortados/orchestrator-usage.json"}}       (all optional; absent = off)
`s1.health_url` defaults to the scorer's bind address (s1_endpoint.bind_address; a wildcard bind is asked on loopback).
The disk measured is the home directory's volume.

CLI:  python3 -B -m cortados_router.live_heartbeat [--config PATH] [--dry-run | --check]
Exit 0 posted (or printed, --dry-run; or the config, URL and token file are fine, --check), 1 the post failed, 2 the config or body is invalid (nothing sent).
On an older provider that refuses the `orchestrator` block (400), it posts once more without it; one that also refuses
the `hardware`/`s1` blocks gets them folded into job summaries, so the host still shows while the provider is behind.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):                       # run as a file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortados_router.provider import live  # noqa: E402

CONFIG_ENV = "CORTADOS_HEARTBEAT_CONFIG"
DEFAULT_CONFIG = "~/.config/cortados/heartbeat.json"
DEFAULT_TOKEN_FILE = "~/.config/cortados/live-heartbeat-token"
PROVIDER_URL_FILE = "~/.config/cortados/provider-url"      # the remote hosts' existing pointer (README, exec hosts)
DEFAULT_URL = "http://127.0.0.1:8432"
DEFAULT_TTL_S = 180          # posted every 60 s: three missed posts before the host reads stale
LAUNCHCTL = "/bin/launchctl"   # CORTADOS_LAUNCHCTL overrides it (the install helper's and the tests' seam)
LAUNCHCTL_TIMEOUT = 5
HEALTH_TIMEOUT = 3
POST_TIMEOUT = 10
S1_FLAG_FILES = (("no-promote", "no-promote"), ("no-train-tonight", "no-train-tonight"),
                 ("nightly.stop", "nightly-stop"), ("sweep-request.json", "sweep-requested"))
PENDING_FILE = "pending-candidate"
PENDING_MAX_BYTES = 4096     # four short fields; more is not the marker
HEALTH_MAX_BYTES = 4096
RESERVED_JOBS = ("hardware", "s1")   # legacy(): the blocks folded into jobs for an older provider
# The transcript scan reads every main-session file touched in the last 7 days: cheap enough every 5 min, wasteful
# every minute, and the work split is a day/week measure, so a 5 min old figure is current enough.
ORCH_EVERY_S = 300
ORCH_EVERY_MIN_S, ORCH_EVERY_MAX_S = 60, 3600
ORCH_CACHE = "~/.cache/cortados/orchestrator-usage.json"
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_KV = re.compile(r"^\s*(state|pid|runs|last exit code|last exit status)\s*=\s*(.+?)\s*$")


class ConfigError(ValueError):
    pass


# ================================================================ config
def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    p = Path(os.path.expanduser(str(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)))
    try:
        cfg = json.loads(p.read_text())
    except FileNotFoundError:
        raise ConfigError(f"no config at {p} (scripts/install_live_heartbeat.sh writes it)") from None
    except ValueError as exc:
        raise ConfigError(f"{p}: not JSON ({exc})") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{p}: not an object")
    host = cfg.get("host")
    if not isinstance(host, str) or not live._HOST_RE.fullmatch(host):
        raise ConfigError(f"{p}: host must be a host slug ([a-z][a-z0-9-]{{0,31}})")
    ttl = cfg.get("ttl_s", DEFAULT_TTL_S)
    if not live._num(ttl) or not live.TTL_MIN_S <= ttl <= live.TTL_MAX_S:
        raise ConfigError(f"{p}: ttl_s must be {live.TTL_MIN_S}..{live.TTL_MAX_S}")
    jobs = []
    for j in cfg.get("jobs") or []:
        label, name = (j, None) if isinstance(j, str) else ((j or {}).get("label"), (j or {}).get("name"))
        if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
            raise ConfigError(f"{p}: a job label is not a launchd label")
        name = name or job_name(label)
        if not isinstance(name, str) or not live._JOB_RE.fullmatch(name):
            raise ConfigError(f"{p}: job name for {label} must match {live._JOB_RE.pattern}")
        jobs.append((label, name))
    if len(jobs) > live.JOBS_MAX - 2:                # room for the legacy fold-in jobs
        raise ConfigError(f"{p}: at most {live.JOBS_MAX - 2} jobs")
    if len({n for _, n in jobs}) != len(jobs):
        raise ConfigError(f"{p}: two jobs share a name")
    if {n for _, n in jobs} & set(RESERVED_JOBS):
        raise ConfigError(f"{p}: job names {', '.join(RESERVED_JOBS)} are the legacy body's own")
    s1 = cfg.get("s1")
    if s1 is not None and not isinstance(s1, dict):
        raise ConfigError(f"{p}: s1 must be an object")
    orch = cfg.get("orchestrator")
    if orch is not None:
        if orch is True:
            orch = {}
        if not isinstance(orch, dict) or set(orch) - {"projects", "task_roots", "every_s", "cache"}:
            raise ConfigError(f"{p}: orchestrator must be an object (projects, task_roots, every_s, cache)")
        every = orch.get("every_s", ORCH_EVERY_S)
        if not live._num(every) or not ORCH_EVERY_MIN_S <= every <= ORCH_EVERY_MAX_S:
            raise ConfigError(f"{p}: orchestrator.every_s must be {ORCH_EVERY_MIN_S}..{ORCH_EVERY_MAX_S}")
        roots = orch.get("task_roots", [])
        if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
            raise ConfigError(f"{p}: orchestrator.task_roots must be a list of folders")
        for k in ("projects", "cache"):
            if orch.get(k) is not None and not (isinstance(orch[k], str) and orch[k]):
                raise ConfigError(f"{p}: orchestrator.{k} must be a path")
        orch = {"projects": orch.get("projects"), "task_roots": roots, "every_s": int(every), "cache": orch.get("cache")}
    return {"host": host, "ttl_s": int(ttl), "jobs": jobs, "hardware": cfg.get("hardware", True) is True,
            "disk": cfg.get("disk", True) is True, "s1": s1, "orchestrator": orch,
            "provider_url": cfg.get("provider_url"), "token_file": cfg.get("token_file") or DEFAULT_TOKEN_FILE}


def job_name(label: str) -> str:
    """A launchd label as a job name: lowercase, at most 48 characters (com.cortados.provider stays as it is)."""
    n = re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip("-._")[:48]
    return n or "job"


def provider_url(cfg: dict[str, Any]) -> str:
    url = cfg.get("provider_url")
    if not url:
        try:
            url = Path(os.path.expanduser(PROVIDER_URL_FILE)).read_text().strip()
        except OSError:
            url = DEFAULT_URL
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ConfigError("provider_url must be an http:// or https:// URL")
    return url.rstrip("/")


def read_token(path: str) -> str:
    """The client token from a 0600 file owned by this user; refused when anyone else can read it."""
    p = Path(os.path.expanduser(path))
    try:
        st = p.stat()
    except OSError:
        raise ConfigError(f"no token file at {p}") from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO) or st.st_uid != os.getuid():
        raise ConfigError(f"{p} must be 0600 and owned by this user")
    tok = p.read_text().strip()
    if not tok or len(tok) > 512 or any(c.isspace() for c in tok):
        raise ConfigError(f"{p} does not hold one token")
    return tok


# ================================================================ checks
def launchd_job(label: str, name: str, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """One job from `launchctl print gui/<uid>/<label>`. Only state, pid presence, runs and the last exit are read."""
    try:
        r = run([os.environ.get("CORTADOS_LAUNCHCTL") or LAUNCHCTL, "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True, text=True,
                timeout=LAUNCHCTL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"name": name, "state": "stopped", "summary": f"launchctl failed: {type(exc).__name__}"}
    if r.returncode != 0:
        return {"name": name, "state": "stopped", "summary": "not loaded"}
    kv: dict[str, str] = {}
    depth = 0
    for line in (r.stdout or "").splitlines():
        if depth <= 1:                              # the service's own keys, not a nested block's
            m = _KV.match(line)
            if m and m.group(1) not in kv:
                kv[m.group(1)] = m.group(2)
        depth += line.count("{") - line.count("}")
    running = kv.get("state") == "running"
    runs = kv.get("runs") if (kv.get("runs") or "").isdigit() else None
    last = kv.get("last exit code") or kv.get("last exit status")
    m = re.match(r"(-?\d{1,6})(?!\d)", last or "")   # "78: EX_CONFIG" -> 78; launchd's wording after it is dropped
    code = int(m.group(1)) if m else None
    if running:
        state = "running"
    elif code is None:
        state = "idle"                              # loaded, never exited (or a status launchd words differently)
    else:
        state = "ok" if code == 0 else "failed"
    parts = []
    if running and kv.get("pid", "").isdigit():
        parts.append("pid up")
    if runs is not None:
        parts.append(f"{runs} run" + ("" if runs == "1" else "s"))
    if code is not None:
        parts.append(f"last exit {code}")
    elif last and "never" in last:
        parts.append("never exited")
    return {"name": name, "state": state, "summary": ", ".join(parts)[:80] or None}


def hardware_block(sample: Callable[[], dict[str, Any]] | None = None, disk_path: str | None = "~",
                   disk_usage: Callable[[str], Any] = shutil.disk_usage) -> dict[str, Any]:
    """The heartbeat's hardware block from hardware.sample() (numbers and the chip name only) and the disk's free %."""
    out: dict[str, Any] = {}
    if sample is not None:
        try:
            d = sample() or {}
        except Exception as exc:                     # a failed sample costs the block, never the heartbeat
            print(f"live_heartbeat: hardware sample failed: {type(exc).__name__}", file=sys.stderr)
            d = {}

        def num(k: str) -> Any:
            v = d.get(k)
            return v if live._num(v) else None
        for k in ("cpu_busy_pct", "gpu_busy_pct"):
            v = num(k)
            out[k] = round(min(100.0, max(0.0, v)), 1) if v is not None else None
        for k in ("mem_available_bytes", "mem_total_bytes"):
            v = num(k)
            out[k] = int(v) if v is not None and 0 <= v <= 2 ** 50 else None
        load = d.get("load_avg")
        out["load_1m"] = round(float(load[0]), 2) if isinstance(load, list) and load and live._num(load[0]) \
            and 0 <= load[0] <= 4096 else None
        chip = d.get("chip")
        out["chip"] = chip.strip()[:80] if isinstance(chip, str) and live._SUMMARY_RE.fullmatch(chip.strip()[:80] or "-") \
            else None
        out["sampled_at"] = num("sampled_at")
    if disk_path:
        try:
            u = disk_usage(os.path.expanduser(disk_path))
            out["disk_free_pct"] = round(100.0 * u.free / u.total, 1) if u.total else None
        except OSError:
            out["disk_free_pct"] = None
    return out


def s1_block(cfg: dict[str, Any], fetch: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    """Flags by fixed name (presence only), the pending candidate's reason slug, and the scorer's /health."""
    data = Path(os.path.expanduser(str(cfg.get("data_dir") or "~/cortados/train/data")))
    flags = [flag for fname, flag in S1_FLAG_FILES if (data / fname).exists()]
    pending = None
    try:
        p = data / PENDING_FILE
        if p.is_file() and p.stat().st_size <= PENDING_MAX_BYTES:
            fields = p.read_text(errors="replace").split()
            if len(fields) >= 3 and live._REASON_RE.fullmatch(fields[2]):
                pending = fields[2]
            elif fields:
                pending = "unreadable"
    except OSError:
        pending = "unreadable"
    out: dict[str, Any] = {"health": "unknown", "model": None, "flags": flags, "pending": pending, "loaded_s": None}
    url = cfg.get("health_url") or _s1_health_base()
    if url:
        try:
            body = json.loads((fetch or _get)(url.rstrip("/") + "/health"))
            out["health"] = "up" if isinstance(body, dict) and body.get("ok") is True else "down"
            if isinstance(body, dict):
                m = body.get("model")
                if isinstance(m, str) and live._SUMMARY_RE.fullmatch(m.strip()[:80] or "-") \
                        and not live.office._has_address(m):
                    out["model"] = m.strip()[:80]
                if live._num(body.get("loaded_s")) and 0 <= body["loaded_s"] <= 86400:
                    out["loaded_s"] = round(float(body["loaded_s"]), 1)
        except Exception:                            # unreachable, refused, not JSON: the scorer is down here
            out["health"] = "down"
    return out


def _s1_health_base() -> str | None:
    try:
        from cortados_router.s1_endpoint import bind_address
        host, port = bind_address()
    except Exception:
        return None
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as r:
        return r.read(HEALTH_MAX_BYTES + 1)[:HEALTH_MAX_BYTES]


def orchestrator_block(cfg: dict[str, Any], now: float, scan: Callable[..., dict[str, Any]] | None = None,
                       salt: Callable[[], str | None] | None = None) -> dict[str, Any] | None:
    """This host's main-session tokens (orchestrator_usage.scan), from the cache when it is younger than every_s.
    None when the scan fails (said on stderr by type only; the rest of the heartbeat still goes)."""
    from cortados_router import orchestrator_usage as ou
    cache = Path(os.path.expanduser(cfg.get("cache") or ORCH_CACHE))
    try:
        cached = json.loads(cache.read_text())
        if isinstance(cached, dict) and live._num(cached.get("sampled_at")) \
                and 0 <= now - cached["sampled_at"] < cfg["every_s"] \
                and live.work_split.validate_block(cached, now, live.TIME_SKEW_S)[1] is None:
            return cached                           # an invalid cache (an older sender's format) is rescanned
    except (OSError, ValueError):
        pass                                        # no cache yet, or unreadable: scan
    try:
        block = (scan or ou.scan)(cfg.get("projects") or ou.DEFAULT_PROJECTS, now=now,
                                  salt=(salt or ou.read_salt)(), task_roots=cfg.get("task_roots") or ())
    except Exception as exc:
        print(f"live_heartbeat: orchestrator scan failed: {type(exc).__name__}", file=sys.stderr)
        return None
    bad = live.work_split.validate_block(block, now, live.TIME_SKEW_S)[1]
    if bad is not None:                             # a sender bug costs the block, never the heartbeat
        print(f"live_heartbeat: orchestrator block dropped: field orchestrator{bad} would be refused", file=sys.stderr)
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(block, fh)
        os.replace(tmp, cache)
    except OSError as exc:                          # the block still goes; the next run scans again
        print(f"live_heartbeat: orchestrator cache not written: {type(exc).__name__}", file=sys.stderr)
    return block


# ================================================================ the body
def build(cfg: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
          sample: Callable[[], dict[str, Any]] | None = None, fetch: Callable[[str], bytes] | None = None,
          disk_usage: Callable[[str], Any] = shutil.disk_usage, now: float | None = None,
          scan: Callable[..., dict[str, Any]] | None = None, salt: Callable[[], str | None] | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    body: dict[str, Any] = {"host": cfg["host"], "ttl_s": cfg["ttl_s"],
                            "jobs": [launchd_job(label, name, run) for label, name in cfg["jobs"]]}
    if cfg.get("hardware") or cfg.get("disk"):
        if cfg.get("hardware") and sample is None:
            from cortados_router import hardware
            sample = hardware.sample
        hw = hardware_block(sample if cfg.get("hardware") else None, "~" if cfg.get("disk") else None, disk_usage)
        if hw.get("sampled_at") is not None and abs(hw["sampled_at"] - now) > live.TIME_SKEW_S:
            hw["sampled_at"] = None
        body["hardware"] = hw
    if cfg.get("s1") is not None:
        body["s1"] = s1_block(cfg["s1"], fetch)
    if cfg.get("orchestrator") is not None:
        block = orchestrator_block(cfg["orchestrator"], now, scan, salt)
        if block is not None:
            body["orchestrator"] = block
    return body


def check(body: dict[str, Any], now: float | None = None) -> bytes:
    """The encoded body, after the server's own validation and byte cap; ConfigError naming the bad field otherwise."""
    _, bad = live.validate_heartbeat(body, time.time() if now is None else now)
    if bad:
        raise ConfigError(f"the heartbeat would be refused: field {bad}")
    raw = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > live.HEARTBEAT_MAX_BYTES:
        raise ConfigError(f"the heartbeat is {len(raw)} bytes, over {live.HEARTBEAT_MAX_BYTES}")
    return raw


def legacy(body: dict[str, Any]) -> dict[str, Any]:
    """The same heartbeat for a provider that predates the hardware/s1 blocks: both folded into job summaries."""
    out = {k: v for k, v in body.items() if k not in ("hardware", "s1", "orchestrator")}
    jobs = list(out.get("jobs") or [])
    hw = body.get("hardware")
    if hw:
        bits = [f"{k[:3]} {hw[k]:.0f}%" for k in ("cpu_busy_pct", "gpu_busy_pct") if hw.get(k) is not None]
        if hw.get("mem_available_bytes") is not None and hw.get("mem_total_bytes"):
            bits.append(f"mem {100 - 100 * hw['mem_available_bytes'] / hw['mem_total_bytes']:.0f}% used")
        if hw.get("disk_free_pct") is not None:
            bits.append(f"disk {hw['disk_free_pct']:.0f}% free")
        jobs.append({"name": "hardware", "state": "ok", "summary": ", ".join(bits)[:80] or None})
    s1 = body.get("s1")
    if s1:
        bits = [s1.get("model") or "no model"] + list(s1.get("flags") or []) + \
            ([f"pending {s1['pending']}"] if s1.get("pending") else [])
        jobs.append({"name": "s1", "state": {"up": "running", "down": "failed"}.get(s1.get("health"), "idle"),
                     "summary": "; ".join(bits)[:80]})
    out["jobs"] = jobs[:live.JOBS_MAX]
    return out


def post(url: str, token: str, raw: bytes, opener: Callable[..., Any] = urllib.request.urlopen) -> tuple[int, str]:
    """(HTTP status, the server's error message or "") for one POST; (0, exception type) when it never answered."""
    req = urllib.request.Request(url + live.HEARTBEAT_PATH, data=raw, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with opener(req, timeout=POST_TIMEOUT) as r:
            r.read(4096)
            return r.status, ""
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read(4096) or b"{}").get("error", {}).get("message", "")
        except (ValueError, AttributeError, OSError):
            msg = ""
        return e.code, str(msg)[:200]
    except (OSError, ValueError) as exc:
        return 0, type(exc).__name__


def main(argv: list[str] | None = None, *, run: Callable[..., Any] = subprocess.run,
         sample: Callable[[], dict[str, Any]] | None = None, fetch: Callable[[str], bytes] | None = None,
         opener: Callable[..., Any] = urllib.request.urlopen, disk_usage: Callable[[str], Any] = shutil.disk_usage,
         scan: Callable[..., dict[str, Any]] | None = None, salt: Callable[[], str | None] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Post this host's heartbeat to the CortadOS live view (never prints a token).")
    ap.add_argument("--config", help=f"default ${CONFIG_ENV} or {DEFAULT_CONFIG}")
    ap.add_argument("--dry-run", action="store_true", help="print the body it would post; send nothing")
    ap.add_argument("--check", action="store_true",
                    help="check the config, the provider URL and the token file only; sample and send nothing")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.check:
            provider_url(cfg), read_token(cfg["token_file"])
            print(f"live_heartbeat: config ok for host {cfg['host']} ({len(cfg['jobs'])} jobs)")
            return 0
        body = build(cfg, run=run, sample=sample, fetch=fetch, disk_usage=disk_usage, scan=scan, salt=salt)
        raw = check(body)
        if a.dry_run:
            print(json.dumps(body, indent=1))
            return 0
        url, token = provider_url(cfg), read_token(cfg["token_file"])
    except ConfigError as exc:
        print(f"live_heartbeat: {exc}", file=sys.stderr)
        return 2
    code, msg = post(url, token, raw, opener)
    if code == 400 and "orchestrator" in body:
        # a provider from before the work split: the orchestrator block is an unknown field there
        print(f"live_heartbeat: provider refused the orchestrator block ({msg or 400}); posting without it",
              file=sys.stderr)
        body = {k: v for k, v in body.items() if k != "orchestrator"}
        try:
            raw = check(body)
        except ConfigError as exc:
            print(f"live_heartbeat: {exc}", file=sys.stderr)
            return 2
        code, msg = post(url, token, raw, opener)
    if code == 400 and ("hardware" in body or "s1" in body):
        # an older provider: the extra blocks are an unknown field there. Said once per run, then the legacy body.
        print(f"live_heartbeat: provider refused the full body ({msg or 400}); posting the legacy body", file=sys.stderr)
        try:
            raw = check(legacy(body))
        except ConfigError as exc:
            print(f"live_heartbeat: {exc}", file=sys.stderr)
            return 2
        code, msg = post(url, token, raw, opener)
    if code == 200:
        return 0
    print(f"live_heartbeat: post failed: {code or 'no answer'} {msg}".rstrip(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
