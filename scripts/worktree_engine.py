#!/usr/bin/env python3
"""Deterministic git engine for the worktrees plugin.

Lands linked worktrees into a target branch by **rebase + fast-forward** (never
a merge commit), with the order-sensitive, non-judgemental steps scripted and
the judgement gaps (conflict resolution, ordering, test-failure decisions) left
to the calling skill. Generalises ~/.claude/skills/rmws/scripts/rmws.py to act
on an explicitly-named worktree from the primary checkout, and adds snapshot /
undo so a post-land abort can restore the repo to exactly its pre-land state.

Subcommands (each prints one JSON object on stdout, a summary on stderr, and
exits with the contract code below):

  preflight        resolve TARGET (symbolic-ref → 'main' fallback) and report
                   clean/dirty status for each branch's worktree. Replaces two+
                   model round-trips (symbolic-ref + per-worktree status checks)
                   with a single call.
  finish-preflight capture all identity data for /finish-worktree (primary,
                   target, branch, commit_count) in one call, replacing three
                   sequential model-issued git commands. Accepts optional
                   --target to override symbolic-ref and keep commit_count
                   consistent when the user passes a non-default target.
  land             preflight + rebase <branch> onto <target> (in the worktree)
                   + ff-merge into <target> (in the primary). On conflict the
                   rebase is LEFT IN PROGRESS for the caller to resolve.
  rebase-continue  `git rebase --continue` after the caller staged a resolution;
                   ff-merges when the rebase completes.
  snapshot         persist restore anchors to a JSON file (target tip, each
                   branch tip, and its worktree path) so a later undo can rebuild
                   the pre-land branch/target tips and the worktrees.
  undo             restore target + branches from a snapshot (scoped, AUTHORIZED
                   `git reset --hard`) and recreate any torn-down worktree on its
                   branch.
  teardown         idempotent worktree removal + branch -d + prune (post-land).
  recover          read-only scan for recoverable content: stranded (prunable)
                   worktrees whose branch holds commits not yet landed, plus WIP
                   capture bundles, each with the exact restore command. With
                   --gc-days N, deletes WIP bundles older than N days.

Exit codes:
  0   ok
  10  not applicable (branch == target, or nothing to land)
  11  worktree dirty (uncommitted non-noise changes)
  12  primary unsafe (off-target / dirty / path gate)
  13  rebase conflict (LEFT IN PROGRESS — resolve then rebase-continue)
  14  fast-forward merge failed
  15  git / internal error
  17  core.bare corruption detected
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

EXIT_OK = 0
EXIT_NOT_APPLICABLE = 10
EXIT_DIRTY_WORKTREE = 11
EXIT_PRIMARY_UNSAFE = 12
EXIT_REBASE_CONFLICT = 13
EXIT_MERGE_FAILED = 14
EXIT_GIT_ERROR = 15
EXIT_CORE_BARE = 17

# Harness-regenerated file; discarding it is user-authorized (mirrors rmws.py).
NOISE_PATH = ".claude/settings.local.json"

# Rebase must never block on an interactive editor.
_REBASE_ENV = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}


class GitError(RuntimeError):
    """A git subprocess exited non-zero where success was required."""


@dataclass
class Outcome:
    code: int
    status: str
    message: str
    target: str = ""
    branch: str = ""
    primary: str = ""
    worktree: str = ""
    details: dict[str, object] = field(default_factory=dict)

    def emit(self) -> int:
        print(json.dumps(self.__dict__, indent=2))
        print(f"[worktree-engine] {self.status}: {self.message}", file=sys.stderr)
        return self.code


def _git(args: list[str], cwd: str | None = None, check: bool = True, strip: bool = True) -> str:
    """Run a git command and return stdout. Raise GitError on failure if check."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} (cwd={cwd or '.'}) failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip() if strip else proc.stdout


