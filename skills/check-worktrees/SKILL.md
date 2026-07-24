---
name: check-worktrees
description: Review this repo's linked worktrees.
allowed-tools: Bash(python3 *) Skill(worktree-warden:merge-worktrees)
---

Repo-scoped only.

1. Fetch table + JSON in one call:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --bundle-json
```

- Read `table` and `worktrees`.
- if `table` is empty, say so and stop
- otherwise show the table verbatim in a code block
- Keep each ready worktree's `path` and `branch`.

2. Offer only `ready: true` worktrees.

- if `cooldown` or `blocked` exist, mention them in one short line
- if any `category == "merged"` worktrees exist: `commit_count == 0` there is ambiguous by
  construction — it means either this branch's work already landed by some other path, or
  the branch never had any commits to begin with (both look identical once HEAD is an
  ancestor of base). Before offering these in "Merge all N", check ground truth for each
  one (its tracked issue/PR status, or another record of the work) — do not take the
  "merged" label on faith. Call this out to the user as a distinct line, separate from
  plain `ready` worktrees.
- if none are ready, stop
- AskUserQuestion options:
  - Merge all N
  - Merge none
  - Choose specific
- For `Choose specific`, page ready worktrees at 4 max with `multiSelect: true`

3. If the chosen set is non-empty, invoke `/worktree-warden:merge-worktrees` ONCE with the chosen branches in the selected order.

- Pass every selected `path` + `branch` together in the same invocation.
- Never invoke `/worktree-warden:merge-worktrees` once per worktree.

Notes:

- dirty worktrees with 0 commits are still mergeable; merge-worktrees commits them first
- `cooldown` is overridable only on explicit user request
- never offer live-session worktrees for automatic merge
