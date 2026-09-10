"""Tests for ``teardown --discard``: removing a worktree whose work must NOT land.

The gap this closes: ``teardown`` refused both a dirty worktree (exit
``dirty_worktree``) and an unmerged branch (exit ``branch_unmerged``). Both
guards are correct defaults -- they exist so work is never silently destroyed --
but together they left no sanctioned path for the real case of *abandoning* a
branch. Committing the dirty state to escape the first guard just moves you to
the second, so the only remaining route was hand-rolled ``git worktree remove
--force`` + ``git branch -D``, which the plugin's own skills forbid.

``--discard`` is that missing path. It is opt-in, it never bypasses the
path gate (the primary checkout stays untouchable), and it is recoverable by
construction: the branch tip is captured before deletion and any dirty tracked
state is salvaged into a dangling object first, so both survive in the object
store until gc.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import worktree_engine as engine


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _object_exists(sha: str, cwd: Path) -> bool:
    """Report whether `sha` is still present in the object store."""
    return subprocess.run(
        ["git", "cat-file", "-e", sha], cwd=cwd, capture_output=True
    ).returncode == 0


class TeardownDiscardTest(unittest.TestCase):
    """An abandoned branch can be torn down without landing it."""

    def setUp(self) -> None:
        """Build a repo whose branch is both dirty and unmerged."""
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        _git("init", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@t.test", cwd=self.repo)
        _git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "seed.txt").write_text("seed\n")
        _git("add", "seed.txt", cwd=self.repo)
        _git("commit", "-m", "seed", cwd=self.repo)

        # A branch with an unmerged commit AND uncommitted changes on top --
        # the exact state that hit both guards at once.
        self.wt = self.base / "abandoned"
        _git("worktree", "add", "-b", "abandoned", str(self.wt), cwd=self.repo)
        (self.wt / "wrong.txt").write_text("a change that must not land\n")
        _git("add", "wrong.txt", cwd=self.wt)
        _git("commit", "-m", "work that must not land", cwd=self.wt)
        self.branch_tip = _git("rev-parse", "abandoned", cwd=self.repo)
        (self.wt / "wrong.txt").write_text("dirty edit on top\n")

    def tearDown(self) -> None:
        """Remove the temp repo."""
        self._tmp.cleanup()

    def test_without_discard_both_guards_still_refuse(self):
        """The defaults must not change: dirty is still refused."""
        out = engine.cmd_teardown("abandoned", "main", str(self.repo), dry_run=False)
        self.assertEqual(out.status, "dirty_worktree")
        self.assertTrue(self.wt.exists())
        self.assertIn("abandoned", _git("branch", "--list", "abandoned", cwd=self.repo))

    def test_without_discard_unmerged_branch_still_refuses_once_clean(self):
        """Committing to escape the dirty guard must still hit the unmerged guard."""
        _git("commit", "-am", "commit the dirty edit", cwd=self.wt)
        out = engine.cmd_teardown("abandoned", "main", str(self.repo), dry_run=False)
        self.assertEqual(out.status, "branch_unmerged")
        self.assertTrue(self.wt.exists())

    def test_discard_removes_dirty_unmerged_worktree_and_branch(self):
        """The gap: --discard tears down what both guards otherwise block."""
        out = engine.cmd_teardown(
            "abandoned", "main", str(self.repo), dry_run=False, discard=True
        )
        self.assertEqual(out.status, "teardown_complete")
        self.assertFalse(self.wt.exists())
        self.assertEqual(_git("branch", "--list", "abandoned", cwd=self.repo), "")
        self.assertTrue(out.details["discarded"])

    def test_discard_never_lands_anything_on_target(self):
        """Discarding is the opposite of merging -- target must be untouched."""
        before = _git("rev-parse", "main", cwd=self.repo)
        engine.cmd_teardown("abandoned", "main", str(self.repo), dry_run=False, discard=True)
        self.assertEqual(_git("rev-parse", "main", cwd=self.repo), before)

    def test_discard_keeps_the_branch_tip_recoverable(self):
        """Committed work stays in the object store, so the discard is reversible."""
        out = engine.cmd_teardown(
            "abandoned", "main", str(self.repo), dry_run=False, discard=True
        )
        self.assertEqual(out.details["branch_tip"], self.branch_tip)
        self.assertTrue(_object_exists(self.branch_tip, self.repo))

    def test_discard_salvages_uncommitted_tracked_changes(self):
        """Dirty state is captured as a dangling object before the worktree is removed."""
        out = engine.cmd_teardown(
            "abandoned", "main", str(self.repo), dry_run=False, discard=True
        )
        salvaged = out.details.get("salvaged_stash")
        self.assertTrue(salvaged, "dirty tracked changes must be salvaged, not vaporized")
        self.assertTrue(_object_exists(salvaged, self.repo))
        self.assertIn("dirty edit on top", _git("show", f"{salvaged}:wrong.txt", cwd=self.repo))

    def test_discard_reports_what_it_destroyed(self):
        """The audit trail must record the override, not just that it succeeded."""
        out = engine.cmd_teardown(
            "abandoned", "main", str(self.repo), dry_run=False, discard=True
        )
        self.assertEqual(out.details["uncommitted"], ["wrong.txt"])
        self.assertEqual(out.details["unmerged_commits"], 1)

    def test_discard_still_refuses_the_primary_checkout(self):
        """--discard overrides the safety guards, never the path gate."""
        out = engine.cmd_teardown("main", "main", str(self.repo), dry_run=False, discard=True)
        self.assertEqual(out.status, "path_gate_failed")
        self.assertTrue((self.repo / "seed.txt").exists())

    def test_discard_dry_run_changes_nothing(self):
        """A dry run must remain a no-op even with the override set."""
        out = engine.cmd_teardown(
            "abandoned", "main", str(self.repo), dry_run=True, discard=True
        )
        self.assertEqual(out.status, "dry_run")
        self.assertTrue(self.wt.exists())
        self.assertIn("abandoned", _git("branch", "--list", "abandoned", cwd=self.repo))

    def test_branch_delete_failed_after_discard_preserves_salvaged_stash(self):
        """A branch_delete_failed partial failure must not drop the salvaged stash.

        The worktree removal (and its preceding salvage) succeeds; only the
        subsequent branch delete fails. The salvaged commit is unreferenced by
        any ref once the worktree is gone -- its SHA in ``details`` is the
        only recovery handle, so it must survive onto this failure Outcome.
        """
        real_git_rc = engine._git_rc

        def fake(args, cwd=None, env=None):
            if args[:2] == ["branch", "-D"]:
                return 1, "", "simulated branch delete failure"
            return real_git_rc(args, cwd=cwd, env=env)

        with mock.patch.object(engine, "_git_rc", side_effect=fake):
            out = engine.cmd_teardown(
                "abandoned", "main", str(self.repo), dry_run=False, discard=True
            )
        self.assertEqual(out.status, "branch_delete_failed")
        self.assertFalse(self.wt.exists())  # worktree remove ran and succeeded
        salvaged = out.details.get("salvaged_stash")
        self.assertTrue(salvaged, "salvaged_stash must survive a branch_delete_failed Outcome")
        self.assertTrue(_object_exists(salvaged, self.repo))
        self.assertEqual(out.details.get("branch_tip"), self.branch_tip)

    def test_worktree_remove_failed_after_discard_preserves_salvaged_stash(self):
        """A worktree_remove_failed partial failure must not drop the salvaged stash."""
        real_git_rc = engine._git_rc

        def fake(args, cwd=None, env=None):
            if args[:2] == ["worktree", "remove"]:
                return 1, "", "simulated worktree remove failure"
            return real_git_rc(args, cwd=cwd, env=env)

        with mock.patch.object(engine, "_git_rc", side_effect=fake):
            out = engine.cmd_teardown(
                "abandoned", "main", str(self.repo), dry_run=False, discard=True
            )
        self.assertEqual(out.status, "worktree_remove_failed")
        salvaged = out.details.get("salvaged_stash")
        self.assertTrue(salvaged, "salvaged_stash must survive a worktree_remove_failed Outcome")
        self.assertTrue(_object_exists(salvaged, self.repo))
        self.assertEqual(out.details.get("branch_tip"), self.branch_tip)


class TeardownUnlockTest(unittest.TestCase):
    """cmd_teardown unlocks a locked worktree before removing it (REQ-LOCKING-TEARDOWN)."""

    def setUp(self) -> None:
        """Build a repo with one landed, clean, LOCKED linked worktree."""
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        _git("init", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@t.test", cwd=self.repo)
        _git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "seed.txt").write_text("seed\n")
        _git("add", "seed.txt", cwd=self.repo)
        _git("commit", "-m", "seed", cwd=self.repo)
        self.wt = self.base / "locked-feat"
        _git("worktree", "add", "-b", "locked-feat", str(self.wt), cwd=self.repo)
        _git("merge", "--ff-only", "locked-feat", cwd=self.repo)  # land it
        _git("worktree", "lock", str(self.wt), "--reason", "test-lock", cwd=self.repo)

    def tearDown(self) -> None:
        """Discard the temporary repo tree."""
        self._tmp.cleanup()

    def test_teardown_unlocks_and_removes_a_locked_worktree(self) -> None:
        """Without an unlock, `git worktree remove` would fail with a lock error."""
        out = engine.cmd_teardown("locked-feat", "main", str(self.repo), dry_run=False)
        self.assertEqual(out.status, "teardown_complete")
        self.assertFalse(self.wt.exists())

    def test_unlock_of_a_never_locked_worktree_does_not_abort_teardown(self) -> None:
        """The unlock call is best-effort/fail-open: an error must not block removal."""
        _git("worktree", "unlock", str(self.wt), cwd=self.repo)  # already unlocked
        out = engine.cmd_teardown("locked-feat", "main", str(self.repo), dry_run=False)
        self.assertEqual(out.status, "teardown_complete")
        self.assertFalse(self.wt.exists())


if __name__ == "__main__":
    unittest.main()
