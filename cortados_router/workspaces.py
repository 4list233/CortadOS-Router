"""Task workspaces on an execution host (owner decision 2026-11-08, issues #86/#89; layout: ~/CortadOS/README.md there).

  <root>/repos/<name>      one clone per repository; every task worktree branches from it
  <root>/active/<task_id>  the task's working folder while it runs: a git worktree, or a plain folder (no repo)
  <root>/archive/<task_id> finished and pushed (or no repository): HEAD detached at its commit so the branch is free
                           for the next task; deleted by cleanup_archive() after ARCHIVE_AGE_S
  <root>/held/<task_id>    finished with unpushed work (any file not proven published, regenerable caches aside; see
                           CACHE_DIR_NAMES): keeps its branch checked out (a new task on that branch is refused);
                           never deleted automatically, listed for the owner; `held_paths` names what held it
  <root>/index.jsonl       one line per task (rewritten in place under index.lock, never appended twice for a task)

Only archive/ is ever deleted from, only entries named like a task id, only regular directories directly inside it
(never a symlink, never through one), only with an index record saying `archived` long enough ago. held/, repos/,
services/ and everything else are never touched: cleanup_archive() refuses them by construction, not by convention.

<root>/cleanup.lock serializes the cleanup's reassess-through-delete section with every move of a worktree between the
layout directories: settle()'s move out of active/ and a task start that frees a legacy archived worktree. The cleanup's
run budget (CLEANUP_MAX_SECONDS) bounds how long it holds the lock, because every git call it makes gets the time left
as its timeout; a start or a settle waits for the lock at most CLEANUP_LOCK_WAIT_S. A start is then refused with a
retryable reason; a settle leaves the folder in active/ marked `settle_deferred`, and the next cleanup settles it (#93).
An assessment that times out is an unknown: nothing is deleted or moved on it.

Lock order (#93): repos/.<name>.lock -> cleanup.lock -> index.lock, never the reverse. A start holds its clone's lock
and may then take cleanup.lock (bounded); the cleanup and settle() take cleanup.lock and never a clone lock; index.lock
is a leaf. settle() takes cleanup.lock rather than the clone lock because the cleanup would otherwise need the clone
lock inside cleanup.lock, the reverse of a start's order.

Worktree admin dirs (<clone>/.git/worktrees/<id>: the worktree's HEAD, index and reflogs; HEAD and index keep an
unpublished detached commit reachable) are never pruned globally (#93). `git worktree prune` drops EVERY entry whose path is
missing, and a moved but not yet repaired worktree looks exactly like that, so this module never runs it. Instead:
  - the cleanup removes only the admin dir of the worktree it deletes, resolved from that worktree's own .git file and
    checked to be inside the clone and to name the deleted path, through no-follow descriptors;
  - every managed clone has gc.worktreePruneExpire=never and runs no automatic gc or maintenance at all (gc.auto=0,
    gc.autoDetach=false, maintenance.auto=false; set on clone, checked on every prepare, settle and cleanup; every fetch
    also passes -c gc.auto=0 -c maintenance.auto=false). Even with an expiry of never, gc's `git worktree prune` drops
    every registry entry whose backlink duplicates another's (a registry_conflict leaves exactly that), so no gc may run
    unasked (#93 round 4). An explicit `git gc` by a person is outside this module: fix a registry_conflict first;
  - every move (settle's, the cleanup's move to held/) is a rename followed by `git worktree repair` of the new path
    under its own fixed cap; a failed repair is recorded (`repair_error`), never rolled back;
  - every cleanup, and every start that finds cleanup.lock free, repairs the registry entry of any worktree under
    active/, archive/ or held/ whose admin dir names another path, under a deadline and with a per-entry backoff
    recorded in index.jsonl, unless the path it names is an existing worktree whose .git names the admin dir back:
    then the admin dir belongs to that worktree, the entry is a conflict (reported, `registry_conflict`), and
    nothing is rewritten. A deletion likewise refuses an admin dir that any other existing worktree names.
A registry entry at a stale path is therefore never destructive: it only waits for the next repair.

Stdlib only. git runs as a subprocess with GIT_TERMINAL_PROMPT=0 (an unauthenticated clone fails instead of hanging).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import time

ARCHIVE_AGE_S = 3 * 24 * 3600       # owner decision 2026-11-08: archived (pushed) work is kept three days, then deleted
TASK_ID_RE = re.compile(r"t\d{14}-[0-9a-f]{8}")     # child_tasks.TASK_ID_RE; repeated here so this module stands alone
# A clone directory name under repos/: from the URL's last path segment. Dots, dashes and underscores as git hosts
# allow; no leading dot or dash (hidden dirs, option-looking names); 64 is plenty for any repository name.
REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# git check-ref-format, the subset we accept: no control chars/space/~^:?*[\ , no "..", no "@{", no leading "-" or "/",
# no trailing "/", ".lock" or "."; 128 chars is our bound (a branch name longer than that is pasted text, not a name).
BRANCH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,127}")
TASK_BRANCH_PREFIX = "task/"        # a task that names no branch works on task/<task_id>, never on the default branch
# Timeouts (seconds). A clone over the tailnet of a large repository takes minutes; past these it is stuck, not slow.
CLONE_TIMEOUT_S = 900
FETCH_TIMEOUT_S = 300
GIT_TIMEOUT_S = 60                  # local operations: worktree add/move/remove, status, rev-list
CLEANUP_MAX_DELETIONS = 100          # daily cleanup stays bounded even after a long outage
# The cleanup's whole-run budget. It is checked between entries and, since #93, carried into every git call an entry
# makes (each call gets min(its own cap, time left)), so one hung fetch cannot hold cleanup.lock past the budget.
CLEANUP_MAX_SECONDS = 300            # held moves and failures consume this budget too
# How long a task start waits for cleanup.lock before it is refused (#93). A cleanup entry normally holds the lock for
# one fetch plus a few local commands (seconds); 60 s covers a slow one, while a start that waited the full cleanup
# budget (300 s) would already have failed its caller's expectations. The refusal is retryable, never a bypass.
CLEANUP_LOCK_WAIT_S = 60
# settle() waits as long as a start for the lock (a cleanup entry is seconds); past it the move is deferred, not lost.
SETTLE_LOCK_WAIT_S = CLEANUP_LOCK_WAIT_S
CLEANUP_LOCK_POLL_S = 0.1            # between non-blocking flock attempts: no spin, and at most 0.1 s added after release
GIT = "git"                          # resolved on PATH at call time (the execution host's PATH is set by its entry)
SUBDIRS = ("repos", "active", "archive", "held")
# A registry repair that failed is retried after 1 h, doubling per consecutive failure up to a week (#93 round 3): the
# daily cleanup still retries a transient failure the next day, and a persistently hanging entry costs one capped
# attempt per backoff period instead of the front of every run's budget.
REPAIR_BACKOFF_BASE_S = 3600
REPAIR_BACKOFF_MAX_S = 7 * 24 * 3600
# Ignored content that may be archived (and so deleted with the archive after ARCHIVE_AGE_S) without being published.
# Only regenerable tool caches: deleting them loses nothing a rerun does not rebuild. Every other ignored file is owner
# data (this repo's own s1/data/ holds S1 training data and is git-ignored), so it still holds the workspace. Empty
# ignored directories hold nothing and never block (2026-11-13: five pushed worktrees were held for an empty
# s1/data/outcome/journals/ that a test left behind).
CACHE_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
CACHE_FILE_SUFFIXES = (".pyc", ".pyo")
IGNORED_WALK_MAX = 20000            # entries walked per assessment; past it the content is unknown, so it holds
HELD_PATHS_MAX = 20                 # paths recorded as `held_paths` (names only); the reason names the first few
# settle() reassesses a worktree held only for leftover files after these pauses (seconds): the child's process group
# is already stopped, but a process that left the group (setsid, nohup) may still be removing its temp file. A
# reassessment can only turn "held" into "archived" when it proves everything published again; ~7 s in total.
SETTLE_RECHECK_DELAYS_S = (2.0, 5.0)
# Writer quiescence (review of held-settle-fix, P1): a worktree is archived (settle) or deleted (cleanup) only when two
# clean assessments, separated by a pause, are bracketed by identical snapshots of the tree (file set, sizes, mtimes,
# ctimes and inodes of everything outside the regenerable caches). settle() pauses SETTLE_RECHECK_DELAYS_S; the
# cleanup pauses CLEANUP_QUIESCE_S per entry it would delete. Anything that cannot be snapshotted holds.
CLEANUP_QUIESCE_S = 2.0
SNAPSHOT_MAX = 200000               # entries per snapshot; past it quiescence cannot be established, so it holds
# Code points HFS+/APFS ignore when comparing names (git's is_hfs_dotgit list): ".g\u200cit" names .git there too.
_HFS_IGNORABLE = frozenset(map(chr, (0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                                     0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F, 0xFEFF)))
_sleep = time.sleep                 # the pauses between assessments; tests patch this seam, never the shared time.sleep


class WorkspaceError(Exception):
    pass


class _Reason(str):
    """A settle/hold reason (human text, may name paths) carrying a path-free `code` for exports (`reason_code`)."""
    def __new__(cls, code: str, text: str):
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


def reason_code(reason) -> str:
    return getattr(reason, "code", None) or ("pushed" if reason == "pushed" else "other")


def _is_git_meta(name: str) -> bool:
    """Whether a directory entry named `name` is git metadata (a .git directory, or a .git file of a worktree or
    submodule) on a case-insensitive file system: .GIT and .Git are .git on macOS, as is a name with HFS-ignorable
    code points in it."""
    return "".join(c for c in name if c not in _HFS_IGNORABLE).lower() == ".git"


class GitTimeoutError(WorkspaceError):
    """A git call hit its timeout (or had no time left before it started). Nothing is known about what it would have
    reported, so callers that act on git's answer must treat this as "unknown", never as "clean" or "not pushed"."""


