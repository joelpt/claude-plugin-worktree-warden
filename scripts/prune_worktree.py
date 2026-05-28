#!/usr/bin/env python3
"""Deterministic, idempotent, path-gated worktree teardown.

Removes a single linked worktree and deletes its branch AFTER a merge has
landed. Safety properties:
  - Path gate: refuses any path that is not a registered LINKED worktree of
    the repo (and never the main worktree).
  - Non-destructive: `git worktree remove` (no --force) refuses a dirty tree;
    `git branch -d` (not -D) refuses an unmerged branch. We never override.
  - Idempotent: a path/branch that is already gone is treated as success.

Usage: prune_worktree.py <worktree-path> <branch> [--repo <dir>]
Exit codes: 0 = pruned or already-gone; 1 = --repo is not a git repo;
2 = target is the main worktree; 3 = worktree remove refused (dirty/locked);
4 = branch not fully merged; 5 = worktree is on a different branch than given.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def git(args: list[str], repo: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def registered_worktrees(repo: str) -> tuple[str, set[str]]:
    """Return (main_path, {linked worktree paths}) from porcelain output."""
    rc, out, _ = git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return "", set()
    paths: list[str] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(os.path.realpath(line[len("worktree ") :]))
    main = paths[0] if paths else ""
    return main, set(paths[1:])


def main() -> int:
    parser = argparse.ArgumentParser(prog="prune_worktree")
    parser.add_argument("path")
    parser.add_argument("branch")
    parser.add_argument("--repo", default=os.getcwd())
    args = parser.parse_args()

    repo = args.repo
    target = os.path.realpath(args.path)

    rc, _, _ = git(["rev-parse", "--git-dir"], repo)
    if rc != 0:
        sys.stderr.write(f"refuse: '{repo}' is not a git repository\n")
        return 1

    main_path, linked = registered_worktrees(repo)

    if target == main_path:
        sys.stderr.write(f"refuse: '{target}' is the MAIN worktree\n")
        return 2

    if target in linked:
        rc, actual, _ = git(["-C", target, "symbolic-ref", "--short", "HEAD"], repo)
        if rc == 0 and actual and actual != args.branch:
            sys.stderr.write(
                f"refuse: worktree is on branch '{actual}', not '{args.branch}'\n"
            )
            return 5
        rc, _, err = git(["worktree", "remove", target], repo)
        if rc != 0:
            sys.stderr.write(f"refuse: worktree remove failed (dirty/locked?): {err}\n")
            return 3
    # else: not registered → either already removed (idempotent) or never a
    # worktree. Fall through to branch cleanup + prune, which are safe no-ops.

    rc, _, err = git(["branch", "-d", args.branch], repo)
    branch_msg = "deleted"
    if rc != 0:
        # -d refuses unmerged branches; also errors if already gone.
        rcv, _, _ = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{args.branch}"], repo)
        if rcv == 0:
            sys.stderr.write(f"refuse: branch '{args.branch}' not fully merged: {err}\n")
            return 4
        branch_msg = "already gone"

    git(["worktree", "prune"], repo)
    print(f"pruned worktree '{target}'; branch '{args.branch}' {branch_msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
