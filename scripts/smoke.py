#!/usr/bin/env python3
"""Black-box smoke test: drive the shipped hook and gate CLI end-to-end.

Runs the real ``enforce_worktree_hook.py`` (fed the Claude Code stdin contract)
and the ``worktree_gate.py`` CLI as subprocesses against a throwaway git repo,
asserting the full enforcement lifecycle: block, grant, allow, finished, block,
the linked-worktree allow path, and the disable/enable round-trip. Isolated via
a temp repo and a temp ``XDG_CONFIG_HOME`` so it never touches the real gate
config or audit log.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _ROOT / "hooks" / "enforce_worktree_hook.py"
_GATE = _ROOT / "scripts" / "worktree_gate.py"


class _Smoke:
    """Accumulate check results and remember failures for the exit code."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, passed: bool) -> None:
        """Print one check result and record it when it fails.

        Args:
            label: Human-readable description of the check.
            passed: Whether the check held.
        """
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}")
        if not passed:
            self.failures.append(label)


def _git(repo: Path, *args: str) -> None:
    """Run a git command in repo, raising on a non-zero exit."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_hook(cwd: Path, env: Mapping[str, str]) -> tuple[int, str]:
    """Drive the enforce hook for an Edit at ``cwd/src/main.py``.

    Args:
        cwd: Session working directory the hook should classify against.
        env: Isolated environment (temp XDG plus the plugin root).

    Returns:
        The hook exit code and its stderr text.
    """
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "cwd": str(cwd),
            "tool_input": {"file_path": str(cwd / "src" / "main.py")},
        }
    )
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    return proc.returncode, proc.stderr


def _cli(repo: Path, env: Mapping[str, str], *args: str) -> str:
    """Run the gate CLI in repo and return its stdout."""
    proc = subprocess.run(
        [sys.executable, str(_GATE), *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    return proc.stdout


def _seed_repo(repo: Path) -> None:
    """Create a minimal committed git repo at repo."""
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "smoke@test.invalid")
    _git(repo, "config", "user.name", "smoke")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "seed")


def main() -> int:
    """Run the smoke lifecycle against an isolated repo.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    smoke = _Smoke()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        repo = base / "repo"
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(base / "xdg"),
            "CLAUDE_PLUGIN_ROOT": str(_ROOT),
        }
        _seed_repo(repo)

        print("worktree-warden smoke:")

        rc, err = _run_hook(repo, env)
        smoke.check("main-checkout edit is blocked (exit 2)", rc == 2)
        smoke.check(
            "guidance names request-exception/finish-exception skills",
            "worktree-warden:request-exception" in err
            and "worktree-warden:finish-exception" in err,
        )

        _cli(repo, env, "grant", "smoke reason")
        smoke.check("grant opens the window (edit allowed)", _run_hook(repo, env)[0] == 0)

        _cli(repo, env, "finished")
        smoke.check("finished re-gates (edit blocked)", _run_hook(repo, env)[0] == 2)

        worktree = base / "wt"
        _git(repo, "worktree", "add", "-b", "feature", str(worktree))
        smoke.check("linked worktree is allowed", _run_hook(worktree, env)[0] == 0)

        _cli(repo, env, "disable")
        smoke.check("disable lifts the gate", _run_hook(repo, env)[0] == 0)
        _cli(repo, env, "enable")
        smoke.check("enable restores the gate", _run_hook(repo, env)[0] == 2)

    if smoke.failures:
        print(f"SMOKE FAIL ({len(smoke.failures)})")
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
