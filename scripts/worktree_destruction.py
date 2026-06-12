#!/usr/bin/env python3
"""Deterministic gate over worktree-destroying shell commands.

The merge/teardown engine is already guarded, but it only governs work that goes
*through* the engine. A worktree (and its uncommitted or un-landed content) can
still be thrown away by a raw shell command — ``git worktree remove --force``,
``rm -rf <worktree>``, ``git branch -D <branch>`` — issued by Claude, a sub-agent
orchestrator, or any tool that shells out. Nothing in the plugin saw those, so a
worktree could disappear with content that was never committed *or* never landed
in the default branch.

This module is the pure, testable core of a ``PreToolUse(Bash)`` gate that closes
that hole. It parses a (possibly compound) shell command for destructive intent,
resolves the targeted worktree/branch, and rules **block** unless the target is
provably (a) clean — no dirty/untracked non-noise files — *and* (b) landed —
every commit already an ancestor of the default branch. Clean+landed worktrees
hold nothing worth keeping, so their removal is allowed; everything else is
refused with the reason and the command to land first.

The engine's own ``subprocess`` git calls do NOT pass through the Bash tool, so
this gate never intercepts the engine — only operator/agent-issued shell
commands. It fails OPEN: any parse/IO surprise allows the command (a buggy gate
must never brick the shell), mirroring ``enforce_worktree_hook``.

Known coverage limits (deliberate — they fail open, never closed): destruction
reached through indirection the static parser cannot follow — ``xargs git
worktree remove``, ``bash -c '…'``, command substitution, or a script the Bash
call merely invokes — is not seen here. The Stop-hook WIP capture and the
``recover`` audit are the backstops for those windows.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Literal

import worktree_gate as gate

NOISE_PATH = ".claude/settings.local.json"

IntentKind = Literal["worktree_remove", "rm_path", "branch_delete"]

_SEGMENT_SEPARATORS = ("&&", "||", "|", ";", "\n", "&")

_REDIRECTION = {">", ">>", "<", "2>", "2>>", "&>", "1>", "<<", "<<<"}

# Leading tokens that wrap a real command without changing which command runs.
_WRAPPERS = {
    "sudo", "doas", "env", "command", "builtin", "exec",
    "time", "nice", "nohup", "stdbuf", "ionice",
}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class DestructiveIntent:
    """One destructive operation parsed out of a shell command.

    Attributes:
        kind: Which destructive shape this is.
        target: The path (for ``worktree_remove``/``rm_path``) or branch name
            (for ``branch_delete``) the command would destroy.
        cwd: Directory the command runs in, after honoring any ``git -C <dir>``.
        forced: Whether a force flag (``--force``/``-f``/``-D``) was present.
    """

    kind: IntentKind
    target: str
    cwd: str
    forced: bool


@dataclass(frozen=True)
class Verdict:
    """The gate's ruling on a single command.

    Attributes:
        allow: Whether the command may proceed.
        reason: Short machine-ish explanation of the ruling.
        recovery: A user-facing hint (land/commit first) when blocking, else "".
    """

    allow: bool
    reason: str
    recovery: str = ""


def _split_segments(command: str) -> list[str]:
    """Split a compound command on shell operators into runnable segments."""
    segments = [command]
    for sep in _SEGMENT_SEPARATORS:
        nxt: list[str] = []
        for seg in segments:
            nxt.extend(seg.split(sep))
        segments = nxt
    return [s.strip() for s in segments if s.strip()]


def _safe_split(segment: str) -> list[str]:
    """``shlex.split`` a segment, tolerating unbalanced quotes (posix=False)."""
    try:
        return shlex.split(segment)
    except ValueError:
        try:
            return shlex.split(segment, posix=False)
        except ValueError:
            return segment.split()


def _strip_git_global_opts(tokens: list[str], cwd: str) -> tuple[list[str], str]:
    """Drop leading ``git`` global options, returning (rest, effective_cwd).

    Honors ``-C <dir>`` so the command's real working directory is used for the
    git lookups; other global options that take a value are skipped so the
    subcommand is found. Unknown value-less flags are skipped individually.
    """
    i = 1  # tokens[0] == "git"
    eff_cwd = cwd
    takes_value = {
        "-C", "--git-dir", "--work-tree", "--namespace", "-c",
        "--exec-path", "--super-prefix",
    }
    while i < len(tokens) and tokens[i].startswith("-"):
        flag = tokens[i]
        if flag == "-C" and i + 1 < len(tokens):
            cand = tokens[i + 1]
            eff_cwd = cand if os.path.isabs(cand) else os.path.join(cwd, cand)
            i += 2
            continue
        if "=" in flag:  # e.g. --git-dir=foo
            i += 1
            continue
        if flag in takes_value and i + 1 < len(tokens):
            i += 2
            continue
        i += 1
    return tokens[i:], eff_cwd


def _operands(args: list[str]) -> list[str]:
    """Non-flag, non-redirection operands from a token list.

    Drops flags, shell redirection operators (``>``, ``2>``, …) and the filename
    that immediately follows a redirection, so ``... <wt> > /dev/null`` does not
    surface ``/dev/null`` as an operand.
    """
    out: list[str] = []
    skip_next = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if tok in _REDIRECTION:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        out.append(tok)
    return out


def _unwrap_prefix(tokens: list[str]) -> list[str]:
    """Drop leading env-assignments and command wrappers (sudo/env/time/…).

    So ``FOO=bar git worktree remove …`` and ``sudo rm -rf …`` are still seen as
    the git/rm command they ultimately run, rather than slipping past the gate.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ENV_ASSIGN.match(tok):
            i += 1
            continue
        if os.path.basename(tok.lstrip("\\")) in _WRAPPERS:
            i += 1
            continue
        break
    return tokens[i:]


