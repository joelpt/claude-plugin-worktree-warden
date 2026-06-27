---
name: auto-teardown
description: Stop-hook policy for pending linked worktree work.
disable-model-invocation: true
---

`finish-worktree` is the default landing path for one worktree.

1. Read mode:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py teardown-mode
```

If `never`, stop.

2. Read worktree state:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json
```

If nothing is pending, stop.

3. Act by mode:

- `ask`
  - AskUserQuestion with: finish now, commit only, skip
- `auto`
  - if confidently done: run `/worktree-warden:finish-worktree`
  - if uncertain: use `ask`
  - else stop
- `commit-only`
  - if dirty: run `/commit-commands:commitall`
- `always`
  - require: task complete, tested, no non-trivial main conflict
  - if all true: run `/worktree-warden:finish-worktree`
  - else report which check failed

4. If `finish-worktree` falls back to merge recovery, follow that path.
