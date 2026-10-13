#!/usr/bin/env python3
"""Deterministic gate over Bash commands that destroy un-landed worktree state.

Three families are gated, all judged from the command's *shape* so the
verdict never depends on the live lock state (the PreToolUse hook runs before
the command, so in ``unlock X && remove --force X`` the lock still reads as
held when the gate looks):

* `git branch -d/-D <branch>` -- blocked when the branch's worktree is dirty or
  the branch holds commits not landed in the default branch.
* `git worktree unlock` and `rm` of anything under `.git/worktrees/` (the admin
  record or its ``locked`` file) -- blocked outright. The engine's own
  teardown unlocks from a Python subprocess that never passes through this
  gate, so no sanctioned flow needs these from Bash.
* `git worktree remove` with two or more force flags (``-f -f``, ``-ff``,
  ``--force --force``, any ``--f…`` abbreviation) -- blocked outright, because
  git's own error for a locked worktree advertises ``remove -f -f`` as the
  override. A single force is judged by the target worktree's *path* (dirty
  files, then un-landed commits on its HEAD, which also covers a detached
  HEAD); a target that does not resolve to a registered worktree is refused.

Every linked worktree is also locked at creation
(`hooks/lock_worktree_hook.py`), which makes git refuse a single-force remove
of a locked worktree, but that lock is a second layer: the hook fails open, and
the shapes above are what an agent reaches for once git refuses.
`evaluate_exit_worktree` (the separate, non-parser `ExitWorktree`-tool gate)
re-verifies the worktree's *current* lock state and falls back to the
clean+landed check below whenever it finds the worktree unlocked.

A destructive command whose repository or working directory the gate cannot
determine statically -- ``GIT_DIR``/``--git-dir``/``--work-tree`` overrides,
``cd $VAR``, ``cd -``, ``popd`` or a subshell ``cd`` followed by a gated
command -- is blocked outright (``unresolved_context``) rather than judged
against the wrong repo.

Known, PERMANENT, backstopped gap (deliberate -- see README's "PreToolUse
destruction gate" section): `rm -rf <worktree>` on the worktree directory
itself and any Bash-hidden or indirect destruction (`xargs`, `sh -c '...'`,
`eval`, command substitution, `git -c alias.x=...`) are not intercepted by this
module or by git's lock -- a locked worktree's files can still be deleted
directly on disk, it is only the *admin record* that git refuses to drop. The
Stop-hook WIP capture and the `recover`/`reconcile` audit are the sole
backstops for that window; they make the loss detectable and recoverable, not
prevented.

Fails OPEN on parse/IO surprises in the *parser* (a buggy gate must never brick
the shell), mirroring `enforce_worktree_hook`; the worktree-remove verdicts
fail CLOSED when the target cannot be resolved or its status cannot be read.
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

IntentKind = Literal[
    "branch_delete",
    "worktree_remove",
    "worktree_unlock",
    "worktree_admin_rm",
    "unresolved_context",
]

# Environment variables that redirect which repository git operates on.
_GIT_REDIRECT_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}
_GIT_REDIRECT_OPTS = ("--git-dir", "--work-tree")
_DYNAMIC_PATH_CHARS = ("$", "`", "*", "?", "[")

# Matches a path that points into a repo's linked-worktree admin area
# (`<common-dir>/worktrees/<name>[/locked]`); deleting it drops git's lock.
_ADMIN_AREA = re.compile(r"(^|/)\.git/worktrees(/|$)|(^|/)worktrees/[^/]+/locked$")

_SEGMENT_SEPARATORS = ("&&", "||", "|", ";", "\n", "&")

# Leading tokens that wrap a real command without changing which command runs.
_WRAPPERS = {
    "sudo", "doas", "env", "command", "builtin", "exec",
    "time", "nice", "nohup", "stdbuf", "ionice",
}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_REDIRECTION = {">", ">>", "<", "2>", "2>>", "&>", "1>", "<<", "<<<"}


@dataclass(frozen=True)
class DestructiveIntent:
    """One destructive operation parsed out of a shell command.

    Attributes:
        kind: Which gated shape this is (branch delete, worktree remove or
            unlock, or `rm` aimed at the worktree admin area).
        target: The branch name, worktree path/name, or `rm` operand affected.
        cwd: Directory the command runs in, after honoring any ``git -C <dir>``
            and any earlier ``cd`` in the same compound command.
        forced: Whether ``-D``/``--delete --force`` (branch) or any force flag
            (worktree remove) was used.
        force_count: Number of force flags on a `worktree remove` (``-ff`` is 2).

    """

    kind: IntentKind
    target: str
    cwd: str
    forced: bool
    force_count: int = 0


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
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote[-1]:
                quote = ""
            elif ch == "\\" and quote in ('"', "$'") and i + 1 < len(command):
                i += 1
                buf.append(command[i])
        elif ch == "\\" and i + 1 < len(command):
            buf.append(ch + command[i + 1])
            i += 1
        elif ch in "'\"":
            # bash ANSI-C quoting ($'...') honors backslash escapes, unlike '...'.
            quote = "$'" if ch == "'" and command[i - 1 : i] == "$" else ch
            buf.append(ch)
        else:
            sep = next((s for s in _SEGMENT_SEPARATORS if command.startswith(s, i)), "")
            if sep:
                segments.append("".join(buf))
                buf = []
                i += len(sep) - 1
            else:
                buf.append(ch)
        i += 1
    segments.append("".join(buf))
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


def _strip_git_global_opts(
    tokens: list[str], cwd: str
) -> tuple[list[str], str, list[str]]:
    """Drop leading ``git`` global options, returning (rest, effective_cwd, opts).

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
    return tokens[i:], eff_cwd, tokens[1:i]


