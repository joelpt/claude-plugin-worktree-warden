#!/usr/bin/env python3
"""Stop hook: inject an auto-teardown prompt when there is pending worktree work.

Fires on every Stop event. Fast-exits unless all eligibility conditions hold:
  * stop_hook_active is not True (loop guard)
  * cwd is inside a linked worktree (not the main checkout)
  * teardown_mode is not 'never'
  * the worktree has at least one commit ahead of the default branch, or dirty files

When eligible and the debounce allows it, the hook emits a ``decision: block``
response whose ``reason`` field instructs Claude to self-assess whether the task
is complete and act per the configured mode.  Claude decides completion; the hook
only decides eligibility.

Fail-open contract: any unhandled exception exits 0 (silent pass-through) so a
buggy hook never traps the user in an unresponsive session.  This extends to the
``worktree_gate`` import: if the scripts dir cannot be resolved the hook silently
does nothing rather than printing a traceback and exiting non-zero.

Debounce: after firing for a given (session, commit_count) pair, the hook stays
silent until either the commit count increases OR DEBOUNCE_RESEND_MINUTES have
elapsed.  This handles two cases:
  * Claude said "not done" — same commit count blocks repeated prompting.
  * Dirty-only worktrees — commit count stays at 0, so time-based re-fire
    ensures the user is nudged again after a long working stretch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict, cast

_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
_scripts_dir = (
    Path(_root) if _root else Path(__file__).resolve().parent.parent
) / "scripts"
sys.path.insert(0, str(_scripts_dir))

_STATE_PREFIX = "auto-teardown-"
_STATE_SUFFIX = ".json"
DEBOUNCE_RESEND_MINUTES = 15
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


class _DebounceState(TypedDict, total=False):
    """Shape of the per-session debounce state file."""

    last_fire_commit_count: int
    last_fire_time: float


class _HookPayload(TypedDict, total=False):
    """Shape of the Stop hook stdin payload."""

    stop_hook_active: bool
    cwd: str
    session_id: str


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a git subcommand and return (returncode, stripped-stdout)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception:
        return 1, ""


def _count_ahead(cwd: str) -> tuple[int, str]:
    """Return (commits_ahead_of_base, base_ref) for the current HEAD.

    Tries common merge-target refs in order: remote HEAD, then origin/main,
    origin/master, then local main/master.  The feature branch's own upstream
    tracking ref is intentionally excluded — ``origin/feature..HEAD`` would
    return 0 for any pushed branch, silencing the hook on ready-to-land work.

    Returns (0, '') when no base can be resolved — treated by the caller as
    "nothing committed yet."
    """
    candidates = [
        "origin/HEAD",
        "origin/main",
        "origin/master",
        "main",
        "master",
    ]
    for ref in candidates:
        rc, out = _git(["rev-list", "--count", f"{ref}..HEAD"], cwd)
        if rc == 0:
            try:
                return int(out), ref
            except ValueError:
                continue
    return 0, ""


def _is_dirty(cwd: str) -> bool:
    """Return True if the worktree has any uncommitted changes."""
    rc, out = _git(["status", "--porcelain"], cwd)
    return rc == 0 and bool(out)


def _current_branch(cwd: str) -> str:
    """Return the abbreviated HEAD ref, or 'unknown' on failure."""
    rc, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out if rc == 0 and out else "unknown"


def _safe_session_id(session_id: str) -> str:
    """Sanitize session_id to characters safe for use in a filename."""
    return _SAFE_ID_RE.sub("", session_id)[:64]


def _state_path(git_common_dir: str, session_id: str) -> Path:
    """Return the per-session debounce state file path."""
    return (
        Path(git_common_dir)
        / f"{_STATE_PREFIX}{_safe_session_id(session_id)}{_STATE_SUFFIX}"
    )


def _read_state(git_common_dir: str, session_id: str) -> _DebounceState:
    """Read the debounce state file; return an empty state on missing or malformed."""
    try:
        path = _state_path(git_common_dir, session_id)
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return cast(_DebounceState, data)
    except Exception:
        pass
    return _DebounceState()


def _write_state(git_common_dir: str, session_id: str, commit_count: int) -> None:
    """Persist the debounce state file with current commit count and timestamp."""
    try:
        _state_path(git_common_dir, session_id).write_text(
            json.dumps(
                {
                    "last_fire_commit_count": commit_count,
                    "last_fire_time": time.time(),
                }
            )
        )
    except Exception:
        pass


def _should_debounce(state: _DebounceState, commit_count: int) -> bool:
    """Return True if the hook should stay silent given prior fire state.

    Silences the hook when the commit count is unchanged AND the last fire was
    recent enough.  A new commit always re-enables the hook.  After
    DEBOUNCE_RESEND_MINUTES the hook re-fires even with the same count, so
    dirty-only worktrees (commit_count == 0 throughout) still get periodic nudges.

    Args:
        state: Previously persisted debounce state for this session.
        commit_count: Current count of commits ahead of the default branch.

    Returns:
        True when the hook should stay silent.
    """
    last_count = state.get("last_fire_commit_count")
    last_time = state.get("last_fire_time", 0.0)
    if last_count != commit_count:
        return False
    elapsed_minutes = (time.time() - float(last_time or 0.0)) / 60
    return elapsed_minutes < DEBOUNCE_RESEND_MINUTES


def _build_reason(
    mode: str,
    branch: str,
    commit_count: int,
    base_ref: str,
    dirty: bool,
) -> str:
    """Build the reason string for the Stop hook block response.

    Args:
        mode: Effective teardown mode.
        branch: Current branch name.
        commit_count: Number of commits ahead of base_ref.
        base_ref: The base ref name used for the count.
        dirty: Whether the worktree has uncommitted changes.

    Returns:
        A multi-line instruction string for Claude.
    """
    state_parts = [f"branch: {branch}"]
    if commit_count:
        base_label = base_ref or "base"
        state_parts.append(f"{commit_count} commit(s) ahead of {base_label}")
    if dirty:
        state_parts.append("uncommitted changes present")
    state = ", ".join(state_parts)

    if mode == "ask":
        return (
            f"You are in a linked git worktree ({state}).\n"
            "Your auto-teardown policy: ask.\n\n"
            "Self-assess: is the task genuinely complete?\n"
            "  - YES → use AskUserQuestion now to offer commit + merge + teardown."
            " Show branch/commit count. Respect the answer.\n"
            "  - NO  → ignore this message and continue normally."
        )
    if mode == "auto":
        return (
            f"You are in a linked git worktree ({state}).\n"
            "Your auto-teardown policy: auto.\n\n"
            "Self-assess: is the task genuinely complete?\n"
            "  - YES, confident  → commit any dirty files, then run"
            " /worktrees:merge-worktrees to land and tear down. No confirmation needed.\n"
            "  - YES, uncertain  → use AskUserQuestion to confirm before proceeding.\n"
            "  - NO              → ignore this message and continue normally."
        )
    if mode == "commit-only":
        return (
            f"You are in a linked git worktree ({state}).\n"
            "Your auto-teardown policy: commit-only.\n\n"
            "Self-assess: is it safe to commit uncommitted work?\n"
            "  - YES → commit any dirty changes (/commit-commands:commitall)."
            " Do NOT merge or tear down.\n"
            "  - NO  → ignore this message and continue normally."
        )
    if mode == "always":
        return (
            f"You are in a linked git worktree ({state}).\n"
            "Your auto-teardown policy: always.\n\n"
            "Self-assess — ALL three must be true:\n"
            "  1. Task is complete per the original specification.\n"
            "  2. Work has been appropriately tested (run tests if not done).\n"
            "  3. No non-trivial conflict with main (trivial rebase conflict = fine;"
            " main was refactored while you worked = stop).\n\n"
            "  - ALL three true → commit, merge via /worktrees:merge-worktrees,"
            " tear down. No confirmation.\n"
            "  - Any criterion fails → report WHICH one failed and why. Do NOT proceed."
        )
    return ""


def main() -> int:
    """Hook entry point: parse payload, apply eligibility gates, emit decision.

    Returns:
        Always 0 — the hook never hard-fails (fail-open contract).
    """
    try:
        payload = cast(_HookPayload, json.load(sys.stdin))
    except Exception:
        return 0

    if payload.get("stop_hook_active") is True:
        return 0

    cwd: str = payload.get("cwd") or os.getcwd()
    session_id: str = payload.get("session_id") or ""

    try:
        import worktree_gate as gate  # noqa: PLC0415
    except ImportError:
        return 0

    facts = gate.git_facts(cwd)
    if not facts.in_linked_worktree:
        return 0

    mode = gate.read_teardown_mode(facts)
    if mode == "never":
        return 0

    commit_count, base_ref = _count_ahead(cwd)
    dirty = _is_dirty(cwd)
    if commit_count == 0 and not dirty:
        return 0

    # commit-only mode is only relevant when there are dirty files to commit
    if mode == "commit-only" and not dirty:
        return 0

    if facts.git_common_dir and session_id:
        state = _read_state(facts.git_common_dir, session_id)
        if _should_debounce(state, commit_count):
            return 0

    # Non-destructively snapshot uncommitted work to a bundle before nudging.
    # The destruction gate cannot see the harness's native worktree cleanup, so
    # this is the backstop that keeps dirty WIP recoverable if the directory is
    # later removed by something the gate never intercepts. Best-effort, rate
    # limited by the same debounce above; surfaced later via `recover`.
    if dirty:
        try:
            import worktree_wip  # noqa: PLC0415

            worktree_wip.capture_wip(cwd)
        except Exception:
            pass

    branch = _current_branch(cwd)

    reason = _build_reason(mode, branch, commit_count, base_ref, dirty)
    # Write state AFTER emitting output so a broken-pipe on print() leaves
    # debounce unarmed — the next Stop will fire again rather than being
    # silenced for 15 minutes against a message Claude never received.
    print(json.dumps({"decision": "block", "reason": reason}))
    if facts.git_common_dir and session_id:
        _write_state(facts.git_common_dir, session_id, commit_count)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
