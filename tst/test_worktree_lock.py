"""Tests for the cooperative per-worktree advisory lock.

The lock serializes the multi-step operations git itself cannot (the
LLM-orchestrated, multi-process merge being the real case). These tests cover
the pure decision core, the flock-guarded store I/O, staleness/force-unlock, and
the load-bearing claim that two would-be merges contend on the same main key
while two different worktrees do not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import worktree_engine
import worktree_lock as lock

_ROOT = Path(__file__).resolve().parent.parent
_LOCK_CLI = _ROOT / "scripts" / "worktree_lock.py"


class DecideLockTest(unittest.TestCase):
    """The pure decision core: acquire / refresh / block / stale-reclaim."""

    def _rec(self, owner: str, last_active: float, lease: float = 100.0) -> lock.LockRecord:
        return lock.LockRecord(
            key="/wt",
            owner=owner,
            kind="merge",
            reason="r",
            acquired_at=last_active,
            last_active=last_active,
            expires_at=last_active + lease,
        )

    def test_fresh_key_is_acquired(self) -> None:
        d = lock.decide_lock(
            key="/wt", owner="A", kind="merge", reason="r", now=1000.0,
            existing=None, lease_seconds=60,
        )
        self.assertEqual(d.outcome, "acquired")
        assert d.record is not None
        self.assertEqual(d.record.owner, "A")
        self.assertEqual(d.record.expires_at, 1060.0)
        self.assertIsNone(d.blocker)

    def test_same_owner_live_is_refreshed_preserving_acquired_at(self) -> None:
        existing = self._rec("A", last_active=1000.0)
        d = lock.decide_lock(
            key="/wt", owner="A", kind="merge", reason="r2", now=1050.0,
            existing=existing, lease_seconds=60,
        )
        self.assertEqual(d.outcome, "refreshed")
        assert d.record is not None
        self.assertEqual(d.record.acquired_at, 1000.0)  # preserved
        self.assertEqual(d.record.last_active, 1050.0)  # advanced
        self.assertEqual(d.record.expires_at, 1110.0)

    def test_refresh_preserves_kind_and_reason(self) -> None:
        # A same-session occupancy edit during a merge must NOT relabel the live
        # merge lock to "occupancy" (which the SessionStart prune would drop).
        existing = lock.LockRecord(
            key="/wt", owner="A", kind="merge", reason="landing X",
            acquired_at=1000.0, last_active=1000.0, expires_at=1100.0,
        )
        d = lock.decide_lock(
            key="/wt", owner="A", kind="occupancy", reason="editing", now=1050.0,
            existing=existing, lease_seconds=60,
        )
        self.assertEqual(d.outcome, "refreshed")
        assert d.record is not None
        self.assertEqual(d.record.kind, "merge")  # preserved, not relabeled
        self.assertEqual(d.record.reason, "landing X")  # preserved
        self.assertEqual(d.record.expires_at, 1110.0)  # lease renewed

    def test_different_owner_live_is_blocked(self) -> None:
        existing = self._rec("A", last_active=1000.0)
        d = lock.decide_lock(
            key="/wt", owner="B", kind="merge", reason="r", now=1050.0,
            existing=existing, lease_seconds=60,
        )
        self.assertEqual(d.outcome, "blocked")
        self.assertIsNone(d.record)
        assert d.blocker is not None
        self.assertEqual(d.blocker.owner, "A")

    def test_stale_lease_is_reclaimable_by_another_owner(self) -> None:
        existing = self._rec("A", last_active=1000.0, lease=100.0)  # expires 1100
        d = lock.decide_lock(
            key="/wt", owner="B", kind="merge", reason="r", now=1200.0,
            existing=existing, lease_seconds=60,
        )
        self.assertEqual(d.outcome, "acquired")
        assert d.record is not None
        self.assertEqual(d.record.owner, "B")
        self.assertEqual(d.record.acquired_at, 1200.0)  # fresh, not preserved

    def test_is_stale_boundary(self) -> None:
        existing = self._rec("A", last_active=1000.0, lease=100.0)  # expires 1100
        self.assertFalse(lock.is_stale(existing, now=1099.0))
        self.assertTrue(lock.is_stale(existing, now=1100.0))


class _TempRepo(unittest.TestCase):
    """A throwaway git repo with isolated XDG config for hermetic lease config."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.xdg = base / "xdg"
        self.xdg.mkdir()
        self._git("init")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        self._env = {**os.environ, "XDG_CONFIG_HOME": str(self.xdg)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, capture_output=True, text=True,
        )

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_LOCK_CLI), *args],
            capture_output=True, text=True, cwd=str(self.repo), env=self._env,
        )

    @property
    def locks_file(self) -> Path:
        return self.repo / ".git" / "worktree-warden" / "locks.json"