# Every fetch runs with automatic gc and maintenance off, whatever the clone's config says (belt and braces next to
# protect_clone): a fetch ends with `git maintenance run --auto`, whose gc prunes duplicate worktree entries (#93).
# And with hooks off: a fetch runs the reference-transaction hook, and a hook that writes a file into the worktree
# being assessed (pushed() fetches from inside it) would make a clean, pushed worktree look dirty and hold it.
_FETCH_NO_GC = ("-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.hooksPath=/dev/null")


def _git(args, cwd=None, timeout=GIT_TIMEOUT_S) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    pre = _FETCH_NO_GC if args and args[0] == "fetch" else ()
    try:
        p = subprocess.run([GIT, *pre, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise WorkspaceError("git is not installed or not on PATH on this execution host") from None
    except subprocess.TimeoutExpired:
        raise GitTimeoutError(f"git {args[0]} did not finish within {timeout:g} s") from None
    if p.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed (exit {p.returncode}): {p.stderr.strip()[-400:]}")
    return p.stdout


def _within(deadline, cap: float) -> float:
    """The timeout for one git call under an overall deadline (a time.monotonic() value, or None for no deadline):
    the call's own cap, or less when the deadline comes first. Raises GitTimeoutError when no time is left, so a call
    that could not finish anyway is not started (and the caller handles it exactly like a call that timed out)."""
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    if left <= 0:
        raise GitTimeoutError("no time left in the cleanup budget before this git call started")
    return min(cap, left)


def parse_repo(repo):
    """(url, name) for a `repo` argument: a clone URL/path (anything with '://', 'git@...' or a '/' in it) gives both;
    a bare name gives (None, name) and must already be cloned under repos/."""
    if not isinstance(repo, str) or not repo.strip():
        raise WorkspaceError("repo must be a clone URL (https://..., git@host:owner/name.git) or the name of an existing clone")
    repo = repo.strip()
    if any(c.isspace() for c in repo) or repo.startswith("-"):
        raise WorkspaceError(f"repo {repo!r} is not a clone URL or a clone name")
    is_url = "://" in repo or repo.startswith("git@") or "/" in repo or ":" in repo
    last = repo.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] if is_url else repo
    name = last[:-4] if last.endswith(".git") else last
    if not REPO_NAME_RE.fullmatch(name):
        raise WorkspaceError(f"cannot derive a clone name from repo {repo!r}: the last path segment must match "
                             f"{REPO_NAME_RE.pattern}")
    return (repo if is_url else None), name


def check_branch(branch) -> str:
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch) or ".." in branch or "@{" in branch \
            or branch.endswith(("/", ".lock", ".")) or "//" in branch:
        raise WorkspaceError(f"branch {branch!r} is not a valid git branch name")
    return branch


def ensure_root(root: str) -> str:
    """The workspace root must already exist (it is the host's layout, created by its owner); its four subdirectories
    are created if missing. index.jsonl is created on first write."""
    if not os.path.isdir(root):
        raise WorkspaceError(f"workspace root {root} does not exist on this execution host")
    for sub in SUBDIRS:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def workspace_spec(task_id: str, repo=None, branch=None) -> dict:
    """What the runner will prepare: validated now (so start_task refuses a bad name at once), executed by prepare()
    later (a clone can take minutes; start_task must answer in seconds)."""
    if not TASK_ID_RE.fullmatch(task_id):
        raise WorkspaceError(f"invalid task id {task_id!r}")
    if repo is None:
        if branch is not None:
            raise WorkspaceError("branch needs repo")
        return {"kind": "folder", "task_id": task_id}
    url, name = parse_repo(repo)
    return {"kind": "repo", "task_id": task_id, "url": url, "name": name,
            "branch": check_branch(branch) if branch is not None else None}


def _worktree_branches(repo_dir: str) -> dict:
    """{branch: worktree path} for every worktree of the clone (git worktree list --porcelain)."""
    out = _git(["worktree", "list", "--porcelain"], cwd=repo_dir)
    found, path = {}, None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path:
            found[line[len("branch refs/heads/"):]] = path
    return found


def _ref_exists(repo_dir: str, ref: str) -> bool:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run([GIT, "rev-parse", "--verify", "--quiet", ref], cwd=repo_dir, capture_output=True, text=True,
                       timeout=GIT_TIMEOUT_S, env=env)
    return p.returncode == 0


