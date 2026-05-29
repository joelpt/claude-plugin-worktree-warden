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

import worktree_gate as gate  # noqa: E402  -- sibling module, path bootstrapped above


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
            return 0

        gate.log_event("block", decision.reason, file_path)
        sys.stderr.write(gate.block_message(facts, file_path))
        return 2
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
