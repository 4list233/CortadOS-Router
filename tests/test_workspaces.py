"""workspaces: repo clone + worktree, archive/held classification, index.jsonl, and the archive cleanup's limits —
against a real git on a throwaway bare 'origin' in a temp dir (git is on every macOS; nothing leaves the temp dir)."""
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

    def test_record_without_settled_time_or_wrong_state_is_kept(self):
        path = self.archived(TID, ws.ARCHIVE_AGE_S + 60)
        ws.update_index(self.root, {"task_id": TID, "state": "held", "settled_at": 0, "path": path})
        self.assertEqual(ws.cleanup_archive(self.root)["deleted"], [])
        ws.update_index(self.root, {"task_id": TID, "state": "archived", "path": path})
        self.assertEqual(ws.cleanup_archive(self.root)["deleted"], [])
        self.assertTrue(os.path.isdir(path))


if __name__ == "__main__":
    unittest.main()