def _git_rc(args: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a git command, returning (returncode, stdout, stderr) — never raises."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _primary_worktree(repo: str) -> str:
    """Absolute path of the repo's primary worktree (first porcelain record)."""
    for line in _git(["worktree", "list", "--porcelain"], cwd=repo).splitlines():
        if line.startswith("worktree "):
            return os.path.realpath(line[len("worktree ") :])
    raise GitError("could not determine primary worktree")


def _dirty_entries(cwd: str) -> list[str]:
    """`git status --porcelain -z` paths for cwd (rename/copy aware)."""
    raw = _git(["status", "--porcelain", "-z"], cwd=cwd, strip=False)
    tokens = [t for t in raw.split("\0") if t]
    entries: list[str] = []
    i = 0
    while i < len(tokens):
        status = tokens[i][:2]
        entries.append(tokens[i][3:])
        i += 2 if ("R" in status or "C" in status) else 1
    return entries


def _non_noise_dirty(cwd: str) -> list[str]:
    return [p for p in _dirty_entries(cwd) if p != NOISE_PATH]


def _tracked_dirty(cwd: str) -> list[str]:
    """Modified/staged/deleted TRACKED paths only (excludes untracked '??').

    Used for the primary-checkout safety gate: a fast-forward merge is safe
    against untracked files (incl. nested worktrees, which show as untracked),
    so only tracked modifications make the primary unsafe to land into.
    """
    raw = _git(["status", "--porcelain", "-z"], cwd=cwd, strip=False)
    tokens = [t for t in raw.split("\0") if t]
    out: list[str] = []
    i = 0
    while i < len(tokens):
        status, path = tokens[i][:2], tokens[i][3:]
        if status != "??" and path != NOISE_PATH:
            out.append(path)
        i += 2 if ("R" in status or "C" in status) else 1
    return out


def _neutralize_noise(worktree: str) -> bool:
    """Discard the known harness-regenerated settings file if present."""
    target = Path(worktree) / NOISE_PATH
    status = _git(["status", "--porcelain", "--", NOISE_PATH], cwd=worktree)
    if not status.strip():
        return False
    if status.lstrip().startswith("??"):
        target.unlink(missing_ok=True)
    else:
        _git(["checkout", "--", NOISE_PATH], cwd=worktree)
    return True


def _ahead_count(base: str, tip: str, repo: str) -> int:
    """Commits in *tip* not in *base*."""
    return int(_git(["rev-list", "--count", f"{base}..{tip}"], cwd=repo) or "0")


def _branch_exists(branch: str, repo: str) -> bool:
    rc, _, _ = _git_rc(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
    return rc == 0


def _is_ancestor(branch: str, target: str, repo: str) -> bool:
    rc, _, _ = _git_rc(["merge-base", "--is-ancestor", branch, target], cwd=repo)
    return rc == 0


def _worktree_for_branch(branch: str, repo: str) -> str | None:
    """Path of the linked worktree checked out at *branch*, else None."""
    current: str | None = None
    ref = f"refs/heads/{branch}"
    for line in _git(["worktree", "list", "--porcelain"], cwd=repo).splitlines():
        if line.startswith("worktree "):
            current = os.path.realpath(line[len("worktree ") :])
        elif line.startswith("branch ") and line[len("branch ") :] == ref:
            return current
    return None


def _registered_worktrees(repo: str) -> tuple[str, set[str]]:
    paths: list[str] = []
    for line in _git(["worktree", "list", "--porcelain"], cwd=repo).splitlines():
        if line.startswith("worktree "):
            paths.append(os.path.realpath(line[len("worktree ") :]))
    return (paths[0] if paths else ""), set(paths[1:])


def _core_bare(repo: str) -> bool:
    rc, out, _ = _git_rc(["config", "--bool", "core.bare"], cwd=repo)
    return rc == 0 and out == "true"


def _rebase_in_progress(worktree: str) -> bool:
    rc, git_dir, _ = _git_rc(["rev-parse", "--git-dir"], cwd=worktree)
    if rc != 0:  # worktree path gone / not a repo → not in progress
        return False
    gd = git_dir if os.path.isabs(git_dir) else os.path.join(worktree, git_dir)
    return os.path.exists(os.path.join(gd, "rebase-merge")) or os.path.exists(
        os.path.join(gd, "rebase-apply")
    )


def _primary_blocker(primary: str, target: str) -> tuple[str, dict]:
    """Return (reason, details) if primary is unsafe to ff-merge into, else ('', {})."""
    pb = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=primary)
    dirty = _tracked_dirty(primary)
    reasons: list[str] = []
    if pb != target:
        reasons.append(f"on '{pb}' (need '{target}')")
    if dirty:
        reasons.append(f"{len(dirty)} uncommitted change(s)")
    if reasons:
        return "; ".join(reasons), {"primary_branch": pb, "primary_uncommitted": dirty}
    return "", {}


def _conflicts(worktree: str) -> list[str]:
    out = _git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False)
    return [ln for ln in out.splitlines() if ln.strip()]


def _ff_merge(branch: str, target: str, repo: str, base: Outcome) -> Outcome:
    """ff-only merge *branch* into *target* (checked out in *repo*)."""
    ahead = _ahead_count(target, branch, repo)
    rc, _, err = _git_rc(["merge", "--ff-only", branch], cwd=repo)
    if rc != 0:
        base.code = EXIT_MERGE_FAILED
        base.status = "merge_failed"
        base.message = f"Fast-forward merge of '{branch}' into '{target}' failed: {err}"
        base.details = {"git_stderr": err}
        return base
    base.code = EXIT_OK
    base.status = "landed"
    base.message = f"Rebased and fast-forwarded {ahead} commit(s) from '{branch}' into '{target}'."
    base.details = {"commits_merged": ahead, "head": _git(["rev-parse", "HEAD"], cwd=repo)}
    return base