def prepare(spec: dict, root: str) -> dict:
    """Create the task's working folder under <root>/active/ and return its record (written to the task dir as
    workspace.json and to index.jsonl). repo: clone into repos/<name> if absent, fetch, then add a worktree."""
    ensure_root(root)
    tid = spec["task_id"]
    path = os.path.join(root, "active", tid)
    if os.path.lexists(path):
        raise WorkspaceError(f"{path} already exists")
    rec = {"task_id": tid, "kind": spec["kind"], "path": path, "state": "active", "created_at": time.time()}
    if spec["kind"] == "folder":
        os.mkdir(path)
        return rec
    name, url, branch = spec["name"], spec.get("url"), spec.get("branch")
    repo_dir = os.path.join(root, "repos", name)
    lock = os.open(os.path.join(root, "repos", f".{name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        return _prepare_repo_locked(rec, tid, path, repo_dir, name, url, branch, root)
    finally:
        os.close(lock)


def _prepare_repo_locked(rec, tid, path, repo_dir, name, url, branch, root):
    """Clone/fetch/add while holding repos/.<name>.lock."""
    if os.path.islink(repo_dir):
        raise WorkspaceError(f"repos/{name} is a symlink; refusing to use it")
    if not os.path.isdir(repo_dir):
        if not url:
            raise WorkspaceError(f"no clone named {name!r} under {os.path.join(root, 'repos')}; pass the clone URL as repo")
        _git(["clone", "--", url, repo_dir], timeout=CLONE_TIMEOUT_S)
        protect_clone(repo_dir)
    else:
        if url:
            have = _git(["remote", "get-url", "origin"], cwd=repo_dir).strip()
            if have != url and have != url.rstrip("/") and have + ".git" != url and have != url + ".git":
                raise WorkspaceError(f"repos/{name} is a clone of {have}, not {url}; use that URL or another name")
        protect_clone(repo_dir)     # before the fetch: a fetch can start an auto-gc
        # Point the registry back at any of this clone's worktrees that moved without a repair, so the branch listing
        # below names their real paths. Only when cleanup.lock is free right now (the lock order allows it: the clone
        # lock is held, cleanup.lock is taken second); a running cleanup does the same repair itself.
        try:
            with _cleanup_lock(root, timeout=0):
                fixed = repair_registry(root, repo_dir=repo_dir, deadline=time.monotonic() + CLEANUP_LOCK_WAIT_S)
            if fixed["errors"]:
                rec["registry_errors"] = fixed["errors"]
        except CleanupBusyError:
            pass
        _git(["fetch", "--prune", "origin"], cwd=repo_dir, timeout=FETCH_TIMEOUT_S)
    if not _ref_exists(repo_dir, "refs/remotes/origin/HEAD"):
        _git(["remote", "set-head", "origin", "--auto"], cwd=repo_dir, timeout=FETCH_TIMEOUT_S)
    taken = _worktree_branches(repo_dir)
    if branch is not None and branch in taken:
        if _settled_under(taken[branch], root) == "archive":
            # Releasing a legacy archived worktree must not interleave with the archive cleanup's reassess-then-delete
            # section: take its lock, then look again (cleanup may have deleted the holder meanwhile).
            # The wait is bounded (#93): a cleanup that is slow keeps the lock, and the start is refused with a
            # retryable reason rather than blocking for the cleanup's whole budget. It never proceeds without the lock.
            try:
                with _cleanup_lock(root, timeout=CLEANUP_LOCK_WAIT_S):
                    taken = _worktree_branches(repo_dir)
                    if branch in taken:
                        _release_branch(rec, taken[branch], branch, repo_dir, root)
            except CleanupBusyError as exc:       # only __enter__ raises it: the body was never run
                raise WorkspaceError(f"branch {branch!r} is checked out by an archived worktree ({taken[branch]}) "
                                     f"that was not released: {exc}") from None
        else:
            _release_branch(rec, taken[branch], branch, repo_dir, root)
    if branch is None:
        branch = TASK_BRANCH_PREFIX + tid
        _git(["worktree", "add", "-b", branch, "--", path, "origin/HEAD"], cwd=repo_dir)
    elif _ref_exists(repo_dir, "refs/heads/" + branch):
        if _ref_exists(repo_dir, "refs/remotes/origin/" + branch):
            counts = _git(["rev-list", "--left-right", "--count",
                           branch + "...origin/" + branch], cwd=repo_dir).split()
            if len(counts) != 2:
                raise WorkspaceError(f"could not compare local branch {branch!r} with origin/{branch}")
            local_only, remote_only = map(int, counts)
            if local_only and remote_only:
                raise WorkspaceError(f"local branch {branch!r} has diverged from origin/{branch}; refusing to reset "
                                     "or discard either history")
            if not local_only and remote_only:
                _git(["branch", "-f", branch, "origin/" + branch], cwd=repo_dir)
        # A local branch is owner work even when a remote branch has the same name. Check it out unchanged; never use
        # `-B`, because resetting to the remote can discard commits that have not been published.
        _git(["worktree", "add", "--", path, branch], cwd=repo_dir)
    elif _ref_exists(repo_dir, "refs/remotes/origin/" + branch):
        _git(["worktree", "add", "-b", branch, "--", path, "origin/" + branch], cwd=repo_dir)
    else:
        _git(["worktree", "add", "-b", branch, "--", path, "origin/HEAD"], cwd=repo_dir)   # a new branch
    rec.update(repo=name, url=url or _git(["remote", "get-url", "origin"], cwd=repo_dir).strip(),
               repo_dir=repo_dir, branch=branch)
    return rec


def _release_branch(rec, holder, branch, repo_dir, root):
    """`branch` is checked out at `holder`: refuse, unless holder is a legacy archived worktree that can be detached
    without losing anything (then record it as freed). An archived holder is only released under the cleanup lock."""
    where = _settled_under(holder, root)
    if where == "held":
        raise WorkspaceError(f"branch {branch!r} is checked out by a held worktree with unpushed work ({holder}); "
                             "publish or recover that work there first, or name another branch")
    if where != "archive":
        raise WorkspaceError(f"branch {branch!r} is checked out by another worktree ({holder}); wait for that "
                             "task or name another branch")
    why = _free_archived_branch(holder, branch, repo_dir, root)
    if why:
        raise WorkspaceError(f"branch {branch!r} is checked out by another worktree ({holder}); it is under "
                             f"archive/ but was not detached: {why}")
    rec["freed_from_archive"] = holder


class CleanupBusyError(WorkspaceError):
    """cleanup.lock stayed held for the whole bounded wait. The operation that wanted it was not run; it can be retried."""


class _cleanup_lock:
    """<root>/cleanup.lock, held by cleanup_archive() from reassessment through deletion and by a start that releases
    a legacy archived worktree, so neither can act on an entry the other is examining or has just changed.

    timeout=None blocks until the lock is free (the cleanup itself: a second cleanup simply runs after the first).
    A timeout in seconds polls a non-blocking flock every CLEANUP_LOCK_POLL_S and raises CleanupBusyError once it is
    exceeded (a start: see CLEANUP_LOCK_WAIT_S). flock itself has no timed variant, hence the poll."""

    def __init__(self, root: str, timeout=None):
        self.root = root
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        self.fd = os.open(os.path.join(self.root, "cleanup.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if self.timeout is None:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + self.timeout     # timeout=0: a single non-blocking attempt
                while True:
                    try:
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:                        # EWOULDBLOCK: another process holds it
                        if time.monotonic() >= deadline:
                            raise CleanupBusyError("archive cleanup is running; retry in a minute (waited "
                                                   f"{self.timeout:g} s for {os.path.join(self.root, 'cleanup.lock')})") \
                                from None
                        time.sleep(min(CLEANUP_LOCK_POLL_S, max(0.0, deadline - time.monotonic())))
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *exc):
        os.close(self.fd)       # closing the only descriptor releases the flock
        return False


def _settled_under(path: str, root: str):
    """'archive' or 'held' when path is lexically a direct entry of <root>/archive or <root>/held, else None.

    The layout directories must be real directories: with archive -> active, a still-running worktree would otherwise
    resolve to "archive" and be detached. A symlinked active/, archive/ or held/ is refused outright. Paths are
    compared without resolving anything below the root (only the root itself may sit under a symlinked ancestor)."""
    for sub in ("active", "archive", "held"):
        layout = os.path.join(root, sub)
        try:
            info = os.lstat(layout)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError(f"{layout} is a symlink or not a directory; refusing to classify worktrees under it")
    parent = os.path.dirname(os.path.normpath(os.path.abspath(path)))
    roots = {os.path.normpath(os.path.abspath(root)), os.path.realpath(root)}
    for sub in ("archive", "held"):
        if parent in {os.path.join(r, sub) for r in roots}:
            return sub
    return None


def _git_nohooks(args, cwd):
    """git with every hook disabled (post-checkout, reference-transaction, ...): a hook can create files, and a file
    created in an archived worktree is unpublished data that the archive cleanup would delete."""
    return _git(["-c", "core.hooksPath=/dev/null", *args], cwd=cwd)


def _detach_head(path: str, expect=None):
    """Detach the worktree's HEAD at the commit it is on, so its branch is free for another worktree. Plumbing only:
    `update-ref --no-deref HEAD` rewrites the worktree's own HEAD and nothing else (no checkout, no file, no hook).
    `expect`, when given, is the commit HEAD must still be on. Returns None on success, else why it did not happen."""
    try:
        head = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path).strip()
        if expect is not None and head != expect:
            return f"HEAD moved from {expect} to {head} before detaching"
        _git_nohooks(["update-ref", "--no-deref", "-m", "cortados: detach settled worktree", "HEAD", head, head],
                     cwd=path)
        after = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path).strip()
        attached = subprocess.run([GIT, "symbolic-ref", "--quiet", "HEAD"], cwd=path, capture_output=True,
                                  text=True, timeout=GIT_TIMEOUT_S,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
    except (WorkspaceError, OSError, subprocess.TimeoutExpired) as exc:
        return f"could not detach HEAD: {exc}"
    if after != head:
        return f"HEAD moved from {head} to {after} while detaching"
    if attached:
        return "HEAD is still on a branch after detaching"
    return None


def _free_archived_branch(path: str, branch: str, repo_dir: str, root: str):
    """A worktree archived before settle() detached HEAD may still hold `branch`. Detach it only when that cannot lose
    anything: its tree is clean (no untracked, ignored or hidden-modified file) and its HEAD is exactly origin/<branch>
    as fetched just now with an explicit all-heads refspec (a narrowed remote.origin.fetch would leave it stale; a
    failed fetch refuses). The archive/ directory and the entry must be the same directories (st_dev/st_ino) from
    assessment to detach. Called under the cleanup lock. Returns None once detached, else why it was left alone."""
    layout = os.path.join(root, "archive")
    name = os.path.basename(os.path.normpath(path))
    try:
        layout_fd = _open_dir(layout)
    except OSError as exc:
        return f"archive/ is not a real directory ({exc})"
    entry_fd = None
    try:
        try:
            entry_fd = _open_dir(name, dir_fd=layout_fd)
        except OSError:
            return "the archived worktree is missing, a symlink or not a directory"
        if not (_same_entry(layout_fd, layout) and _same_entry(entry_fd, path)):
            return "the archived worktree is not the directory at its recorded path"
        try:
            if _git(["symbolic-ref", "--quiet", "HEAD"], cwd=path).strip() != "refs/heads/" + branch:
                return f"its HEAD is not on {branch!r}"
            head = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path).strip()
            try:
                _git(["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=repo_dir,
                     timeout=FETCH_TIMEOUT_S)
            except WorkspaceError as exc:
                return f"could not refresh origin/{branch}, so its tip is not proven current: {exc}"
            if not _ref_exists(path, "refs/remotes/origin/" + branch):
                return f"origin/{branch} does not exist, so its commits are not proven published"
            tip = _git(["rev-parse", "--verify", "refs/remotes/origin/" + branch + "^{commit}"], cwd=path).strip()
            if head != tip:
                return f"its HEAD {head[:12]} is not the tip of origin/{branch} ({tip[:12]})"
            flagged = _git(["ls-files", "-v"], cwd=path)
            if any(line and (line[0].islower() or line[0] == "S") for line in flagged.splitlines()):
                return "it has files hidden by assume-unchanged or skip-worktree"
            left = _leftovers(path)
            if left:
                return f"it has {_held_reason(left)}"
        except WorkspaceError as exc:
            return f"could not assess it: {exc}"
        if not (_same_entry(layout_fd, layout) and _same_entry(entry_fd, path)):
            return "the archived worktree changed while it was being assessed"
        return _detach_head(path, expect=head)
    finally:
        if entry_fd is not None:
            os.close(entry_fd)
        os.close(layout_fd)


def _is_cache_path(rel: str) -> bool:
    parts = rel.strip("/").split("/")
    return any(p in CACHE_DIR_NAMES for p in parts) or parts[-1].endswith(CACHE_FILE_SUFFIXES)


def _ignored_blocker(top: str, rel: str):
    """None when the ignored entry `rel` (a file, or a directory as `git status` names it: "dir/") holds nothing but
    regenerable caches or empty directories; else the first path inside it that is owner data. A nested .git always
    blocks (it may hold unpushed commits), and so does anything that cannot be walked."""
    full = os.path.join(top, rel.rstrip("/"))
    try:
        info = os.lstat(full)
    except FileNotFoundError:
        return None                                  # already gone: nothing left to lose
    except OSError as exc:
        return f"{rel} (cannot be read: {type(exc).__name__})"
    if not stat.S_ISDIR(info.st_mode):
        return None if _is_cache_path(rel) else rel
    errors, seen = [], 0
    for directory, dirs, files in os.walk(full, followlinks=False, onerror=errors.append):
        base = os.path.relpath(directory, top)
        meta = next((n for n in (*dirs, *files) if _is_git_meta(n)), None)
        if meta is not None:
            return os.path.join(base, meta)
        real_dirs = []
        for name in dirs:                            # a symlink to a directory is listed here and never followed
            (files if os.path.islink(os.path.join(directory, name)) else real_dirs).append(name)
        dirs[:] = real_dirs
        seen += len(dirs) + len(files)
        if seen > IGNORED_WALK_MAX:
            return f"{rel} (more than {IGNORED_WALK_MAX} entries; not assessed)"
        for name in files:
            if not _is_cache_path(os.path.join(base, name)):
                return os.path.join(base, name)
    if errors:
        return f"{rel} (cannot be walked: {type(errors[0]).__name__})"
    return None


def _leftovers(path: str, deadline=None) -> list:
    """Working-tree paths that keep `path` from being proven published: every tracked change and every untracked file
    git does not ignore (named by `git status`), and, for ignored content, whatever _ignored_blocker finds that is not
    a regenerable cache or an empty directory. Paths only, relative to the worktree, in git's order."""
    out = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"], cwd=path,
               timeout=_within(deadline, GIT_TIMEOUT_S))
    tokens = out.split("\0")
    blocking, i = [], 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, rel = entry[:2], entry[3:]
        if xy[0] in "RC":
            i += 1                                   # -z: a rename's or copy's source path follows as its own token
        if xy == "!!":
            why = _ignored_blocker(path, rel)
            if why is not None:
                blocking.append(why)
        else:
            blocking.append(rel)
    return blocking


def _held_reason(paths: list) -> str:
    shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
    return f"{len(paths)} uncommitted or untracked file(s) (ignored ones included, regenerable caches not): {shown}"


def assess(path: str, deadline=None):
    """(ok, reason, paths): pushed()'s answer plus, when the worktree is held for files in it, the paths that held it
    (at most HELD_PATHS_MAX; [] otherwise), so a hold can be diagnosed after the files are gone."""
    try:
        remotes = _git(["remote"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S)).split()
        if not remotes:
            return False, _Reason("no_remote", "could not assess the worktree: it has no remote"), []
        for remote in remotes:
            # An explicit all-heads refspec also prunes stale refs excluded by a locally narrowed fetch refspec.
            _git(["fetch", "--prune", remote,
                  f"+refs/heads/*:refs/remotes/{remote}/*"], cwd=path, timeout=_within(deadline, FETCH_TIMEOUT_S))
        submodules = _git(["ls-files", "--stage"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S))
        if any(line.startswith("160000 ") for line in submodules.splitlines()):
            return False, _Reason("submodule", "submodule content cannot be proven published; holding the workspace"), []
        flagged = _git(["ls-files", "-v"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S))
        hidden = [line for line in flagged.splitlines() if line and (line[0].islower() or line[0] == "S")]
        if hidden:
            return (False, _Reason("hidden_files", f"{len(hidden)} tracked file(s) hidden by assume-unchanged or skip-worktree"),
                    [line[2:] for line in hidden[:HELD_PATHS_MAX]])
        # Ignored content is owner data too, except regenerable caches and empty directories (CACHE_DIR_NAMES):
        # everything archived is deleted after ARCHIVE_AGE_S, so only what a rerun rebuilds may go unpublished.
        paths = _leftovers(path, deadline)
        if paths:
            return False, _Reason("unpushed_files", _held_reason(paths)), paths[:HELD_PATHS_MAX]
        ahead = _git(["rev-list", "HEAD", "--not", "--remotes"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S))
        if ahead.strip():
            return False, _Reason("unpushed_commits", f"{len(ahead.split())} commit(s) not on any remote"), []
    except GitTimeoutError as exc:
        return None, _Reason("assess_timeout", f"could not assess the worktree in time: {exc}"), []
    except WorkspaceError as exc:
        return False, _Reason("assess_failed", f"could not assess the worktree: {exc}"), []
    return True, _Reason("pushed", "pushed"), []


def _snapshot(path: str, deadline=None) -> frozenset:
    """What a late writer would change: (relative path, type, size, mtime_ns, ctime_ns, inode) for every entry outside
    the regenerable caches (directories by name only, so a cache file's creation does not count), plus any git
    metadata at any depth, caches included. The worktree's own top-level .git (rewritten by `git worktree repair`
    after a move) is left out. Never follows a symlink. Raises GitTimeoutError when `deadline` passes and
    WorkspaceError when the tree cannot be walked completely or has more than SNAPSHOT_MAX entries."""
    out, errors = set(), []
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=errors.append):
        if deadline is not None and time.monotonic() >= deadline:
            raise GitTimeoutError("no time left to snapshot the worktree")
        base = os.path.relpath(directory, path)
        if base == ".":
            dirs[:] = [n for n in dirs if n != ".git"]
            files = [n for n in files if n != ".git"]
        real_dirs = []
        for name in dirs:
            (files if os.path.islink(os.path.join(directory, name)) else real_dirs).append(name)
        dirs[:] = real_dirs
        for name in real_dirs:
            rel = os.path.normpath(os.path.join(base, name))
            if _is_git_meta(name) or not _is_cache_path(rel):
                out.add((rel, "d"))
        for name in files:
            rel = os.path.normpath(os.path.join(base, name))
            if not _is_git_meta(name) and _is_cache_path(rel):
                continue
            try:
                st = os.lstat(os.path.join(directory, name))
            except FileNotFoundError:
                continue                                # gone between listing and stat: the next snapshot differs
            except OSError as exc:
                raise WorkspaceError(f"cannot stat {rel}: {type(exc).__name__}")
            out.add((rel, stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino))
        if len(out) > SNAPSHOT_MAX:
            raise WorkspaceError(f"more than {SNAPSHOT_MAX} entries")
    if errors:
        raise WorkspaceError(f"cannot be walked: {type(errors[0]).__name__}")
    return frozenset(out)