def _parse_segment(segment: str, cwd: str) -> list[DestructiveIntent]:
    """Parse one command segment into zero or more DestructiveIntents."""
    tokens = _unwrap_prefix(_safe_split(segment))
    if not tokens:
        return []
    cmd = os.path.basename(tokens[0].lstrip("\\"))

    if cmd == "git":
        rest, eff_cwd = _strip_git_global_opts(tokens, cwd)
        if not rest:
            return []
        sub = rest[0]
        if sub == "worktree" and len(rest) >= 2 and rest[1] == "remove":
            args = rest[2:]
            forced = any(a in ("--force", "-f") for a in args)
            paths = _operands(args)
            # `git worktree remove` takes exactly one path operand.
            if paths:
                return [DestructiveIntent("worktree_remove", paths[0], eff_cwd, forced)]
            return []
        if sub == "branch":
            args = rest[1:]
            is_delete = any(a in ("-d", "-D", "--delete") for a in args)
            if not is_delete:
                return []
            forced = "-D" in args or (
                "--delete" in args and "--force" in args
            )
            # `git branch -D a b c` deletes every named branch; gate each.
            return [
                DestructiveIntent("branch_delete", name, eff_cwd, forced)
                for name in _operands(args)
            ]
        return []

    if cmd == "rm":
        args = tokens[1:]
        short = [f[1:].lower() for f in args if f.startswith("-") and not f.startswith("--")]
        long = [f for f in args if f.startswith("--")]
        recursive = any("r" in s for s in short) or "--recursive" in long
        forced = any("f" in s for s in short) or "--force" in long
        if not recursive:
            return []
        return [
            DestructiveIntent("rm_path", path, cwd, forced)
            for path in _operands(args)
        ]
    return []


