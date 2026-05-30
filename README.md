# worktrees

Keeps substantive edits **inside git worktrees**: a `PreToolUse` gate blocks
edits to a repo's main checkout (with a deliberate, time-boxed exception
mechanism), surfaces a repo's **mergeable git worktrees** at session start, and
offers a guided, confidence-gated, conflict-aware merge into the default branch.

`/check-worktrees` lists **every** linked worktree with a `Ready?` verdict: ✅ ready
to merge (or mergeable after a commit), 🧹 prunable (empty, or already merged into
`main`), ⏳ recently active (edited or had session activity in the last 15 min, so
held back from the auto-offer as a safety harness — overridable on explicit request),
or ❌ blocked by a **live `claude` session**. Everything is
**repo-scoped**: although `claude agents --json` lists sessions
across every repo on the machine, this plugin only ever inspects and acts on
worktrees of the repo your current session belongs to — never cross-repo.

## Components

- **PreToolUse gate** (`Edit|Write`) — hard-blocks edits to a repo's **main
  checkout**, steering substantive work into a worktree. Enforced under every
  permission mode (it blocks via **exit code 2**, *before* the permission layer,
  so `bypassPermissions` does not slip past it). It auto-allows — no action
  needed — when cwd is a linked worktree, the file is outside the checkout
  (cross-repo / `~/.claude`), the path is inside `.git/`, or cwd is not a git
  repo. It **fails open**: any unexpected error allows the edit, so a bug can
  never brick editing. On by default; opt out per-repo or globally. See
  [Enforcement gate](#enforcement-gate).
- **SessionStart hook** — on `startup`/`resume`, only when cwd is the repo's
  **main** worktree, silently checks for actionable worktrees (ready to merge,
  mergeable after a commit, or prunable — prune is not distinguished from merge
  in the count; recently-active ⏳ and live-session ❌ worktrees are excluded).
  If any exist, it emits a user-facing `systemMessage` banner
  naming the count plus the full table, and points the user at `/merge-worktrees`
  to land them (or `/check-worktrees` to review first). It stays silent when
  there is nothing actionable — including when **every** worktree is blocked by
  a live session. The banner is shown to the user only — it is **not** injected
  into the agent's context and never asks the agent to act, so merging stays a
  deliberate, explicit user opt-in (you type the slash command). The hook never
  merges or mutates anything.
- **`/worktrees:check-worktrees`** — renders a table of **every** linked worktree
  with a `Ready?` verdict and a concise `Note` (state, commits ahead, last edit),
  then asks which to merge (All / None / a paged subset). Worktrees blocked by a
  live session are shown but never offered for merge.
- **`/worktrees:merge-worktrees`** — lands the chosen worktrees into the default
  branch from the primary checkout by **rebase + fast-forward** (linear history, no
  merge commits): commits dirty trees (via `commit-commands:commitall`), snapshots a
  restore anchor, determines a land order (escalating advisor → thinking-suite → HITL
  only when confidence is low), lands each via the engine with conflict handling,
  runs the test suite, and on failure rolls back to the exact pre-land state. The
  deterministic git work lives in `worktree_engine.py`; the skill only fills the
  judgement gaps.
- **`/worktrees:finish-worktree`** — lands the current worktree into the default
  branch and tears it down. Works whether the session arrived via `EnterWorktree`
  or started in the worktree directly (background jobs). Delegates to
  `/worktrees:merge-worktrees` for all git mutation, re-entering the worktree on
  rollback if the session was relocated.

## Scripts

- `scripts/worktree_gate.py` — the gate's pure decision policy plus the
  `worktree-gate` CLI (`grant`, `finished`, `disable`, `enable`, `set-window`,
  `status`). The PreToolUse hook imports its policy; the CLI manages exceptions
  and persistent settings.
- `scripts/check_worktrees.py` — async, stdlib-only detector/renderer that lists
  every linked worktree and classifies each one's readiness. Shared by the hook
  and the skill (table + `--json`). Flags: `--cwd <path>`, `--json`.
- `scripts/worktree_engine.py` — deterministic land engine: `land` (preflight +
  rebase + ff-merge; leaves the rebase in progress on conflict), `rebase-continue`,
  `snapshot` (writes a JSON of the target tip, each branch tip, and its worktree
  path) / `undo` (restores those tips and **recreates any torn-down worktree on
  its branch** — so a roll-back even after teardown reconstructs the worktrees),
  and `teardown` (idempotent, path-gated worktree removal + `branch -d`, no
  `--force`/`-D`).

## Enforcement gate

The gate is **on by default**. When it blocks a main-checkout edit it prints the
commands to proceed. Two ways forward:

- **Isolate the work** (preferred): call `EnterWorktree`, then retry the edit.
- **Open a timed exception** when the edit is *legitimately* main-side (conflict
  resolution, landing to the default branch, or an explicit request to edit
  main):

  ```bash
  worktree_gate grant "resolving a merge conflict on main"   # opens a window
  # ... do the main-side edits ...
  worktree_gate finished                                     # close it early
  ```

  An exception is a single deliberate, **logged**, self-expiring window
  (15 min by default); it covers a burst of related edits and then closes on its
  own. Closing it the moment the work is done (`finished`) is expected.

### Settings & opt-out

Persisted as small JSON config at two scopes; a **project** value overrides the
**user** value key-by-key:

- user — `${XDG_CONFIG_HOME:-~/.config}/worktree-gate/config.json` (all repos).
- project — `<git-common-dir>/worktree-gate-config.json` (this clone only; lives
  inside `.git`, never committed).

```bash
worktree_gate disable            # turn the gate off for this repo
worktree_gate disable --user     # turn it off everywhere
worktree_gate enable [--user]    # turn it back on
worktree_gate set-window 30m     # tune the exception window (e.g. 900, 30s, 1h)
worktree_gate status             # show effective settings + any active exception
```

The grant token and project config live next to each other under the repo's
git directory; the user config and an `audit.log` of grants/uses/blocks live
under the user config dir.

## Safety

- Linear history only — rebase + ff-merge, never a merge commit.
- Rollback (engine `undo`) restores the target + each branch tip via a **scoped,
  anchor-protected `git reset --hard`** (or `update-ref` where no worktree holds the
  branch) and recreates any torn-down worktree on its branch: safe because everything
  is committed and snapshotted before any rebase, and `--hard` never touches untracked
  files. Mid-rebase conflicts abort cleanly (no reset).
- Conflict resolution prompts the user only when confidence is low/medium.
- Teardown refuses dirty worktrees and unmerged branches.
- Respects the active project's CLAUDE.md (e.g. SSH-approval gates).

## License

MIT
