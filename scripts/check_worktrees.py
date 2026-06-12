#!/usr/bin/env python3
"""Detect a repo's linked worktrees and classify each one's merge readiness.

Repo-scoped by construction: `git worktree list` only ever returns worktrees
of the repo that `--cwd` belongs to, so this never looks cross-repo. The
`claude agents --json` data is used ONLY to test whether a live session's cwd
falls inside one of THIS repo's worktrees.

Every linked worktree is always listed (no orphan filtering); each is tagged
with a `Readiness` bucket — ready to merge, mergeable after a commit, empty or
already-merged (prunable), recently active (held back as a safety harness), or
blocked by a live session.

Modes:
  (default)     pretty box-drawing table of every linked worktree, or nothing
                if there are none.
  --json        emit a JSON array instead of the table (for the skill to drive
                selection / merge / prune).

Exit code is always 0; absence of output means "nothing to surface".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import unicodedata

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


RECENT_WINDOW_SECONDS = 15 * 60


class Readiness(str, Enum):
    """How a worktree relates to being landed into the default branch."""

    READY = "ready"  # clean, commits ahead of base — land as-is
    NEEDS_COMMIT = "needs_commit"  # uncommitted work — commit, then land
    MERGED = "merged"  # clean, HEAD already in base's chain but behind it — prune
    PRUNE = "prune"  # clean, HEAD == base tip — nothing here, just prune
    COOLDOWN = "cooldown"  # active within the recent window — hold off (overridable)
    BLOCKED = "blocked"  # a live claude session sits inside it
    UNKNOWN = "unknown"  # git state could not be read — never auto-prune


_EMOJI: dict[Readiness, str] = {
    Readiness.READY: "✅",
    Readiness.NEEDS_COMMIT: "✅",
    Readiness.MERGED: "🧹",
    Readiness.PRUNE: "🧹",
    Readiness.COOLDOWN: "⏳",
    Readiness.BLOCKED: "❌",
    Readiness.UNKNOWN: "❓",
}

_NOTE: dict[Readiness, str] = {
    Readiness.READY: "ready to merge",
    Readiness.NEEDS_COMMIT: "can merge after commit",
    Readiness.MERGED: "merged, can be pruned",
    Readiness.PRUNE: "empty, can be pruned",
    Readiness.COOLDOWN: "active <15m ago",
    Readiness.BLOCKED: "live session",
    Readiness.UNKNOWN: "state unreadable",
}


@dataclass
class Worktree:
    """One linked worktree of the current repo plus its computed state."""

    path: str
    branch: str
    head: str
    dirty: bool = False
    commit_count: int = 0
    commits: list[str] = field(default_factory=list)
    last_rel: str = ""
    last_iso: str = ""
    mtime: float = 0.0
    file_mtime: float = 0.0  # most recent file modification time
    file_mtime_rel: str = ""  # relative time string for file_mtime
    behind: int = 0
    session_status: str = ""
    session_kind: str = ""
    session_name: str = ""
    recently_active: bool = False  # edited or had transcript activity within the window
    unreadable: bool = False  # a git state query failed — do not trust dirty/commit_count

    @property
    def has_session(self) -> bool:
        """True iff a live claude session sits inside this worktree."""
        return bool(self.session_status)

    @property
    def readiness(self) -> Readiness:
        """Classify the worktree. A live session blocks regardless of content.

        Recent activity (an edit or transcript write within the window) holds
        the worktree on COOLDOWN below a live session but above its git state —
        a safety harness against landing half-baked work, overridable on
        explicit request. A clean worktree with no commits ahead of base has its
        HEAD already in base's chain; `behind > 0` means real history that base
        has moved past (merged), while `behind == 0` means HEAD sits exactly on
        base (empty).

        When a git state query failed (`unreadable`), the dirty/commit_count
        fields are not trustworthy — a failed `git status` defaults `dirty` to
        False, which would otherwise misclassify a worktree with real work as
        PRUNE under exactly the system-stress conditions that make git flaky.
        Such a worktree is reported UNKNOWN and never offered for auto-merge or
        pruning; a live session still takes precedence (BLOCKED is the safe call
        regardless of unreadable git state).
        """
        if self.has_session:
            return Readiness.BLOCKED
        if self.unreadable:
            return Readiness.UNKNOWN
        if self.recently_active:
            return Readiness.COOLDOWN
        if self.dirty:
            return Readiness.NEEDS_COMMIT
        if self.commit_count > 0:
            return Readiness.READY
        if self.behind > 0:
            return Readiness.MERGED
        return Readiness.PRUNE

    @property
    def ready_emoji(self) -> str:
        """The Ready? column glyph for this worktree's bucket."""
        return _EMOJI[self.readiness]

    @property
    def ready_note(self) -> str:
        """A concise, deterministic reason for this worktree's bucket."""
        return _NOTE[self.readiness]

    @property
    def is_mergeable(self) -> bool:
        """True iff this worktree is *offerable* for auto-merge.

        That means not blocked by a live session, not on the recent-activity
        cooldown, and not in an unreadable state. Git state may still permit an
        explicit merge of a cooldown worktree — this gate only governs what is
        offered/counted automatically, never the engine, so an explicit user
        request can still land one.
        """
        return self.readiness not in (
            Readiness.BLOCKED,
            Readiness.COOLDOWN,
            Readiness.UNKNOWN,
        )