def cmd_land(worktree: str, branch: str, target: str, repo: str) -> Outcome:
    """Preflight + rebase <branch> onto <target> + ff-merge into primary."""
    try:
        primary = _primary_worktree(repo)
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc))
    wt = os.path.realpath(worktree)
    base = Outcome(EXIT_OK, "ok", "", target=target, branch=branch, primary=primary, worktree=wt)

    if _core_bare(repo):
        base.code, base.status = EXIT_CORE_BARE, "core_bare"
        base.message = "core.bare is true on a non-bare repo — refusing (corruption)."
        return base

    main_path, linked = _registered_worktrees(repo)
    if wt == main_path or wt not in linked:
        base.code, base.status = EXIT_PRIMARY_UNSAFE, "not_linked_worktree"
        base.message = f"'{wt}' is not a registered linked worktree of {repo}."
        return base

    if branch == target:
        base.code, base.status = EXIT_NOT_APPLICABLE, "not_applicable"
        base.message = f"Worktree is on '{target}' itself; nothing to land."
        return base

    wt_dirty = _non_noise_dirty(wt)
    if wt_dirty:
        base.code, base.status = EXIT_DIRTY_WORKTREE, "dirty_worktree"
        base.message = "Worktree has uncommitted changes; commit before landing."
        base.details = {"uncommitted": wt_dirty}
        return base

    reason, det = _primary_blocker(primary, target)
    if reason:
        base.code, base.status = EXIT_PRIMARY_UNSAFE, "primary_unsafe"
        base.message = f"Primary checkout ({primary}) unsafe: {reason}."
        base.details = det
        return base

    if _is_ancestor(branch, target, repo) and _ahead_count(target, branch, repo) == 0:
        base.code, base.status = EXIT_NOT_APPLICABLE, "already_merged"
        base.message = f"'{branch}' is already an ancestor of '{target}'; ready for teardown."
        return base

    base.details = {"behind_base": _ahead_count(branch, target, repo)}
    _neutralize_noise(wt)

    rc, _, err = _git_rc(["rebase", target], cwd=wt, env=_REBASE_ENV)
    if rc != 0:
        base.code, base.status = EXIT_REBASE_CONFLICT, "rebase_conflict"
        base.message = (
            f"Rebase of '{branch}' onto '{target}' hit conflicts and is LEFT IN "
            "PROGRESS. Resolve in the worktree, `git add`, then rebase-continue."
        )
        base.details = {"conflicts": _conflicts(wt), "git_stderr": err, "rebase_in_progress": True}
        return base

    return _ff_merge(branch, target, repo, base)


def cmd_rebase_continue(worktree: str, branch: str, target: str, repo: str) -> Outcome:
    """Resume an in-progress rebase after the caller staged a resolution."""
    try:
        primary = _primary_worktree(repo)
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc))
    wt = os.path.realpath(worktree)
    base = Outcome(EXIT_OK, "ok", "", target=target, branch=branch, primary=primary, worktree=wt)

    if not _rebase_in_progress(wt):
        base.code, base.status = EXIT_GIT_ERROR, "no_rebase"
        base.message = f"No rebase in progress in {wt}; run `land` first."
        return base

    # Primary may have drifted while the caller resolved conflicts — re-check
    # before resuming, since a clean rebase falls straight into the ff-merge.
    reason, det = _primary_blocker(primary, target)
    if reason:
        base.code, base.status = EXIT_PRIMARY_UNSAFE, "primary_unsafe"
        base.message = f"Primary checkout ({primary}) unsafe: {reason}."
        base.details = det
        return base

    rc, _, err = _git_rc(["rebase", "--continue"], cwd=wt, env=_REBASE_ENV)
    if rc != 0:
        if _rebase_in_progress(wt):
            base.code, base.status = EXIT_REBASE_CONFLICT, "rebase_conflict"
            base.message = "More conflicts after --continue; resolve, `git add`, continue again."
            base.details = {"conflicts": _conflicts(wt), "git_stderr": err, "rebase_in_progress": True}
            return base
        base.code, base.status = EXIT_GIT_ERROR, "git_error"
        base.message = f"rebase --continue failed: {err}"
        return base
    return _ff_merge(branch, target, repo, base)


class WorktreeStatus(TypedDict):
    """Per-branch status entry returned by cmd_preflight."""

    branch: str
    path: str | None
    clean: bool
    dirty_files: list[str]


def _worktrees_by_branch(repo: str) -> dict[str, str]:
    """Return a mapping of short branch name to realpath for every linked worktree.

    Runs ``git worktree list --porcelain`` once and builds the full map, so
    callers that need to resolve multiple branches avoid N separate subprocess
    calls.

    Args:
        repo: Primary checkout path.

    Returns:
        Dict mapping each linked worktree's branch name to its realpath.
        The primary worktree and detached-HEAD worktrees are excluded.
    """
    mapping: dict[str, str] = {}
    current: str | None = None
    for line in _git(["worktree", "list", "--porcelain"], cwd=repo).splitlines():
        if line.startswith("worktree "):
            current = os.path.realpath(line[len("worktree "):])
        elif line.startswith("branch ") and current is not None:
            ref = line[len("branch "):]
            if ref.startswith("refs/heads/"):
                mapping[ref[len("refs/heads/"):]] = current
    return mapping


_ORIGIN_HEAD_PREFIX = "refs/remotes/origin/"


