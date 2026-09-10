"""Unit tests for the worktree-gate policy and settings resolution."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import worktree_gate as gate


def _main_facts(root: str, common_dir: str) -> gate.GitFacts:
    """Build GitFacts for a main checkout at root with the given git dir."""
    return gate.GitFacts(
        is_repo=True,
        repo_root=os.path.realpath(root),
        git_common_dir=os.path.realpath(common_dir),
        in_linked_worktree=False,
    )


class DecideTest(unittest.TestCase):
    """Exhaustive rulings for the pure decision function."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.git_dir = os.path.join(self.root, ".git")
        os.makedirs(self.git_dir, exist_ok=True)
        self.facts = _main_facts(self.root, self.git_dir)
        self.target = os.path.join(self.root, "src", "main.py")

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        self._tmp.cleanup()

    def test_user_disabled_allows(self) -> None:
        """User disabled allows."""
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=0.0,
            disabled_scope="user",
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)
        self.assertIn("user", d.reason)

    def test_project_disabled_allows(self) -> None:
        """Project disabled allows."""
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=0.0,
            disabled_scope="project",
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_non_repo_allows(self) -> None:
        """Non repo allows."""
        facts = gate.GitFacts(False, None, None, False)
        d = gate.decide(
            file_path=self.target,
            facts=facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_linked_worktree_allows(self) -> None:
        """Linked worktree allows."""
        facts = gate.GitFacts(True, self.root, self.git_dir, True)
        d = gate.decide(
            file_path=self.target,
            facts=facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_no_file_path_allows(self) -> None:
        """No file path allows."""
        d = gate.decide(
            file_path=None,
            facts=self.facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_outside_checkout_allows(self) -> None:
        """Outside checkout allows."""
        outside = os.path.join(os.path.dirname(self.root), "other-repo", "x.py")
        d = gate.decide(
            file_path=outside,
            facts=self.facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_inside_git_dir_allows(self) -> None:
        """Inside git dir allows."""
        d = gate.decide(
            file_path=os.path.join(self.git_dir, "config"),
            facts=self.facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_main_checkout_without_grant_blocks(self) -> None:
        """Main checkout without grant blocks."""
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertFalse(d.allow)

    def test_main_checkout_with_expired_grant_blocks(self) -> None:
        """Main checkout with expired grant blocks."""
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=999.0,
        )
        self.assertFalse(d.allow)

    def test_main_checkout_with_active_grant_allows(self) -> None:
        """Main checkout with active grant allows."""
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=1500.0,
        )
        self.assertTrue(d.allow)
        self.assertTrue(d.log_grant_use)

    def test_unborn_head_allows_without_grant(self) -> None:
        """Unborn head allows without grant."""
        # A repo with no commits cannot host a worktree (nothing to branch
        # from), so the gate steps aside; enforcement resumes once HEAD is born.
        facts = gate.GitFacts(
            is_repo=True,
            repo_root=self.root,
            git_common_dir=os.path.realpath(self.git_dir),
            in_linked_worktree=False,
            head_unborn=True,
        )
        d = gate.decide(
            file_path=self.target,
            facts=facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)
        self.assertIn("unborn", d.reason)
        self.assertFalse(d.log_grant_use)


class DurationTest(unittest.TestCase):
    """Duration parsing and clamping."""

    def test_bare_seconds(self) -> None:
        """Bare seconds."""
        self.assertEqual(gate.parse_duration("900"), 900)

    def test_minutes_suffix(self) -> None:
        """Minutes suffix."""
        self.assertEqual(gate.parse_duration("15m"), 900)

    def test_hours_suffix(self) -> None:
        """Hours suffix."""
        self.assertEqual(gate.parse_duration("1h"), 3600)

    def test_below_min_clamps_up(self) -> None:
        """Below min clamps up."""
        self.assertEqual(gate.parse_duration("30s"), gate.MIN_WINDOW_SECONDS)

    def test_above_max_clamps_down(self) -> None:
        """Above max clamps down."""
        self.assertEqual(gate.parse_duration("100h"), gate.MAX_WINDOW_SECONDS)

    def test_invalid_raises(self) -> None:
        """Invalid raises."""
        with self.assertRaises(ValueError):
            gate.parse_duration("soon")


class SettingsTest(unittest.TestCase):
    """Scope-resolved settings with project overriding user."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._xdg = base / "xdg"
        self._git = base / "repo" / ".git"
        self._git.mkdir(parents=True)
        self._prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self._xdg)
        self.common_dir = os.path.realpath(str(self._git))
        self.repo_root = str(base / "repo")
        self.facts = _main_facts(self.repo_root, str(self._git))

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        if self._prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_xdg
        self._tmp.cleanup()

    def _write_user(self, data: dict[str, object]) -> None:
        path = gate.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def _write_project(self, data: dict[str, object]) -> None:
        path = gate.project_config_path(self.repo_root)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_defaults(self) -> None:
        """Defaults."""
        settings = gate.resolve_settings(self.facts)
        self.assertIsNone(settings.disabled_scope)
        self.assertEqual(settings.window_seconds, gate.DEFAULT_WINDOW_SECONDS)
        self.assertEqual(settings.startup_display, gate.DEFAULT_STARTUP_DISPLAY)

    def test_user_startup_display(self) -> None:
        """User startup display."""
        self._write_user({"startup_display": "never"})
        self.assertEqual(gate.resolve_settings(self.facts).startup_display, "never")

    def test_project_startup_display_overrides_user(self) -> None:
        """Project startup display overrides user."""
        self._write_user({"startup_display": "never"})
        self._write_project({"startup_display": "mergeable"})
        self.assertEqual(
            gate.resolve_settings(self.facts).startup_display, "mergeable"
        )

    def test_invalid_startup_display_falls_back_to_default(self) -> None:
        """Invalid startup display falls back to default."""
        self._write_user({"startup_display": "bogus"})
        self.assertEqual(
            gate.resolve_settings(self.facts).startup_display,
            gate.DEFAULT_STARTUP_DISPLAY,
        )

    def test_user_opt_out(self) -> None:
        """User opt out."""
        self._write_user({"enforce": False})
        self.assertEqual(gate.resolve_settings(self.facts).disabled_scope, "user")

    def test_project_overrides_user_opt_out(self) -> None:
        """Project overrides user opt out."""
        self._write_user({"enforce": False})
        self._write_project({"enforce": False})
        self.assertEqual(
            gate.resolve_settings(self.facts).disabled_scope, "project"
        )

    def test_project_enable_overrides_user_disable(self) -> None:
        """Project enable overrides user disable."""
        self._write_user({"enforce": False})
        self._write_project({"enforce": True})
        self.assertIsNone(gate.resolve_settings(self.facts).disabled_scope)

    def test_project_disable_overrides_user_enable(self) -> None:
        """Project disable overrides user enable."""
        self._write_user({"enforce": True})
        self._write_project({"enforce": False})
        self.assertEqual(
            gate.resolve_settings(self.facts).disabled_scope, "project"
        )

    def test_user_window(self) -> None:
        """User window."""
        self._write_user({"window_seconds": 1800})
        self.assertEqual(gate.resolve_settings(self.facts).window_seconds, 1800)

    def test_project_window_overrides_user(self) -> None:
        """Project window overrides user."""
        self._write_user({"window_seconds": 1800})
        self._write_project({"window_seconds": 600})
        self.assertEqual(gate.resolve_settings(self.facts).window_seconds, 600)

    def test_malformed_config_falls_back_to_defaults(self) -> None:
        """Malformed config falls back to defaults."""
        path = gate.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        settings = gate.resolve_settings(self.facts)
        self.assertIsNone(settings.disabled_scope)
        self.assertEqual(settings.window_seconds, gate.DEFAULT_WINDOW_SECONDS)


class GrantExpiryTest(unittest.TestCase):
    """Reading the active-exception token."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        self.common = self._tmp.name

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        self._tmp.cleanup()

    def test_missing_token_is_none(self) -> None:
        """Missing token is none."""
        self.assertIsNone(gate.read_grant_expiry(self.common))

    def test_valid_token_returns_expiry(self) -> None:
        """Valid token returns expiry."""
        path = gate.grant_path(self.common)
        assert path is not None
        path.write_text(json.dumps({"expires_at": 1234.5, "reason": "x"}))
        self.assertEqual(gate.read_grant_expiry(self.common), 1234.5)

    def test_malformed_token_is_none(self) -> None:
        """Malformed token is none."""
        path = gate.grant_path(self.common)
        assert path is not None
        path.write_text("garbage")
        self.assertIsNone(gate.read_grant_expiry(self.common))


class UnbornNoticeTest(unittest.TestCase):
    """One-time unborn-HEAD notice claim."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        self.common = self._tmp.name

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        self._tmp.cleanup()

    def test_outside_repo_never_claims(self) -> None:
        """Outside repo never claims."""
        self.assertFalse(gate.claim_unborn_notice(None))

    def test_claims_exactly_once(self) -> None:
        """Claims exactly once."""
        self.assertTrue(gate.claim_unborn_notice(self.common))
        self.assertFalse(gate.claim_unborn_notice(self.common))


class BlockMessageTest(unittest.TestCase):
    """block_message content when an edit is blocked."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.git_dir = os.path.join(self.root, ".git")
        os.makedirs(self.git_dir, exist_ok=True)
        self.facts = _main_facts(self.root, self.git_dir)

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        self._tmp.cleanup()

    def test_names_request_exception_skill(self) -> None:
        """block_message names the request-exception skill."""
        msg = gate.block_message(self.facts, "/some/file.py")
        self.assertIn("worktree-warden:request-exception", msg)

    def test_names_finish_exception_skill(self) -> None:
        """block_message names the finish-exception skill."""
        msg = gate.block_message(self.facts, "/some/file.py")
        self.assertIn("worktree-warden:finish-exception", msg)

    def test_does_not_contain_raw_grant_invocation(self) -> None:
        """block_message no longer prints the raw CLI grant invocation."""
        msg = gate.block_message(self.facts, "/some/file.py")
        self.assertNotIn('grant "<why', msg)

    def test_works_without_file_path(self) -> None:
        """block_message handles a None file_path gracefully."""
        msg = gate.block_message(self.facts, None)
        self.assertIn("worktree-warden:request-exception", msg)
        self.assertIn("worktree-warden:finish-exception", msg)


class TeardownModeTest(unittest.TestCase):
    """Teardown mode config read and CLI round-trip."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._xdg = base / "xdg"
        self._git = base / "repo" / ".git"
        self._git.mkdir(parents=True)
        self._prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self._xdg)
        self.common_dir = str(os.path.realpath(str(self._git)))
        self.repo_root = str(base / "repo")
        self.facts = _main_facts(self.repo_root, str(self._git))

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        if self._prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_xdg
        self._tmp.cleanup()

    def _write_user(self, data: dict[str, object]) -> None:
        path = gate.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def _write_project(self, data: dict[str, object]) -> None:
        path = gate.project_config_path(self.repo_root)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_default_is_ask(self) -> None:
        """Default is ask."""
        self.assertEqual(gate.read_teardown_mode(self.facts), "ask")

    def test_user_mode_respected(self) -> None:
        """User mode respected."""
        self._write_user({"teardown_mode": "auto"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "auto")

    def test_all_valid_modes(self) -> None:
        """All valid modes."""
        for mode in gate.VALID_TEARDOWN_MODES:
            self._write_user({"teardown_mode": mode})
            self.assertEqual(gate.read_teardown_mode(self.facts), mode)

    def test_project_overrides_user(self) -> None:
        """Project overrides user."""
        self._write_user({"teardown_mode": "auto"})
        self._write_project({"teardown_mode": "never"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "never")

    def test_invalid_mode_ignored_falls_back_to_default(self) -> None:
        """Invalid mode ignored falls back to default."""
        self._write_user({"teardown_mode": "invalid"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "ask")

    def test_empty_project_config_does_not_override_user(self) -> None:
        """Empty project config does not override user."""
        self._write_user({"teardown_mode": "never"})
        self._write_project({})
        self.assertEqual(gate.read_teardown_mode(self.facts), "never")

    def test_project_invalid_mode_does_not_override_user(self) -> None:
        """Project invalid mode does not override user."""
        self._write_user({"teardown_mode": "auto"})
        self._write_project({"teardown_mode": "bogus"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "auto")

    def test_no_repo_returns_default(self) -> None:
        """No repo returns default."""
        no_repo = gate.GitFacts(False, None, None, False)
        self.assertEqual(gate.read_teardown_mode(no_repo), "ask")


class RotateAuditLogTest(unittest.TestCase):
    """Unit coverage for :func:`gate.rotate_audit_log`'s cascade/retention logic."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "audit.log"

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        self._tmp.cleanup()

    def _write(self, path: Path, size: int, marker: str = "") -> None:
        """Write exactly `size` bytes to path, with an optional leading marker."""
        content = (marker + "\n").encode() if marker else b""
        content += b"a" * max(size - len(content), 0)
        path.write_bytes(content)

    def test_below_threshold_is_a_noop(self) -> None:
        """Below threshold is a noop."""
        self._write(self.path, 100, marker="fresh")
        gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)
        self.assertTrue(self.path.exists())
        self.assertIn(b"fresh", self.path.read_bytes())
        self.assertFalse((self.dir / "audit.log.1").exists())

    def test_single_rotation(self) -> None:
        """Single rotation."""
        self._write(self.path, 1000, marker="oversized")
        gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)
        self.assertFalse(self.path.exists())
        rotated = self.dir / "audit.log.1"
        self.assertTrue(rotated.exists())
        self.assertIn(b"oversized", rotated.read_bytes())

    def test_cascade_of_existing_generations(self) -> None:
        """Cascade of existing generations."""
        (self.dir / "audit.log.1").write_bytes(b"gen1")
        (self.dir / "audit.log.2").write_bytes(b"gen2")
        self._write(self.path, 1000, marker="current")
        gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)
        self.assertEqual((self.dir / "audit.log.1").read_bytes(), b"current\n" + b"a" * (1000 - 8))
        self.assertEqual((self.dir / "audit.log.2").read_bytes(), b"gen1")
        self.assertEqual((self.dir / "audit.log.3").read_bytes(), b"gen2")

    def test_retention_eviction_beyond_backup_count(self) -> None:
        """Retention eviction beyond backup count."""
        for gen in range(1, 6):
            (self.dir / f"audit.log.{gen}").write_bytes(f"gen{gen}".encode())
        self._write(self.path, 1000, marker="current")
        gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)
        self.assertFalse((self.dir / "audit.log.6").exists())
        for gen in range(1, 6):
            self.assertTrue((self.dir / f"audit.log.{gen}").exists())
        self.assertEqual((self.dir / "audit.log.5").read_bytes(), b"gen4")

    def test_spurious_rotation_guard_when_already_fresh_after_lock(self) -> None:
        """A caller that wins the lock but finds the file already rotated must not re-rotate."""
        self._write(self.path, 1000, marker="oversized")

        real_flock = gate.fcntl.flock
        calls: list[int] = []

        def fake_flock(fd: int, op: int) -> None:
            real_flock(fd, op)
            if op == gate.fcntl.LOCK_EX | gate.fcntl.LOCK_NB and not calls:
                calls.append(1)
                # Simulate another process having already rotated between our
                # first stat and lock acquisition.
                self.path.write_bytes(b"already-fresh")

        with unittest.mock.patch.object(gate.fcntl, "flock", side_effect=fake_flock):
            gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)

        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_bytes(), b"already-fresh")
        self.assertFalse((self.dir / "audit.log.1").exists())

    def test_lock_contention_skips_rotation_safely(self) -> None:
        """Lock contention skips rotation safely."""
        self._write(self.path, 1000, marker="oversized")

        with unittest.mock.patch.object(
            gate.fcntl, "flock", side_effect=OSError("locked")
        ):
            gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)

        self.assertTrue(self.path.exists())
        self.assertIn(b"oversized", self.path.read_bytes())
        self.assertFalse((self.dir / "audit.log.1").exists())

    def test_filesystem_error_is_swallowed(self) -> None:
        """Filesystem error is swallowed."""
        self._write(self.path, 1000, marker="oversized")

        with unittest.mock.patch("os.rename", side_effect=PermissionError("denied")):
            gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)

        # No exception raised; original file left in place since the rename failed.
        self.assertTrue(self.path.exists())

    def test_lock_sentinel_never_matches_generation_glob(self) -> None:
        """Lock sentinel never matches generation glob."""
        self._write(self.path, 1000, marker="oversized")
        gate.rotate_audit_log(self.path, max_bytes=1000, backup_count=5)
        sentinel = self.dir / "audit.log.rotate.lock"
        self.assertTrue(sentinel.exists())
        generations = sorted(p.name for p in self.dir.glob("audit.log.[0-9]*"))
        self.assertNotIn(sentinel.name, generations)
        self.assertEqual(generations, ["audit.log.1"])

    def test_log_event_rotates_when_oversized(self) -> None:
        """Log event rotates when oversized."""
        with unittest.mock.patch.object(gate, "user_config_dir", return_value=self.dir):
            self._write(self.path, gate.AUDIT_MAX_BYTES, marker="oversized")
            gate.log_event("land", "test", "/some/file.py")
        self.assertTrue((self.dir / "audit.log.1").exists())
        self.assertIn(b"oversized", (self.dir / "audit.log.1").read_bytes())
        self.assertTrue(self.path.exists())
        self.assertIn(b"land", self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()


class RunTestsOnFinishTest(unittest.TestCase):
    """Teardown mode config read and CLI round-trip."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._xdg = base / "xdg"
        self._git = base / "repo" / ".git"
        self._git.mkdir(parents=True)
        self._prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self._xdg)
        self.common_dir = str(os.path.realpath(str(self._git)))
        self.repo_root = str(base / "repo")
        self.facts = _main_facts(self.repo_root, str(self._git))

    def tearDown(self) -> None:
        if self._prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_xdg
        self._tmp.cleanup()

    def _write_user(self, data: dict[str, object]) -> None:
        path = gate.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def _write_project(self, data: dict[str, object]) -> None:
        path = gate.project_config_path(self.repo_root)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_default_is_off(self) -> None:
        self.assertFalse(gate.read_run_tests_on_finish(self.facts))

    def test_user_enables(self) -> None:
        self._write_user({"run_tests_on_finish": True})
        self.assertTrue(gate.read_run_tests_on_finish(self.facts))

    def test_project_overrides_user(self) -> None:
        self._write_user({"run_tests_on_finish": True})
        self._write_project({"run_tests_on_finish": False})
        self.assertFalse(gate.read_run_tests_on_finish(self.facts))

    def test_non_bool_ignored(self) -> None:
        self._write_user({"run_tests_on_finish": "yes"})
        self.assertFalse(gate.read_run_tests_on_finish(self.facts))

    def test_force_disable_overrides_enabled_setting(self) -> None:
        self._write_user({"run_tests_on_finish": True})
        env = {"WORKTREE_WARDEN_FORCE_DISABLE": "1"}
        self.assertFalse(gate.tests_on_finish_enabled(self.facts, env))
        self.assertTrue(gate.tests_on_finish_enabled(self.facts, {}))
