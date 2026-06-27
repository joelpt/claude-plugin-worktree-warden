---
name: merge-worktrees
description: Land selected worktrees into the default branch.
allowed-tools: Bash(python3 *) Bash(git *) Skill(commit-commands:commitall)
---

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`,
`GATE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py`,
`LOCK=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_lock.py`,
`RESOLVE_TEST=${CLAUDE_PLUGIN_ROOT}/scripts/resolve_test_cmd.py`

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

Read `details.target` and `details.worktrees`.

3. Acquire the main-target lock:

```bash
python3 $LOCK --repo $REPO acquire-main "merge-worktrees: landing <b1,b2,...> into $TARGET"
```

- blocked: ask whether to wait; force-unlock only on explicit user direction
- broken or fail-open output: proceed, but note lock protection is off

4. Commit dirty worktrees one at a time.

- show dirty files and `git -C <path> diff HEAD --stat`
- ask before committing
- if yes: `EnterWorktree(path:<path>)` -> `/commit-commands:commitall` -> `ExitWorktree(action:"keep")`
- if no: drop that worktree from the set

5. Snapshot:

```bash
python3 $ENGINE --repo $REPO snapshot --target $TARGET --branches <b1,b2,...> --require-lease
```

Save `details.snapshot_file`.

6. Choose land order.

- default: oldest first
- if unclear: read [order.md](order.md)

7. Land each worktree:

```bash
python3 $ENGINE --repo $REPO land --worktree <path> --branch <branch> --target $TARGET --require-lease
```

- `0`: continue
- `10`: already merged; continue to teardown later
- `13`: read [conflict.md](conflict.md)
- `19`: stop immediately; report `message`
- `11`, `12`, `14`, `15`, `17`: report `message`, release lock, stop

8. Verify and test after all lands.

```bash
git -C $REPO log --oneline -n 20
git -C $REPO status --porcelain
python3 $RESOLVE_TEST "$REPO"
```

Run the returned test argv unless it is `--skip-tests`.

- pass: continue
- fail or verify wrong: read [failure.md](failure.md)

9. Teardown landed worktrees:

```bash
python3 $ENGINE --repo $REPO teardown --branch <branch> --target $TARGET --require-lease
python3 $LOCK --repo $REPO release-main
```

Never hand-roll merge, reset, or teardown.
