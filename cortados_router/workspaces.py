"""Task workspaces on an execution host (owner decision 2026-11-08, issues #86/#89; layout: ~/CortadOS/README.md there).

  <root>/repos/<name>      one clone per repository; every task worktree branches from it
  <root>/active/<task_id>  the task's working folder while it runs: a git worktree, or a plain folder (no repo)
  <root>/archive/<task_id> finished and pushed (or no repository): HEAD detached at its commit so the branch is free
                           for the next task; deleted by cleanup_archive() after ARCHIVE_AGE_S
  <root>/held/<task_id>    finished with unpushed work: keeps its branch checked out (a new task on that branch is
                           refused); never deleted automatically, listed for the owner
  <root>/index.jsonl       one line per task (rewritten in place under index.lock, never appended twice for a task)

Only archive/ is ever deleted from, only entries named like a task id, only regular directories directly inside it
(never a symlink, never through one), only with an index record saying `archived` long enough ago. held/, repos/,
services/ and everything else are never touched: cleanup_archive() refuses them by construction, not by convention.

<root>/cleanup.lock serializes the cleanup's reassess-through-delete section with a task start that frees a legacy
archived worktree. The cleanup's run budget (CLEANUP_MAX_SECONDS) bounds how long it holds the lock, because every git
call it makes gets the time left as its timeout; a start waits for the lock at most CLEANUP_LOCK_WAIT_S and is then
refused with a retryable reason (#93). An assessment that times out is an unknown: nothing is deleted or moved on it.
The budget never cuts a move short: once an entry is renamed to held/, the `git worktree repair` that points the clone's
registry at the new path runs under its own fixed cap and is undone (back to archive/) if it fails, and no
`git worktree prune` runs while a registry entry names a missing path whose directory is in held/.

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
CLEANUP_LOCK_POLL_S = 0.1            # between non-blocking flock attempts: no spin, and at most 0.1 s added after release
GIT = "git"                          # resolved on PATH at call time (the execution host's PATH is set by its entry)
SUBDIRS = ("repos", "active", "archive", "held")


class WorkspaceError(Exception):
    pass


class GitTimeoutError(WorkspaceError):
    """A git call hit its timeout (or had no time left before it started). Nothing is known about what it would have
    reported, so callers that act on git's answer must treat this as "unknown", never as "clean" or "not pushed"."""


def _git(args, cwd=None, timeout=GIT_TIMEOUT_S) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
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
    else:
        if url:
            have = _git(["remote", "get-url", "origin"], cwd=repo_dir).strip()
            if have != url and have != url.rstrip("/") and have + ".git" != url and have != url + ".git":
                raise WorkspaceError(f"repos/{name} is a clone of {have}, not {url}; use that URL or another name")
        _git(["fetch", "--prune", "origin"], cwd=repo_dir, timeout=FETCH_TIMEOUT_S)
    if not _ref_exists(repo_dir, "refs/remotes/origin/HEAD"):
        _git(["remote", "set-head", "origin", "--auto"], cwd=repo_dir, timeout=FETCH_TIMEOUT_S)
    taken = _worktree_branches(repo_dir)
    if branch is not None and branch in taken:
        if _settled_under(taken[branch], root) == "archive":
            # Releasing a legacy archived worktree must not interleave with the archive cleanup's reassess-then-delete
            # section: take its lock, then look again (cleanup may have deleted and pruned the holder meanwhile).
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
                deadline = time.monotonic() + self.timeout
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
            if _git(["status", "--porcelain", "--untracked-files=all", "--ignored=matching"], cwd=path).strip():
                return "it has uncommitted, untracked or ignored files"
        except WorkspaceError as exc:
            return f"could not assess it: {exc}"
        if not (_same_entry(layout_fd, layout) and _same_entry(entry_fd, path)):
            return "the archived worktree changed while it was being assessed"
        return _detach_head(path, expect=head)
    finally:
        if entry_fd is not None:
            os.close(entry_fd)
        os.close(layout_fd)


