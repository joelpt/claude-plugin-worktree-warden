---
name: finish-worktree
description: Land the current linked worktree into its default branch and tear it down. Use when wrapping up work from inside a worktree — the user signals done/finished, or the session is winding down. For landing worktrees from the primary checkout, use check-worktrees instead.
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
- `COMMIT_COUNT` — run `git rev-list --count $TARGET..HEAD` (while still in the worktree).
  This count is needed for the recap in step 6; capture it now because the branch ref is
  deleted after teardown.

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

- **Green** (worktree landed and pruned) → confirm worktree/branch are gone; proceed to
  step 6.
- **Aborted / rolled back** → the engine's `undo` has restored the repo to exactly its
  pre-land state. Call `EnterWorktree(path:$WORKTREE_PATH)` to return the session to the
  intact worktree; report what happened + why, verbatim. Do not produce a recap.

### 6. Extended recap (green path only)

The user was not watching the work that happened in this worktree. Write a prose narrative
— **target 500–1200 words** — that brings them fully up to speed, as if handing off from
one engineer to another.

**Gather the raw data first:**

```bash
# All commits that landed, newest-first, with full stats
git -C $PRIMARY log --stat \
  --format="%ncommit %h  (%ai)%n%s%n%b" \
  -$COMMIT_COUNT

# One-line summary of overall file changes across all landed commits
git -C $PRIMARY diff HEAD~$COMMIT_COUNT HEAD --stat
```

If `COMMIT_COUNT` is 0 for some reason (engine reported already-merged), describe the
branch as having had no new commits to land and skip the detail sections below.

**Narrative structure** — write flowing prose in each section, not bullet lists:

**What this branch was about** (1–2 paragraphs): Synthesize the branch name and commit
messages into a plain-English description of the goal. What problem was being solved, what
feature was being added, or what was being cleaned up? Someone who knew nothing about the
task should come away with a clear mental model of the work's purpose.

**What changed** (2–4 paragraphs): Walk through the commits chronologically, grouped by
logical theme when there are multiple. For each meaningful commit (or group), explain what
was actually changed and why — not just which files, but what the change *does*. Reference
specific files and directories when it helps orient the reader (e.g. "the gate logic in
`hooks/worktree_gate.py`"), but don't enumerate every file changed in each commit; that's
what `git log --stat` is for. Focus on the intent behind the changes.

**Anything notable** (0–2 paragraphs, omit if nothing notable): Any conflicts resolved,
tricky design decisions visible from commit messages, tests added or fixed, refactors
performed, or edge cases handled. If the commit history is clean and unremarkable, skip
this section rather than padding it.

**How it landed** (1 short paragraph): State that the branch has been rebased and merged
into `$TARGET`, the worktree torn down, and tests passed. Mention how many commits landed.
This is the one factual "outcome" paragraph — keep it brief.

**Tone:** Informative and conversational, like a knowledgeable colleague writing a Slack
summary. No bullet lists in the main narrative. Prose only. Don't start with "Here is a
recap" or any meta-announcement — open directly with the substance.

## Hard rules

- Never hand-roll merge or teardown — `/worktree-warden:merge-worktrees` owns all git mutation.
- Never `ExitWorktree action:"remove"` (refuses for session-created worktrees; no-op
  otherwise) — always use `"keep"` then delegate.
- Exit-12/13-style refusals from the engine are full stops — report, don't force.
