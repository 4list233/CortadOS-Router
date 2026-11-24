"""The local gate's platform seam: the few calls that differ between macOS (the Pro) and Windows (the PC node, #145).

local_gate.py, and node_policy.py on Windows, call only these for what POSIX and Windows do differently. On macOS each
function does exactly what local_gate.py did before this module existed (same calls, same messages), so the Pro's gate
is unchanged. On Windows:

  lock_file        msvcrt.locking on the first byte, retried without blocking until LOCK_WAIT_S (fcntl.flock does not
                   exist there). Used for the gate epoch file only (local_gate.next_epoch).
  NOFOLLOW         os.O_NOFOLLOW does not exist on Windows; is_link() refuses a reparse point (symlink or junction)
                   from lstat instead, before the file is opened.
  fsync_dir        a directory cannot be opened with os.open on Windows; NTFS journals the rename's metadata, so the
                   directory fsync is skipped there (the file itself is still fsynced before the rename).
  token_file_problem   a mode check means nothing on NTFS (st_mode is 0o666 / 0o444 whatever the ACL). The token file's
                   owner and DACL are read instead (WinApi.file_security): the owner must be the gate's own account,
                   SYSTEM or Administrators, and no ACE may grant anyone else (Everyone, Users, Authenticated Users,
                   another account) any access. Administrators and SYSTEM are allowed because they can read every file
                   on the machine anyway (take ownership), so their ACE exposes nothing more; the owner decided on
                   2026-11-24 that the gate may run as the administrator account.

WinApi is the only code that touches ctypes, and only on Windows (imported lazily). The tests run on macOS through
fakes of WinApi and of the module's WINDOWS flag; nothing here is run on the PC by the tests. Stdlib only.
"""
from __future__ import annotations

import os
import stat
import sys
import time

WINDOWS = sys.platform == "win32"
if WINDOWS:
    fcntl = None                       # no fcntl on Windows: lock_file uses msvcrt
else:
    import fcntl

# os.O_NOFOLLOW where it exists; 0 on Windows, where is_link() checks the path before it is opened.
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# msvcrt.locking's LK_NBLCK fails at once when another process holds the byte. The epoch file is held for a read, a
# small write and an fsync (milliseconds); two gates starting together wait for each other well within 30 s, and a
# holder that never lets go makes the gate refuse to start rather than hang at boot.
LOCK_WAIT_S = 30.0
LOCK_RETRY_S = 0.05
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Well-known SIDs allowed on the token file besides the gate's own account (module docstring).
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"
TRUSTED_SIDS = frozenset({SID_SYSTEM, SID_ADMINISTRATORS})
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
INHERIT_ONLY_ACE = 0x08


class LockTimeout(OSError):
    pass


def lock_file(fd: int, *, windows: bool | None = None, msvcrt_mod=None, clock=time.monotonic,
              sleep=time.sleep) -> None:
    """An exclusive lock on the open file `fd`, held until the fd is closed. POSIX: fcntl.flock(LOCK_EX) (blocking,
    as before). Windows: msvcrt.locking(LK_NBLCK) on byte 0, retried every LOCK_RETRY_S up to LOCK_WAIT_S, then
    LockTimeout. `windows` and `msvcrt_mod` are the tests' seams."""
    if not (WINDOWS if windows is None else windows):
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if msvcrt_mod is None:
        import msvcrt as msvcrt_mod                           # Windows only
    os.lseek(fd, 0, os.SEEK_SET)                              # msvcrt.locking locks from the current position
    end = clock() + LOCK_WAIT_S
    while True:
        try:
            msvcrt_mod.locking(fd, msvcrt_mod.LK_NBLCK, 1)
            return
        except OSError:
            if clock() >= end:
                raise LockTimeout(f"another process held the lock for more than {LOCK_WAIT_S:g}s") from None
            sleep(LOCK_RETRY_S)