def parse_destructive_command(command: str, cwd: str) -> list[DestructiveIntent]:
    """Extract every destructive intent from a (possibly compound) command.

    Args:
        command: The raw shell command string from the Bash tool call.
        cwd: The session working directory the command runs in.

    Returns:
        A list of DestructiveIntent (empty when the command destroys nothing
        this gate cares about).
    """
    intents: list[DestructiveIntent] = []
    for segment in _split_segments(command):
        intents.extend(_parse_segment(segment, cwd))
    return intents


def _default_branch(repo: str) -> str:
    """Resolve the repo's default branch (origin/HEAD leaf, else main/master)."""
    rc, out = gate.run_git(
        ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo
    )
    if rc == 0 and out.startswith("refs/remotes/origin/"):
        return out.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc2, _ = gate.run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{cand}"], repo
        )
        if rc2 == 0:
            return cand
    return "main"


def _worktree_branch(repo: str, wt_path: str) -> str | None:
    """Branch checked out at ``wt_path``, or None if it is not a worktree."""
    target = os.path.realpath(wt_path)
    rc, out = gate.run_git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return None
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = os.path.realpath(line[len("worktree ") :])
        elif line.startswith("branch ") and current == target:
            ref = line[len("branch ") :]
            return ref.removeprefix("refs/heads/")
    return None