def pushed(path: str, deadline=None):
    """(True, "pushed") only after fresh remote refs prove every byte and commit is published.

    (False, reason): not proven published; any git or fetch failure is uncertainty too, so the caller holds the
    workspace rather than risking owner data. (None, reason): the assessment did not finish (a git call timed out, or
    `deadline` — a time.monotonic() value — left no time for one), so nothing is known either way; a caller that
    would otherwise move or delete must leave the workspace exactly as it is. `None` is falsy on purpose: a caller
    that only asks "proven pushed?" (settle) holds, which is the conservative answer.
    """
    try:
        remotes = _git(["remote"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S)).split()
        if not remotes:
            return False, "could not assess the worktree: it has no remote"
        for remote in remotes:
            # An explicit all-heads refspec also prunes stale refs excluded by a locally narrowed fetch refspec.
            _git(["fetch", "--prune", remote,
                  f"+refs/heads/*:refs/remotes/{remote}/*"], cwd=path, timeout=_within(deadline, FETCH_TIMEOUT_S))
        submodules = _git(["ls-files", "--stage"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S))
        if any(line.startswith("160000 ") for line in submodules.splitlines()):
            return False, "submodule content cannot be proven published; holding the workspace"
        # Ignored content is owner data too. No cache (including __pycache__ or node_modules) is exempt: generated and
        # unpublished content cannot be distinguished safely here, and holding is the required conservative default.
        flagged = _git(["ls-files", "-v"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S))
        hidden = [line for line in flagged.splitlines() if line and (line[0].islower() or line[0] == "S")]
        if hidden:
            return False, f"{len(hidden)} tracked file(s) hidden by assume-unchanged or skip-worktree"
        dirty = _git(["status", "--porcelain", "--untracked-files=all", "--ignored=matching"], cwd=path,
                     timeout=_within(deadline, GIT_TIMEOUT_S))
        if dirty.strip():
            n = len(dirty.splitlines())
            return False, f"{n} uncommitted or untracked file(s), including ignored content"
        ahead = _git(["rev-list", "HEAD", "--not", "--remotes"], cwd=path, timeout=_within(deadline, GIT_TIMEOUT_S))
        if ahead.strip():
            return False, f"{len(ahead.split())} commit(s) not on any remote"
    except GitTimeoutError as exc:
        return None, f"could not assess the worktree in time: {exc}"
    except WorkspaceError as exc:
        return False, f"could not assess the worktree: {exc}"
    return True, "pushed"


def settle(rec: dict, root: str) -> dict:
    """Move a finished task's folder out of active/: to archive/ when it is pushed or had no repository, else to
    held/. Returns the updated record (state archived|held, or still active with `error` if the move failed, in which
    case the folder stays where it is and nothing is deleted)."""
    rec = dict(rec)
    path = rec.get("path")
    if rec.get("state") != "active" or not path:
        return rec
    if not os.path.isdir(path):
        rec.update(state="missing", settled_at=time.time(), reason="the working folder no longer exists")
        return rec
    if rec.get("kind") == "repo":
        ok, reason = pushed(path)
    else:
        ok = not _contains_git_entry(path)
        reason = "no repository" if ok else "plain folder contains a nested .git entry"
    dest_dir = os.path.join(root, "archive" if ok else "held")
    dest = os.path.join(dest_dir, rec["task_id"])
    try:
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.lexists(dest):
            raise WorkspaceError(f"{dest} already exists")
        if rec.get("kind") == "repo" and os.path.isdir(rec.get("repo_dir") or ""):
            _git(["worktree", "move", "--", path, dest], cwd=rec["repo_dir"])   # keeps the worktree valid at its new path
        else:
            os.rename(path, dest)
    except (OSError, WorkspaceError) as exc:
        rec["error"] = f"could not move {path} to {dest}: {exc}"
        return rec
    rec.update(state="archived" if ok else "held", path=dest, settled_at=time.time(), reason=reason)
    if ok and rec.get("kind") == "repo":
        # Pushed work: free the branch for the next task (the commit stays checked out, detached). Held work keeps
        # its branch, so a new task cannot start on it while it is unpushed. A failed detach loses nothing: the
        # worktree stays archived and attached, and the reason is recorded here and in index.jsonl.
        why = _detach_head(dest)
        if why:
            rec["detach_error"] = why
        else:
            rec["detached"] = True
    return rec


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
    lock = os.open(os.path.join(root, "index.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recs = read_index(root)
        recs[rec["task_id"]] = {k: v for k, v in rec.items() if k != "error"} if "error" not in rec else dict(rec)
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
    """Find a .git entry at any depth without traversing symlinks."""
    for directory, dirs, files in os.walk(path, followlinks=False):
        if ".git" in dirs or ".git" in files:
            return True
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(directory, name))]
    return False


