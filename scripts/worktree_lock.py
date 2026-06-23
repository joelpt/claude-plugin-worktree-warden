#!/usr/bin/env python3
"""Cooperative, advisory, per-worktree lock for serializing multi-step git work.

Git has no native "lock this branch for a span of edits" primitive -- only
transient, single-operation locks (``.git/index.lock``, ``refs/heads/<b>.lock``)
that it creates and removes around one ref/index update. Two concurrent
``git commit``s collide on ``index.lock`` and *error*; they do not silently
corrupt. The gap this module fills is the **logically-interleaved, multi-step**
operation -- above all the merge to the default branch, which warden orchestrates
across many separate ``worktree_engine.py`` invocations (preflight → snapshot →
land → rebase-continue → undo → teardown). Two of those interleaving on the same
``main`` is the real hazard; this lock serializes them.

The model is one lock keyed by ``realpath(worktree-toplevel)``. The main checkout
is itself a worktree, so its toplevel is the natural "main-target" key: two would
-be merges, and a merge racing a direct main edit, all contend on it, while two
sessions in *different* worktrees use different keys and never contend. The owner
is the Claude session id (``CLAUDE_CODE_SESSION_ID`` for CLI callers), so a lock
is re-entrant for its holder.

Crash safety without a liveness signal: there is no cheap, reliable way for a
short-lived process to know whether a session is paused or dead, so staleness is
a **sliding lease** (renewed by the holder's own activity) plus a deterministic
``force-unlock`` escape and a SessionStart surface. A force-killed session's lease
simply lapses after the window; nobody is left holding the ``flock`` (that is held
only for the milliseconds of each check-and-set), so the store never wedges.

**The guarantee is honest about its bound:** mutual exclusion holds only WITHIN a
lease window. An operation that pauses longer than its lease (e.g. a merge waiting
on a >2h human review) lapses the lease, and a second session may then reclaim the
key -- exactly what ``force-unlock`` is for, in reverse. Operation locks therefore
carry a long lease (``MERGE_LEASE_SECONDS``, renewed by each engine subcommand) to
outlast a realistic pause; occupancy carries the shorter default. That narrows the
lapse window (Option A); the engine closes it (Option B) by *aborting* a merge step
whose lease ``refresh`` reports ``"lost"`` (``--require-lease`` →
``EXIT_LEASE_LOST``), so a reclaimed key halts the merge before it can race a second
writer onto ``main`` rather than silently continuing.

The whole subsystem fails OPEN: any error proceeds *without* a lock rather than
blocking work, mirroring the rest of the plugin's gates.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import worktree_gate as gate

LOCKS_RELDIR = "worktree-warden"
LOCKS_FILENAME = "locks.json"
LOCKS_MUTEX_FILENAME = "locks.lock"

DEFAULT_LEASE_SECONDS = 30 * 60
LEASE_CONFIG_KEY = "lock_lease_seconds"
# Operation locks (merge/bumpall) get a much longer lease than occupancy: a merge
# spans many separate engine subprocesses with gaps (conflict resolution, tests,
# HITL pauses) that are not continuously refreshed, so the lease must outlast a
# realistic pause or it lapses mid-merge and a second merge could start. Mutual
# exclusion therefore holds only WITHIN a lease window; a pause longer than the
# window can lapse the lease (force-unlock is the deterministic escape).
MERGE_LEASE_SECONDS = 120 * 60
MERGE_LEASE_CONFIG_KEY = "merge_lease_seconds"
OCCUPANCY_CONFIG_KEY = "occupancy_lock"

VALID_KINDS = ("merge", "bumpall", "occupancy")
_OPERATION_KINDS = ("merge", "bumpall")


@dataclass(frozen=True)
class LockRecord:
    """One held lock on a single worktree key.

    Attributes:
        key: Realpath of the worktree toplevel this lock guards.
        owner: The session id holding the lock.
        kind: What the lock is for -- one of VALID_KINDS.
        reason: Human-readable purpose, surfaced when another session is blocked.
        acquired_at: Epoch seconds when the lock was first taken (preserved across
            refreshes; reset only on a fresh acquire or stale reclaim).
        last_active: Epoch seconds of the most recent acquire/refresh.
        expires_at: Epoch seconds after which the lease is stale and reclaimable.
    """

    key: str
    owner: str
    kind: str
    reason: str
    acquired_at: float
    last_active: float
    expires_at: float


@dataclass(frozen=True)
class LockDecision:
    """Outcome of evaluating one acquire/refresh against the current record.

    Attributes:
        outcome: One of "acquired", "refreshed", "blocked", or "lost".
        record: The record to persist (acquired/refreshed), or None.
        blocker: The live record held by another owner (blocked), or None.
    """

    outcome: str
    record: LockRecord | None
    blocker: LockRecord | None


def is_stale(record: LockRecord, now: float) -> bool:
    """Return whether a lock's lease has lapsed as of ``now``."""
    return now >= record.expires_at


