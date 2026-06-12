"""Tests for the deterministic worktree-destruction gate.

Two layers: pure parser tests (no git), and verdict tests against a real temp
repo with a linked worktree exercised through clean/dirty/landed/unlanded states.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import worktree_destruction as wd


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class ParseTest(unittest.TestCase):
    """The parser recognizes destructive shapes and ignores everything else."""

    def test_worktree_remove(self) -> None:
        intents = wd.parse_destructive_command(
            "git worktree remove --force /tmp/wt", "/repo"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "worktree_remove")
        self.assertEqual(intents[0].target, "/tmp/wt")
        self.assertTrue(intents[0].forced)

    def test_branch_delete_force(self) -> None:
        intents = wd.parse_destructive_command("git branch -D feat", "/repo")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "branch_delete")
        self.assertEqual(intents[0].target, "feat")
        self.assertTrue(intents[0].forced)

    def test_rm_rf_path(self) -> None:
        intents = wd.parse_destructive_command("rm -rf /tmp/wt", "/repo")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "rm_path")
        self.assertEqual(intents[0].target, "/tmp/wt")

    def test_rm_without_recursive_is_ignored(self) -> None:
        self.assertEqual(wd.parse_destructive_command("rm /tmp/file", "/r"), [])

    def test_git_dash_c_sets_cwd(self) -> None:
        intents = wd.parse_destructive_command(
            "git -C /other worktree remove /other/wt", "/repo"
        )
        self.assertEqual(intents[0].cwd, "/other")

    def test_compound_command_finds_destructive_segment(self) -> None:
        intents = wd.parse_destructive_command(
            "cd /repo && echo hi && git branch -D feat", "/repo"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "branch_delete")

    def test_non_destructive_returns_empty(self) -> None:
        for cmd in ("git status", "ls -la", "git worktree list", "rm -f x"):
            self.assertEqual(wd.parse_destructive_command(cmd, "/r"), [])

    def test_rm_uppercase_recursive_flags(self) -> None:
        for cmd in ("rm -Rf /tmp/wt", "rm -R /tmp/wt", "rm -fR /tmp/wt"):
            intents = wd.parse_destructive_command(cmd, "/r")
            self.assertEqual(len(intents), 1, cmd)
            self.assertEqual(intents[0].kind, "rm_path")
            self.assertEqual(intents[0].target, "/tmp/wt")

    def test_branch_delete_multiple_targets(self) -> None:
        intents = wd.parse_destructive_command("git branch -D a b c", "/r")
        self.assertEqual([i.target for i in intents], ["a", "b", "c"])

    def test_worktree_remove_ignores_redirection(self) -> None:
        intents = wd.parse_destructive_command(
            "git worktree remove --force /tmp/wt > /dev/null", "/r"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].target, "/tmp/wt")

    def test_background_ampersand_separates(self) -> None:
        intents = wd.parse_destructive_command(
            "sleep 1 & git branch -D feat", "/r"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].target, "feat")

    def test_env_assignment_prefix_does_not_hide_destruction(self) -> None:
        intents = wd.parse_destructive_command(
            "FOO=bar GIT_PAGER=cat git worktree remove /tmp/wt", "/r"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "worktree_remove")

    def test_wrapper_prefixes_do_not_hide_destruction(self) -> None:
        for cmd in (
            "sudo rm -rf /tmp/wt",
            "env git worktree remove /tmp/wt",
            "time git branch -D feat",
            "command rm -rf /tmp/wt",
            "\\git branch -D feat",
        ):
            self.assertEqual(
                len(wd.parse_destructive_command(cmd, "/r")), 1, cmd
            )

    def test_rm_long_flag_without_recursive_is_ignored(self) -> None:
        # '--verbose' contains 'r' but is not recursive.
        self.assertEqual(
            wd.parse_destructive_command("rm --verbose /tmp/wt", "/r"), []
        )


class VerdictTest(unittest.TestCase):
    """End-to-end rulings against a real repo + linked worktree."""

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

    def _commit_in_wt(self) -> None:
        (self.wt / "work.txt").write_text("work\n")
        _git("add", "work.txt", cwd=self.wt)
        _git("commit", "-m", "feat work", cwd=self.wt)

    def test_block_remove_of_unlanded_branch(self) -> None:
        self._commit_in_wt()
        v = wd.evaluate_command(
            f"git worktree remove --force {self.wt}", str(self.repo)
        )
        self.assertFalse(v.allow)
        self.assertIn("not landed", v.reason)

    def test_block_rm_of_unlanded_worktree(self) -> None:
        self._commit_in_wt()
        v = wd.evaluate_command(f"rm -rf {self.wt}", str(self.repo))
        self.assertFalse(v.allow)

    def test_block_branch_delete_of_dirty_worktree(self) -> None:
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        v = wd.evaluate_command("git branch -D feat", str(self.repo))
        self.assertFalse(v.allow)
        self.assertIn("uncommitted", v.reason)

    def test_unstaged_tracked_modification_blocks_with_correct_path(self) -> None:
        # First porcelain record is " M seed.txt" (leading space). A stripping
        # reader would corrupt it to "eed.txt"; assert the real name survives.
        (self.wt / "seed.txt").write_text("modified\n")
        v = wd.evaluate_command("git branch -D feat", str(self.repo))
        self.assertFalse(v.allow)
        self.assertIn("uncommitted", v.reason)
        self.assertIn("seed.txt", v.recovery)

    def test_allow_remove_when_clean_and_landed(self) -> None:
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)  # land it
        v = wd.evaluate_command(
            f"git worktree remove {self.wt}", str(self.repo)
        )
        self.assertTrue(v.allow)

    def test_allow_remove_of_empty_clean_worktree(self) -> None:
        v = wd.evaluate_command(
            f"git worktree remove {self.wt}", str(self.repo)
        )
        self.assertTrue(v.allow)  # feat == main tip, nothing ahead, clean

    def test_noise_only_dirty_is_allowed(self) -> None:
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)
        settings = self.wt / ".claude"
        settings.mkdir()
        (settings / "settings.local.json").write_text("{}\n")
        v = wd.evaluate_command(f"git worktree remove {self.wt}", str(self.repo))
        self.assertTrue(v.allow)

    def test_rm_of_unrelated_path_is_allowed(self) -> None:
        junk = self.base / "junk"
        junk.mkdir()
        v = wd.evaluate_command(f"rm -rf {junk}", str(self.repo))
        self.assertTrue(v.allow)

    def test_branch_delete_of_landed_branch_allowed(self) -> None:
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)
        v = wd.evaluate_command("git branch -d feat", str(self.repo))
        self.assertTrue(v.allow)

    def test_exit_worktree_remove_blocked_when_unlanded(self) -> None:
        self._commit_in_wt()
        v = wd.evaluate_exit_worktree("remove", str(self.wt))
        self.assertFalse(v.allow)

    def test_exit_worktree_keep_always_allowed(self) -> None:
        self._commit_in_wt()
        v = wd.evaluate_exit_worktree("keep", str(self.wt))
        self.assertTrue(v.allow)

    def test_exit_worktree_remove_allowed_from_primary(self) -> None:
        v = wd.evaluate_exit_worktree("remove", str(self.repo))
        self.assertTrue(v.allow)  # primary checkout is not a linked worktree

    def test_rebase_landed_branch_is_allowed(self) -> None:
        # Engine lands via rebase+ff: the branch tip is rewritten, so SHA
        # ancestry would say "unlanded" — git cherry must see it as landed.
        self._commit_in_wt()
        # advance main so the branch needs a rebase to land
        (self.repo / "other.txt").write_text("other\n")
        _git("add", "other.txt", cwd=self.repo)
        _git("commit", "-m", "advance main", cwd=self.repo)
        _git("rebase", "main", cwd=self.wt)
        _git("merge", "--ff-only", "feat", cwd=self.repo)  # land rebased commits
        # feat tip is now an ancestor of main, but exercise cherry-equivalence:
        v = wd.evaluate_command(f"git worktree remove {self.wt}", str(self.repo))
        self.assertTrue(v.allow)

    def test_multiple_branch_delete_blocks_if_any_unlanded(self) -> None:
        self._commit_in_wt()  # feat is unlanded
        _git("branch", "landed-branch", "main", cwd=self.repo)  # landed (== main)
        v = wd.evaluate_command("git branch -D landed-branch feat", str(self.repo))
        self.assertFalse(v.allow)
        self.assertIn("feat", v.reason)

    def test_missing_default_branch_fails_open(self) -> None:
        # A branch compared against a non-existent target must not hard-block:
        # the landed helper fails OPEN (treats as landed) on a git error.
        self.assertTrue(wd._is_landed(str(self.repo), "feat", "nonexistent-branch"))


if __name__ == "__main__":
    unittest.main()
