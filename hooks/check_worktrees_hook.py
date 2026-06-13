#!/usr/bin/env python3
"""SessionStart hook: surface this repo's git worktrees and inject enforcement context.

Fires only for startup/resume (enforced by hooks.json matchers; re-checked
defensively here). Two preconditions always hold before anything is output:
cwd is inside a git repo, and cwd is the repo's MAIN worktree (never a linked
worktree).

Outputs up to two JSON fields:

  ``systemMessage`` (user-facing banner, not injected into agent context):
    Governed by the ``startup_display`` setting — "always" (default), "mergeable",
    or "never". Shows a category breakdown and the worktree table.

  ``additionalInformation`` (injected into agent context):
    Present only when the worktree-first enforcement gate is active. Contains
    a MANDATORY directive instructing the agent to call EnterWorktree before
    any Edit or Write operation.

Exit code is always 0 — a failing SessionStart hook degrades every user session.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
_scripts_dir = (Path(_root) if _root else Path(__file__).resolve().parent.parent) / "scripts"
sys.path.insert(0, str(_scripts_dir))

if TYPE_CHECKING:
    import check_worktrees as cw

ALLOWED_SOURCES = {"startup", "resume"}

_GATE_LOAD_ERROR_ADVISORY = (
    "⚠️  gate-load-error: worktree-warden hooks failed to import their gate module "
    "at last startup — the worktree gate and destruction gate are OPEN "
    "(all edits and destructive operations are allowed). "
    "Fix the plugin installation to restore protection. "
    "Sentinel: <git-common-dir>/worktree-warden/gate-load-error"
)


def _read_load_error_advisory(git_common_dir: str | None) -> str:
    """Return a high-priority advisory string if the gate-load-error sentinel exists.

    Args:
        git_common_dir: Absolute path to the git common directory, or None.

    Returns:
        Non-empty advisory string when the sentinel is present, empty string otherwise.
        Never raises.
    """
    if not git_common_dir:
        return ""
    try:
        sentinel = Path(git_common_dir) / "worktree-warden" / "gate-load-error"
        if sentinel.exists():
            return _GATE_LOAD_ERROR_ADVISORY
        return ""
    except Exception:
        return ""


def _get_git_common_dir(cwd: str) -> str | None:
    """Return the git common dir for cwd, or None on any error."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        common = proc.stdout.strip()
        if not os.path.isabs(common):
            common = str(Path(cwd) / common)
        return common
    except Exception:
        return None


def read_stdin() -> dict[str, object]:
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


_ENFORCEMENT_MSG = (
    "MANDATORY: This repo enforces worktree-first editing. "
    "You MUST call EnterWorktree BEFORE using the Edit or Write tool on any file "
    "in this repository. "
    "The PreToolUse gate hard-blocks direct edits to the main checkout "
    "(exit code 2; no bypass exists under any permission mode). "
    "Required workflow: (1) call EnterWorktree, "
    "(2) make all edits inside the returned worktree path, "
    "(3) call /worktree-warden:finish-worktree when done."
)


def get_gate_state(cwd: str) -> tuple[str, bool, str | None]:
    """Return the effective (startup_display_mode, enforcement_active, disabled_scope).

    Reads the shared worktree-gate config (user scope overridden by project).
    Falls back to safe defaults on any error so a config-read failure never
    silences the banner or suppresses the enforcement directive unexpectedly.

    Args:
        cwd: Absolute path used to locate the repo's gate configuration.

    Returns:
        A tuple of (startup_display, enforcement_active, disabled_scope) where
        enforcement_active is True when the gate is enabled and disabled_scope
        is the scope that disabled it ("user" or "project"), or None if active.
    """
    try:
        import worktree_gate as wg  # noqa: PLC0415

        settings = wg.resolve_settings(wg.git_facts(cwd))
        return settings.startup_display, settings.disabled_scope is None, settings.disabled_scope
    except Exception:
        return "always", False, None


def gather(cwd: str) -> list[cw.Worktree]:
    """Resolve every linked worktree of cwd's repo, or [] on any error."""
    try:
        import check_worktrees as cw  # noqa: PLC0415

        return asyncio.run(asyncio.wait_for(cw.gather_worktrees(cwd), timeout=20.0))
    except Exception:
        return []


def build_banner(worktrees: list[cw.Worktree], mode: str) -> str | None:
    """Compose the SessionStart banner for the given mode, or None to stay silent.

    Args:
        worktrees: Every linked worktree of the repo (any readiness).
        mode: One of "mergeable", "always", "never".

    Returns:
        The banner text, or None when nothing should be shown — "never", no
        worktrees at all, or "mergeable" mode with nothing offerable.
    """
    if mode == "never" or not worktrees:
        return None

    # Safe import: a non-empty `worktrees` can only have come from gather(),
    # which already imported check_worktrees — so this is a sys.modules hit.
    import check_worktrees as cw  # noqa: PLC0415

    mergeable = [wt for wt in worktrees if wt.is_mergeable]
    cooldown = [wt for wt in worktrees if wt.readiness is cw.Readiness.COOLDOWN]
    blocked = [wt for wt in worktrees if wt.readiness is cw.Readiness.BLOCKED]

    if mode == "mergeable" and not mergeable:
        return None

    try:
        table = cw.render_table(worktrees)
    except Exception:
        table = ""

    header = _summary(len(worktrees), len(mergeable), len(cooldown), len(blocked))
    recommendation = _recommendation(
        len(mergeable), len(cooldown), len(blocked), cw.RECENT_WINDOW_SECONDS // 60
    )
    body = "\n\n".join(part for part in (table, recommendation) if part)
    return f"\n\n{header}\n\n{body}"


