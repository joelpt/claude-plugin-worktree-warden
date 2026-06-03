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


class UnbornHeadTest(unittest.TestCase):
    """A freshly-init'd repo with no commits cannot host a worktree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.xdg = base / "xdg"
        subprocess.run(
            ["git", "-C", str(self.repo), "init"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.target = str(self.repo / "README.md")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.xdg),
            "CLAUDE_PLUGIN_ROOT": str(_ROOT),
        }

    def _hook(self, file_path: str) -> subprocess.CompletedProcess[str]:
        payload = json.dumps(
            {"tool_name": "Edit", "cwd": str(self.repo), "tool_input": {"file_path": file_path}}
        )
        return subprocess.run(
            [sys.executable, str(_HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env=self._env(),
        )

    def test_unborn_head_edit_is_allowed_with_notice(self) -> None:
        proc = self._hook(self.target)
        self.assertEqual(proc.returncode, 0)
        # Best-effort one-time notice rides the allow on stdout.
        emitted = json.loads(proc.stdout)
        self.assertEqual(
            emitted["hookSpecificOutput"]["hookEventName"], "PreToolUse"
        )
        self.assertEqual(
            emitted["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        self.assertIn(
            "commit", emitted["hookSpecificOutput"]["additionalContext"].lower()
        )

    def test_notice_is_one_time_per_repo(self) -> None:
        self.assertEqual(self._hook(self.target).returncode, 0)
        second = self._hook(self.target)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout.strip(), "")

    def test_enforcement_resumes_after_first_commit(self) -> None:
        self.assertEqual(self._hook(self.target).returncode, 0)
        for args in (
            ("config", "user.email", "t@t.test"),
            ("config", "user.name", "Test"),
            ("commit", "--allow-empty", "-m", "born"),
        ):
            subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(self._hook(self.target).returncode, 2)

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_detached_head_is_still_enforced(self) -> None:
        # A repo with history has commits on a ref, so it can host a worktree --
        # a detached checkout must stay gated even though HEAD itself is bare.
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        self._git("commit", "--allow-empty", "-m", "born")
        self._git("checkout", "--detach", "HEAD")
        self.assertEqual(self._hook(self.target).returncode, 2)

    def test_orphan_branch_checkout_is_enforced(self) -> None:
        # HEAD is unborn on the orphan branch, but the repo has commits on main,
        # so a worktree IS possible -- the carve-out must not fire here.
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        self._git("commit", "--allow-empty", "-m", "born")
        self._git("checkout", "--orphan", "docs")
        self.assertEqual(self._hook(self.target).returncode, 2)

    def test_user_disable_suppresses_unborn_notice(self) -> None:
        subprocess.run(
            [sys.executable, str(_GATE), "disable", "--user"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env=self._env(),
        )
        proc = self._hook(self.target)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
