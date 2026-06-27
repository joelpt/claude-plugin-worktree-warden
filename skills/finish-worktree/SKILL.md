---
name: finish-worktree
description: Land the current linked worktree into its default branch and tear it down. Use when wrapping up work from inside a worktree. From the primary checkout, use check-worktrees instead.
argument-hint: "[target-branch]"
allowed-tools: Bash(python3 *) Bash(git *) Bash(cd *) ExitWorktree EnterWorktree Skill(worktree-warden:merge-worktrees) Skill(worktree-warden:check-worktrees) Skill(commit-commands:commitall)
---

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`

## Contract

`finish-worktree` is the single-worktree fast path. The green path is one engine
call after relocation:

```bash
python3 $ENGINE --repo $PRIMARY finish --worktree $WORKTREE_PATH --branch $BRANCH --target $TARGET --test-cmd "<cmd>"
```

Do not invoke `/worktree-warden:merge-worktrees` on the green path. It is only
for exit `13` conflicts, exit `18` test failures, multiple worktrees, or a
deliberate landing order.

## Procedure

1. Run preflight:

   ```bash
   python3 $ENGINE finish-preflight --worktree "$(git rev-parse --show-toplevel)" [--target $ARGUMENTS]
   ```

   Read top-level `primary`, `target`, `branch`, and `worktree`; read
   `details.kind` and `details.detached`.

2. Stop or reroute:

   - not a repo / engine error: report the message.
   - `details.kind == "primary"`: use `/worktree-warden:check-worktrees`; this
     command only finishes the current linked worktree.
   - `details.detached`: stop; there is no branch to land.
   - `BRANCH == TARGET`: stop; nothing should land.

3. Relocate to the primary checkout:

   - First call `ExitWorktree(action:"keep")`.
   - If it is a no-op because this was not an `EnterWorktree` session, run
     `cd "$PRIMARY"` via Bash. Continue with all engine calls using `--repo`.

4. Resolve the test command, in order: `just test` if Justfile has `test`;
   `npm test` if `package.json`; `pytest` if Python tests/project metadata;
   `cargo test` if Cargo; otherwise use `--skip-tests`.

5. Run `finish`:

   ```bash
   python3 $ENGINE --repo $PRIMARY finish --worktree $WORKTREE_PATH --branch $BRANCH --target $TARGET --test-cmd "<cmd>"
   ```

   Use `--skip-tests` instead of `--test-cmd` only when no test command applies.

6. Handle by exit code:

   - `0 finished`: landed, tested if applicable, torn down, lock released.
   - `10 already_merged`: torn down; no new commits to recap.
   - `11 dirty_worktree`: `EnterWorktree(path:$WORKTREE_PATH)`,
     `/commit-commands:commitall`, relocate again, then retry step 5.
   - `13 rebase_conflict`: stop the fast path. Hand off to
     `/worktree-warden:merge-worktrees --worktree $WORKTREE_PATH --branch $BRANCH --repo $PRIMARY --target $TARGET`.
   - `18 tests_failed`: state is preserved and the lock is kept. Hand off to
     `/worktree-warden:merge-worktrees` for fix-forward / undo / abandon.
   - `16 lock_blocked`: report the holder from `message`/`details`; wait or
     force-unlock only on explicit user direction.
   - `12`, `14`, `15`, `17`, `19`: report `message` verbatim and stop.

## Recap

Keep the success recap compact by default: no more than 200 words or 5-10 bullets.
Use `details.recap` from the `finish` JSON when present. Mention:

- target and branch
- number of commits landed
- commit subjects
- changed file count and capped top file list
- test result and teardown result

Never run raw `git log --stat` or full diff output for the default recap. Use
larger git output only if the user explicitly asks for a detailed recap.

## Hard Rules

- Never hand-roll merge or teardown.
- Never `ExitWorktree(action:"remove")`; use `"keep"` and delegate teardown to
  the engine.
- `merge-worktrees` is fallback orchestration, not part of the normal
  single-worktree success path.
