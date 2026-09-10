#!/usr/bin/env python3
"""PostToolUse(EnterWorktree) hook: lock a newly-created linked worktree.

A linked worktree's admin record is prunable by default: once its directory
disappears (an external ``rm -rf``, a stray cleanup, a force-quit followed by
housekeeping), a bare ``git worktree prune`` erases the record entirely and the
work it pointed at becomes much harder to notice, let alone recover. Native
``git worktree lock <path>`` flips that: git refuses to prune (or ``remove``) a
locked worktree, so the admin record survives indefinitely and stays
recoverable (see ``worktree_engine._prunable_worktrees`` / ``cmd_recover``,
and ``worktree_population.reconcile``, both of which are taught to treat a
still-registered-but-disk-absent entry as stranded too).

This hook locks the worktree at the earliest reliable attachment point: right
after the harness's own ``EnterWorktree`` tool creates it. ``worktree_engine``
has no real ``git worktree add`` call site of its own to hook (creation
happens inside the harness, outside this plugin), so a ``PostToolUse`` hook on
the ``EnterWorktree`` tool is the correct -- and only -- place to do this.

The worktree's path is read from the ``tool_response`` payload first (several
plausible key names, since the harness's exact response shape is not a
documented contract), falling back to ``cwd`` -- by analogy with
``guard_destruction_hook.py``'s own ``payload.get("cwd")`` convention, since
``EnterWorktree`` switches the session's working directory to the new
worktree before this ``PostToolUse`` hook fires. Only ``EnterWorktree`` calls
that actually *created* a new worktree should be locked -- a switch into an
*existing* worktree via ``path`` re-enters something that may already be
intentionally unlocked, so this hook no-ops whenever the tool_input carried an
explicit ``path`` (an entry, not a creation).

Fails OPEN, unconditionally: locking is best-effort defense-in-depth, never a
requirement for the edit/session to proceed. Any error -- bad payload, no git,
already locked, git predates lock support -- is swallowed and this exits 0.
The ``git worktree lock`` call's own exit code IS checked, though: a nonzero
return writes a diagnostic to stderr so a silent lock failure is at least
detectable (the ``rm -rf``/prune protection this hook exists to provide is the
worktree's *only* protection now that ``worktree_destruction.py`` no longer
independently parses ``worktree remove``/``rm -rf`` -- see that module's
docstring). A hook's stderr on a ``PostToolUse`` exit 0 is not surfaced to the
user by the harness, so this is a best-effort audit trail, not a UI alert.
"""

from __future__ import annotations

import json
import subprocess
import sys

LOCK_REASON = "worktree-warden managed"

# Plausible keys the harness might use for the newly created worktree's path
# inside tool_response. Checked in order; first non-empty string wins.
_RESPONSE_PATH_KEYS = ("path", "worktree_path", "cwd", "directory")


def _resolve_new_worktree_path(payload: dict[str, object]) -> str | None:
    """Best-effort resolution of the worktree path EnterWorktree just created."""
    tool_response = payload.get("tool_response")
    if isinstance(tool_response, dict):
        for key in _RESPONSE_PATH_KEYS:
            value = tool_response.get(key)
            if isinstance(value, str) and value:
                return value
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return None


def main() -> int:
    """Lock the worktree EnterWorktree just created. Always exits 0 (fail-open)."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        if payload.get("tool_name") != "EnterWorktree":
            return 0

        tool_input = payload.get("tool_input") or {}
        if isinstance(tool_input, dict) and tool_input.get("path"):
            # Entering an EXISTING worktree, not creating one -- nothing to lock.
            return 0

        path = _resolve_new_worktree_path(payload)
        if not path:
            return 0

        result = subprocess.run(
            ["git", "-C", path, "worktree", "lock", path, "--reason", LOCK_REASON],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            sys.stderr.write(
                "worktree-warden: failed to lock newly created worktree "
                f"{path!r} (git worktree lock exited {result.returncode}): "
                f"{result.stderr.strip()}\n"
                "This worktree now has ZERO destruction protection -- neither "
                "git's lock refusal nor worktree_destruction.py's (removed) "
                "independent gate applies.\n"
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
