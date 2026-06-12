"""Unit tests for the worktree readiness classification and table/JSON output.

The readiness model is pure logic over a Worktree's dirty/commit/session state,
so these construct Worktree instances directly rather than driving git.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

import check_worktrees as cw


def _wt(
    *,
    dirty: bool = False,
    commit_count: int = 0,
    behind: int = 0,
    session: bool = False,
    recently_active: bool = False,
    unreadable: bool = False,
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
        unreadable=unreadable,
    )


class ReadinessTest(unittest.TestCase):
    """The four readiness buckets and their precedence."""

    def test_clean_with_commits_is_ready_to_merge(self) -> None:
        wt = _wt(commit_count=2)
        self.assertEqual(wt.readiness, cw.Readiness.READY)
        self.assertEqual(wt.ready_emoji, "✅")
        self.assertEqual(wt.ready_note, "ready to merge")
        self.assertTrue(wt.is_mergeable)

    def test_unreadable_is_unknown_never_prune(self) -> None:
        # A failed git query defaults dirty=False, commit_count=0 — which would
        # otherwise be PRUNE. Unreadable must override that to UNKNOWN.
        wt = _wt(unreadable=True)
        self.assertEqual(wt.readiness, cw.Readiness.UNKNOWN)
        self.assertFalse(wt.is_mergeable)
        self.assertEqual(wt.ready_note, "state unreadable")

    def test_live_session_outranks_unreadable(self) -> None:
        wt = _wt(unreadable=True, session=True)
        self.assertEqual(wt.readiness, cw.Readiness.BLOCKED)

    def test_dirty_can_merge_after_commit(self) -> None:
        wt = _wt(dirty=True, commit_count=0)
        self.assertEqual(wt.readiness, cw.Readiness.NEEDS_COMMIT)
        self.assertEqual(wt.ready_emoji, "✅")
        self.assertEqual(wt.ready_note, "can merge after commit")
        self.assertTrue(wt.is_mergeable)

    def test_clean_zero_commits_at_base_tip_is_empty(self) -> None:
        wt = _wt(dirty=False, commit_count=0, behind=0)
        self.assertEqual(wt.readiness, cw.Readiness.PRUNE)
        self.assertEqual(wt.ready_emoji, "🧹")
        self.assertEqual(wt.ready_note, "empty, can be pruned")
        self.assertTrue(wt.is_mergeable)

    def test_clean_head_already_in_base_chain_is_merged(self) -> None:
        wt = _wt(dirty=False, commit_count=0, behind=3)
        self.assertEqual(wt.readiness, cw.Readiness.MERGED)
        self.assertEqual(wt.ready_emoji, "🧹")
        self.assertEqual(wt.ready_note, "merged, can be pruned")
        self.assertTrue(wt.is_mergeable)

    def test_dirty_outranks_already_merged(self) -> None:
        wt = _wt(dirty=True, commit_count=0, behind=3)
        self.assertEqual(wt.readiness, cw.Readiness.NEEDS_COMMIT)

    def test_recently_active_is_cooldown_and_not_offered(self) -> None:
        wt = _wt(commit_count=2, recently_active=True)
        self.assertEqual(wt.readiness, cw.Readiness.COOLDOWN)
        self.assertEqual(wt.ready_emoji, "⏳")
        self.assertEqual(wt.ready_note, "active <15m ago")
        self.assertFalse(wt.is_mergeable)

    def test_live_session_outranks_cooldown(self) -> None:
        wt = _wt(recently_active=True, session=True)
        self.assertEqual(wt.readiness, cw.Readiness.BLOCKED)

    def test_cooldown_outranks_git_state(self) -> None:
        wt = _wt(dirty=True, commit_count=5, recently_active=True)
        self.assertEqual(wt.readiness, cw.Readiness.COOLDOWN)
        self.assertFalse(wt.is_mergeable)


class RecencyHelpersTest(unittest.TestCase):
    """The deterministic recency helpers (clock passed in, never read)."""

    _NOW = 1_000_000.0

    def test_recent_true_within_window(self) -> None:
        self.assertTrue(cw._recent(self._NOW - 60, self._NOW))

    def test_recent_false_outside_window(self) -> None:
        self.assertFalse(cw._recent(self._NOW - 1000, self._NOW))

    def test_recent_false_for_zero_mtime(self) -> None:
        self.assertFalse(cw._recent(0.0, self._NOW))

    def test_last_edit_uses_file_mtime_when_dirty(self) -> None:
        wt = _wt(dirty=True)
        wt.file_mtime = 12345.0
        self.assertEqual(cw._last_edit_mtime(wt), 12345.0)

    def test_last_edit_parses_commit_iso_when_clean(self) -> None:
        wt = _wt(commit_count=1)
        wt.last_iso = "2026-05-29T12:08:56-07:00"
        expected = datetime.fromisoformat(wt.last_iso).timestamp()
        self.assertEqual(cw._last_edit_mtime(wt), expected)

    def test_last_edit_zero_on_unparseable_iso(self) -> None:
        wt = _wt(commit_count=1)
        wt.last_iso = "not-a-timestamp"
        self.assertEqual(cw._last_edit_mtime(wt), 0.0)

    def test_last_edit_ignores_inherited_commit_time_when_zero_ahead(self) -> None:
        # A 0-ahead clean worktree's last_iso is the base tip it inherited, not
        # its own activity — it must not register as a last-edit signal.
        wt = _wt(commit_count=0)
        wt.last_iso = "2026-05-29T12:08:56-07:00"
        self.assertEqual(cw._last_edit_mtime(wt), 0.0)

    def test_encode_project_dir_matches_observed_claude_mapping(self) -> None:
        # Pins the observed Claude Code cwd -> ~/.claude/projects/<dir> mapping,
        # verified against the live filesystem: every "/" and "." becomes "-",
        # a leading "/" yields a leading "-", "/.claude" collapses to "--claude",
        # and literal hyphens in path segments (my-app, feat-x) are preserved.
        # The mapping is lossy/forward-only and external — if Claude changes it,
        # re-pin this against a fresh real project-dir name.
        encoded = cw._encode_project_dir("/Users/dev/code/my-app/.claude/worktrees/feat-x")
        self.assertEqual(encoded, "-Users-dev-code-my-app--claude-worktrees-feat-x")

    def test_live_session_is_blocked(self) -> None:
        wt = _wt(commit_count=3, session=True)
        self.assertEqual(wt.readiness, cw.Readiness.BLOCKED)
        self.assertEqual(wt.ready_emoji, "❌")
        self.assertEqual(wt.ready_note, "live session")
        self.assertFalse(wt.is_mergeable)

    def test_session_takes_precedence_over_dirty_and_commits(self) -> None:
        wt = _wt(dirty=True, commit_count=5, session=True)
        self.assertEqual(wt.readiness, cw.Readiness.BLOCKED)
        self.assertFalse(wt.is_mergeable)

    def test_dirty_takes_precedence_over_commits_ahead(self) -> None:
        wt = _wt(dirty=True, commit_count=5)
        self.assertEqual(wt.readiness, cw.Readiness.NEEDS_COMMIT)


class RenderTableTest(unittest.TestCase):
    """The rendered table's columns and emoji alignment."""

    def test_headers_reflect_new_columns(self) -> None:
        table = cw.render_table([_wt(commit_count=1)])
        self.assertIn("Ready?", table)
        self.assertIn("Note", table)
        self.assertIn("Last edit", table)
        self.assertNotIn("Branch", table)
        self.assertNotIn("Session", table)
        self.assertNotIn("Last modified", table)

    def test_emoji_cell_does_not_break_border_alignment(self) -> None:
        table = cw.render_table(
            [_wt(commit_count=1), _wt(session=True), _wt(recently_active=True)]
        )
        body = [ln for ln in table.splitlines() if ln.startswith("│")]
        widths = {cw._display_width(ln) for ln in body}
        self.assertEqual(len(widths), 1, f"misaligned rows: {body}")


class ToJsonTest(unittest.TestCase):
    """The JSON payload carries the readiness fields the skill routes on."""

    def test_json_includes_readiness_fields_and_branch(self) -> None:
        payload = json.loads(cw.to_json([_wt(dirty=True)]))
        entry = payload[0]
        self.assertEqual(entry["branch"], "feat-x")
        self.assertEqual(entry["category"], "needs_commit")
        self.assertEqual(entry["note"], "can merge after commit")
        self.assertTrue(entry["ready"])

    def test_blocked_worktree_is_not_ready_in_json(self) -> None:
        payload = json.loads(cw.to_json([_wt(commit_count=1, session=True)]))
        self.assertFalse(payload[0]["ready"])
        self.assertEqual(payload[0]["category"], "blocked")

    def test_cooldown_worktree_in_json(self) -> None:
        payload = json.loads(cw.to_json([_wt(commit_count=1, recently_active=True)]))
        self.assertEqual(payload[0]["category"], "cooldown")
        self.assertTrue(payload[0]["recently_active"])
        self.assertFalse(payload[0]["ready"])


if __name__ == "__main__":
    unittest.main()