def _not_quiet(text: str):
    return _Reason("not_quiescent", f"writer quiescence not established ({text}); held rather than risk a late write")


def _quiet_assess(path: str, delays, deadline=None):
    """assess() until two clean assessments `delays[i]` seconds apart are bracketed by identical snapshots: a snapshot
    taken before the first and one taken after the second (so a file written during either assessment, or between
    them, makes them differ). Returns (ok, reason, paths, rechecks, snapshot): `snapshot` is the closing one when ok,
    for the caller to compare again just before it moves, detaches or deletes. Not ok when the files kept changing,
    could not be snapshotted, or `delays` is empty (one assessment proves nothing about writers); None as assess()
    when it ran out of time (a deadline that leaves no room for the next pause counts). Paused only while a retry can
    help: a hold for commits or an error is final at once."""
    delays = tuple(delays)
    prev = None                                       # (ok, snapshot taken before that assessment)
    ok, reason, paths, rechecks = False, _not_quiet("no pause to compare across"), [], 0
    for i in range(len(delays) + 1):
        if i:
            if deadline is not None and time.monotonic() + delays[i - 1] >= deadline:
                return None, _Reason("assess_timeout", "no time left to establish writer quiescence"), [], rechecks, None
            _sleep(delays[i - 1])
            rechecks += 1
        try:
            pre = _snapshot(path, deadline)
            ok, reason, paths = assess(path, deadline)
            post = _snapshot(path, deadline) if ok else None
        except GitTimeoutError as exc:
            return None, _Reason("assess_timeout", f"could not assess the worktree in time: {exc}"), [], rechecks, None
        except WorkspaceError as exc:
            return False, _not_quiet(f"snapshot failed: {exc}"), [], rechecks, None
        if ok is None:
            return None, reason, [], rechecks, None
        if ok and prev is not None and prev[0] is True and prev[1] == post:
            return True, reason, [], rechecks, post
        prev = (ok, pre)
        if not ok and not paths:
            return ok, reason, paths, rechecks, None
    if ok:
        return False, _not_quiet(f"its files changed across {rechecks} pause(s)" if delays else
                                 "no pause to compare across"), [], rechecks, None
    return ok, reason, paths, rechecks, None


def _same_snapshot(path: str, expect, deadline=None) -> bool:
    try:
        return _snapshot(path, deadline) == expect
    except WorkspaceError:                            # timeouts included: unknown is not "unchanged"
        return False


def _writers_running(writers_alive) -> bool:
    """True when the task's process group is alive or its state is unknown (`writers_alive` returned None or
    raised); False when there is no probe (no task process to wait for) or it says the group is gone."""
    if writers_alive is None:
        return False
    try:
        return writers_alive() is not False
    except Exception:                                 # a broken probe is not proof the writers are gone
        return True


WRITERS_RUNNING = ("the task's process group is still running, or its state is unknown; held rather than risk a "
                   "late write")


