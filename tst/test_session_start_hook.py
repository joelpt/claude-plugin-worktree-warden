"""Unit tests for the SessionStart hook's display-mode policy and banner.

The detector classifies each worktree (covered in test_check_worktrees); this
covers the policy the hook layers on top — what the ``startup_display`` mode
(mergeable / always / never) decides to surface, how the banner reads, and
whether the enforcement ``additionalInformation`` directive is injected.

``build_banner`` is pure over a list of Worktree fixtures, so the modes are
exercised without driving git.  ``main`` is driven with its git-touching seams
(``is_main_worktree``, ``get_gate_state``, ``gather``) stubbed.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import check_worktrees as cw
import check_worktrees_hook as hook


def _wt(
    *,
    dirty: bool = False,
    commit_count: int = 0,
    behind: int = 0,
    session: bool = False,
    recently_active: bool = False,
) -> cw.Worktree:
    """Build a Worktree fixture with only the readiness-relevant fields set."""
    return cw.Worktree(
        path="/repo/.claude/worktrees/feat-x",
        branch="feat-x",
        head="deadbeef",
        dirty=dirty,
        commit_count=commit_count,
        behind=behind,
        session_status="running" if session else "",
        recently_active=recently_active,
    )


class BuildBannerTest(TestCase):
    """build_banner decides show/silent per mode and composes the message."""

    def test_never_mode_is_silent_even_with_worktrees(self) -> None:
        self.assertIsNone(hook.build_banner([_wt(commit_count=1)], "never"))

    def test_no_worktrees_is_silent(self) -> None:
        self.assertIsNone(hook.build_banner([], "always"))
        self.assertIsNone(hook.build_banner([], "mergeable"))

    def test_mergeable_mode_silent_when_only_held_back(self) -> None:
        held = [_wt(commit_count=1, recently_active=True), _wt(session=True)]
        self.assertIsNone(hook.build_banner(held, "mergeable"))

    def test_mergeable_mode_shows_when_one_offerable(self) -> None:
        banner = hook.build_banner([_wt(commit_count=2), _wt(session=True)], "mergeable")
        assert banner is not None
        self.assertIn("1 mergeable", banner)
        self.assertIn("/merge-worktrees", banner)

    def test_always_mode_shows_only_cooldown(self) -> None:
        banner = hook.build_banner([_wt(commit_count=1, recently_active=True)], "always")
        assert banner is not None
        self.assertIn("1 on cooldown", banner)
        self.assertIn("⏳", banner)
        self.assertNotIn("mergeable", banner)

    def test_always_mode_shows_only_live_session(self) -> None:
        banner = hook.build_banner([_wt(commit_count=1, session=True)], "always")
        assert banner is not None
        self.assertIn("1 in a live session", banner)
        self.assertIn("live session", banner)

    def test_always_mode_full_breakdown_and_table(self) -> None:
        worktrees = [
            _wt(commit_count=2),  # ready -> mergeable
            _wt(dirty=True),  # needs_commit -> mergeable
            _wt(commit_count=0, behind=3),  # merged -> mergeable
            _wt(commit_count=1, recently_active=True),  # cooldown
            _wt(commit_count=1, session=True),  # blocked
        ]
        banner = hook.build_banner(worktrees, "always")
        assert banner is not None
        self.assertIn("5 git worktrees in this repo", banner)
        self.assertIn("3 mergeable", banner)
        self.assertIn("1 on cooldown", banner)
        self.assertIn("1 in a live session", banner)
        self.assertIn("Ready?", banner)  # the rendered table

    def test_singular_noun_for_one_worktree(self) -> None:
        banner = hook.build_banner([_wt(commit_count=1)], "always")
        assert banner is not None
        self.assertIn("1 git worktree in this repo", banner)


class MainEmitTest(TestCase):
    """main() honors the resolved mode, the startup/resume gate, and enforcement."""

    def _run_main(
        self,
        worktrees: list[cw.Worktree],
        *,
        mode: str = "always",
        enforcement: bool = False,
        disabled_scope: str | None = None,
        source: str = "startup",
        is_main: bool = True,
    ) -> tuple[int, str]:
        """Drive main() with its git-touching seams stubbed and stdin supplied."""
        payload = json.dumps({"source": source, "cwd": "/repo"})
        with (
            mock.patch.object(hook, "is_main_worktree", return_value=is_main),
            mock.patch.object(
                hook, "get_gate_state", return_value=(mode, enforcement, disabled_scope)
            ),
            mock.patch.object(hook, "gather", return_value=worktrees),
            mock.patch("sys.stdin", io.StringIO(payload)),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            rc = hook.main()
        return rc, out.getvalue()

    def test_emits_banner_in_always_mode(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1, session=True)], mode="always")
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertIn("git worktree", emitted["systemMessage"])

    def test_never_mode_no_enforcement_is_silent(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1)], mode="never", enforcement=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_never_mode_with_enforcement_emits_additional_information(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1)], mode="never", enforcement=True)
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertNotIn("systemMessage", emitted)
        self.assertIn("MANDATORY", emitted["additionalInformation"])
        self.assertIn("EnterWorktree", emitted["additionalInformation"])

    def test_enforcement_emits_additional_information_alongside_banner(self) -> None:
        rc, out = self._run_main(
            [_wt(commit_count=1)], mode="always", enforcement=True
        )
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertIn("systemMessage", emitted)
        self.assertIn("MANDATORY", emitted["additionalInformation"])

    def test_mergeable_mode_silent_when_all_held_back_no_enforcement(self) -> None:
        rc, out = self._run_main([_wt(session=True)], mode="mergeable", enforcement=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_ignores_non_startup_source(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1)], source="compact")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_silent_when_not_main_worktree(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1)], is_main=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_project_disabled_emits_warning_in_system_message(self) -> None:
        rc, out = self._run_main(
            [_wt(commit_count=1)],
            mode="always",
            enforcement=False,
            disabled_scope="project",
        )
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertIn("⚠️", emitted["systemMessage"])
        self.assertIn("project config", emitted["systemMessage"].lower())
        self.assertNotIn("additionalInformation", emitted)

    def test_project_disabled_warning_prepended_to_banner(self) -> None:
        rc, out = self._run_main(
            [_wt(commit_count=1)],
            mode="always",
            enforcement=False,
            disabled_scope="project",
        )
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertTrue(emitted["systemMessage"].startswith("⚠️"))

    def test_project_disabled_warning_not_suppressed_by_never_mode(self) -> None:
        rc, out = self._run_main(
            [_wt(commit_count=1)],
            mode="never",
            enforcement=False,
            disabled_scope="project",
        )
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertIn("⚠️", emitted["systemMessage"])


class GateLoadErrorSentinelTest(TestCase):
    """main() surfaces gate-load-error sentinel as a high-priority advisory."""

    def _run_main_patched(
        self,
        *,
        load_error_advisory: str,
        mode: str = "never",
        enforcement: bool = False,
        worktrees: list[cw.Worktree] | None = None,
    ) -> tuple[int, str]:
        """Drive main() with _read_load_error_advisory stubbed to return a fixed value."""
        payload = json.dumps({"source": "startup", "cwd": "/repo"})
        with (
            mock.patch.object(hook, "is_main_worktree", return_value=True),
            mock.patch.object(
                hook, "get_gate_state", return_value=(mode, enforcement, None)
            ),
            mock.patch.object(hook, "gather", return_value=worktrees or []),
            mock.patch.object(hook, "_read_load_error_advisory", return_value=load_error_advisory),
            mock.patch("sys.stdin", io.StringIO(payload)),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            rc = hook.main()
        return rc, out.getvalue()

    def test_gate_load_error_advisory_prepended_to_system_message(self) -> None:
        advisory = "⚠️ gate-load-error: worktree_gate failed to import"
        rc, out = self._run_main_patched(load_error_advisory=advisory, mode="never")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip(), "Expected output when sentinel advisory present")
        emitted = json.loads(out)
        self.assertIn("systemMessage", emitted)
        self.assertIn("gate-load-error", emitted["systemMessage"])

    def test_gate_load_error_advisory_absent_when_no_sentinel(self) -> None:
        rc, out = self._run_main_patched(load_error_advisory="", mode="never")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_gate_load_error_advisory_absent_when_git_common_dir_none(self) -> None:
        advisory = hook._read_load_error_advisory(None)
        self.assertEqual(advisory, "")

    def test_read_load_error_advisory_returns_string_when_sentinel_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            warden_dir = Path(tmpdir) / "worktree-warden"
            warden_dir.mkdir()
            (warden_dir / "gate-load-error").write_text("2026-01-01T00:00:00Z enforce_worktree_hook\n")
            advisory = hook._read_load_error_advisory(tmpdir)
            self.assertTrue(advisory, "Expected non-empty advisory string")
            self.assertIn("gate-load-error", advisory)

    def test_read_load_error_advisory_returns_empty_when_no_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            warden_dir = Path(tmpdir) / "worktree-warden"
            warden_dir.mkdir()
            advisory = hook._read_load_error_advisory(tmpdir)
            self.assertEqual(advisory, "")
