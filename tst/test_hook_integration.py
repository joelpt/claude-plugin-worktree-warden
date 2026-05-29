"""End-to-end tests: drive the real hook subprocess against a temp repo.

These cover what the pure unit tests cannot: that the hook script loads the
gate module, returns exit code 2 to block (the bypassPermissions-safe path),
and that the grant/finished/disable CLI round-trips actually flip the gate.
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
_HOOK = _ROOT / "hooks" / "enforce_worktree_hook.py"
_GATE = _ROOT / "scripts" / "worktree_gate.py"


class HookIntegrationTest(unittest.TestCase):
    """Drive the hook and CLI as real subprocesses."""

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
        self.target = str(self.repo / "src" / "main.py")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
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

    def _hook(self, cwd: str, file_path: str, tool: str = "Edit") -> tuple[int, str]:
        payload = json.dumps(
            {"tool_name": tool, "cwd": cwd, "tool_input": {"file_path": file_path}}
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

    def _cli(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(_GATE), *args],
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env=self._env(),
        )
        return proc.stdout

    def test_main_checkout_edit_is_blocked(self) -> None:
        rc, stderr = self._hook(str(self.repo), self.target)
        self.assertEqual(rc, 2)
        self.assertIn("Worktree gate", stderr)

    def test_non_edit_tool_ignored(self) -> None:
        rc, _ = self._hook(str(self.repo), self.target, tool="Bash")
        self.assertEqual(rc, 0)

    def test_outside_repo_is_allowed(self) -> None:
        rc, _ = self._hook(str(self.repo), os.path.join(self._tmp.name, "x.py"))
        self.assertEqual(rc, 0)

    def test_grant_then_allowed_then_finished_blocks(self) -> None:
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 2)
        self._cli("grant", "resolving", "a", "conflict")
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 0)
        self._cli("finished")
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 2)

    def test_project_disable_then_enable(self) -> None:
        self._cli("disable")
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 0)
        self._cli("enable")
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 2)

    def test_user_disable_allows(self) -> None:
        self._cli("disable", "--user")
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 0)

    def test_linked_worktree_is_allowed(self) -> None:
        wt = Path(self._tmp.name) / "wt"
        self._git("worktree", "add", "-b", "feature", str(wt))
        rc, _ = self._hook(str(wt), str(wt / "src" / "main.py"))
        self.assertEqual(rc, 0)

    def test_set_window_persists_to_status(self) -> None:
        self._cli("set-window", "60")
        self.assertIn("1 min", self._cli("status"))

    def _write_grant(self, expires_at: float) -> None:
        token = self.repo / ".git" / "worktree-gate-grant.json"
        token.write_text(json.dumps({"reason": "x", "expires_at": expires_at}))

    def test_active_grant_token_allows(self) -> None:
        self._write_grant(9_999_999_999.0)
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 0)

    def test_expired_grant_token_blocks(self) -> None:
        self._write_grant(1.0)
        self.assertEqual(self._hook(str(self.repo), self.target)[0], 2)


if __name__ == "__main__":
    unittest.main()
