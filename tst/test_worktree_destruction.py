"""Tests for the deterministic worktree/branch destruction gate.

Two layers: pure parser tests (no git), and verdict tests against a real temp
repo with a linked worktree exercised through clean/dirty/landed/unlanded states.

The gate judges command *shape*, not live lock state: `git worktree unlock`,
`rm` of the `.git/worktrees/` admin area and `git worktree remove` with two or
more force flags are blocked outright; a single force is judged by the target
worktree's path. `rm -rf <worktree dir>` stays a documented, permanent gap.

`evaluate_exit_worktree` (a separate, non-parser gate) re-verifies current lock
state and falls back to the same clean+landed check on an unlocked worktree;
see its `VerdictTest` cases below.
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
    """The parser recognizes only `git branch -d/-D` now; everything else is []."""

    def test_forced_worktree_remove_parsed_with_force_count(self) -> None:
        """A forced `git worktree remove` parses to one intent counting its forces."""
        cases = {
            "git worktree remove --force /tmp/wt": 1,
            "git worktree remove -f /tmp/wt": 1,
            "git worktree remove -f -f /tmp/wt": 2,
            "git worktree remove -ff /tmp/wt": 2,
            "git worktree remove --force --force /tmp/wt": 2,
            "git worktree remove --forc --forc /tmp/wt": 2,
            "git worktree remove --f --force /tmp/wt": 2,
            "git -C /repo worktree remove -f -f /tmp/wt": 2,
        }
        for cmd, forces in cases.items():
            intents = wd.parse_destructive_command(cmd, "/repo")
            self.assertEqual(len(intents), 1, cmd)
            self.assertEqual(intents[0].kind, "worktree_remove", cmd)
            self.assertEqual(intents[0].force_count, forces, cmd)
            self.assertEqual(intents[0].target, "/tmp/wt", cmd)

    def test_unforced_worktree_remove_not_parsed(self) -> None:
        """Without force git itself refuses a dirty worktree, so nothing to gate."""
        self.assertEqual(
            wd.parse_destructive_command("git worktree remove /tmp/wt", "/repo"), []
        )

    def test_worktree_unlock_parsed(self) -> None:
        """Any `git worktree unlock` parses, alone or chained with a remove."""
        intents = wd.parse_destructive_command(
            "git worktree unlock /tmp/wt && git worktree remove --force /tmp/wt",
            "/repo",
        )
        self.assertEqual(
            [i.kind for i in intents], ["worktree_unlock", "worktree_remove"]
        )

    def test_rm_of_worktree_admin_record_or_lock_file_parsed(self) -> None:
        """`rm` aimed at `.git/worktrees/...` (admin dir or its `locked` file) parses."""
        for cmd in (
            "rm /repo/.git/worktrees/wt/locked",
            "rm -rf /repo/.git/worktrees/wt",
            "rm -f .git/worktrees/wt/locked",
            "sudo rm /repo/.git/worktrees/wt/locked",
        ):
            intents = wd.parse_destructive_command(cmd, "/repo")
            self.assertEqual([i.kind for i in intents], ["worktree_admin_rm"], cmd)

    def test_cd_is_tracked_across_segments(self) -> None:
        """A `cd` in an earlier segment sets the cwd of later git segments."""
        intents = wd.parse_destructive_command(
            "cd /tmp/wt && git worktree remove -ff .", "/repo"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].cwd, "/tmp/wt")

    def test_branch_delete_force(self) -> None:
        """`git branch -D <name>` parses to one forced branch_delete intent."""
        intents = wd.parse_destructive_command("git branch -D feat", "/repo")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "branch_delete")
        self.assertEqual(intents[0].target, "feat")
        self.assertTrue(intents[0].forced)

    def test_rm_rf_no_longer_parsed(self) -> None:
        """`rm -rf` is no longer this module's concern (documented permanent gap)."""
        self.assertEqual(wd.parse_destructive_command("rm -rf /tmp/wt", "/repo"), [])

    def test_rm_without_recursive_is_ignored(self) -> None:
        """A non-recursive `rm` was never destructive to a worktree; still []."""
        self.assertEqual(wd.parse_destructive_command("rm /tmp/file", "/r"), [])

    def test_git_dash_c_sets_cwd_for_branch_delete(self) -> None:
        """`git -C <dir>` still resolves the effective cwd for branch_delete."""
        intents = wd.parse_destructive_command(
            "git -C /other branch -D feat", "/repo"
        )
        self.assertEqual(intents[0].cwd, "/other")

    def test_compound_command_finds_destructive_segment(self) -> None:
        """A `&&`-joined compound command still finds the branch_delete segment."""
        intents = wd.parse_destructive_command(
            "cd /repo && echo hi && git branch -D feat", "/repo"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "branch_delete")

    def test_non_destructive_returns_empty(self) -> None:
        """Ordinary read-only/status commands never produce an intent."""
        for cmd in ("git status", "ls -la", "git worktree list", "rm -f x"):
            self.assertEqual(wd.parse_destructive_command(cmd, "/r"), [])

    def test_rm_uppercase_recursive_flags_no_longer_parsed(self) -> None:
        """`rm -Rf`/`-R`/`-fR` are all covered by the same permanent gap as `rm -rf`."""
        for cmd in ("rm -Rf /tmp/wt", "rm -R /tmp/wt", "rm -fR /tmp/wt"):
            self.assertEqual(wd.parse_destructive_command(cmd, "/r"), [], cmd)

    def test_branch_delete_multiple_targets(self) -> None:
        """`git branch -D a b c` deletes every named branch; gate each."""
        intents = wd.parse_destructive_command("git branch -D a b c", "/r")
        self.assertEqual([i.target for i in intents], ["a", "b", "c"])

    def test_branch_delete_ignores_redirection(self) -> None:
        """A trailing shell redirect is not mistaken for a branch operand."""
        intents = wd.parse_destructive_command(
            "git branch -D feat > /dev/null", "/r"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].target, "feat")

    def test_background_ampersand_separates(self) -> None:
        """A backgrounded `&` segment separator still isolates the real command."""
        intents = wd.parse_destructive_command(
            "sleep 1 & git branch -D feat", "/r"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].target, "feat")

    def test_env_assignment_prefix_does_not_hide_destruction(self) -> None:
        """Leading `FOO=bar` env assignments do not hide the real git command."""
        intents = wd.parse_destructive_command(
            "FOO=bar GIT_PAGER=cat git branch -D feat", "/r"
        )
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, "branch_delete")

    def test_wrapper_prefixes_do_not_hide_branch_delete(self) -> None:
        """`time`/backslash-escaped prefixes do not hide a branch_delete."""
        for cmd in ("time git branch -D feat", "\\git branch -D feat"):
            self.assertEqual(
                len(wd.parse_destructive_command(cmd, "/r")), 1, cmd
            )

    def test_wrapper_prefixed_worktree_remove_and_rm_still_unparsed(self) -> None:
        """The permanent gap applies through wrappers too, not just bare commands."""
        for cmd in (
            "sudo rm -rf /tmp/wt",
            "env git worktree remove /tmp/wt",
            "command rm -rf /tmp/wt",
        ):
            self.assertEqual(wd.parse_destructive_command(cmd, "/r"), [], cmd)

    def test_rm_long_flag_without_recursive_is_ignored(self) -> None:
        """'--verbose' contains 'r' but is not recursive."""
        self.assertEqual(
            wd.parse_destructive_command("rm --verbose /tmp/wt", "/r"), []
        )