def _operands(args: list[str]) -> list[str]:
    """Non-flag, non-redirection operands from a token list.

    Drops flags, shell redirection operators (``>``, ``2>``, …) and the filename
    that immediately follows a redirection, so ``... feat > /dev/null`` does not
    surface ``/dev/null`` as an operand.
    """
    out: list[str] = []
    skip_next = False
    for i, tok in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            out.extend(args[i + 1 :])
            break
        if tok in _REDIRECTION:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        out.append(tok)
    return out


def _branch_delete_mode(args: list[str]) -> tuple[bool, bool]:
    """Return (is_delete, forced) for ``git branch`` arguments.

    Understands short-flag clusters (``-fD``, ``-rd``) and unambiguous long
    option abbreviations (``--del``, ``--forc``), as git itself does. ``-d``
    combined with a force flag is equivalent to ``-D``. Scanning stops at a
    bare ``--``.
    """
    delete = force = False
    for tok in args:
        if tok == "--":
            break
        if tok.startswith("--"):
            if len(tok) >= 5 and "--delete".startswith(tok):
                delete = True
            elif len(tok) >= 6 and "--force".startswith(tok):
                force = True
        elif tok.startswith("-") and tok[1:].isalpha():
            letters = tok[1:]
            if "D" in letters:
                delete = force = True
            if "d" in letters:
                delete = True
            if "f" in letters:
                force = True
    return delete, delete and force


def _unwrap_prefix(tokens: list[str]) -> list[str]:
    """Drop leading env-assignments and command wrappers (sudo/env/time/…).

    So ``FOO=bar git branch -D feat`` and ``sudo git branch -D feat`` are still
    seen as the git command they ultimately run, rather than slipping past the
    gate.
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


def _force_count(args: list[str]) -> int:
    """Count force flags among ``git worktree remove`` arguments.

    Counts every ``f`` in a short-flag cluster (``-f``, ``-ff``) and every long
    option that is an abbreviation of ``--force`` git would accept (``--f``,
    ``--forc``, ``--force``). Scanning stops at a bare ``--``.
    """
    count = 0
    for tok in args:
        if tok == "--":
            break
        if tok.startswith("--"):
            if len(tok) >= 3 and "--force".startswith(tok):
                count += 1
        elif tok.startswith("-") and tok[1:].isalpha():
            count += tok[1:].count("f")
    return count


def _cwd_change(segment: str, cwd: str) -> tuple[bool, str | None]:
    """Classify a segment's effect on the working directory.

    Returns:
        ``(False, None)`` when the segment does not change directory;
        ``(True, new_cwd)`` for a resolvable ``cd``/``pushd``; and
        ``(True, None)`` when the new directory cannot be known statically
        (``cd -``, ``popd``, ``$VAR``/backtick/glob targets, or a ``cd`` inside
        a subshell whose scope ends before the next segment).
    """
    scoped = segment.lstrip().startswith(("(", "{"))
    tokens = _safe_split(segment.lstrip("({ \t"))
    if not tokens or tokens[0] not in ("cd", "pushd", "popd"):
        return False, None
    if scoped or tokens[0] == "popd":
        return True, None
    dirs = [t for t in tokens[1:] if not t.startswith("-") or t == "-"]
    target = dirs[0] if dirs else "~"
    if target == "-" or any(c in target for c in _DYNAMIC_PATH_CHARS):
        return True, None
    target = os.path.expanduser(target)
    return True, target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd, target))


def _sets_git_redirect(segment: str) -> bool:
    """Return whether a segment persistently sets a git-redirecting env var."""
    tokens = _safe_split(segment)
    if tokens and tokens[0] in ("export", "declare", "typeset"):
        tokens = tokens[1:]
    elif not tokens or not all(_ENV_ASSIGN.match(t) for t in tokens):
        return False
    return any(t.split("=", 1)[0] in _GIT_REDIRECT_ENV for t in tokens)


def _redirects_git(raw_tokens: list[str], git_global_opts: list[str]) -> bool:
    """Return whether a git command's env prefix or global options redirect its repo."""
    for tok in raw_tokens:
        if not _ENV_ASSIGN.match(tok) and os.path.basename(tok) not in _WRAPPERS:
            break
        if tok.split("=", 1)[0] in _GIT_REDIRECT_ENV:
            return True
    return any(
        o == opt or o.startswith(opt + "=")
        for o in git_global_opts
        for opt in _GIT_REDIRECT_OPTS
    )


