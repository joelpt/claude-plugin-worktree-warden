"""Tests for the PostToolUse(EnterWorktree) worktree-locking hook.

Drives ``lock_worktree_hook.main()`` in-process (unit level, unlike the
end-to-end ``test_guard_destruction_hook.py`` subprocess tests) since this
hook has no gate-load-failure fallback path to exercise as a subprocess.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lock_worktree_hook as h


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _is_locked(path: Path, repo: Path) -> bool:
    """Return whether ``path`` shows a ``locked`` porcelain record in ``repo``."""
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    target = f"worktree {os.path.realpath(path)}"
    blocks = out.split("\n\n")
    for block in blocks:
        if target in block.splitlines():
            return any(line.startswith("locked") for line in block.splitlines())
    return False


def _run_main(payload: dict[str, object]) -> int:
    """Feed ``payload`` to the hook's ``main()`` via a patched stdin."""
    with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
        return h.main()


class LockWorktreeHookTest(unittest.TestCase):
    """The hook locks a freshly-created worktree and fails open otherwise."""

    def setUp(self) -> None:
        """Build a primary repo with one linked worktree to lock."""
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

    def tearDown(self) -> None:
        """Discard the temporary repo tree."""
        self._tmp.cleanup()

    def test_locks_newly_created_worktree_via_cwd(self) -> None:
        """A creation payload with no tool_response locks via payload['cwd']."""
        payload = {
            "tool_name": "EnterWorktree",
            "cwd": str(self.wt),
            "tool_input": {"name": "feat"},
        }
        code = _run_main(payload)
        self.assertEqual(code, 0)
        self.assertTrue(_is_locked(self.wt, self.repo))

    def test_locks_newly_created_worktree_via_tool_response_path(self) -> None:
        """A tool_response['path'] takes priority over a stale/unrelated cwd."""
        payload = {
            "tool_name": "EnterWorktree",
            "cwd": str(self.base),  # deliberately NOT the worktree path
            "tool_input": {"name": "feat"},
            "tool_response": {"path": str(self.wt)},
        }
        code = _run_main(payload)
        self.assertEqual(code, 0)
        self.assertTrue(_is_locked(self.wt, self.repo))

    def test_noop_for_entering_an_existing_worktree_by_path(self) -> None:
        """Switching into an existing worktree (tool_input.path) never locks."""
        payload = {
            "tool_name": "EnterWorktree",
            "cwd": str(self.wt),
            "tool_input": {"path": str(self.wt)},
        }
        code = _run_main(payload)
        self.assertEqual(code, 0)
        self.assertFalse(_is_locked(self.wt, self.repo))

    def test_noop_for_other_tool_names(self) -> None:
        """Any tool other than EnterWorktree is ignored entirely."""
        payload = {"tool_name": "Bash", "cwd": str(self.wt), "tool_input": {}}
        code = _run_main(payload)
        self.assertEqual(code, 0)
        self.assertFalse(_is_locked(self.wt, self.repo))

    def test_fails_open_on_bad_stdin(self) -> None:
        """Malformed stdin never raises; the hook always exits 0."""
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(h.main(), 0)

    def test_fails_open_when_git_lock_errors(self) -> None:
        """A path with nothing git-related to lock still exits 0, never raises."""
        payload = {
            "tool_name": "EnterWorktree",
            "cwd": str(self.base / "does-not-exist"),
            "tool_input": {"name": "feat"},
        }
        code = _run_main(payload)
        self.assertEqual(code, 0)

    def test_logs_diagnostic_to_stderr_when_lock_fails(self) -> None:
        """A nonzero `git worktree lock` exit is surfaced on stderr, not silent."""
        payload = {
            "tool_name": "EnterWorktree",
            "cwd": str(self.base / "does-not-exist"),
            "tool_input": {"name": "feat"},
        }
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch("sys.stderr", io.StringIO()) as fake_stderr:
                code = h.main()
        self.assertEqual(code, 0)
        self.assertIn("failed to lock", fake_stderr.getvalue())
        self.assertIn(str(self.base / "does-not-exist"), fake_stderr.getvalue())

    def test_no_stderr_diagnostic_when_lock_succeeds(self) -> None:
        """A successful lock writes nothing to stderr."""
        payload = {
            "tool_name": "EnterWorktree",
            "cwd": str(self.wt),
            "tool_input": {"name": "feat"},
        }
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch("sys.stderr", io.StringIO()) as fake_stderr:
                code = h.main()
        self.assertEqual(code, 0)
        self.assertEqual(fake_stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