def pushed(path: str, deadline=None):
    """(True, "pushed") only after fresh remote refs prove every byte and commit is published (assess() without the
    paths).

    (False, reason): not proven published; any git or fetch failure is uncertainty too, so the caller holds the
    workspace rather than risking owner data. (None, reason): the assessment did not finish (a git call timed out, or
    `deadline` — a time.monotonic() value — left no time for one), so nothing is known either way; a caller that
    would otherwise move or delete must leave the workspace exactly as it is. `None` is falsy on purpose: a caller
    that only asks "proven pushed?" (settle) holds, which is the conservative answer.
    """
    ok, reason, _ = assess(path, deadline)
    return ok, reason


def settle(rec: dict, root: str, lock_wait=SETTLE_LOCK_WAIT_S, recheck_delays=SETTLE_RECHECK_DELAYS_S,
           writers_alive=None) -> dict:
    """Move a finished task's folder out of active/: to archive/ when it is pushed or had no repository, else to
    held/. Returns the updated record (state archived|held, or still active with `error` if the move failed, in which
    case the folder stays where it is and nothing is deleted).

    The move runs under cleanup.lock (waited for at most `lock_wait` seconds; see the lock order in the module
    docstring), so a cleanup never acts on a registry it listed before the move. When the lock stays busy the folder
    stays in active/ and the record says `settle_deferred`: the next cleanup run settles it (cleanup_archive).

    Archiving needs writer quiescence: `writers_alive` (a callable: True / None while the task's process group is
    alive or unknown, False once it is gone) must say gone, before the assessment and again under the lock; and two
    clean assessments `recheck_delays` apart must be bracketed by identical snapshots (_quiet_assess), compared again
    under the lock before the move and before HEAD is detached. Otherwise it is held. The record keeps the paths that
    held it (`held_paths`), how many reassessments ran, and a path-free `reason_code`."""
    rec = dict(rec)
    path = rec.get("path")
    if rec.get("state") != "active" or not path:
        return rec
    if not os.path.isdir(path):
        return _already_settled(rec, root)
    snap = None
    if _writers_running(writers_alive):
        ok, reason = False, _Reason("writers_running", WRITERS_RUNNING)
    elif rec.get("kind") == "repo":
        try:
            if os.path.isdir(rec.get("repo_dir") or ""):
                protect_clone(rec["repo_dir"])          # before pushed()'s fetch, which can start an auto-gc
        except WorkspaceError as exc:
            rec.update(error=f"not settled: {exc}", settle_deferred=True)
            return rec
        ok, reason, paths, rechecks, snap = _quiet_assess(path, recheck_delays)
        rec.pop("held_paths", None)
        if paths:
            rec["held_paths"] = paths
        if rechecks:
            rec["settle_rechecks"] = rechecks
    else:
        ok = not _contains_git_entry(path)
        reason = _Reason("no_repository", "no repository") if ok else \
            _Reason("nested_git", "plain folder contains a nested .git entry")
    try:
        with _cleanup_lock(root, timeout=lock_wait):
            if ok and _writers_running(writers_alive):
                ok, reason, snap = False, _Reason("writers_running", WRITERS_RUNNING), None
            return _move_settled(rec, root, bool(ok), reason, snapshot=snap)
    except CleanupBusyError as exc:                     # only __enter__ raises it: nothing was moved
        rec.update(error=f"not settled yet, left in active/ for the next cleanup to settle: {exc}",
                   settle_deferred=True)
        return rec


def _already_settled(rec: dict, root: str) -> dict:
    """The active folder is gone: when the index says the task was settled since (a deferred settle the cleanup
    completed) and that folder exists, that record is the answer; otherwise the folder is really missing."""
    done = read_index(root).get(rec.get("task_id"))
    if isinstance(done, dict) and done.get("state") in ("archived", "held") and isinstance(done.get("path"), str) \
            and os.path.isdir(done["path"]):
        return dict(done)
    return {**rec, "state": "missing", "settled_at": time.time(), "reason": "the working folder no longer exists"}


def _move_settled(rec: dict, root: str, ok: bool, reason: str, snapshot=None) -> dict:
    """settle()'s move, under cleanup.lock: rename into archive/ or held/, then repair the clone's registry entry
    (fixed cap). A failed repair is recorded as `repair_error` and is not rolled back: the folder is where its state
    says, and the stale entry is repaired by the next cleanup or start (it is never pruned, see the module docstring).

    A repository worktree goes to archive/ only with `snapshot` (_quiet_assess's closing one) still matching just
    before the move, and its HEAD is detached only if it still matches after the move; a change before the move holds
    it, a change after moves it on from archive/ to held/ with its branch still checked out."""
    path = rec["path"]
    if not os.path.isdir(path):
        return _already_settled(rec, root)
    if ok and rec.get("kind") == "repo" and (snapshot is None or not _same_snapshot(path, snapshot)):
        ok, reason = False, _not_quiet("its files changed after the last assessment, before the move")
    dest_dir = os.path.join(root, "archive" if ok else "held")
    dest = os.path.join(dest_dir, rec["task_id"])
    try:
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.lexists(dest):
            raise WorkspaceError(f"{dest} already exists")
        os.rename(path, dest)
    except (OSError, WorkspaceError) as exc:
        rec["error"] = f"could not move {path} to {dest}: {exc}"
        return rec
    for key in ("error", "settle_deferred", *_REPAIR_FIELDS):    # the repair below decides these afresh
        rec.pop(key, None)
    rec.update(state="archived" if ok else "held", path=dest, settled_at=time.time(), reason=str(reason),
               reason_code=reason_code(reason))
    _repair_settled(rec, dest)
    if ok and rec.get("kind") == "repo" and not _same_snapshot(dest, snapshot):
        # A late write after the move: not detached (the branch stays held by it), and on to held/, never deleted.
        ok = False
        late = _not_quiet("its files changed after the move to archive/; moved on to held/ without detaching HEAD")
        held = os.path.join(root, "held", rec["task_id"])
        try:
            os.makedirs(os.path.dirname(held), exist_ok=True)
            if os.path.lexists(held):
                raise WorkspaceError(f"{held} already exists")
            os.rename(dest, held)
        except (OSError, WorkspaceError) as exc:
            rec.update(detached=False, detach_error=f"HEAD not detached: {late}; and it could not be moved to held/ "
                                                    f"({exc}), so the cleanup reassesses it before any deletion")
            return rec
        for key in _REPAIR_FIELDS + ("registry_conflict",):
            rec.pop(key, None)
        rec.update(state="held", path=held, reason=str(late), reason_code=late.code)
        _repair_settled(rec, held)
        return rec
    if ok and rec.get("kind") == "repo" and (rec.get("registry_conflict") or rec.get("repair_error")):
        # #93 round 4: no git operation through a .git that may name another worktree's admin dir: detaching "this"
        # HEAD would detach that worktree's, freeing its branch. The branch stays held here until the entry is fixed.
        rec["detached"] = False
        rec["detach_error"] = ("HEAD not detached: the registry entry was not repaired after the move, so its .git "
                               "may name another worktree's admin dir; nothing was run through it")
    elif ok and rec.get("kind") == "repo":
        # Pushed work: free the branch for the next task (the commit stays checked out, detached). Held work keeps
        # its branch, so a new task cannot start on it while it is unpushed. A failed detach loses nothing: the
        # worktree stays archived and attached, and the reason is recorded here and in index.jsonl.
        why = _detach_head(dest)
        if why:
            rec["detach_error"] = why
        else:
            rec["detached"] = True
    return rec


def _repair_settled(rec: dict, dest: str) -> None:
    repo_dir = rec.get("repo_dir")
    if rec.get("kind") == "repo" and isinstance(repo_dir, str) and os.path.isdir(repo_dir):
        try:
            _repair_worktree(dest, repo_dir)
        except RegistryConflictError as exc:
            rec["registry_conflict"] = str(exc)
        except WorkspaceError as exc:
            rec["repair_error"] = (f"moved to {dest} but `git worktree repair` failed ({exc}); the clone's registry "
                                   "still names the old path until the next cleanup or start repairs it")


# --- index.jsonl: one line per task ------------------------------------------------------------------------------

def index_path(root: str) -> str:
    return os.path.join(root, "index.jsonl")


def read_index(root: str) -> dict:
    """{task_id: record}; a line that is not a JSON object with a task_id is skipped (it is reported by cleanup)."""
    out = {}
    try:
        with open(index_path(root), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("task_id"), str):
                    out[rec["task_id"]] = rec
    except FileNotFoundError:
        pass
    return out


def update_index(root: str, rec: dict) -> None:
    """Replace (or add) the task's line, atomically, under index.lock (the runner and a cleanup may write at once)."""
    if not isinstance(rec.get("task_id"), str):
        raise WorkspaceError("an index record needs a task_id")

    def replace(recs):
        recs[rec["task_id"]] = {k: v for k, v in rec.items() if k != "error"} if "error" not in rec else dict(rec)
        return True
    _index_transaction(root, replace)


