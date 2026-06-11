"""Tests for worktree_engine preflight subcommand.

Verifies: target resolution (symbolic-ref → leaf → 'main' fallback), clean/dirty
classification per worktree, branches without a linked worktree, and the CLI
JSON-output contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import worktree_engine as engine


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class PreflightTest(unittest.TestCase):
    """cmd_preflight resolves TARGET and reports clean/dirty status per worktree."""

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

        self.wt_clean = self.base / "wt-clean"
        _git("worktree", "add", "-b", "feat-clean", str(self.wt_clean), cwd=self.repo)

        self.wt_dirty = self.base / "wt-dirty"
        _git("worktree", "add", "-b", "feat-dirty", str(self.wt_dirty), cwd=self.repo)
        (self.wt_dirty / "work.txt").write_text("uncommitted\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolves_target_fallback_to_main(self) -> None:
        """Without origin/HEAD, target defaults to 'main'."""
        out = engine.cmd_preflight(str(self.repo), ["feat-clean"])
        self.assertEqual(out.code, engine.EXIT_OK)
        self.assertEqual(out.details["target"], "main")

    def test_clean_worktree_reported_clean(self) -> None:
        """A worktree with no modifications is marked clean with no dirty_files."""
        out = engine.cmd_preflight(str(self.repo), ["feat-clean"])
        self.assertEqual(out.code, engine.EXIT_OK)
        worktrees = out.details["worktrees"]
        assert isinstance(worktrees, list)
        self.assertEqual(len(worktrees), 1)
        entry = worktrees[0]
        assert isinstance(entry, dict)
        self.assertEqual(entry["branch"], "feat-clean")
        self.assertTrue(entry["clean"])
        self.assertEqual(entry["dirty_files"], [])

    def test_dirty_worktree_reported_dirty(self) -> None:
        """An untracked file in a worktree marks it dirty and lists the path."""
        out = engine.cmd_preflight(str(self.repo), ["feat-dirty"])
        worktrees = out.details["worktrees"]
        assert isinstance(worktrees, list)
        entry = worktrees[0]
        assert isinstance(entry, dict)
        self.assertEqual(entry["branch"], "feat-dirty")
        self.assertFalse(entry["clean"])
        self.assertIn("work.txt", entry["dirty_files"])

    def test_multiple_branches_all_reported(self) -> None:
        """Both clean and dirty branches appear in the same preflight result."""
        out = engine.cmd_preflight(str(self.repo), ["feat-clean", "feat-dirty"])
        self.assertEqual(out.code, engine.EXIT_OK)
        worktrees = out.details["worktrees"]
        assert isinstance(worktrees, list)
        self.assertEqual(len(worktrees), 2)
        by_branch = {e["branch"]: e for e in worktrees}  # type: ignore[index]
        self.assertTrue(by_branch["feat-clean"]["clean"])
        self.assertFalse(by_branch["feat-dirty"]["clean"])

    def test_target_resolved_from_origin_head(self) -> None:
        """When refs/remotes/origin/HEAD is set, target is the full branch name after the prefix."""
        _git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/trunk",
            cwd=self.repo,
        )
        out = engine.cmd_preflight(str(self.repo), ["feat-clean"])
        self.assertEqual(out.details["target"], "trunk")

    def test_target_preserves_slash_in_branch_name(self) -> None:
        """A slash-containing default branch like 'release/2.0' is not truncated to '2.0'."""
        _git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/release/2.0",
            cwd=self.repo,
        )
        out = engine.cmd_preflight(str(self.repo), ["feat-clean"])
        self.assertEqual(out.details["target"], "release/2.0")

    def test_stale_worktree_returns_error_outcome(self) -> None:
        """A registered worktree whose directory was deleted returns EXIT_GIT_ERROR, not a traceback."""
        import shutil
        shutil.rmtree(str(self.wt_clean))
        out = engine.cmd_preflight(str(self.repo), ["feat-clean"])
        self.assertEqual(out.code, engine.EXIT_GIT_ERROR)
        self.assertIn("inaccessible", out.message)

    def test_branch_without_worktree_is_clean_with_no_path(self) -> None:
        """A branch that has no linked worktree appears with path=None, clean=True."""
        _git("branch", "orphan-branch", cwd=self.repo)
        out = engine.cmd_preflight(str(self.repo), ["orphan-branch"])
        worktrees = out.details["worktrees"]
        assert isinstance(worktrees, list)
        entry = worktrees[0]
        assert isinstance(entry, dict)
        self.assertEqual(entry["branch"], "orphan-branch")
        self.assertIsNone(entry["path"])
        self.assertTrue(entry["clean"])
        self.assertEqual(entry["dirty_files"], [])

    def test_details_carries_target_key(self) -> None:
        """details['target'] matches the top-level Outcome.target field."""
        out = engine.cmd_preflight(str(self.repo), ["feat-clean"])
        self.assertEqual(out.details["target"], out.target)

    def test_cli_outputs_valid_json(self) -> None:
        """CLI subcommand exits 0 and emits valid JSON with the expected shape."""
        engine_path = Path(__file__).resolve().parent.parent / "scripts" / "worktree_engine.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(engine_path),
                "--repo",
                str(self.repo),
                "preflight",
                "--branches",
                "feat-clean,feat-dirty",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("worktrees", data["details"])
        self.assertEqual(len(data["details"]["worktrees"]), 2)
        self.assertIn("target", data["details"])
