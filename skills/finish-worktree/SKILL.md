---
name: finish-worktree
description: Land the current linked worktree into its default branch and tear it down. Use when wrapping up work from inside a worktree — the user signals done/finished, or the session is winding down. For landing worktrees from the primary checkout, use check-worktrees instead.
argument-hint: "[target-branch]"
allowed-tools: Bash(python3 *) Bash(git *) Bash(cd *) ExitWorktree EnterWorktree Skill(worktree-warden:merge-worktrees) Skill(worktree-warden:check-worktrees) Skill(commit-commands:commitall)
---

## Live context

- Worktree toplevel: !`git rev-parse --show-toplevel 2>/dev/null || echo "(not a git repo)"`
- git-dir (".git" ⟹ primary): !`git rev-parse --git-dir 2>/dev/null`
- Current branch: !`git rev-parse --abbrev-ref HEAD 2>/dev/null`
- Worktrees: !`git worktree list 2>/dev/null`

## Engine

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`

## What /finish-worktree does

Lands **this linked worktree** into `$ARGUMENTS` (default: the repo's default branch) and
tears it down. The happy path is a **single** `worktree_engine.py finish` call (lock →
snapshot → rebase + ff-merge → test gate → teardown → release); non-trivial cases (conflict,
test failure, dirty, multiple worktrees) fall back to `/worktree-warden:merge-worktrees` for
its confidence-gated judgment. This skill handles the one thing `finish` can't: relocating
the session out of the worktree first (a subprocess can't `cd` its parent).

Two session origins, one code path:

- **EnterWorktree session** — `ExitWorktree(action:"keep")` succeeds, session moves to the
  primary checkout before the land.
- **Direct-start session** (background job, session started inside the worktree) —
  `ExitWorktree` is a no-op; fall back to `cd $PRIMARY` via Bash. All engine calls take
  `--repo $PRIMARY` explicitly. The session UI may still display the old path, but shell
  operations run from `$PRIMARY`.

A **dirty** worktree is not auto-committed: `finish` refuses it (exit 11) and step 5 commits
via `/commit-commands:commitall` before retrying.

## Procedure

### 1. Sanity gate (from Live context)

- **Not a git repo** → stop and report.
- **`git-dir` is `.git`** (cwd is the primary checkout, not a linked worktree) → this is not
  what `/finish-worktree` lands. Punt to **`/worktree-warden:check-worktrees`** (it surfaces the
  repo's mergeable worktrees and offers to land any). Do not proceed to step 2.
- **Current branch is `HEAD`** (detached HEAD state) → stop and report; there is no
  branch to land.
- **Inside a linked worktree but already on the target branch** → stop (misconfiguration;
  nothing to land).
- **Otherwise** (inside a linked worktree on a non-target branch) → proceed.

### 2. Capture identity

`WORKTREE_PATH` and `BRANCH` are available from Live context above.
Capture `PRIMARY`, `TARGET`, and `COMMIT_COUNT` with one engine call:

```bash
python3 $ENGINE finish-preflight --worktree $WORKTREE_PATH [--target $ARGUMENTS]
```

Pass `--target $ARGUMENTS` only when `$ARGUMENTS` was provided — this ensures
`commit_count` is computed against the same target used for the recap.
From `details`: `primary` → `PRIMARY`, `target` → `TARGET`,
`commit_count` → `COMMIT_COUNT`. `BRANCH` is confirmed by `details.branch`.

`COMMIT_COUNT` is needed for the recap in step 6; capture it now because the branch
ref is deleted after teardown.

### 3. Relocate if possible

Call **`ExitWorktree(action:"keep")`**:

- **Succeeds** → `RELOCATED=true`. Session cwd is now the primary checkout.
- **"No-op: there is no active EnterWorktree session"** → fall back to
  `Bash: cd $PRIMARY`. This moves the shell cwd to the primary checkout even without an
  active EnterWorktree session. Set `RELOCATED=true`. Note to the user that the session UI
  may still display the old worktree path, but all subsequent operations run from
  `$PRIMARY`.

### 4. Land via the one-shot `finish` command

The straightforward case (clean worktree, no conflicts, tests pass) is a **single command**:
`finish` acquires the main-target lock, snapshots, rebases + ff-merges, runs the test gate,
tears down, and releases the lock — all internally. No more hand-chaining lock + snapshot +
land + teardown.

First resolve the project's test command (first that applies): Justfile `test` → `just test`;
else `npm test` (if `package.json`); else `pytest` (if Python); else `cargo test` (if Cargo);
else none.

```bash
python3 $ENGINE --repo $PRIMARY finish --worktree $WORKTREE_PATH --branch $BRANCH \
  --target $TARGET --test-cmd "<resolved test command>"
