"""Unit tests for the worktree-gate policy and settings resolution."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
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
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.git_dir = os.path.join(self.root, ".git")
        os.makedirs(self.git_dir, exist_ok=True)
        self.facts = _main_facts(self.root, self.git_dir)
        self.target = os.path.join(self.root, "src", "main.py")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_user_disabled_allows(self) -> None:
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
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=0.0,
            disabled_scope="project",
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_non_repo_allows(self) -> None:
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
        d = gate.decide(
            file_path=None,
            facts=self.facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_outside_checkout_allows(self) -> None:
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
        d = gate.decide(
            file_path=os.path.join(self.git_dir, "config"),
            facts=self.facts,
            now=0.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertTrue(d.allow)

    def test_main_checkout_without_grant_blocks(self) -> None:
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=None,
        )
        self.assertFalse(d.allow)

    def test_main_checkout_with_expired_grant_blocks(self) -> None:
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=999.0,
        )
        self.assertFalse(d.allow)

    def test_main_checkout_with_active_grant_allows(self) -> None:
        d = gate.decide(
            file_path=self.target,
            facts=self.facts,
            now=1000.0,
            disabled_scope=None,
            grant_expires_at=1500.0,
        )
        self.assertTrue(d.allow)
        self.assertTrue(d.log_grant_use)


class DurationTest(unittest.TestCase):
    """Duration parsing and clamping."""

    def test_bare_seconds(self) -> None:
        self.assertEqual(gate.parse_duration("900"), 900)

    def test_minutes_suffix(self) -> None:
        self.assertEqual(gate.parse_duration("15m"), 900)

    def test_hours_suffix(self) -> None:
        self.assertEqual(gate.parse_duration("1h"), 3600)

    def test_below_min_clamps_up(self) -> None:
        self.assertEqual(gate.parse_duration("30s"), gate.MIN_WINDOW_SECONDS)

    def test_above_max_clamps_down(self) -> None:
        self.assertEqual(gate.parse_duration("100h"), gate.MAX_WINDOW_SECONDS)

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            gate.parse_duration("soon")


class SettingsTest(unittest.TestCase):
    """Scope-resolved settings with project overriding user."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._xdg = base / "xdg"
        self._git = base / "repo" / ".git"
        self._git.mkdir(parents=True)
        self._prev_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self._xdg)
        self.common_dir = os.path.realpath(str(self._git))
        self.facts = _main_facts(str(base / "repo"), str(self._git))

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
        path = gate.project_config_path(self.common_dir)
        assert path is not None
        path.write_text(json.dumps(data))

    def test_defaults(self) -> None:
        settings = gate.resolve_settings(self.facts)
        self.assertIsNone(settings.disabled_scope)
        self.assertEqual(settings.window_seconds, gate.DEFAULT_WINDOW_SECONDS)
        self.assertEqual(settings.startup_display, gate.DEFAULT_STARTUP_DISPLAY)

    def test_user_startup_display(self) -> None:
        self._write_user({"startup_display": "never"})
        self.assertEqual(gate.resolve_settings(self.facts).startup_display, "never")

    def test_project_startup_display_overrides_user(self) -> None:
        self._write_user({"startup_display": "never"})
        self._write_project({"startup_display": "mergeable"})
        self.assertEqual(
            gate.resolve_settings(self.facts).startup_display, "mergeable"
        )

    def test_invalid_startup_display_falls_back_to_default(self) -> None:
        self._write_user({"startup_display": "bogus"})
        self.assertEqual(
            gate.resolve_settings(self.facts).startup_display,
            gate.DEFAULT_STARTUP_DISPLAY,
        )

    def test_user_opt_out(self) -> None:
        self._write_user({"enforce": False})
        self.assertEqual(gate.resolve_settings(self.facts).disabled_scope, "user")

    def test_project_overrides_user_opt_out(self) -> None:
        self._write_user({"enforce": False})
        self._write_project({"enforce": False})
        self.assertEqual(
            gate.resolve_settings(self.facts).disabled_scope, "project"
        )

    def test_project_enable_overrides_user_disable(self) -> None:
        self._write_user({"enforce": False})
        self._write_project({"enforce": True})
        self.assertIsNone(gate.resolve_settings(self.facts).disabled_scope)

    def test_project_disable_overrides_user_enable(self) -> None:
        self._write_user({"enforce": True})
        self._write_project({"enforce": False})
        self.assertEqual(
            gate.resolve_settings(self.facts).disabled_scope, "project"
        )

    def test_user_window(self) -> None:
        self._write_user({"window_seconds": 1800})
        self.assertEqual(gate.resolve_settings(self.facts).window_seconds, 1800)

    def test_project_window_overrides_user(self) -> None:
        self._write_user({"window_seconds": 1800})
        self._write_project({"window_seconds": 600})
        self.assertEqual(gate.resolve_settings(self.facts).window_seconds, 600)

    def test_malformed_config_falls_back_to_defaults(self) -> None:
        path = gate.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        settings = gate.resolve_settings(self.facts)
        self.assertIsNone(settings.disabled_scope)
        self.assertEqual(settings.window_seconds, gate.DEFAULT_WINDOW_SECONDS)


class GrantExpiryTest(unittest.TestCase):
    """Reading the active-exception token."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.common = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_token_is_none(self) -> None:
        self.assertIsNone(gate.read_grant_expiry(self.common))

    def test_valid_token_returns_expiry(self) -> None:
        path = gate.grant_path(self.common)
        assert path is not None
        path.write_text(json.dumps({"expires_at": 1234.5, "reason": "x"}))
        self.assertEqual(gate.read_grant_expiry(self.common), 1234.5)

    def test_malformed_token_is_none(self) -> None:
        path = gate.grant_path(self.common)
        assert path is not None
        path.write_text("garbage")
        self.assertIsNone(gate.read_grant_expiry(self.common))


class TeardownModeTest(unittest.TestCase):
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
        self.facts = _main_facts(str(base / "repo"), str(self._git))

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
        path = gate.project_config_path(self.common_dir)
        assert path is not None
        path.write_text(json.dumps(data))

    def test_default_is_ask(self) -> None:
        self.assertEqual(gate.read_teardown_mode(self.facts), "ask")

    def test_user_mode_respected(self) -> None:
        self._write_user({"teardown_mode": "auto"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "auto")

    def test_all_valid_modes(self) -> None:
        for mode in gate.VALID_TEARDOWN_MODES:
            self._write_user({"teardown_mode": mode})
            self.assertEqual(gate.read_teardown_mode(self.facts), mode)

    def test_project_overrides_user(self) -> None:
        self._write_user({"teardown_mode": "auto"})
        self._write_project({"teardown_mode": "never"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "never")

    def test_invalid_mode_ignored_falls_back_to_default(self) -> None:
        self._write_user({"teardown_mode": "invalid"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "ask")

    def test_empty_project_config_does_not_override_user(self) -> None:
        self._write_user({"teardown_mode": "never"})
        self._write_project({})
        self.assertEqual(gate.read_teardown_mode(self.facts), "never")

    def test_project_invalid_mode_does_not_override_user(self) -> None:
        self._write_user({"teardown_mode": "auto"})
        self._write_project({"teardown_mode": "bogus"})
        self.assertEqual(gate.read_teardown_mode(self.facts), "auto")

    def test_no_repo_returns_default(self) -> None:
        no_repo = gate.GitFacts(False, None, None, False)
        self.assertEqual(gate.read_teardown_mode(no_repo), "ask")


if __name__ == "__main__":
    unittest.main()