async def run_git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run `git <args>` in cwd, returning (returncode, stripped stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def is_git_repo(cwd: str) -> bool:
    rc, out = await run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0 and out == "true"


async def default_branch(cwd: str) -> str:
    """Resolve a base ref that is GUARANTEED to resolve for log comparisons.

    Prefers a local branch (origin HEAD's leaf, then main/master); falls back to
    the remote-tracking ref `origin/<name>` when only that exists (shallow / CI /
    --no-track checkouts), so `<base>..HEAD` never silently fails and makes an
    unmerged worktree look already-merged.
    """
    rc, out = await run_git(
        ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd
    )
    if rc == 0 and out:
        name = out.rsplit("/", 1)[-1]
        rc2, _ = await run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd
        )
        if rc2 == 0:
            return name
        rc3, _ = await run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"], cwd
        )
        if rc3 == 0:
            return f"origin/{name}"
    for candidate in ("main", "master"):
        rc, _ = await run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"], cwd
        )
        if rc == 0:
            return candidate
    return "main"


async def list_worktrees(cwd: str) -> tuple[str, list[Worktree]]:
    """Return (main_worktree_path, [linked worktrees]) via porcelain output."""
    rc, out = await run_git(["worktree", "list", "--porcelain"], cwd)
    if rc != 0:
        return "", []
    blocks = out.split("\n\n")
    main_path = ""
    linked: list[Worktree] = []
    for i, block in enumerate(blocks):
        path = branch = head = ""
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line.startswith("branch "):
                branch = line[len("branch ") :].removeprefix("refs/heads/")
            elif line.startswith("HEAD "):
                head = line[len("HEAD ") :]
            elif line.strip() == "detached":
                branch = "(detached)"
        if not path:
            continue
        if i == 0:
            main_path = path
            continue
        linked.append(Worktree(path=path, branch=branch, head=head))
    return main_path, linked


def _mtime_to_relative(mtime: float) -> str:
    """Convert a file modification timestamp to a relative time string like '2 hours ago'."""
    if mtime <= 0:
        return ""
    now = time.time()
    delta = int(now - mtime)
    if delta < 0:
        return "in the future"
    if delta < 60:
        return "just now" if delta < 10 else f"{delta}s ago"
    if delta < 3600:
        minutes = delta // 60
        return f"{minutes}m ago"
    if delta < 86400:
        hours = delta // 3600
        return f"{hours}h ago"
    if delta < 604800:
        days = delta // 86400
        return f"{days}d ago"
    weeks = delta // 604800
    return f"{weeks}w ago"


