#!/usr/bin/env python3
"""PreToolUse(Edit|Write) gate: block main-checkout edits unless excepted.

Blocking uses **exit code 2** (stderr fed back to Claude), NOT the JSON
``hookSpecificOutput.permissionDecision: "deny"`` route. This is load-bearing,
not stylistic: the JSON route is adjudicated *at* the permission layer, which
``permissions.defaultMode: "bypassPermissions"`` skips entirely -- under bypass
it would silently no-op, recreating the exact "loads fine, does nothing"
failure this plugin exists to prevent. Exit 2 runs *before* the permission
layer and blocks regardless of mode. Do not "modernize" this to JSON output.

The hook fails OPEN: any unexpected error allows the edit. It fires on every
Edit/Write in every repo where the plugin is enabled, so a crash must never
brick editing globally. The kill switch (opt-out) is evaluated first, inside
the gate's pure decision, so the escape hatch survives later-stage breakage.
The nuclear option remains disabling the plugin.
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
    failure would silently disable the gate in every repo with no signal. This
    converts that silent fail-open into a *detectable* one: a durable sentinel
    under ``<git-common-dir>/worktree-warden/gate-load-error`` that the
    SessionStart banner surfaces on the next session start, plus a best-effort
    stderr line (which, on exit 0, reaches only Claude Code's debug log -- the
    sentinel is the user-visible channel). The hook still exits 0 (documented
    fail-open) so a broken module never bricks editing globally.

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
        "The gate is FAILING OPEN -- edits to the main checkout are NOT being "
        "blocked. Fix the import error in the plugin's scripts/ to restore "
        "protection.\n"
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
    import worktree_gate as gate  # noqa: E402  -- sibling module, path bootstrapped above
except Exception as _exc:  # noqa: E402  -- fail open LOUDLY, never on Python's exit 1
    try:
        _report_gate_load_failure("worktree_gate", _exc, traceback.format_exc())
    except Exception:
        # The failsafe itself must never propagate -- an exception escaping here
        # would skip sys.exit(0) and let Python exit 1, which Claude Code reads
        # as *allow*: the silent fail-open this whole branch exists to prevent.
        pass
    sys.exit(0)


def _emit_unborn_notice(facts: gate.GitFacts, file_path: str | None) -> None:
    """Surface the one-time unborn-HEAD advisory on the allowed edit.

    Best-effort: the edit is already allowed via exit 0, so this only adds
    context. It rides ``hookSpecificOutput.additionalContext``, which the harness
    injects as a ``hook_additional_context`` attachment even under
    ``bypassPermissions`` (verified live on Claude Code 2.1.161 -- only the
    permission *adjudication* is skipped under bypass, not context injection).
    Kept best-effort regardless: its failure mode is benign (the model just
    misses a reassurance line), unlike the block path, which must use exit 2.
    Fires once per repo via an atomic sentinel claim.

    Args:
        facts: Git state for the current repository.
        file_path: The file being edited, for audit-log context.
    """
    try:
        if not gate.claim_unborn_notice(facts.git_common_dir):
            return
        gate.log_event("unborn", "edit allowed: repository has no commits yet", file_path)
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "additionalContext": gate.unborn_notice_message(),
                    }
                }
            )
            + "\n"
        )
    except Exception:
        pass


def main() -> int:
    """Evaluate the pending Edit/Write and block it when the gate says so."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        if payload.get("tool_name") not in ("Edit", "Write"):
            return 0
        cwd = payload.get("cwd") or os.getcwd()
        tool_input = payload.get("tool_input") or {}
        file_path = tool_input.get("file_path")
        # Resolve relative paths against the session cwd, not this process's cwd,
        # so the gate classifies the real edit target (decide() calls realpath).
        if file_path and not os.path.isabs(file_path):
            file_path = os.path.join(cwd, file_path)

        facts = gate.git_facts(cwd)
        settings = gate.resolve_settings(facts)
        decision = gate.decide(
            file_path=file_path,
            facts=facts,
            now=time.time(),
            disabled_scope=settings.disabled_scope,
            grant_expires_at=gate.read_grant_expiry(facts.git_common_dir),
        )

        if decision.allow:
            if decision.log_grant_use:
                gate.log_event("use", decision.reason, file_path)
            elif settings.disabled_scope == "project":
                gate.log_event("project-disabled", "edit allowed: project config disabled enforcement", file_path)
            elif settings.disabled_scope is None and facts.head_unborn:
                # Only when enforcement is actually live -- under a user-scope
                # disable the gate stays off after the first commit too, so the
                # "enforcement resumes" notice would be false.
                _emit_unborn_notice(facts, file_path)
            return 0

        gate.log_event("block", decision.reason, file_path)
        sys.stderr.write(gate.block_message(facts, file_path))
        return 2
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
