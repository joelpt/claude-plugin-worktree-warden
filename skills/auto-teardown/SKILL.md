---
name: auto-teardown
description: Commit, merge, teardown worktrees
disable-model-invocation: true
---

You are running the **auto-teardown** skill. This is invoked automatically by the Stop hook when it detects pending worktree work.

## Step 1 — Read the configured mode

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py teardown-mode
```

Note the effective mode (`ask`, `auto`, `commit-only`, `always`, or `never`). If `never`, stop immediately.

## Step 2 — Show worktree state

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json
```

Display a brief summary: branch name, commits ahead, dirty files. If the worktree shows 0 commits ahead and no dirty files, report "nothing to land" and stop.

## Step 3 — Act per mode

### `ask`

Use **AskUserQuestion** with:
- Question: "This worktree has pending work — what would you like to do?"
- Options:
  - "Commit + merge + teardown" — commit dirty files, land to main, remove the worktree
  - "Commit only" — commit dirty files, leave the worktree in place
  - "Skip for now" — do nothing

Proceed with the chosen action after the user responds.

### `auto`

Self-assess completion:

- **Confident the task is done**: Commit any dirty files (via `/commit-commands:commitall`), then invoke `/worktree-warden:merge-worktrees` to land and tear down. No confirmation needed.
- **Uncertain**: Use AskUserQuestion as in `ask` mode.
- **Not done**: Report "task not yet complete" and stop.

### `commit-only`

If there are dirty files to commit: run `/commit-commands:commitall`. Do **not** merge or tear down. Report what was committed.

### `always`

Self-assess **all three criteria** before proceeding:

1. **Task complete** — the original request has been fully addressed.
2. **Tested** — run `just test` (or `npm test` / `pytest` / `cargo test` if Justfile absent) and confirm passing. If tests fail, report the failure and stop.
3. **No non-trivial conflict with main** — check `git log --oneline main..HEAD` and `git diff --stat main`. Trivial changes (new files, isolated edits) are fine. If main was significantly refactored since the worktree branched, stop and explain.

If all three hold: commit, invoke `/worktree-warden:merge-worktrees`, tear down.
If any fails: report **which criterion** failed and why. Do **not** proceed.

## Step 4 — After committing/merging

Report the outcome:
- What was committed (if anything)
- Whether the branch was merged to main
- Whether the worktree was removed
- The new state of the default branch (tip SHA, brief log)

If `/worktree-warden:merge-worktrees` encounters a conflict it cannot resolve automatically, it will pause and explain — follow its guidance.
