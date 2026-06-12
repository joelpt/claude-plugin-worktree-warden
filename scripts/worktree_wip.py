#!/usr/bin/env python3
"""Non-destructive capture of a worktree's uncommitted WIP into a bundle.

The destruction gate stops a *Bash-issued* command from throwing away un-landed
work, but it cannot see the harness's own background-isolation worktree cleanup,
which removes a worktree natively at session end. The only defense against losing
*uncommitted* content to a removal we never see is to capture it first.

``capture_wip`` snapshots everything in the working tree — tracked modifications
**and** untracked files — into a single commit built through a TEMPORARY index
(the real index and working tree are never touched), then writes that commit to a
git **bundle** under ``<git-common-dir>/worktree-warden/wip/``. Bundles live
*outside* all refs, so they are invisible to ``git log`` / ``git branch`` (no
history pollution) and self-contained; ``recover`` lists them and ``recover
--gc-days`` expires them. Recovery: ``git fetch <bundle> <ref>``.

Everything here is best-effort: any failure returns None and captures nothing, so
a Stop hook can call it without risk of blocking or erroring the session.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."


def _git(args: list[str], cwd: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a git command, returning (returncode, stripped stdout). Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except Exception:
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def _safe(name: str) -> str:
    """Reduce a branch name to filename-safe characters."""
    return "".join(c if c in _SAFE else "-" for c in name)[:80] or "wip"


def capture_wip(cwd: str) -> str | None:
    """Capture all uncommitted content in ``cwd``'s worktree into a bundle.

    Tracked modifications and untracked (non-ignored) files are both included.
    The working tree and the real index are never modified.

    Args:
        cwd: A path inside the linked worktree whose WIP to capture.

    Returns:
        The bundle file path as a string, or None when there is nothing to
        capture (clean worktree) or any step fails (best-effort).
    """
    rc, status = _git(["status", "--porcelain"], cwd)
    if rc != 0 or not status.strip():
        return None  # not a repo, or nothing uncommitted

    rc, common = _git(["rev-parse", "--git-common-dir"], cwd)
    if rc != 0 or not common:
        return None
    common_abs = common if os.path.isabs(common) else os.path.join(cwd, common)

    rc, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    branch = branch if rc == 0 and branch else "detached"
    rc, head = _git(["rev-parse", "HEAD"], cwd)
    if rc != 0 or not head:
        return None  # unborn HEAD — nothing to parent the capture on

    try:
        tmp_index = tempfile.NamedTemporaryFile(
            prefix="warden-wip-index-", delete=False
        )
        tmp_index.close()
    except OSError:
        return None
    env = {**os.environ, "GIT_INDEX_FILE": tmp_index.name}
    try:
        if _git(["read-tree", "HEAD"], cwd, env)[0] != 0:
            return None
        if _git(["add", "-A"], cwd, env)[0] != 0:
            return None
        rc, tree = _git(["write-tree"], cwd, env)
        if rc != 0 or not tree:
            return None
    finally:
        try:
            os.unlink(tmp_index.name)
        except OSError:
            pass

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rc, commit = _git(
        ["commit-tree", tree, "-p", head, "-m", f"warden wip {branch} {stamp}"],
        cwd,
    )
    if rc != 0 or not commit:
        return None

    # Unique per invocation: refs/worktree-warden/* is NOT per-worktree isolated,
    # so a fixed name would let two concurrent captures of the same repo clobber
    # each other's ref and lose one WIP — the exact failure this feature exists
    # to prevent. The pid+random token makes collisions effectively impossible.
    wip_ref = f"refs/worktree-warden/wip-staging-{os.getpid()}-{secrets.token_hex(4)}"
    if _git(["update-ref", wip_ref, commit], cwd)[0] != 0:
        return None
    try:
        wip_dir = Path(common_abs) / "worktree-warden" / "wip"
        wip_dir.mkdir(parents=True, exist_ok=True)
        bundle = wip_dir / f"wip-{_safe(branch)}-{stamp}-{os.getpid()}.bundle"
        # Thin bundle: include only the WIP delta (`wip_ref ^head`), with HEAD as
        # a prerequisite. The recovery scenario is "worktree directory removed,
        # repo intact", so HEAD's objects are always present to fetch against —
        # keeping the bundle tiny and fast even on a large repo (a full-ancestry
        # bundle could blow the subprocess timeout and capture nothing).
        rc, _ = _git(["bundle", "create", str(bundle), wip_ref, f"^{head}"], cwd)
        if rc != 0:
            return None
        return str(bundle)
    except OSError:
        return None
    finally:
        # Drop the transient staging ref so nothing lingers in the ref namespace.
        _git(["update-ref", "-d", wip_ref], cwd)


def capture_dirty_orphans(repo: str) -> list[str]:
    """Capture WIP for every dirty LINKED worktree of ``repo``.

    Called at SessionStart so a worktree a *force-quit* left dirty — its session
    gone without ever firing a Stop hook — gets its uncommitted work bundled
    before anything can remove the directory. This is the backstop the
    graceful-Stop capture cannot provide. Best-effort throughout.

    Args:
        repo: Primary checkout path.

    Returns:
        The bundle paths created (empty when nothing was dirty or on error).
    """
    rc, out = _git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return []
    bundles: list[str] = []
    first = True
    for block in out.split("\n\n"):
        if first:  # primary checkout
            first = False
            continue
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
                if os.path.isdir(path):
                    bundle = capture_wip(path)
                    if bundle:
                        bundles.append(bundle)
                break
    return bundles
