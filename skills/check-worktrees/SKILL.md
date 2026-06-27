---
name: check-worktrees
description: Review linked worktrees in this repo.
allowed-tools: Bash(python3 *) Skill(worktree-warden:merge-worktrees)
---

Repo-scoped only.

1. Render the table:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py
```

- empty output: say so and stop
- otherwise show the table verbatim in a code block

2. Get JSON:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json
```

Keep `path` and `branch`.

3. Offer only `ready: true` worktrees.

- if `cooldown` or `blocked` exist, mention them in one short line
- if none are ready, stop
- AskUserQuestion options:
  - Merge all N
  - Merge none
  - Choose specific
- For `Choose specific`, page ready worktrees at 4 max with `multiSelect: true`

4. If the chosen set is non-empty, invoke `/worktree-warden:merge-worktrees` with each chosen `path` + `branch`.

Notes:

- dirty worktrees with 0 commits are still mergeable; merge-worktrees commits them first
- `cooldown` is overridable only on explicit user request
- never offer live-session worktrees for automatic merge
