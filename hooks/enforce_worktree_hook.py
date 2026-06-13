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

import datetime
import json
import os
import subprocess
import sys
import time
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
    import worktree_gate as gate  # noqa: E402  -- sibling module, path bootstrapped above

    _IMPORT_ERROR: ImportError | None = None
except (ImportError, Exception) as _exc:
    _IMPORT_ERROR = _exc if isinstance(_exc, ImportError) else ImportError(str(_exc))


def _drop_load_error_sentinel(_scripts_dir_for_import: Path, cwd: str | None) -> None:
    """Write a stderr diagnostic and drop the gate-load-error sentinel file.

    Self-contained stdlib-only: worktree_gate is unavailable at this call site,
    so the git-common-dir must be resolved via subprocess.  Never raises.
    """
    module_name = "worktree_gate"
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    sys.stderr.write(
        f"worktree-warden: failed to import {module_name} from {_scripts_dir_for_import} — "
        f"gate is OPEN (all edits allowed). Fix the plugin installation to restore protection.\n"
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
