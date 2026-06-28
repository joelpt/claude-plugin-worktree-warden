---
name: finish-worktree
description: Finish the current linked worktree.
argument-hint: "[target-branch]"
allowed-tools: Bash(python3 *) Bash(git *) Bash(cd *) ExitWorktree EnterWorktree Skill(worktree-warden:merge-worktrees) Skill(worktree-warden:check-worktrees) Skill(commit-commands:commitall)
---

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`

Single-worktree fast path. Prefer this over `/worktree-warden:merge-worktrees`.

1. Preflight:

```bash
python3 $ENGINE finish-preflight [--target $ARGUMENTS]
```

- Read `primary`, `target`, `branch`, `worktree`, `details.kind`, `details.detached`, `details.test_argv`.
- If not a repo or engine error: report `message`.
- If `details.kind == "primary"`: use `/worktree-warden:check-worktrees`.
- If detached or `branch == target`: stop.

2. Relocate:

- `ExitWorktree(action:"keep")`
- If no-op, `cd "$PRIMARY"`

3. Run finish:

```bash
python3 $ENGINE --repo $PRIMARY finish --worktree $WORKTREE_PATH --branch $BRANCH --target $TARGET ...
```

Append the `details.test_argv` returned by `finish-preflight`.

4. Exit handling:

- `0`: recap from `details.recap`; keep it compact
- non-zero: read [fallbacks.md](fallbacks.md)

Rules:

- never hand-roll merge or teardown
- never `ExitWorktree(action:"remove")`
- use `/worktree-warden:merge-worktrees` only for fallback paths
