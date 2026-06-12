"""Tests for the engine ``recover`` subcommand.

Covers detection of stranded (prunable) worktrees whose branch holds unlanded
commits, plus WIP-bundle listing and age-based gc.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast

import worktree_engine as engine


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class RecoverTest(unittest.TestCase):
    """A stranded worktree with unlanded work is surfaced, not buried."""

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
        (self.wt / "work.txt").write_text("unlanded work\n")
        _git("add", "work.txt", cwd=self.wt)
        _git("commit", "-m", "feat work", cwd=self.wt)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stranded_unlanded_worktree_is_surfaced(self) -> None:
        # Remove the directory out from under git (the ffff scenario) -> prunable.
        shutil.rmtree(self.wt)
        outcome = engine.cmd_recover(str(self.repo), "main", None)
        self.assertEqual(outcome.code, engine.EXIT_OK)
        stranded = cast(list[dict[str, object]], outcome.details["stranded"])
        self.assertEqual(len(stranded), 1)
        entry = stranded[0]
        self.assertEqual(entry["branch"], "feat")
        self.assertTrue(entry["unlanded"])
        self.assertIn("worktree add", str(entry["recovery"]))
        self.assertIn("feat", str(entry["recovery"]))

    def test_emitted_recovery_command_actually_restores(self) -> None:
        # The recovery string must work verbatim — a prunable worktree is still
        # registered, so it needs `prune &&` before `worktree add`.
        shutil.rmtree(self.wt)
        outcome = engine.cmd_recover(str(self.repo), "main", None)
        recovery = cast(
            list[dict[str, object]], outcome.details["stranded"]
        )[0]["recovery"]
        result = subprocess.run(
            str(recovery), shell=True, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.wt / "work.txt").exists())
        self.assertEqual(
            _git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt), "feat"
        )

    def test_no_stranded_when_all_present(self) -> None:
        outcome = engine.cmd_recover(str(self.repo), "main", None)
        self.assertEqual(
            cast(list[object], outcome.details["stranded"]), []
        )

    def test_gc_removes_old_bundles_only(self) -> None:
        wip = Path(engine._git_common_dir(str(self.repo))) / "worktree-warden" / "wip"
        wip.mkdir(parents=True)
        old = wip / "old.bundle"
        new = wip / "new.bundle"
        old.write_text("x")
        new.write_text("y")
        old_time = time.time() - 10 * 86400
        os.utime(old, (old_time, old_time))

        outcome = engine.cmd_recover(str(self.repo), "main", 7.0)
        self.assertEqual(
            cast(list[str], outcome.details["gc_removed"]), [str(old)]
        )
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
