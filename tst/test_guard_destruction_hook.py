"""End-to-end tests driving the real guard_destruction_hook subprocess.

Covers what the pure-module tests cannot: that the hook script loads, returns
exit code 2 to block (the bypassPermissions-safe path), and exit 0 to allow.
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
_HOOK = _ROOT / "hooks" / "guard_destruction_hook.py"


class GuardHookTest(unittest.TestCase):
    """Drive the destruction-guard hook as a real subprocess."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        self.wt = self.base / "wt"
        self._git("worktree", "add", "-b", "feat", str(self.wt))

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
        return {**os.environ, "CLAUDE_PLUGIN_ROOT": str(_ROOT)}

    def _run(self, command: str, cwd: str) -> tuple[int, str]:
        payload = json.dumps(
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
        )
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=self._env(),
        )
        return proc.returncode, proc.stderr

    def test_blocks_unlanded_worktree_remove_with_exit_2(self) -> None:
        (self.wt / "w.txt").write_text("work\n")
        self._git("add", "w.txt", cwd=self.wt)
        self._git("commit", "-m", "work", cwd=self.wt)
        code, err = self._run(
            f"git worktree remove --force {self.wt}", str(self.repo)
        )
        self.assertEqual(code, 2)
        self.assertIn("worktree-warden blocked", err)

    def test_allows_harmless_command_with_exit_0(self) -> None:
        code, _ = self._run("git status", str(self.repo))
        self.assertEqual(code, 0)

    def test_allows_clean_landed_remove(self) -> None:
        code, _ = self._run(f"git worktree remove {self.wt}", str(self.repo))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
