"""Tests for cross-session worktree-population tracking + external-removal detection."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import worktree_population as wp


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class PopulationTest(unittest.TestCase):
    """reconcile() flags content-bearing disappearances warden did not cause."""

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
        (self.wt / "work.txt").write_text("unlanded\n")
        _git("add", "work.txt", cwd=self.wt)
        _git("commit", "-m", "feat work", cwd=self.wt)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rm_admin_and_dir(self) -> None:
        # Simulate an external removal: directory gone AND admin record pruned,
        # so it is fully absent from `git worktree list` (recover can't see it).
        shutil.rmtree(self.wt)
        _git("worktree", "prune", cwd=self.repo)

    def test_first_run_records_no_removal(self) -> None:
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_external_removal_of_unlanded_worktree_is_flagged(self) -> None:
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
        _git("merge", "--ff-only", "feat", cwd=self.repo)  # land it
        wp.reconcile(str(self.repo))  # baseline (now clean + landed)
        self._rm_admin_and_dir()
        self.assertEqual(wp.reconcile(str(self.repo)), [])

    def test_warden_teardown_is_not_flagged(self) -> None:
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

    def test_git_read_failure_does_not_flag_or_wipe_baseline(self) -> None:
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
        wp.reconcile(str(self.repo))
        self._rm_admin_and_dir()
        advisory = wp.format_advisory(wp.reconcile(str(self.repo)))
        self.assertIn("feat", advisory)
        self.assertIn("external removal", advisory)

    def test_corrupt_snapshot_does_not_crash_and_self_heals(self) -> None:
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
