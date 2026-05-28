#!/usr/bin/env python3
"""SessionStart hook: surface this repo's mergeable worktrees.

Gate-then-inject. Fires only for startup/resume (enforced by hooks.json
matchers; re-checked defensively here). Stays completely silent unless:
  - cwd is inside a git repo, AND
  - cwd is the repo's MAIN worktree (never a linked worktree — the review
    skill must not run from inside a worktree), AND
  - the repo has >=1 linked worktree with NO live claude session.

When all hold, it prints (plain stdout → injected as SessionStart context)
an instruction to invoke the /worktrees:check-worktrees skill. It never
merges or mutates anything; the skill drives the human-gated flow.

Repo-scoped by construction: the detector only inspects worktrees of this
repo. Exit code is always 0 — a failing SessionStart hook would degrade the
user's session for no benefit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ALLOWED_SOURCES = {"startup", "resume"}


def read_stdin() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def is_main_worktree(cwd: str) -> bool:
    """True iff cwd is the main (non-linked) worktree of its git repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    lines = proc.stdout.strip().splitlines()
    if len(lines) != 2:
        return False
    git_dir, common_dir = lines
    # Linked worktrees have a distinct per-worktree git dir; main does not.
    return os.path.realpath(git_dir) == os.path.realpath(common_dir)


def detector_path() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root) / "scripts" / "check_worktrees.py"
    return Path(__file__).resolve().parent.parent / "scripts" / "check_worktrees.py"


def count_orphans(cwd: str) -> int:
    """Run the detector in --json mode; return the orphan count (0 on error)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(detector_path()), "--cwd", cwd, "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return 0
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return 0
    return len(data) if isinstance(data, list) else 0


def main() -> int:
    payload = read_stdin()
    source = payload.get("source", "")
    if source and source not in ALLOWED_SOURCES:
        return 0
    cwd = payload.get("cwd") or os.getcwd()

    if not is_main_worktree(cwd):
        return 0

    n = count_orphans(cwd)
    if n <= 0:
        return 0

    plural = "worktree" if n == 1 else "worktrees"
    print(
        f"## Mergeable worktrees detected\n\n"
        f"This repo has {n} linked git {plural} with no live `claude` session. "
        f"Invoke the `/worktrees:check-worktrees` skill now to show their state "
        f"and optionally merge any of them into the default branch before "
        f"continuing with other work.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