def decide_lock(
    *,
    key: str,
    owner: str,
    kind: str,
    reason: str,
    now: float,
    existing: LockRecord | None,
    lease_seconds: float,
) -> LockDecision:
    """Rule on one acquire request. Pure: the caller resolves all I/O.

    A live lock held by a *different* owner blocks. A live lock held by the
    *same* owner is refreshed: only the lease is renewed; the lock's identity
    (``kind``, ``reason``, ``acquired_at``) is fixed at acquire time and preserved.
    Anything else -- no record, or a record whose lease has lapsed -- is acquired
    fresh.

    Args:
        key: The worktree-toplevel key being locked.
        owner: The requesting session id.
        kind: The lock kind (VALID_KINDS).
        reason: Human-readable purpose for the lock.
        now: Current epoch seconds (injected for testability).
        existing: The record currently at ``key``, or None.
        lease_seconds: Lifetime applied to the (re)acquired lease.

    Returns:
        The acquire/refresh/block decision.
    """
    if existing is not None and not is_stale(existing, now) and existing.owner != owner:
        return LockDecision("blocked", None, existing)
    if existing is not None and existing.owner == owner and not is_stale(existing, now):
        # Preserve the existing kind/reason -- a refresh renews the lease, it does
        # not redefine the lock. Otherwise a same-session occupancy edit during a
        # merge would relabel the live "merge" lock to "occupancy", which the
        # SessionStart prune then silently drops, destroying the crashed-merge
        # signal. (The new kind/reason args apply only to a fresh acquire below.)
        renewed = LockRecord(
            key,
            owner,
            existing.kind,
            existing.reason,
            existing.acquired_at,
            now,
            now + lease_seconds,
        )
        return LockDecision("refreshed", renewed, None)
    fresh = LockRecord(key, owner, kind, reason, now, now, now + lease_seconds)
    return LockDecision("acquired", fresh, None)


def _locks_dir(git_common_dir: str) -> Path:
    """Return the per-repo worktree-warden state dir under the shared git dir."""
    return Path(git_common_dir) / LOCKS_RELDIR


def locks_path(git_common_dir: str) -> Path:
    """Return the lock-store JSON path for a repo's shared git dir."""
    return _locks_dir(git_common_dir) / LOCKS_FILENAME


