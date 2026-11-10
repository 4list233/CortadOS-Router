"""workspaces: repo clone + worktree, archive/held classification, index.jsonl, and the archive cleanup's limits —
against a real git on a throwaway bare 'origin' in a temp dir (git is on every macOS; nothing leaves the temp dir)."""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortados_router import workspaces as ws  # noqa: E402

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_TERMINAL_PROMPT": "0", "HOME": tempfile.gettempdir()}
TID = "t20261108120000-0000000a"
TID2 = "t20261108120100-0000000b"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV).stdout


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "CortadOS")
        os.mkdir(self.root)
        self.origin = os.path.join(self.tmp, "origin", "demo.git")
        os.makedirs(os.path.dirname(self.origin))
        git("-c", "init.defaultBranch=main", "init", "-q", "--bare", self.origin)
        seed = os.path.join(self.tmp, "seed")
        git("clone", "-q", self.origin, seed)
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("demo\n")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "init", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        git("checkout", "-q", "-b", "feature", cwd=seed)
        with open(os.path.join(seed, "f.txt"), "w") as f:
            f.write("f\n")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "feature", cwd=seed)
        git("push", "-q", "origin", "feature", cwd=seed)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)
        shutil.rmtree(seed)

    def prepare(self, tid=TID, repo=None, branch=None):
        return ws.prepare(ws.workspace_spec(tid, repo, branch), self.root)


class ParseTests(unittest.TestCase):
    def test_parse_repo(self):
        self.assertEqual(ws.parse_repo("https://github.com/4list233/CortadOS-Router.git"),
                         ("https://github.com/4list233/CortadOS-Router.git", "CortadOS-Router"))
        self.assertEqual(ws.parse_repo("git@github.com:4list233/CortadOS-Router.git")[1], "CortadOS-Router")
        self.assertEqual(ws.parse_repo("/Users/x/origin/demo.git"), ("/Users/x/origin/demo.git", "demo"))
        self.assertEqual(ws.parse_repo("CortadOS-Router"), (None, "CortadOS-Router"))
        for bad in ("", "  ", "-rf", "https://x/.hidden.git", "https://x/a b", 7, "name with space"):
            with self.assertRaises(ws.WorkspaceError, msg=bad):
                ws.parse_repo(bad)

    def test_check_branch(self):
        for ok in ("main", "feature/x-1", "task/t20261108120000-0000000a", "v1.2"):
            self.assertEqual(ws.check_branch(ok), ok)
        for bad in ("", "-x", "a..b", "a@{1}", "x/", "x.lock", "a//b", "has space", "a~b", None, "x" * 200):
            with self.assertRaises(ws.WorkspaceError, msg=bad):
                ws.check_branch(bad)

    def test_spec_rules(self):
        self.assertEqual(ws.workspace_spec(TID)["kind"], "folder")
        with self.assertRaises(ws.WorkspaceError):
            ws.workspace_spec(TID, None, "main")                 # branch needs repo
        with self.assertRaises(ws.WorkspaceError):
            ws.workspace_spec("../x", "demo")


class PrepareTests(_Base):
    def test_folder(self):
        rec = self.prepare()
        self.assertEqual((rec["kind"], rec["state"]), ("folder", "active"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "active", TID)))
        for sub in ws.SUBDIRS:
            self.assertTrue(os.path.isdir(os.path.join(self.root, sub)))
        with self.assertRaises(ws.WorkspaceError):
            self.prepare()                                        # already exists

    def test_root_must_exist(self):
        with self.assertRaises(ws.WorkspaceError) as cm:
            ws.prepare(ws.workspace_spec(TID), os.path.join(self.tmp, "nope"))
        self.assertIn("does not exist on this execution host", str(cm.exception))

    def test_repo_clones_once_and_each_task_gets_its_own_task_branch(self):
        rec = self.prepare(repo=self.origin)
        repo_dir = os.path.join(self.root, "repos", "demo")
        self.assertEqual(rec["repo_dir"], repo_dir)
        self.assertEqual(rec["branch"], "task/" + TID)
        path = os.path.join(self.root, "active", TID)
        self.assertEqual(rec["path"], path)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip(), "task/" + TID)
        self.assertTrue(os.path.exists(os.path.join(path, "README.md")))
        self.assertFalse(os.path.exists(os.path.join(path, "f.txt")))      # from main, not feature
        marker = os.path.join(repo_dir, "marker")
        open(marker, "w").close()
        rec2 = self.prepare(TID2, repo="demo")                     # by name: no URL, no re-clone
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(rec2["branch"], "task/" + TID2)
        self.assertEqual(rec2["url"], self.origin)
        listed = git("worktree", "list", "--porcelain", cwd=repo_dir)
        self.assertIn(path, listed)
        self.assertIn(rec2["path"], listed)

    def test_concurrent_first_tasks_share_one_clone_safely(self):
        barrier = threading.Barrier(2)
        records, errors = [], []

        def run(tid):
            try:
                barrier.wait()
                records.append(self.prepare(tid, repo=self.origin))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(tid,)) for tid in (TID, TID2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({rec["task_id"] for rec in records}, {TID, TID2})
        self.assertTrue(all(os.path.isdir(rec["path"]) for rec in records))

    def test_named_branches(self):
        rec = self.prepare(repo=self.origin, branch="feature")     # exists on origin: checked out from it
        self.assertTrue(os.path.exists(os.path.join(rec["path"], "f.txt")))
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")       # checked out by the first task
        self.assertIn("checked out by another worktree", str(cm.exception))
        self.assertIn(rec["path"], str(cm.exception))
        rec2 = self.prepare(TID2, repo="demo", branch="brand-new")  # not on origin: a new branch from origin/HEAD
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=rec2["path"]).strip(), "brand-new")
        self.assertFalse(os.path.exists(os.path.join(rec2["path"], "f.txt")))

    def test_existing_local_branch_is_preserved_and_divergence_is_refused(self):
        first = self.prepare(repo=self.origin, branch="feature")
        with open(os.path.join(first["path"], "local.txt"), "w") as f:
            f.write("unpublished\n")
        git("add", ".", cwd=first["path"])
        git("commit", "-q", "-m", "local only", cwd=first["path"])
        local_head = git("rev-parse", "HEAD", cwd=first["path"]).strip()
        git("worktree", "remove", "--force", first["path"], cwd=first["repo_dir"])

        preserved = self.prepare(TID2, repo="demo", branch="feature")
        self.assertEqual(git("rev-parse", "HEAD", cwd=preserved["path"]).strip(), local_head)
        self.assertTrue(os.path.exists(os.path.join(preserved["path"], "local.txt")))
        git("worktree", "remove", "--force", preserved["path"], cwd=preserved["repo_dir"])

        seed = os.path.join(self.tmp, "remote-change")
        git("clone", "-q", self.origin, seed)
        git("checkout", "-q", "feature", cwd=seed)
        with open(os.path.join(seed, "remote.txt"), "w") as f:
            f.write("remote\n")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "remote only", cwd=seed)
        git("push", "-q", "origin", "feature", cwd=seed)
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare("t20261108120200-0000000c", repo="demo", branch="feature")
        self.assertIn("diverged", str(cm.exception))

    def test_name_without_clone_and_url_mismatch_are_refused(self):
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(repo="demo")
        self.assertIn("no clone named 'demo'", str(cm.exception))
        self.prepare(repo=self.origin)
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="/elsewhere/demo.git")
        self.assertIn("is a clone of", str(cm.exception))


