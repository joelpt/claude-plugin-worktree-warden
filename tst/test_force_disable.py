"""Tests for the hidden force-disable switch (set by the Jack plugin).

When ``WORKTREE_WARDEN_FORCE_DISABLE`` is exactly ``"1"`` every warden hook must
exit 0 silently, with no output and no side effects, so that during a Jack-driven
session Jack alone owns worktree teardown. The switch must also stay undiscoverable
by Claude: its name must never appear in any string a hook can print or write.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

import auto_teardown_hook
import check_worktrees_hook
import enforce_worktree_hook
import guard_destruction_hook
import worktree_gate

_ROOT = Path(__file__).resolve().parent.parent
_NAME = "WORKTREE_WARDEN_FORCE_DISABLE"
_HOOKS = {
    "auto_teardown_hook": _ROOT / "hooks" / "auto_teardown_hook.py",
    "check_worktrees_hook": _ROOT / "hooks" / "check_worktrees_hook.py",
    "enforce_worktree_hook": _ROOT / "hooks" / "enforce_worktree_hook.py",
    "guard_destruction_hook": _ROOT / "hooks" / "guard_destruction_hook.py",
}

#: Values the switch must NOT honor: only the exact string "1" enables it.
_NOT_ENABLING = ["0", "true", "yes", "", " 1", "1 ", "1\n", "01", "2"]


class _Raises(Mapping[str, str]):
    """A mapping whose lookups raise, to prove the helper never propagates."""

    def get(self, key: str, default: object = None) -> str:  # type: ignore[override]
        """Always raise."""
        raise RuntimeError("boom")

    def __getitem__(self, key: str) -> str:
        """Always raise."""
        raise RuntimeError("boom")

    def __iter__(self):  # type: ignore[no-untyped-def]
        """Iterate nothing."""
        return iter(())

    def __len__(self) -> int:
        """Report empty."""
        return 0


class ForceDisabledHelperTest(unittest.TestCase):
    """``worktree_gate.force_disabled`` honors exactly the string ``"1"``."""

    def test_exact_one_enables(self) -> None:
        """Only ``"1"`` turns the switch on."""
        self.assertTrue(worktree_gate.force_disabled({_NAME: "1"}))

    def test_every_other_value_does_not_enable(self) -> None:
        """Near-misses (whitespace, words, other digits) leave warden active."""
        for value in _NOT_ENABLING:
            with self.subTest(value=value):
                self.assertFalse(worktree_gate.force_disabled({_NAME: value}))

    def test_missing_does_not_enable(self) -> None:
        """An absent variable leaves warden active."""
        self.assertFalse(worktree_gate.force_disabled({}))

    def test_never_raises(self) -> None:
        """A broken mapping or a non-mapping degrades to ``False``."""
        self.assertFalse(worktree_gate.force_disabled(_Raises()))
        self.assertFalse(worktree_gate.force_disabled(object()))  # type: ignore[arg-type]

    def test_default_reads_the_process_environment(self) -> None:
        """With no argument the helper consults ``os.environ``."""
        with mock.patch.dict(os.environ, {_NAME: "1"}):
            self.assertTrue(worktree_gate.force_disabled())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_NAME, None)
            self.assertFalse(worktree_gate.force_disabled())


class HookCopiesAgreeWithTheHelperTest(unittest.TestCase):
    """Each hook keeps its own dependency-light copy; it must not drift."""

    def test_every_hook_copy_matches_the_shared_helper(self) -> None:
        """Hook-local ``_force_disabled`` and the gate helper give the same answer."""
        modules = (auto_teardown_hook, check_worktrees_hook, enforce_worktree_hook, guard_destruction_hook)
        for value in ["1", *_NOT_ENABLING]:
            with mock.patch.dict(os.environ, {_NAME: value}):
                expected = worktree_gate.force_disabled()
                for module in modules:
                    with self.subTest(module=module.__name__, value=value):
                        self.assertEqual(module._force_disabled(), expected)

    def test_every_hook_copy_is_false_when_the_variable_is_absent(self) -> None:
        """No variable means active, in every hook."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_NAME, None)
            for module in (auto_teardown_hook, check_worktrees_hook, enforce_worktree_hook, guard_destruction_hook):
                with self.subTest(module=module.__name__):
                    self.assertFalse(module._force_disabled())


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """Map every file under *root* to ``(size, content)``; empty if *root* is absent."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _HookRepoCase(unittest.TestCase):
    """A temp repo with one linked worktree holding committed, unlanded work."""

    def setUp(self) -> None:
        """Build the repo, a linked worktree, an XDG dir and a clean Claude config dir."""
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.xdg = base / "xdg"
        self.claude_config = base / "claude-config"
        self.claude_config.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        self.wt = base / "wt"
        self._git("worktree", "add", "-b", "feature", str(self.wt))
        (self.wt / "work.txt").write_text("work\n")
        self._git("add", "work.txt", cwd=self.wt)
        self._git("commit", "-m", "work", cwd=self.wt)

    def tearDown(self) -> None:
        """Remove the temp tree."""
        self._tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", "-C", str(cwd or self.repo), *args], check=True, capture_output=True, text=True)

    def _env(self, *, switch: str | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != _NAME}
        env.update(
            XDG_CONFIG_HOME=str(self.xdg),
            CLAUDE_CONFIG_DIR=str(self.claude_config),
            CLAUDE_PLUGIN_ROOT=str(_ROOT),
        )
        if switch is not None:
            env[_NAME] = switch
        return env

    def _run(self, hook: Path, payload: dict[str, object], *, switch: str | None, cwd: Path | None = None):  # type: ignore[no-untyped-def]
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(cwd or self.repo),
            env=self._env(switch=switch),
        )

    def _payloads(self) -> dict[str, tuple[Path, dict[str, object], Path]]:
        """Return ``name -> (hook path, payload, cwd)`` for payloads that normally act."""
        return {
            "auto_teardown_hook": (
                _HOOKS["auto_teardown_hook"],
                {"cwd": str(self.wt), "session_id": "s1"},
                self.wt,
            ),
            "check_worktrees_hook": (
                _HOOKS["check_worktrees_hook"],
                {"source": "startup", "cwd": str(self.repo)},
                self.repo,
            ),
            "enforce_worktree_hook": (
                _HOOKS["enforce_worktree_hook"],
                {"tool_name": "Edit", "cwd": str(self.repo), "tool_input": {"file_path": str(self.repo / "x.py")}},
                self.repo,
            ),
            "guard_destruction_hook": (
                _HOOKS["guard_destruction_hook"],
                {
                    "tool_name": "Bash",
                    "cwd": str(self.repo),
                    "tool_input": {"command": f"git worktree remove --force {self.wt}"},
                },
                self.repo,
            ),
        }


class BaselineActsTest(_HookRepoCase):
    """With the switch unset each hook really acts, so the silence below is meaningful."""

    def test_each_hook_produces_output_or_blocks_without_the_switch(self) -> None:
        """Every payload either prints something or exits non-zero when warden is active."""
        for name, (hook, payload, cwd) in self._payloads().items():
            with self.subTest(hook=name):
                proc = self._run(hook, payload, switch=None, cwd=cwd)
                self.assertTrue(
                    proc.stdout.strip() != "" or proc.stderr.strip() != "" or proc.returncode != 0,
                    f"{name} did nothing with the switch unset: rc={proc.returncode}",
                )


class ForceDisabledHooksTest(_HookRepoCase):
    """With the switch on, every hook is a silent no-op with no side effects."""

    def test_every_hook_is_silent_and_exits_zero(self) -> None:
        """Exit 0, empty stdout, empty stderr."""
        for name, (hook, payload, cwd) in self._payloads().items():
            with self.subTest(hook=name):
                proc = self._run(hook, payload, switch="1", cwd=cwd)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(proc.stderr, "")

    def test_every_hook_leaves_the_repo_and_config_dirs_untouched(self) -> None:
        """No WIP bundle, lock, audit entry, debounce state or error sentinel is written."""
        git_dir = self.repo / ".git"
        for name, (hook, payload, cwd) in self._payloads().items():
            with self.subTest(hook=name):
                before = (_snapshot(git_dir), _snapshot(self.xdg), _snapshot(self.claude_config))
                self._run(hook, payload, switch="1", cwd=cwd)
                after = (_snapshot(git_dir), _snapshot(self.xdg), _snapshot(self.claude_config))
                self.assertEqual(before, after)

    def test_values_other_than_one_leave_warden_active(self) -> None:
        """``"0"`` and ``"true"`` do not disable anything."""
        hook, payload, cwd = self._payloads()["auto_teardown_hook"]
        for value in ("0", "true"):
            with self.subTest(value=value):
                # A fresh session id per value: the first run arms the hook's debounce.
                proc = self._run(hook, {**payload, "session_id": f"sess-{value}"}, switch=value, cwd=cwd)
                self.assertNotEqual(proc.stdout.strip(), "")


class BrokenImportStaysSilentTest(_HookRepoCase):
    """The check must run before the guarded imports, so a broken install stays silent too."""

    def _hook_without_scripts(self, name: str) -> Path:
        """Copy one hook next to NO sibling ``scripts/`` dir so its gate import fails."""
        plugin = Path(self._tmp.name) / f"broken-plugin-{name}"
        (plugin / "hooks").mkdir(parents=True)
        shutil.copy(_HOOKS[name], plugin / "hooks" / f"{name}.py")
        return plugin / "hooks" / f"{name}.py"

    def test_guard_and_enforce_report_nothing_when_their_imports_fail(self) -> None:
        """Without the switch a failed import is reported; with it, nothing is."""
        payloads = self._payloads()
        for name in ("guard_destruction_hook", "enforce_worktree_hook"):
            hook = self._hook_without_scripts(name)
            _, payload, cwd = payloads[name]
            with self.subTest(hook=name, switch="unset"):
                proc = self._run(hook, payload, switch=None, cwd=cwd)
                self.assertNotEqual(proc.stderr.strip(), "")
            with self.subTest(hook=name, switch="1"):
                proc = self._run(hook, payload, switch="1", cwd=cwd)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))


class SwitchIsNeverNamedInOutputTest(unittest.TestCase):
    """The switch must be undiscoverable in anything Claude can see in a normal session."""

    def test_the_name_is_absent_from_skills_and_manifests(self) -> None:
        """No skill text, hook config or plugin manifest mentions it."""
        paths = [*(_ROOT / "skills").rglob("*"), _ROOT / "hooks" / "hooks.json", _ROOT / ".claude-plugin" / "plugin.json"]
        offenders = [str(p.relative_to(_ROOT)) for p in paths if p.is_file() and _NAME in p.read_text(errors="ignore")]
        self.assertEqual(offenders, [])

    def test_no_string_a_hook_could_print_contains_the_name(self) -> None:
        """In code, the name appears only as the bare identifier string, never inside a message."""
        offenders: list[str] = []
        for path in [*(_ROOT / "hooks").glob("*.py"), *(_ROOT / "scripts").glob("*.py")]:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and _NAME in node.value:
                    if node.value != _NAME and not _is_docstring(tree, node):
                        offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])


def _is_docstring(tree: ast.AST, node: ast.Constant) -> bool:
    """Return True when *node* is a module, class or function docstring."""
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = parent.body
            if body and isinstance(body[0], ast.Expr) and body[0].value is node:
                return True
    return False


if __name__ == "__main__":
    unittest.main()