def _parse_git_worktree(rest: list[str], cwd: str) -> list[DestructiveIntent]:
    """Parse the ``worktree`` subcommand: unlock always, remove only when forced."""
    if len(rest) < 2 or rest[1] not in ("remove", "unlock"):
        return []
    args = rest[2:]
    operands = _operands(args)
    target = operands[0] if operands else ""
    if rest[1] == "unlock":
        return [DestructiveIntent("worktree_unlock", target, cwd, False)]
    forces = _force_count(args)
    if forces == 0:
        return []
    return [DestructiveIntent("worktree_remove", target, cwd, True, forces)]


def _parse_segment(segment: str, cwd: str) -> list[DestructiveIntent]:
    """Parse one command segment into zero or more destructive intents."""
    raw = _safe_split(segment)
    tokens = _unwrap_prefix(raw)
    if not tokens:
        return []
    cmd = os.path.basename(tokens[0].lstrip("\\"))

    if cmd == "rm":
        for operand in _operands(tokens[1:]):
            if _ADMIN_AREA.search(os.path.normpath(operand)):
                return [DestructiveIntent("worktree_admin_rm", operand, cwd, False)]
        return []
    if cmd != "git":
        return []

    rest, eff_cwd, global_opts = _strip_git_global_opts(tokens, cwd)
    if rest and rest[0] == "worktree":
        intents = _parse_git_worktree(rest, eff_cwd)
    elif rest and rest[0] == "branch":
        args = rest[1:]
        is_delete, forced = _branch_delete_mode(args)
        # `git branch -D a b c` deletes every named branch; gate each.
        intents = (
            [
                DestructiveIntent("branch_delete", name, eff_cwd, forced)
                for name in _operands(args)
            ]
            if is_delete
            else []
        )
    else:
        return []
    if intents and _redirects_git(raw, global_opts):
        return [_unresolved(intents[0].target, eff_cwd)]
    return intents


def _unresolved(target: str, cwd: str) -> DestructiveIntent:
    """Build the intent for a destructive command whose repo/cwd is unknowable."""
    return DestructiveIntent("unresolved_context", target, cwd, False)


def parse_destructive_command(command: str, cwd: str) -> list[DestructiveIntent]:
    """Extract every gated destructive intent from a (possibly compound) command.

    A ``cd <dir>`` segment moves the working directory used to resolve the
    segments after it, so ``cd wt && git worktree remove -ff .`` is judged
    against ``wt``.

    Args:
        command: The raw shell command string from the Bash tool call.
        cwd: The session working directory the command runs in.

    Returns:
        A list of DestructiveIntent (empty when the command does nothing this
        gate cares about; see the module docstring for the gated shapes and
        the permanent `rm -rf <worktree>` gap).

    """
    intents: list[DestructiveIntent] = []
    unresolved = False
    for segment in _split_segments(command):
        is_cd, moved = _cwd_change(segment, cwd)
        if is_cd:
            if moved is None:
                unresolved = True
            else:
                cwd = moved
            continue
        if _sets_git_redirect(segment):
            unresolved = True
            continue
        parsed = _parse_segment(segment, cwd)
        if unresolved and parsed:
            parsed = [_unresolved(p.target, p.cwd) for p in parsed]
        intents.extend(parsed)
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