def cmd_preflight(repo: str, branches: list[str]) -> Outcome:
    """Resolve the default branch and report cleanliness for each worktree.

    Combines the two model round-trips that merge-worktrees previously issued
    separately (``git symbolic-ref`` + per-worktree ``git status``) into one
    engine call. Returns everything the skill needs before the dirty-commit HITL
    step and snapshot.

    Args:
        repo: Primary checkout path.
        branches: Branch names to inspect.

    Returns:
        Outcome with ``details["target"]`` (resolved default branch) and
        ``details["worktrees"]`` (list of WorktreeStatus entries).
    """
    rc, ref_out, _ = _git_rc(
        ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo
    )
    target = (
        ref_out[len(_ORIGIN_HEAD_PREFIX):]
        if rc == 0 and ref_out.startswith(_ORIGIN_HEAD_PREFIX)
        else "main"
    )

    branch_to_path = _worktrees_by_branch(repo)
    worktrees: list[WorktreeStatus] = []
    for branch in branches:
        path = branch_to_path.get(branch)
        if path is not None:
            try:
                dirty_files = _non_noise_dirty(path)
            except OSError as exc:
                if not os.path.isdir(path):
                    recovery = (
                        f"git worktree prune && git worktree add"
                        f" {shlex.quote(path)} {shlex.quote(branch)}"
                    )
                    return Outcome(
                        EXIT_GIT_ERROR,
                        "stale_worktree",
                        f"Worktree directory for '{branch}' is missing: {path}\n"
                        f"Run: {recovery}",
                        target=target,
                        details={
                            "branch": branch,
                            "path": path,
                            "recovery": recovery,
                        },
                    )
                return Outcome(
                    EXIT_GIT_ERROR,
                    "git_error",
                    f"Worktree path {path!r} for branch '{branch}' is inaccessible: {exc}",
                    target=target,
                )
        else:
            dirty_files = []
        worktrees.append(
            WorktreeStatus(
                branch=branch,
                path=path,
                clean=not dirty_files,
                dirty_files=dirty_files,
            )
        )

    dirty_count = sum(1 for w in worktrees if not w["clean"])
    out = Outcome(
        EXIT_OK,
        "preflight",
        f"TARGET={target}; {len(worktrees)} worktree(s), {dirty_count} dirty.",
        target=target,
        details={"target": target, "worktrees": [dict(w) for w in worktrees]},
    )
    return out


def cmd_finish_preflight(worktree: str, target_override: str | None = None) -> Outcome:
    """Capture all identity data for /finish-worktree in one engine call.

    Replaces three sequential model-issued git calls (``git worktree list`` →
    PRIMARY, ``git symbolic-ref`` → TARGET, ``git rev-list --count`` →
    COMMIT_COUNT) with a single scripted call.  Accepts an optional
    ``target_override`` so ``commit_count`` is computed against the same target
    that will be used for the recap—critical when the user passes a non-default
    target via ``$ARGUMENTS`` in ``/finish-worktree``.

    Args:
        worktree: Absolute path of the linked worktree being finished.
        target_override: If provided, skip symbolic-ref lookup and use this
            branch as TARGET.  Pass when the user supplies a non-default target
            via ``$ARGUMENTS`` so that ``commit_count`` stays consistent.

    Returns:
        Outcome with ``details``: ``primary``, ``target``, ``branch``,
        ``commit_count``.
    """
    try:
        worktree = os.path.realpath(worktree)
        primary = _primary_worktree(worktree)
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree)
        if target_override:
            target = target_override
        else:
            rc, ref_out, _ = _git_rc(
                ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=worktree
            )
            target = (
                ref_out[len(_ORIGIN_HEAD_PREFIX):]
                if rc == 0 and ref_out.startswith(_ORIGIN_HEAD_PREFIX)
                else "main"
            )
        commit_count = int(
            _git(["rev-list", "--count", f"{target}..HEAD"], cwd=worktree) or "0"
        )
    except (GitError, ValueError) as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc))

    return Outcome(
        EXIT_OK,
        "finish_preflight",
        f"branch={branch}, primary={primary}, target={target}, commit_count={commit_count}",
        target=target,
        branch=branch,
        primary=primary,
        worktree=worktree,
        details={
            "primary": primary,
            "target": target,
            "branch": branch,
            "commit_count": commit_count,
        },
    )


def _git_common_dir(repo: str) -> str:
    """Absolute path of the repo's shared git dir (stable across worktrees)."""
    cd = _git(["rev-parse", "--git-common-dir"], cwd=repo)
    return cd if os.path.isabs(cd) else os.path.realpath(os.path.join(repo, cd))


