# Fallbacks

Read only if `finish` exits non-zero.

- `11 dirty_worktree`
  - `EnterWorktree(path:$WORKTREE_PATH)`
  - run `/commit-commands:commitall`
  - relocate to `$PRIMARY`
  - retry `finish`
- `13 rebase_conflict`
  - hand off to `/worktree-warden:merge-worktrees --worktree $WORKTREE_PATH --branch $BRANCH --repo $PRIMARY --target $TARGET`
- `18 tests_failed`
  - same handoff as above; state and lock are preserved
- `16 lock_blocked`
  - report holder; wait or force-unlock only on explicit user direction
- `10 already_merged`
  - recap briefly; teardown already happened
- `12`, `14`, `15`, `17`, `19`
  - report `message` verbatim and stop
