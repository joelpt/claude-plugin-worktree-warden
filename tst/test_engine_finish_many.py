"""Tests for the ``finish-many`` batch land command.

``cmd_finish_many`` had no direct test coverage before this file. Focused on
the "empty branch" warning: a branch that's already an ancestor of target
because it never had commits looks identical, at the git-object-graph level,
to one whose commits already landed by another path. The batch call must
surface that ambiguity rather than silently tearing down worktrees as if
"already merged" always meant "this work is done".
"""

from __future__ import annotations

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


class FinishManyRepo(unittest.TestCase):
    """A temp repo with two linked worktrees: one with real work, one empty."""

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

        self.wt_real = self.base / "wtReal"
        _git("worktree", "add", "-b", "real-work", str(self.wt_real), cwd=self.repo)
        (self.wt_real / "f.txt").write_text("real work\n")
        _git("add", "f.txt", cwd=self.wt_real)
        _git("commit", "-m", "real work", cwd=self.wt_real)

        self.wt_empty = self.base / "wtEmpty"
        _git("worktree", "add", "-b", "empty-branch", str(self.wt_empty), cwd=self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _branch_exists(self, name: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", name],
                capture_output=True,
            ).returncode
            == 0
        )


class FinishManyEmptyBranchTest(FinishManyRepo):
    def test_mixed_batch_flags_only_the_empty_branch(self) -> None:
        out = engine.cmd_finish_many(
            str(self.repo), ["real-work", "empty-branch"], "main",
            test_cmd=None, skip_tests=True, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_OK, out.message)
        self.assertEqual(out.details["landed_branches"], ["real-work"])
        self.assertEqual(out.details["already_merged_branches"], ["empty-branch"])
        self.assertEqual(out.details["empty_branches"], ["empty-branch"])
        self.assertIn("WARNING", out.message)
        self.assertIn("empty-branch", out.message)
        self.assertNotIn("real-work", out.message.split("WARNING", 1)[1])
        self.assertFalse(self.wt_real.exists())
        self.assertFalse(self.wt_empty.exists())
        self.assertFalse(self._branch_exists("real-work"))
        self.assertFalse(self._branch_exists("empty-branch"))
        self.assertIn("f.txt", _git("show", "--name-only", "--format=", "main", cwd=self.repo))

    def test_all_real_batch_has_no_warning(self) -> None:
        out = engine.cmd_finish_many(
            str(self.repo), ["real-work"], "main",
            test_cmd=None, skip_tests=True, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_OK, out.message)
        self.assertEqual(out.details["empty_branches"], [])
        self.assertNotIn("WARNING", out.message)
