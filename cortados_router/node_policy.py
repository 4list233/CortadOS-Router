"""When a local node that is someone's everyday machine may serve: the PC node's "owner away" policy (#145, owner
decision 2026-11-24: the gaming PC serves ONLY while the owner is not using it, plus a manual toggle).

The Pro's gate decides with local_resources.pro_local_allowed (manual switch + AC power). A gate whose backend config
has a `node_policy` section of kind `windows-idle` decides with WindowsIdlePolicy.allowed() instead, in this order,
each refusal a fixed reason (REFUSALS, so the provider may repeat it to a client: local_lane.PRO_LOCAL_REFUSALS):

  1. the manual switch: the same flag file as on the Pro (local_resources.local_switch_path, set through the gate's
     /cortados/local-switch by the provider's local_switch tool or the PC's own toggle script). Off: "local switch off".
  2. AC power (GetSystemPowerStatus), as on the Pro; `require_ac: false` for a desktop that reports "unknown".
  3. the owner. Signed-in sessions are counted from session 0 (WTSEnumerateSessions). None active (signed out, or a
     disconnected remote desktop): the owner is away. One or more: the activity file written by the activity writer
     (run in the owner's session at logon, `python -m cortados_router.node_policy activity`) must be fresh and say
     input idle >= idle_after_s and no full-screen app. Stale, missing or unreadable: unknown, refused.
     Why a writer: GetLastInputInfo and SHQueryUserNotificationState answer for the CALLER's session only, and the gate
     runs in session 0 (a startup task, so it is up with nobody signed in), where they would always say "idle".
  4. the GPU: nvidia-smi's utilization.gpu at or over gpu_busy_pct means another program (a game, a render, a video
     export) is using the card. Skipped while this gate has a turn in flight and for own_gpu_grace_s after the last one
     ended: the gate's own decoding is the GPU load then, and refusing a conversation's next turn for it would break the
     conversation (an auto-routed conversation stays bound to its node).
  5. memory load (GlobalMemoryStatusEx), only when max_memory_load_pct is set (default off: with the 35B loaded,
     Strata keeps ~19 GiB of experts in host RAM and measured free RAM was 0.4 GiB, ~99 % load, so a threshold would
     refuse all the time until the owner measures what is normal).
  6. the switch again (the samples above may have taken a moment: never admit after the owner turned it off).

Every unreadable input is a refusal, never "free" (no silent fallback). Samples (power, sessions, the activity file,
memory) are re-read at most every sample_s; the switch is read on every call. In-flight turns are never stopped when the
owner comes back: the gate only stops admitting (a running turn is bounded by the gate's read timeout and its
max_tokens; at ~30 tok/s a 4K-token answer is about two minutes). To free the GPU at once, the PC's toggle script's
`off -FreeGpu` also asks Strata to unload the model (scripts/windows/cortados-serving.ps1).

Thresholds (each with its reason; owner may change them in the node's local-backend.json):
  idle_after_s 300        5 minutes without keyboard or mouse input. Shorter, a pause in a game or a long read would
                          start serving under the owner; longer wastes the evening. A model load takes ~195 s, so
                          serving starts about 8 minutes after the owner stops.
  activity_stale_s 20     the writer writes every 5 s; four missed writes mean it stopped (crash, logoff mid-write).
  gpu_busy_pct 40         desktop compositing, an animated wallpaper and video decode stay well under 40 % of an RTX
                          3060; a game or a render runs far above it.
  own_gpu_grace_s 10      utilization.gpu averages over up to a second, and a client sends its next turn within seconds.
  sample_s 3              power and activity changes stop new admissions within seconds, without a subprocess or
                          file read per request (the same as the Pro's POWER_CACHE_S).
  nvidia_smi_timeout_s 2  nvidia-smi answers in well under a second; 2 s keeps the switch status within the
                          provider's 6 s wait (provider/local_switch.GATE_TIMEOUT_S).
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from cortados_router import gate_platform, local_resources

KIND_WINDOWS_IDLE = "windows-idle"
KINDS = (KIND_WINDOWS_IDLE,)
DEFAULTS: dict[str, Any] = {
    "idle_after_s": 300.0,
    "activity_file": None,               # required: an absolute path the writer and the gate share
    "activity_stale_s": 20.0,
    "fullscreen_blocks": True,
    "gpu_busy_pct": 40,
    "own_gpu_grace_s": 10.0,
    "nvidia_smi": "nvidia-smi",
    "nvidia_smi_timeout_s": 2.0,
    "gpu_index": 0,
    "max_memory_load_pct": None,
    "require_ac": True,
    "sample_s": 3.0,
}
NUMBER_KEYS = ("idle_after_s", "activity_stale_s", "own_gpu_grace_s", "nvidia_smi_timeout_s", "sample_s")
# The writer's interval: activity_stale_s must leave room for at least a few missed writes.
WRITE_INTERVAL_S = 5.0
# A clock step between the writer and the gate (the same machine, so only an NTP correction): an activity file
# timestamped this far in the future is still accepted; further is unreadable (a broken clock must not keep it fresh).
FUTURE_SKEW_S = 5.0
ACTIVITY_MAX_BYTES = 4096
ACTIVITY_VERSION = 1

# The fixed refusal reasons (step numbers from the module docstring). The provider repeats only these
# (local_lane.PRO_LOCAL_REFUSALS; a test keeps the sets in step).
SWITCH_OFF = "local switch off"
ON_BATTERY = "local disabled: on battery"
POWER_UNKNOWN = "local disabled: unknown power source"
POWER_FAILED = "local disabled: power check failed"
OWNER_ACTIVE = "local disabled: owner active"
OWNER_UNKNOWN = "local disabled: owner activity unknown"
FULLSCREEN = "local disabled: full-screen app running"
GPU_BUSY = "local disabled: GPU busy with another program"
GPU_UNKNOWN = "local disabled: GPU state unknown"
MEMORY_HIGH = "local disabled: memory load high"
MEMORY_UNKNOWN = "local disabled: memory state unknown"
SWITCH_UNREADABLE = "local disabled: switch unreadable"
REFUSALS = frozenset({SWITCH_OFF, ON_BATTERY, POWER_UNKNOWN, POWER_FAILED, OWNER_ACTIVE, OWNER_UNKNOWN, FULLSCREEN,
                      GPU_BUSY, GPU_UNKNOWN, MEMORY_HIGH, MEMORY_UNKNOWN, SWITCH_UNREADABLE})
ALLOWED = "local enabled: owner away + manual switch on"
# SHQueryUserNotificationState values that mean a full-screen app holds the display: QUNS_BUSY (2, full-screen or
# borderless full-screen app), QUNS_RUNNING_D3D_FULL_SCREEN (3), QUNS_PRESENTATION_MODE (4).
FULLSCREEN_STATES = {2: "busy", 3: "d3d", 4: "presentation"}


class PolicyConfigError(ValueError):
    pass


def check_config(raw: Any) -> dict[str, Any] | None:
    """The validated `node_policy` section (None when absent: the Pro's policy). Unknown keys and bad values are
    refused: a threshold silently ignored would decide by a number nobody chose."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PolicyConfigError("node_policy must be an object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise PolicyConfigError(f"node_policy.kind must be one of {', '.join(KINDS)}, got {kind!r}")
    out = dict(DEFAULTS, kind=kind)
    for key, v in raw.items():
        if key.startswith("_") or key == "kind":
            continue
        if key not in DEFAULTS:
            raise PolicyConfigError(f"node_policy: unknown key {key!r}")
        out[key] = v
    for key in NUMBER_KEYS:
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise PolicyConfigError(f"node_policy.{key} must be a positive number of seconds, got {v!r}")
        out[key] = float(v)
    af = out["activity_file"]
    if not isinstance(af, str) or not af.strip():
        raise PolicyConfigError("node_policy.activity_file must name the file the activity writer keeps fresh")
    # "~" is the account's profile: the gate's startup task and the writer's logon task run as the same account
    out["activity_file"] = os.path.expanduser(af.strip())
    if out["activity_stale_s"] < 2 * WRITE_INTERVAL_S:
        raise PolicyConfigError(f"node_policy.activity_stale_s must be at least {2 * WRITE_INTERVAL_S:g} (the writer "
                                f"writes every {WRITE_INTERVAL_S:g}s)")
    for key in ("gpu_busy_pct", "max_memory_load_pct"):
        v = out[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100):
            raise PolicyConfigError(f"node_policy.{key} must be an integer percent from 1 to 100, or null (off)")
    gi = out["gpu_index"]
    if isinstance(gi, bool) or not isinstance(gi, int) or gi < 0:
        raise PolicyConfigError("node_policy.gpu_index must be a non-negative integer")
    if not isinstance(out["nvidia_smi"], str) or not out["nvidia_smi"].strip():
        raise PolicyConfigError("node_policy.nvidia_smi must name the nvidia-smi executable")
    for key in ("fullscreen_blocks", "require_ac"):
        if not isinstance(out[key], bool):
            raise PolicyConfigError(f"node_policy.{key} must be true or false")
    return out


def read_activity(path: str, now: float, stale_s: float) -> dict[str, Any] | None:
    """The activity file's {"idle_s", "fullscreen"} when it is fresh and well formed, else None (unknown)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(ACTIVITY_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > ACTIVITY_MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("v") != ACTIVITY_VERSION:
        return None
    at, idle, fs = data.get("at"), data.get("idle_s"), data.get("fullscreen")
    for v in (at, idle):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
    if idle < 0 or not (now - stale_s <= at <= now + FUTURE_SKEW_S):
        return None
    if fs not in ("none", "unknown", *FULLSCREEN_STATES.values()):
        return None
    return {"idle_s": float(idle), "fullscreen": fs}


def gpu_utilization(cfg: dict[str, Any], run: Callable = subprocess.run) -> int | None:
    """nvidia-smi's utilization.gpu (percent) of the configured GPU, or None when it cannot be read."""
    argv = [cfg["nvidia_smi"], "-i", str(cfg["gpu_index"]), "--query-gpu=utilization.gpu",
            "--format=csv,noheader,nounits"]
    try:
        r = run(argv, capture_output=True, text=True, timeout=cfg["nvidia_smi_timeout_s"])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(r, "returncode", 1) != 0 or not isinstance(r.stdout, str):
        return None
    lines = [x.strip() for x in r.stdout.strip().splitlines() if x.strip()]
    if len(lines) != 1 or not lines[0].isdigit():
        return None
    v = int(lines[0])
    return v if 0 <= v <= 100 else None


class WindowsIdlePolicy:
    """The windows-idle policy (module docstring). `api` (gate_platform.WinApi), `run` (subprocess.run, for
    nvidia-smi), `clock` (monotonic), `wall` (time.time) and `switch_on` are the tests' seams."""

    def __init__(self, cfg: dict[str, Any], *, api=None, run: Callable = subprocess.run,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 switch_on: Callable[[], bool] | None = None):
        self.cfg = cfg
        self._api = api
        self._run, self._clock, self._wall = run, clock, wall
        self._switch_on = switch_on or (lambda: local_resources.local_switch_path().is_file())
        self._lock = threading.Lock()
        self._sample: tuple[float, str | None, dict[str, Any]] | None = None
        self._own = 0                         # this gate's turns in flight (forward_started / forward_ended)
        self._own_until = float("-inf")       # the GPU check is skipped until then (own_gpu_grace_s)

    @property
    def api(self):
        if self._api is None:
            self._api = gate_platform.WinApi()
        return self._api

    # ---- this gate's own GPU use
    def forward_started(self) -> None:
        with self._lock:
            self._own += 1

    def forward_ended(self) -> None:
        with self._lock:
            self._own = max(0, self._own - 1)
            self._own_until = self._clock() + self.cfg["own_gpu_grace_s"]

    def _own_gpu(self) -> bool:
        with self._lock:
            return self._own > 0 or self._clock() < self._own_until

    # ---- the inputs
    def _switch(self) -> str | None:
        try:
            return None if self._switch_on() else SWITCH_OFF
        except OSError:
            return SWITCH_UNREADABLE

    def _power(self) -> tuple[str | None, str]:
        try:
            line = self.api.ac_line_status()
        except OSError:
            return POWER_FAILED, "unknown"
        power = {1: "ac", 0: "battery"}.get(line, "unknown")
        if not self.cfg["require_ac"]:
            return None, power
        return {"ac": None, "battery": ON_BATTERY}.get(power, POWER_UNKNOWN), power

    def _owner(self) -> tuple[str | None, str]:
        """(refusal or None, the owner word: away | idle | active | unknown)."""
        try:
            sessions = self.api.active_user_sessions()
        except OSError:
            return OWNER_UNKNOWN, "unknown"
        if sessions == 0:
            return None, "away"
        act = read_activity(self.cfg["activity_file"], self._wall(), self.cfg["activity_stale_s"])
        if act is None:
            return OWNER_UNKNOWN, "unknown"
        if act["idle_s"] < self.cfg["idle_after_s"]:
            return OWNER_ACTIVE, "active"
        if self.cfg["fullscreen_blocks"]:
            if act["fullscreen"] == "unknown":
                return OWNER_UNKNOWN, "unknown"
            if act["fullscreen"] != "none":
                return FULLSCREEN, "active"
        return None, "idle"

    def _memory(self) -> str | None:
        top = self.cfg["max_memory_load_pct"]
        if top is None:
            return None
        try:
            load = self.api.memory_load_pct()
        except OSError:
            return MEMORY_UNKNOWN
        return MEMORY_HIGH if load > top else None

    def _gpu(self) -> str | None:
        top = self.cfg["gpu_busy_pct"]
        if top is None or self._own_gpu():
            return None
        util = gpu_utilization(self.cfg, self._run)
        if util is None:
            return GPU_UNKNOWN
        return GPU_BUSY if util >= top else None

    def _sampled(self) -> tuple[str | None, dict[str, Any]]:
        """(the first refusal among power, owner, memory and GPU, or None; the words for the status), re-read at most
        every sample_s. The GPU part is re-read each time this gate's own use has just ended (own_gpu)."""
        with self._lock:
            got = self._sample
        now = self._clock()
        if got is not None and 0 <= now - got[0] < self.cfg["sample_s"]:
            return got[1], got[2]
        why, power = self._power()
        owner_why, owner = self._owner()
        why = why or owner_why or self._memory() or self._gpu()
        words = {"power": power, "activity": owner}
        with self._lock:
            self._sample = (self._clock(), why, words)
        return why, words

    def allowed(self) -> tuple[bool, str]:
        """(allowed, reason): the decision for one new turn (module docstring)."""
        why = self._switch()
        if why:
            return False, why
        why, _ = self._sampled()
        if why:
            return False, why
        why = self._switch()                  # never admit after the owner turned it off during the samples
        if why:
            return False, why
        return True, ALLOWED

    def status(self) -> dict[str, Any]:
        """The switch status the gate answers (local_gate.GateHandler.switch_status): the same fields as
        local_resources.local_switch_status, plus `activity` (away | idle | active | unknown)."""
        allowed, reason = self.allowed()
        _, words = self._sampled()
        try:
            switch = "on" if self._switch_on() else "off"
        except OSError:
            switch = "unknown"
        return {"switch": switch, "allowed": allowed, "reason": reason, "power": words["power"],
                "activity": words["activity"]}


def build(cfg: dict[str, Any] | None, **seams):
    """The policy object for a checked config, or None (the Pro's pro_local_allowed)."""
    if cfg is None:
        return None
    if cfg["kind"] == KIND_WINDOWS_IDLE:
        return WindowsIdlePolicy(cfg, **seams)
    raise PolicyConfigError(f"unknown node_policy kind {cfg['kind']!r}")


# ---- the activity writer (runs in the owner's interactive session; scripts/windows/install_pc_node.ps1)
def activity_sample(api, wall: Callable[[], float] = time.time) -> dict[str, Any]:
    """One activity record: the input idle time and the full-screen state of THIS session. A failed fullscreen read is
    written as "unknown" (the gate refuses on it); a failed idle read raises OSError (nothing is written, the file
    goes stale and the gate refuses)."""
    idle = api.input_idle_s()
    try:
        fs = FULLSCREEN_STATES.get(api.notification_state(), "none")
    except OSError:
        fs = "unknown"
    return {"v": ACTIVITY_VERSION, "at": wall(), "idle_s": round(float(idle), 1), "fullscreen": fs}


def write_activity(path: str, record: dict[str, Any]) -> None:
    """Write the record atomically (temp file + rename): the gate never reads half a file. On Windows the rename fails
    with a sharing violation while the gate has the file open for its read (milliseconds, every sample_s at most);
    run_writer logs it and writes again 5 s later, and activity_stale_s allows several such misses."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="ascii") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def run_writer(path: str, *, api=None, once: bool = False, interval: float = WRITE_INTERVAL_S,
               sleep: Callable[[float], None] = time.sleep, wall: Callable[[], float] = time.time,
               log=None) -> int:
    """The writer loop: one record every `interval` seconds. A failed sample or write is logged and retried next
    time; the file then goes stale and the gate refuses (fail closed). `once`: one record, exit 0 / 1."""
    api = api or gate_platform.WinApi()
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    while True:
        try:
            write_activity(path, activity_sample(api, wall))
            ok = True
        except OSError as exc:
            log(f"node_policy activity: sample or write failed ({type(exc).__name__}); the gate will see the file "
                "go stale and refuse")
            ok = False
        if once:
            return 0 if ok else 1
        sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cortados_router.node_policy", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("activity", help="write the owner-activity file (run in the owner's session at logon)")
    w.add_argument("--out", required=True, help="the activity file (the node_policy.activity_file of the gate)")
    w.add_argument("--once", action="store_true", help="write one record and exit (acceptance check)")
    s = sub.add_parser("status", help="print the policy decision now, from the gate's backend config")
    s.add_argument("--backend-config", default=None, help="the gate's local-backend.json")
    a = ap.parse_args(argv)
    if a.cmd == "activity":
        if not gate_platform.WINDOWS:
            print("node_policy: the activity writer runs on Windows only", file=sys.stderr)
            return 2
        return run_writer(a.out, once=a.once)
    from cortados_router import local_backend
    try:
        cfg = local_backend.load(a.backend_config, explicit=a.backend_config is not None)
    except local_backend.BackendConfigError as exc:
        print(f"node_policy: {exc}", file=sys.stderr)
        return 2
    policy = build(cfg.get("node_policy"))
    if policy is None:
        print(json.dumps(local_resources.local_switch_status()))
        return 0
    print(json.dumps(policy.status()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
