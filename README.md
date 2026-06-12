# worktree-warden

> Renamed from `worktrees` — update any slash commands you have saved to use the `worktree-warden:` prefix.

Keeps substantive edits **inside git worktrees**: a `PreToolUse` gate blocks
edits to a repo's main checkout (with a deliberate, time-boxed exception
mechanism), surfaces a repo's **git worktrees** at session start, and
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
  (cross-repo / `~/.claude`), the path is inside `.git/`, cwd is not a git
  repo, or the repo has **no commits yet** (an unborn-HEAD repo cannot host a
  worktree, so bootstrap edits land main-side until the first commit, with a
  one-time notice; enforcement resumes automatically once HEAD is born). It
  **fails open**: any unexpected error allows the edit, so a bug can
  never brick editing. On by default; opt out per-repo or globally. See
  [Enforcement gate](#enforcement-gate).
- **PreToolUse destruction gate** (`Bash|ExitWorktree`) — hard-blocks any
  command that would throw away a worktree whose content is **dirty or not yet
  landed** in the default branch: a raw `git worktree remove [--force]`,
  `rm -rf <worktree>`, `git branch -d/-D <branch>`, or an `ExitWorktree` remove.
  The merge/teardown engine is already guarded, but commands issued directly
  through the Bash tool bypass it — this closes that hole so a worktree can never
  be destroyed with un-landed or uncommitted work in it, regardless of which
  agent issues the command. The check is deterministic (clean **and** an ancestor
  of the default branch → allowed; anything else → exit-2 refusal with the
  command to land first). Blocks under every permission mode; **fails open** on
  any surprise (it only refuses when it can *prove* content is at risk). The
  engine's own internal git calls do not pass through Bash, so the gate never
  fights the engine.
- **SessionStart hook** — on `startup`/`resume`, only when cwd is the repo's
  **main** worktree, surfaces the repo's linked worktrees as a user-facing
  `systemMessage` banner: a category breakdown (mergeable / ⏳ cooldown / ❌
  live-session), the full `Ready?` table, and a concise recommendation of what
  each command does. What triggers it is the **`startup_display`** setting:
  - `always` (**default**) — show whenever the repo has ≥1 linked worktree of
    any kind, so a worktree you forgot about (on cooldown, or open in another
    tab's live session) is still surfaced for awareness.
  - `mergeable` — show only when ≥1 worktree is offerable for auto-merge; stay
    silent when every worktree is held back by cooldown or a live session.
  - `never` — suppress the banner entirely.

  The banner is shown to the user only — it is **not** injected into the agent's
  context and never asks the agent to act, so merging stays a deliberate,
  explicit user opt-in (you type the slash command). The hook never merges or
  mutates anything.
- **`/worktree-warden:check-worktrees`** — renders a table of **every** linked worktree
  with a `Ready?` verdict and a concise `Note` (state, commits ahead, last edit),
  then asks which to merge (All / None / a paged subset). Worktrees blocked by a
  live session are shown but never offered for merge.
- **`/worktree-warden:merge-worktrees`** — lands the chosen worktrees into the default
  branch from the primary checkout by **rebase + fast-forward** (linear history, no
  merge commits): commits dirty trees (via `commit-commands:commitall`), snapshots a
  restore anchor, determines a land order (escalating advisor → thinking-suite → HITL
  only when confidence is low), lands each via the engine with conflict handling,
  runs the test suite, and on failure rolls back to the exact pre-land state. The
  deterministic git work lives in `worktree_engine.py`; the skill only fills the
  judgement gaps.
- **`/worktree-warden:finish-worktree`** — lands the current worktree into the default
  branch and tears it down. Works whether the session arrived via `EnterWorktree`
  or started in the worktree directly (background jobs). Delegates to
  `/worktree-warden:merge-worktrees` for all git mutation, re-entering the worktree on
  rollback if the session was relocated.

## Scripts

- `scripts/worktree_gate.py` — the gate's pure decision policy plus the
  `worktree-gate` CLI (`grant`, `finished`, `disable`, `enable`, `set-window`,
  `set-startup-display`, `status`). The PreToolUse hook imports its policy; the
  CLI manages exceptions and persistent settings.
- `scripts/worktree_destruction.py` — pure, testable core of the destruction
  gate: parses a (possibly compound) shell command for destructive intent
  (`git worktree remove`, `rm -rf`, `git branch -d/-D`) and rules block/allow by
  checking the target worktree/branch is clean **and** landed. Imported by
  `hooks/guard_destruction_hook.py`.
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

The gate is **on by default**. When it blocks a main-checkout edit it prints
guidance to proceed. Two ways forward:

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

When the gate blocks Claude, its guidance names the
`worktree-warden:request-exception` and `worktree-warden:finish-exception` skills —
thin wrappers around the same `grant` / `finished` commands shown above.

### Settings & opt-out

Persisted as small JSON config at two scopes; a **project** value overrides the
**user** value key-by-key:

- **user** — `${XDG_CONFIG_HOME:-~/.config}/worktree-gate/config.json` (all repos).
- **project** — `<repo-root>/.claude/settings.worktree-warden.json` (this repo only;
  committable — teams can share a project config).

```bash
worktree_gate disable                    # turn the gate off for this repo
worktree_gate disable --user             # turn it off everywhere
worktree_gate enable [--user]            # turn it back on
worktree_gate set-window 30m             # tune the exception window (e.g. 900, 30s, 1h)
worktree_gate set-startup-display never  # session-start banner: mergeable | always | never
worktree_gate teardown-mode auto         # Stop hook behaviour: ask|auto|commit-only|always|never
worktree_gate teardown-mode auto --user  # same, user scope (all repos)
worktree_gate status                     # show effective settings + any active exception
```

Settings are stored per scope (project overrides user); the grant token and
debounce state live in `.git/` (ephemeral, never committed); the user config and
an `audit.log` of grants/uses/blocks live under the user config dir.

### Auto-teardown

The **Stop hook** fires when a session ends while inside a linked worktree that
has pending work. Its behaviour is governed by the `teardown_mode` setting:

| Mode | Behaviour |
| --- | --- |
| `ask` (default) | Self-assess completion; if done, use `AskUserQuestion` to offer commit + merge + teardown. |
| `auto` | Self-assess; if confidently done, commit + merge + teardown without confirmation. |
| `commit-only` | Commit dirty files; do not merge or tear down. |
| `always` | Verify task complete + tests pass + no major conflict with main; then commit + merge + teardown. |
| `never` | Never trigger. |

Set it at user scope (`--user`) to apply across all repos, or project scope
(no flag) to override per-repo.

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
