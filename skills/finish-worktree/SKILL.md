---
name: finish-worktree
description: Land current worktree to main and tear it down
disable-model-invocation: true
argument-hint: "[target-branch]"
allowed-tools: Bash(git *) Bash(cd *) ExitWorktree EnterWorktree Skill(worktree-warden:merge-worktrees) Skill(worktree-warden:check-worktrees)
---

## Live context

- Worktree toplevel: !`git rev-parse --show-toplevel 2>/dev/null || echo "(not a git repo)"`
- git-dir (".git" ⟹ primary): !`git rev-parse --git-dir 2>/dev/null`
- Current branch: !`git rev-parse --abbrev-ref HEAD 2>/dev/null`
- Worktrees: !`git worktree list 2>/dev/null`

## What /finish-worktree does

Lands **this linked worktree** into `$ARGUMENTS` (default: the repo's default branch) and
tears it down — by delegating to the worktrees plugin's `/worktree-warden:merge-worktrees` engine
(rebase + ff-merge, post-land tests, exact-state rollback on failure). Handles the one thing
the engine can't: relocating the session out of the worktree before teardown.

Two session origins, one code path:

- **EnterWorktree session** — `ExitWorktree(action:"keep")` succeeds, session moves to the
  primary checkout before the merge, and re-enters the worktree on rollback.
- **Direct-start session** (background job, session started inside the worktree) —
  `ExitWorktree` is a no-op; fall back to `cd $PRIMARY` via Bash so the shell cwd moves
  to the primary checkout regardless. The merge and all engine calls use `--repo $PRIMARY`
  explicitly. The session UI may still display the old path, but shell operations run from
  `$PRIMARY`.

Does **not** commit first — `/merge-worktrees` commits dirty work via
`/commit-commands:commitall` as its first step, so uncommitted work is captured in the
snapshot before any rebase.

## Procedure

### 1. Sanity gate (from Live context)

- **Not a git repo** → stop and report.
- **`git-dir` is `.git`** (cwd is the primary checkout, not a linked worktree) → this is not
  what `/finish-worktree` lands. Punt to **`/worktree-warden:check-worktrees`** (it surfaces the
  repo's mergeable worktrees and offers to land any). Do not proceed to step 2.
- **Inside a linked worktree but already on the target branch** → stop (misconfiguration;
  nothing to land).
- **Otherwise** (inside a linked worktree on a non-target branch) → proceed.

### 2. Capture identity

Record:
- `WORKTREE_PATH` — absolute toplevel from Live context.
- `BRANCH` — current branch from Live context.
- `PRIMARY` — first path from `git worktree list` (the main checkout).
- `TARGET` — `$ARGUMENTS` if provided, else resolve from
  `git symbolic-ref --quiet refs/remotes/origin/HEAD` (leaf), else `main`.

### 3. Relocate if possible

Call **`ExitWorktree(action:"keep")`**:

- **Succeeds** → `RELOCATED=true`. Session cwd is now the primary checkout.
- **"No-op: there is no active EnterWorktree session"** → fall back to
  `Bash: cd $PRIMARY`. This moves the shell cwd to the primary checkout even without an
  active EnterWorktree session. Set `RELOCATED=true`. Note to the user that the session UI
  may still display the old worktree path, but all subsequent operations run from
  `$PRIMARY`.

### 4. Delegate the land

Invoke **`/worktree-warden:merge-worktrees`** passing:
- `--worktree $WORKTREE_PATH`
- `--branch $BRANCH`
- `--repo $PRIMARY` (explicit; merge-worktrees uses this for all engine calls and skips its
  own session-cwd primary check when this is provided)
- `--target $TARGET` if non-default

It runs the full flow: commit-if-dirty → snapshot → order → rebase + ff-merge → verify +
tests → teardown, with confidence-gated conflict/rollback handling.

### 5. Handle the result

- **Green** (worktree landed and pruned) → confirm worktree/branch are gone; report commits
  landed and test result.
- **Aborted / rolled back** → the engine's `undo` has restored the repo to exactly its
  pre-land state. Call `EnterWorktree(path:$WORKTREE_PATH)` to return the session to the
  intact worktree; report what happened + why, verbatim.

## Hard rules

- Never hand-roll merge or teardown — `/worktree-warden:merge-worktrees` owns all git mutation.
- Never `ExitWorktree action:"remove"` (refuses for session-created worktrees; no-op
  otherwise) — always use `"keep"` then delegate.
- Exit-12/13-style refusals from the engine are full stops — report, don't force.
