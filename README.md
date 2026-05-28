# worktrees

Surfaces a repo's **mergeable git worktrees** at session start and offers a
guided, confidence-gated, conflict-aware merge into the default branch.

A worktree is "mergeable" when it has **no live `claude` session** sitting in it.
Everything is **repo-scoped**: although `claude agents --json` lists sessions
across every repo on the machine, this plugin only ever inspects and acts on
worktrees of the repo your current session belongs to — never cross-repo.

## Components

- **SessionStart hook** — on `startup`/`resume`, only when cwd is the repo's
  **main** worktree, silently checks for mergeable worktrees. If any exist, it
  injects an instruction to run `/worktrees:check-worktrees`. It never merges or
  mutates anything; silent when there's nothing to surface.
- **`/worktrees:check-worktrees`** — renders a table of the repo's linked
  worktrees (dirty state, commits ahead, last commit, live session) and asks
  which to merge (All / None / a paged subset). `--show-all` includes worktrees
  that currently have a session.
- **`/worktrees:merge-worktrees`** — lands the chosen worktrees into the default
  branch from the primary checkout: commits dirty trees (via
  `commit-commands:commitall`), guards against stale bases, re-checks for live
  sessions, determines a merge order (escalating advisor → thinking-suite → HITL
  only when confidence is low), merges with conflict handling, verifies, runs the
  test suite, and prunes merged worktrees with a deterministic teardown.

## Scripts

- `scripts/check_worktrees.py` — async, stdlib-only detector/renderer. Shared by
  the hook (`--json`, for the gate) and the skill (table + `--json`).
  `--cwd <path>`, `--show-all`, `--json`.
- `scripts/prune_worktree.py` — deterministic, idempotent, path-gated worktree +
  branch teardown (no `--force`, no `branch -D`).

## Safety

- The merge flow never uses `git reset`; rollback is via `git revert`.
- Conflict resolution prompts the user only when confidence is low/medium.
- Teardown refuses dirty worktrees and unmerged branches.
- Respects the active project's CLAUDE.md (e.g. SSH-approval gates).

## License

MIT