class SettleTests(_Base):
    def test_pushed_classification_and_moves(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        self.assertEqual(ws.pushed(rec["path"]), (True, "pushed"))
        # untracked file: held
        with open(os.path.join(rec["path"], "scratch.txt"), "w") as f:
            f.write("x")
        ok, why = ws.pushed(rec["path"])
        self.assertFalse(ok)
        self.assertIn("uncommitted or untracked", why)
        # committed but not pushed: held
        git("add", ".", cwd=rec["path"])
        git("commit", "-q", "-m", "wip", cwd=rec["path"])
        ok, why = ws.pushed(rec["path"])
        self.assertFalse(ok)
        self.assertIn("not on any remote", why)
        settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "held")
        held = os.path.join(self.root, "held", TID)
        self.assertEqual(settled["path"], held)
        self.assertTrue(os.path.isdir(held))
        self.assertFalse(os.path.exists(rec["path"]))
        self.assertIn(held, git("worktree", "list", "--porcelain", cwd=rec["repo_dir"]))   # still a valid worktree
        self.assertEqual(ws.settle(settled, self.root), settled)                           # idempotent
        # pushed: archived
        rec2 = self.prepare(TID2, repo="demo")
        with open(os.path.join(rec2["path"], "a.txt"), "w") as f:
            f.write("a")
        git("add", ".", cwd=rec2["path"])
        git("commit", "-q", "-m", "a", cwd=rec2["path"])
        git("push", "-q", "origin", "HEAD", cwd=rec2["path"])
        settled2 = ws.settle(rec2, self.root)
        self.assertEqual(settled2["state"], "archived")
        self.assertEqual(settled2["path"], os.path.join(self.root, "archive", TID2))
        self.assertEqual(settled2["reason"], "pushed")

    def test_folder_is_archived_and_a_missing_folder_is_recorded(self):
        rec = self.prepare()
        settled = ws.settle(rec, self.root)
        self.assertEqual((settled["state"], settled["reason"]), ("archived", "no repository"))
        shutil.rmtree(settled["path"])
        gone = ws.settle({**rec}, self.root)
        self.assertEqual(gone["state"], "missing")

    def test_plain_folder_with_nested_git_entry_is_held_at_settle_and_cleanup(self):
        rec = self.prepare()
        nested = os.path.join(rec["path"], "nested")
        os.makedirs(os.path.join(nested, ".git"))
        settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "held")
        self.assertIn("nested .git", settled["reason"])

        path = self.prepare(TID2)["path"]
        archived = ws.settle({"task_id": TID2, "kind": "folder", "path": path, "state": "active"}, self.root)
        archived["settled_at"] = 0
        ws.update_index(self.root, archived)
        os.makedirs(os.path.join(archived["path"], "late", ".git"))
        report = ws.cleanup_archive(self.root, now=ws.ARCHIVE_AGE_S + 1)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(ws.read_index(self.root)[TID2]["state"], "held")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "held", TID2, "late", ".git")))

    def test_ignored_files_are_unpublished_work(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        with open(os.path.join(rec["path"], ".gitignore"), "w") as f:
            f.write("private.dat\n")
        git("add", ".gitignore", cwd=rec["path"])
        git("commit", "-q", "-m", "ignore private data", cwd=rec["path"])
        git("push", "-q", "origin", "feature", cwd=rec["path"])
        with open(os.path.join(rec["path"], "private.dat"), "w") as f:
            f.write("must survive\n")
        settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "held")
        self.assertTrue(os.path.exists(os.path.join(settled["path"], "private.dat")))

    def test_skip_worktree_cannot_hide_unpublished_bytes(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        git("update-index", "--skip-worktree", "f.txt", cwd=rec["path"])
        with open(os.path.join(rec["path"], "f.txt"), "w") as f:
            f.write("private replacement\n")
        ok, why = ws.pushed(rec["path"])
        self.assertFalse(ok)
        self.assertIn("skip-worktree", why)

    def test_deleted_remote_branch_is_not_proof_of_publication(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        git("update-ref", "-d", "refs/heads/feature", cwd=self.origin)
        ok, why = ws.pushed(rec["path"])
        self.assertFalse(ok)
        self.assertIn("not on any remote", why)

    def test_index_has_one_line_per_task(self):
        rec = self.prepare()
        ws.update_index(self.root, rec)
        ws.update_index(self.root, ws.settle(rec, self.root))
        ws.update_index(self.root, {"task_id": TID2, "state": "active"})
        with open(ws.index_path(self.root)) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual([x["task_id"] for x in lines], [TID, TID2])
        self.assertEqual(lines[0]["state"], "archived")
        self.assertEqual(ws.read_index(self.root)[TID]["state"], "archived")
        with self.assertRaises(ws.WorkspaceError):
            ws.update_index(self.root, {"state": "x"})


class BranchReleaseTests(_Base):
    """Issue #92: an archived worktree frees its branch; a held one keeps it; a legacy attached archive is freed only
    when that provably loses nothing."""

    def head(self, path):
        return git("rev-parse", "HEAD", cwd=path).strip()

    def attached(self, path):
        return subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=path, capture_output=True,
                              env=GIT_ENV).returncode == 0

    def archive_feature(self, tid=TID):
        rec = self.prepare(tid, repo=self.origin, branch="feature")
        commit = self.head(rec["path"])
        settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "archived")
        return settled, commit

    def test_archive_detaches_at_same_commit_and_branch_is_reusable(self):
        settled, commit = self.archive_feature()
        self.assertTrue(settled.get("detached"))
        self.assertNotIn("detach_error", settled)
        self.assertFalse(self.attached(settled["path"]))
        self.assertEqual(self.head(settled["path"]), commit)
        self.assertTrue(os.path.exists(os.path.join(settled["path"], "f.txt")))
        rec2 = self.prepare(TID2, repo="demo", branch="feature")
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=rec2["path"]).strip(), "feature")
        self.assertEqual(self.head(rec2["path"]), commit)
        self.assertNotIn("freed_from_archive", rec2)

    def test_held_worktree_keeps_branch_and_refusal_names_it(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        with open(os.path.join(rec["path"], "wip.txt"), "w") as f:
            f.write("unpushed\n")
        settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "held")
        self.assertTrue(self.attached(settled["path"]))
        self.assertNotIn("detached", settled)
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        msg = str(cm.exception)
        self.assertIn("held worktree with unpushed work", msg)
        self.assertIn(settled["path"], msg)
        self.assertTrue(os.path.exists(os.path.join(settled["path"], "wip.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "active", TID2)))

    def test_failed_detach_is_recorded_and_loses_nothing(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        commit = self.head(rec["path"])
        gitdir = git("rev-parse", "--absolute-git-dir", cwd=rec["path"]).strip()
        lock = os.path.join(gitdir, "HEAD.lock")              # makes git refuse to rewrite HEAD
        open(lock, "w").close()
        settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "archived")
        self.assertEqual(settled["path"], os.path.join(self.root, "archive", TID))
        self.assertIn("could not detach HEAD", settled["detach_error"])
        self.assertNotIn("detached", settled)
        self.assertTrue(self.attached(settled["path"]))
        self.assertEqual(self.head(settled["path"]), commit)
        ws.update_index(self.root, settled)
        self.assertIn("detach_error", ws.read_index(self.root)[TID])
        # still locked: the start-time release fails too, and the refusal says why
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("was not detached", str(cm.exception))
        os.unlink(lock)
        rec2 = self.prepare(TID2, repo="demo", branch="feature")
        self.assertEqual(rec2["freed_from_archive"], settled["path"])

    def legacy(self):
        """An archive entry from before #92: settled, then re-attached to its branch."""
        settled, commit = self.archive_feature()
        git("checkout", "-q", "feature", cwd=settled["path"])
        self.assertTrue(self.attached(settled["path"]))
        return settled["path"], commit

    def test_legacy_clean_archive_at_remote_tip_is_detached_on_start(self):
        path, commit = self.legacy()
        rec2 = self.prepare(TID2, repo="demo", branch="feature")
        self.assertEqual(rec2["freed_from_archive"], path)
        self.assertFalse(self.attached(path))
        self.assertEqual(self.head(path), commit)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=rec2["path"]).strip(), "feature")

    def test_legacy_dirty_archive_is_refused_and_untouched(self):
        path, _ = self.legacy()
        with open(os.path.join(path, "late.txt"), "w") as f:
            f.write("late\n")
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("checked out by another worktree", str(cm.exception))
        self.assertIn("untracked", str(cm.exception))
        self.assertTrue(self.attached(path))
        self.assertTrue(os.path.exists(os.path.join(path, "late.txt")))

    def test_legacy_archive_ahead_of_remote_is_refused_and_untouched(self):
        path, commit = self.legacy()
        with open(os.path.join(path, "local.txt"), "w") as f:
            f.write("local\n")
        git("add", ".", cwd=path)
        git("commit", "-q", "-m", "local only", cwd=path)
        ahead = self.head(path)
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("is not the tip of origin/feature", str(cm.exception))
        self.assertTrue(self.attached(path))
        self.assertEqual(self.head(path), ahead)

    def install_hooks(self):
        """post-checkout and reference-transaction hooks (shared by every worktree of the clone) that create a file in
        whatever worktree they run in: unpublished data if they ever ran in an archived worktree."""
        hooks = os.path.join(self.root, "repos", "demo", ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        for name in ("post-checkout", "reference-transaction"):
            hook = os.path.join(hooks, name)
            with open(hook, "w") as f:
                f.write(f"#!/bin/sh\necho ran > \"$(git rev-parse --show-toplevel)/hook-{name}.txt\"\n")
            os.chmod(hook, 0o755)
        return [f"hook-{name}.txt" for name in ("post-checkout", "reference-transaction")]

    def test_settle_detach_runs_no_hook(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        files = self.install_hooks()
        settled = ws.settle(rec, self.root)
        self.assertTrue(settled.get("detached"), settled)
        self.assertFalse(self.attached(settled["path"]))
        for name in files:
            self.assertFalse(os.path.exists(os.path.join(settled["path"], name)), name)
        self.assertEqual(ws.pushed(settled["path"]), (True, "pushed"))

    def test_legacy_release_runs_no_hook_and_cleanup_deletes_nothing_unpublished(self):
        path, commit = self.legacy()
        files = self.install_hooks()
        rec2 = self.prepare(TID2, repo="demo", branch="feature")
        self.assertEqual(rec2["freed_from_archive"], path)
        self.assertFalse(self.attached(path))
        self.assertEqual(self.head(path), commit)
        for name in files:
            self.assertFalse(os.path.exists(os.path.join(path, name)), name)
        # the old archive is still provably published, so cleanup may delete it; nothing unpublished goes with it
        self.assertEqual(ws.pushed(path), (True, "pushed"))
        ws.update_index(self.root, {"task_id": TID, "kind": "repo", "path": path, "repo_dir": rec2["repo_dir"],
                                    "state": "archived", "settled_at": 0})
        report = ws.cleanup_archive(self.root, now=ws.ARCHIVE_AGE_S + 1)
        self.assertEqual(report["deleted"], [TID])

    def hold_cleanup_lock(self):
        """What a cleanup in its reassess-through-delete section holds; released by closing the descriptor."""
        lock = os.open(os.path.join(self.root, "cleanup.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock

    def test_legacy_release_waits_for_the_cleanup_lock(self):
        """#93: the wait is bounded, and a lock that frees within the bound lets the start proceed."""
        path, _ = self.legacy()
        lock = self.hold_cleanup_lock()
        out = []
        thread = threading.Thread(target=lambda: out.append(self.prepare(TID2, repo="demo", branch="feature")))
        with mock.patch.object(ws, "CLEANUP_LOCK_WAIT_S", 20):
            try:
                thread.start()
                thread.join(1.5)
                self.assertTrue(thread.is_alive(), "start released the archive while cleanup held its lock")
                self.assertTrue(self.attached(path))
            finally:
                os.close(lock)
                thread.join(30)
        self.assertEqual(out[0]["freed_from_archive"], path)
        self.assertFalse(self.attached(path))

    def test_legacy_release_is_refused_when_the_cleanup_lock_stays_held(self):
        """#93: a cleanup that holds its lock past CLEANUP_LOCK_WAIT_S refuses the start with a retryable reason; the
        start never proceeds without the lock and leaves the archive exactly as it was."""
        path, commit = self.legacy()
        lock = self.hold_cleanup_lock()
        try:
            with mock.patch.object(ws, "CLEANUP_LOCK_WAIT_S", 0.5):
                t0 = time.monotonic()
                with self.assertRaises(ws.WorkspaceError) as cm:
                    self.prepare(TID2, repo="demo", branch="feature")
                waited = time.monotonic() - t0
        finally:
            os.close(lock)
        msg = str(cm.exception)
        self.assertIn("archive cleanup is running; retry in a minute", msg)
        self.assertIn("'feature'", msg)
        self.assertIn(path, msg)
        self.assertGreaterEqual(waited, 0.5)
        self.assertLess(waited, 10, "the start kept waiting after the limit")
        self.assertTrue(self.attached(path))
        self.assertEqual(self.head(path), commit)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "active", TID2)))
        # the refusal is retryable: with the lock free, the same start succeeds (and left no lock of its own behind)
        rec2 = self.prepare(TID2, repo="demo", branch="feature")
        self.assertEqual(rec2["freed_from_archive"], path)
        self.assertFalse(self.attached(path))

    def test_cleanup_lock_poll_never_takes_the_lock_after_giving_up(self):
        lock = self.hold_cleanup_lock()
        try:
            with self.assertRaises(ws.CleanupBusyError):
                with ws._cleanup_lock(self.root, timeout=0.2):
                    self.fail("entered the locked section while another holder had cleanup.lock")
        finally:
            os.close(lock)
        with ws._cleanup_lock(self.root, timeout=0.2):      # free now: acquired at once
            with self.assertRaises(ws.CleanupBusyError):    # and held by us: a second bounded taker is refused
                with ws._cleanup_lock(self.root, timeout=0.2):
                    pass

    def test_narrowed_refspec_and_force_moved_remote_is_refused(self):
        path, commit = self.legacy()
        repo_dir = os.path.join(self.root, "repos", "demo")
        git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main", cwd=repo_dir)
        main = git("rev-parse", "refs/heads/main", cwd=self.origin).strip()
        git("update-ref", "refs/heads/feature", main, cwd=self.origin)     # force-moved: commit is now unpublished
        self.assertEqual(git("rev-parse", "refs/remotes/origin/feature", cwd=repo_dir).strip(), commit)  # stale
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("is not the tip of origin/feature", str(cm.exception))
        self.assertTrue(self.attached(path))
        self.assertEqual(self.head(path), commit)

    def test_failed_fetch_refuses_release(self):
        path, _ = self.legacy()
        real_git = ws._git

        def failing_fetch(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[:2] == ["fetch", "--prune"] and "+refs/heads/*:refs/remotes/origin/*" in args:
                raise ws.WorkspaceError("simulated network failure")
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=failing_fetch):
            with self.assertRaises(ws.WorkspaceError) as cm:
                self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("not proven current", str(cm.exception))
        self.assertTrue(self.attached(path))

    def test_archive_symlinked_to_active_is_refused(self):
        rec = self.prepare(repo=self.origin, branch="feature")       # still running in active/
        archive = os.path.join(self.root, "archive")
        os.rmdir(archive)
        os.symlink(os.path.join(self.root, "active"), archive)
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("symlink", str(cm.exception))
        self.assertTrue(self.attached(rec["path"]))
        self.assertFalse(os.path.exists(os.path.join(self.root, "active", TID2)))

    def test_archive_entry_replaced_during_assessment_is_not_detached(self):
        path, _ = self.legacy()
        aside = os.path.join(self.tmp, "aside")
        real_git = ws._git

        def swap_after_status(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            out = real_git(args, cwd=cwd, timeout=timeout)
            if args[0] == "status" and cwd == path and not os.path.exists(aside):
                os.rename(path, aside)
                shutil.copytree(aside, path, symlinks=True)        # same name, same content, another directory
            return out

        with mock.patch.object(ws, "_git", side_effect=swap_after_status):
            with self.assertRaises(ws.WorkspaceError) as cm:
                self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("changed while it was being assessed", str(cm.exception))
        self.assertTrue(self.attached(path))

    def test_active_worktree_keeps_original_refusal(self):
        rec = self.prepare(repo=self.origin, branch="feature")
        with self.assertRaises(ws.WorkspaceError) as cm:
            self.prepare(TID2, repo="demo", branch="feature")
        self.assertIn("wait for that task", str(cm.exception))
        self.assertTrue(self.attached(rec["path"]))


class CleanupTests(_Base):
    def archived(self, tid, age_s, kind="folder"):
        """An archive/<tid> entry with an index record settled age_s ago."""
        if kind == "repo":
            rec = ws.settle(self.prepare(tid, repo=self.origin), self.root)
            self.assertEqual(rec["state"], "archived")
        else:
            rec = ws.settle(self.prepare(tid), self.root)
        rec["settled_at"] = time.time() - age_s
        ws.update_index(self.root, rec)
        return rec["path"]

    def test_deletes_only_old_recorded_archive_entries(self):
        week = ws.ARCHIVE_AGE_S
        old = self.archived(TID, week + 60, kind="repo")
        young = self.archived(TID2, week - 60)
        arch = os.path.join(self.root, "archive")
        unrecorded = os.path.join(arch, "t20261108120200-0000000c")
        os.mkdir(unrecorded)
        stray = os.path.join(arch, "notes")
        os.mkdir(stray)
        plain = os.path.join(arch, "t20261108120300-0000000d")
        open(plain, "w").close()                                  # a file, not a directory
        # held/, repos/, services/: never visited, however old their records say they are
        held_dir = os.path.join(self.root, "held", "t20261108120400-0000000e")
        os.mkdir(held_dir)
        ws.update_index(self.root, {"task_id": "t20261108120400-0000000e", "state": "held", "settled_at": 0, "path": held_dir})
        services = os.path.join(self.root, "services", "context-engine")
        os.makedirs(services)
        # a symlink in archive/ named like a task id, pointing at the held dir, with an old archive record: refused
        link = os.path.join(arch, "t20261108120500-0000000f")
        os.symlink(held_dir, link)
        ws.update_index(self.root, {"task_id": "t20261108120500-0000000f", "state": "archived", "settled_at": 0, "path": link})
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [TID])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(young))
        self.assertEqual(report["kept"], 1)
        self.assertTrue(os.path.isdir(unrecorded))
        self.assertTrue(os.path.isdir(stray))
        self.assertTrue(os.path.exists(plain))
        self.assertTrue(os.path.isdir(held_dir))
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isdir(services))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "repos", "demo")))
        refused = " ".join(report["refused"])
        for needle in ("no archive record", "not a task id", "not a directory", "symlink"):
            self.assertIn(needle, refused)
        self.assertEqual(report["errors"], [])
        self.assertEqual(ws.read_index(self.root)[TID]["state"], "deleted")
        self.assertNotIn(old, git("worktree", "list", "--porcelain", cwd=os.path.join(self.root, "repos", "demo")))
        # a second run finds nothing more to delete
        self.assertEqual(ws.cleanup_archive(self.root)["deleted"], [])

    def test_max_deletions_bounds_one_call(self):
        week = ws.ARCHIVE_AGE_S
        for tid in (TID, TID2, "t20261108120200-0000000c"):
            self.archived(tid, week + 60)
        report = ws.cleanup_archive(self.root, max_deletions=2)
        self.assertEqual(len(report["deleted"]), 2)
        self.assertEqual(report["kept"], 1)
        self.assertEqual(len(ws.cleanup_archive(self.root)["deleted"]), 1)

    def test_cleanup_time_budget_counts_held_reassessments(self):
        for tid in (TID, TID2):
            path = self.archived(tid, ws.ARCHIVE_AGE_S + 60)
            os.makedirs(os.path.join(path, ".git"))
        with mock.patch.object(ws.time, "monotonic", side_effect=[0, 0, 2]):
            report = ws.cleanup_archive(self.root, max_seconds=1)
        self.assertEqual(len([r for r in ws.read_index(self.root).values() if r["state"] == "held"]), 1)
        self.assertEqual(report["kept"], 2)

    def hanging_fetch_git(self):
        """A git whose `fetch` never returns (exec sleep: the subprocess timeout kills the sleep itself, so no orphan
        keeps the captured pipes open); every other command is the real git."""
        script = os.path.join(self.tmp, "git-hanging-fetch")
        with open(script, "w") as f:
            f.write('#!/bin/sh\nif [ "$1" = fetch ]; then exec sleep 60; fi\nexec git "$@"\n')
        os.chmod(script, 0o755)
        return script

    def test_hung_fetch_in_cleanup_times_out_within_the_budget_and_keeps_the_entry(self):
        """#93: the run budget is each git call's timeout too. A hung fetch ends at the budget, the entry it was
        assessing is neither deleted nor moved to held/, later entries are kept, and the run returns in time."""
        first = self.archived(TID, ws.ARCHIVE_AGE_S + 60, kind="repo")
        second = self.archived(TID2, ws.ARCHIVE_AGE_S + 60, kind="repo")
        before = {tid: ws.read_index(self.root)[tid] for tid in (TID, TID2)}
        timeouts, real_git = [], ws._git

        def recording_git(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[0] == "fetch":
                timeouts.append(timeout)
            return real_git(args, cwd=cwd, timeout=timeout)

        budget = 2
        with mock.patch.object(ws, "GIT", self.hanging_fetch_git()), \
                mock.patch.object(ws, "_git", side_effect=recording_git):
            t0 = time.monotonic()
            report = ws.cleanup_archive(self.root, max_seconds=budget)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, budget + 5, "cleanup held its lock past the budget")
        self.assertEqual(len(timeouts), 1)                   # the second entry was not even started
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], budget)            # min(FETCH_TIMEOUT_S, time left), not the 300 s cap
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["kept"], 2)
        self.assertEqual(len(report["timed_out"]), 1)
        self.assertIn(TID, report["timed_out"][0])
        self.assertIn("did not finish within", report["timed_out"][0])
        for path in (first, second):
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.exists(os.path.join(path, "README.md")))
        self.assertEqual(os.listdir(os.path.join(self.root, "held")), [])
        self.assertEqual({tid: ws.read_index(self.root)[tid] for tid in (TID, TID2)}, before)
        # with a working git the same entries are deleted by the next run
        self.assertEqual(ws.cleanup_archive(self.root)["deleted"], [TID, TID2])

    def test_no_budget_left_skips_the_git_call_instead_of_starting_it(self):
        with self.assertRaises(ws.GitTimeoutError):
            ws._within(time.monotonic() - 1, ws.FETCH_TIMEOUT_S)
        self.assertEqual(ws._within(None, 7), 7)
        self.assertLessEqual(ws._within(time.monotonic() + 1, ws.FETCH_TIMEOUT_S), 1)
        self.assertEqual(ws._within(time.monotonic() + 1000, 7), 7)

    def test_settle_holds_a_worktree_whose_assessment_timed_out(self):
        rec = self.prepare(TID, repo=self.origin)
        real_git = ws._git

        def timing_out_fetch(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[0] == "fetch":
                raise ws.GitTimeoutError("git fetch did not finish within 300 s")
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=timing_out_fetch):
            self.assertEqual(ws.pushed(rec["path"])[0], None)
            settled = ws.settle(rec, self.root)
        self.assertEqual(settled["state"], "held")          # unknown is held at settle, never archived
        self.assertIn("in time", settled["reason"])

    def test_archive_dir_itself_must_be_real(self):
        ws.ensure_root(self.root)
        arch = os.path.join(self.root, "archive")
        shutil.rmtree(arch)
        os.symlink(os.path.join(self.root, "held"), arch)
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [])
        self.assertTrue(report["errors"])

    def test_changed_archived_worktree_is_moved_to_held_before_cleanup(self):
        path = self.archived(TID, ws.ARCHIVE_AGE_S + 60, kind="repo")
        with open(os.path.join(path, "late.txt"), "w") as f:
            f.write("created after archival\n")
        report = ws.cleanup_archive(self.root)
        held = os.path.join(self.root, "held", TID)
        self.assertEqual(report["deleted"], [])
        self.assertTrue(os.path.exists(os.path.join(held, "late.txt")))
        self.assertEqual(ws.read_index(self.root)[TID]["state"], "held")

    def test_archive_ancestor_swap_cannot_delete_held_workspace(self):
        archived = self.archived(TID, ws.ARCHIVE_AGE_S + 60)
        held = os.path.join(self.root, "held", TID)
        shutil.copytree(archived, held)
        replacement = os.path.join(self.tmp, "original-archive")
        archive = os.path.join(self.root, "archive")
        real_islink = os.path.islink
        real_stat = os.stat
        entry_checks = 0
        swapped = False

        def swap_archive():
            nonlocal swapped
            if not swapped:
                swapped = True
                os.rename(archive, replacement)
                os.symlink(os.path.join(self.root, "held"), archive)

        def islink_then_swap(target):
            nonlocal entry_checks
            result = real_islink(target)
            if target == archived:
                entry_checks += 1
                if entry_checks == 2:  # old implementation: after validation, immediately before rmtree
                    swap_archive()
            return result

        def stat_then_swap(target, *args, **kwargs):
            result = real_stat(target, *args, **kwargs)
            if target == archived and kwargs.get("follow_symlinks") is False:
                swap_archive()  # descriptor implementation: after opening the entry, at its identity check
            return result

        with mock.patch.object(ws.os.path, "islink", side_effect=islink_then_swap), \
                mock.patch.object(ws.os, "stat", side_effect=stat_then_swap):
            report = ws.cleanup_archive(self.root)
        self.assertTrue(swapped)
        self.assertEqual(report["deleted"], [TID])
        self.assertTrue(os.path.isdir(held))
        self.assertFalse(os.path.islink(held))

    def unpushed_commit_in_archive(self, tid):
        """An old archived repo entry that has since gained a commit on its detached HEAD (unpublished work the
        reassessment must hold). Returns (path, sha)."""
        path = self.archived(tid, ws.ARCHIVE_AGE_S + 60, kind="repo")
        with open(os.path.join(path, "late.txt"), "w") as f:
            f.write("unpublished\n")
        git("add", ".", cwd=path)
        git("commit", "-q", "-m", "unpublished", cwd=path)
        return path, git("rev-parse", "HEAD", cwd=path).strip()

    def exhaust_budget_after_reassessment(self, budget):
        """Patches for one cleanup run: time.monotonic jumps past `budget` the moment pushed() returns, so the entry it
        assessed is handled with no budget left (the reviewer's reproduction of the held-worktree data loss)."""
        real_monotonic, real_pushed, offset = time.monotonic, ws.pushed, [0.0]

        def pushed_then_exhaust(path, deadline=None):
            result = real_pushed(path, deadline=deadline)
            offset[0] = budget + 1
            return result

        return (mock.patch.object(ws.time, "monotonic", side_effect=lambda: real_monotonic() + offset[0]),
                mock.patch.object(ws, "pushed", side_effect=pushed_then_exhaust))

    def assert_valid_held_worktree(self, held, sha, repo_dir):
        """held is a working worktree of repo_dir at sha, and sha survives an aggressive gc of the clone."""
        self.assertEqual(git("rev-parse", "HEAD", cwd=held).strip(), sha)
        git("status", "--porcelain", cwd=held)
        self.assertIn(f"worktree {held}\n", git("worktree", "list", "--porcelain", cwd=repo_dir))
        self.assertNotIn("prunable", git("worktree", "list", "--porcelain", cwd=repo_dir))
        git("reflog", "expire", "--expire=now", "--all", cwd=repo_dir)
        git("gc", "-q", "--prune=now", cwd=repo_dir)
        git("cat-file", "-e", sha + "^{commit}", cwd=repo_dir)
        self.assertEqual(git("rev-parse", "HEAD", cwd=held).strip(), sha)

    def test_held_move_with_no_budget_left_still_repairs_the_registry_so_a_later_prune_cannot_break_it(self):
        """#93 review (data loss): reassessment finds unpublished work just before the deadline. The move to held/
        must still repair the clone's registry; otherwise a later cleanup's `git worktree prune` deletes the held
        worktree's admin dir (index, detached HEAD, reflog) and its unpublished commit becomes garbage."""
        first, sha = self.unpushed_commit_in_archive(TID)
        self.archived(TID2, ws.ARCHIVE_AGE_S + 60, kind="repo")      # clean: deleted (and pruned) by the second run
        repo_dir = ws.read_index(self.root)[TID]["repo_dir"]
        clock, pushed = self.exhaust_budget_after_reassessment(budget=30)
        with clock, pushed:
            report = ws.cleanup_archive(self.root, max_seconds=30)
        held = os.path.join(self.root, "held", TID)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["kept"], 2)                            # TID held, TID2 not started (budget gone)
        self.assertEqual(ws.read_index(self.root)[TID]["state"], "held")
        self.assertTrue(os.path.isdir(held))
        self.assertFalse(os.path.exists(first))
        listing = git("worktree", "list", "--porcelain", cwd=repo_dir)
        self.assertIn(f"worktree {held}\n", listing)                 # registry repaired despite the exhausted budget
        self.assertNotIn(f"worktree {first}\n", listing)
        second = ws.cleanup_archive(self.root)                        # deletes TID2 and prunes the clone's registry
        self.assertEqual(second["deleted"], [TID2])
        self.assertEqual(second["errors"], [])
        self.assert_valid_held_worktree(held, sha, repo_dir)
        self.assertTrue(os.path.exists(os.path.join(held, "late.txt")))

    def admin_dir(self, worktree):
        with open(os.path.join(worktree, ".git")) as f:
            return f.read().split("gitdir: ", 1)[1].strip()

    def age_and_gc(self, repo_dir, admin):
        """What an auto-gc does to worktree metadata a year old: `git gc` runs `git worktree prune` with
        gc.worktreePruneExpire (default 3 months), dropping entries whose path is missing; then everything
        unreachable goes."""
        old = time.time() - 365 * 24 * 3600
        for leaf in ("index", "gitdir", "HEAD"):
            if os.path.exists(os.path.join(admin, leaf)):
                os.utime(os.path.join(admin, leaf), (old, old))
        git("reflog", "expire", "--expire=now", "--all", cwd=repo_dir)
        git("gc", "-q", "--prune=now", cwd=repo_dir)

    def clean_archive(self, tid):
        return self.archived(tid, ws.ARCHIVE_AGE_S + 60, kind="repo")

    def test_repair_that_wrote_the_registry_then_timed_out_keeps_the_entry_held(self):
        """#93 review finding 1: a repair can time out after it has written the registry. The old rollback renamed the
        worktree back to archive/ under a registry naming held/, and the next prune deleted its admin dir. Now there
        is no rollback: it stays in held/ (never deleted), the failure is recorded and reported, and it survives
        later cleanups and an aggressive gc."""
        first, sha = self.unpushed_commit_in_archive(TID)
        repo_dir = ws.read_index(self.root)[TID]["repo_dir"]
        real_git = ws._git

        def repair_then_timeout(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            out = real_git(args, cwd=cwd, timeout=timeout)
            if args[:2] == ["worktree", "repair"]:
                raise ws.GitTimeoutError("git worktree did not finish within 60 s")
            return out

        with mock.patch.object(ws, "_git", side_effect=repair_then_timeout):
            report = ws.cleanup_archive(self.root)
        held = os.path.join(self.root, "held", TID)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn(TID, report["errors"][0])
        self.assertIn("repair", report["errors"][0])
        self.assertFalse(os.path.exists(first))
        rec = ws.read_index(self.root)[TID]
        self.assertEqual((rec["state"], rec["path"]), ("held", held))
        self.assertIn("repair_error", rec)
        # a later cleanup deletes another entry of the same clone, then gc runs: the held worktree is intact
        self.clean_archive(TID2)
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [TID2])
        self.assertEqual(report["errors"], [])
        self.assertNotIn("repair_error", ws.read_index(self.root)[TID])     # registry right: the record is cleared
        self.age_and_gc(repo_dir, self.admin_dir(held))
        self.assert_valid_held_worktree(held, sha, repo_dir)

    def test_failed_repair_leaves_a_stale_entry_that_survives_and_is_repaired_later(self):
        """A held move whose repair fails outright keeps the worktree in held/ with a stale registry entry. Nothing
        prunes it: not the deletion of another entry, not gc on year-old metadata. The next cleanup repairs it."""
        first, sha = self.unpushed_commit_in_archive(TID)
        self.clean_archive(TID2)
        repo_dir = ws.read_index(self.root)[TID]["repo_dir"]
        real_git = ws._git

        def failing_repair(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[:2] == ["worktree", "repair"]:
                raise ws.WorkspaceError("git worktree failed (exit 1): simulated")
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=failing_repair):
            report = ws.cleanup_archive(self.root)
        held = os.path.join(self.root, "held", TID)
        self.assertEqual(report["deleted"], [TID2])
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("repair", report["errors"][0])
        self.assertEqual(ws.read_index(self.root)[TID]["state"], "held")
        self.assertIn("repair_error", ws.read_index(self.root)[TID])
        self.assertIn(f"worktree {first}\n", git("worktree", "list", "--porcelain", cwd=repo_dir))   # stale, kept
        self.age_and_gc(repo_dir, self.admin_dir(held))
        self.assertTrue(os.path.isdir(self.admin_dir(held)))
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["repaired"], [held])
        self.assertEqual(report["errors"], [])
        self.assertNotIn("repair_error", ws.read_index(self.root)[TID])
        self.assert_valid_held_worktree(held, sha, repo_dir)

    def test_clones_are_protected_from_gc_pruning_worktree_metadata(self):
        """#93 review finding 2: the auto-gc a fetch can start runs `git worktree prune` with gc.worktreePruneExpire
        (3 months) and drops a moved, unrepaired worktree's admin dir. Every managed clone sets it to never: on
        clone, and on prepare (before the fetch) for a clone that lacks it."""
        rec = self.prepare(TID, repo=self.origin)
        repo_dir = rec["repo_dir"]
        self.assertEqual(git("config", "--local", "--get", "gc.worktreePruneExpire", cwd=repo_dir).strip(), "never")
        git("config", "--local", "--unset", "gc.worktreePruneExpire", cwd=repo_dir)
        calls, real_git = [], ws._git

        def recording_git(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            calls.append(list(args))
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=recording_git):
            self.prepare(TID2, repo="demo")
        self.assertEqual(git("config", "--local", "--get", "gc.worktreePruneExpire", cwd=repo_dir).strip(), "never")
        fetches = [i for i, c in enumerate(calls) if c[0] == "fetch"]
        config = [i for i, c in enumerate(calls) if c[:2] == ["config", "--local"]]
        self.assertTrue(fetches and config and config[0] < fetches[0])
        # aged metadata of a worktree moved without a repair survives gc; without the setting the same gc drops it
        with open(os.path.join(rec["path"], "w.txt"), "w") as f:
            f.write("unpublished\n")
        git("add", ".", cwd=rec["path"])
        git("commit", "-q", "-m", "w", cwd=rec["path"])
        sha = git("rev-parse", "HEAD", cwd=rec["path"]).strip()
        held = os.path.join(self.root, "held", TID)
        os.rename(rec["path"], held)
        admin = self.admin_dir(held)
        self.age_and_gc(repo_dir, admin)
        self.assertTrue(os.path.isdir(admin))
        git("cat-file", "-e", sha + "^{commit}", cwd=repo_dir)
        git("config", "--local", "--unset", "gc.worktreePruneExpire", cwd=repo_dir)
        self.age_and_gc(repo_dir, admin)
        self.assertFalse(os.path.isdir(admin))           # the reproduction: default gc deletes it

    def test_cleanup_protects_every_clone_and_never_runs_worktree_prune(self):
        """No code path runs `git worktree prune` (or gc): a git wrapper logs every invocation over a full lifecycle
        (prepare, settle to archive and held, cleanup deleting one and holding one), and the source never names it."""
        log = os.path.join(self.tmp, "git.log")
        wrapper = os.path.join(self.tmp, "git-logging")
        with open(wrapper, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec git "$@"\n')
        os.chmod(wrapper, 0o755)
        with mock.patch.object(ws, "GIT", wrapper):
            self.unpushed_commit_in_archive(TID)
            self.clean_archive(TID2)
            rec = self.prepare("t20261108120200-0000000c", repo="demo")
            with open(os.path.join(rec["path"], "x.txt"), "w") as f:
                f.write("x")
            self.assertEqual(ws.settle(rec, self.root)["state"], "held")
            git("config", "--local", "--unset", "gc.worktreePruneExpire", cwd=rec["repo_dir"])
            report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [TID2])
        self.assertEqual(report["errors"], [])
        self.assertEqual(git("config", "--local", "--get", "gc.worktreePruneExpire", cwd=rec["repo_dir"]).strip(),
                         "never")
        with open(log) as f:
            lines = f.read().splitlines()
        self.assertTrue(any(line.startswith("worktree repair") for line in lines))
        self.assertFalse([line for line in lines if "worktree prune" in line or line.split()[:1] == ["gc"]])
        with open(ws.__file__) as f:
            source = f.read()
        self.assertNotIn('"worktree", "prune"', source)
        self.assertNotIn("'worktree', 'prune'", source)
        self.assertNotIn('["gc"', source)

    def test_deletion_removes_only_its_own_admin_dir(self):
        """The deleted worktree's admin dir goes; a stale entry of another worktree (moved, unrepaired) stays."""
        path = self.clean_archive(TID)
        repo_dir = ws.read_index(self.root)[TID]["repo_dir"]
        admin = self.admin_dir(path)
        other = self.prepare(TID2, repo="demo")
        other_admin = self.admin_dir(other["path"])
        with mock.patch.object(ws, "repair_registry", return_value={"repaired": [], "errors": [], "skipped": []}):
            os.rename(other["path"], os.path.join(self.tmp, "elsewhere"))           # stale: path missing
            report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [TID])
        self.assertFalse(os.path.exists(admin))
        self.assertTrue(os.path.isdir(other_admin))
        self.assertIn("prunable", git("worktree", "list", "--porcelain", cwd=repo_dir))

    def test_archive_whose_admin_dir_names_another_path_is_not_deleted(self):
        path = self.clean_archive(TID)
        admin = self.admin_dir(path)
        with open(os.path.join(admin, "gitdir"), "w") as f:
            f.write(os.path.join(self.tmp, "somewhere-else", ".git") + "\n")
        with mock.patch.object(ws, "repair_registry", return_value={"repaired": [], "errors": [], "skipped": []}):
            report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [])
        self.assertIn("left alone", report["errors"][0])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.isdir(admin))
        # the normal run repairs the entry first, then deletes it
        report = ws.cleanup_archive(self.root)
        self.assertEqual((report["repaired"], report["deleted"], report["errors"]), ([path], [TID], []))
        self.assertFalse(os.path.exists(admin))

    def test_move_between_cleanup_scan_and_deletion_cannot_lose_the_moved_worktree(self):
        """#93 review finding 3: cleanup lists the registry, a settle renames a worktree, cleanup prunes: the moved
        worktree lost its admin dir. Now nothing prunes: a bare rename (what the unlocked settle did) after the
        cleanup's listing leaves its admin dir in place, and the next run repairs the registry entry."""
        self.clean_archive(TID)
        rec = self.prepare(TID2, repo="demo")
        repo_dir = rec["repo_dir"]
        with open(os.path.join(rec["path"], "w.txt"), "w") as f:
            f.write("unpublished\n")
        git("add", ".", cwd=rec["path"])
        git("commit", "-q", "-m", "w", cwd=rec["path"])
        sha = git("rev-parse", "HEAD", cwd=rec["path"]).strip()
        held = os.path.join(self.root, "held", TID2)
        real_git, moved = ws._git, []

        def settle_move():          # what an unlocked settle did: rename, registry not yet repaired
            if not moved:
                os.rename(rec["path"], held)
                moved.append(held)

        def move_before_prune(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[:2] == ["worktree", "prune"]:
                settle_move()        # the reviewer's interleave: after the cleanup listed the registry
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=move_before_prune):
            report = ws.cleanup_archive(self.root)
        settle_move()                # no prune exists any more: the move simply lands after the run
        self.assertEqual(report["deleted"], [TID])
        self.assertTrue(os.path.isdir(self.admin_dir(held)))
        self.age_and_gc(repo_dir, self.admin_dir(held))
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["repaired"], [held])
        self.assert_valid_held_worktree(held, sha, repo_dir)

    def test_settle_waits_for_the_cleanup_lock_and_defers_when_it_stays_held(self):
        """#93 finding 3: settle()'s move takes cleanup.lock (bounded). Busy: the folder stays in active/, marked
        settle_deferred, and the next cleanup settles it with a correct registry."""
        rec = self.prepare(TID, repo=self.origin)
        with open(os.path.join(rec["path"], "w.txt"), "w") as f:
            f.write("unpublished\n")
        fd = os.open(os.path.join(self.root, "cleanup.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            t0 = time.monotonic()
            out = ws.settle(rec, self.root, lock_wait=0.3)
            self.assertLess(time.monotonic() - t0, 10)
        finally:
            os.close(fd)
        self.assertEqual(out["state"], "active")
        self.assertTrue(out["settle_deferred"])
        self.assertIn("cleanup", out["error"])
        self.assertTrue(os.path.isdir(rec["path"]))
        ws.update_index(self.root, out)
        report = ws.cleanup_archive(self.root)
        held = os.path.join(self.root, "held", TID)
        self.assertEqual(report["settled"], [TID])
        self.assertEqual(report["errors"], [])
        done = ws.read_index(self.root)[TID]
        self.assertEqual((done["state"], done["path"]), ("held", held))
        self.assertNotIn("settle_deferred", done)
        self.assertIn(f"worktree {held}\n", git("worktree", "list", "--porcelain", cwd=rec["repo_dir"]))
        self.assertTrue(os.path.exists(os.path.join(held, "w.txt")))
        # the runner's own retry with its stale record finds the settled one instead of calling it missing
        self.assertEqual(ws.settle(out, self.root)["state"], "held")

    def test_settle_with_a_failed_repair_records_it_and_cleanup_repairs_it(self):
        rec = self.prepare(TID, repo=self.origin)
        real_git = ws._git

        def failing_repair(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[:2] == ["worktree", "repair"]:
                raise ws.GitTimeoutError("git worktree did not finish within 60 s")
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=failing_repair):
            out = ws.settle(rec, self.root)
        self.assertEqual(out["state"], "archived")
        self.assertIn("repair_error", out)
        ws.update_index(self.root, out)
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["repaired"], [out["path"]])
        self.assertNotIn("repair_error", ws.read_index(self.root)[TID])
        self.assertIn(f"worktree {out['path']}\n", git("worktree", "list", "--porcelain", cwd=rec["repo_dir"]))

    def test_start_repairs_its_clone_registry_when_cleanup_is_idle(self):
        rec = self.prepare(TID, repo=self.origin)
        moved = os.path.join(self.root, "held", TID)
        os.rename(rec["path"], moved)
        self.prepare(TID2, repo="demo")
        self.assertIn(f"worktree {moved}\n", git("worktree", "list", "--porcelain", cwd=rec["repo_dir"]))

    def test_record_without_settled_time_or_wrong_state_is_kept(self):
        path = self.archived(TID, ws.ARCHIVE_AGE_S + 60)
        ws.update_index(self.root, {"task_id": TID, "state": "held", "settled_at": 0, "path": path})
        self.assertEqual(ws.cleanup_archive(self.root)["deleted"], [])
        ws.update_index(self.root, {"task_id": TID, "state": "archived", "path": path})
        self.assertEqual(ws.cleanup_archive(self.root)["deleted"], [])
        self.assertTrue(os.path.isdir(path))



TID3 = "t20261108120300-0000000d"


class RegistryOwnershipTests(_Base):
    """#93 round 3: a repair or a deletion never takes an admin dir from the worktree that validly owns it, and the
    registry prepass is bounded (deadline, order, backoff)."""
    archived = CleanupTests.archived
    admin_dir = CleanupTests.admin_dir

    def impostor(self, b_commit):
        """The reviewer's reproduction: archived A (old, clean) whose .git file is a copy of active B's. B optionally
        has an unpublished commit on its HEAD. Returns (a_path, b_path, b_admin, b_head, repo_dir)."""
        a = self.archived(TID, ws.ARCHIVE_AGE_S + 60, kind="repo")
        b = self.prepare(TID2, repo="demo")["path"]
        if b_commit:
            with open(os.path.join(b, "b.txt"), "w") as f:
                f.write("only in B\n")
            git("add", ".", cwd=b)
            git("commit", "-q", "-m", "b", cwd=b)
        shutil.copyfile(os.path.join(b, ".git"), os.path.join(a, ".git"))
        return a, b, self.admin_dir(b), git("rev-parse", "HEAD", cwd=b).strip(), ws.read_index(self.root)[TID]["repo_dir"]

    def assert_b_intact(self, b, b_admin, b_head, repo_dir):
        self.assertTrue(os.path.isdir(b_admin))
        with open(os.path.join(b_admin, "gitdir")) as f:
            self.assertEqual(os.path.normpath(f.read().strip()), os.path.join(b, ".git"))     # backlink untouched
        git("reflog", "expire", "--expire=now", "--all", cwd=repo_dir)
        git("gc", "-q", "--prune=now", cwd=repo_dir)
        git("status", "--porcelain", cwd=b)                                                   # still a valid repo
        self.assertEqual(git("rev-parse", "HEAD", cwd=b).strip(), b_head)
        git("cat-file", "-e", b_head + "^{commit}", cwd=repo_dir)
        self.assertIn(f"worktree {b}\n", git("worktree", "list", "--porcelain", cwd=repo_dir))

    def test_impostor_with_unpublished_owner_is_held_and_never_takes_the_admin_dir(self):
        a, b, b_admin, b_head, repo_dir = self.impostor(b_commit=True)
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["repaired"], [])
        conflicts = [e for e in report["errors"] if b in e and "belongs to" in e]
        self.assertTrue(conflicts, report["errors"])           # loud, naming both paths
        rec = ws.read_index(self.root)[TID]
        self.assertIn(rec["state"], ("archived", "held"))
        self.assertTrue(os.path.isdir(rec["path"]))            # A's files are kept, wherever they are
        self.assertTrue(any(rec["path"] in e for e in conflicts), (rec["path"], conflicts))
        self.assertTrue(rec.get("registry_conflict") or rec.get("repair_error"))
        self.assert_b_intact(b, b_admin, b_head, repo_dir)
        ws.cleanup_archive(self.root)                          # and again: a second run neither steals nor deletes
        self.assert_b_intact(b, b_admin, b_head, repo_dir)

    def test_impostor_of_a_clean_owner_is_not_deleted_and_the_owner_keeps_its_admin_dir(self):
        a, b, b_admin, b_head, repo_dir = self.impostor(b_commit=False)
        report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["repaired"], [])
        self.assertTrue(any("belongs to" in e and a in e and b in e for e in report["errors"]), report["errors"])
        self.assertIn("registry_conflict", ws.read_index(self.root)[TID])
        self.assertTrue(os.path.isdir(a))
        self.assert_b_intact(b, b_admin, b_head, repo_dir)

    def test_deletion_refuses_an_admin_dir_another_worktree_names_even_when_its_backlink_was_stolen(self):
        """The state a wrong repair used to leave: B's admin dir backlink rewritten to A. A's deletion must not take B's
        admin dir, because B's .git file still names it."""
        a, b, b_admin, b_head, repo_dir = self.impostor(b_commit=False)
        with open(os.path.join(b_admin, "gitdir"), "w") as f:
            f.write(os.path.join(a, ".git") + "\n")
        with mock.patch.object(ws, "repair_registry", return_value={"repaired": [], "errors": [], "skipped": []}):
            report = ws.cleanup_archive(self.root)
        self.assertEqual(report["deleted"], [])
        self.assertTrue(any("also named by" in e and b in e for e in report["errors"]), report["errors"])
        self.assertTrue(os.path.isdir(a))
        self.assertTrue(os.path.isdir(b_admin))
        with open(os.path.join(b_admin, "gitdir"), "w") as f:          # the owner fixes the backlink by hand
            f.write(os.path.join(b, ".git") + "\n")
        self.assert_b_intact(b, b_admin, b_head, repo_dir)

    def test_repair_whose_backlink_target_is_gone_still_repairs(self):
        """Only a valid owner blocks a repair: a backlink naming a missing path (an ordinary unrepaired move) is fixed."""
        rec = self.prepare(TID, repo=self.origin)
        held = os.path.join(self.root, "held", TID)
        os.rename(rec["path"], held)
        out = ws.repair_registry(self.root)
        self.assertEqual((out["repaired"], out["errors"]), ([held], []))

    def stale_held(self, rec):
        """A prepared repo worktree moved to held/ without a repair, recorded there in the index."""
        tid = rec["task_id"]
        held = os.path.join(self.root, "held", tid)
        os.makedirs(os.path.dirname(held), exist_ok=True)
        os.rename(rec["path"], held)
        ws.update_index(self.root, {**rec, "state": "held", "path": held, "settled_at": time.time()})
        return held

    def test_slow_repair_cannot_starve_deferred_settles_or_other_entries_forever(self):
        recs = [self.prepare(t, repo=self.origin) for t in (TID, TID2, TID3)]    # each start repairs: move after
        deferred = recs[2]
        with open(os.path.join(deferred["path"], "w.txt"), "w") as f:
            f.write("unpublished\n")
        slow, other = self.stale_held(recs[0]), self.stale_held(recs[1])
        ws.update_index(self.root, {**deferred, "settle_deferred": True, "error": "not settled yet"})
        budget = 30
        real_git, real_monotonic, offset, repairs = ws._git, time.monotonic, [0.0], []

        def slow_git(args, cwd=None, timeout=ws.GIT_TIMEOUT_S):
            if args[:2] == ["worktree", "repair"]:
                repairs.append(args[-1])
                if args[-1] == slow:                # hangs for its whole fixed cap, which eats the run's budget
                    offset[0] += budget + 1
                    raise ws.GitTimeoutError("git worktree did not finish within 60 s")
            return real_git(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(ws, "_git", side_effect=slow_git), \
                mock.patch.object(ws.time, "monotonic", side_effect=lambda: real_monotonic() + offset[0]):
            report = ws.cleanup_archive(self.root, max_seconds=budget)
        self.assertEqual(report["settled"], [TID3])                    # deferred settle ran before the prepass
        self.assertEqual(ws.read_index(self.root)[TID3]["state"], "held")
        self.assertEqual(repairs.count(slow), 1)                        # once per run, then the deadline
        self.assertNotIn(other, repairs)
        self.assertTrue(any(TID2 in t and "no budget" in t for t in report["timed_out"]), report["timed_out"])
        rec = ws.read_index(self.root)[TID]
        self.assertEqual(rec["repair_failures"], 1)
        self.assertGreater(rec["repair_next_at"], time.time() + ws.REPAIR_BACKOFF_BASE_S - 60)
        # next run: the failing entry is backing off, so the other one is repaired, not starved
        repairs.clear()
        offset[0] = 0.0
        with mock.patch.object(ws, "_git", side_effect=slow_git):
            report = ws.cleanup_archive(self.root, max_seconds=budget)
        self.assertEqual(repairs, [other])
        self.assertEqual(report["repaired"], [other])
        self.assertTrue(any(TID in t and "backing off" in t for t in report["timed_out"]), report["timed_out"])
        # once the backoff is over it is retried; success clears every repair field
        ws.update_index(self.root, {**ws.read_index(self.root)[TID], "repair_next_at": time.time() - 1})
        report = ws.cleanup_archive(self.root, max_seconds=budget)
        self.assertEqual(report["repaired"], [slow])
        rec = ws.read_index(self.root)[TID]
        for k in ("repair_failures", "repair_next_at", "repair_error", "registry_conflict"):
            self.assertNotIn(k, rec)

    def test_backoff_grows_and_is_capped(self):
        self.assertEqual(ws._repair_backoff(1), ws.REPAIR_BACKOFF_BASE_S)
        self.assertEqual(ws._repair_backoff(2), 2 * ws.REPAIR_BACKOFF_BASE_S)
        self.assertEqual(ws._repair_backoff(50), ws.REPAIR_BACKOFF_MAX_S)

if __name__ == "__main__":
    unittest.main()
