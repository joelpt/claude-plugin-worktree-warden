"""Fail-open-LOUDLY tests: a broken gate-module import must signal, not vanish.

Claude Code blocks only on exit 2; Python's default exit 1 on an
``ImportError``/``SyntaxError`` is treated as *allow*, so a bad sibling import
would silently disable the gate. These tests drive the real hook scripts as
subprocesses against a temp plugin root whose gate module is deliberately
broken, asserting the loud contract: exit 0, a stderr diagnostic, and a durable
sentinel under ``<git-common-dir>/worktree-warden/gate-load-error`` that the
SessionStart hook then surfaces (and self-heals once the gate loads again).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SENTINEL_RELPATH = Path("worktree-warden") / "gate-load-error"
_BROKEN_MODULE = "raise RuntimeError('simulated gate import failure')\n"


class _TempPluginRepo(unittest.TestCase):
    """A temp git repo plus a temp plugin root with a swappable scripts/ dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.xdg = base / "xdg"
        self.plugin_root = base / "plugin"
        (self.plugin_root / "scripts").mkdir(parents=True)
        (self.plugin_root / "hooks").mkdir(parents=True)
        self._git("init")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _copy_hook(self, name: str) -> Path:
        dest = self.plugin_root / "hooks" / name
        shutil.copy(_ROOT / "hooks" / name, dest)
        return dest

    def _copy_real_script(self, name: str) -> None:
        shutil.copy(_ROOT / "scripts" / name, self.plugin_root / "scripts" / name)

    def _break_script(self, name: str) -> None:
        (self.plugin_root / "scripts" / name).write_text(_BROKEN_MODULE)

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.xdg),
            "CLAUDE_PLUGIN_ROOT": str(self.plugin_root),
        }

    def _run(self, hook: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env=self._env(),
        )

    @property
    def sentinel(self) -> Path:
        return self.repo / ".git" / _SENTINEL_RELPATH


class EnforceHookFailsafeTest(_TempPluginRepo):
    """A broken ``worktree_gate`` import must fail the edit gate open, loudly."""

    def test_broken_import_exit0_stderr_and_sentinel(self) -> None:
        self._break_script("worktree_gate.py")
        hook = self._copy_hook("enforce_worktree_hook.py")
        proc = self._run(
            hook,
            {
                "tool_name": "Edit",
                "cwd": str(self.repo),
                "tool_input": {"file_path": str(self.repo / "src" / "x.py")},
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FAILING OPEN", proc.stderr)
        self.assertIn("worktree_gate", proc.stderr)
        self.assertTrue(self.sentinel.exists())
        self.assertIn("worktree_gate", self.sentinel.read_text())

    def test_syntax_error_import_also_fails_open_loudly(self) -> None:
        # A SyntaxError is raised at the import statement and caught identically.
        (self.plugin_root / "scripts" / "worktree_gate.py").write_text("def (:\n")
        hook = self._copy_hook("enforce_worktree_hook.py")
        proc = self._run(
            hook,
            {
                "tool_name": "Edit",
                "cwd": str(self.repo),
                "tool_input": {"file_path": str(self.repo / "src" / "x.py")},
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FAILING OPEN", proc.stderr)
        self.assertTrue(self.sentinel.exists())


class GuardHookFailsafeTest(_TempPluginRepo):
    """A broken ``worktree_destruction`` import must fail the guard open, loudly."""

    def test_broken_import_exit0_stderr_and_sentinel(self) -> None:
        self._break_script("worktree_destruction.py")
        self._copy_real_script("worktree_gate.py")
        hook = self._copy_hook("guard_destruction_hook.py")
        proc = self._run(
            hook,
            {
                "tool_name": "Bash",
                "cwd": str(self.repo),
                "tool_input": {"command": "git worktree remove --force x"},
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FAILING OPEN", proc.stderr)
        self.assertTrue(self.sentinel.exists())

    def test_broken_gate_module_also_fails_guard_open_loudly(self) -> None:
        # worktree_destruction imports worktree_gate at top level, so a broken
        # worktree_gate (the likeliest edit target) fails the guard's import too.
        self._break_script("worktree_gate.py")
        self._copy_real_script("worktree_destruction.py")
        hook = self._copy_hook("guard_destruction_hook.py")
        proc = self._run(
            hook,
            {
                "tool_name": "Bash",
                "cwd": str(self.repo),
                "tool_input": {"command": "git worktree remove --force x"},
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FAILING OPEN", proc.stderr)
        self.assertTrue(self.sentinel.exists())


class SessionStartSurfacingTest(_TempPluginRepo):
    """SessionStart surfaces a live sentinel and self-heals a stale one."""

    def _seed_sentinel(self) -> None:
        self.sentinel.parent.mkdir(parents=True, exist_ok=True)
        self.sentinel.write_text("2026-06-14T00:00:00Z\tworktree_gate\tboom\n")

    def test_surfaces_sentinel_when_gate_still_broken(self) -> None:
        self._seed_sentinel()
        self._break_script("worktree_gate.py")
        hook = self._copy_hook("check_worktrees_hook.py")
        proc = self._run(hook, {"source": "startup", "cwd": str(self.repo)})
        self.assertEqual(proc.returncode, 0)
        emitted = json.loads(proc.stdout)
        self.assertIn("FAILED TO LOAD", emitted["systemMessage"])
        self.assertTrue(self.sentinel.exists())

    def test_self_heals_when_gate_loads_again(self) -> None:
        self._seed_sentinel()
        for name in ("worktree_gate.py", "worktree_destruction.py"):
            self._copy_real_script(name)
        hook = self._copy_hook("check_worktrees_hook.py")
        proc = self._run(hook, {"source": "startup", "cwd": str(self.repo)})
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(self.sentinel.exists())
        if proc.stdout.strip():
            self.assertNotIn("FAILED TO LOAD", proc.stdout)


if __name__ == "__main__":
    unittest.main()
