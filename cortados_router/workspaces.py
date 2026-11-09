"""Task workspaces on an execution host (owner decision 2026-11-08, issues #86/#89; layout: ~/CortadOS/README.md there).

  <root>/repos/<name>      one clone per repository; every task worktree branches from it
  <root>/active/<task_id>  the task's working folder while it runs: a git worktree, or a plain folder (no repo)
  <root>/archive/<task_id> finished and pushed (or no repository): deleted by cleanup_archive() after ARCHIVE_AGE_S
  <root>/held/<task_id>    finished with unpushed work: never deleted automatically, listed for the owner
  <root>/index.jsonl       one line per task (rewritten in place under index.lock, never appended twice for a task)

Only archive/ is ever deleted from, only entries named like a task id, only regular directories directly inside it
(never a symlink, never through one), only with an index record saying `archived` long enough ago. held/, repos/,
services/ and everything else are never touched: cleanup_archive() refuses them by construction, not by convention.

Stdlib only. git runs as a subprocess with GIT_TERMINAL_PROMPT=0 (an unauthenticated clone fails instead of hanging).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
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
GIT = "git"                          # resolved on PATH at call time (the execution host's PATH is set by its entry)
SUBDIRS = ("repos", "active", "archive", "held")


class WorkspaceError(Exception):
    pass


def _git(args, cwd=None, timeout=GIT_TIMEOUT_S) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise WorkspaceError("git is not installed or not on PATH on this execution host") from None
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {args[0]} did not finish within {timeout} s") from None
    if p.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed (exit {p.returncode}): {p.stderr.strip()[-400:]}")
    return p.stdout


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
    if branch is None:
        branch = TASK_BRANCH_PREFIX + tid
        _git(["worktree", "add", "-b", branch, "--", path, "origin/HEAD"], cwd=repo_dir)
    elif branch in taken:
        raise WorkspaceError(f"branch {branch!r} is checked out by another worktree ({taken[branch]}); wait for that "
                             "task or name another branch")
    elif _ref_exists(repo_dir, "refs/remotes/origin/" + branch):
        # The branch as it is on origin: a stale local branch of the same name (left by an archived, i.e. fully
        # pushed, task) is reset to it; -B never drops anything that is not already on the remote.
        _git(["worktree", "add", "-B", branch, "--", path, "origin/" + branch], cwd=repo_dir)
    elif _ref_exists(repo_dir, "refs/heads/" + branch):
        _git(["worktree", "add", "--", path, branch], cwd=repo_dir)       # local-only branch: keep its commits
    else:
        _git(["worktree", "add", "-b", branch, "--", path, "origin/HEAD"], cwd=repo_dir)   # a new branch
    rec.update(repo=name, url=url or _git(["remote", "get-url", "origin"], cwd=repo_dir).strip(),
               repo_dir=repo_dir, branch=branch)
    return rec


def pushed(path: str):
    """(True, "pushed") when the worktree holds no uncommitted or untracked change and every commit reachable from
    HEAD is on some remote ref; otherwise (False, why). A git failure is (False, why): work we cannot assess is held."""
    try:
        dirty = _git(["status", "--porcelain", "--untracked-files=all"], cwd=path)
        if dirty.strip():
            n = len(dirty.splitlines())
            return False, f"{n} uncommitted or untracked file(s)"
        ahead = _git(["rev-list", "HEAD", "--not", "--remotes"], cwd=path)
        if ahead.strip():
            return False, f"{len(ahead.split())} commit(s) not on any remote"
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
        ok, reason = True, "no repository"
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

def cleanup_archive(root: str, now=None, max_deletions=None) -> dict:
    """Delete archive/<task_id> entries whose index record says `archived` more than ARCHIVE_AGE_S ago. Everything
    else is refused and listed: a name that is not a task id, a symlink (never followed, never removed), a non-
    directory, an entry outside the real archive directory, an entry without an archive record (we do not delete what
    we did not archive), or one still within the week. held/, repos/, services/ are never visited. `max_deletions`
    bounds one call (start_task runs this opportunistically and must answer quickly; the daily job passes None)."""
    now = time.time() if now is None else now
    archive = os.path.join(root, "archive")
    out = {"deleted": [], "kept": 0, "refused": [], "errors": []}
    if os.path.islink(archive) or not os.path.isdir(archive):
        out["errors"].append(f"{archive} is not a real directory; nothing deleted")
        return out
    real_archive = os.path.realpath(archive)
    index = read_index(root)
    for name in sorted(os.listdir(archive)):
        p = os.path.join(archive, name)
        if not TASK_ID_RE.fullmatch(name):
            out["refused"].append(f"{name}: not a task id")
            continue
        if os.path.islink(p):
            out["refused"].append(f"{name}: symlink")
            continue
        if not os.path.isdir(p):
            out["refused"].append(f"{name}: not a directory")
            continue
        if os.path.dirname(os.path.realpath(p)) != real_archive:
            out["refused"].append(f"{name}: resolves outside archive/")
            continue
        rec = index.get(name)
        if not isinstance(rec, dict) or rec.get("state") != "archived" or not isinstance(rec.get("settled_at"), (int, float)):
            out["refused"].append(f"{name}: no archive record in index.jsonl")
            continue
        if now - rec["settled_at"] < ARCHIVE_AGE_S:
            out["kept"] += 1
            continue
        if max_deletions is not None and len(out["deleted"]) >= max_deletions:
            out["kept"] += 1
            continue
        try:
            _delete_entry(p, rec)
        except (OSError, WorkspaceError) as exc:
            out["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        update_index(root, {**rec, "state": "deleted", "deleted_at": now})
        out["deleted"].append(name)
    return out


def _delete_entry(p: str, rec: dict) -> None:
    repo_dir = rec.get("repo_dir")
    if rec.get("kind") == "repo" and isinstance(repo_dir, str) and os.path.isdir(repo_dir):
        try:
            _git(["worktree", "remove", "--force", "--", p], cwd=repo_dir)   # the folder and the clone's record of it
        except WorkspaceError:
            pass                                    # fall through: remove the folder, then prune the clone's records
    if os.path.lexists(p):
        if os.path.islink(p):
            raise WorkspaceError("became a symlink; refusing")
        shutil.rmtree(p)
    if rec.get("kind") == "repo" and isinstance(repo_dir, str) and os.path.isdir(repo_dir):
        try:
            _git(["worktree", "prune"], cwd=repo_dir)
        except WorkspaceError:
            pass                                    # the folder is gone; a stale record in the clone is harmless
