"""Phase-2 per-worktree occupancy lock tests.

Occupancy is layered onto the edit gate's ALLOW path: a session editing a
worktree claims it (keyed by the worktree toplevel, owner = session id), and a
*different* live session editing the same worktree is blocked. A session's own
subagents share its session id, so they never block each other. The lock is a
best-effort coordination aid, deliberately decoupled from the core edit gate: a
broken lock module disables only occupancy, never enforcement.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import worktree_gate as gate
import worktree_lock as lock

_ROOT = Path(__file__).resolve().parent.parent


class OccupancyUnitTest(unittest.TestCase):
    """Pure-ish unit coverage for the occupancy helpers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.xdg = base / "xdg"
        self.xdg.mkdir()
        self.repo = base / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        self.facts = gate.git_facts(str(self.repo))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def _write_user_cfg(self, **kv: object) -> None:
        d = self.xdg / "worktree-gate"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(kv))

    def test_occupancy_enabled_defaults_true(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            self.assertTrue(lock.occupancy_enabled(self.facts))

    def test_occupancy_disabled_via_user_config(self) -> None:
        self._write_user_cfg(occupancy_lock=False)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            self.assertFalse(lock.occupancy_enabled(self.facts))

    def test_acquire_rejects_unknown_kind(self) -> None:
        # `kind` is load-bearing (prune/surface branch on it); a stray kind must
        # raise at the acquire boundary, not silently corrupt the store.
        with self.assertRaises(ValueError):
            lock.acquire(self.facts, "A", "bogus", "r", 1000.0)

    def test_block_message_names_holder_and_force_unlock(self) -> None:
        rec = lock.LockRecord(
            key="/wt/x", owner="SESS-A", kind="occupancy", reason="editing",
            acquired_at=1.0, last_active=1.0, expires_at=2.0,
        )
        msg = lock.occupancy_block_message(rec, "/wt/x")
        self.assertIn("SESS-A", msg)
        self.assertIn("force-unlock", msg)
        self.assertIn("/wt/x", msg)

    def test_prune_removes_stale_occupancy_only(self) -> None:
        common = self.facts.git_common_dir
        assert common is not None
        now = 1000.0
        store = {
            "/wt/stale-occ": lock.LockRecord("/wt/stale-occ", "dead", "occupancy", "e", 1, 1, 2),
            "/wt/live-occ": lock.LockRecord("/wt/live-occ", "alive", "occupancy", "e", 1, now, now + 999),
            "/wt/stale-merge": lock.LockRecord("/wt/stale-merge", "dead", "merge", "m", 1, 1, 2),
            "/main": lock.LockRecord("/main", "dead", "bumpall", "b", 1, 1, 2),
        }
        with lock._flock(common):
            lock._write_store(common, store)
        pruned = lock.prune_stale_occupancy(common, now)
        self.assertEqual(pruned, ["/wt/stale-occ"])
        remaining = lock.read_store(common)
        # Operation locks (merge/bumpall) are non-prunable (surfaced when stale);
        # only occupancy is disposable.
        self.assertEqual(set(remaining), {"/wt/live-occ", "/wt/stale-merge", "/main"})

    def test_session_advisory_skips_live_occupancy(self) -> None:
        common = self.facts.git_common_dir
        assert common is not None
        now = 1000.0
        store = {
            "/wt/a": lock.LockRecord("/wt/a", "EDITOR", "occupancy", "e", 1, now, now + 999),
            "/main": lock.LockRecord("/main", "MERGER", "merge", "landing", 1, now, now + 999),
        }
        with lock._flock(common):
            lock._write_store(common, store)
        adv = lock.session_advisory(common, now)
        assert adv is not None
        self.assertIn("MERGER", adv)  # live operation surfaced
        self.assertNotIn("EDITOR", adv)  # live occupancy is steady-state noise


class _HookHarness(unittest.TestCase):
    """Temp repo + linked worktree + temp plugin root running the real edit hook."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.xdg = base / "xdg"
        self.plugin_root = base / "plugin"
        (self.plugin_root / "scripts").mkdir(parents=True)
        (self.plugin_root / "hooks").mkdir(parents=True)
        self._git("init")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        for name in ("worktree_gate.py", "worktree_lock.py"):
            shutil.copy(_ROOT / "scripts" / name, self.plugin_root / "scripts" / name)
        shutil.copy(
            _ROOT / "hooks" / "enforce_worktree_hook.py",
            self.plugin_root / "hooks" / "enforce_worktree_hook.py",
        )
        self.wt = base / "wt"
        self._git("worktree", "add", str(self.wt), "-b", "feature")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def _break_lock_module(self) -> None:
        (self.plugin_root / "scripts" / "worktree_lock.py").write_text("def (:\n")

    def _disable_occupancy(self) -> None:
        d = self.xdg / "worktree-gate"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps({"occupancy_lock": False}))

    def _open_grant(self) -> None:
        gdir = self.repo / ".git"
        (gdir / "worktree-gate-grant.json").write_text(
            json.dumps({"reason": "test", "granted_at": 0.0, "expires_at": 9_999_999_999.0})
        )

    def _lock_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.plugin_root / "scripts" / "worktree_lock.py"), *args],
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env={**os.environ, "XDG_CONFIG_HOME": str(self.xdg), "CLAUDE_PLUGIN_ROOT": str(self.plugin_root)},
        )

    def _edit(
        self, cwd: Path, file_path: Path, session_id: str, agent_id: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {
            "tool_name": "Edit",
            "cwd": str(cwd),
            "tool_input": {"file_path": str(file_path)},
            "session_id": session_id,
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return subprocess.run(
            [sys.executable, str(self.plugin_root / "hooks" / "enforce_worktree_hook.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env={**os.environ, "XDG_CONFIG_HOME": str(self.xdg), "CLAUDE_PLUGIN_ROOT": str(self.plugin_root)},
        )

    @property
    def store(self) -> dict[str, dict[str, object]]:
        f = self.repo / ".git" / "worktree-warden" / "locks.json"
        if not f.exists():
            return {}
        return cast("dict[str, dict[str, object]]", json.loads(f.read_text()))

    @property
    def sentinel(self) -> Path:
        return self.repo / ".git" / "worktree-warden" / "gate-load-error"


class OccupancyHookTest(_HookHarness):
    """End-to-end occupancy behavior through the real PreToolUse edit hook."""

    def test_linked_edit_allowed_and_records_occupancy(self) -> None:
        proc = self._edit(self.wt, self.wt / "f.py", "SESS-A")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        key = os.path.realpath(self.wt)
        self.assertIn(key, self.store)
        self.assertEqual(self.store[key]["owner"], "SESS-A")
        self.assertEqual(self.store[key]["kind"], "occupancy")

    def test_other_session_is_blocked(self) -> None:
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-A").returncode, 0)
        proc = self._edit(self.wt, self.wt / "f.py", "SESS-B")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("occupied by another session", proc.stderr)
        self.assertIn("SESS-A", proc.stderr)
        self.assertIn("force-unlock", proc.stderr)

    def test_same_session_subagent_is_reentrant(self) -> None:
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-A").returncode, 0)
        # A subagent of the same session shares session_id (distinct agent_id) →
        # same owner → must NOT be blocked.
        proc = self._edit(self.wt, self.wt / "g.py", "SESS-A", agent_id="agent-xyz")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_phantom_claim_when_gate_blocks_main_edit(self) -> None:
        # A main-checkout edit with no grant is gate-blocked; occupancy must NOT
        # run, so no record is claimed on the primary key.
        proc = self._edit(self.repo, self.repo / "x.py", "SESS-A")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(os.path.realpath(self.repo), self.store)

    def test_occupancy_disabled_lets_other_session_through(self) -> None:
        self._disable_occupancy()
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-A").returncode, 0)
        proc = self._edit(self.wt, self.wt / "f.py", "SESS-B")
        self.assertEqual(proc.returncode, 0, proc.stderr)  # not blocked when disabled

    def test_broken_lock_module_still_allows_linked_edit(self) -> None:
        # Decoupling guarantee: a broken worktree_lock disables ONLY occupancy.
        # The core gate (which does not import it) still allows the linked edit,
        # and drops no gate-load-error sentinel.
        self._break_lock_module()
        # First, an occupancy claim by A is impossible now (module broken), so even
        # a second session must be allowed (occupancy silently skipped).
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-A").returncode, 0)
        proc = self._edit(self.wt, self.wt / "f.py", "SESS-B")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.sentinel.exists())
        # Fail-open LOUDLY: the swallowed occupancy import error is recorded, not silent.
        audit = self.xdg / "worktree-gate" / "audit.log"
        self.assertTrue(audit.exists() and "occupancy-error" in audit.read_text())

    def test_cross_repo_edit_does_not_claim_worktree(self) -> None:
        # Editing a file OUTSIDE the worktree (from the worktree cwd) must not
        # claim the worktree -- so it can't block another session.
        outside = Path(self._tmp.name) / "outside.txt"
        self.assertEqual(self._edit(self.wt, outside, "SESS-A").returncode, 0)
        self.assertNotIn(os.path.realpath(self.wt), self.store)
        # A genuinely separate session can still edit inside the worktree.
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-B").returncode, 0)

    def test_force_unlock_named_worktree_clears_its_occupancy(self) -> None:
        # The block message tells a stuck session to force-unlock the LINKED
        # worktree; that command must clear the linked key (not resolve to primary).
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-A").returncode, 0)
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-B").returncode, 2)
        fu = self._lock_cli("force-unlock", "--repo", str(self.wt))
        self.assertEqual(fu.returncode, 0, fu.stdout)
        self.assertNotIn(os.path.realpath(self.wt), self.store)  # linked key cleared
        self.assertEqual(self._edit(self.wt, self.wt / "f.py", "SESS-B").returncode, 0)

    def test_main_checkout_grant_edit_claims_nothing(self) -> None:
        # Occupancy is a per-linked-worktree concept: a main-checkout edit (allowed
        # here by a grant) must NOT claim the primary key. A live main edit already
        # blocks a merge via the engine's dirty-primary refusal; keeping occupancy
        # off the primary key avoids stale-claim noise and merge collisions.
        self._open_grant()
        proc = self._edit(self.repo, self.repo / "x.py", "SESS-A")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(os.path.realpath(self.repo), self.store)


if __name__ == "__main__":
    unittest.main()