def _summary(total: int, n_merge: int, n_cool: int, n_block: int) -> str:
    """One-line header: total worktrees with a per-category breakdown."""
    noun = "worktree" if total == 1 else "worktrees"
    parts: list[str] = []
    if n_merge:
        parts.append(f"{n_merge} mergeable")
    if n_cool:
        parts.append(f"{n_cool} on cooldown")
    if n_block:
        parts.append(f"{n_block} in a live session")
    detail = f" — {', '.join(parts)}" if parts else ""
    return f"🌳 {total} git {noun} in this repo{detail}."


def _recommendation(n_merge: int, n_cool: int, n_block: int, cooldown_min: int) -> str:
    """Per-category lines describing what acting on each kind would do."""
    lines: list[str] = []
    if n_merge:
        them = "it" if n_merge == 1 else "them"
        lines.append(
            f"→ /merge-worktrees lands the {n_merge} mergeable into the default branch "
            f"by rebase + fast-forward (empty/already-merged ones are pruned instead); "
            f"/check-worktrees reviews {them} first."
        )
    if n_cool:
        lines.append(
            f"→ ⏳ on cooldown = edited in the last {cooldown_min} min, held back so "
            "half-baked work isn't auto-landed; merges once quiet, or land one now via "
            "/check-worktrees if you're sure."
        )
    if n_block:
        lines.append(
            "→ ❌ live session = a claude session is open in it (another tab/agent); "
            "shown for awareness, never auto-merged."
        )
    return "\n".join(lines)


def main() -> int:
    """Run the SessionStart hook."""
    payload = read_stdin()
    source = str(payload.get("source", ""))
    if source and source not in ALLOWED_SOURCES:
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())

    if not is_main_worktree(cwd):
        return 0

    mode, enforcement_active, disabled_scope = get_gate_state(cwd)

    # Gate-load-error is the highest-priority advisory: the hooks are not running.
    # Check it before the early-exit so it surfaces even in "never" mode.
    load_error_advisory = _read_load_error_advisory(_get_git_common_dir(cwd))

    if mode == "never" and not enforcement_active and disabled_scope != "project" and not load_error_advisory:
        return 0

    # Force-quit backstop + external-removal detection. Both are best-effort and
    # independent of the banner: capture WIP of any dirty worktree a prior
    # force-quit stranded (before it can be removed uncaptured), then diff the
    # worktree population against last session to surface removals warden did not
    # itself perform. Failures here must never affect the rest of the hook.
    removal_advisory = ""
    try:
        import worktree_wip  # noqa: PLC0415
        import worktree_population  # noqa: PLC0415

        worktree_wip.capture_dirty_orphans(cwd)
        removal_advisory = worktree_population.format_advisory(
            worktree_population.reconcile(cwd)
        )
    except Exception:
        removal_advisory = ""

    output: dict[str, str] = {}
    if mode != "never":
        banner = build_banner(gather(cwd), mode)
        if banner:
            output["systemMessage"] = banner

    # An external-removal advisory is high-priority — surface it above the banner,
    # and independently of startup_display (the user must see a silent loss even
    # in "never" mode).
    if removal_advisory:
        existing = output.get("systemMessage", "")
        output["systemMessage"] = (
            removal_advisory + ("\n\n" + existing if existing else "")
        )

    # Gate-load-error is the highest-priority advisory — prepend above all other
    # content, and surface it even when startup_display is "never".
    if load_error_advisory:
        existing = output.get("systemMessage", "")
        output["systemMessage"] = (
            load_error_advisory + ("\n\n" + existing if existing else "")
        )

    if enforcement_active:
        output["additionalInformation"] = _ENFORCEMENT_MSG
    elif disabled_scope == "project":
        # Project config (committable) disabled enforcement — surface it so the
        # user notices that a checked-in file is overriding the safety gate.
        warning = (
            "⚠️  Worktree gate DISABLED by project config "
            "(.claude/settings.worktree-warden.json). "
            "Direct main-checkout edits are not blocked in this repo. "
            "Run `worktree_gate enable` to re-enable, or verify this is intentional."
        )
        existing = output.get("systemMessage", "")
        output["systemMessage"] = (warning + "\n\n" + existing).strip()
        try:
            import worktree_gate as wg  # noqa: PLC0415
            wg.log_event("project-disabled-notice", "SessionStart: project config disabled enforcement", None)
        except Exception:
            pass

    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
