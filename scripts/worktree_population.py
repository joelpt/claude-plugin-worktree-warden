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

import calendar
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import TypedDict, cast

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(ts: object) -> float | None:
    """Parse an ``_audit()``-written UTC timestamp string to epoch seconds."""
    if not isinstance(ts, str):
        return None
    try:
        return calendar.timegm(time.strptime(ts, _TS_FORMAT))
    except ValueError:
        return None

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
    """Return True only when ``ref`` resolves AND is provably not an ancestor of target.

    Fails CLOSED toward "not unlanded": a ref that no longer resolves (gc'd) or a
    git error yields False, so a vanished/unresolvable SHA never produces a false
    "unlanded" advisory with a recovery command that would itself fail.
    """
    if not ref:
        return False
    if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo)[0] != 0:
        return False
    return _git(["merge-base", "--is-ancestor", ref, target], repo)[0] == 1


_ACCOUNTED_ACTIONS = ("teardown", "undo", "finish", "finish-many")


def _proven_torn_down_branches(rec: dict) -> list[str]:
    """Branch names this record proves warden actually tore down.

    `teardown`/`undo`/`finish` carry the branch at the top level, gated on the
    record's own `code`. `finish-many` audits a whole batch under one record;
    a branch counts only when ITS OWN entry in `details.teardown_results`
    shows success. `details.landed_branches` is deliberately NOT trusted here:
    it only proves the land step ran, not that the worktree was torn down — a
    batch that lands several branches and then fails a later teardown would
    otherwise wrongly excuse branches whose worktrees survive.
    """
    action = rec.get("action")
    if action in ("teardown", "undo", "finish"):
        if rec.get("code", 0) != 0:
            return []
        top = rec.get("branch")
        return [top] if top else []
    if action == "finish-many":
        details = rec.get("details")
        results = details.get("teardown_results") if isinstance(details, dict) else None
        if not isinstance(results, list):
            return []
        return [
            r["branch"]
            for r in results
            if isinstance(r, dict)
            and r.get("code", 0) == 0
            and isinstance(r.get("branch"), str)
        ]
    return []


def _warden_accounted(common: str, path: str, branch: str, since_ts: float | None) -> bool:
    """Return True if warden's audit log records a teardown/undo touching this worktree.

    A disappearance warden itself caused is expected, not an external removal.
    That includes the composite `finish`/`finish-many` commands, which tear a
    worktree down internally without ever writing a standalone `teardown`
    record. Branch-name matches are bounded to records at or after
    ``since_ts`` (the previous snapshot's save time): unbounded matching would
    let an old `finish` record for branch "feat" permanently suppress
    detection of a LATER, unrelated worktree that reuses the same branch name
    and is genuinely removed externally. Path matches need no such bound — a
    worktree path is not reused the way a branch name commonly is.
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
        if not isinstance(rec, dict):
            continue
        if rec.get("action") not in _ACCOUNTED_ACTIONS:
            continue
        if path and rec.get("code", 0) == 0 and rec.get("worktree") == path:
            return True
        if not branch:
            continue
        ts = _parse_ts(rec.get("ts"))
        if since_ts is not None and (ts is None or ts < since_ts):
            continue
        if branch in _proven_torn_down_branches(rec):
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
    snapshot_path = Path(common) / "worktree-warden" / _POPULATION_FILE
    try:
        # The previous snapshot's save time bounds branch-name matching in
        # _warden_accounted: only audit records at or after it could explain
        # something that disappeared since then. No prior snapshot (first run
        # for this repo) means nothing to bound against. Floor to whole
        # seconds: audit timestamps have only second precision, so an audit
        # record written the same wall-clock second as (but nanoseconds
        # before) this mtime must still count as "at or after" it.
        since_ts: float | None = float(int(snapshot_path.stat().st_mtime))
    except OSError:
        since_ts = None
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
        if _warden_accounted(common, path, branch, since_ts):
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

    # Skip the write for a repo with no worktree-tracking history at all (no
    # prior snapshot AND no linked worktrees right now) — reconcile() now runs
    # unconditionally from the hook, so without this every git repo on disk
    # would otherwise grow an empty `.git/worktree-warden/population.json`.
    if previous or current:
        _save_snapshot(common, current)
    return removals


def format_advisory(removals: list[ExternalRemoval], repo: str = "") -> str:
    """Render an external-removal advisory for the SessionStart banner.

    Args:
        removals: Entries produced by ``reconcile()``.
        repo: Primary checkout path, used to compose a runnable ``recover``
            command. When omitted, the trailing "run this" line is skipped
            rather than emit an unpasteable placeholder.

    """
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
    if repo:
        engine = Path(__file__).resolve().with_name("worktree_engine.py")
        cmd = f"python3 {shlex.quote(str(engine))} --repo {shlex.quote(repo)} recover"
        lines.append(f"  Run `{cmd}` for the full picture.")
    return "\n".join(lines)
