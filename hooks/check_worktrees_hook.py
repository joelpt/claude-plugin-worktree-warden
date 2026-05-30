#!/usr/bin/env python3
"""SessionStart hook: surface this repo's git worktrees.

Gate-then-inject. Fires only for startup/resume (enforced by hooks.json
matchers; re-checked defensively here). Two preconditions always hold before
anything is shown: cwd is inside a git repo, and cwd is the repo's MAIN
worktree (never a linked worktree — the review skill must not run from inside
one).

What it then surfaces is governed by the `startup_display` setting (resolved
through the shared worktree-gate config, user scope overridden by project):

  - "always" (default) — show a banner whenever the repo has >=1 linked
    worktree of any kind, so a worktree you forgot about (sitting on cooldown
    or open in another tab's live session) is still surfaced for awareness.
  - "mergeable" — show only when >=1 worktree is offerable for auto-merge
    (ready, mergeable-after-commit, or prunable); stay silent when every
    worktree is blocked by a live session or held on the recent-activity
    cooldown.
  - "never" — never show the banner.

The banner's `systemMessage` carries a category breakdown (mergeable /
cooldown / live-session), the same box-drawing table /check-worktrees renders
(every worktree, held-back ones included), and a concise recommendation of
what each command does. `systemMessage` is user-facing only — it is NOT added
to the agent's context and never instructs the agent to act. Merging is a
deliberate, explicit user opt-in (the user types the slash command), so
nothing relies on the agent honoring an injected instruction. This hook never
merges or mutates anything.

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
from typing import TYPE_CHECKING

_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
_scripts_dir = (Path(_root) if _root else Path(__file__).resolve().parent.parent) / "scripts"
sys.path.insert(0, str(_scripts_dir))

if TYPE_CHECKING:
    import check_worktrees as cw

ALLOWED_SOURCES = {"startup", "resume"}


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


def resolve_startup_display(cwd: str) -> str:
    """Return the effective startup-display mode for cwd's repo.

    Reads the shared worktree-gate config (user scope overridden by project).
    Falls back to the built-in default on any error — a config-read failure
    must never silence the banner unexpectedly nor crash the hook.
    """
    try:
        import worktree_gate as wg  # noqa: PLC0415

        return wg.resolve_settings(wg.git_facts(cwd)).startup_display
    except Exception:
        return "always"


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
    payload = read_stdin()
    source = str(payload.get("source", ""))
    if source and source not in ALLOWED_SOURCES:
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())

    if not is_main_worktree(cwd):
        return 0

    mode = resolve_startup_display(cwd)
    if mode == "never":
        return 0

    message = build_banner(gather(cwd), mode)
    if message:
        print(json.dumps({"systemMessage": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
