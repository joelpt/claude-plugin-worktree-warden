---
name: merge-worktrees
description: Land selected worktrees into the default branch.
allowed-tools: Bash(python3 *) Bash(git *) Skill(commit-commands:commitall)
---

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`,
`GATE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py`,
`LOCK=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_lock.py`

Use this skill for:

- multiple worktrees
- conflict recovery after `finish-worktree`
- test-failure recovery after `finish-worktree`
- explicit merge selection from `/worktree-warden:check-worktrees`

Prefer `/worktree-warden:finish-worktree` for one normal worktree.

1. Establish `REPO`.

- If `--repo` was passed, use it.
- Otherwise be in the primary checkout. If inside a linked worktree, `ExitWorktree(action:"keep")`. If that is a no-op, `cd $REPO`.

2. Preflight once:

```bash
python3 $ENGINE --repo $REPO preflight --branches <b1,b2,...>
```

- Read `details.target` and `details.worktrees`.
- Choose the land order now; the final engine call will honor the order you pass.

3. Commit dirty worktrees one at a time.

- show dirty files and `git -C <path> diff HEAD --stat`
- ask before committing
- if yes: `EnterWorktree(path:<path>)` -> `/commit-commands:commitall` -> `ExitWorktree(action:"keep")`
- if no: drop that worktree from the set

4. Run the batched engine path once for the remaining clean branches, in the chosen order.

- default order: oldest first
- if unclear: read [order.md](order.md)
- resolve the repo test command inline at skill-run time if needed:
  - `just test` when `Justfile` has `test:`
  - else `npm test` when `package.json` exists
  - else `pytest` for Python repos
  - else `cargo test` when `Cargo.toml` exists
  - else `--skip-tests`

```bash
python3 $ENGINE --repo $REPO finish-many --target $TARGET --branches <ordered,b1,b2,...> ...
```

- Append either `--test-cmd <cmd>` or `--skip-tests`.
- `0`: recap from `details.recap`; keep it compact
- `13`: read [conflict.md](conflict.md)
- `18`: read [failure.md](failure.md)
- `16`: report holder; wait or force-unlock only on explicit user direction
- `11`, `12`, `14`, `15`, `17`, `19`: report `message` and stop

Never hand-roll merge, reset, or teardown.
