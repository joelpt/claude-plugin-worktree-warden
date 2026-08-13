"""Tests for cross-session worktree-population tracking + external-removal detection."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import worktree_population as wp


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _now_ts() -> str:
    """Return the current UTC time in the format `_audit()` writes to audit records."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PopulationTest(unittest.TestCase):
    """reconcile() flags content-bearing disappearances warden did not cause."""

    def setUp(self) -> None:
        """Build a primary repo with one dirty-then-committed linked worktree."""
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
        (self.wt / "work.txt").write_text("unlanded\n")
        _git("add", "work.txt", cwd=self.wt)
        _git("commit", "-m", "feat work", cwd=self.wt)

    def tearDown(self) -> None:
        """Discard the temporary repo tree."""
        self._tmp.cleanup()

    def _rm_admin_and_dir(self) -> None:
        # Simulate an external removal: directory gone AND admin record pruned,
        # so it is fully absent from `git worktree list` (recover can't see it).
        shutil.rmtree(self.wt)
        _git("worktree", "prune", cwd=self.repo)

    def test_first_run_records_no_removal(self) -> None:
        """A never-before-seen repo has nothing to diff against."""
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_external_removal_of_unlanded_worktree_is_flagged(self) -> None:
        """A worktree with unlanded commits that vanishes unaccounted is flagged."""
        wp.reconcile(str(self.repo))  # record baseline
        self._rm_admin_and_dir()
        removals = wp.reconcile(str(self.repo))
        self.assertEqual(len(removals), 1)
        r = removals[0]
        self.assertEqual(r["branch"], "feat")
        self.assertTrue(r["unlanded"])
        self.assertTrue(r["branch_alive"])  # branch ref survives the dir removal
        self.assertIn("worktree add", r["recovery"])

    def test_landed_clean_removal_is_not_flagged(self) -> None:
        """A clean, fully-landed worktree's disappearance loses nothing."""
        _git("merge", "--ff-only", "feat", cwd=self.repo)  # land it
        wp.reconcile(str(self.repo))  # baseline (now clean + landed)
        self._rm_admin_and_dir()
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_warden_teardown_is_not_flagged(self) -> None:
        """A `teardown` audit record attributes the removal to warden itself."""
        wp.reconcile(str(self.repo))  # baseline
        # Write an audit line attributing the removal to warden.
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.log").write_text(
            '{"action":"teardown","branch":"feat","worktree":"%s"}\n'
            % str(self.wt.resolve())
        )
        self._rm_admin_and_dir()
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_warden_finish_is_not_flagged(self) -> None:
        """A `finish` audit record (teardown happens internally) must suppress."""
        wp.reconcile(str(self.repo))  # baseline
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.log").write_text(
            '{"action":"finish","status":"finished","code":0,"branch":"feat",'
            '"worktree":"","ts":"%s"}\n' % _now_ts()
        )
        self._rm_admin_and_dir()
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_warden_finish_many_is_not_flagged_for_nested_branch(self) -> None:
        """finish-many audits its batch under one record.

        Branch names for a batch come only from each entry's own
        details.teardown_results[i].code — landed_branches alone is not
        trusted, since landing without a successful teardown leaves the
        worktree in place.
        """
        wp.reconcile(str(self.repo))  # baseline
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.log").write_text(
            '{"action":"finish-many","status":"finished","code":0,"branch":"",'
            '"worktree":"","ts":"%s","details":{"landed_branches":["feat"],'
            '"teardown_results":[{"branch":"feat","status":"torn_down","code":0}]}}\n'
            % _now_ts()
        )
        self._rm_admin_and_dir()
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_finish_many_landed_without_teardown_is_not_suppressed(self) -> None:
        """landed_branches alone must not suppress a removal.

        Without a successful teardown_results entry, the branch was landed
        but its worktree was never torn down, so an external removal is
        still real content loss.
        """
        wp.reconcile(str(self.repo))  # baseline
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.log").write_text(
            '{"action":"finish-many","status":"teardown_failed","code":1,'
            '"branch":"","worktree":"","ts":"%s","details":{"landed_branches":'
            '["feat"],"teardown_results":[{"branch":"feat",'
            '"status":"worktree_remove_failed","code":1}]}}\n' % _now_ts()
        )
        self._rm_admin_and_dir()
        removals = wp.reconcile(str(self.repo))
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0]["branch"], "feat")

    def test_failed_finish_does_not_suppress_real_removal(self) -> None:
        """A merge_failed finish (code != 0) never tore anything down.

        It must not excuse a genuine external removal of that branch's worktree.
        """
        wp.reconcile(str(self.repo))  # baseline
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.log").write_text(
            '{"action":"finish","status":"merge_failed","code":14,"branch":"feat",'
            '"worktree":"","ts":"%s"}\n' % _now_ts()
        )
        self._rm_admin_and_dir()
        removals = wp.reconcile(str(self.repo))
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0]["branch"], "feat")

    def test_stale_finish_record_does_not_suppress_reused_branch_name(self) -> None:
        """A stale `finish` record must not excuse a later removal.

        A record from BEFORE the last snapshot must not excuse a later,
        genuine external removal of a new worktree reusing the same branch
        name.
        """
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        # Stale record: predates the baseline snapshot about to be taken.
        (audit_dir / "audit.log").write_text(
            '{"action":"finish","status":"finished","code":0,"branch":"feat",'
            '"worktree":"","ts":"2000-01-01T00:00:00Z"}\n'
        )
        wp.reconcile(str(self.repo))  # baseline snapshot, taken AFTER the stale record
        self._rm_admin_and_dir()
        removals = wp.reconcile(str(self.repo))
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0]["branch"], "feat")

    def test_non_dict_audit_line_does_not_crash(self) -> None:
        """A syntactically-valid but non-object audit line must not raise."""
        wp.reconcile(str(self.repo))  # baseline
        audit_dir = self.repo / ".git" / "worktree-warden"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.log").write_text('"just a string"\n[1, 2, 3]\n')
        self._rm_admin_and_dir()
        removals = wp.reconcile(str(self.repo))  # must not raise
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0]["branch"], "feat")

    def test_git_read_failure_does_not_flag_or_wipe_baseline(self) -> None:
        """A failed live `git worktree list` read must not touch the baseline."""
        wp.reconcile(str(self.repo))  # baseline with feat present
        common = self.repo / ".git" / "worktree-warden"
        snap_before = (common / "population.json").read_text()
        # current_population returns None on a bad repo path → reconcile bails.
        self.assertIsNone(wp.current_population(str(self.base / "nonexistent")))
        result = wp.reconcile(str(self.base / "nonexistent"))
        self.assertEqual(result, [])
        # The real repo's baseline is untouched.
        self.assertEqual((common / "population.json").read_text(), snap_before)

    def test_advisory_text_mentions_branch(self) -> None:
        """The rendered advisory names the affected branch and calls it out."""
        wp.reconcile(str(self.repo))
        self._rm_admin_and_dir()
        advisory = wp.format_advisory(wp.reconcile(str(self.repo)), str(self.repo))
        self.assertIn("feat", advisory)
        self.assertIn("external removal", advisory)

    def test_recovery_advisory_has_no_placeholder_and_is_runnable(self) -> None:
        """The `recover` command in the advisory is real and actually runs."""
        wp.reconcile(str(self.repo))
        self._rm_admin_and_dir()
        advisory = wp.format_advisory(wp.reconcile(str(self.repo)), str(self.repo))
        self.assertNotIn("<plugin>", advisory)
        self.assertIn("worktree_engine.py", advisory)
        # Extract and run the emitted `recover` command; it must exit 0.
        line = next(ln for ln in advisory.splitlines() if "worktree_engine.py" in ln)
        cmd = line.split("Run `", 1)[1].rsplit("`", 1)[0]
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_snapshot_written_for_repo_with_no_worktree_history(self) -> None:
        """A primary-only repo (no linked worktrees, ever) grows no state file.

        reconcile() now runs unconditionally from the hook, so this guards
        against every git repo on disk quietly growing a population.json.
        """
        bare = self.base / "plain"
        bare.mkdir()
        _git("init", "-b", "main", cwd=bare)
        _git("config", "user.email", "t@t.test", cwd=bare)
        _git("config", "user.name", "Test", cwd=bare)
        (bare / "f.txt").write_text("x\n")
        _git("add", "f.txt", cwd=bare)
        _git("commit", "-m", "seed", cwd=bare)
        self.assertEqual(wp.reconcile(str(bare)), [])
        self.assertFalse((bare / ".git" / "worktree-warden").exists())

    def test_corrupt_snapshot_does_not_crash_and_self_heals(self) -> None:
        """A hand-corrupted snapshot must not crash reconcile; it self-heals."""
        common = self.repo / ".git" / "worktree-warden"
        common.mkdir(parents=True, exist_ok=True)
        # Valid JSON, wrong value shapes — must not raise (would wedge detection).
        (common / "population.json").write_text('{"/some/path": "a-string", "x": 5}')
        result = wp.reconcile(str(self.repo))  # must not raise
        self.assertIsInstance(result, list)
        # Snapshot was re-saved with the real (well-shaped) population.
        import json as _json

        healed = _json.loads((common / "population.json").read_text())
        self.assertTrue(all(isinstance(v, dict) for v in healed.values()))


if __name__ == "__main__":
    unittest.main()