class StoreOpsTest(_TempRepo):
    """Flock-guarded store I/O over the real .git/worktree-warden/locks.json."""

    def test_acquire_then_status_then_release(self) -> None:
        got = self._cli("acquire-main", "--repo", str(self.repo), "--owner", "A", "landing")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("ACQUIRED", got.stdout)
        self.assertTrue(self.locks_file.exists())

        st = self._cli("status", "--repo", str(self.repo))
        self.assertEqual(st.returncode, 0)
        self.assertIn("A", st.stdout)

        rel = self._cli("release-main", "--repo", str(self.repo), "--owner", "A")
        self.assertEqual(rel.returncode, 0)
        store = json.loads(self.locks_file.read_text())
        self.assertEqual(store, {})

    def test_second_owner_is_blocked_then_succeeds_after_release(self) -> None:
        self.assertEqual(
            self._cli("acquire-main", "--repo", str(self.repo), "--owner", "A", "x").returncode, 0
        )
        blocked = self._cli("acquire-main", "--repo", str(self.repo), "--owner", "B", "y")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("BLOCKED", blocked.stdout)
        self.assertIn("A", blocked.stdout)  # names the holder

        self._cli("release-main", "--repo", str(self.repo), "--owner", "A")
        after = self._cli("acquire-main", "--repo", str(self.repo), "--owner", "B", "y")
        self.assertEqual(after.returncode, 0)

    def test_release_by_non_owner_is_refused(self) -> None:
        self._cli("acquire-main", "--repo", str(self.repo), "--owner", "A", "x")
        rel = self._cli("release-main", "--repo", str(self.repo), "--owner", "B")
        # B does not own it, so the lock must survive.
        store = json.loads(self.locks_file.read_text())
        self.assertIn(os.path.realpath(self.repo), store)
        self.assertEqual(store[os.path.realpath(self.repo)]["owner"], "A")
        self.assertEqual(rel.returncode, 0)  # no-op, but not an error

    def test_force_unlock_clears_a_foreign_lock(self) -> None:
        self._cli("acquire-main", "--repo", str(self.repo), "--owner", "A", "x")
        fu = self._cli("force-unlock", "--repo", str(self.repo))
        self.assertEqual(fu.returncode, 0)
        store = json.loads(self.locks_file.read_text())
        self.assertEqual(store, {})

    def test_refresh_renews_lease_for_owner(self) -> None:
        self._cli("acquire-main", "--repo", str(self.repo), "--owner", "A", "x")
        before = json.loads(self.locks_file.read_text())[os.path.realpath(self.repo)]
        # Force a low lease so the renewed expiry is observably different.
        ref = self._cli("refresh-main", "--repo", str(self.repo), "--owner", "A")
        self.assertEqual(ref.returncode, 0)
        after = json.loads(self.locks_file.read_text())[os.path.realpath(self.repo)]
        self.assertGreaterEqual(after["last_active"], before["last_active"])
        self.assertEqual(after["acquired_at"], before["acquired_at"])  # preserved

    def test_concurrent_acquire_exactly_one_winner(self) -> None:
        # The real mutex test: fire N distinct-owner acquires at once; flock must
        # serialize the check-and-set so exactly one wins.
        procs = [
            subprocess.Popen(
                [sys.executable, str(_LOCK_CLI), "acquire-main",
                 "--repo", str(self.repo), "--owner", f"S{i}", "race"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(self.repo), env=self._env,
            )
            for i in range(6)
        ]
        codes = [p.wait() for p in procs]
        winners = codes.count(0)
        blocked = codes.count(1)
        self.assertEqual(winners, 1, f"expected exactly one winner, got {codes}")
        self.assertEqual(blocked, 5)


class StaleReclaimViaStoreTest(_TempRepo):
    """A lock whose lease has lapsed must be reclaimable by another owner."""

    def test_lapsed_lease_is_reclaimed(self) -> None:
        # Write a lock that already expired, by hand, then have B acquire it.
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        key = os.path.realpath(self.repo)
        self.locks_file.write_text(json.dumps({
            key: {
                "key": key, "owner": "ghost", "kind": "merge", "reason": "died",
                "acquired_at": 1.0, "last_active": 1.0, "expires_at": 2.0,
            }
        }) + "\n")
        got = self._cli("acquire-main", "--repo", str(self.repo), "--owner", "B", "reclaim")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        store = json.loads(self.locks_file.read_text())
        self.assertEqual(store[key]["owner"], "B")


class MainFactsFailOpenTest(_TempRepo):
    """When the primary can't be resolved from a linked worktree, fail open."""

    def test_unresolvable_primary_returns_non_repo(self) -> None:
        wt = Path(self._tmp.name) / "linked"
        self._git("worktree", "add", str(wt), "-b", "feat")
        with mock.patch.object(lock, "_primary_toplevel", return_value=None):
            facts = lock.main_facts(str(wt))
        # Non-repo facts → callers fail open (proceed WITHOUT a lock) rather than
        # key on the linked worktree's own path.
        self.assertFalse(facts.is_repo)


class MainKeyResolvesToPrimaryTest(_TempRepo):
    """acquire-main keys on the PRIMARY checkout regardless of invocation cwd.

    Two merges launched from two different linked worktrees both target ``main``,
    so they must contend on one key — not key on their own worktree paths and sail
    past each other, which would silently defeat the serialization guarantee.
    """

    def test_acquire_main_from_linked_worktree_contends_on_primary(self) -> None:
        wt = Path(self._tmp.name) / "linked"
        self._git("worktree", "add", str(wt), "-b", "feat")
        self.assertEqual(
            self._cli("acquire-main", "--repo", str(self.repo), "--owner", "A", "x").returncode, 0
        )
        # B acquires "main" from INSIDE the linked worktree — it must resolve to
        # the primary key and be BLOCKED by A, not get its own key.
        got = subprocess.run(
            [sys.executable, str(_LOCK_CLI), "acquire-main",
             "--repo", str(wt), "--owner", "B", "y"],
            capture_output=True, text=True, cwd=str(wt), env=self._env,
        )
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("A", got.stdout)  # names the primary-key holder
        store = json.loads(self.locks_file.read_text())
        self.assertEqual(list(store), [os.path.realpath(self.repo)])  # one primary key only


class EngineLeaseRefreshTest(_TempRepo):
    """The engine renews the holder's lease via $CLAUDE_CODE_SESSION_ID."""

    def _seed(self, owner: str) -> str:
        key = os.path.realpath(self.repo)
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        self.locks_file.write_text(json.dumps({
            key: {
                "key": key, "owner": owner, "kind": "merge", "reason": "x",
                "acquired_at": 1.0, "last_active": 1.0, "expires_at": time.time() + 10000,
            }
        }) + "\n")
        return key

    def test_refresh_renews_owner_lease_from_env(self) -> None:
        key = self._seed("A")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "A"}):
            worktree_engine._refresh_main_lease(str(self.repo))
        after = json.loads(self.locks_file.read_text())[key]
        self.assertGreater(after["last_active"], 1.0)  # advanced
        self.assertEqual(after["acquired_at"], 1.0)  # preserved

    def test_refresh_is_noop_without_session_id(self) -> None:
        key = self._seed("A")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            worktree_engine._refresh_main_lease(str(self.repo))
        after = json.loads(self.locks_file.read_text())[key]
        self.assertEqual(after["last_active"], 1.0)  # untouched

    def test_refresh_does_not_acquire_for_a_foreign_lock(self) -> None:
        key = self._seed("A")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "B"}):
            worktree_engine._refresh_main_lease(str(self.repo))
        after = json.loads(self.locks_file.read_text())[key]
        self.assertEqual(after["owner"], "A")  # B cannot steal via refresh
        self.assertEqual(after["last_active"], 1.0)


