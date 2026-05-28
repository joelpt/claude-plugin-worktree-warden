---
name: check-worktrees
description: List the current repo's linked git worktrees and their state (dirty, commits ahead, last commit, live session), then offer to merge the mergeable ones. Use on /check-worktrees, "show worktrees", "any stale worktrees?", or when the SessionStart hook flags mergeable worktrees. Pass --show-all to include worktrees that have a live session.
argument-hint: "[--show-all]"
allowed-tools: Bash(python3 *) Skill(worktrees:merge-worktrees)
---

# /check-worktrees

Repo-scoped review of git worktrees. Operates ONLY on the current repo (the
one this session's cwd belongs to) and its linked worktrees — never cross-repo.

## Procedure

### 1. Render the table
Run the detector (pass `--show-all` only if the user supplied it):

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py $ARGUMENTS
```

- **Empty output** → there are no mergeable worktrees. Say so in one line and
  stop (when triggered by the SessionStart hook, just continue silently).
- **Otherwise** → show the table verbatim to the user. By default it lists only
  **orphans** (worktrees with no live `claude` session); `--show-all` adds the
  ones that have a session, annotated with their `status`/`kind`.

### 2. Get the structured set
Fetch the machine-readable list (same flags you used above, plus `--json`):

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json $ARGUMENTS
```

Each entry has `path`, `branch`, `dirty`, `commit_count`, `behind_base`,
`last_rel`, `mtime`, and `session_*`. Keep this list — `/worktrees:merge-worktrees`
needs the exact `path` + `branch` of each chosen worktree.

### 3. Ask which to merge
`AskUserQuestion` — *"Merge worktrees before continuing?"* with options:
- **Merge all N** — every listed worktree.
- **Merge none** — stop here, change nothing.
- **Choose specific** — proceed to subset selection.

For **Choose specific**, present the worktrees with `multiSelect: true` in
**pages of at most 4** (one option per worktree; label = `branch` + age from
`last_rel`, e.g. `feat-x · 2 hours ago`). Accumulate selections across pages
until every worktree has been offered. The union of ticked options is the set.

### 4. Hand off
If the chosen set is non-empty, invoke **`/worktrees:merge-worktrees`**, passing
the chosen worktrees' `path` + `branch` (from step 2's JSON). If empty, stop.

## Notes
- `--show-all` is for manual inspection; never merge a worktree that has a live
  session without the user explicitly choosing it (the merge skill re-checks).
- This skill only lists and asks — all merging/pruning happens in
  `/worktrees:merge-worktrees`, which is human-gated when confidence is low.
