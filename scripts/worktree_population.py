#!/usr/bin/env python3
"""Track a repo's worktree population across sessions to catch external removals.

The audit log proves what *warden* did; it cannot name a worktree that vanished
because something *else* removed it (the harness's background-isolation cleanup,
a stray ``rm -rf``, a force-quit followed by a cleanup). This module closes that
blind spot: at each SessionStart it records the repo's current worktree
population (path → branch/head/dirty) to a snapshot, and diffs against the prior
snapshot. A worktree that was present last session and is gone now — and that
warden's own audit log does NOT account for — is surfaced as an **external
removal**, with the last-known SHA so its content can be recovered.

Clean, landed worktrees that simply disappeared are not flagged (their removal
lost nothing). Only worktrees that were dirty, or whose branch still carries
commits not in the default branch, raise a flag — high signal, low noise.

All functions are best-effort and never raise: a tracking failure must never
break SessionStart.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import TypedDict, cast

_POPULATION_FILE = "population.json"
_AUDIT_FILE = "audit.log"
_NOISE_PATH = ".claude/settings.local.json"


class WorktreeRecord(TypedDict):
    """A single worktree's tracked state in the population snapshot."""

    branch: str
    head: str
    dirty: bool


class ExternalRemoval(TypedDict):
    """A worktree that vanished without warden accounting for it."""

    path: str
    branch: str
    head: str
    was_dirty: bool
    branch_alive: bool
    unlanded: bool
    recovery: str


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a git command, returning (rc, stripped stdout). Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def _common_dir(repo: str) -> str | None:
    rc, out = _git(["rev-parse", "--git-common-dir"], repo)
    if rc != 0 or not out:
        return None
    return out if os.path.isabs(out) else os.path.realpath(os.path.join(repo, out))


def _default_branch(repo: str) -> str:
    rc, out = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo)
    if rc == 0 and out.startswith("refs/remotes/origin/"):
        return out.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{cand}"], repo)[0] == 0:
            return cand
    return "main"


def _worktree_dirty(path: str) -> bool:
    """Best-effort: True if the worktree has non-noise uncommitted changes."""
    if not os.path.isdir(path):
        return False
    rc, out = _git(["status", "--porcelain"], path)
    if rc != 0:
        return False
    return any(
        line[3:] != _NOISE_PATH for line in out.splitlines() if line.strip()
    )


def current_population(repo: str) -> dict[str, WorktreeRecord] | None:
    """Map each LINKED worktree's realpath to its branch/head/dirty state.

    The primary checkout (first porcelain record) is excluded — only linked
    worktrees are tracked for disappearance.

    Args:
        repo: Primary checkout path.

    Returns:
        Dict of linked-worktree realpath → WorktreeRecord, or None when the live
        ``git worktree list`` read FAILED. None is distinct from an empty dict
        (a repo with genuinely no linked worktrees): the caller must not treat a
        failed read as "everything disappeared", which would false-flag every
        worktree and wipe the baseline.
    """
    rc, out = _git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return None
    pop: dict[str, WorktreeRecord] = {}
    blocks = out.split("\n\n")
    for i, block in enumerate(blocks):
        if i == 0:  # primary checkout
            continue
        path = branch = head = ""
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = os.path.realpath(line[len("worktree ") :])
            elif line.startswith("branch "):
                branch = line[len("branch ") :].removeprefix("refs/heads/")
            elif line.startswith("HEAD "):
                head = line[len("HEAD ") :]
        if path:
            pop[path] = WorktreeRecord(
                branch=branch, head=head, dirty=_worktree_dirty(path)
            )
    return pop