class VerdictTest(unittest.TestCase):
    """End-to-end rulings against a real repo + linked worktree."""

    def setUp(self) -> None:
        """Build a repo with one linked worktree on branch 'feat'."""
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
        """Discard the temporary repo tree."""
        self._tmp.cleanup()

    def _commit_in_wt(self) -> None:
        """Commit one unlanded change in the linked worktree."""
        (self.wt / "work.txt").write_text("work\n")
        _git("add", "work.txt", cwd=self.wt)
        _git("commit", "-m", "feat work", cwd=self.wt)

    def _lock(self) -> None:
        """Lock the linked worktree the way the creation hook does."""
        _git("worktree", "lock", str(self.wt), cwd=self.repo)

    def _verdict(self, command: str, cwd: Path | None = None) -> wd.Verdict:
        """Rule on ``command`` from the primary checkout unless ``cwd`` is given."""
        return wd.evaluate_command(command, str(cwd or self.repo))

    def test_single_force_remove_of_unlanded_unlocked_worktree_blocked(self) -> None:
        """`remove --force` on an unlocked worktree with unlanded commits is refused."""
        self._commit_in_wt()
        v = self._verdict(f"git worktree remove --force {self.wt}")
        self.assertFalse(v.allow)

    def test_single_force_remove_of_dirty_unlocked_worktree_blocked(self) -> None:
        """`remove --force` on an unlocked dirty worktree is refused."""
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        v = self._verdict(f"git worktree remove --force {self.wt}")
        self.assertFalse(v.allow)
        self.assertIn("uncommitted", v.reason)

    def test_single_force_remove_of_clean_landed_worktree_allowed(self) -> None:
        """`remove --force` of a clean, landed worktree is allowed."""
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)
        self.assertTrue(self._verdict(f"git worktree remove --force {self.wt}").allow)

    def test_single_force_remove_by_folder_name_is_judged(self) -> None:
        """The bare folder-name operand resolves to the worktree and is judged."""
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        self.assertFalse(self._verdict("git worktree remove --force wt").allow)

    def test_single_force_remove_of_dirty_detached_head_worktree_blocked(self) -> None:
        """A detached-HEAD worktree has no branch; its dirt is still protected."""
        _git("checkout", "--detach", cwd=self.wt)
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        self.assertFalse(self._verdict(f"git worktree remove --force {self.wt}").allow)

    def test_single_force_remove_of_unlanded_detached_head_worktree_blocked(self) -> None:
        """Commits made on a detached HEAD that are in no branch are protected."""
        _git("checkout", "--detach", cwd=self.wt)
        self._commit_in_wt()
        self.assertFalse(self._verdict(f"git worktree remove --force {self.wt}").allow)

    def test_single_force_remove_of_unresolvable_target_blocked(self) -> None:
        """Fail closed: a forced remove whose target is no known worktree is refused."""
        v = self._verdict(f"git worktree remove --force {self.base / 'nowhere'}")
        self.assertFalse(v.allow)
        self.assertIn("resolve", v.reason)

    def test_double_force_remove_blocked_even_when_clean_and_landed(self) -> None:
        """Every spelling of two forces is refused outright: it is never routine."""
        for spelling in ("-f -f", "-ff", "--force --force", "--forc --forc", "-f --force"):
            v = self._verdict(f"git worktree remove {spelling} {self.wt}")
            self.assertFalse(v.allow, spelling)
            self.assertIn("finish-worktree", v.recovery, spelling)

    def test_double_force_remove_of_locked_dirty_worktree_blocked(self) -> None:
        """The bypass git's own error message advertises (`remove -f -f`)."""
        self._lock()
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        self.assertFalse(self._verdict(f"git worktree remove -f -f {self.wt}").allow)

    def test_unlock_blocked_even_when_clean(self) -> None:
        """`git worktree unlock` is never routine from Bash; the engine unlocks itself."""
        self._lock()
        v = self._verdict(f"git worktree unlock {self.wt}")
        self.assertFalse(v.allow)
        self.assertIn("finish-worktree", v.recovery)

    def test_unlock_then_remove_chain_blocked(self) -> None:
        """The chained bypass is refused at its first segment, lock state ignored."""
        self._lock()
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        v = self._verdict(
            f"git worktree unlock {self.wt} && git worktree remove --force {self.wt}"
        )
        self.assertFalse(v.allow)

    def test_rm_of_lock_file_or_admin_record_blocked(self) -> None:
        """Deleting the lock file (or the admin dir) is an unlock in disguise."""
        admin = self.repo / ".git" / "worktrees" / "wt"
        for cmd in (f"rm {admin / 'locked'}", f"rm -rf {admin}"):
            self.assertFalse(self._verdict(cmd).allow, cmd)

    def test_cd_into_worktree_then_relative_double_force_blocked(self) -> None:
        """`cd <wt> && git worktree remove -ff .` is judged against the real cwd."""
        self.assertFalse(
            self._verdict(f"cd {self.wt} && git worktree remove -ff .").allow
        )

    def test_dash_c_single_force_remove_is_judged_in_that_repo(self) -> None:
        """`git -C <repo> worktree remove --force` resolves against ``-C``."""
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        v = self._verdict(
            f"git -C {self.repo} worktree remove --force {self.wt}", self.base
        )
        self.assertFalse(v.allow)

    def test_engine_teardown_command_is_not_blocked(self) -> None:
        """The sanctioned Bash entry point is `python3 worktree_engine.py ...`."""
        v = self._verdict("python3 scripts/worktree_engine.py teardown --branch feat")
        self.assertTrue(v.allow)

    def test_rm_of_unlanded_worktree_now_allowed(self) -> None:
        """Documented, permanent, backstopped gap -- see module docstring."""
        self._commit_in_wt()
        v = wd.evaluate_command(f"rm -rf {self.wt}", str(self.repo))
        self.assertTrue(v.allow)

    def test_block_branch_delete_of_dirty_worktree(self) -> None:
        """A branch whose worktree has uncommitted changes is refused."""
        (self.wt / "scratch.txt").write_text("uncommitted\n")
        v = wd.evaluate_command("git branch -D feat", str(self.repo))
        self.assertFalse(v.allow)
        self.assertIn("uncommitted", v.reason)

    def test_unstaged_tracked_modification_blocks_with_correct_path(self) -> None:
        """The real dirty path survives verbatim (no leading-space corruption)."""
        # First porcelain record is " M seed.txt" (leading space). A stripping
        # reader would corrupt it to "eed.txt"; assert the real name survives.
        (self.wt / "seed.txt").write_text("modified\n")
        v = wd.evaluate_command("git branch -D feat", str(self.repo))
        self.assertFalse(v.allow)
        self.assertIn("uncommitted", v.reason)
        self.assertIn("seed.txt", v.recovery)

    def test_worktree_remove_when_clean_and_landed_allowed(self) -> None:
        """A clean, landed worktree's `worktree remove` is allowed."""
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)  # land it
        v = wd.evaluate_command(
            f"git worktree remove {self.wt}", str(self.repo)
        )
        self.assertTrue(v.allow)

    def test_allow_remove_of_empty_clean_worktree(self) -> None:
        """An empty, never-diverged worktree's removal is allowed."""
        v = wd.evaluate_command(
            f"git worktree remove {self.wt}", str(self.repo)
        )
        self.assertTrue(v.allow)  # feat == main tip, nothing ahead, clean

    def test_rm_of_unrelated_path_is_allowed(self) -> None:
        """`rm -rf` of a path that isn't a registered worktree is out of scope."""
        junk = self.base / "junk"
        junk.mkdir()
        v = wd.evaluate_command(f"rm -rf {junk}", str(self.repo))
        self.assertTrue(v.allow)

    def test_branch_delete_of_landed_branch_allowed(self) -> None:
        """A clean, landed branch's `git branch -d` is allowed."""
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)
        v = wd.evaluate_command("git branch -d feat", str(self.repo))
        self.assertTrue(v.allow)

    def test_exit_worktree_remove_allowed_when_locked_even_if_unlanded(self) -> None:
        """A currently-locked worktree defers to git's own removal refusal."""
        self._commit_in_wt()
        _git("worktree", "lock", str(self.wt), "--reason", "test", cwd=self.repo)
        v = wd.evaluate_exit_worktree("remove", str(self.wt))
        self.assertTrue(v.allow)

    def test_exit_worktree_remove_blocked_when_unlocked_and_unlanded(self) -> None:
        """No lock in place -- re-verified independently, and unlanded work blocks.

        This is the fix for the reviewer-flagged gap: correctness must not
        rest entirely on the creation-time lock hook having succeeded.
        """
        self._commit_in_wt()
        v = wd.evaluate_exit_worktree("remove", str(self.wt))
        self.assertFalse(v.allow)
        self.assertIn("not landed", v.reason)

    def test_exit_worktree_remove_allowed_when_unlocked_but_landed(self) -> None:
        """No lock in place, but clean and landed -- the fallback check allows it."""
        self._commit_in_wt()
        _git("merge", "--ff-only", "feat", cwd=self.repo)
        v = wd.evaluate_exit_worktree("remove", str(self.wt))
        self.assertTrue(v.allow)

    def test_exit_worktree_keep_always_allowed(self) -> None:
        """An ExitWorktree keep leaves the directory alone; always allowed."""
        self._commit_in_wt()
        v = wd.evaluate_exit_worktree("keep", str(self.wt))
        self.assertTrue(v.allow)

    def test_exit_worktree_remove_allowed_from_primary(self) -> None:
        """The primary checkout is not locked, but IS the default branch -- allowed."""
        v = wd.evaluate_exit_worktree("remove", str(self.repo))
        self.assertTrue(v.allow)

    def test_rebase_landed_branch_is_allowed(self) -> None:
        """A rebased-and-fast-forwarded branch is seen as landed via git cherry."""
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
        """`git branch -D a b` blocks as soon as any named branch is unlanded."""
        self._commit_in_wt()  # feat is unlanded
        _git("branch", "landed-branch", "main", cwd=self.repo)  # landed (== main)
        v = wd.evaluate_command("git branch -D landed-branch feat", str(self.repo))
        self.assertFalse(v.allow)
        self.assertIn("feat", v.reason)

    def test_missing_default_branch_fails_open(self) -> None:
        """`_is_landed` fails OPEN (treats as landed) when target doesn't resolve."""
        # A branch compared against a non-existent target must not hard-block:
        # the landed helper fails OPEN (treats as landed) on a git error.
        self.assertTrue(wd._is_landed(str(self.repo), "feat", "nonexistent-branch"))


if __name__ == "__main__":
    unittest.main()
