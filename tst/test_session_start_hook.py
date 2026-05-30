"""Unit tests for the SessionStart hook's aggregate trigger rule.

The detector classifies each worktree (covered in test_check_worktrees); this
covers the policy the hook layers on top: surface the table when >=1 worktree is
actionable — ready to merge, mergeable after a commit, or empty and prunable —
and stay completely silent when every worktree is blocked by a live session.

``gather_worktrees`` shells out to ``git`` and ``claude agents --json``, and the
all-blocked branch cannot be reproduced against a temp repo (a live session
can't be conjured), so it is monkeypatched to return fixtures here.
"""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Iterator
from unittest import TestCase, mock

import check_worktrees as cw
import check_worktrees_hook as hook


def _wt(
    *,
    dirty: bool = False,
    commit_count: int = 0,
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
        session_status="running" if session else "",
        recently_active=recently_active,
    )


@contextlib.contextmanager
def _patch_gather(worktrees: list[cw.Worktree]) -> Iterator[None]:
    """Replace the detector's gather_worktrees with a stub returning fixtures."""

    async def _fake(_cwd: str) -> list[cw.Worktree]:
        return worktrees

    with mock.patch.object(cw, "gather_worktrees", _fake):
        yield


class GetReadyInfoTest(TestCase):
    """get_ready_info counts actionable worktrees and renders only when >=1."""

    def test_mixed_counts_only_actionable_and_renders_table(self) -> None:
        worktrees = [
            _wt(commit_count=2),  # ready
            _wt(dirty=True),  # needs_commit
            _wt(commit_count=0),  # prune
            _wt(commit_count=4, session=True),  # blocked — excluded
        ]
        with _patch_gather(worktrees):
            count, table = hook.get_ready_info("/repo")
        self.assertEqual(count, 3)
        self.assertTrue(table)

    def test_all_blocked_is_silent(self) -> None:
        worktrees = [_wt(commit_count=3, session=True), _wt(dirty=True, session=True)]
        with _patch_gather(worktrees):
            count, table = hook.get_ready_info("/repo")
        self.assertEqual(count, 0)
        self.assertEqual(table, "")

    def test_all_cooldown_is_silent(self) -> None:
        worktrees = [
            _wt(commit_count=2, recently_active=True),
            _wt(dirty=True, recently_active=True),
        ]
        with _patch_gather(worktrees):
            count, table = hook.get_ready_info("/repo")
        self.assertEqual(count, 0)
        self.assertEqual(table, "")

    def test_cooldown_excluded_from_count_when_mixed(self) -> None:
        worktrees = [_wt(commit_count=2), _wt(commit_count=3, recently_active=True)]
        with _patch_gather(worktrees):
            count, table = hook.get_ready_info("/repo")
        self.assertEqual(count, 1)
        self.assertTrue(table)

    def test_no_worktrees_is_silent(self) -> None:
        with _patch_gather([]):
            count, table = hook.get_ready_info("/repo")
        self.assertEqual(count, 0)
        self.assertEqual(table, "")

    def test_lone_prunable_is_actionable(self) -> None:
        with _patch_gather([_wt(commit_count=0)]):
            count, table = hook.get_ready_info("/repo")
        self.assertEqual(count, 1)
        self.assertTrue(table)


class MainEmitTest(TestCase):
    """main() emits a systemMessage only when actionable worktrees exist."""

    def _run_main(self, worktrees: list[cw.Worktree], *, source: str = "startup") -> tuple[int, str]:
        """Drive main() with a stubbed main-worktree check and stdin payload."""
        payload = json.dumps({"source": source, "cwd": "/repo"})
        with (
            mock.patch.object(hook, "is_main_worktree", return_value=True),
            _patch_gather(worktrees),
            mock.patch("sys.stdin", io.StringIO(payload)),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            rc = hook.main()
        return rc, out.getvalue()

    def test_emits_banner_when_actionable(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1)])
        self.assertEqual(rc, 0)
        emitted = json.loads(out)
        self.assertIn("mergeable git worktree", emitted["systemMessage"])

    def test_silent_when_all_blocked(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1, session=True)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_ignores_non_startup_source(self) -> None:
        rc, out = self._run_main([_wt(commit_count=1)], source="compact")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
