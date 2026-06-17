"""Tests for the ``finish`` one-shot happy-path land command.

``finish`` collapses lock -> snapshot -> land -> test -> teardown -> release for
the common case, and on any non-trivial condition stops cleanly with a structured
code: conflict and test-failure are mid-flight bail paths (state preserved, lock
KEPT); dirty / unsafe / misconfigured land nothing (lock released). It never
auto-rolls-back -- that judgment stays with the caller.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import worktree_engine as engine
import worktree_lock as lock


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class _FinishRepo(unittest.TestCase):
    """A temp repo with one clean, landable linked worktree on branch ``feat``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.xdg = self.base / "xdg"
        self.xdg.mkdir()
        _git("init", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@t.test", cwd=self.repo)
        _git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "seed.txt").write_text("seed\n")
        _git("add", "seed.txt", cwd=self.repo)
        _git("commit", "-m", "seed", cwd=self.repo)

        self.wt = self.base / "wtA"
        _git("worktree", "add", "-b", "feat", str(self.wt), cwd=self.repo)
        (self.wt / "f.txt").write_text("feat work\n")
        _git("add", "f.txt", cwd=self.wt)
        _git("commit", "-m", "feat work", cwd=self.wt)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def main_sha(self) -> str:
        return _git("rev-parse", "main", cwd=self.repo)

    def _branch_exists(self, name: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", name],
                capture_output=True,
            ).returncode
            == 0
        )

    @property
    def _store(self) -> dict[str, dict[str, object]]:
        f = self.repo / ".git" / "worktree-warden" / "locks.json"
        return json.loads(f.read_text()) if f.exists() else {}


class FinishHappyTest(_FinishRepo):
    """Clean worktree → landed, tested, torn down — all in one call."""

    def test_clean_land_skip_tests(self) -> None:
        out = engine.cmd_finish(
            str(self.wt), "feat", "main", str(self.repo),
            test_cmd=None, skip_tests=True, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_OK, out.message)
        self.assertFalse(self.wt.exists())  # torn down
        self.assertFalse(self._branch_exists("feat"))  # branch deleted
        self.assertIn("f.txt", _git("show", "--name-only", "--format=", "main", cwd=self.repo))

    def test_clean_land_tests_pass(self) -> None:
        out = engine.cmd_finish(
            str(self.wt), "feat", "main", str(self.repo),
            test_cmd="true", skip_tests=False, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_OK, out.message)
        self.assertTrue(out.details["tested"])
        self.assertFalse(self.wt.exists())


class FinishBailTest(_FinishRepo):
    """Non-trivial conditions stop cleanly without auto-rollback."""

    def test_tests_fail_preserves_landed_state(self) -> None:
        before = self.main_sha
        out = engine.cmd_finish(
            str(self.wt), "feat", "main", str(self.repo),
            test_cmd="false", skip_tests=False, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_TESTS_FAILED)
        self.assertNotEqual(self.main_sha, before)  # landed (NOT rolled back)
        self.assertTrue(self.wt.exists())  # NOT torn down
        self.assertIn("snapshot_file", out.details)  # undo available IF caller chooses

    def test_dirty_worktree_lands_nothing(self) -> None:
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        before = self.main_sha
        out = engine.cmd_finish(
            str(self.wt), "feat", "main", str(self.repo),
            test_cmd=None, skip_tests=True, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_DIRTY_WORKTREE)
        self.assertEqual(self.main_sha, before)  # nothing landed
        self.assertTrue(self.wt.exists())

    def test_misconfigured_mutates_nothing(self) -> None:
        before = self.main_sha
        out = engine.cmd_finish(
            str(self.wt), "feat", "main", str(self.repo),
            test_cmd=None, skip_tests=False, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_GIT_ERROR)
        self.assertEqual(out.status, "finish_misconfigured")
        self.assertEqual(self.main_sha, before)
        self.assertTrue(self.wt.exists())

    def test_conflict_keeps_state_for_caller(self) -> None:
        (self.repo / "seed.txt").write_text("main change\n")
        _git("add", "seed.txt", cwd=self.repo)
        _git("commit", "-m", "main edits seed", cwd=self.repo)
        (self.wt / "seed.txt").write_text("feat change\n")
        _git("add", "seed.txt", cwd=self.wt)
        _git("commit", "-m", "feat edits seed", cwd=self.wt)
        out = engine.cmd_finish(
            str(self.wt), "feat", "main", str(self.repo),
            test_cmd="true", skip_tests=False, use_lock=False, owner="",
        )
        self.assertEqual(out.code, engine.EXIT_REBASE_CONFLICT)
        self.assertIn("snapshot_file", out.details)
        self.assertTrue(self.wt.exists())  # left in place for resolution


class FinishLockTest(_FinishRepo):
    """The lock is released on the clean path and KEPT on the bail paths."""

    def test_happy_path_releases_lock(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            out = engine.cmd_finish(
                str(self.wt), "feat", "main", str(self.repo),
                test_cmd="true", skip_tests=False, use_lock=True, owner="A",
            )
        self.assertEqual(out.code, engine.EXIT_OK, out.message)
        self.assertEqual(self._store, {})  # released

    def test_test_failure_keeps_lock(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            out = engine.cmd_finish(
                str(self.wt), "feat", "main", str(self.repo),
                test_cmd="false", skip_tests=False, use_lock=True, owner="A",
            )
        self.assertEqual(out.code, engine.EXIT_TESTS_FAILED)
        key = os.path.realpath(self.repo)
        self.assertIn(key, self._store)  # lock KEPT for the caller's follow-up
        self.assertEqual(self._store[key]["owner"], "A")

    def test_blocked_by_another_owner_lands_nothing(self) -> None:
        before = self.main_sha
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            facts = lock.main_facts(str(self.repo))
            lock.acquire(facts, "OTHER", "merge", "their merge", time.time())
            out = engine.cmd_finish(
                str(self.wt), "feat", "main", str(self.repo),
                test_cmd="true", skip_tests=False, use_lock=True, owner="A",
            )
        self.assertEqual(out.code, engine.EXIT_LOCK_BLOCKED)
        self.assertIn("OTHER", out.message)
        self.assertEqual(self.main_sha, before)  # nothing landed
        self.assertTrue(self.wt.exists())


if __name__ == "__main__":
    unittest.main()
