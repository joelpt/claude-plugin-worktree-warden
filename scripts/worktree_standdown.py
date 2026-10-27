#!/usr/bin/env python3
"""Session-scoped, multi-owner stand-down registry shared by every warden hook.

Any plugin may ask warden to stand down for ONE Claude Code session by registering a
claim here; warden stays fully active for every other session. The registry lives in
the repo's shared git dir so the main checkout and all linked worktrees see one file:

    <git-common-dir>/worktree-warden-standdown.json   (data, replaced atomically)
    <git-common-dir>/worktree-warden-standdown.lock   (flock sidecar, never unlinked)

Schema v1::

    {"version": 1, "sessions": {"<session_id>": {"claims": {"<owner>": {
        "pid": 123, "pid_comm": "claude", "reason": "...", "since": "<iso8601 utc>"}}}}}

Claims are keyed by session AND owner so two plugins in one session never overwrite
each other; warden stands down while ANY valid claim exists for the session. A claim is
valid only while its pid is alive AND that pid's command name still matches the one
recorded (guards against pid reuse), so a crashed client cannot strand the registry.

Writers (``register``/``release``) take an exclusive flock on the sidecar and prune
every dead claim, never touching a live one. Readers (``is_standing_down``) never write
and never lock. Every read-side failure resolves to "not standing down": warden stays
ACTIVE, which is the safe direction.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REGISTRY_BASENAME = "worktree-warden-standdown.json"
LOCK_BASENAME = "worktree-warden-standdown.lock"
SCHEMA_VERSION = 1
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"

_PS_TIMEOUT_S = 5
_GIT_TIMEOUT_S = 10
_MAX_ANCESTOR_DEPTH = 64
_AUDIT_REASON_MAX = 200
_OWNER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def canonical_common_dir(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return the realpath of the git common dir for *cwd*.

    The main checkout and every linked worktree of one repo resolve to the same path.

    Args:
        cwd: Directory to resolve from; defaults to the process cwd.

    Returns:
        Absolute, symlink-resolved path of the shared git dir.

    Raises:
        RuntimeError: *cwd* is not inside a git repository.
    """
    base = os.fspath(cwd) if cwd is not None else os.getcwd()
    try:
        proc = subprocess.run(
            ["git", "-C", base, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git failed: {exc}") from exc
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"not a git repository: {base}")
    return os.path.realpath(out)


def _registry_path(common: str) -> Path:
    return Path(common) / REGISTRY_BASENAME


def _lock_path(common: str) -> Path:
    return Path(common) / LOCK_BASENAME


def _proc_comm(pid: int) -> str | None:
    """Return the basename of *pid*'s command name, or None when it cannot be read."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        return None
    return os.path.basename(out)


def _pid_alive(pid: object) -> bool:
    """Return True when *pid* is a live process (EPERM counts as alive)."""
    # bool is an int subclass; pid <= 0 would signal a whole process group.
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _claim_valid(claim: object) -> bool:
    """Return True when *claim* names a live process whose command still matches."""
    if not isinstance(claim, dict):
        return False
    pid = claim.get("pid")
    recorded = claim.get("pid_comm")
    if not _pid_alive(pid) or not isinstance(recorded, str) or not recorded:
        return False
    live = _proc_comm(pid)  # type: ignore[arg-type]
    return live is not None and live == os.path.basename(recorded)


def _parse_sessions(raw: str) -> dict[str, dict[str, object]] | None:
    """Parse registry text into its sessions map; None for malformed or unknown schema."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        return None
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return None
    return sessions


def _valid_claims(sessions: dict[str, dict[str, object]], session_id: str) -> dict[str, dict[str, object]]:
    """Return *session_id*'s claims that pass the liveness check."""
    entry = sessions.get(session_id)
    claims = entry.get("claims") if isinstance(entry, dict) else None
    if not isinstance(claims, dict):
        return {}
    return {
        owner: claim
        for owner, claim in claims.items()
        if isinstance(owner, str) and _OWNER_RE.fullmatch(owner) and _claim_valid(claim)
    }


def _prune(sessions: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    """Return *sessions* with every invalid claim and every claim-less session removed."""
    pruned: dict[str, dict[str, object]] = {}
    for session_id in sessions:
        claims = _valid_claims(sessions, session_id)
        if claims:
            pruned[session_id] = {"claims": claims}
    return pruned


def _publish(common: str, sessions: dict[str, dict[str, object]]) -> None:
    """Atomically write *sessions*, or delete only the data file when it is empty."""
    path = _registry_path(common)
    if not sessions:
        path.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=common, prefix=".worktree-warden-standdown.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"version": SCHEMA_VERSION, "sessions": sessions}, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _mutate(cwd: str | os.PathLike[str] | None, change: object) -> None:
    """Run ``change(sessions)`` on the pruned registry under the exclusive sidecar lock."""
    common = canonical_common_dir(cwd)
    lock_fd = os.open(_lock_path(common), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            raw = _registry_path(common).read_text()
        except FileNotFoundError:
            raw = ""
        # Malformed, empty or unknown-version content is overwritten, never fatal.
        sessions = _prune(_parse_sessions(raw) or {}) if raw.strip() else {}
        change(sessions)  # type: ignore[operator]
        _publish(common, _prune(sessions))
    finally:
        os.close(lock_fd)


def register(
    session_id: str,
    owner: str,
    reason: str,
    pid: int | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> None:
    """Record (or refresh) *owner*'s stand-down claim for *session_id*.

    Args:
        session_id: Claude Code session the claim applies to.
        owner: Registering plugin's name; one claim per (session, owner).
        reason: Human-readable explanation, surfaced in the audit log.
        pid: Process whose lifetime bounds the claim; defaults to this process.
        cwd: Directory inside the repo; defaults to the process cwd.

    Raises:
        RuntimeError: Empty identifiers, unreadable pid command name, or not a git repo.
    """
    if not session_id or not owner:
        raise RuntimeError("session id and owner are required")
    if not _OWNER_RE.fullmatch(owner):
        raise RuntimeError("owner must match [A-Za-z0-9._-]{1,64}")
    claim_pid = os.getpid() if pid is None else pid
    comm = _proc_comm(claim_pid)
    if comm is None:
        raise RuntimeError(f"cannot read command name of pid {claim_pid}")
    claim = {
        "pid": claim_pid,
        "pid_comm": comm,
        "reason": reason,
        "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    def _add(sessions: dict[str, dict[str, object]]) -> None:
        entry = sessions.setdefault(session_id, {"claims": {}})
        entry.setdefault("claims", {})[owner] = claim  # type: ignore[index]

    _mutate(cwd, _add)


def release(session_id: str, owner: str, cwd: str | os.PathLike[str] | None = None) -> None:
    """Remove *owner*'s claim for *session_id*; a no-op when none exists.

    Args:
        session_id: Session the claim was registered for.
        owner: The owner whose claim to remove; other owners are untouched.
        cwd: Directory inside the repo; defaults to the process cwd.

    Raises:
        RuntimeError: Not a git repository.
    """

    def _drop(sessions: dict[str, dict[str, object]]) -> None:
        entry = sessions.get(session_id)
        claims = entry.get("claims") if isinstance(entry, dict) else None
        if isinstance(claims, dict):
            claims.pop(owner, None)

    _mutate(cwd, _drop)


def active_claims(session_id: str | None, cwd: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Return ``{owner: reason}`` for *session_id*'s valid claims; empty on any failure.

    Never writes and never takes the lock.
    """
    try:
        if not session_id:
            return {}
        raw = _registry_path(canonical_common_dir(cwd)).read_text()
        sessions = _parse_sessions(raw)
        if sessions is None:
            return {}
        return {owner: str(claim.get("reason", "")) for owner, claim in _valid_claims(sessions, session_id).items()}
    except Exception:  # noqa: BLE001 - every failure must leave warden ACTIVE
        return {}


def is_standing_down(session_id: str | None, cwd: str | os.PathLike[str] | None = None) -> bool:
    """Return True iff *session_id* holds at least one valid claim.

    Args:
        session_id: The caller's own session id; no id means not standing down.
        cwd: Directory inside the repo; defaults to the process cwd.

    Returns:
        True only on positive evidence; False on any error.
    """
    return bool(active_claims(session_id, cwd))


def stand_down_for(hook: str, session_id: str | None, cwd: str | os.PathLike[str] | None = None) -> bool:
    """Return True when *hook* should stand down, auditing each claim that caused it.

    Args:
        hook: Name of the calling hook, recorded in the audit line.
        session_id: Session id from the hook payload (env fallback applied here).
        cwd: Directory inside the repo.

    Returns:
        True iff a valid registry claim exists for the session. Never raises.
    """
    try:
        claims = active_claims(session_id or os.environ.get(SESSION_ENV), cwd)
        if not claims:
            return False
        try:
            import worktree_gate  # noqa: PLC0415 - audit is best-effort

            for owner, reason in claims.items():
                text = " ".join(reason.split())[:_AUDIT_REASON_MAX]
                worktree_gate.log_event("standdown", f"{hook}: owner={owner} reason={text}", None)
        except Exception:  # noqa: BLE001 - auditing must not change the decision
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


def find_claude_ancestor() -> int | None:
    """Walk up from this process to the owning ``claude`` process; None if not found."""
    pid = os.getpid()
    for _ in range(_MAX_ANCESTOR_DEPTH):
        try:
            proc = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=_PS_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        fields = proc.stdout.strip().split(None, 1)
        if proc.returncode != 0 or len(fields) != 2 or not fields[0].isdigit():
            return None
        if pid != os.getpid() and "claude" in os.path.basename(fields[1]):
            return pid
        parent = int(fields[0])
        if parent <= 1 or parent == pid:
            return None
        pid = parent
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worktree_standdown", description="Warden stand-down registry.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "release", "active", "show"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--session-id", default=os.environ.get(SESSION_ENV), help=f"default: ${SESSION_ENV}")
        cmd.add_argument("--cwd", default=None, help="default: current directory")
        if name in ("register", "release"):
            cmd.add_argument("--owner", required=True)
        if name == "register":
            cmd.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    cwd = args.cwd or os.getcwd()
    try:
        if args.command == "active":
            return 0 if is_standing_down(args.session_id, cwd) else 1
        if args.command == "show":
            path = _registry_path(canonical_common_dir(cwd))
            print(path.read_text().strip() if path.exists() else "{}")
            return 0
        if not args.session_id:
            raise RuntimeError(f"no session id: pass --session-id or set {SESSION_ENV}")
        if args.command == "register":
            pid = find_claude_ancestor()
            if pid is None:
                raise RuntimeError("no claude ancestor process found; refusing to register")
            register(args.session_id, args.owner, args.reason, pid=pid, cwd=cwd)
        else:
            release(args.session_id, args.owner, cwd=cwd)
        return 0
    except (RuntimeError, OSError) as exc:
        sys.stderr.write(f"worktree_standdown: {args.command} failed: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