def cmd_snapshot(
    repo: str, target: str, branches: list[str], out_path: str | None = None
) -> Outcome:
    """Persist restore anchors: target tip, each branch tip + its worktree path.

    `merge-worktrees` commits all worktree changes (tracked and untracked) before
    snapshotting, so each branch tip already captures the full content; undo only
    needs the SHA plus the worktree path to rebuild the worktree on that branch.
    The snapshot is written as JSON (default: under the shared git dir) and its
    path is emitted.

    Args:
        repo: Primary checkout path.
        target: Default branch being landed into.
        branches: Branch names whose worktrees are being landed.
        out_path: Where to write the snapshot JSON; defaults under the git dir.

    Returns:
        Outcome carrying the snapshot file path and captured anchors.
    """
    try:
        target_sha = _git(["rev-parse", target], cwd=repo)
        branch_info: dict[str, object] = {}
        for branch in branches:
            branch_info[branch] = {
                "sha": _git(["rev-parse", branch], cwd=repo),
                "worktree": _worktree_for_branch(branch, repo),
            }
        path = out_path or os.path.join(_git_common_dir(repo), "worktree-snapshot.json")
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc), target=target)

    data: dict[str, object] = {
        "target": target,
        "target_sha": target_sha,
        "branches": branch_info,
    }
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", f"could not write snapshot: {exc}", target=target)

    out = Outcome(EXIT_OK, "snapshot", f"Captured anchors for {target} + {len(branches)} branch(es).", target=target)
    out.details = {"snapshot_file": path, **data}
    return out


def cmd_undo(repo: str, snapshot_path: str) -> Outcome:
    """Restore target, branches, and worktrees from a snapshot.

    Restores the target and each branch tip (scoped, AUTHORIZED `git reset --hard`
    where a worktree exists; `update-ref` where it does not) and recreates any
    torn-down worktree on its branch. The recreated worktree is a clean checkout
    of the restored tip, which already holds all pre-land content (committed by
    `merge-worktrees` before the snapshot); whether to `git reset` that content
    back into the working tree is the user's call.

    Args:
        repo: Primary checkout path.
        snapshot_path: Path to the JSON written by `snapshot`.

    Returns:
        Outcome listing what was restored, or a partial/error state.
    """
    try:
        primary = _primary_worktree(repo)
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc))
    out = Outcome(EXIT_OK, "undone", "", primary=primary)

    try:
        data = json.loads(Path(snapshot_path).read_text())
    except (OSError, ValueError) as exc:
        out.code, out.status = EXIT_GIT_ERROR, "bad_snapshot"
        out.message = f"could not read snapshot {snapshot_path}: {exc}"
        return out

    target = str(data.get("target", ""))
    target_sha = data.get("target_sha")
    branches: dict[str, dict[str, object]] = data.get("branches", {}) or {}
    out.target = target

    anchors = [target_sha, *(info.get("sha") for info in branches.values())]
    for sha in anchors:
        # Explicit type guard before any git call: anchors come from on-disk JSON,
        # and a non-str (or flag-like) value must never reach a git command.
        if not isinstance(sha, str):
            out.code, out.status = EXIT_GIT_ERROR, "bad_anchor"
            out.message = f"Anchor {sha!r} is not a SHA string; refusing undo (nothing changed)."
            return out
        rc, _, _ = _git_rc(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], cwd=repo)
        if not sha or rc != 0:
            out.code, out.status = EXIT_GIT_ERROR, "bad_anchor"
            out.message = f"Anchor '{sha}' does not resolve to a commit; refusing undo (nothing changed)."
            return out

    # Capture what the scoped `reset --hard` is about to overwrite, BEFORE it
    # runs. git's reflog already records these (the real recovery path), but
    # recording them in the audit makes a stale-snapshot mistake — undo run
    # against a snapshot taken before later commits — diagnosable from one log,
    # not a reflog spelunk. Best-effort; never blocks the undo.
    pre_undo: dict[str, str] = {}
    rc_pre, cur_target, _ = _git_rc(["rev-parse", "HEAD"], cwd=primary)
    if rc_pre == 0:
        pre_undo[target] = cur_target
    for branch in branches:
        rc_br, br_sha, _ = _git_rc(["rev-parse", branch], cwd=repo)
        if rc_br == 0:
            pre_undo[branch] = br_sha

    restored: list[str] = []
    failed: list[str] = []

    # Target FIRST — main is what verify/tests read, so restore it even if a
    # later branch step fails.
    rc, _, err = _git_rc(["reset", "--hard", str(target_sha)], cwd=primary)
    (restored if rc == 0 else failed).append(
        f"{target}->{str(target_sha)[:8]}" if rc == 0 else f"{target}: {err}"
    )

    for branch, info in branches.items():
        sha = str(info.get("sha"))
        wt_path = info.get("worktree")
        cur_wt = _worktree_for_branch(branch, repo)

        if cur_wt is not None:
            if _rebase_in_progress(cur_wt):
                _git_rc(["rebase", "--abort"], cwd=cur_wt, env=_REBASE_ENV)
            rc, _, err = _git_rc(["reset", "--hard", sha], cwd=cur_wt)
        else:
            # update-ref is reached only when NO worktree (primary included —
            # _worktree_for_branch scans every porcelain record) holds the branch,
            # so it never desyncs a live checkout.
            rc, _, err = _git_rc(["update-ref", f"refs/heads/{branch}", sha], cwd=repo)

        if rc != 0:
            failed.append(f"{branch}: {err}")
            continue

        if isinstance(wt_path, str) and cur_wt is None:
            # Clear stale worktree admin records first so a prior (even partial)
            # teardown doesn't make `worktree add` think the path is still linked.
            _git_rc(["worktree", "prune"], cwd=repo)
            rc2, _, err2 = _git_rc(["worktree", "add", wt_path, branch], cwd=repo)
            if rc2 != 0:
                # Branch tip is already restored; only the checkout is missing.
                # `git worktree add` refuses a non-empty leftover dir — surface the
                # path so the user can clear it and re-add, rather than silently half-restore.
                failed.append(f"{branch} worktree ({wt_path}) — restore by hand: {err2}")
                continue

        restored.append(f"{branch}->{sha[:8]}")

    if failed:
        out.code, out.status = EXIT_GIT_ERROR, "partial_undo"
        out.message = (
            f"Undo PARTIAL — repo may be in an intermediate state. "
            f"Restored: {restored}. FAILED: {failed}."
        )
        out.details = {"restored": restored, "failed": failed, "pre_undo": pre_undo}
        return out
    out.message = f"Restored to snapshot: {', '.join(restored)}."
    out.details = {"restored": restored, "pre_undo": pre_undo}
    return out