def _load_snapshot(common: str) -> dict[str, WorktreeRecord]:
    try:
        data = json.loads((Path(common) / "worktree-warden" / _POPULATION_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only well-shaped entries: a hand-corrupted or schema-drifted value
    # (string/list/None) would otherwise reach `rec.get(...)` and raise, crashing
    # reconcile BEFORE it re-saves — permanently wedging detection on the bad
    # file. Filtering here lets the next save self-heal the snapshot.
    return cast(
        "dict[str, WorktreeRecord]",
        {k: v for k, v in data.items() if isinstance(v, dict)},
    )


def _save_snapshot(common: str, population: dict[str, WorktreeRecord]) -> None:
    try:
        d = Path(common) / "worktree-warden"
        d.mkdir(parents=True, exist_ok=True)
        # Atomic write: a torn read by a concurrent SessionStart would parse as
        # empty and wipe the baseline. Write to a unique temp then os.replace.
        tmp = d / f"{_POPULATION_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(population, indent=2) + "\n")
        os.replace(tmp, d / _POPULATION_FILE)
    except OSError:
        return


def _definitely_unlanded(repo: str, ref: str, target: str) -> bool:
    """True only when ``ref`` resolves AND is provably not an ancestor of target.

    Fails CLOSED toward "not unlanded": a ref that no longer resolves (gc'd) or a
    git error yields False, so a vanished/unresolvable SHA never produces a false
    "unlanded" advisory with a recovery command that would itself fail.
    """
    if not ref:
        return False
    if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo)[0] != 0:
        return False
    return _git(["merge-base", "--is-ancestor", ref, target], repo)[0] == 1


def _warden_accounted(common: str, path: str, branch: str) -> bool:
    """True if warden's audit log records a teardown/undo touching this worktree.

    A disappearance warden itself caused (teardown after a land, or an undo) is
    expected, not an external removal.
    """
    try:
        lines = (Path(common) / "worktree-warden" / _AUDIT_FILE).read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("action") not in ("teardown", "undo"):
            continue
        if rec.get("worktree") == path or (branch and rec.get("branch") == branch):
            return True
    return False


def reconcile(repo: str) -> list[ExternalRemoval]:
    """Diff the current worktree population against the prior snapshot.

    Records the current population (for next session) and returns worktrees that
    disappeared without warden accounting for them AND that still hold content
    worth worrying about (were dirty, or their branch carries unlanded commits).

    Args:
        repo: Primary checkout path.

    Returns:
        List of ExternalRemoval entries (empty when nothing of concern vanished).
    """
    common = _common_dir(repo)
    if common is None:
        return []
    previous = _load_snapshot(common)
    current = current_population(repo)
    if current is None:
        # The live read failed — we cannot tell what is present. Do NOT flag
        # anything and do NOT overwrite the baseline; try again next session.
        return []
    target = _default_branch(repo)

    removals: list[ExternalRemoval] = []
    for path, rec in previous.items():
        if path in current:
            continue
        branch = rec.get("branch", "")
        head = rec.get("head", "")
        if _warden_accounted(common, path, branch):
            continue

        branch_alive = bool(branch) and _git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo
        )[0] == 0
        # Unlanded if the branch (or its last-known head) still carries commits
        # not in the default branch — i.e. real work that did not reach main.
        unlanded = _definitely_unlanded(repo, branch if branch_alive else head, target)

        if not rec.get("dirty") and not unlanded:
            continue  # clean and landed — its removal lost nothing

        qrepo, qpath = shlex.quote(repo), shlex.quote(path)
        recovery = ""
        if branch_alive:
            recovery = (
                f"git -C {qrepo} worktree prune && "
                f"git -C {qrepo} worktree add {qpath} {shlex.quote(branch)}"
            )
        elif head and _git(
            ["rev-parse", "--verify", "--quiet", f"{head}^{{commit}}"], repo
        )[0] == 0:
            # Branch gone but the commit object still resolves — pin it to a ref.
            recovery = f"git -C {qrepo} branch recovered-{head[:8]} {shlex.quote(head)}"

        removals.append(
            ExternalRemoval(
                path=path,
                branch=branch,
                head=head,
                was_dirty=bool(rec.get("dirty")),
                branch_alive=branch_alive,
                unlanded=unlanded,
                recovery=recovery,
            )
        )

    _save_snapshot(common, current)
    return removals


def format_advisory(removals: list[ExternalRemoval]) -> str:
    """Render an external-removal advisory for the SessionStart banner."""
    if not removals:
        return ""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        f"⚠️  worktree-warden: {len(removals)} worktree(s) disappeared since the "
        f"last session, and warden's audit log does not account for them — an "
        f"external removal (as of {stamp}):",
    ]
    for r in removals:
        flags = []
        if r["was_dirty"]:
            flags.append("had uncommitted changes")
        if r["unlanded"]:
            flags.append("had unlanded commits")
        label = r["branch"] or r["head"][:8] or "(unknown)"
        lines.append(f"  • {label} — {', '.join(flags) or 'content at risk'}")
        if r["recovery"]:
            lines.append(f"      recover: {r['recovery']}")
    lines.append(
        "  Run `python3 <plugin>/scripts/worktree_engine.py recover` for the full picture."
    )
    return "\n".join(lines)
