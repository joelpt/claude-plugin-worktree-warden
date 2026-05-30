"""Tests for the auto-teardown Stop hook.

Drives the hook as a real subprocess against a temp git repo with a linked
worktree, verifying the JSON decision output under the key eligibility conditions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _ROOT / "hooks" / "auto_teardown_hook.py"
_GATE = _ROOT / "scripts" / "worktree_gate.py"


class AutoTeardownHookTest(unittest.TestCase):
    """Drive the Stop hook subprocess against a temp git repo + linked worktree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.xdg = base / "xdg"
        self._git("init")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        self.wt = base / "wt"
        self._git("worktree", "add", "-b", "feature", str(self.wt))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.xdg),
            "CLAUDE_PLUGIN_ROOT": str(_ROOT),
        }

    def _hook(self, payload: dict[str, object]) -> tuple[int, str]:
        """Run the hook with payload on stdin; return (returncode, stdout)."""
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self._env(),
        )
        return proc.returncode, proc.stdout

    def _add_commit(self, cwd: Path, msg: str = "work") -> None:
        """Commit a new file in the given worktree."""
        f = cwd / f"{msg}.txt"
        f.write_text(f"{msg}\n")
        subprocess.run(
            ["git", "-C", str(cwd), "add", str(f)],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(cwd), "commit", "-m", msg],
            check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    def _set_teardown_mode(self, mode: str, scope: str = "--user") -> None:
        subprocess.run(
            [sys.executable, str(_GATE), "teardown-mode", mode, scope],
            check=True, capture_output=True, env=self._env(),
            cwd=str(self.repo),
        )

    # ── loop guard ────────────────────────────────────────────────────────────

    def test_stop_hook_active_exits_silently(self) -> None:
        self._add_commit(self.wt)
        rc, out = self._hook({"stop_hook_active": True, "cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    # ── eligibility gates ─────────────────────────────────────────────────────

    def test_main_checkout_is_silent(self) -> None:
        self._add_commit(self.repo)
        rc, out = self._hook({"cwd": str(self.repo), "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_mode_never_is_silent(self) -> None:
        self._add_commit(self.wt)
        self._set_teardown_mode("never")
        rc, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_no_commits_no_dirty_is_silent(self) -> None:
        # Fresh worktree: 0 commits ahead, clean
        rc, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_commit_only_mode_silent_when_no_dirty_files(self) -> None:
        # commit-only only makes sense for dirty work; commits-ahead with clean
        # tree has nothing to commit, so the hook should be silent.
        self._add_commit(self.wt)
        self._set_teardown_mode("commit-only")
        rc, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    # ── trigger conditions ────────────────────────────────────────────────────

    def test_committed_work_emits_block(self) -> None:
        self._add_commit(self.wt)
        rc, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("reason", data)

    def test_dirty_only_emits_block(self) -> None:
        (self.wt / "dirty.txt").write_text("dirty\n")
        rc, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")

    def test_reason_mentions_branch(self) -> None:
        self._add_commit(self.wt)
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertIn("feature", data["reason"])

    # ── mode-specific reason content ──────────────────────────────────────────

    def test_ask_mode_reason(self) -> None:
        self._add_commit(self.wt)
        self._set_teardown_mode("ask")
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertIn("ask", data["reason"])
        self.assertIn("AskUserQuestion", data["reason"])

    def test_auto_mode_reason(self) -> None:
        self._add_commit(self.wt)
        self._set_teardown_mode("auto")
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertIn("auto", data["reason"])
        self.assertIn("merge-worktrees", data["reason"])

    def test_always_mode_reason(self) -> None:
        self._add_commit(self.wt)
        self._set_teardown_mode("always")
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertIn("always", data["reason"])
        self.assertIn("tested", data["reason"])

    def test_commit_only_mode_reason(self) -> None:
        # commit-only fires only when dirty files exist (not for commits-ahead alone)
        (self.wt / "dirty.txt").write_text("dirty\n")
        self._set_teardown_mode("commit-only")
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertIn("commit-only", data["reason"])
        self.assertNotIn("merge-worktrees", data["reason"])

    # ── debounce ──────────────────────────────────────────────────────────────

    def test_same_commit_count_debounces(self) -> None:
        self._add_commit(self.wt)
        # First call triggers
        _, out1 = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertIn("block", out1)
        # Same session, same commit count → silent
        rc2, out2 = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertEqual(rc2, 0)
        self.assertEqual(out2.strip(), "")

    def test_new_commit_resets_debounce(self) -> None:
        self._add_commit(self.wt)
        # First call triggers
        self._hook({"cwd": str(self.wt), "session_id": "s1"})
        # Add a second commit
        self._add_commit(self.wt, msg="more-work")
        # Should trigger again
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")

    def test_different_session_is_independent(self) -> None:
        self._add_commit(self.wt)
        self._hook({"cwd": str(self.wt), "session_id": "s1"})
        # Different session should trigger even with same commit count
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s2"})
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")

    def test_pushed_branch_still_emits_block(self) -> None:
        """A branch pushed to a bare remote must not be silenced by @{upstream}."""
        bare = Path(self._tmp.name) / "bare.git"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        self._git("remote", "add", "origin", str(bare))
        self._git("push", "-u", "origin", "main")
        self._git("push", "origin", "feature", cwd=self.wt)
        self._git("branch", "--set-upstream-to=origin/feature", "feature", cwd=self.wt)
        self._add_commit(self.wt)
        _, out = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")

    def test_dirty_only_refires_after_debounce_window(self) -> None:
        """Dirty-only worktrees (0 commits) must re-fire after the debounce window."""
        (self.wt / "dirty.txt").write_text("dirty\n")
        # First fire
        _, out1 = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        self.assertIn("block", out1)

        # Locate the state file and backdate it to simulate the window passing
        result = subprocess.run(
            ["git", "-C", str(self.wt), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        )
        git_common_dir = result.stdout.strip()
        state_file = Path(git_common_dir) / "auto-teardown-s1.json"
        state = json.loads(state_file.read_text())
        state["last_fire_time"] = 0.0  # epoch = definitely past the window
        state_file.write_text(json.dumps(state))

        # Should re-fire after window passes
        _, out2 = self._hook({"cwd": str(self.wt), "session_id": "s1"})
        data = json.loads(out2)
        self.assertEqual(data["decision"], "block")

    # ── failure safety ────────────────────────────────────────────────────────

    def test_malformed_stdin_exits_0(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            env=self._env(),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_empty_stdin_exits_0(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input="",
            capture_output=True,
            text=True,
            env=self._env(),
        )
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