```

Use `--skip-tests` in place of `--test-cmd` only when the repo has no test setup (or a
docs/config-only landing the user is fine to land ungated). The lock owner is your session id
automatically. `finish` is a subprocess and **cannot** relocate you out of the worktree —
that is why step 3 already moved you to `$PRIMARY`.

### 5. Handle the result (by exit code)

- **0 `finished`** → landed + tested + torn down. Confirm worktree/branch are gone; go to step 6.
- **10** (already merged) → `finish` tore it down anyway; go to step 6 (no new commits to recap).
- **13 `rebase_conflict`** → the rebase is LEFT IN PROGRESS and the **lock is still held by you**.
  Hand off to **`/worktree-warden:merge-worktrees`** (`--worktree $WORKTREE_PATH --branch $BRANCH
  --repo $PRIMARY --target $TARGET`) for its confidence-gated conflict ladder; it re-acquires your
  same-session lock re-entrantly and releases at the end.
- **18 `tests_failed`** → the branch IS landed but tests failed; state is **PRESERVED** (not
  rolled back) and the **lock is still held by you**. `details.snapshot_file` is the undo anchor.
  Hand off to **`/worktree-warden:merge-worktrees`**'s step-6 rollback ladder (fix-forward / `undo`
  / abandon / ask). Do NOT auto-`undo`.
- **11 `dirty_worktree`** → commit first: `EnterWorktree(path:$WORKTREE_PATH)` →
  `/commit-commands:commitall` → `ExitWorktree(action:"keep")` (or `cd $PRIMARY`), then re-run step 4.
- **16 `lock_blocked`** → another session is merging to `$TARGET`; pause and report the holder
  (from the message). Retry when it frees, or force-unlock if you are certain it is dead.
- **12 / 14 / 15 / 17** → preflight/safety refusals; report `message` verbatim and stop.

Multiple worktrees or deliberate land ordering still go through **`/worktree-warden:merge-worktrees`**
directly — `finish` is the single-worktree fast path.

### 6. Extended recap (green path only)

The user was not watching the work that happened in this worktree. Write a recap —
**target 100–1000 words** — that brings them fully up to speed. Prose and bullet points
are both welcome; use whichever fits the content (bullets for lists of changes, prose for
context and explanation).

**Gather the raw data first:**

```bash
# All commits that landed, with full stats
git -C $PRIMARY log --stat \
  --format="%ncommit %h  (%ai)%n%s%n%b" \
  -$COMMIT_COUNT

# Overall file-change summary across all landed commits
git -C $PRIMARY diff HEAD~$COMMIT_COUNT HEAD --stat
```

If `COMMIT_COUNT` is 0 (engine reported already-merged), note that the branch had no new
commits to land and skip the detail sections.

**Visual vocabulary** — apply these consistently so the user can scan at a glance:

| Signal | Meaning |
|--------|---------|
| 🎯 | Branch goal / what this was for |
| ✨ | New capability or feature added |
| 🔧 | Bug fix or correction |
| ♻️ | Refactor or cleanup (no behavior change) |
| 🧪 | Test changes |
| 📚 | Docs or config-only changes |
| ⚠️ | Conflict resolved, edge case, or notable risk |
| 💡 | Non-obvious design decision |
| 🚀 | Landing outcome line |
| `` `code span` `` | File paths, function names, config keys, branch names |
| **bold** | Commit subjects and key terms |
| _italic_ | Supporting context, rationale, caveats |

**Structure** (omit any section that would be empty):

**🎯 Goal** — One or two sentences: what problem was being solved or what was being
added. Written for someone with zero context on the task.

**What changed** — Walk the commits chronologically. Lead each change with the
appropriate emoji (✨ / 🔧 / ♻️ / 🧪 / 📚), the **commit subject in bold**, then a
sentence or two (or a short bullet list) explaining what the change actually does and why.
Reference `file paths` inline where it helps orient the reader. Don't enumerate every
file; focus on intent.

**⚠️ Notable** _(omit if nothing notable)_ — Conflicts resolved, tricky decisions,
edge cases handled, or anything that would surprise a reader of the diff.

**🚀 Landed** — One line: N commit(s) rebased onto `$TARGET`, worktree torn down,
tests passed (or the test outcome if different).

**Tone:** Knowledgeable colleague handing off work. Don't open with "Here is a recap"
or any meta-announcement — start directly with 🎯.

## Hard rules

- Never hand-roll merge or teardown — `/worktree-warden:merge-worktrees` owns all git mutation.
- Never `ExitWorktree action:"remove"` (refuses for session-created worktrees; no-op
  otherwise) — always use `"keep"` then delegate.
- Exit-12/13-style refusals from the engine are full stops — report, don't force.