def _porcelain_z(cwd: str) -> str | None:
    """Raw, UNSTRIPPED ``git status --porcelain -z`` output for ``cwd``.

    The plugin's shared ``run_git`` strips stdout, which would eat the leading
    space of the FIRST porcelain record (e.g. ``" M path"`` → ``"M path"``) and
    desync the fixed-offset parse. Status parsing must see bytes verbatim, so
    this reads them directly. Returns None on any failure; callers fail closed.
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


def _non_noise_dirty(repo: str, branch: str) -> list[str] | None:
    """Non-noise dirty paths in the worktree checked out at ``branch``.

    Reads status from the branch's worktree directory; returns [] when the
    worktree directory is missing (already gone — nothing dirty to protect)
    and None when the status exists but cannot be read.
    """
    wt = _branch_worktree_path(repo, branch)
    if wt is None or not os.path.isdir(wt):
        return []
    return _dirty_paths(wt)


def _dirty_paths(wt: str) -> list[str] | None:
    """Non-noise dirty paths at worktree directory ``wt``; None if status fails."""
    out = _porcelain_z(wt)
    if out is None:
        return None
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
    """Return whether ``branch`` resolves as a local ref in ``repo``."""
    rc, _ = gate.run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo
    )
    return rc == 0


def _is_landed(repo: str, branch: str, target: str) -> bool:
    """Return True iff every commit on ``branch`` is already present in ``target``.

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
    """Rule on deleting ``branch`` via `git branch -d/-D`."""
    if not _branch_exists(repo, branch):
        return Verdict(True, f"branch '{branch}' does not exist")
    target = _default_branch(repo)
    if branch == target:
        return Verdict(True, f"'{branch}' is the default branch")
    dirty = _non_noise_dirty(repo, branch)
    if dirty is None:
        return Verdict(
            False,
            f"cannot read the status of the worktree for '{branch}'",
            recovery=_ENGINE_RECOVERY,
        )
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


_ENGINE_RECOVERY = (
    "Use `/worktree-warden:finish-worktree` (or `/worktree-warden:merge-worktrees`): "
    "the engine unlocks and removes worktrees itself after landing them. If a "
    "stale worktree really must go, ask the user to run the command themselves "
    "with the `!` prefix."
)


def _resolve_worktree(repo: str, target: str, cwd: str) -> str | None:
    """Resolve a `worktree remove` operand to a registered worktree path.

    Matches the operand (absolute, or relative to ``cwd``) against
    ``git worktree list --porcelain`` by real path, then by unique folder name
    (git accepts the bare name). Returns None when nothing, or more than one
    worktree, matches.
    """
    if not target:
        return None
    rc, out = gate.run_git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return None
    paths = [
        os.path.realpath(line[len("worktree ") :])
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]
    wanted = os.path.realpath(target if os.path.isabs(target) else os.path.join(cwd, target))
    if wanted in paths:
        return wanted
    named = [p for p in paths if os.path.basename(p) == target]
    return named[0] if len(named) == 1 else None


def _evaluate_worktree_path(repo: str, path: str) -> Verdict:
    """Rule on force-removing the worktree at ``path``, judged by path not branch.

    Dirty files are checked first (an unreadable status fails CLOSED), then
    commits on the worktree's HEAD that are not in the default branch -- which
    also protects a detached HEAD that no branch name would reveal.
    """
    dirty = _dirty_paths(path)
    if dirty is None:
        return Verdict(
            False,
            f"cannot read the status of worktree '{path}'",
            recovery=_ENGINE_RECOVERY,
        )
    if dirty:
        sample = ", ".join(dirty[:5]) + ("…" if len(dirty) > 5 else "")
        return Verdict(
            False,
            f"worktree '{path}' has {len(dirty)} uncommitted change(s)",
            recovery=f"Commit first (uncommitted: {sample}). {_ENGINE_RECOVERY}",
        )
    target = _default_branch(repo)
    rc, out = gate.run_git(["cherry", target, "HEAD"], path)
    if rc == 0 and any(line.startswith("+") for line in out.splitlines()):
        n = sum(1 for line in out.splitlines() if line.startswith("+"))
        return Verdict(
            False,
            f"worktree '{path}' has {n} commit(s) not landed in '{target}'",
            recovery=f"Land them first. {_ENGINE_RECOVERY}",
        )
    return Verdict(True, f"worktree '{path}' is clean and landed in '{target}'")