def is_link(st: os.stat_result) -> bool:
    """True for a symlink, and on Windows also for any reparse point (a junction is not S_ISLNK)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def fsync_dir(path: str, *, windows: bool | None = None) -> None:
    """fsync a directory after a rename into it (POSIX). Skipped on Windows (module docstring)."""
    if WINDOWS if windows is None else windows:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def acl_problem(owner_sid: str | None, aces: list[tuple[int, int, str]] | None, user_sid: str) -> str | None:
    """Why a token file's owner and DACL let more than the gate's account read it, or None. Pure, the Windows rule:
    `aces` are (ace type, ace flags, SID string); None is a NULL DACL (everyone has full access). Allowed trustees:
    `user_sid` (the gate's account), SYSTEM and Administrators. An inherit-only ACE does not apply to the file itself;
    a deny ACE takes access away; any other ACE type is refused because its layout is not read here (fail closed)."""
    allowed = TRUSTED_SIDS | {user_sid}
    if aces is None:
        return "has no DACL (everyone has full access)"
    if owner_sid not in allowed:
        return f"is owned by {owner_sid or 'an unreadable owner'}, not by the gate's account, SYSTEM or Administrators"
    for kind, flags, sid in aces:
        if flags & INHERIT_ONLY_ACE or kind == ACCESS_DENIED_ACE_TYPE:
            continue
        if kind != ACCESS_ALLOWED_ACE_TYPE:
            return f"has an ACE of type {kind} this check does not read (remove it)"
        if sid not in allowed:
            return f"grants access to {sid} (only the gate's account, SYSTEM and Administrators may have any)"
    return None


def token_file_problem(path, st: os.stat_result, *, windows: bool | None = None, api=None) -> str | None:
    """Why the gate's token file is readable by more than its owner, or None. POSIX: the mode (the message local_gate
    gave before). Windows: the owner and DACL (acl_problem); a file whose security cannot be read is refused."""
    if not (WINDOWS if windows is None else windows):
        if st.st_mode & 0o077:
            return (f"has mode {stat.S_IMODE(st.st_mode):04o}; group/other access must be "
                    "none (chmod 600)")
        return None
    api = api or WinApi()
    try:
        owner, aces = api.file_security(str(path))
        user = api.current_user_sid()
    except OSError as exc:
        return f"security descriptor unreadable ({type(exc).__name__}); refusing it"
    why = acl_problem(owner, aces, user)
    if why is None:
        return None
    return (f"{why}; set it with icacls (scripts/windows/install_pc_node.ps1 shows the command: inheritance removed, "
            "read for the gate's account, full for SYSTEM and Administrators)")


class WinApi:
    """The Windows calls the PC node needs, through ctypes (stdlib). Each method raises OSError when the call fails,
    so a caller fails closed. Constructed only on Windows: ctypes.windll does not exist elsewhere. The tests replace
    the whole object with a fake."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _fail(self, what: str, code: int | None = None) -> OSError:
        code = self.ct.get_last_error() if code is None else code
        return OSError(code, f"{what} failed (Windows error {code})")

    def _sid_string(self, psid) -> str:
        ct, wt = self.ct, self.wt
        out = wt.LPWSTR()
        if not self.advapi32.ConvertSidToStringSidW(psid, ct.byref(out)):
            raise self._fail("ConvertSidToStringSidW")
        try:
            return out.value
        finally:
            self.kernel32.LocalFree(out)

    def file_security(self, path: str) -> tuple[str | None, list[tuple[int, int, str]] | None]:
        """(owner SID, [(ace type, ace flags, SID)] or None for a NULL DACL) of a file."""
        ct, wt = self.ct, self.wt
        owner, dacl, sd = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
        OWNER_SECURITY_INFORMATION, DACL_SECURITY_INFORMATION, SE_FILE_OBJECT = 0x1, 0x4, 1
        rc = self.advapi32.GetNamedSecurityInfoW(wt.LPCWSTR(path), SE_FILE_OBJECT,
                                                 OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                                 ct.byref(owner), None, ct.byref(dacl), None, ct.byref(sd))
        if rc != 0:
            raise self._fail("GetNamedSecurityInfoW", rc)
        try:
            owner_sid = self._sid_string(owner) if owner.value else None
            if not dacl.value:
                return owner_sid, None

            class ACL_SIZE_INFORMATION(ct.Structure):
                _fields_ = [("AceCount", wt.DWORD), ("AclBytesInUse", wt.DWORD), ("AclBytesFree", wt.DWORD)]

            class ACE_HEADER(ct.Structure):
                _fields_ = [("AceType", wt.BYTE), ("AceFlags", wt.BYTE), ("AceSize", wt.WORD)]

            info = ACL_SIZE_INFORMATION()
            AclSizeInformation = 2
            if not self.advapi32.GetAclInformation(dacl, ct.byref(info), ct.sizeof(info), AclSizeInformation):
                raise self._fail("GetAclInformation")
            aces = []
            for i in range(info.AceCount):
                pace = ct.c_void_p()
                if not self.advapi32.GetAce(dacl, i, ct.byref(pace)):
                    raise self._fail("GetAce")
                head = ACE_HEADER.from_address(pace.value)
                sid = ""
                if head.AceType in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                    # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: header (4 bytes), Mask (DWORD), then the SID
                    sid = self._sid_string(ct.c_void_p(pace.value + 8))
                aces.append((int(head.AceType), int(head.AceFlags), sid))
            return owner_sid, aces
        finally:
            self.kernel32.LocalFree(sd)

    def current_user_sid(self) -> str:
        """The SID of the account this process runs as (its token's TokenUser)."""
        ct, wt = self.ct, self.wt
        TOKEN_QUERY, TokenUser = 0x0008, 1
        token = wt.HANDLE()
        self.kernel32.GetCurrentProcess.restype = wt.HANDLE
        # wrapped: a bare int would be passed as a 32-bit C int, and the pseudo-handle (-1) needs all 64 bits
        process = wt.HANDLE(self.kernel32.GetCurrentProcess())
        if not self.advapi32.OpenProcessToken(process, TOKEN_QUERY, ct.byref(token)):
            raise self._fail("OpenProcessToken")
        try:
            size = wt.DWORD()
            self.advapi32.GetTokenInformation(token, TokenUser, None, 0, ct.byref(size))
            buf = ct.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(token, TokenUser, buf, size, ct.byref(size)):
                raise self._fail("GetTokenInformation")
            psid = ct.c_void_p.from_buffer(buf).value          # TOKEN_USER.User.Sid is the first field
            return self._sid_string(ct.c_void_p(psid))
        finally:
            self.kernel32.CloseHandle(token)

    def ac_line_status(self) -> int:
        """GetSystemPowerStatus's ACLineStatus: 0 offline (battery), 1 online (AC), 255 unknown."""
        ct, wt = self.ct, self.wt

        class SYSTEM_POWER_STATUS(ct.Structure):
            _fields_ = [("ACLineStatus", wt.BYTE), ("BatteryFlag", wt.BYTE), ("BatteryLifePercent", wt.BYTE),
                        ("SystemStatusFlag", wt.BYTE), ("BatteryLifeTime", wt.DWORD),
                        ("BatteryFullLifeTime", wt.DWORD)]

        s = SYSTEM_POWER_STATUS()
        if not self.kernel32.GetSystemPowerStatus(ct.byref(s)):
            raise self._fail("GetSystemPowerStatus")
        return int(s.ACLineStatus)

    def memory_load_pct(self) -> int:
        """GlobalMemoryStatusEx's dwMemoryLoad: the percent of physical memory in use (0-100)."""
        ct, wt = self.ct, self.wt

        class MEMORYSTATUSEX(ct.Structure):
            _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ct.c_ulonglong),
                        ("ullAvailPhys", ct.c_ulonglong), ("ullTotalPageFile", ct.c_ulonglong),
                        ("ullAvailPageFile", ct.c_ulonglong), ("ullTotalVirtual", ct.c_ulonglong),
                        ("ullAvailVirtual", ct.c_ulonglong), ("ullAvailExtendedVirtual", ct.c_ulonglong)]

        m = MEMORYSTATUSEX()
        m.dwLength = ct.sizeof(MEMORYSTATUSEX)
        if not self.kernel32.GlobalMemoryStatusEx(ct.byref(m)):
            raise self._fail("GlobalMemoryStatusEx")
        return int(m.dwMemoryLoad)

    def session_identity(self, session_id: int) -> dict | None:
        """{"id", "user" ("DOMAIN\\name"), "logon" (the session's logon time, FILETIME ticks)} of one session, or None
        when no user is signed in to it. node_policy binds each activity record to this triple: the writer stamps its own
        session's (current_session), the gate compares it with each active session's (active_sessions). Both sides read
        it through this one call, so they agree; the logon time tells a new logon from an earlier one that reused the
        session id. WTSSessionInfo answers for any session from session 0."""
        ct, wt = self.ct, self.wt
        wtsapi32 = ct.WinDLL("wtsapi32", use_last_error=True)

        class WTSINFOW(ct.Structure):
            # wtsapi32.h: WINSTATIONNAME_LENGTH 32, DOMAIN_LENGTH 17, USERNAME_LENGTH 20 (+1); LARGE_INTEGER times.
            _fields_ = [("State", ct.c_int), ("SessionId", wt.DWORD), ("IncomingBytes", wt.DWORD),
                        ("OutgoingBytes", wt.DWORD), ("IncomingFrames", wt.DWORD), ("OutgoingFrames", wt.DWORD),
                        ("IncomingCompressedBytes", wt.DWORD), ("OutgoingCompressedBytes", wt.DWORD),
                        ("WinStationName", wt.WCHAR * 32), ("Domain", wt.WCHAR * 17), ("UserName", wt.WCHAR * 21),
                        ("ConnectTime", ct.c_longlong), ("DisconnectTime", ct.c_longlong),
                        ("LastInputTime", ct.c_longlong), ("LogonTime", ct.c_longlong), ("CurrentTime", ct.c_longlong)]

        WTSSessionInfo = 24
        buf, size = ct.c_void_p(), wt.DWORD()
        if not wtsapi32.WTSQuerySessionInformationW(None, wt.DWORD(session_id), WTSSessionInfo, ct.byref(buf),
                                                    ct.byref(size)):
            raise self._fail("WTSQuerySessionInformationW")
        try:
            if not buf.value or size.value < ct.sizeof(WTSINFOW):
                raise OSError(0, "WTSQuerySessionInformationW returned a short WTSINFOW")
            info = ct.cast(buf, ct.POINTER(WTSINFOW)).contents
            if not info.UserName:
                return None
            return {"id": int(session_id), "user": f"{info.Domain}\\{info.UserName}", "logon": int(info.LogonTime)}
        finally:
            wtsapi32.WTSFreeMemory(buf)

    def active_sessions(self) -> list:
        """session_identity of every session that is WTSActive with a signed-in user (the console or a remote desktop;
        a locked session is still active, a disconnected one is not). Works from session 0, where the gate runs."""
        ct, wt = self.ct, self.wt
        wtsapi32 = ct.WinDLL("wtsapi32", use_last_error=True)

        class WTS_SESSION_INFOW(ct.Structure):
            _fields_ = [("SessionId", wt.DWORD), ("pWinStationName", wt.LPWSTR), ("State", ct.c_int)]

        WTSActive = 0
        info, count = ct.POINTER(WTS_SESSION_INFOW)(), wt.DWORD()
        if not wtsapi32.WTSEnumerateSessionsW(None, 0, 1, ct.byref(info), ct.byref(count)):
            raise self._fail("WTSEnumerateSessionsW")
        try:
            ids = [int(info[i].SessionId) for i in range(count.value) if info[i].State == WTSActive]
        finally:
            wtsapi32.WTSFreeMemory(info)
        return [s for s in (self.session_identity(i) for i in ids) if s is not None]

    def current_session(self) -> dict:
        """session_identity of this process's own session (the activity writer's). OSError when it has no user."""
        ct, wt = self.ct, self.wt
        sid = wt.DWORD()
        if not self.kernel32.ProcessIdToSessionId(self.kernel32.GetCurrentProcessId(), ct.byref(sid)):
            raise self._fail("ProcessIdToSessionId")
        got = self.session_identity(int(sid.value))
        if got is None:
            raise OSError(0, "this process's session has no signed-in user")
        return got

    def input_idle_s(self) -> float:
        """Seconds since the last keyboard or mouse input IN THIS PROCESS'S SESSION (GetLastInputInfo is per session:
        run it in the owner's interactive session, node_policy's activity writer, never in session 0)."""
        ct, wt = self.ct, self.wt
        user32 = ct.WinDLL("user32", use_last_error=True)

        class LASTINPUTINFO(ct.Structure):
            _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

        li = LASTINPUTINFO()
        li.cbSize = ct.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ct.byref(li)):
            raise self._fail("GetLastInputInfo")
        self.kernel32.GetTickCount.restype = wt.DWORD
        # Both are 32-bit millisecond tick counts that wrap every 49.7 days; the difference modulo 2**32 does not.
        return ((self.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0

    def notification_state(self) -> int:
        """SHQueryUserNotificationState in this process's session: 1 not present, 2 busy (a full-screen app),
        3 running a Direct3D full-screen app, 4 presentation mode, 5 accepts notifications, 6 quiet time, 7 an app."""
        ct = self.ct
        shell32 = ct.WinDLL("shell32", use_last_error=True)
        state = ct.c_int()
        hr = shell32.SHQueryUserNotificationState(ct.byref(state))
        if hr != 0:
            raise OSError(hr & 0xFFFFFFFF, f"SHQueryUserNotificationState failed (HRESULT {hr & 0xFFFFFFFF:#x})")
        return int(state.value)