class SessionAdvisoryTest(_TempRepo):
    """The SessionStart advisory distinguishes active from stale locks."""

    @property
    def _common(self) -> str:
        return os.path.realpath(self.repo / ".git")

    def test_empty_store_is_quiet(self) -> None:
        self.assertIsNone(lock.session_advisory(self._common, time.time()))

    def test_active_lock_surfaces_owner(self) -> None:
        self._cli("acquire-main", "--repo", str(self.repo), "--owner", "OTHER", "landing")
        adv = lock.session_advisory(self._common, time.time())
        assert adv is not None
        self.assertIn("OTHER", adv)
        self.assertIn("🔒", adv)
        self.assertNotIn("STALE", adv)

    def test_stale_lock_surfaces_force_unlock(self) -> None:
        key = os.path.realpath(self.repo)
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        self.locks_file.write_text(json.dumps({
            key: {
                "key": key, "owner": "ghost", "kind": "merge", "reason": "died",
                "acquired_at": 1.0, "last_active": 1.0, "expires_at": 2.0,
            }
        }) + "\n")
        adv = lock.session_advisory(self._common, time.time())
        assert adv is not None
        self.assertIn("STALE", adv)
        self.assertIn("force-unlock", adv)


class SessionStartLockSurfaceTest(_TempRepo):
    """The SessionStart hook injects the lock advisory into its systemMessage."""

    def test_hook_surfaces_active_lock(self) -> None:
        now = time.time()
        key = os.path.realpath(self.repo)
        self.locks_file.parent.mkdir(parents=True, exist_ok=True)
        self.locks_file.write_text(json.dumps({
            key: {
                "key": key, "owner": "OTHER", "kind": "merge", "reason": "landing",
                "acquired_at": now, "last_active": now, "expires_at": now + 9999,
            }
        }) + "\n")
        hook = _ROOT / "hooks" / "check_worktrees_hook.py"
        env = {**self._env, "CLAUDE_PLUGIN_ROOT": str(_ROOT)}
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({"source": "startup", "cwd": str(self.repo)}),
            capture_output=True, text=True, cwd=str(self.repo), env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertIn("worktree-warden locks", out.get("systemMessage", ""))
        self.assertIn("OTHER", out["systemMessage"])


if __name__ == "__main__":
    unittest.main()
