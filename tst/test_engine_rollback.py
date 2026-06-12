"""Exact-state rollback tests for worktree_engine, against a real temp repo.

Verifies the restore contract: after a worktree is torn down (directory removed,
branch deleted) and the target advances, ``undo`` restores the target and branch
tips and recreates the worktree on its branch. All worktree content is committed
before the snapshot (as ``merge-worktrees`` does), so the recreated worktree is a
clean checkout that already holds every file.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import worktree_engine as engine


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class ExactStateRollbackTest(unittest.TestCase):
    """A torn-down worktree is fully reconstructed by undo."""

    def setUp(self) -> None:
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

        self.wt = self.base / "wtA"
        _git("worktree", "add", "-b", "feat", str(self.wt), cwd=self.repo)
        # merge-worktrees commits ALL content (formerly-untracked included) before
        # snapshotting, so the branch tip already captures everything.
        (self.wt / "tracked.txt").write_text("tracked work\n")
        (self.wt / "formerly_untracked.txt").write_text("scratch notes\n")
        (self.wt / "sub").mkdir()
        (self.wt / "sub" / "more.txt").write_text("nested scratch\n")
        _git("add", "tracked.txt", "formerly_untracked.txt", "sub/more.txt", cwd=self.wt)
        _git("commit", "-m", "feat work (all content)", cwd=self.wt)

        self.snap_file = self.base / "snap.json"
        self.feat_sha = _git("rev-parse", "feat", cwd=self.repo)
        self.target_sha = _git("rev-parse", "main", cwd=self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self) -> int:
        return engine.cmd_snapshot(
            str(self.repo), "main", ["feat"], str(self.snap_file)
        ).emit()

    def test_snapshot_then_teardown_then_undo_restores_everything(self) -> None:
        self.assertEqual(self._snapshot(), engine.EXIT_OK)
        self.assertTrue(self.snap_file.exists())

        _git("worktree", "remove", "--force", str(self.wt), cwd=self.repo)
        _git("branch", "-D", "feat", cwd=self.repo)
        (self.repo / "after.txt").write_text("post-snapshot main work\n")
        _git("add", "after.txt", cwd=self.repo)
        _git("commit", "-m", "advance main", cwd=self.repo)

        self.assertFalse(self.wt.exists())
        self.assertNotEqual(_git("rev-parse", "main", cwd=self.repo), self.target_sha)

        code = engine.cmd_undo(str(self.repo), str(self.snap_file)).emit()
        self.assertEqual(code, engine.EXIT_OK)

        self.assertEqual(_git("rev-parse", "main", cwd=self.repo), self.target_sha)
        self.assertEqual(_git("rev-parse", "feat", cwd=self.repo), self.feat_sha)
        self.assertTrue(self.wt.exists())
        self.assertEqual(
            _git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt), "feat"
        )
        self.assertEqual((self.wt / "tracked.txt").read_text(), "tracked work\n")
        self.assertEqual(
            (self.wt / "formerly_untracked.txt").read_text(), "scratch notes\n"
        )
        self.assertEqual(
            (self.wt / "sub" / "more.txt").read_text(), "nested scratch\n"
        )
        # All content was committed pre-snapshot, so the recreated worktree is clean.
        self.assertEqual(_git("status", "--porcelain", cwd=self.wt), "")

    def test_undo_when_worktree_still_present_resets_in_place(self) -> None:
        self.assertEqual(self._snapshot(), engine.EXIT_OK)
        (self.wt / "tracked.txt").write_text("post-snapshot drift\n")
        _git("commit", "-am", "drift", cwd=self.wt)
        self.assertNotEqual(_git("rev-parse", "feat", cwd=self.repo), self.feat_sha)

        code = engine.cmd_undo(str(self.repo), str(self.snap_file)).emit()
        self.assertEqual(code, engine.EXIT_OK)
        self.assertEqual(_git("rev-parse", "feat", cwd=self.repo), self.feat_sha)
        self.assertEqual((self.wt / "tracked.txt").read_text(), "tracked work\n")

    def test_undo_with_leftover_dir_reports_failure_not_half_restore(self) -> None:
        self.assertEqual(self._snapshot(), engine.EXIT_OK)
        _git("worktree", "remove", "--force", str(self.wt), cwd=self.repo)
        _git("branch", "-D", "feat", cwd=self.repo)
        self.wt.mkdir()
        (self.wt / "leftover.txt").write_text("stale\n")

        code = engine.cmd_undo(str(self.repo), str(self.snap_file)).emit()
        self.assertEqual(code, engine.EXIT_GIT_ERROR)
        # The branch tip is still restored even though the checkout could not be.
        self.assertEqual(_git("rev-parse", "feat", cwd=self.repo), self.feat_sha)

    def test_undo_bad_snapshot_file_is_git_error(self) -> None:
        self.snap_file.write_text("{not json")
        code = engine.cmd_undo(str(self.repo), str(self.snap_file)).emit()
        self.assertEqual(code, engine.EXIT_GIT_ERROR)

    def test_teardown_writes_audit_line_with_branch_tip(self) -> None:
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)  # land it
        tip = _git("rev-parse", "feat", cwd=self.repo)
        outcome = engine.cmd_teardown("feat", "main", str(self.repo), False)
        engine._audit(str(self.repo), "teardown", outcome)
        self.assertEqual(outcome.status, "teardown_complete")

        audit = self.repo / ".git" / "worktree-warden" / "audit.log"
        self.assertTrue(audit.exists())
        record = json.loads(audit.read_text().splitlines()[-1])
        self.assertEqual(record["action"], "teardown")
        self.assertEqual(record["branch"], "feat")
        self.assertEqual(record["details"]["branch_tip"], tip)
        self.assertTrue(record["details"]["branch_deleted"])

    def _commit_in_wt(self) -> None:
        (self.wt / "extra.txt").write_text("more\n")
        _git("add", "extra.txt", cwd=self.wt)
        _git("commit", "-m", "extra", cwd=self.wt)


if __name__ == "__main__":
    unittest.main()