def _index_transaction(root: str, change) -> None:
    """Read index.jsonl, apply change(recs) (which edits the dict in place and returns whether to write) and write it
    back, all under index.lock: a read-modify-write no other writer can interleave with."""
    lock = os.open(os.path.join(root, "index.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recs = read_index(root)
        if not change(recs):
            return
        fd, tmp = tempfile.mkstemp(prefix=".index.", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for tid in sorted(recs):
                    f.write(json.dumps(recs[tid], sort_keys=True) + "\n")
            os.replace(tmp, index_path(root))
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(lock)


# --- cleanup: archive/ only -------------------------------------------------------------------------------------

def _open_dir(path: str, *, dir_fd=None) -> int:
    """Open a real directory without following its final component."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def _same_entry(fd: int, path: str) -> bool:
    """Whether path still names the directory held by fd (all ancestors included)."""
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    opened = os.fstat(fd)
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


def _remove_tree_fd(directory_fd: int) -> None:
    """Remove a directory's contents relative to an already verified descriptor, never through a symlink."""
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_dir(name, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise WorkspaceError(f"directory entry {name!r} changed while opening it")
                _remove_tree_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _contains_git_entry(path: str) -> bool:
    """Find a .git entry (any case, file or directory: _is_git_meta) at any depth without traversing symlinks. A tree
    that cannot be walked counts as containing one (it holds)."""
    errors = []
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=errors.append):
        if any(_is_git_meta(n) for n in (*dirs, *files)):
            return True
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(directory, name))]
    return bool(errors)


def _fd_contains_git_entry(directory_fd: int) -> bool:
    for name in os.listdir(directory_fd):
        if _is_git_meta(name):
            return True
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_dir(name, dir_fd=directory_fd)
            try:
                if _fd_contains_git_entry(child_fd):
                    return True
            finally:
                os.close(child_fd)
    return False


# --- worktree registry: protect, repair, remove one entry (never `git worktree prune`) ----------------------------

_PROTECT_CONFIG = (("gc.worktreePruneExpire", "never"), ("gc.auto", "0"), ("gc.autoDetach", "false"),
                   ("maintenance.auto", "false"))


def protect_clone(repo_dir: str) -> None:
    """Set gc.worktreePruneExpire=never in the clone's own config, so no gc ever prunes a worktree admin dir whose path
    is missing, and turn automatic gc and maintenance off (gc.auto=0, gc.autoDetach=false, maintenance.auto=false):
    gc's worktree prune also drops duplicate registry entries whatever the expiry, so the auto-gc a fetch or commit
    can start must never run in a managed clone (#93 round 4). Idempotent: each key is written only when its local
    value differs.

    Nothing else is needed: with every admin dir kept, gc treats each worktree's HEAD and index as roots, so an
    unpublished commit on a held worktree's HEAD (or staged in its index) stays reachable whatever gc.pruneExpire
    says. Its reflog is NOT a permanent root: a commit reachable only from expired reflog entries (committed, then
    reset away) is lost to git's normal reflog expiry (gc.reflogExpire*), as in any repository. That is acceptable
    because held work's commits are on its HEAD."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for key, value in _PROTECT_CONFIG:
        try:
            have = subprocess.run([GIT, "config", "--local", "--get", key], cwd=repo_dir,
                                  capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env).stdout.strip()
        except FileNotFoundError:
            raise WorkspaceError("git is not installed or not on PATH on this execution host") from None
        except subprocess.TimeoutExpired:
            raise GitTimeoutError(f"git config did not finish within {GIT_TIMEOUT_S:g} s") from None
        if have != value:
            _git(["config", "--local", key, value], cwd=repo_dir)


def _same_path(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b) or os.path.realpath(a) == os.path.realpath(b)


def _parse_gitfile(text: str, base: str):
    """The path in a `gitdir: <path>` line (relative paths, as worktree.useRelativePaths writes them, resolved
    against `base`), or None."""
    line = text.split("\n", 1)[0].strip()
    if line.startswith("gitdir: "):
        line = line[len("gitdir: "):]
    if not line:
        return None
    return os.path.normpath(line if os.path.isabs(line) else os.path.join(base, line))


def _read_small(path: str, dir_fd=None):
    """A regular file's text (at most 4 KiB), never through a symlink; None when it is missing or not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 4096).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _clone_of_admin(admin: str, root: str):
    """The managed clone <root>/repos/<name> whose .git/worktrees/ holds `admin`, else None (never a foreign repo)."""
    worktrees = os.path.dirname(admin)
    dot_git = os.path.dirname(worktrees)
    clone = os.path.dirname(dot_git)
    if os.path.basename(worktrees) != "worktrees" or os.path.basename(dot_git) != ".git":
        return None
    if not _same_path(os.path.dirname(clone), os.path.join(root, "repos")):
        return None
    return clone


def _registry_names(path: str, admin: str) -> bool:
    """Whether the admin dir's `gitdir` file names path/.git."""
    text = _read_small(os.path.join(admin, "gitdir"))
    recorded = None if text is None else _parse_gitfile(text, admin)
    return recorded is not None and _same_path(recorded, os.path.join(path, ".git"))


class RegistryConflictError(WorkspaceError):
    """A worktree's .git file names an admin dir that another existing worktree validly owns (its `gitdir` backlink
    names that worktree, whose own .git file names the admin dir back). Repairing would hand the owner's admin dir
    (HEAD, index, reflogs) to the impostor, and a later deletion of the impostor would delete it (#93 round 3)."""


def _gitfile_admin(worktree: str):
    """The admin dir a worktree's .git file names (no-follow read), or None."""
    text = _read_small(os.path.join(worktree, ".git"))
    return None if text is None else _parse_gitfile(text, worktree)


def _valid_owner(admin: str, path: str):
    """The worktree, other than `path`, that validly owns `admin`: the admin dir's `gitdir` names it and its own .git
    file names the admin dir back. None when the backlink names `path`, is missing, or no longer points back."""
    text = _read_small(os.path.join(admin, "gitdir"))
    recorded = None if text is None else _parse_gitfile(text, admin)
    if recorded is None or _same_path(recorded, os.path.join(path, ".git")):
        return None
    owner = os.path.dirname(recorded)
    back = _gitfile_admin(owner)
    if back is not None and _same_path(back, admin):
        return owner
    return None


def _repair_worktree(path: str, repo_dir: str) -> None:
    """Point the clone's registry at a worktree's current path, and check that it does. Local and fast; it gets its own
    fixed cap, never the cleanup budget. Raises WorkspaceError (GitTimeoutError included) when it did not succeed.
    Even then nothing is lost: the entry is only stale, and the next repair_registry() run tries again.

    Raises RegistryConflictError, and runs nothing, when the admin dir is validly owned by another existing worktree:
    only a backlink whose target is missing or no longer points back is ever rewritten."""
    admin = _gitfile_admin(path)
    if admin is not None:
        owner = _valid_owner(admin, path)
        if owner is not None:
            raise RegistryConflictError(
                f"{path}/.git names the admin dir {admin}, which belongs to the existing worktree {owner} (its "
                f"backlink names {owner} and {owner}/.git names it back); not repaired, nothing touched: {path}'s .git "
                "file is wrong and needs the owner's attention; fix it before anyone runs `git gc` or `git worktree "
                f"prune` in {repo_dir}, which drop a duplicate registry entry even with gc.worktreePruneExpire=never")
    _git(["worktree", "repair", "--", path], cwd=repo_dir, timeout=GIT_TIMEOUT_S)
    text = _read_small(os.path.join(path, ".git"))
    admin = None if text is None else _parse_gitfile(text, path)
    if admin is None or not _registry_names(path, admin):
        raise WorkspaceError(f"`git worktree repair` ran but the registry still does not name {path}")


def _repair_backoff(failures: int) -> float:
    """Seconds before an entry whose repair failed `failures` times in a row is tried again."""
    return min(REPAIR_BACKOFF_BASE_S * 2 ** max(0, failures - 1), REPAIR_BACKOFF_MAX_S)


def repair_registry(root: str, repo_dir=None, deadline=None, now=None) -> dict:
    """Repair the registry entry of every worktree under active/, archive/ or held/ (of `repo_dir` only, when given)
    whose admin dir names another path: a move whose repair failed or never ran. Returns {"repaired": [...],
    "errors": [...], "skipped": [...]}; an error is loud but not destructive (the entry stays stale until a later run).

    Never steals an admin dir: an entry whose admin dir another existing worktree validly owns is not repaired; it is
    an error naming both paths, recorded as `registry_conflict` in its index record (RegistryConflictError).

    Bounded (#93 round 3): `deadline` (time.monotonic()) is checked before each repair, and a repair, once started,
    keeps its own fixed cap; entries not reached are `skipped` (not an error; the next run reaches them). Each entry is
    tried at most once per run. A failed repair is recorded in its index record (`repair_failures`, `repair_next_at`,
    exponential backoff), entries in backoff are skipped until then, and the entries with the fewest failures go
    first, so one entry that always hangs cannot take every run's budget. Success clears all of these fields."""
    now = time.time() if now is None else now
    out = {"repaired": [], "errors": [], "skipped": []}
    index = read_index(root)
    todo = []
    for sub in ("active", "archive", "held"):
        layout = os.path.join(root, sub)
        try:
            if not stat.S_ISDIR(os.lstat(layout).st_mode):
                continue
            names = sorted(os.listdir(layout))
        except OSError:
            continue
        for name in names:
            if not TASK_ID_RE.fullmatch(name):
                continue
            path = os.path.join(layout, name)
            try:
                if not stat.S_ISDIR(os.lstat(path).st_mode):
                    continue
            except OSError:
                continue
            admin = _gitfile_admin(path)
            clone = None if admin is None else _clone_of_admin(admin, root)
            if clone is None or (repo_dir is not None and not _same_path(clone, repo_dir)):
                continue                         # a plain folder, a nested repo, or another clone's worktree
            rec = index.get(name)
            rec = rec if isinstance(rec, dict) and rec.get("path") == path else None
            if _registry_names(path, admin):
                if rec is not None and any(k in rec for k in _REPAIR_FIELDS):
                    _note_repair(root, name, path, {"repaired_at": now}, clear=True)
                continue
            failures = rec.get("repair_failures", 0) if rec is not None else 0
            failures = failures if isinstance(failures, int) else 0
            # unindexed entries (no backoff can be recorded for them) go last
            todo.append((rec is None, failures, sub, name, path, admin, clone, rec))
    todo.sort(key=lambda t: t[:4])
    for _, failures, sub, name, path, admin, clone, rec in todo:
        next_at = rec.get("repair_next_at") if rec is not None else None
        if isinstance(next_at, (int, float)) and next_at > now:
            out["skipped"].append(f"{name}: registry repair of {path} backing off after {failures} failure(s) until "
                                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(next_at))}")
            continue
        if not os.path.isdir(admin):
            out["errors"].append(f"{name}: {path} names the admin dir {admin}, which does not exist; "
                                 "its registry entry cannot be repaired")
            continue
        if deadline is not None and time.monotonic() >= deadline:
            out["skipped"].append(f"{name}: registry repair of {path} not started: no budget left this run")
            continue
        try:
            _repair_worktree(path, clone)
        except RegistryConflictError as exc:
            out["errors"].append(f"{name}: {exc}")
            if rec is not None:
                _note_repair(root, name, path, {"registry_conflict": str(exc)})
            continue
        except WorkspaceError as exc:
            failures += 1
            wait = _repair_backoff(failures)
            out["errors"].append(f"{name}: the registry of {clone} does not name {path} and could not be repaired "
                                 f"({exc}); not destructive, retried in {wait:g} s or later")
            if rec is not None:
                _note_repair(root, name, path, {"repair_failures": failures, "repair_next_at": now + wait,
                                                "repair_error": f"registry repair failed ({exc}); stale, not destructive"})
            continue
        out["repaired"].append(path)
        if rec is not None:
            _note_repair(root, name, path, {"repaired_at": now}, clear=True)
    return out