def _fd_contains_git_entry(directory_fd: int) -> bool:
    for name in os.listdir(directory_fd):
        if name == ".git":
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


def _moved_held_worktrees(repo_dir: str, root: str, deadline=None) -> list:
    """Paths under <root>/held/ of worktrees whose registry entry in repo_dir still names a path with no .git: an
    archived worktree that was renamed to held/ but whose `git worktree repair` never ran (a crash or an earlier
    failure). `git worktree prune` would delete their admin dirs (index, detached HEAD, reflog), so they are repaired
    before any prune. The listing itself gets the budget: with no time left this raises GitTimeoutError and the
    caller skips the prune (stale admin metadata is harmless; a moved worktree is not)."""
    out = _git(["worktree", "list", "--porcelain"], cwd=repo_dir, timeout=_within(deadline, GIT_TIMEOUT_S))
    held_layout = os.path.join(root, "held")
    moved = []
    for line in out.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):]
        name = os.path.basename(os.path.normpath(path))
        if not TASK_ID_RE.fullmatch(name) or os.path.exists(os.path.join(path, ".git")):
            continue
        held_path = os.path.join(held_layout, name)
        try:
            info = os.lstat(held_path)
        except OSError:
            continue                                  # really gone (a deleted archive entry): prunable
        if stat.S_ISDIR(info.st_mode) and os.path.isfile(os.path.join(held_path, ".git")):
            moved.append(held_path)
    return moved


def _repair_worktree(held_path: str, repo_dir: str) -> None:
    """Point the clone's registry at a worktree's new path. Local and fast; it gets its own fixed cap, never the
    cleanup budget, because skipping it leaves a registry entry at a missing path that the next prune would delete
    along with the worktree's admin dir. Raises WorkspaceError (GitTimeoutError included) when it did not succeed."""
    _git(["worktree", "repair", held_path], cwd=repo_dir, timeout=GIT_TIMEOUT_S)


def _prune_worktrees(rec: dict, root: str, deadline=None) -> list:
    """Drop the registry entry of the worktree just deleted. Never runs while a registry entry names a missing path
    whose directory is in held/: those are repaired first (fixed cap), and if one cannot be repaired nothing is
    pruned. Returns a list of error strings (the deletion itself is complete either way; a prune skipped for lack
    of budget is not an error, a repair that failed is)."""
    repo_dir = rec.get("repo_dir")
    if rec.get("kind") != "repo" or not isinstance(repo_dir, str) or not os.path.isdir(repo_dir):
        return []
    try:
        moved = _moved_held_worktrees(repo_dir, root, deadline=deadline)
    except WorkspaceError:
        return []       # could not list (or no time left): nothing pruned, stale admin metadata can be pruned later
    errors = []
    for held_path in moved:
        try:
            _repair_worktree(held_path, repo_dir)
        except WorkspaceError as exc:
            errors.append(f"{os.path.basename(held_path)}: held worktree {held_path} is registered at a missing path "
                          f"and could not be repaired ({exc}); not pruning {repo_dir} until it is")
    if errors:
        return errors
    try:
        _git(["worktree", "prune", "--expire", "now"], cwd=repo_dir, timeout=_within(deadline, GIT_TIMEOUT_S))
    except WorkspaceError:
        pass  # The data operation is complete; stale administrative metadata is safe and can be pruned later.
    return []


def _hold_changed_archive(root: str, archive_fd: int, name: str, rec: dict, reason: str, now: float) -> dict:
    """Move a newly changed archived worktree to held/ using anchored source and destination descriptors, then repair
    the clone's registry so it names the new path. The repair is never skipped (it runs under its own fixed cap, not
    the cleanup budget): a held worktree the registry still lists at archive/<name> would lose its admin dir to the
    next `git worktree prune`. If the repair fails the move is undone (the entry goes back to archive/, where the
    next cleanup reassesses it again) and the failure is raised for the cleanup report; the index is only written
    once the worktree is in held/ with a correct registry entry."""
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
        updated = {**rec, "state": "held", "path": held_path, "settled_at": now, "reason": reason}
        repo_dir = rec.get("repo_dir")
        if rec.get("kind") == "repo" and isinstance(repo_dir, str) and os.path.isdir(repo_dir):
            try:
                _repair_worktree(held_path, repo_dir)
            except WorkspaceError as exc:
                try:
                    os.rename(name, name, src_dir_fd=held_fd, dst_dir_fd=archive_fd)
                except OSError as back:
                    # Worst case: in held/ with a registry entry at the old path. Record it so the owner sees it in
                    # the held listing; _prune_worktrees() repairs it before any prune of this clone.
                    updated["repair_error"] = str(exc)
                    update_index(root, updated)
                    raise WorkspaceError(f"moved to held/ but `git worktree repair` failed ({exc}) and moving it back "
                                         f"to archive/ failed too ({back}); its registry entry still names "
                                         f"{os.path.join(root, 'archive', name)}") from None
                raise WorkspaceError(f"not held: `git worktree repair` failed after the move ({exc}); moved back to "
                                     "archive/ for the next cleanup to reassess") from None
    finally:
        os.close(held_fd)
    update_index(root, updated)
    return updated