def _evaluate_intent(intent: DestructiveIntent) -> Verdict:
    """Rule on one parsed destructive intent."""
    rc, top = gate.run_git(["rev-parse", "--show-toplevel"], intent.cwd)
    repo = top if rc == 0 else intent.cwd
    if intent.kind == "branch_delete":
        return _evaluate_branch(repo, intent.target)
    if intent.kind == "unresolved_context":
        return Verdict(
            False,
            "destructive git command runs in a directory or repository the gate "
            "cannot determine (git-dir override, `cd $VAR`, `cd -`, `popd` or a subshell `cd`)",
            recovery=(
                "Re-run it with a literal path (`git -C <dir> …`) and no "
                "GIT_DIR/--git-dir override, or use `/worktree-warden:finish-worktree`."
            ),
        )
    if intent.kind == "worktree_unlock":
        return Verdict(
            False,
            "`git worktree unlock` from Bash removes the lock that protects worktrees",
            recovery=_ENGINE_RECOVERY,
        )
    if intent.kind == "worktree_admin_rm":
        return Verdict(
            False,
            f"`rm {intent.target}` deletes a worktree's git admin record or lock file",
            recovery=_ENGINE_RECOVERY,
        )
    if intent.force_count >= 2:
        return Verdict(
            False,
            "`git worktree remove` with two force flags overrides the worktree lock",
            recovery=_ENGINE_RECOVERY,
        )
    path = _resolve_worktree(repo, intent.target, intent.cwd)
    if path is None:
        return Verdict(
            False,
            f"cannot resolve '{intent.target}' to a registered worktree",
            recovery=_ENGINE_RECOVERY,
        )
    return _evaluate_worktree_path(repo, path)


def evaluate_command(command: str, cwd: str) -> Verdict:
    """Rule on a Bash command. Allow unless it destroys un-landed worktree state.

    Args:
        command: The raw shell command from the Bash tool call.
        cwd: The session working directory.

    Returns:
        A Verdict; ``allow=True`` for any command that does none of the gated
        operations (the overwhelmingly common case). See the module docstring
        for the gated shapes and the permanent `rm -rf <worktree>` gap.

    """
    intents = parse_destructive_command(command, cwd)
    if not intents:
        return Verdict(True, "no destructive worktree or branch operation")

    for intent in intents:
        verdict = _evaluate_intent(intent)
        if not verdict.allow:
            return verdict
    return Verdict(True, "all destructive targets are clean and landed")


def _is_worktree_locked(repo: str, path: str) -> bool:
    """Return whether the worktree at ``path`` currently carries a `git worktree lock`.

    Reads the shared `git worktree list --porcelain` view (works from any
    worktree of the repo, not just ``path`` itself) rather than trusting that
    locking succeeded earlier -- see ``evaluate_exit_worktree``.
    """
    rc, out = gate.run_git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return False
    target = os.path.realpath(path)
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = os.path.realpath(line[len("worktree ") :])
        elif line == "locked" or line.startswith("locked "):
            if current == target:
                return True
    return False


def evaluate_exit_worktree(action: str, cwd: str) -> Verdict:
    """Re-verify the worktree is locked before trusting git's own removal refusal.

    Every linked worktree is locked at creation (`lock_worktree_hook.py`,
    REQ-LOCKING-CREATE) and only unlocked immediately before the engine's own
    teardown (REQ-LOCKING-TEARDOWN), so `ExitWorktree`'s own `git worktree
    remove [--force]` call is normally refused by git itself for a still-locked
    worktree. But that protection is now an IMPLICIT dependency on the earlier
    lock hook having actually succeeded -- this module used to independently
    re-verify clean+landed state regardless of lock status, and that
    defense-in-depth was dropped when the parser collapsed to branch-delete
    only. This re-adds it, scoped narrowly: query the worktree's *current*
    lock state directly rather than assuming it. When it IS locked, defer to
    git's own refusal (unchanged, cheap). When it is NOT locked -- lock never
    applied, the lock hook silently failed, or it was unlocked ahead of a
    sanctioned teardown -- fall back to the same clean+landed check this
    module always used for `git branch -d/-D`, so an accidental/`ExitWorktree`
    removal of un-landed work is still caught rather than silently trusted to
    an external precondition that may not hold.

    Args:
        action: The ExitWorktree ``action`` argument ("keep" or "remove").
        cwd: The session working directory (the worktree being exited).

    Returns:
        An allow Verdict when locked (or nothing at risk to verify), else the
        clean+landed verdict for the worktree's current branch.

    """
    if action != "remove":
        return Verdict(True, "keep does not remove anything")

    rc, top = gate.run_git(["rev-parse", "--show-toplevel"], cwd)
    repo = top if rc == 0 else cwd

    if _is_worktree_locked(repo, cwd):
        return Verdict(True, "git's own worktree lock gates ExitWorktree removal")

    rc2, branch = gate.run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if rc2 != 0 or not branch or branch == "HEAD":
        return Verdict(True, "cannot resolve current branch; nothing to verify")
    return _evaluate_branch(repo, branch)


if __name__ == "__main__":
    pass