@contextlib.contextmanager
def _flock(git_common_dir: str) -> Iterator[None]:
    """Hold an exclusive ``flock`` over the repo's lock mutex for a check-and-set.

    The mutex is a sibling file to the store; the lock is held only for the
    enclosed read-modify-write, never across CLI invocations -- so a crashed
    holder leaves no flock behind, only a (lease-expirable) record.

    Args:
        git_common_dir: The repo's shared git dir.

    Yields:
        None, with the mutex held for the duration of the ``with`` block.
    """
    directory = _locks_dir(git_common_dir)
    directory.mkdir(parents=True, exist_ok=True)
    mutex = directory / LOCKS_MUTEX_FILENAME
    fd = os.open(str(mutex), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _coerce_float(value: object) -> float:
    """Coerce a stored JSON scalar to float; raise for anything non-numeric.

    Mirrors what ``float(...)`` accepted (int/float/numeric str) without leaking
    the parsed value's ``object`` type into the call -- ``bool`` is rejected
    explicitly since it is an ``int`` subclass that should never be a timestamp.

    Args:
        value: A scalar pulled from the parsed lock store.

    Returns:
        The value as a float.

    Raises:
        TypeError: The value is not a number or numeric string.
        ValueError: The value is a string that does not parse as a float.
    """
    if isinstance(value, bool):
        raise TypeError("expected a number, got bool")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _record_from_dict(data: object) -> LockRecord | None:
    """Parse one stored record, returning None for anything malformed."""
    if not isinstance(data, dict):
        return None
    fields = cast("dict[str, object]", data)
    try:
        return LockRecord(
            key=str(fields["key"]),
            owner=str(fields["owner"]),
            kind=str(fields.get("kind", "")),
            reason=str(fields.get("reason", "")),
            acquired_at=_coerce_float(fields["acquired_at"]),
            last_active=_coerce_float(fields["last_active"]),
            expires_at=_coerce_float(fields["expires_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _record_to_dict(record: LockRecord) -> dict[str, object]:
    """Serialize a record to a JSON-safe dict."""
    return {
        "key": record.key,
        "owner": record.owner,
        "kind": record.kind,
        "reason": record.reason,
        "acquired_at": record.acquired_at,
        "last_active": record.last_active,
        "expires_at": record.expires_at,
    }


def read_store(git_common_dir: str) -> dict[str, LockRecord]:
    """Read every valid lock record. Safe to call without the mutex.

    The store is written atomically (temp + rename), so a lock-free read can
    never see a torn file; malformed records are silently dropped. A wholly
    unreadable store (IO error) reads as ``{}`` -- the deliberate fail-open: an
    acquire then proceeds rather than wedging, accepting that under such a fault
    serialization is lost (the pre-feature state), never that work is blocked.

    Args:
        git_common_dir: The repo's shared git dir.

    Returns:
        A mapping of worktree-key to its current LockRecord.
    """
    raw = gate._load_json(locks_path(git_common_dir))
    store: dict[str, LockRecord] = {}
    for key, value in raw.items():
        record = _record_from_dict(value)
        if record is not None:
            store[str(key)] = record
    return store


def read_store_strict(git_common_dir: str) -> dict[str, LockRecord]:
    """Like ``read_store`` but RAISES on an unreadable/corrupt store.

    ``read_store`` (via ``gate._load_json``) fails OPEN: an IO error reads as an
    empty ``{}``, indistinguishable from a store that genuinely holds no records.
    That ambiguity is fine for acquire/prune/surface, but NOT for the lost-lease
    abort -- an IO fault must never masquerade as a force-unlocked lease and halt a
    healthy merge. This reader therefore lets ``OSError``/``ValueError`` propagate
    so the caller can fail OPEN (proceed) on a fault, while still treating a
    cleanly-read, record-less store as a genuine loss. A *missing* file is a
    genuine empty store (``force-unlock --all`` can rewrite the file with no keys,
    and a never-locked repo has none), not a fault, so it returns ``{}``.

    Args:
        git_common_dir: The repo's shared git dir.

    Returns:
        A mapping of worktree-key to its current LockRecord.

    Raises:
        OSError: The store file exists but could not be read.
        ValueError: The store file exists but is not valid JSON.
    """
    path = locks_path(git_common_dir)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        # A valid-JSON but non-object store (e.g. ``[]`` from partial corruption)
        # is structurally invalid, not an empty store. Raise so the caller fails
        # OPEN rather than reading it as a lost lease and spuriously aborting.
        raise ValueError(f"lock store is not a JSON object: {type(raw).__name__}")
    store: dict[str, LockRecord] = {}
    for key, value in raw.items():
        record = _record_from_dict(value)
        if record is None:
            # ``read_store`` silently drops a malformed record (fail-open); the
            # strict reader must NOT -- a torn-but-JSON-valid OWN record would
            # then read as a missing lease and spuriously abort a healthy merge.
            # Raise so the caller fails OPEN on the corruption instead.
            raise ValueError(f"lock store has a malformed record for key {key!r}")
        store[str(key)] = record
    return store


def _write_store(git_common_dir: str, store: dict[str, LockRecord]) -> None:
    """Persist the lock store atomically."""
    payload: dict[str, object] = {
        key: _record_to_dict(record) for key, record in store.items()
    }
    gate._write_json(locks_path(git_common_dir), payload)


def read_lease_seconds(facts: gate.GitFacts) -> int:
    """Return the effective lease length, resolving user → project config.

    Reuses the same two config files as the worktree gate; project overrides
    user. An absent or invalid value falls back to DEFAULT_LEASE_SECONDS, and the
    result is clamped to the gate's supported window bounds.

    Args:
        facts: Resolved git context (project config is keyed by its repo root).

    Returns:
        The lease length in seconds.
    """
    user_cfg = gate._load_json(gate.user_config_path())
    proj_cfg = gate._load_json(gate.project_config_path(facts.repo_root))
    seconds = DEFAULT_LEASE_SECONDS
    for cfg in (user_cfg, proj_cfg):
        value = cfg.get(LEASE_CONFIG_KEY)
        if isinstance(value, int) and not isinstance(value, bool):
            seconds = gate._clamp_window(value)
    return seconds


def read_merge_lease_seconds(facts: gate.GitFacts) -> int:
    """Return the operation-lock (merge/bumpall) lease length, user → project.

    Defaults to the long MERGE_LEASE_SECONDS so a merge survives a realistic
    HITL/test pause between engine subprocesses; configurable via
    ``merge_lease_seconds``. Clamped to the gate's supported window bounds.

    Args:
        facts: Resolved git context (project config is keyed by its repo root).

    Returns:
        The merge lease length in seconds.
    """
    user_cfg = gate._load_json(gate.user_config_path())
    proj_cfg = gate._load_json(gate.project_config_path(facts.repo_root))
    seconds = MERGE_LEASE_SECONDS
    for cfg in (user_cfg, proj_cfg):
        value = cfg.get(MERGE_LEASE_CONFIG_KEY)
        if isinstance(value, int) and not isinstance(value, bool):
            seconds = gate._clamp_window(value)
    return seconds


def _lease_for_kind(facts: gate.GitFacts, kind: str) -> int:
    """Return the lease length appropriate to a lock kind.

    Operation locks (merge/bumpall) get the long merge lease; occupancy gets the
    shorter default. Used by BOTH acquire and refresh so the engine's
    per-subcommand refresh renews a merge lease at its own (long) length rather
    than silently shrinking it to the occupancy default.

    Args:
        facts: Resolved git context.
        kind: The lock kind.

    Returns:
        The lease length in seconds for that kind.
    """
    if kind in _OPERATION_KINDS:
        return read_merge_lease_seconds(facts)
    return read_lease_seconds(facts)


def occupancy_enabled(facts: gate.GitFacts) -> bool:
    """Return whether per-worktree occupancy locking is enabled (default True).

    Resolves the same user → project config files as the gate; project overrides
    user. Lets a user turn occupancy off (``"occupancy_lock": false``) without
    disabling the rest of the plugin.

    Args:
        facts: Resolved git context (project config is keyed by its repo root).

    Returns:
        True when occupancy locking is active.
    """
    user_cfg = gate._load_json(gate.user_config_path())
    proj_cfg = gate._load_json(gate.project_config_path(facts.repo_root))
    enabled = True
    for cfg in (user_cfg, proj_cfg):
        value = cfg.get(OCCUPANCY_CONFIG_KEY)
        if isinstance(value, bool):
            enabled = value
    return enabled


def occupancy_block_message(blocker: LockRecord, repo: str) -> str:
    """Compose the exit-2 message shown when another session occupies a worktree.

    Args:
        blocker: The live lock record held by the other session.
        repo: The occupied worktree's toplevel (shown + used in the recovery hint).

    Returns:
        The stderr message, naming the holder and the exact force-unlock command.
    """
    age = _format_age(time.time() - blocker.last_active)
    return (
        "🔒 worktree-warden: this worktree is occupied by another session.\n"
        f"   {repo}\n"
        f"   held by session {blocker.owner} ({blocker.kind}, active {age}).\n"
        "   Two sessions editing one worktree can clobber each other. Wait for it "
        "to finish, or — if that session is gone — release it:\n"
        f"     {_cli_prefix()} force-unlock --repo {shlex.quote(repo)}"
    )


def prune_stale_occupancy(git_common_dir: str, now: float) -> list[str]:
    """Drop stale ``occupancy`` records so abandoned claims don't accrue.

    Occupancy locks have no explicit release (a session holds a worktree only via
    its sliding lease); when a session ends, its claim lapses and is meaningless.
    Pruning lapsed occupancy records at SessionStart keeps the store tidy and
    prevents stale-lock alarm-fatigue, while leaving stale *operation* locks
    (merge/bumpall) in place -- those are actionable (a crashed merge).

    Args:
        git_common_dir: The repo's shared git dir (where the store lives).
        now: Current epoch seconds.

    Returns:
        The keys pruned.
    """
    if not locks_path(git_common_dir).exists():
        return []  # nothing to prune; don't create the mutex on a quiescent repo
    with _flock(git_common_dir):
        store = read_store(git_common_dir)
        pruned = [
            key
            for key, record in store.items()
            if record.kind == "occupancy" and is_stale(record, now)
        ]
        for key in pruned:
            del store[key]
        if pruned:
            _write_store(git_common_dir, store)
    return pruned


def acquire(
    facts: gate.GitFacts, owner: str, kind: str, reason: str, now: float
) -> LockDecision:
    """Acquire (or refresh, or be blocked on) the lock for this repo's key.

    Args:
        facts: Resolved git context; ``repo_root`` is the key, ``git_common_dir``
            the store location.
        owner: The requesting session id.
        kind: The lock kind (VALID_KINDS).
        reason: Human-readable purpose for the lock.
        now: Current epoch seconds.

    Returns:
        The decision; on acquire/refresh the record has already been persisted.

    Raises:
        ValueError: If ``kind`` is not a known kind. ``kind`` is load-bearing --
            ``prune_stale_occupancy`` and ``session_advisory`` branch on it -- so a
            stray kind must surface, not silently corrupt the prune/surface rules.
            A ``raise`` (not ``assert``) so it survives ``python -O``. Callers wrap
            ``acquire`` in their own try/except and fail open, so this never bricks
            an edit; it only makes the bug traceable.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown lock kind: {kind!r}")
    assert facts.git_common_dir is not None and facts.repo_root is not None
    lease = _lease_for_kind(facts, kind)
    with _flock(facts.git_common_dir):
        store = read_store(facts.git_common_dir)
        existing = store.get(facts.repo_root)
        decision = decide_lock(
            key=facts.repo_root,
            owner=owner,
            kind=kind,
            reason=reason,
            now=now,
            existing=existing,
            lease_seconds=lease,
        )
        if decision.record is not None:
            store[facts.repo_root] = decision.record
            _write_store(facts.git_common_dir, store)
    return decision


def _refresh_with(
    facts: gate.GitFacts,
    owner: str,
    now: float,
    reader: Callable[[str], dict[str, LockRecord]],
) -> LockDecision:
    """Renew this owner's live lease under the mutex, reading via ``reader``.

    The whole check-renew-or-report-loss runs inside a single ``_flock`` so the
    record decided on is the record written -- no read/decide/re-read window.

    Args:
        facts: Resolved git context.
        owner: The session id expected to hold the lock.
        now: Current epoch seconds.
        reader: The store reader -- ``read_store`` (fail-open) or
            ``read_store_strict`` (raises on a corrupt/unreadable store).

    Returns:
        A "refreshed" decision (record persisted) or a "lost" decision whose
        ``blocker`` is whatever record currently occupies the key (or None).
    """
    assert facts.git_common_dir is not None and facts.repo_root is not None
    with _flock(facts.git_common_dir):
        store = reader(facts.git_common_dir)
        existing = store.get(facts.repo_root)
        if existing is not None and existing.owner == owner and not is_stale(existing, now):
            # Renew at the lease length for the record's OWN kind, so a merge lock
            # refreshed between engine subcommands keeps its long window.
            lease = _lease_for_kind(facts, existing.kind)
            renewed = replace(existing, last_active=now, expires_at=now + lease)
            store[facts.repo_root] = renewed
            _write_store(facts.git_common_dir, store)
            return LockDecision("refreshed", renewed, None)
        return LockDecision("lost", None, existing)


def refresh(facts: gate.GitFacts, owner: str, now: float) -> LockDecision:
    """Renew the holder's own live lease; no-op if it is gone or foreign.

    Called best-effort by each engine subcommand so a long merge keeps its lease
    alive between steps. It deliberately never *acquires* -- only the operation's
    entry point acquires -- so a refresh that finds the lock missing or taken by
    someone else (e.g. after a force-unlock) just reports "lost". Reads fail-open:
    an IO fault reads as an empty store and reports "lost" without raising.

    Args:
        facts: Resolved git context.
        owner: The session id expected to hold the lock.
        now: Current epoch seconds.

    Returns:
        A "refreshed" decision (record persisted) or a "lost" decision.
    """
    return _refresh_with(facts, owner, now, read_store)


def refresh_strict(facts: gate.GitFacts, owner: str, now: float) -> LockDecision:
    """Like ``refresh`` but reads strictly, so a fault RAISES instead of "lost".

    The lost-lease abort must distinguish a genuine loss (clean store, our record
    gone) from a transient fault (which fail-open ``refresh`` would misreport as
    "lost"). Reading via ``read_store_strict`` under the *same* flock that does the
    renewal collapses that into one atomic, race-free decision: a corrupt or
    unreadable store raises here so the caller can fail OPEN (proceed, no abort),
    while a cleanly-read store missing our live record is a true loss.

    Args:
        facts: Resolved git context.
        owner: The session id expected to hold the lock.
        now: Current epoch seconds.

    Returns:
        A "refreshed" or "lost" decision, derived from a single strict read.

    Raises:
        OSError: The store file exists but could not be read.
        ValueError: The store file exists but is structurally invalid.
    """
    return _refresh_with(facts, owner, now, read_store_strict)


def release(facts: gate.GitFacts, owner: str) -> bool:
    """Release the lock iff this owner holds it.

    Args:
        facts: Resolved git context.
        owner: The session id that must own the lock for release to occur.

    Returns:
        True if a lock owned by ``owner`` was removed, else False.
    """
    assert facts.git_common_dir is not None and facts.repo_root is not None
    with _flock(facts.git_common_dir):
        store = read_store(facts.git_common_dir)
        existing = store.get(facts.repo_root)
        if existing is not None and existing.owner == owner:
            del store[facts.repo_root]
            _write_store(facts.git_common_dir, store)
            return True
    return False


def force_unlock(facts: gate.GitFacts, all_keys: bool) -> list[str]:
    """Remove this repo's lock (or every lock in the repo) regardless of owner.

    The deterministic human escape hatch for a stale lock left by a force-killed
    session, surfaced with the exact command by the SessionStart banner.

    Args:
        facts: Resolved git context.
        all_keys: Remove every key in the store when True; only this repo's key
            otherwise.

    Returns:
        The list of keys removed.
    """
    assert facts.git_common_dir is not None and facts.repo_root is not None
    with _flock(facts.git_common_dir):
        store = read_store(facts.git_common_dir)
        removed = list(store) if all_keys else ([facts.repo_root] if facts.repo_root in store else [])
        for key in removed:
            del store[key]
        if removed:
            _write_store(facts.git_common_dir, store)
    return removed


def session_advisory(git_common_dir: str, now: float) -> str | None:
    """Compose a SessionStart banner line for this repo's locks, or None if quiet.

    Active *operation* locks are surfaced as awareness (a merge or bumpall is
    running in another session); a live *occupancy* lock is steady state (a session
    simply editing its worktree) and is deliberately NOT surfaced -- it would be
    per-SessionStart noise. Stale locks are surfaced as *actionable*, with the exact
    ``force-unlock`` command, since a stale lock is the residue of a force-killed
    holder and is the one thing a human may need to clear. Best-effort: any error
    reading the store yields None so a session never fails to start over a lock.

    Args:
        git_common_dir: The repo's shared git dir (where the store lives).
        now: Current epoch seconds.

    Returns:
        The advisory text, or None when there are no locks to report.
    """
    try:
        store = read_store(git_common_dir)
    except Exception:
        return None
    if not store:
        return None
    active: list[str] = []
    stale: list[str] = []
    for key, record in sorted(store.items()):
        age = _format_age(now - record.last_active)
        if is_stale(record, now):
            stale.append(
                f"  ⚠️  STALE: {key}\n"
                f"      from session {record.owner} ({record.kind}, last active {age}). "
                "If that session is gone, clear it:\n"
                f"        {_cli_prefix()} force-unlock --repo {shlex.quote(key)}"
            )
        elif record.kind != "occupancy":
            active.append(
                f"  🔒 {key} — session {record.owner} ({record.kind}, active {age})"
            )
    if not active and not stale:
        return None
    return "🔐 worktree-warden locks in this repo:\n" + "\n".join(stale + active)


def _format_age(seconds: float) -> str:
    """Render an elapsed-seconds count as a compact human duration."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _primary_toplevel(cwd: str) -> str | None:
    """Return the primary checkout's toplevel via ``git worktree list``, or None.

    The first ``worktree`` record git emits is always the main checkout.

    Args:
        cwd: Any path inside the repository.

    Returns:
        The realpath of the primary checkout toplevel, or None on any git error.
    """
    rc, out = gate.run_git(["worktree", "list", "--porcelain"], cwd)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("worktree "):
            return os.path.realpath(line[len("worktree ") :])
    return None


def main_facts(repo: str | None) -> gate.GitFacts:
    """Resolve git context keyed on the PRIMARY checkout (the main-target key).

    Every current lock command targets ``main``, so the lock must key on the
    primary checkout's toplevel regardless of which worktree it is invoked from.
    Otherwise two merges launched from two different linked worktrees would key on
    different records and never contend -- silently defeating serialization, the
    one guarantee this lock exists to provide. When ``repo`` already resolves to
    the primary (the nominal case -- the merge skill and bumpall pass it), this is
    a no-op; the linked-worktree branch is defense-in-depth for any direct caller.

    Args:
        repo: A repo/worktree path, or None to use cwd.

    Returns:
        Resolved GitFacts whose ``repo_root`` is the primary checkout toplevel.
    """
    path = os.path.realpath(repo) if repo else os.getcwd()
    facts = gate.git_facts(path)
    if facts.in_linked_worktree and facts.repo_root is not None:
        primary = _primary_toplevel(facts.repo_root)
        if primary is None:
            # Cannot determine the main-target key (git error). Return a non-repo
            # context so callers fail OPEN (proceed without a lock) rather than key
            # on the linked worktree's own path -- which would silently let two
            # merges launched from two worktrees not contend.
            return gate.GitFacts(False, None, None, False)
        return gate.git_facts(primary)
    return facts


def _repo_facts(repo: str | None) -> gate.GitFacts:
    """Resolve git context for ``repo`` WITHOUT primary resolution.

    Used by ``force-unlock``, whose key is the literal worktree the caller names
    -- a linked worktree's own occupancy key -- not the primary-resolved key the
    main-target commands use. Resolving to the primary here would clear the wrong
    lock (and the occupancy block message's force-unlock hint names the worktree).

    Args:
        repo: A repo/worktree path, or None to use cwd.

    Returns:
        GitFacts whose ``repo_root`` is the named path's own worktree toplevel.
    """
    return gate.git_facts(os.path.realpath(repo) if repo else os.getcwd())


def _resolve_owner(explicit: str | None) -> str:
    """Return the explicit owner, else the session id from the environment."""
    return explicit or os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def _fail_open(message: str) -> int:
    """Print a fail-open warning and return 0 so callers proceed unlocked."""
    print(f"⚠️  worktree-warden lock: {message}; proceeding WITHOUT a lock.")
    return 0


def cmd_acquire_main(args: argparse.Namespace) -> int:
    """Acquire the main-target lock; exit 1 only when blocked by another owner."""
    facts = main_facts(args.repo)
    if not facts.is_repo or facts.git_common_dir is None or facts.repo_root is None:
        return _fail_open("not inside a git repository")
    owner = _resolve_owner(args.owner)
    if not owner:
        return _fail_open("no session id (CLAUDE_CODE_SESSION_ID unset)")
    reason = " ".join(args.reason).strip() or "(no reason given)"
    try:
        decision = acquire(facts, owner, args.kind, reason, time.time())
    except Exception as exc:
        return _fail_open(f"lock subsystem error ({exc!r})")
    if decision.outcome == "blocked":
        assert decision.blocker is not None
        blocker = decision.blocker
        age = _format_age(time.time() - blocker.last_active)
        gate.log_event("lock-block", f"{facts.repo_root} held by {blocker.owner}", None)
        print(
            f"⛔ worktree-warden LOCK BLOCKED: {facts.repo_root}\n"
            f"   held by session {blocker.owner} ({blocker.kind}, active {age}; "
            f"reason: {blocker.reason}).\n"
            "   Another session is doing main-side work here. Wait for it, or — if "
            "you are certain it is dead — override:\n"
            f"     {_cli_prefix()} force-unlock --repo {shlex.quote(facts.repo_root)}"
        )
        return 1
    try:
        lease_min = read_lease_seconds(facts) // 60
    except Exception:
        lease_min = DEFAULT_LEASE_SECONDS // 60  # lock is already held; never fail the success path
    gate.log_event("lock-acquire", f"{decision.outcome} {facts.repo_root} by {owner}", None)
    verb = "ACQUIRED" if decision.outcome == "acquired" else "REFRESHED"
    print(
        f"🔒 worktree-warden LOCK {verb}: {facts.repo_root}\n"
        f"   owner {owner} ({args.kind}), ~{lease_min}m lease. Release with: "
        f"{_cli_prefix()} release-main --repo {shlex.quote(facts.repo_root)}"
    )
    return 0


def cmd_release_main(args: argparse.Namespace) -> int:
    """Release the main-target lock if held by this owner."""
    facts = main_facts(args.repo)
    if not facts.is_repo or facts.git_common_dir is None or facts.repo_root is None:
        return _fail_open("not inside a git repository")
    owner = _resolve_owner(args.owner)
    try:
        released = release(facts, owner) if owner else False
    except Exception as exc:
        return _fail_open(f"lock subsystem error ({exc!r})")
    if released:
        gate.log_event("lock-release", f"{facts.repo_root} by {owner}", None)
        print(f"🔓 worktree-warden lock released: {facts.repo_root}")
    else:
        print("worktree-warden lock: nothing to release for this session.")
    return 0


def cmd_refresh_main(args: argparse.Namespace) -> int:
    """Renew the holder's lease; always exits 0 (best-effort)."""
    facts = main_facts(args.repo)
    if not facts.is_repo or facts.git_common_dir is None or facts.repo_root is None:
        return 0
    owner = _resolve_owner(args.owner)
    if not owner:
        return 0
    try:
        decision = refresh(facts, owner, time.time())
    except Exception:
        return 0
    if decision.outcome == "refreshed":
        print(f"🔒 worktree-warden lease refreshed: {facts.repo_root}")
    else:
        print("worktree-warden lock: no live lease to refresh for this session.")
    return 0


def cmd_force_unlock(args: argparse.Namespace) -> int:
    """Remove the named worktree's lock (or all locks with --all), regardless of owner."""
    facts = _repo_facts(args.repo)
    if not facts.is_repo or facts.git_common_dir is None or facts.repo_root is None:
        return _fail_open("not inside a git repository")
    try:
        removed = force_unlock(facts, args.all)
    except Exception as exc:
        return _fail_open(f"lock subsystem error ({exc!r})")
    if removed:
        gate.log_event("force-unlock", ", ".join(removed), None)
        print("🔓 worktree-warden force-unlocked:\n  " + "\n  ".join(removed))
    else:
        print("worktree-warden lock: no locks to remove.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print every lock recorded for this repo and whether each is stale."""
    facts = main_facts(args.repo)
    if not facts.is_repo or facts.git_common_dir is None:
        print("worktree-warden lock: not inside a git repository.")
        return 0
    store = read_store(facts.git_common_dir)
    if not store:
        print("worktree-warden lock: no active locks in this repo.")
        return 0
    now = time.time()
    print(f"worktree-warden locks ({locks_path(facts.git_common_dir)}):")
    for key, record in sorted(store.items()):
        state = "STALE" if is_stale(record, now) else "active"
        age = _format_age(now - record.last_active)
        print(
            f"  [{state}] {key}\n"
            f"     owner {record.owner} ({record.kind}), active {age}; "
            f"reason: {record.reason}"
        )
    return 0


def _cli_prefix() -> str:
    """Return the runnable ``python3 /abs/worktree_lock.py`` command prefix."""
    return f"{sys.executable} {Path(__file__).resolve()}"


def build_parser() -> argparse.ArgumentParser:
    """Construct the worktree-lock argument parser.

    ``--repo``/``--owner`` are accepted in EITHER position -- before the
    subcommand (``--repo X acquire-main``, matching ``worktree_engine.py``'s
    convention) or after it (``acquire-main --repo X``). The top-level options
    carry the real defaults; the per-subcommand copies use ``SUPPRESS`` so that
    omitting them after the subcommand does not clobber a value given before it.
    """
    parser = argparse.ArgumentParser(prog="worktree-lock")
    parser.add_argument("--repo", default=None, help="repo/worktree path (default: cwd)")
    parser.add_argument(
        "--owner", default=None, help="session id (default: $CLAUDE_CODE_SESSION_ID)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=argparse.SUPPRESS, help="repo/worktree path")
        p.add_argument("--owner", default=argparse.SUPPRESS, help="session id")

    acquire_p = sub.add_parser("acquire-main", help="acquire the main-target lock")
    _common(acquire_p)
    # Only operation kinds may take the main-target lock from the CLI; ``occupancy``
    # is hook-internal and must never land on the primary key (it is prunable).
    acquire_p.add_argument("--kind", choices=("merge", "bumpall"), default="merge")
    acquire_p.add_argument("reason", nargs="*", help="why the lock is held")

    release_p = sub.add_parser("release-main", help="release the main-target lock")
    _common(release_p)

    refresh_p = sub.add_parser("refresh-main", help="renew the holder's lease")
    _common(refresh_p)

    force_p = sub.add_parser("force-unlock", help="remove a (possibly stale) lock")
    _common(force_p)
    force_p.add_argument("--all", action="store_true", help="remove every lock in the repo")

    status_p = sub.add_parser("status", help="show active locks for this repo")
    _common(status_p)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse arguments and dispatch to the subcommand."""
    args = build_parser().parse_args(argv)
    handlers = {
        "acquire-main": cmd_acquire_main,
        "release-main": cmd_release_main,
        "refresh-main": cmd_refresh_main,
        "force-unlock": cmd_force_unlock,
        "status": cmd_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
