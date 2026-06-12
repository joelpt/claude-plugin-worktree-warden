"""Tests for non-destructive WIP capture into bundles."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import worktree_wip as wip


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class CaptureWipTest(unittest.TestCase):
    """capture_wip snapshots tracked + untracked work without touching state."""

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
        self.wt = self.base / "wt"
        _git("worktree", "add", "-b", "feat", str(self.wt), cwd=self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_worktree_captures_nothing(self) -> None:
        self.assertIsNone(wip.capture_wip(str(self.wt)))

    def test_captures_tracked_and_untracked_without_touching_worktree(self) -> None:
        (self.wt / "seed.txt").write_text("modified\n")  # tracked change
        (self.wt / "scratch.txt").write_text("untracked notes\n")  # untracked
        status_before = _git("status", "--porcelain", cwd=self.wt)

        bundle = wip.capture_wip(str(self.wt))
        self.assertIsNotNone(bundle)
        assert bundle is not None
        self.assertTrue(Path(bundle).exists())

        # The capture must not have touched the working tree or staged anything.
        self.assertEqual(_git("status", "--porcelain", cwd=self.wt), status_before)
        # No staging ref left behind.
        refs = _git("for-each-ref", "refs/worktree-warden", cwd=self.repo)
        self.assertEqual(refs, "")

        # The bundle's commit (objects remain in the odb post-capture) contains
        # both the modification and the new file. list-heads prints "<sha> <ref>";
        # the ref was dropped from the repo, so inspect by SHA.
        heads = _git("bundle", "list-heads", bundle, cwd=self.repo)
        self.assertTrue(heads)
        sha = heads.split()[0]
        files = _git("ls-tree", "-r", "--name-only", sha, cwd=self.repo)
        self.assertIn("scratch.txt", files)
        blob = _git("show", f"{sha}:seed.txt", cwd=self.repo)
        self.assertEqual(blob, "modified")

    def test_bundle_invisible_to_normal_history(self) -> None:
        (self.wt / "scratch.txt").write_text("x\n")
        self.assertIsNotNone(wip.capture_wip(str(self.wt)))
        # The captured commit must not appear as a branch or in log --all.
        self.assertNotIn("warden wip", _git("log", "--all", "--oneline", cwd=self.repo))
        self.assertNotIn("warden", _git("branch", "-a", cwd=self.repo))


if __name__ == "__main__":
    unittest.main()