def _note_repair(root: str, name: str, path: str, fields: dict, clear: bool = False) -> None:
    """Merge repair bookkeeping into the task's CURRENT index record in one transaction under index.lock (#93 round
    4): only the repair fields (and `repaired_at`) are written, so a field the runner wrote meanwhile, such as
    `settle_deferred`, is never erased. Skipped when the record no longer names `path`."""
    def merge(recs):
        rec = recs.get(name)
        if not isinstance(rec, dict) or rec.get("path") != path:
            return False
        if clear:
            for k in _REPAIR_FIELDS:
                rec.pop(k, None)
        rec.update(fields)
        return True
    _index_transaction(root, merge)


_REPAIR_FIELDS = ("repair_error", "repair_failures", "repair_next_at", "registry_conflict")


def _other_referrer(admin: str, path: str, repo_dir: str, root: str):
    """Another existing worktree whose .git file names `admin`: any entry of active/, archive/ or held/ other than
    `path`, or the clone itself. None when there is none."""
    candidates = [repo_dir]
    for sub in ("active", "archive", "held"):
        layout = os.path.join(root, sub)
        try:
            if stat.S_ISDIR(os.lstat(layout).st_mode):
                candidates += [os.path.join(layout, n) for n in sorted(os.listdir(layout))]
        except OSError:
            continue
    for cand in candidates:
        if _same_path(cand, path):
            continue
        named = _gitfile_admin(cand)
        if named is not None and _same_path(named, admin):
            return cand
    return None


class _AdminDir:
    """The admin dir of exactly one worktree, opened through no-follow descriptors before the worktree is deleted:
    resolved from the worktree's own .git file, required to sit in <repo_dir>/.git/worktrees/, to name the
    worktree's path in its `gitdir` file, and to be named by no other existing worktree's .git file (under active/,
    archive/ or held/, or the clone's own top level). Raises WorkspaceError otherwise (the caller then deletes nothing):
    a backlink alone is not proof of ownership, since a wrong repair could have rewritten it (#93 round 3)."""

    def __init__(self, entry_fd: int, path: str, repo_dir: str, root: str):
        self.fds = []
        try:
            text = _read_small(".git", dir_fd=entry_fd)
            admin = None if text is None else _parse_gitfile(text, path)
            if admin is None:
                raise WorkspaceError(f"{path} has no readable .git file naming its admin dir")
            if not _same_path(os.path.dirname(admin), os.path.join(repo_dir, ".git", "worktrees")):
                raise WorkspaceError(f"{path}'s admin dir {admin} is not inside {repo_dir}/.git/worktrees")
            self.name = os.path.basename(admin)
            if self.name in ("", ".", "..") or "/" in self.name:
                raise WorkspaceError(f"{path}'s admin dir name {self.name!r} is not a plain name")
            fd = _open_dir(repo_dir)
            self.fds.append(fd)
            for part in (".git", "worktrees"):
                fd = _open_dir(part, dir_fd=fd)
                self.fds.append(fd)
            self.worktrees_fd = fd
            self.fd = _open_dir(self.name, dir_fd=fd)
            self.fds.append(self.fd)
            recorded = _read_small("gitdir", dir_fd=self.fd)
            recorded = None if recorded is None else _parse_gitfile(recorded, admin)
            if recorded is None or not _same_path(recorded, os.path.join(path, ".git")):
                raise WorkspaceError(f"the admin dir {admin} names {recorded}, not {path}; it is left alone")
            other = _other_referrer(admin, path, repo_dir, root)
            if other is not None:
                raise WorkspaceError(f"the admin dir {admin} is also named by the existing worktree {other}'s .git "
                                     f"file; neither {path} nor the admin dir is deleted")
        except BaseException:
            self.close()
            raise

    def remove(self) -> None:
        _remove_tree_fd(self.fd)
        os.rmdir(self.name, dir_fd=self.worktrees_fd)

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []


