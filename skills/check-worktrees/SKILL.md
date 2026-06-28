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
