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

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT")
_SCRIPTS_DIR = (
    Path(_PLUGIN_ROOT) / "scripts"
    if _PLUGIN_ROOT
    else Path(__file__).resolve().parent.parent / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    import worktree_destruction as wd  # noqa: E402  -- sibling module, path bootstrapped
    import worktree_gate as gate  # noqa: E402

    _IMPORT_ERROR: ImportError | None = None
except (ImportError, Exception) as _exc:
    _IMPORT_ERROR = _exc if isinstance(_exc, ImportError) else ImportError(str(_exc))


def _drop_load_error_sentinel(_scripts_dir_for_import: Path, cwd: str | None) -> None:
    """Write a stderr diagnostic and drop the gate-load-error sentinel file.

    Self-contained stdlib-only: worktree_gate is unavailable at this call site,
    so the git-common-dir must be resolved via subprocess.  Never raises.
    """
    module_name = "worktree_destruction/worktree_gate"
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    sys.stderr.write(
        f"worktree-warden: failed to import {module_name} from {_scripts_dir_for_import} — "
        f"destruction gate is OPEN (all destructive operations allowed). "
        f"Fix the plugin installation to restore protection.\n"
        f"Error: {_IMPORT_ERROR}\n"
    )
    try:
        effective_cwd = cwd or os.getcwd()
        proc = subprocess.run(
            ["git", "-C", effective_cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return
        git_common_dir = proc.stdout.strip()
        if not os.path.isabs(git_common_dir):
            git_common_dir = str(Path(effective_cwd) / git_common_dir)
        sentinel = Path(git_common_dir) / "worktree-warden" / "gate-load-error"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(f"{ts} {module_name}\n")
    except Exception:
        pass


def _block(reason: str, recovery: str) -> int:
    """Emit the exit-2 refusal message and return code 2."""
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
    if _IMPORT_ERROR is not None:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            payload = {}
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        _drop_load_error_sentinel(_SCRIPTS_DIR, cwd)
        return 0

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