def _get_most_recent_file_mtime(path: str) -> float:
    """Recursively find the most recent file modification time in a directory tree.
    
    Walks the entire directory tree (except .git) and returns the highest mtime found.
    Returns 0.0 if no files found or on error.
    """
    max_mtime = 0.0
    try:
        for root, dirs, files in os.walk(path):
            # Skip .git directory
            dirs[:] = [d for d in dirs if d != ".git"]
            for fname in files:
                try:
                    fpath = os.path.join(root, fname)
                    mtime = os.stat(fpath).st_mtime
                    max_mtime = max(max_mtime, mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return max_mtime


def _recent(mtime: float, now: float, window: int = RECENT_WINDOW_SECONDS) -> bool:
    """True iff `mtime` is a real timestamp within `window` seconds of `now`."""
    return mtime > 0 and (now - mtime) <= window


def _last_edit_mtime(wt: Worktree) -> float:
    """Epoch of the worktree's last edit, mirroring the table's `Last edit`.

    For dirty worktrees that is the newest working-tree file mtime. For clean
    worktrees it is the last commit time, but ONLY when the worktree has its own
    commits ahead of base — a 0-ahead worktree's `last_iso` is the base tip it
    inherited, not activity in this worktree, so using it would falsely flag the
    worktree as recently active whenever base was just committed. Returns 0.0
    when unknown, inherited, or unparseable (the transcript signal still stands).
    """
    if wt.dirty:
        return wt.file_mtime
    if wt.commit_count > 0 and wt.last_iso:
        try:
            return datetime.fromisoformat(wt.last_iso).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _projects_dir() -> Path:
    """Resolve the Claude config `projects/` dir holding session transcripts."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".claude"
    return root / "projects"


def _encode_project_dir(path: str) -> str:
    """Encode an absolute cwd into its `~/.claude/projects/` folder name.

    Claude maps a cwd to a transcript dir by replacing `/` and `.` with `-`;
    the encoding is lossy (not reversible) but stable forward, which is all we
    need to look up a known worktree path's transcripts.
    """
    return os.path.realpath(path).replace("/", "-").replace(".", "-")


def _latest_transcript_mtime(path: str, projects_dir: Path) -> float:
    """Newest transcript `.jsonl` mtime for sessions whose cwd is `path`.

    Keys on the worktree's exact path (top-level `*.jsonl` only), so a session
    rooted in a *subdir* of the worktree, or a subagent transcript nested under
    a `subagents/` dir, won't contribute — the last-edit signal usually covers
    those. Fail-silent: a missing dir, odd encoding, or permission error yields
    0.0 so the recency check degrades to the last-edit signal rather than
    erroring.
    """
    try:
        d = projects_dir / _encode_project_dir(path)
        return max((f.stat().st_mtime for f in d.glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


async def fill_state(wt: Worktree, base: str, cwd: str) -> None:
    """Populate a worktree's dirty/commit/last-commit/mtime/behind fields."""
    status_t = run_git(["-C", wt.path, "status", "--porcelain"], cwd)
    log_t = run_git(
        ["-C", wt.path, "log", "--oneline", "--no-decorate", f"{base}..HEAD"], cwd
    )
    last_t = run_git(["-C", wt.path, "log", "-1", "--format=%cr%x1f%cI"], cwd)
    behind_t = run_git(
        ["-C", wt.path, "rev-list", "--count", f"HEAD..{base}"], cwd
    )
    (src, status), (lrc, log), (_, last), (brc, behind) = await asyncio.gather(
        status_t, log_t, last_t, behind_t
    )
    # A failed status/log query makes dirty/commit_count untrustworthy: an empty
    # `status` from a *failure* is indistinguishable from a genuinely clean tree,
    # and defaulting to "clean + 0 commits" would misclassify a worktree holding
    # real work as prunable — precisely under the system stress that makes git
    # flaky. Mark it unreadable so readiness reports UNKNOWN, never PRUNE.
    wt.unreadable = src != 0 or lrc != 0
    wt.dirty = bool(status.strip())
    wt.commits = [ln for ln in log.splitlines() if ln.strip()]
    wt.commit_count = len(wt.commits)
    if last and "\x1f" in last:
        wt.last_rel, wt.last_iso = last.split("\x1f", 1)
    wt.behind = int(behind) if brc == 0 and behind.isdigit() else 0
    try:
        wt.mtime = os.stat(wt.path).st_mtime
    except OSError:
        wt.mtime = 0.0
    # For dirty worktrees, compute the most recent file modification time
    if wt.dirty:
        wt.file_mtime = _get_most_recent_file_mtime(wt.path)
        wt.file_mtime_rel = _mtime_to_relative(wt.file_mtime)


async def load_sessions() -> list[dict]:
    """Return parsed `claude agents --json`, or [] on any failure.

    On timeout, kills the child so a hung `claude` binary can't leave a zombie
    behind on every SessionStart.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "agents",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return []
    try:
        data = json.loads(out.decode("utf-8", "replace") or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def match_sessions(worktrees: list[Worktree], sessions: list[dict]) -> None:
    """Attach a session's status/kind/name to any worktree it sits inside.

    Match is path-prefix WITH a trailing separator (or exact equality) so a
    session in `…/foo/sub` maps to worktree `…/foo` while `…/foo-bar` does not.
    """
    for wt in worktrees:
        wt_norm = os.path.realpath(wt.path)
        for sess in sessions:
            scwd = sess.get("cwd")
            if not scwd:
                continue
            s_norm = os.path.realpath(scwd)
            if s_norm == wt_norm or s_norm.startswith(wt_norm + os.sep):
                wt.session_status = str(sess.get("status", "") or "")
                wt.session_kind = str(sess.get("kind", "") or "")
                wt.session_name = str(sess.get("name", "") or "")
                break


def _display_width(text: str) -> int:
    """Terminal column count, counting East-Asian-wide glyphs (emoji) as 2.

    The status emoji (✅/❌/🧹) carry East Asian width 'W', so plain ``len`` would
    under-count them by one and skew the table's right border.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _truncate(text: str, width: int) -> str:
    """Trim text to a display width, appending '…' when it overflows.

    Display-width aware to match the renderer's padding/width logic, so a wide
    glyph in a capped cell can't push the cell past its column.
    """
    if _display_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + ch_w > width - 1:  # reserve one column for the ellipsis
            break
        out += ch
        used += ch_w
    return out + "…"


def _pad(text: str, width: int, *, right: bool = False) -> str:
    """Pad text to a display width, accounting for wide glyphs."""
    gap = width - _display_width(text)
    if gap <= 0:
        return text
    return (" " * gap + text) if right else (text + " " * gap)


def render_table(worktrees: list[Worktree]) -> str:
    """Render a box-drawing table + per-worktree distinct-commit lists."""
    headers = ["Ready?", "Worktree", "State", "Commits", "Last edit", "Note"]
    caps = [6, 22, 5, 7, 16, 22]
    rows: list[list[str]] = []
    for wt in worktrees:
        # For dirty worktrees show file_mtime_rel, for clean show last_rel
        display_time = wt.file_mtime_rel if wt.dirty else wt.last_rel
        rows.append(
            [
                wt.ready_emoji,
                _truncate(os.path.basename(wt.path.rstrip("/")), caps[1]),
                "dirty" if wt.dirty else "clean",
                str(wt.commit_count),
                _truncate(display_time, caps[4]),
                _truncate(wt.ready_note, caps[5]),
            ]
        )
    widths = [
        min(
            caps[i],
            max(len(headers[i]), *(_display_width(r[i]) for r in rows))
            if rows
            else len(headers[i]),
        )
        for i in range(len(headers))
    ]
    right = {3}  # right-align the Commits column

    def fmt_row(cells: list[str]) -> str:
        out = [_pad(c, widths[i], right=i in right) for i, c in enumerate(cells)]
        return "│ " + " │ ".join(out) + " │"

    def rule(left: str, mid: str, rightc: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + rightc

    lines = [
        rule("┌", "┬", "┐"),
        fmt_row(headers),
        rule("├", "┼", "┤"),
    ]
    lines += [fmt_row(r) for r in rows]
    lines.append(rule("└", "┴", "┘"))

    detail: list[str] = []
    for wt in worktrees:
        if wt.commits:
            detail.append(f"\n{wt.branch} — {wt.commit_count} commit(s) ahead of base:")
            detail += [f"  {c}" for c in wt.commits[:10]]
            if wt.commit_count > 10:
                detail.append(f"  … and {wt.commit_count - 10} more")
    return "\n".join(lines + detail)


def to_json(worktrees: list[Worktree]) -> str:
    payload = [
        {
            "path": wt.path,
            "branch": wt.branch,
            "dirty": wt.dirty,
            "commit_count": wt.commit_count,
            "behind_base": wt.behind,
            "last_rel": wt.last_rel,
            "last_iso": wt.last_iso,
            "mtime": wt.mtime,
            "file_mtime": wt.file_mtime,
            "file_mtime_rel": wt.file_mtime_rel,
            "session_status": wt.session_status,
            "session_kind": wt.session_kind,
            "session_name": wt.session_name,
            "recently_active": wt.recently_active,
            "unreadable": wt.unreadable,
            "category": wt.readiness.value,
            "note": wt.ready_note,
            "ready": wt.is_mergeable,
        }
        for wt in worktrees
    ]
    return json.dumps(payload, indent=2)


async def gather_worktrees(cwd: str) -> list[Worktree]:
    """Resolve, populate, and session-match every linked worktree of the repo.

    Returns all linked worktrees (oldest first); readiness classification is a
    per-worktree property, so callers filter on `is_mergeable`/`readiness`
    rather than this function pre-filtering the list.
    """
    if not await is_git_repo(cwd):
        return []
    linked = (await list_worktrees(cwd))[1]
    if not linked:
        return []
    base_branch = await default_branch(cwd)
    await asyncio.gather(*(fill_state(wt, base_branch, cwd) for wt in linked))
    sessions = await load_sessions()
    match_sessions(linked, sessions)
    now = time.time()
    projects_dir = _projects_dir()
    for wt in linked:
        wt.recently_active = _recent(_last_edit_mtime(wt), now) or _recent(
            _latest_transcript_mtime(wt.path, projects_dir), now
        )
    linked.sort(key=lambda w: w.mtime)
    return linked


def main() -> int:
    parser = argparse.ArgumentParser(prog="check_worktrees")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args()

    try:
        worktrees = asyncio.run(gather_worktrees(args.cwd))
    except Exception:  # noqa: BLE001 — honor the "exit 0, no output" contract
        if args.as_json:
            print("[]")
        return 0
    if not worktrees:
        if args.as_json:
            print("[]")
        return 0
    print(to_json(worktrees) if args.as_json else render_table(worktrees))
    return 0


if __name__ == "__main__":
    sys.exit(main())