def cmd_teardown(branch: str, target: str, repo: str, dry_run: bool) -> Outcome:
    """Idempotently remove a landed worktree + delete its branch + prune."""
    try:
        primary = _primary_worktree(repo)
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc), target=target, branch=branch)
    base = Outcome(EXIT_OK, "ok", "", target=target, branch=branch, primary=primary)

    wt_path = _worktree_for_branch(branch, repo)
    branch_exists = _branch_exists(branch, repo)

    if wt_path is None and not branch_exists:
        base.status, base.message = "noop", f"Nothing to tear down for '{branch}' (already gone)."
        if not dry_run:
            _git_rc(["worktree", "prune"], cwd=primary)
        base.details = {"worktree_removed": False, "branch_deleted": False}
        return base

    main_path, linked = _registered_worktrees(repo)
    if wt_path is not None:
        base.worktree = wt_path
        if wt_path == main_path or wt_path not in linked:
            base.code, base.status = EXIT_PRIMARY_UNSAFE, "path_gate_failed"
            base.message = f"'{wt_path}' is not a registered linked worktree; refusing teardown."
            return base
        dirty = _non_noise_dirty(wt_path)
        if dirty:
            base.code, base.status = EXIT_DIRTY_WORKTREE, "dirty_worktree"
            base.message = f"Worktree {wt_path} has uncommitted changes; refusing teardown."
            base.details = {"uncommitted": dirty}
            return base

    if branch_exists and not _is_ancestor(branch, target, repo):
        ahead = _ahead_count(target, branch, repo)
        base.code, base.status = EXIT_PRIMARY_UNSAFE, "branch_unmerged"
        base.message = f"Branch '{branch}' is not an ancestor of '{target}' ({ahead} unmerged); refusing."
        base.details = {"unmerged_commits": ahead}
        return base

    if dry_run:
        base.status, base.message = "dry_run", f"DRY RUN: would tear down '{branch}'."
        return base

    # Captured before deletion so the audit trail records exactly which commit
    # the torn-down branch pointed at (recoverable via reflog / it is in target).
    # Non-raising: a capture hiccup must not abort the teardown's structured path.
    _, branch_tip, _ = (
        _git_rc(["rev-parse", branch], cwd=repo) if branch_exists else (0, "", "")
    )

    if wt_path is not None:
        _neutralize_noise(wt_path)
        rc, _, err = _git_rc(["worktree", "remove", wt_path], cwd=primary)
        if rc != 0:
            base.code, base.status = EXIT_GIT_ERROR, "worktree_remove_failed"
            base.message = f"git worktree remove {wt_path} failed: {err}"
            return base
    if branch_exists:
        rc, _, err = _git_rc(["branch", "-d", branch], cwd=primary)
        if rc != 0:
            base.code, base.status = EXIT_GIT_ERROR, "branch_delete_failed"
            base.message = f"git branch -d {branch} failed: {err}"
            return base
    _git_rc(["worktree", "prune"], cwd=primary)
    base.status = "teardown_complete"
    base.message = f"Tore down '{branch}'" + (f" (worktree {wt_path})" if wt_path else "") + "."
    base.details = {
        "worktree_removed": wt_path is not None,
        "branch_deleted": branch_exists,
        "branch_tip": branch_tip,
    }
    return base


def _audit(repo: str, action: str, outcome: Outcome) -> None:
    """Append a forensic JSON line for an engine mutation; never raises.

    Written under the shared git dir (``<git-common-dir>/worktree-warden/
    audit.log``) so the trail lives with the repo and survives teardown. This is
    what makes a "mysteriously vanished worktree" diagnosable: every land /
    teardown / undo / snapshot records the action, status, branch, target,
    worktree path, and key SHAs with a UTC timestamp. Best-effort — auditing
    must never block or fail the operation it records.

    Args:
        repo: Primary checkout path.
        action: The engine subcommand that ran (e.g. ``"teardown"``).
        outcome: The Outcome the subcommand produced.
    """
    try:
        common = _git_common_dir(repo)
        log_dir = Path(common) / "worktree-warden"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "status": outcome.status,
            "code": outcome.code,
            "branch": outcome.branch,
            "target": outcome.target,
            "worktree": outcome.worktree,
            "details": outcome.details,
        }
        with (log_dir / "audit.log").open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        return


