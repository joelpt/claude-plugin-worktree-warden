"""Tests for aborting a merge on a lost main-target lease (issue #40, F-A Option B).

The multi-process merge holds its main-target lease across many separate
``worktree_engine.py`` subprocesses; the gaps between them (conflict resolution,
tests, human review) are not continuously refreshed, so a long pause -- or a
human ``force-unlock`` -- can let a *second* session reclaim the key mid-merge.
Option A (a long, per-subcommand-renewed lease) narrows that window; Option B,
tested here, *closes* it: a mutating subcommand invoked with ``--require-lease``
that finds its lease lost ABORTS before touching anything, so the repo is left in
the previous step's (recoverable) state rather than racing a second writer onto
``main``.

The flag is opt-in precisely because ``refresh()`` alone cannot tell "I held a
lease and lost it" from "I never acquired one" (a direct CLI land) -- both read as
an empty/foreign store. Only the orchestrator knows it is mid-merge, so only it
passes ``--require-lease``. ``undo`` deliberately has no such flag: recovery must
never be blockable by the very lease-loss it recovers from.
"""

from __future__ import annotations

import contextlib
import io
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


class _TwoWorktreeRepo(unittest.TestCase):
    """A temp repo on ``main`` with two clean, landable linked worktrees."""

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

        self.wtA = self.base / "wtA"
        _git("worktree", "add", "-b", "featA", str(self.wtA), cwd=self.repo)
        (self.wtA / "a.txt").write_text("a\n")
        _git("add", "a.txt", cwd=self.wtA)
        _git("commit", "-m", "feat a", cwd=self.wtA)

        self.wtB = self.base / "wtB"
        _git("worktree", "add", "-b", "featB", str(self.wtB), cwd=self.repo)
        (self.wtB / "b.txt").write_text("b\n")
        _git("add", "b.txt", cwd=self.wtB)
        _git("commit", "-m", "feat b", cwd=self.wtB)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def key(self) -> str:
        return os.path.realpath(self.repo)

    @property
    def main_sha(self) -> str:
        return _git("rev-parse", "main", cwd=self.repo)

    @property
    def locks_file(self) -> Path:
        return self.repo / ".git" / "worktree-warden" / "locks.json"

    def _seed_lock(self, owner: str, *, expires_in: float = 10_000.0) -> None:
        """Write a held main-target lock owned by ``owner`` (a merge lease)."""
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        self.locks_file.write_text(
            json.dumps(
                {
                    self.key: {
                        "key": self.key,
                        "owner": owner,
                        "kind": "merge",
                        "reason": "landing featA,featB",
                        "acquired_at": 1.0,
                        "last_active": 1.0,
                        "expires_at": time.time() + expires_in,
                    }
                }
            )
            + "\n"
        )

    def _force_unlock(self) -> None:
        lock.force_unlock(lock.main_facts(str(self.repo)), all_keys=True)

    def _run(self, *argv: str, owner: str | None) -> tuple[int, dict[str, object]]:
        """Invoke ``engine.main`` capturing the exit code and emitted JSON."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        if owner is not None:
            env["CLAUDE_CODE_SESSION_ID"] = owner
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(buf):
            code = engine.main(list(argv))
        payload: dict[str, object] = {}
        try:
            parsed = json.loads(buf.getvalue())
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
        return code, payload

    @staticmethod
    def _details(payload: dict[str, object]) -> dict[str, object]:
        """Narrow ``payload['details']`` to a typed mapping (or empty)."""
        details = payload.get("details", {})
        return details if isinstance(details, dict) else {}

    @staticmethod
    def _message(payload: dict[str, object]) -> str:
        """Narrow ``payload['message']`` to a str (or empty)."""
        message = payload.get("message", "")
        return message if isinstance(message, str) else ""


class RefreshMainLeaseReturnsLostTest(_TwoWorktreeRepo):
    """``_refresh_main_lease`` reports ``(lost, holder)`` for the abort path."""

    def test_returns_false_on_successful_refresh(self) -> None:
        self._seed_lock("A")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertFalse(lost)
        self.assertIsNone(holder)

    def test_returns_lost_with_no_holder_when_lease_gone(self) -> None:
        # No lock record at all -> the lease this session expected is gone, but
        # nobody holds it now -> no live contender to name.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertTrue(lost)
        self.assertIsNone(holder)

    def test_returns_lost_naming_the_reclaimer(self) -> None:
        self._seed_lock("B")  # someone else now holds main
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertTrue(lost)
        self.assertEqual(holder, "B")

    def test_returns_not_lost_without_session_id(self) -> None:
        # Fail-open: no session id -> never an abort signal.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertFalse(lost)
        self.assertIsNone(holder)

    def test_io_fault_is_never_read_as_a_lost_lease(self) -> None:
        # An unreadable store would make a fail-open read return {} ("lost"); the
        # strict read RAISES on the fault instead, so _refresh_main_lease fails
        # OPEN (no abort) rather than halt a healthy merge.
        with (
            mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}),
            mock.patch.object(lock, "read_store_strict", side_effect=OSError("disk blip")),
        ):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertFalse(lost)
        self.assertIsNone(holder)

    def test_strict_fault_still_attempts_best_effort_renewal(self) -> None:
        # A strict-read fault suppresses the abort, but the step also skipped its
        # renewal; _refresh_main_lease must fall back to a fail-open refresh() so a
        # live lease cannot silently lapse across steps under recurring blips.
        self._seed_lock("A")
        with (
            mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}),
            mock.patch.object(lock, "refresh_strict", side_effect=OSError("blip")),
            mock.patch.object(lock, "refresh", wraps=lock.refresh) as spy,
        ):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertFalse(lost)
        self.assertIsNone(holder)
        spy.assert_called_once()  # the fail-open renewal fallback fired

    def test_corrupt_non_object_store_is_never_read_as_a_lost_lease(self) -> None:
        # A valid-JSON but non-object store (e.g. ``[]`` from partial corruption)
        # makes a fail-open read return {} ("lost"); the strict read must RAISE on
        # it (not return {}), so _refresh_main_lease fails OPEN -- a corrupt
        # store must never masquerade as a force-unlocked lease and abort a merge.
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        self.locks_file.write_text("[]\n")
        with self.assertRaises(ValueError):
            lock.read_store_strict(str(self.repo / ".git"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertFalse(lost)
        self.assertIsNone(holder)

    def test_corrupt_own_record_is_never_read_as_a_lost_lease(self) -> None:
        # Our OWN record is present but per-record malformed (e.g. a non-numeric
        # expires_at from a torn write that still parses as JSON). read_store drops
        # it (fail-open) -> "lost"; the strict read must RAISE on the bad record,
        # not also drop it, so _refresh_main_lease fails OPEN. Otherwise a healthy
        # merge whose record merely got corrupted is spuriously aborted.
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        self.locks_file.write_text(
            json.dumps(
                {
                    self.key: {
                        "key": self.key,
                        "owner": "A",
                        "kind": "merge",
                        "reason": "landing",
                        "acquired_at": 1.0,
                        "last_active": 1.0,
                        "expires_at": "not-a-number",  # torn field -> unparseable
                    }
                }
            )
            + "\n"
        )
        with self.assertRaises(ValueError):
            lock.read_store_strict(str(self.repo / ".git"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertFalse(lost)
        self.assertIsNone(holder)

    def test_stale_foreign_record_is_lost_but_unnamed(self) -> None:
        # A foreign owner whose lease already lapsed is no live contender: lost,
        # but holder is None so the caller knows undo is safe.
        self._seed_lock("B", expires_in=-1.0)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            lost, holder = engine._refresh_main_lease(str(self.repo))
        self.assertTrue(lost)
        self.assertIsNone(holder)


class RequireLeaseAbortTest(_TwoWorktreeRepo):
    """``--require-lease`` turns a lost lease into a pre-mutation abort."""

    def test_lost_lease_aborts_land_before_mutation(self) -> None:
        self._seed_lock("A")
        self._force_unlock()  # human force-unlock mid-merge
        before = self.main_sha
        code, payload = self._run(
            "--repo", str(self.repo), "land",
            "--worktree", str(self.wtA), "--branch", "featA", "--target", "main",
            "--require-lease", owner="A",
        )
        self.assertEqual(code, engine.EXIT_LEASE_LOST)
        self.assertEqual(payload.get("status"), "lease_lost")
        self.assertEqual(self.main_sha, before)  # nothing landed

    def test_lost_lease_does_not_abort_without_the_flag(self) -> None:
        # Backward-compat: a direct land that never held a lease still lands.
        before = self.main_sha
        code, _ = self._run(
            "--repo", str(self.repo), "land",
            "--worktree", str(self.wtA), "--branch", "featA", "--target", "main",
            owner="A",
        )
        self.assertEqual(code, engine.EXIT_OK)
        self.assertNotEqual(self.main_sha, before)  # featA landed normally

    def test_healthy_lease_proceeds_with_the_flag(self) -> None:
        self._seed_lock("A")
        before = self.main_sha
        code, _ = self._run(
            "--repo", str(self.repo), "land",
            "--worktree", str(self.wtA), "--branch", "featA", "--target", "main",
            "--require-lease", owner="A",
        )
        self.assertEqual(code, engine.EXIT_OK)
        self.assertNotEqual(self.main_sha, before)  # lease alive -> landed

    def test_abort_details_name_a_live_reclaimer(self) -> None:
        # When another live session reclaimed the key, the holder is surfaced so
        # the orchestrator knows undo would collide with a live writer.
        self._seed_lock("B")
        code, payload = self._run(
            "--repo", str(self.repo), "land",
            "--worktree", str(self.wtA), "--branch", "featA", "--target", "main",
            "--require-lease", owner="A",
        )
        self.assertEqual(code, engine.EXIT_LEASE_LOST)
        self.assertEqual(self._details(payload).get("holder"), "B")

    def test_abort_details_report_no_holder_after_force_unlock(self) -> None:
        # Force-unlocked and unreclaimed -> no live contender; undo is safe.
        self._seed_lock("A")
        self._force_unlock()
        code, payload = self._run(
            "--repo", str(self.repo), "land",
            "--worktree", str(self.wtA), "--branch", "featA", "--target", "main",
            "--require-lease", owner="A",
        )
        self.assertEqual(code, engine.EXIT_LEASE_LOST)
        self.assertIsNone(self._details(payload).get("holder"))

    def test_snapshot_abort_does_not_tell_caller_to_undo(self) -> None:
        # Aborting AT the snapshot step: nothing landed, no snapshot file exists,
        # so the recovery guidance must NOT say "undo the held snapshot".
        self._seed_lock("A")
        self._force_unlock()
        code, payload = self._run(
            "--repo", str(self.repo), "snapshot",
            "--target", "main", "--branches", "featA,featB",
            "--require-lease", owner="A",
        )
        self.assertEqual(code, engine.EXIT_LEASE_LOST)
        msg = self._message(payload).lower()
        self.assertNotIn("undo the held snapshot", msg)  # no false recovery instruction
        self.assertIn("nothing to undo", msg)  # the honest guidance instead

    def test_undo_is_never_blocked_by_a_lost_lease(self) -> None:
        # undo is the recovery path: it must run even with a lost lease (it has
        # no --require-lease flag, so the abort can never fire for it).
        snap = engine.cmd_snapshot(str(self.repo), "main", ["featA"])
        snapshot_file = str(snap.details["snapshot_file"])
        self._force_unlock()  # no lock at all
        code, _ = self._run(
            "--repo", str(self.repo), "undo", "--snapshot", snapshot_file, owner="A",
        )
        self.assertNotEqual(code, engine.EXIT_LEASE_LOST)
        self.assertEqual(code, engine.EXIT_OK)


class LostLeaseRecoverabilityTest(_TwoWorktreeRepo):
    """The acceptance scenario: abort mid-merge, then ``undo`` restores ``main``."""

    def test_force_unlock_mid_merge_aborts_and_undo_recovers(self) -> None:
        pre_land = self.main_sha

        # Step 2: snapshot the restore anchors BEFORE any land.
        snap = engine.cmd_snapshot(str(self.repo), "main", ["featA", "featB"])
        self.assertEqual(snap.code, engine.EXIT_OK)
        snapshot_file = str(snap.details["snapshot_file"])

        # Step 0/5: hold the lease, land featA onto main.
        self._seed_lock("A")
        landed = engine.cmd_land(str(self.wtA), "featA", "main", str(self.repo))
        self.assertEqual(landed.code, engine.EXIT_OK)
        after_a = self.main_sha
        self.assertNotEqual(after_a, pre_land)  # featA is on main now

        # A human force-unlocks mid-merge; the lease is lost.
        self._force_unlock()

        # The next mutating subcommand aborts -- featB does NOT land.
        code, _ = self._run(
            "--repo", str(self.repo), "land",
            "--worktree", str(self.wtB), "--branch", "featB", "--target", "main",
            "--require-lease", owner="A",
        )
        self.assertEqual(code, engine.EXIT_LEASE_LOST)
        self.assertEqual(self.main_sha, after_a)  # featB blocked; no further mutation

        # Recovery: undo (no --require-lease) restores main to its pre-land tip,
        # rolling back featA's land -- proving work already on main is recoverable.
        undone = engine.cmd_undo(str(self.repo), snapshot_file)
        self.assertEqual(undone.code, engine.EXIT_OK, undone.message)
        self.assertEqual(self.main_sha, pre_land)


if __name__ == "__main__":
    unittest.main()
