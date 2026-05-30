#!/usr/bin/env python3
"""SessionStart hook: surface this repo's mergeable worktrees.

Gate-then-inject. Fires only for startup/resume (enforced by hooks.json
matchers; re-checked defensively here). Stays completely silent unless:
  - cwd is inside a git repo, AND
  - cwd is the repo's MAIN worktree (never a linked worktree — the review
    skill must not run from inside a worktree), AND
  - the repo has >=1 linked worktree with NO live claude session.

When all hold, it emits a JSON hook result whose `systemMessage` shows the
user a banner with the orphan count plus the same box-drawing table that
/check-worktrees renders, so the user can see at a glance what's in scope
before running /merge-worktrees. `systemMessage` is user-facing only — it is
NOT added to the agent's context and never instructs the agent to act.
Merging is a deliberate, explicit user opt-in (the user types the slash
command), so nothing relies on the agent honoring an injected instruction.
This hook never merges or mutates anything.

Repo-scoped by construction: the detector only inspects worktrees of this
repo. Exit code is always 0 — a failing SessionStart hook would degrade the
user's session for no benefit.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
_scripts_dir = (Path(_root) if _root else Path(__file__).resolve().parent.parent) / "scripts"
sys.path.insert(0, str(_scripts_dir))

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


def get_orphan_info(cwd: str) -> tuple[int, str]:
    """Gather orphan worktrees in-process and render the display table.

    Returns (count, rendered_table). Returns (0, '') when there are no
    orphans or on any error.
    """
    try:
        import check_worktrees as cw  # noqa: PLC0415
    except Exception:
        return 0, ""

    try:
        worktrees = asyncio.run(
            asyncio.wait_for(cw.gather_worktrees(cwd, show_all=False), timeout=20.0)
        )
    except Exception:
        return 0, ""

    n = len(worktrees)
    if n == 0:
        return 0, ""

    try:
        table = cw.render_table(worktrees)
    except Exception:
        table = ""

    return n, table


def main() -> int:
    payload = read_stdin()
    source = payload.get("source", "")
    if source and source not in ALLOWED_SOURCES:
        return 0
    cwd = payload.get("cwd") or os.getcwd()

    if not is_main_worktree(cwd):
        return 0

    n, table = get_orphan_info(cwd)
    if n <= 0:
        return 0

    plural = "worktree" if n == 1 else "worktrees"
    pronoun = "it" if n == 1 else "them"
    header = (
        f"\n\n🌳 {n} mergeable git {plural} found by worktree-warden. "
        f"Run /merge-worktrees to land {pronoun} into the default branch, "
        f"or /check-worktrees to review first."
    )
    message = f"{header}\n\n{table}" if table else header
    print(json.dumps({"systemMessage": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