def _prunable_worktrees(repo: str) -> list[dict[str, str]]:
    """Linked worktrees git has marked ``prunable`` (their directory is gone).

    Returns one dict per prunable entry with ``path``, ``branch`` (short name or
    ""), and ``head`` SHA. These are the stranded worktrees — directory removed
    out from under git — whose committed work survives under the branch ref and
    is recoverable by re-adding the worktree.
    """
    out = _git(["worktree", "list", "--porcelain"], cwd=repo)
    blocks = out.split("\n\n")
    stranded: list[dict[str, str]] = []
    for block in blocks:
        path = branch = head = ""
        prunable = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line.startswith("branch "):
                branch = line[len("branch ") :].removeprefix("refs/heads/")
            elif line.startswith("HEAD "):
                head = line[len("HEAD ") :]
            elif line.startswith("prunable"):
                prunable = True
        if prunable and path:
            stranded.append({"path": path, "branch": branch, "head": head})
    return stranded


class WipBundle(TypedDict):
    """A captured WIP bundle file on disk."""

    path: str
    size: int
    mtime: float


def _wip_bundles(repo: str) -> list[WipBundle]:
    """List WIP capture bundles under ``<git-common-dir>/worktree-warden/wip``."""
    try:
        wip_dir = Path(_git_common_dir(repo)) / "worktree-warden" / "wip"
        if not wip_dir.is_dir():
            return []
        bundles: list[WipBundle] = []
        for f in sorted(wip_dir.glob("*.bundle")):
            st = f.stat()
            bundles.append(
                WipBundle(path=str(f), size=st.st_size, mtime=st.st_mtime)
            )
        return bundles
    except (OSError, GitError):
        # GitError from _git_common_dir is best-effort here, like OSError: a WIP
        # listing failure must not break cmd_recover's Outcome contract.
        return []


def cmd_recover(repo: str, target: str, gc_days: float | None) -> Outcome:
    """Surface recoverable content: stranded worktrees + WIP capture bundles.

    Read-only by default. Lists every ``prunable`` worktree (directory gone,
    branch ref alive) — flagging which still hold commits not landed in
    ``target`` — and every WIP bundle, each with the exact command to restore it.
    With ``gc_days`` set, deletes WIP bundles older than that many days (the only
    mutation; stranded worktrees and branch refs are never touched here).

    Args:
        repo: Primary checkout path.
        target: Default branch, for the landed/unlanded determination.
        gc_days: If set, delete WIP bundles older than this age in days.

    Returns:
        Outcome whose ``details`` carries ``stranded`` (list), ``bundles``
        (list), and ``gc_removed`` (list of deleted bundle paths).
    """
    base = Outcome(EXIT_OK, "recover", "", target=target)
    try:
        stranded_raw = _prunable_worktrees(repo)
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc), target=target)

    qrepo = shlex.quote(repo)
    stranded: list[dict[str, object]] = []
    for wt in stranded_raw:
        branch, head = wt["branch"], wt["head"]
        qpath = shlex.quote(wt["path"])
        # `prune` first: a prunable worktree's admin record is still registered,
        # so a bare `worktree add` fails ("missing but already registered").
        prune = f"git -C {qrepo} worktree prune && "
        if branch and _branch_exists(branch, repo):
            unlanded = not _is_ancestor(branch, target, repo)
            recovery = f"{prune}git -C {qrepo} worktree add {qpath} {shlex.quote(branch)}"
        elif head:
            # Detached HEAD: the commits live only at this SHA with no branch ref,
            # so re-add detached to keep them reachable (and recoverable).
            unlanded = not _is_ancestor(head, target, repo)
            recovery = f"{prune}git -C {qrepo} worktree add --detach {qpath} {shlex.quote(head)}"
        else:
            unlanded = False
            recovery = ""
        stranded.append({**wt, "unlanded": unlanded, "recovery": recovery})

    bundles = _wip_bundles(repo)
    gc_removed: list[str] = []
    if gc_days is not None:
        cutoff = time.time() - gc_days * 86400
        for b in bundles:
            if float(b["mtime"]) < cutoff:
                try:
                    Path(str(b["path"])).unlink()
                    gc_removed.append(str(b["path"]))
                except OSError:
                    pass
        bundles = [b for b in bundles if str(b["path"]) not in gc_removed]

    unlanded_n = sum(1 for s in stranded if s["unlanded"])
    base.status = "recover"
    base.message = (
        f"{len(stranded)} stranded worktree(s) ({unlanded_n} with unlanded "
        f"commits), {len(bundles)} WIP bundle(s)"
        + (f", {len(gc_removed)} bundle(s) gc'd" if gc_days is not None else "")
        + "."
    )
    base.details = {
        "stranded": stranded,
        "bundles": bundles,
        "gc_removed": gc_removed,
    }
    return base


