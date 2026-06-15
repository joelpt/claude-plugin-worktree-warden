#!/usr/bin/env python3
"""PreToolUse gate: block destruction of a dirty or un-landed worktree/branch.

Sister to ``enforce_worktree_hook`` (which gates *edits* to the main checkout).
This gates *destruction*: a raw ``git worktree remove --force``, ``rm -rf
<worktree>``, ``git branch -D``, or an ``ExitWorktree`` remove can throw away a
worktree whose content was never committed or never landed in the default
branch. The merge/teardown engine is guarded, but commands issued directly
through the Bash tool bypass it entirely — that is the hole this closes.

Blocking uses **exit code 2** (stderr fed back to Claude), not the JSON
``permissionDecision`` route, for the same load-bearing reason as the edit gate:
exit 2 runs *before* the permission layer, so it blocks even under
``permissions.defaultMode: "bypassPermissions"``. Do not "modernize" to JSON.

Fails OPEN: any unexpected error allows the command. It fires on every Bash /
ExitWorktree call in every repo where the plugin is enabled, so a crash must
never brick the shell. The gate only refuses when it can *prove* the target
holds un-landed or uncommitted content; ambiguity allows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT")
_SCRIPTS_DIR = (
    Path(_PLUGIN_ROOT) / "scripts"
    if _PLUGIN_ROOT
    else Path(__file__).resolve().parent.parent / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

GATE_LOAD_ERROR_RELPATH = Path("worktree-warden") / "gate-load-error"


def _report_gate_load_failure(module_name: str, exc: BaseException, tb: str) -> None:
    """Handle a failed gate-module import: write a stderr diagnostic and drop a sentinel.

    Runs only when a sibling gate module fails to import (``SyntaxError``,
    ``ImportError``, a half-applied edit). Claude Code blocks only on exit 2;
    Python's default exit 1 on an import crash is treated as *allow*, so a bare
    failure would silently disable the destruction guard in every repo with no
    signal. This converts that silent fail-open into a *detectable* one: a
    durable sentinel under ``<git-common-dir>/worktree-warden/gate-load-error``
    that the SessionStart banner surfaces on the next session start, plus a
    best-effort stderr line (which, on exit 0, reaches only Claude Code's debug
    log -- the sentinel is the user-visible channel). The hook still exits 0
    (documented fail-open) so a broken module never bricks the shell globally.

    Uses only the standard library imported above -- it must never depend on the
    very module whose import just failed. Best-effort throughout: never raises.

    Args:
        module_name: The sibling module(s) whose import failed.
        exc: The exception raised by the failed import.
        tb: Pre-formatted traceback text recorded in the sentinel body.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    diagnostic = (
        f"⛔ worktree-warden: {Path(__file__).name} could not import "
        f"'{module_name}': {exc!r}\n"
        "The destruction guard is FAILING OPEN -- unsafe worktree/branch "
        "destruction is NOT being blocked. Fix the import error in the plugin's "
        "scripts/ to restore protection.\n"
    )
    try:
        sys.stderr.write(diagnostic)
    except Exception:
        pass
    try:
        payload = json.load(sys.stdin)
        cwd = payload.get("cwd") or os.getcwd()
    except Exception:
        cwd = os.getcwd()
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            common = Path(os.path.realpath(os.path.join(cwd, proc.stdout.strip())))
            sentinel = common / GATE_LOAD_ERROR_RELPATH
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(f"{stamp}\t{module_name}\t{exc!r}\n\n{tb}")
    except Exception:
        pass


try:
    import worktree_destruction as wd  # noqa: E402  -- sibling module, path bootstrapped
    import worktree_gate as gate  # noqa: E402
except Exception as _exc:  # noqa: E402  -- fail open LOUDLY, never on Python's exit 1
    try:
        _report_gate_load_failure(
            "worktree_destruction/worktree_gate", _exc, traceback.format_exc()
        )
    except Exception:
        # The failsafe itself must never propagate -- an exception escaping here
        # would skip sys.exit(0) and let Python exit 1, which Claude Code reads
        # as *allow*: the silent fail-open this whole branch exists to prevent.
        pass
    sys.exit(0)


def _block(reason: str, recovery: str) -> int:
    """Emit the exit-2 refusal message and return code 2.

    Args:
        reason: Human-readable explanation of why the operation was blocked.
        recovery: Suggested recovery action to show the user.

    Returns:
        Always 2, for use as an exit code via ``return _block(...)``.
    """
    gate.log_event("destroy-block", reason, None)
    msg = (
        "⛔ worktree-warden blocked a destructive operation.\n\n"
        f"  Reason: {reason}\n"
    )
    if recovery:
        msg += f"\n  {recovery}\n"
    msg += (
        "\nThis worktree holds content that is not safely landed in the default "
        "branch. Land or commit it first, then retry — or, if you are certain "
        "the content is disposable, the user can run "
        "`worktree_gate disable` to turn the plugin's gates off.\n"
    )
    sys.stderr.write(msg)
    return 2


def main() -> int:
    """Evaluate the pending Bash / ExitWorktree call and block unsafe destruction."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        tool = payload.get("tool_name")
        cwd = payload.get("cwd") or os.getcwd()
        tool_input = payload.get("tool_input") or {}

        if tool == "Bash":
            command = tool_input.get("command") or ""
            if not command:
                return 0
            verdict = wd.evaluate_command(command, cwd)
        elif tool == "ExitWorktree":
            action = tool_input.get("action") or "keep"
            verdict = wd.evaluate_exit_worktree(action, cwd)
        else:
            return 0

        if verdict.allow:
            return 0
        return _block(verdict.reason, verdict.recovery)
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
