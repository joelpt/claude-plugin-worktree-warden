#!/usr/bin/env python3
"""Detect a repo's linked worktrees and which ones are mergeable.

Repo-scoped by construction: `git worktree list` only ever returns worktrees
of the repo that `--cwd` belongs to, so this never looks cross-repo. The
`claude agents --json` data is used ONLY to test whether a live session's cwd
falls inside one of THIS repo's worktrees.

Modes:
  (default)     pretty box-drawing table of mergeable worktrees (orphans:
                no live claude session), or nothing if there are none.
  --show-all    include every linked worktree (annotate session status),
                not just orphans.
  --json        emit a JSON array instead of the table (for the skill to
                drive selection/merge). Honors --show-all.

Exit code is always 0; absence of output means "nothing to surface".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from dataclasses import dataclass, field


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

    @property
    def has_session(self) -> bool:
        return bool(self.session_status)


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
    (_, status), (_, log), (_, last), (brc, behind) = await asyncio.gather(
        status_t, log_t, last_t, behind_t
    )
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


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(worktrees: list[Worktree]) -> str:
    """Render a box-drawing table + per-worktree distinct-commit lists."""
    headers = ["Worktree", "Branch", "State", "Commits", "Last modified", "Session"]
    caps = [22, 24, 5, 7, 16, 14]
    rows: list[list[str]] = []
    for wt in worktrees:
        sess = f"{wt.session_status}" if wt.session_status else "—"
        if wt.session_kind:
            sess = f"{wt.session_status}/{wt.session_kind}"
        # For dirty worktrees show file_mtime_rel, for clean show last_rel
        display_time = wt.file_mtime_rel if wt.dirty else wt.last_rel
        rows.append(
            [
                _truncate(os.path.basename(wt.path.rstrip("/")), caps[0]),
                _truncate(wt.branch, caps[1]),
                "dirty" if wt.dirty else "clean",
                str(wt.commit_count),
                _truncate(display_time, caps[4]),
                _truncate(sess, caps[5]),
            ]
        )
    widths = [
        min(caps[i], max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i]))
        for i in range(len(headers))
    ]
    right = {3}  # right-align the Commits column

    def fmt_row(cells: list[str]) -> str:
        out = []
        for i, c in enumerate(cells):
            out.append(c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
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
        }
        for wt in worktrees
    ]
    return json.dumps(payload, indent=2)


async def gather_worktrees(cwd: str, show_all: bool) -> list[Worktree]:
    """Resolve, populate, session-match, and filter the repo's worktrees."""
    if not await is_git_repo(cwd):
        return []
    linked = (await list_worktrees(cwd))[1]
    if not linked:
        return []
    base_branch = await default_branch(cwd)
    await asyncio.gather(*(fill_state(wt, base_branch, cwd) for wt in linked))
    sessions = await load_sessions()
    match_sessions(linked, sessions)
    linked.sort(key=lambda w: w.mtime)
    if show_all:
        return linked
    return [wt for wt in linked if not wt.has_session]


def main() -> int:
    parser = argparse.ArgumentParser(prog="check_worktrees")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--show-all", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args()

    worktrees = asyncio.run(gather_worktrees(args.cwd, args.show_all))
    if not worktrees:
        if args.as_json:
            print("[]")
        return 0
    print(to_json(worktrees) if args.as_json else render_table(worktrees))
    return 0


if __name__ == "__main__":
    sys.exit(main())