def cleanup_archive(root: str, now=None, max_deletions=CLEANUP_MAX_DELETIONS, max_seconds=CLEANUP_MAX_SECONDS) -> dict:
    """Serialize reassessment through deletion so a concurrent cleanup cannot move an entry being deleted, and so a
    start releasing a legacy archived worktree (which takes the same lock) cannot change one in between."""
    with _cleanup_lock(root):
        return _cleanup_archive_locked(root, now=now, max_deletions=max_deletions, max_seconds=max_seconds)


def _cleanup_archive_locked(root: str, now=None, max_deletions=CLEANUP_MAX_DELETIONS,
                            max_seconds=CLEANUP_MAX_SECONDS) -> dict:
    """Delete old recorded archive entries through no-follow directory descriptors.

    Repository worktrees are reassessed immediately before deletion. Any change, stale remote, or assessment failure
    moves the workspace to held/. An assessment that does not finish in time (a hung fetch, or no budget left) is
    not a failure but an unknown: the entry is left exactly as it is, counted in `kept` and named in `timed_out`,
    and tried again by a later run. held/, repos/, services/, and a replacement archive ancestor are never traversed.

    `max_seconds` is a whole-run deadline: checked between entries, and given to every git call an entry makes as
    its subprocess timeout (min of the call's own cap and the time left), so the run — and the cleanup lock it holds,
    which a task start may be waiting for — ends within the budget plus one local command. Two steps are never cut
    short by it, because a half-done sequence would leave a held worktree's registry entry at a missing path for a
    prune to destroy: the repair after a move to held/ (_hold_changed_archive, fixed cap, rolled back on failure) and
    the repair of any such entry before a prune (_prune_worktrees, which otherwise skips the prune and reports).
    """
    started = time.monotonic()
    deadline = None if max_seconds is None else started + max_seconds
    now = time.time() if now is None else now
    archive = os.path.join(root, "archive")
    out = {"deleted": [], "kept": 0, "refused": [], "errors": [], "timed_out": []}
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
            entry_fd = None
            try:
                entry_fd = _open_dir(name, dir_fd=archive_fd)
                path = os.path.join(archive, name)
                if not _same_entry(entry_fd, path):
                    raise WorkspaceError("archive path changed after it was opened; refusing deletion")
                if rec.get("kind") == "folder" and _fd_contains_git_entry(entry_fd):
                    _hold_changed_archive(root, archive_fd, name, rec,
                                          "plain folder contains a nested .git entry", now)
                    out["kept"] += 1
                    continue
                if rec.get("kind") == "repo":
                    ok, reason = pushed(path, deadline=deadline)
                    if not _same_entry(entry_fd, path):
                        raise WorkspaceError("archive path changed during reassessment; refusing deletion")
                    if ok is None:
                        # Timed out: nothing is known about the worktree, so it is neither deleted nor moved. Not an
                        # error either (the daily job exits non-zero on errors): a later run reassesses it.
                        out["timed_out"].append(f"{name}: {reason}")
                        out["kept"] += 1
                        continue
                    if not ok:
                        _hold_changed_archive(root, archive_fd, name, rec, reason, now)
                        out["kept"] += 1
                        continue
                _remove_tree_fd(entry_fd)
                os.close(entry_fd)
                entry_fd = None
                os.rmdir(name, dir_fd=archive_fd)
                out["errors"].extend(_prune_worktrees(rec, root, deadline=deadline))
            except (OSError, WorkspaceError) as exc:
                out["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            finally:
                if entry_fd is not None:
                    os.close(entry_fd)
            update_index(root, {**rec, "state": "deleted", "deleted_at": now})
            out["deleted"].append(name)
    finally:
        os.close(archive_fd)
    return out