def _refresh_main_lease(repo: str) -> None:
    """Best-effort renew this session's main-target lock lease (never raises).

    The merge skill acquires the main-target lock once and holds it for the whole
    operation, but each engine subcommand is a separate process, so the lease
    would otherwise only be renewed at acquire time. Renewing it on entry to each
    mutating step keeps a long merge's lease alive across steps. The gaps between
    steps (conflict resolution, ``just test``, HITL pauses) are deliberately not
    covered here -- the generous lease window and ``force-unlock`` are the
    backstops. Everything is lazy and guarded: a missing/broken lock module, an
    absent session id, or any IO error simply skips the refresh so the engine
    (and the merge) is never broken by the lock subsystem.

    Args:
        repo: The primary checkout path (the main-target key).
    """
    try:
        import worktree_lock  # noqa: PLC0415  -- lazy + guarded (fail-open)

        owner = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if not owner:
            return
        facts = worktree_lock.main_facts(repo)  # keys on the primary, like acquire
        if facts.is_repo and facts.git_common_dir is not None:
            worktree_lock.refresh(facts, owner, time.time())
    except Exception:
        pass


_LEASE_REFRESH_CMDS = frozenset({"land", "rebase-continue", "snapshot", "teardown", "undo"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worktree_engine.py")
    parser.add_argument("--repo", default=os.getcwd(), help="Primary checkout (default: cwd).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_land = sub.add_parser("land")
    p_land.add_argument("--worktree", required=True)
    p_land.add_argument("--branch", required=True)
    p_land.add_argument("--target", default="main")

    p_cont = sub.add_parser("rebase-continue")
    p_cont.add_argument("--worktree", required=True)
    p_cont.add_argument("--branch", required=True)
    p_cont.add_argument("--target", default="main")

    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--target", default="main")
    p_snap.add_argument("--branches", default="", help="Comma-separated branch names.")
    p_snap.add_argument("--out", default=None, help="Snapshot JSON path (default: under git dir).")

    p_pre = sub.add_parser("preflight")
    p_pre.add_argument("--branches", default="", help="Comma-separated branch names.")

    p_fp = sub.add_parser("finish-preflight")
    p_fp.add_argument(
        "--worktree",
        default=os.getcwd(),
        help="Linked worktree path (default: cwd).",
    )
    p_fp.add_argument(
        "--target",
        default=None,
        help="Override default branch (skips symbolic-ref lookup).",
    )

    p_undo = sub.add_parser("undo")
    p_undo.add_argument("--snapshot", required=True, help="Path to the snapshot JSON.")

    p_td = sub.add_parser("teardown")
    p_td.add_argument("--branch", required=True)
    p_td.add_argument("--target", default="main")
    p_td.add_argument("--dry-run", action="store_true")

    p_rec = sub.add_parser("recover")
    p_rec.add_argument("--target", default="main")
    p_rec.add_argument(
        "--gc-days",
        type=float,
        default=None,
        help="Delete WIP bundles older than this many days (the only mutation).",
    )

    args = parser.parse_args(argv)
    repo = args.repo
    if args.cmd in _LEASE_REFRESH_CMDS:
        _refresh_main_lease(repo)
    try:
        if args.cmd == "preflight":
            branches = [b for b in args.branches.split(",") if b]
            return cmd_preflight(repo, branches).emit()
        if args.cmd == "finish-preflight":
            return cmd_finish_preflight(args.worktree, args.target).emit()
        if args.cmd == "land":
            outcome = cmd_land(args.worktree, args.branch, args.target, repo)
            _audit(repo, "land", outcome)
            return outcome.emit()
        if args.cmd == "rebase-continue":
            outcome = cmd_rebase_continue(args.worktree, args.branch, args.target, repo)
            _audit(repo, "rebase-continue", outcome)
            return outcome.emit()
        if args.cmd == "snapshot":
            branches = [b for b in args.branches.split(",") if b]
            outcome = cmd_snapshot(repo, args.target, branches, args.out)
            _audit(repo, "snapshot", outcome)
            return outcome.emit()
        if args.cmd == "undo":
            outcome = cmd_undo(repo, args.snapshot)
            _audit(repo, "undo", outcome)
            return outcome.emit()
        if args.cmd == "teardown":
            outcome = cmd_teardown(args.branch, args.target, repo, args.dry_run)
            if not args.dry_run:
                _audit(repo, "teardown", outcome)
            return outcome.emit()
        if args.cmd == "recover":
            outcome = cmd_recover(repo, args.target, args.gc_days)
            if args.gc_days is not None:
                _audit(repo, "recover-gc", outcome)
            return outcome.emit()
    except GitError as exc:
        return Outcome(EXIT_GIT_ERROR, "git_error", str(exc)).emit()
    return EXIT_GIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
