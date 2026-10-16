# PURPOSE: Observe native process identities and contain only this worker's descendants.
# CONTRACT: RT-MCP-001 sections 6-7; creation identity, never PID alone, authorizes signals.
# DEPENDS: Native macOS libproc/sysctl, stdlib; no job-store or routing dependencies.
"""Process ownership and strict termination evidence for the captured macOS host."""
import ctypes
import ctypes.util
import errno
import json
import os
from pathlib import Path
import signal
import sys
import uuid


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
        "svuid", "svgid", "rfu")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "e_tdev", "e_tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True)
    _LIBPROC.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    _LIBPROC.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _LIBPROC.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
else:
    _LIBPROC = None


def host_boot_id():
    """Return the native boot identity, never an observation timestamp."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        size = ctypes.c_size_t()
        if libc.sysctlbyname(b"kern.bootsessionuuid", None, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), "Cannot read host boot identity")
        value = ctypes.create_string_buffer(size.value)
        if libc.sysctlbyname(b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), "Cannot read host boot identity")
        return value.value.decode("ascii")
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _observation(pid):
    if _LIBPROC is not None:
        info = _BSDInfo()
        # PROC_PIDTBSDINFO=3 is the native API selector, not a tunable limit.
        ctypes.set_errno(0)
        size = _LIBPROC.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == 0 and ctypes.get_errno() == errno.ESRCH:
            return None
        if size != ctypes.sizeof(info):
            raise OSError(ctypes.get_errno(), "Native process identity could not be observed")
        if info.status == 5:  # SZOMB: no executable process remains.
            return None
        return ({"pid": pid, "pgid": info.pgid,
                 "creation_time_us": info.start_sec * 1_000_000 + info.start_usec}, info.ppid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        created = boot * 1_000_000 + int(fields[19]) * 1_000_000 // os.sysconf("SC_CLK_TCK")
        return ({"pid": pid, "pgid": int(fields[2]), "creation_time_us": created}, int(fields[1]))
    except (FileNotFoundError, ProcessLookupError):
        return None


def process_identity(pid):
    observation = _observation(pid)
    return observation[0] if observation else None


def process_table():
    if _LIBPROC is not None:
        # Extra capacity permits concurrent fork activity between native count/list calls.
        # Doubling slots costs bounded snapshot memory; no margin could omit a new descendant.
        capacity = max(1, _LIBPROC.proc_listallpids(None, 0)) * 2
        pids = (ctypes.c_int * capacity)()
        count = _LIBPROC.proc_listallpids(pids, ctypes.sizeof(pids))
        if count <= 0 or count >= capacity:
            raise OSError("Native process inventory is incomplete")
        ids = [int(pid) for pid in pids[:count] if pid > 0]
    else:
        ids = [int(path.name) for path in Path("/proc").iterdir() if path.name.isdigit()]
    table = {}
    for pid in ids:
        try:
            row = _observation(pid)
        except PermissionError:
            # Keep inaccessible SIP/other-user processes explicitly unknown. An owned PID
            # or child in this set fails closed below; unrelated OS processes are not exits.
            table[pid] = (None, None)
            continue
        if row is not None:
            table[pid] = row
    return table


def _child_ids(parent, table):
    if _LIBPROC is None:
        return [pid for pid, (_, ppid) in table.items() if ppid == parent]
    # Two inventory-sized buffers allow forks during the prior observation; exhaustion
    # refuses containment instead of silently omitting a potentially effectful child.
    capacity = max(1, len(table)) * 2
    buffer = (ctypes.c_int * capacity)()
    count = _LIBPROC.proc_listchildpids(parent, buffer, ctypes.sizeof(buffer))
    if count < 0 or count >= capacity:
        raise OSError("Native child inventory is incomplete")
    return [pid for pid in buffer[:count] if pid > 0]


def _identity_valid(value):
    return (isinstance(value, dict) and set(value) == {"pid", "pgid", "creation_time_us"}
            and all(type(value[key]) is int and value[key] >= minimum
                    for key, minimum in (("pid", 1), ("pgid", 1), ("creation_time_us", 0))))


def _uuid4(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def validate_receipt(receipt, job_id, launch_nonce, boot_id, watchdog=None):
    """Validate the full closed schema and launch binding before releasing a claim."""
    fields = {"schema", "job_id", "launch_nonce", "host_boot_id", "watchdog", "native",
              "observed", "contained", "observed_at_ms"}
    if not isinstance(receipt, dict) or set(receipt) != fields:
        return False
    if (receipt["schema"] != "cortados.mcp-worker.termination.v1"
            or not _uuid4(receipt["job_id"]) or not _uuid4(receipt["launch_nonce"])
            or receipt["job_id"] != job_id or receipt["launch_nonce"] != launch_nonce
            or not isinstance(receipt["host_boot_id"], str) or not receipt["host_boot_id"]
            or receipt["host_boot_id"] != boot_id or receipt["contained"] is not True
            or type(receipt["observed_at_ms"]) is not int or receipt["observed_at_ms"] < 0
            or not _identity_valid(receipt["watchdog"])):
        return False
    if watchdog is not None and receipt["watchdog"] != watchdog:
        return False
    native, observed = receipt["native"], receipt["observed"]
    if native is not None and not _identity_valid(native):
        return False
    if not isinstance(observed, list) or not all(_identity_valid(row) for row in observed):
        return False
    if len({row["pid"] for row in observed}) != len(observed):
        return False
    return (native is None and not observed) or (native is not None and native in observed)


def atomic_json(path, payload):
    temporary = Path(str(path) + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _same_process(actual, expected):
    return actual is not None and (actual["pid"], actual["creation_time_us"]) == (expected["pid"], expected["creation_time_us"])


class ProcessTracker:
    """Own observed descendants even when a child creates another group or session."""
    def __init__(self, native):
        self.native = native
        self.observed = {native["pid"]: native}
        self.lost_ownership = False
        self.inventory_failed = False

    def sample(self):
        try:
            table = self._sample()
            if not self.inventory_failed:
                return table
        except OSError:
            # Unknown observation is never evidence of exit. Preserve every known
            # identity as potentially live and disable group signals until handback.
            self.inventory_failed = self.lost_ownership = True
        return {pid: (identity, None) for pid, identity in self.observed.items()}

    def _sample(self):
        table = process_table()
        parents = set(self.observed)
        added = True
        while added:
            added = False
            for pid, (identity, parent) in table.items():
                if pid not in parents and parent in parents:
                    previous = self.observed.get(parent)
                    current = table.get(parent)
                    if previous and current and not _same_process(current[0], previous):
                        self.lost_ownership = True
                        continue
                    self.observed[pid] = identity
                    parents.add(pid)
                    added = True
        for pid, expected in list(self.observed.items()):
            if pid in table:
                current = table[pid][0]
                if not _same_process(current, expected):
                    self.lost_ownership = True
                    if current is None:
                        self.inventory_failed = True
                else:
                    # setsid/setpgid changes the group, not the process creation identity.
                    self.observed[pid] = current
                    if pid == self.native["pid"]:
                        self.native = current
            for child in _child_ids(pid, table):
                if child in table and table[child][0] is None:
                    self.inventory_failed = self.lost_ownership = True
        return table

    def remaining(self):
        table = self.sample()
        return [row for pid, row in self.observed.items() if table.get(pid, (None,))[0] == row]

    def signal_groups(self, sig, *, root_only=False):
        table = self.sample()
        if self.inventory_failed:
            return
        live = {pid: row for pid, row in self.observed.items() if table.get(pid, (None,))[0] == row}
        root_group = self.native["pgid"]
        groups = ({root_group} if root_only else {row["pgid"] for row in live.values()})
        # Descendant groups first; root last. Group membership is checked in this observation.
        for pgid in sorted(groups, key=lambda group: group == root_group):
            members = [row for row, _ in table.values() if row is not None and row["pgid"] == pgid]
            if not members:
                continue
            if any(live.get(row["pid"]) != row for row in members):
                self.lost_ownership = True
                continue
            try:
                current = [process_identity(row["pid"]) for row in members]
            except OSError:
                self.inventory_failed = self.lost_ownership = True
                return
            if any(actual is not None and not _same_process(actual, expected)
                   for actual, expected in zip(current, members)):
                self.lost_ownership = True
                continue
            if any(actual is not None and actual["pgid"] != pgid for actual in current):
                # The child regrouped after the snapshot. Resample before authorizing
                # the new group; never signal a stale PGID from a prior observation.
                continue
            if not any(current):  # Natural exit during verification is proof of exit, not PID reuse.
                continue
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                continue

    def contained(self):
        return not self.remaining() and not self.lost_ownership