def _porcelain_z(cwd: str) -> str | None:
    """Raw, UNSTRIPPED ``git status --porcelain -z`` output for ``cwd``.

    The plugin's shared ``run_git`` strips stdout, which would eat the leading
    space of the FIRST porcelain record (e.g. ``" M path"`` → ``"M path"``) and
    desync the fixed-offset parse. Status parsing must see bytes verbatim, so
    this reads them directly. Returns None on any failure (treated as clean).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _non_noise_dirty(repo: str, branch: str) -> list[str]:
    """Non-noise dirty paths in the worktree checked out at ``branch``.

    Reads status from the branch's worktree directory; returns [] when the
    worktree directory is missing (already gone — nothing dirty to protect).
    """
    wt = _branch_worktree_path(repo, branch)
    if wt is None or not os.path.isdir(wt):
        return []
    out = _porcelain_z(wt)
    if out is None:
        return []
    tokens = [t for t in out.split("\0") if t]
    dirty: list[str] = []
    i = 0
    while i < len(tokens):
        status, path = tokens[i][:2], tokens[i][3:]
        if path != NOISE_PATH:
            dirty.append(path)
        i += 2 if ("R" in status or "C" in status) else 1
    return dirty


def _branch_worktree_path(repo: str, branch: str) -> str | None:
    """Path of the worktree checked out at ``branch``, or None."""
    ref = f"refs/heads/{branch}"
    rc, out = gate.run_git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return None
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = os.path.realpath(line[len("worktree ") :])
        elif line.startswith("branch ") and line[len("branch ") :] == ref:
            return current
    return None


def _branch_exists(repo: str, branch: str) -> bool:
    rc, _ = gate.run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo
    )
    return rc == 0


def _is_landed(repo: str, branch: str, target: str) -> bool:
    """True iff every commit on ``branch`` is already present in ``target``.

    Uses ``git cherry``, which compares by *patch content*, not SHA ancestry, so
    a branch whose commits were **rebased** onto ``target`` and fast-forwarded
    (exactly what the engine does) is correctly seen as landed even though its
    old tip is no longer an ancestor. ``git cherry`` prints ``+ <sha>`` for a
    commit not yet in ``target`` and ``- <sha>`` for one already there; landed
    means no ``+`` lines.

    Fails OPEN: if ``git cherry`` errors (e.g. ``target`` does not resolve), the
    gate cannot prove content is at risk, so this returns True (allow). The
    Stop-hook WIP capture is the backstop for those windows.
    """
    rc, out = gate.run_git(["cherry", target, branch], repo)
    if rc != 0:
        return True
    return not any(line.startswith("+") for line in out.splitlines())


def _evaluate_branch(repo: str, branch: str) -> Verdict:
    """Rule on destroying ``branch`` (delete or remove of its worktree)."""
    if not _branch_exists(repo, branch):
        return Verdict(True, f"branch '{branch}' does not exist")
    target = _default_branch(repo)
    if branch == target:
        return Verdict(True, f"'{branch}' is the default branch")
    dirty = _non_noise_dirty(repo, branch)
    if dirty:
        sample = ", ".join(dirty[:5]) + ("…" if len(dirty) > 5 else "")
        return Verdict(
            False,
            f"worktree for '{branch}' has {len(dirty)} uncommitted change(s)",
            recovery=(
                f"Commit first (uncommitted: {sample}). From the worktree: "
                f"`/commit-commands:commitall`, then `/worktree-warden:finish-worktree`."
            ),
        )
    if not _is_landed(repo, branch, target):
        rc, ahead = gate.run_git(
            ["rev-list", "--count", f"{target}..{branch}"], repo
        )
        n = ahead if rc == 0 and ahead.isdigit() else "some"
        return Verdict(
            False,
            f"'{branch}' has {n} commit(s) not landed in '{target}'",
            recovery=(
                f"Land them first: `/worktree-warden:finish-worktree` (or "
                f"`/worktree-warden:merge-worktrees`). worktree-warden will rebase "
                f"'{branch}' onto '{target}' and tear it down safely."
            ),
        )
    return Verdict(True, f"'{branch}' is clean and landed in '{target}'")


def evaluate_command(command: str, cwd: str) -> Verdict:
    """Rule on a Bash command. Allow unless it destroys un-landed/dirty content.

    Args:
        command: The raw shell command from the Bash tool call.
        cwd: The session working directory.

    Returns:
        A Verdict; ``allow=True`` for any command that destroys nothing this gate
        protects (the overwhelmingly common case).
    """
    intents = parse_destructive_command(command, cwd)
    if not intents:
        return Verdict(True, "no destructive worktree/branch operation")

    for intent in intents:
        rc, top = gate.run_git(["rev-parse", "--show-toplevel"], intent.cwd)
        repo = top if rc == 0 else intent.cwd

        if intent.kind == "branch_delete":
            verdict = _evaluate_branch(repo, intent.target)
        else:
            wt = intent.target if os.path.isabs(intent.target) else os.path.join(
                intent.cwd, intent.target
            )
            branch = _worktree_branch(repo, wt)
            if branch is None:
                # Not a registered worktree of this repo — out of scope, allow.
                verdict = Verdict(True, "target is not a linked worktree")
            else:
                verdict = _evaluate_branch(repo, branch)

        if not verdict.allow:
            return verdict
    return Verdict(True, "all destructive targets are clean and landed")


def evaluate_exit_worktree(action: str, cwd: str) -> Verdict:
    """Rule on an ``ExitWorktree`` call (the harness's own removal path).

    Only ``action == "remove"`` from inside a linked worktree is gated; a keep
    leaves the directory on disk and is always allowed.

    Args:
        action: The ExitWorktree ``action`` argument ("keep" or "remove").
        cwd: The session working directory (expected inside the worktree).

    Returns:
        A Verdict ruling on the removal.
    """
    if action != "remove":
        return Verdict(True, "ExitWorktree keep leaves the worktree on disk")
    rc, top = gate.run_git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        return Verdict(True, "not inside a git repository")
    rc2, common = gate.run_git(["rev-parse", "--git-dir", "--git-common-dir"], cwd)
    lines = common.splitlines()
    if rc2 != 0 or len(lines) != 2:
        return Verdict(True, "could not resolve worktree context")
    git_dir = os.path.realpath(os.path.join(cwd, lines[0]))
    common_dir = os.path.realpath(os.path.join(cwd, lines[1]))
    if git_dir == common_dir:
        return Verdict(True, "not a linked worktree")
    branch = _worktree_branch(top, top)
    if branch is None:
        return Verdict(True, "detached or unresolved worktree")
    return _evaluate_branch(top, branch)