def _hold_changed_archive(root: str, archive_fd: int, name: str, rec: dict, reason: str, now: float) -> dict:
    """Move a newly changed archived worktree to held/ using anchored source and destination descriptors, then repair
    the clone's registry so it names the new path (fixed cap, never the cleanup budget). There is no rollback: held/
    is never deleted from, so it is the safe place to be, and a failed repair only leaves a stale registry entry,
    which nothing prunes (module docstring). The failure is recorded in the index (`repair_error`) and raised for
    the cleanup report; the next cleanup or start repairs the entry and clears it."""
    held_path = os.path.join(root, "held", name)
    held_fd = _open_dir(os.path.join(root, "held"))
    try:
        try:
            os.stat(name, dir_fd=held_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise WorkspaceError(f"held/{name} already exists; archived workspace was not deleted")
        os.rename(name, name, src_dir_fd=archive_fd, dst_dir_fd=held_fd)
    finally:
        os.close(held_fd)
    updated = {**rec, "state": "held", "path": held_path, "settled_at": now, "reason": str(reason),
               "reason_code": reason_code(reason)}
    repo_dir = rec.get("repo_dir")
    failure = None
    if rec.get("kind") == "repo" and isinstance(repo_dir, str) and os.path.isdir(repo_dir):
        try:
            _repair_worktree(held_path, repo_dir)
        except WorkspaceError as exc:
            failure = exc
            updated["repair_error"] = (f"moved to held/ but `git worktree repair` failed ({exc}); the next cleanup "
                                       "or start repairs the registry entry")
    update_index(root, updated)
    if failure is not None:
        raise WorkspaceError(f"held, but `git worktree repair` of {held_path} failed ({failure}); its registry entry "
                             "is stale (not destructive) until the next cleanup or start repairs it")
    return updated


def cleanup_archive(root: str, now=None, max_deletions=CLEANUP_MAX_DELETIONS, max_seconds=CLEANUP_MAX_SECONDS) -> dict:
    """Serialize reassessment through deletion so a concurrent cleanup cannot move an entry being deleted, and so a
    settle or a start releasing a legacy archived worktree (both take the same lock) cannot move one in between."""
    with _cleanup_lock(root):
        return _cleanup_archive_locked(root, now=now, max_deletions=max_deletions, max_seconds=max_seconds)


def _protect_clones(root: str, out: dict) -> set:
    """protect_clone() every clone under repos/ (a cleanup's fetches can start an auto-gc in any of them). Returns the
    clones that could not be protected: their worktrees are neither assessed nor deleted this run."""
    unprotected = set()
    repos = os.path.join(root, "repos")
    try:
        names = sorted(os.listdir(repos))
    except OSError:
        return unprotected
    for name in names:
        clone = os.path.join(repos, name)
        if name.startswith(".") or os.path.islink(clone) or not os.path.isdir(os.path.join(clone, ".git")):
            continue
        try:
            protect_clone(clone)
        except WorkspaceError as exc:
            unprotected.add(os.path.normpath(clone))
            out["errors"].append(f"repos/{name}: could not turn off gc worktree pruning and auto gc ({exc}); its worktrees "
                                 "are left alone this run")
    return unprotected


def _settle_deferred(root: str, out: dict, deadline, unprotected: set) -> None:
    """Settle the tasks whose own settle() could not get cleanup.lock (index state active, `settle_deferred`). Runs
    under cleanup.lock. An assessment that times out leaves the folder in active/ for the next run."""
    for tid, rec in sorted(read_index(root).items()):
        if rec.get("state") != "active" or not rec.get("settle_deferred"):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            return
        path = rec.get("path")
        if not isinstance(path, str) or not _same_path(path, os.path.join(root, "active", tid)):
            out["errors"].append(f"{tid}: deferred settle record does not name active/{tid}; left alone")
            continue
        if not os.path.isdir(path):
            done = _already_settled(rec, root)
            update_index(root, {**done, "settled_from": "cleanup"})
            continue
        repo_dir = rec.get("repo_dir")
        if rec.get("kind") == "repo":
            if isinstance(repo_dir, str) and os.path.normpath(repo_dir) in unprotected:
                continue
            ok, reason, paths, _, snap = _quiet_assess(path, (CLEANUP_QUIESCE_S,), deadline=deadline)
            if ok is None:
                out["timed_out"].append(f"{tid}: deferred settle: {reason}")
                continue
            rec = {**rec, "held_paths": paths} if paths else {k: v for k, v in rec.items() if k != "held_paths"}
        else:
            snap = None
            ok = not _contains_git_entry(path)
            reason = _Reason("no_repository", "no repository") if ok else \
                _Reason("nested_git", "plain folder contains a nested .git entry")
        done = _move_settled(rec, root, bool(ok), reason, snapshot=snap)
        update_index(root, {**done, "settled_from": "cleanup"})
        if done.get("state") == "active":
            out["errors"].append(f"{tid}: deferred settle failed: {done.get('error')}")
            continue
        out["settled"].append(tid)
        for key in ("repair_error", "registry_conflict"):
            if done.get(key):
                out["errors"].append(f"{tid}: {done[key]}")


def _cleanup_archive_locked(root: str, now=None, max_deletions=CLEANUP_MAX_DELETIONS,
                            max_seconds=CLEANUP_MAX_SECONDS) -> dict:
    """Delete old recorded archive entries through no-follow directory descriptors.

    First, for every clone: gc.worktreePruneExpire=never; then deferred settles are completed, then the registry is
    repaired for every worktree under active/, archive/ and held/ (repair_registry: bounded by the same deadline,
    with a per-entry backoff, and never for an admin dir another worktree validly owns). Repository worktrees are
    reassessed immediately before deletion. Any change, stale remote, or assessment failure moves the workspace to
    held/. An assessment that does not finish in time (a hung fetch, or no budget left) is not a failure but an
    unknown: the entry is left exactly as it is, counted in `kept` and named in `timed_out`, and tried again by a
    later run. held/, repos/, services/, and a replacement archive ancestor are never traversed. A deleted worktree's
    admin dir is removed alone (_AdminDir), after its files; no other registry entry is ever touched.

    `max_seconds` is a whole-run deadline: checked between entries, and given to every git call an entry makes as
    its subprocess timeout (min of the call's own cap and the time left), so the run — and the cleanup lock it holds,
    which a task start or settle may be waiting for — ends within the budget plus a few local commands. A registry
    repair is not started past it (named in `timed_out`), but one that has started is not cut short: it has its own
    fixed cap.
    """
    started = time.monotonic()
    deadline = None if max_seconds is None else started + max_seconds
    now = time.time() if now is None else now
    archive = os.path.join(root, "archive")
    out = {"deleted": [], "kept": 0, "refused": [], "errors": [], "timed_out": [], "repaired": [], "settled": []}
    unprotected = _protect_clones(root, out)
    # Deferred settles first (#93 round 3): a run whose registry repairs are slow must not starve them. Each move
    # repairs its own entry; the prepass then repairs what is left, under the same deadline (each repair started
    # keeps its fixed cap), before anything in archive/ is assessed or deleted.
    _settle_deferred(root, out, deadline, unprotected)
    fixed = repair_registry(root, deadline=deadline)      # backoff runs on the real clock, not the age test's `now`
    out["repaired"].extend(fixed["repaired"])
    out["errors"].extend(fixed["errors"])
    out["timed_out"].extend(fixed["skipped"])
    try:
        archive_fd = _open_dir(archive)
    except OSError as exc:
        out["errors"].append(f"{archive} is not a real directory; nothing deleted ({exc})")
        return out
    try:
        index = read_index(root)
        names = sorted(os.listdir(archive_fd))
        for pos, name in enumerate(names):
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                out["kept"] += len(names) - pos
                break
            if not TASK_ID_RE.fullmatch(name):
                out["refused"].append(f"{name}: not a task id")
                continue
            try:
                info = os.stat(name, dir_fd=archive_fd, follow_symlinks=False)
            except OSError as exc:
                out["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if stat.S_ISLNK(info.st_mode):
                out["refused"].append(f"{name}: symlink")
                continue
            if not stat.S_ISDIR(info.st_mode):
                out["refused"].append(f"{name}: not a directory")
                continue
            rec = index.get(name)
            if not isinstance(rec, dict) or rec.get("state") != "archived" \
                    or not isinstance(rec.get("settled_at"), (int, float)):
                out["refused"].append(f"{name}: no archive record in index.jsonl")
                continue
            if now - rec["settled_at"] < ARCHIVE_AGE_S:
                out["kept"] += 1
                continue
            if max_deletions is not None and len(out["deleted"]) >= max_deletions:
                out["kept"] += 1
                continue
            if rec.get("kind") == "repo" and isinstance(rec.get("repo_dir"), str) \
                    and os.path.normpath(rec["repo_dir"]) in unprotected:
                out["kept"] += 1
                continue
            entry_fd = admin = None
            try:
                entry_fd = _open_dir(name, dir_fd=archive_fd)
                path = os.path.join(archive, name)
                if not _same_entry(entry_fd, path):
                    raise WorkspaceError("archive path changed after it was opened; refusing deletion")
                if rec.get("kind") == "folder" and _fd_contains_git_entry(entry_fd):
                    _hold_changed_archive(root, archive_fd, name, rec,
                                          _Reason("nested_git", "plain folder contains a nested .git entry"), now)
                    out["kept"] += 1
                    continue
                snap = None
                if rec.get("kind") == "repo":
                    # Two clean assessments CLEANUP_QUIESCE_S apart, bracketed by identical snapshots; compared once
                    # more below, after the admin dir is resolved and immediately before the first unlink.
                    ok, reason, paths, _, snap = _quiet_assess(path, (CLEANUP_QUIESCE_S,), deadline=deadline)
                    if not _same_entry(entry_fd, path):
                        raise WorkspaceError("archive path changed during reassessment; refusing deletion")
                    if ok is None:
                        # Timed out: nothing is known about the worktree, so it is neither deleted nor moved. Not an
                        # error either (the daily job exits non-zero on errors): a later run reassesses it.
                        out["timed_out"].append(f"{name}: {reason}")
                        out["kept"] += 1
                        continue
                    if not ok:
                        _hold_changed_archive(root, archive_fd, name, {**rec, "held_paths": paths} if paths else rec,
                                              reason, now)
                        out["kept"] += 1
                        continue
                    if not isinstance(rec.get("repo_dir"), str):
                        raise WorkspaceError("its record names no clone; refusing deletion")
                    # Resolved and checked before anything is deleted: a worktree whose admin dir cannot be proven
                    # its own is not deleted at all.
                    admin = _AdminDir(entry_fd, path, rec["repo_dir"], root)
                    try:
                        final = _snapshot(path, deadline)
                    except GitTimeoutError as exc:
                        out["timed_out"].append(f"{name}: {exc}")
                        out["kept"] += 1
                        continue
                    except WorkspaceError as exc:
                        final = exc
                    if not _same_entry(entry_fd, path):
                        raise WorkspaceError("archive path changed during the final check; refusing deletion")
                    if final != snap:
                        why = final if isinstance(final, WorkspaceError) else "its files changed after the assessment"
                        _hold_changed_archive(root, archive_fd, name, rec,
                                              _not_quiet(f"{why}; checked immediately before deletion"), now)
                        out["kept"] += 1
                        continue
                _remove_tree_fd(entry_fd)
                os.close(entry_fd)
                entry_fd = None
                os.rmdir(name, dir_fd=archive_fd)
                if admin is not None:
                    # After the files: a crash in between leaves only a stale entry (never pruned, harmless).
                    try:
                        admin.remove()
                    except OSError as exc:
                        out["errors"].append(f"{name}: deleted, but its admin dir in {rec['repo_dir']}/.git/worktrees "
                                             f"was not removed ({exc}); stale, harmless")
            except (OSError, WorkspaceError) as exc:
                out["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            finally:
                if entry_fd is not None:
                    os.close(entry_fd)
                if admin is not None:
                    admin.close()
            update_index(root, {**rec, "state": "deleted", "deleted_at": now})
            out["deleted"].append(name)
    finally:
        os.close(archive_fd)
    return out
