"""When a local node that is someone's everyday machine may serve: the PC node's policy (#145).

Owner decisions. 2026-11-24 (first): serve only while the owner is not using the PC, plus a manual toggle. Changed the
same day (2026-11-24, fix round 1): the PC is on and idle nearly all the time and the owner games 3-4 times a week for
1-2 h, so the default is to SERVE unless the toggle is off, a full-screen app or game runs in a signed-in session, or
other programs use the GPU. Recent keyboard or mouse input alone no longer stops serving (idle_after_s is off by
default and stays available as an option).

The Pro's gate decides with local_resources.pro_local_allowed (manual switch + AC power). A gate whose backend config
has a `node_policy` section of kind `windows-idle` decides with WindowsIdlePolicy.allowed() instead, in this order,
each refusal a fixed reason (REFUSALS, so the provider may repeat it to a client: local_lane.PRO_LOCAL_REFUSALS):

  1. the manual switch: the same flag file as on the Pro (local_resources.local_switch_path, set through the gate's
     /cortados/local-switch by the provider's local_switch tool or the PC's own toggle script, whose "serving OFF
     (game)" shortcut is the owner's way to stop it). Off: "local switch off".
  2. AC power (GetSystemPowerStatus), as on the Pro; `require_ac: false` for a desktop that reports "unknown".
  3. the signed-in sessions. Every session that is active with a user (WTSEnumerateSessions from session 0) must be
     covered by a fresh activity record bound to THAT session: written by the activity writer running in it (at logon,
     `python -m cortados_router.node_policy activity`), into its own file (session_activity_path), stamped with the
     session's id, user and logon time (gate_platform.WinApi.session_identity), which the gate compares with what it
     reads for the session. A missing, stale, malformed or mismatched record (another session's, or an earlier logon's
     that reused the session id) is unknown: refused. A record saying a full-screen app holds the display refuses
     ("full-screen app running"); one that could not read it ("unknown") refuses too. No session active (signed out, a
     disconnected remote desktop): nothing can be full-screen, so this check passes. Optional (off by default):
     idle_after_s refuses while input in any active session is more recent than that ("owner active").
     Why a writer: GetLastInputInfo and SHQueryUserNotificationState answer for the CALLER's session only, and the gate
     runs in session 0 (a startup task, so it is up with nobody signed in), where they would always say "idle".
  4. memory load (GlobalMemoryStatusEx), only when max_memory_load_pct is set (default off: with the 35B loaded,
     Strata keeps ~19 GiB of experts in host RAM and measured free RAM was 0.4 GiB, ~99 % load, so a threshold would
     refuse all the time until the owner measures what is normal).
  5. the GPU: nvidia-smi's utilization.gpu at or over gpu_busy_pct means another program (a game, a render, a video
     export) is using the card. The gate's own decoding must not count as another program, and the gate cannot
     attribute utilization per process (see "Own GPU use" below), so a reading is taken only while the gate is quiet:
     no turn of its own in flight, and own_gpu_settle_s since the last one ended (the check waits out the rest of that
     time). While a turn of its own runs, the last such reading under the threshold stands, for at most
     own_gpu_skip_max_s; past that the state is unknown (refused) until a quiet reading. Turns ending never extend
     that window: only a fresh reading does.
  6. the switch again (the samples above may have taken a moment: never admit after the owner turned it off).

Every unreadable input is a refusal, never "free" (no silent fallback). Samples (power, sessions, the activity files,
memory) are re-read at most every sample_s; the switch is read on every call. The GPU decision is made on every call
(fix round 2): own_gpu_skip_max_s is checked each time, and an nvidia-smi reading is reused for sample_s only while no
turn of the gate's own has started or ended since it was taken, so the first check after a turn ends takes a fresh
reading, waiting out what is left of own_gpu_settle_s first. In-flight turns are never stopped when
a game starts: the gate only stops admitting (a running turn is bounded by the gate's read timeout and its max_tokens; at
~30 tok/s a 4K-token answer is about two minutes). To free the GPU at once, the PC's toggle script's `off -FreeGpu` also
asks Strata to unload the model (scripts/windows/cortados-serving.ps1).

Own GPU use (reviewer finding, fix round 1). The first version skipped the GPU check for own_gpu_grace_s after each
forwarded turn ended, and every ending renewed it, so turns under 10 s apart were admitted forever even with another
program saturating the card. Excluding Strata's own share per process was considered and not chosen: nvidia-smi's
per-process query (--query-compute-apps) reports memory, not utilization, and its per-process utilization monitor
(pmon) is not reliably available under the WDDM driver this desktop card runs; Windows' "GPU Engine" performance
counters do give per-process utilization, but need Strata's PID (it is started by a separate task) and a PDH ctypes
binding that cannot be exercised before the PC deploy. Measuring only while the gate is quiet gives the same answer for
the case that matters (a conversation's next turn arrives seconds after the last ended) with no new Windows code.

Thresholds (each with its reason; owner may change them in the node's local-backend.json):
  idle_after_s null       off (owner 2026-11-24): input alone does not stop serving. A number (seconds) turns the old
                          "no input for N seconds" rule back on.
  activity_stale_s 20     the writer writes every 5 s; four missed writes mean it stopped (crash, logoff mid-write).
  gpu_busy_pct 40         desktop compositing, an animated wallpaper and video decode stay well under 40 % of an RTX
                          3060; a game or a render runs far above it.
  own_gpu_settle_s 1.5    utilization.gpu covers the last 1/6 s to 1 s (nvidia-smi documents the sample period), so
                          1.5 s after the gate's last turn ended its own decoding is out of the reading.
  own_gpu_skip_max_s 60   the longest a quiet reading stands while the gate's own turn runs: a status or probe call
                          during a long turn is answered from it; after a minute it says unknown rather than guess.
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
    "idle_after_s": None,                # off (owner 2026-11-24, module docstring); seconds to turn it on
    "activity_file": None,               # required: an absolute path the writer and the gate share
    "activity_stale_s": 20.0,
    "fullscreen_blocks": True,
    "gpu_busy_pct": 40,
    "own_gpu_settle_s": 1.5,
    "own_gpu_skip_max_s": 60.0,
    "nvidia_smi": "nvidia-smi",
    "nvidia_smi_timeout_s": 2.0,
    "gpu_index": 0,
    "max_memory_load_pct": None,
    "require_ac": True,
    "sample_s": 3.0,
}
NUMBER_KEYS = ("activity_stale_s", "own_gpu_settle_s", "own_gpu_skip_max_s", "nvidia_smi_timeout_s", "sample_s")
OPTIONAL_NUMBER_KEYS = ("idle_after_s",)
# The writer's interval: activity_stale_s must leave room for at least a few missed writes.
WRITE_INTERVAL_S = 5.0
# A clock step between the writer and the gate (the same machine, so only an NTP correction): an activity file
# timestamped this far in the future is still accepted; further is unreadable (a broken clock must not keep it fresh).
FUTURE_SKEW_S = 5.0
ACTIVITY_MAX_BYTES = 4096
# 2: the record names its session (fix round 1, reviewer finding: version 1 records had no session identity).
ACTIVITY_VERSION = 2
SESSION_USER_MAX = 64

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
ALLOWED = "local enabled: manual switch on, no full-screen app, GPU free"
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
    for key in OPTIONAL_NUMBER_KEYS:
        v = out[key]
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise PolicyConfigError(f"node_policy.{key} must be a positive number of seconds, or null (off), got {v!r}")
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


def session_ok(sess: Any) -> bool:
    """A well-formed session identity: {"id": int >= 0, "user": printable, "logon": int} (WinApi.session_identity)."""
    if not isinstance(sess, dict) or set(sess) != {"id", "user", "logon"}:
        return False
    sid, user, logon = sess["id"], sess["user"], sess["logon"]
    return (not isinstance(sid, bool) and isinstance(sid, int) and sid >= 0
            and isinstance(user, str) and 0 < len(user) <= SESSION_USER_MAX and user.isprintable()
            and not isinstance(logon, bool) and isinstance(logon, int))


def session_activity_path(base: str, session_id: int) -> str:
    """The activity file of one session: `<base stem>.s<id><ext>` beside the configured activity_file. One file per
    session, so a writer can only ever speak for its own session (two writers never race on one file)."""
    root, ext = os.path.splitext(base)
    return f"{root}.s{int(session_id)}{ext or '.json'}"


def read_activity(path: str, now: float, stale_s: float, session: dict[str, Any]) -> dict[str, Any] | None:
    """The activity file's {"idle_s", "fullscreen"} when it is fresh, well formed and bound to `session` (the same id,
    user and logon time), else None (unknown)."""
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
    if not session_ok(data.get("session")) or data["session"] != session:
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
                 switch_on: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self._api = api
        self._run, self._clock, self._wall, self._sleep = run, clock, wall, sleep
        self._switch_on = switch_on or (lambda: local_resources.local_switch_path().is_file())
        self._lock = threading.Lock()
        self._sample: tuple[float, str | None, dict[str, Any]] | None = None
        self._own = 0                         # this gate's turns in flight (forward_started / forward_ended)
        self._own_ended = float("-inf")       # when the last one ended (a reading waits own_gpu_settle_s after it)
        self._own_epoch = 0                   # bumped on every start and end: a reading spanning one is mixed
        self._gpu_quiet_at = float("-inf")    # the last reading under the threshold taken while the gate was quiet
        self._gpu_reading: tuple[float, int, int | None] | None = None   # (when, own epoch, util) of the last quiet one

    @property
    def api(self):
        if self._api is None:
            self._api = gate_platform.WinApi()
        return self._api

    # ---- this gate's own GPU use
    def forward_started(self) -> None:
        with self._lock:
            self._own += 1
            self._own_epoch += 1

    def forward_ended(self) -> None:
        """A turn ended (answered, failed or cut off). It never extends how long the GPU check may be skipped: only a
        fresh quiet reading does (module docstring, "Own GPU use")."""
        with self._lock:
            self._own = max(0, self._own - 1)
            self._own_epoch += 1
            self._own_ended = self._clock()

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
        """(refusal or None, the owner word: away | present | idle | active | fullscreen | unknown). Every active
        session must be covered by its own fresh, bound activity record (module docstring, step 3)."""
        try:
            sessions = self.api.active_sessions()
        except OSError:
            return OWNER_UNKNOWN, "unknown"
        if not isinstance(sessions, list) or not all(session_ok(x) for x in sessions):
            return OWNER_UNKNOWN, "unknown"
        if not sessions:
            return None, "away"
        now = self._wall()
        acts = [read_activity(session_activity_path(self.cfg["activity_file"], x["id"]), now,
                              self.cfg["activity_stale_s"], x) for x in sessions]
        if any(a is None for a in acts):
            return OWNER_UNKNOWN, "unknown"
        if self.cfg["fullscreen_blocks"]:
            if any(a["fullscreen"] == "unknown" for a in acts):
                return OWNER_UNKNOWN, "unknown"
            if any(a["fullscreen"] != "none" for a in acts):
                return FULLSCREEN, "fullscreen"
        idle_after = self.cfg["idle_after_s"]
        if idle_after is None:
            return None, "present"
        if any(a["idle_s"] < idle_after for a in acts):
            return OWNER_ACTIVE, "active"
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
        """Another program's GPU use (module docstring, step 5 and "Own GPU use")."""
        top = self.cfg["gpu_busy_pct"]
        if top is None:
            return None
        with self._lock:
            own, ended, epoch = self._own, self._own_ended, self._own_epoch
        if own == 0:
            got = self._gpu_reading
            if got is not None and got[1] == epoch and 0 <= self._clock() - got[0] < self.cfg["sample_s"]:
                # the same quiet stretch (no turn started or ended since), within sample_s: no new nvidia-smi run
                return GPU_UNKNOWN if got[2] is None else GPU_BUSY if got[2] >= top else None
            wait = self.cfg["own_gpu_settle_s"] - (self._clock() - ended)
            if wait > 0:
                self._sleep(wait)                 # the gate's own last decoding leaves the reading
            util = gpu_utilization(self.cfg, self._run)
            with self._lock:
                quiet = self._own == 0 and self._own_epoch == epoch
                if quiet:
                    self._gpu_reading = (self._clock(), epoch, util)
            if util is None:
                return GPU_UNKNOWN
            with self._lock:
                if quiet:
                    # a busy reading also withdraws the last good one: a call during a turn never leans on it
                    self._gpu_quiet_at = self._clock() if util < top else float("-inf")
            if quiet:
                return GPU_BUSY if util >= top else None
            # a turn of the gate's own started or ended during the reading: it may be in it; fall through
        with self._lock:
            quiet_at = self._gpu_quiet_at
        if 0 <= self._clock() - quiet_at < self.cfg["own_gpu_skip_max_s"]:
            return None
        return GPU_UNKNOWN

    def _inputs(self) -> tuple[str | None, dict[str, Any]]:
        """(the first refusal among power, owner and memory, or None; the words for the status), re-read at most every
        sample_s. The GPU is not in this cache: its answer depends on the gate's own turns starting and ending and on
        own_gpu_skip_max_s, so _gpu decides on every check (fix round 2: a cached GPU answer outlived both)."""
        with self._lock:
            got = self._sample
        now = self._clock()
        if got is not None and 0 <= now - got[0] < self.cfg["sample_s"]:
            return got[1], got[2]
        why, power = self._power()
        owner_why, owner = self._owner()
        why = why or owner_why or self._memory()
        words = {"power": power, "activity": owner}
        with self._lock:
            self._sample = (self._clock(), why, words)
        return why, words

    def _sampled(self) -> str | None:
        """The first refusal among power, owner, memory and GPU, or None."""
        why, _ = self._inputs()
        return why or self._gpu()

    def allowed(self) -> tuple[bool, str]:
        """(allowed, reason): the decision for one new turn (module docstring)."""
        why = self._switch()
        if why:
            return False, why
        why = self._sampled()
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
        _, words = self._inputs()
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
    """One activity record of THIS session: its identity (WinApi.current_session), the input idle time and the
    full-screen state. A failed fullscreen read is written as "unknown" (the gate refuses on it); a failed identity or
    idle read raises OSError (nothing is written, the file goes stale and the gate refuses)."""
    session = api.current_session()
    if not session_ok(session):
        raise OSError(0, "the session identity is malformed")
    idle = api.input_idle_s()
    try:
        fs = FULLSCREEN_STATES.get(api.notification_state(), "none")
    except OSError:
        fs = "unknown"
    return {"v": ACTIVITY_VERSION, "at": wall(), "session": session, "idle_s": round(float(idle), 1),
            "fullscreen": fs}


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
    """The writer loop: one record every `interval` seconds, into this session's own file
    (session_activity_path(path, id)). A failed sample or write is logged and retried next time; the file then goes
    stale and the gate refuses (fail closed). `once`: one record, exit 0 / 1."""
    api = api or gate_platform.WinApi()
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    while True:
        try:
            record = activity_sample(api, wall)
            write_activity(session_activity_path(path, record["session"]["id"]), record)
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
    w.add_argument("--out", required=True, help="the gate's node_policy.activity_file; the writer adds its session id")
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
