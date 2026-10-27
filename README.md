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
- **PostToolUse lock hook** (`EnterWorktree`) — `git worktree lock`s every
  linked worktree immediately after the harness creates it, so its admin
  record survives indefinitely (git never marks a locked worktree `prunable`
  or drops it, even once its directory disappears). This is what makes
  `recover` and the population-tracking advisory reliable for a worktree
  whose directory is later removed out from under git: without a lock, a bare
  `git worktree prune` would erase the record entirely once the directory was
  gone. Locking is best-effort and **fails open** — any error (already
  locked, git predates lock support, malformed hook payload) never blocks the
  session. `worktree_engine`'s own `teardown` is the only path that ever
  unlocks (immediately before its own `git worktree remove`), so a worktree
  stays locked for its entire life outside of a sanctioned teardown.
- **PreToolUse destruction gate** (`Bash`) — hard-blocks, under every
  permission mode, the commands that would destroy un-landed worktree state.
  It judges the command's *shape*, never the live lock state (the hook runs
  before the command, so in `unlock X && remove --force X` the lock still reads
  as held):
  - `git branch -d/-D <branch>` whose worktree is **dirty or not yet landed**
    in the default branch (clean **and** landed → allowed).
  - `git worktree unlock` and `rm` of anything under `.git/worktrees/` (a
    worktree's admin record or its `locked` file) — refused outright. The
    engine's own `teardown` unlocks from a Python subprocess that never passes
    through this gate, so no sanctioned flow needs these from Bash.
  - `git worktree remove` with **two or more force flags** (`-f -f`, `-ff`,
    `--force --force`, any `--f…` abbreviation) — refused outright, since git's
    own error for a locked worktree advertises `remove -f -f` as the override.
  - `git worktree remove --force` (one force) — judged by the target
    worktree's *path* (bare folder names and `cd`/`-C` relative targets
    resolve): dirty files, then commits on its HEAD not in the default branch,
    which also covers a detached HEAD. A target that is not a registered
    worktree, or whose status cannot be read, is refused (fails **closed**).
    An unforced `remove` is left to git, which already refuses a dirty tree.

  The refusal points to `/worktree-warden:finish-worktree`; a stale worktree
  that really must go is for the user to run themselves with the `!` prefix.
  The git worktree lock (PostToolUse lock hook above) is a second layer, not
  the gate: that hook fails open, and an `ExitWorktree` remove still relies on
  it (re-verified against the live lock state).

  A command whose text contains a destructive verb (`git branch -D`,
  `git worktree remove|unlock`, `.git/worktrees`) *and* a construct the parser
  cannot model (`sh -c`, `xargs`, `eval`, `find -exec`, command substitution,
  heredocs, `if`/`{ }`/`( )` blocks, an unterminated quote) is refused, as is a
  `GIT_DIR`/`--git-dir`/`--work-tree` override or a `cd` whose target is only
  known at run time.
  This is a text heuristic and can over-block.

  A git invocation that injects config able to run a command (`git -c alias.x=... x`,
  `-c core.fsmonitor=...`, `--config-env`, `GIT_CONFIG_*` and editor/pager env vars)
  is refused outright, for any subcommand.

  **`rm -rf <worktree directory>` and destruction hidden behind a script the
  Bash call merely invokes** remain a **known, PERMANENT,
  uncompensated bypass** — an unclosable static-parser gap, and git's own
  worktree lock does not close it either: locking only protects the admin
  *record* from `prune`, it does nothing to stop the worktree's files being
  deleted directly on disk. The Stop-hook WIP bundle capture (below) and the
  `recover`/`reconcile` external-removal audit are the sole backstops for that
  window — they make the loss *detectable and recoverable*, never *prevented*.
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
- **`/worktree-warden:merge-worktrees`** — fallback / multi-worktree landing path from
  the primary checkout: commit dirty trees, snapshot, choose order, land by
  rebase + fast-forward, test, then tear down. The deterministic git work lives
  in `worktree_engine.py`; the skill handles only the judgment gaps.
- **`/worktree-warden:finish-worktree`** — lands the current worktree into the default
  branch and tears it down. Works whether the session arrived via `EnterWorktree`
  or started in the worktree directly (background jobs). This is the normal
  single-worktree path; it falls back to `/worktree-warden:merge-worktrees` only
  for conflicts, test failures, or deliberate multi-worktree landing.

## Scripts

- `scripts/worktree_gate.py` — the gate's pure decision policy plus the
  `worktree-gate` CLI (`grant`, `finished`, `disable`, `enable`, `set-window`,
  `set-startup-display`, `status`). The PreToolUse hook imports its policy; the
  CLI manages exceptions and persistent settings.
- `scripts/worktree_standdown.py` — the session-scoped stand-down registry
  (`register`, `release`, `active`, `show`); see "Stand-down registry" below.
- `scripts/worktree_destruction.py` — pure, testable core of the destruction
  gate: parses a (possibly compound) shell command for destructive intent
  (`git branch -d/-D`, `git worktree remove`/`unlock`, `rm` of the
  `.git/worktrees/` admin area) and rules block/allow by command shape or by
  checking the target worktree/branch is clean **and** landed. Imported by
  `hooks/guard_destruction_hook.py`.
- `scripts/worktree_wip.py` — non-destructive WIP capture: snapshots a worktree's
  tracked + untracked uncommitted content (via a temporary index) into a git
  bundle outside all refs. Called by the Stop hook and (for dirty orphans) at
  SessionStart; surfaced by engine `recover`.
- `scripts/worktree_population.py` — cross-session worktree-population tracking:
  records each linked worktree's branch/head/dirty state per session and diffs
  against the prior snapshot to flag external removals warden's audit log does
  not account for. Driven by the SessionStart hook.
- `scripts/check_worktrees.py` — async, stdlib-only detector/renderer that lists
  every linked worktree and classifies each one's readiness. Shared by the hook
  and the skill (table + `--json`). Flags: `--cwd <path>`, `--json`.
- `scripts/worktree_engine.py` — deterministic land engine: `land` (preflight +
  rebase + ff-merge; leaves the rebase in progress on conflict), `rebase-continue`,
  `snapshot` (writes a JSON of the target tip, each branch tip, and its worktree
  path) / `undo` (restores those tips and **recreates any torn-down worktree on
  its branch** — so a roll-back even after teardown reconstructs the worktrees),
  and `teardown` (idempotent, path-gated worktree removal + `branch -d`; no
  `--force`/`-D` unless you pass `--discard`, the explicit abandon path for work
  that must NOT land -- which still honours the path gate, and first salvages the
  branch tip and any dirty tracked state into the object store so the discard
  stays recoverable). Every mutation (`land`/`teardown`/`undo`/`snapshot`) appends a
  JSON line to `<git-common-dir>/worktree-warden/audit.log` — recording the
  action, branch, target, worktree path, and key SHAs (the torn-down branch tip,
  the pre-`undo` reset SHAs) — so a vanished worktree is diagnosable and its last
  commit recoverable, not a mystery. This log rotates at 10MB: the current
  `audit.log` becomes `audit.log.1`, existing `audit.log.N` shift to `N+1`, and
  anything beyond `audit.log.5` is dropped — read newest-first as `audit.log`,
  `audit.log.1`, `audit.log.2`, ... for forensic reconstruction. Rotation is a
  non-blocking, best-effort check on every append (never raises, never blocks
  on a lock) so concurrent `land`/`teardown`/`undo`/`snapshot` calls are safe.
  The `recover` subcommand scans for stranded
  (`prunable`) worktrees whose branch still holds unlanded commits, and for WIP
  capture bundles, printing the exact restore command for each (`--gc-days N`
  expires old bundles). `check_worktrees` now reports a worktree whose git state
  could not be read as **UNKNOWN** rather than silently classifying it prunable —
  a flaky `git status` under load can no longer make real work look disposable.
  The **`finish`** subcommand is the one-shot happy path for a single clean worktree —
  lock → snapshot → land → `--test-cmd` gate → teardown → release in a single call
  (what `/finish-worktree` uses); it bails cleanly (preserving state and the lock) on a
  conflict (13) or test failure (18) so the caller can take over, and never auto-rolls-back.
- `scripts/worktree_lock.py` — cooperative, advisory **concurrency lock** plus the
  `worktree-lock` CLI (`acquire-main`, `release-main`, `refresh-main`, `status`,
  `force-unlock [--all]`). Serializes multi-step operations git can't (the
  multi-process merge above all): one lock per `realpath(worktree-toplevel)` in
  `<git-common-dir>/worktree-warden/locks.json`, owner = session id, each
  check-and-set guarded by a short-lived `flock`. Used by `/merge-worktrees`,
  `/bumpall`, and the engine; surfaced at SessionStart. See *Concurrency lock* below.

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

At SessionStart the hook also runs three best-effort safety passes (independent of
the banner). First it **checks for gate-load-error**: if either the PreToolUse edit
gate or the destruction gate failed to import their modules, a high-priority advisory
is surfaced (visible even when `startup_display` is `"never"`), warning that the gate
is OPEN and restoration requires fixing the plugin installation. Second it **captures
the WIP of any dirty worktree** it finds — the backstop for a *force-quit*, which
kills the session before its Stop hook can capture (graceful-Stop capture cannot help
there). Third it **snapshots the repo's worktree population** and diffs it against
the previous session: a worktree that was present last time, is gone now, still held
content (dirty or unlanded commits), and that warden's own audit log does **not**
account for is surfaced as an **external removal** — named, with its last-known SHA
and a recovery command. That is what turns "a worktree mysteriously vanished" into
"X was removed by something other than warden between session A and B; recover it
with this command."

### Gate load failure (fail-open loudly)

The **PreToolUse edit gate** and **destruction gate** each wrap their module imports
in try/except: if `worktree_gate` or `worktree_destruction` fail to import (due to a
syntax error, broken dependency, or misplaced installation), the hook:

- Writes a diagnostic to stderr immediately, naming the module that failed and its error.
- Drops a **sentinel file** at `<git-common-dir>/worktree-warden/gate-load-error`,
  recording a timestamp, module name, exception, and full traceback.
- Exits 0 deliberately (documented fail-open), allowing the edit or operation.

The **SessionStart hook** detects the sentinel and surfaces a high-priority advisory
(see example above). This ensures a plugin deployment error is never silent — every
user sees the diagnostic immediately when they start a session. Once the import error
is fixed, the next session start self-heals: if the modules import cleanly the sentinel
is removed and the advisory disappears automatically. The fix is to restore the plugin
installation (e.g. verify the Python path or re-run `claude plugin reload`), then
retry the session.

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
worktree_gate run-tests-on-finish on     # let finish auto-detect + run the repo's tests (default: off)
worktree_gate status                     # show effective settings + any active exception
```

Settings are stored per scope (project overrides user); the grant token and
debounce state live in `.git/` (ephemeral, never committed); the user config and
an `audit.log` of grants/uses/blocks live under the user config dir. This log
rotates at the same 10MB / 5-generation cap as the engine's audit log above
(`audit.log` -> `audit.log.1` -> ... -> `audit.log.5`, oldest dropped), via the
same non-blocking, best-effort `rotate_audit_log()` helper.

### Force-disable switch (for orchestrators that own the worktree lifecycle)

Setting the environment variable `WORKTREE_WARDEN_FORCE_DISABLE=1` makes all four hooks
(`auto_teardown_hook`, `check_worktrees_hook`, `enforce_worktree_hook`,
`guard_destruction_hook`) exit 0 immediately: no output, no WIP bundle, no lock, no
audit entry, no debounce state, no diagnostic. Only the exact value `1` counts. The check
runs before the hooks' guarded `scripts/` imports, so it holds even on a broken install.
It also turns off the auto-detected post-land test run in finish, regardless of the
`run-tests-on-finish` setting.

It exists for one caller: an orchestrator (the Jack plugin) that drives a whole session
and must be the only thing that merges and removes worktrees. Without it, the Stop hook's
"run finish-worktree" nudge can send a pipeline worker off to land and tear down its own
worktree outside the orchestrator's authority. The variable is inherited by every child
process, and a change to the `env` block of the project's `.claude/settings.local.json`
reaches later hooks of a running session, including subagents and child `claude`
processes. A Bash-tool `export` does not reach hooks.

**This switch is deliberately undiscoverable from inside a session.** It is documented only
in this README, `PLAN.md` and `CHANGELOG.md`. Never mention it, or the variable's name, in
hook output, skill text, error messages, the audit log or any other text an agent reads;
the point is that a Claude session working under warden cannot find it and talk itself
into using it. The skills `finish-worktree`, `check-worktrees` and the others are
unchanged and keep working when the switch is off. A test pins the no-mention rule.

### Stand-down registry (for other plugins)

A plugin that drives a whole session and must be the only thing merging and removing
worktrees can ask warden to stand down for **that session only**. Warden's four hooks and
the finish auto-test run then do nothing for the session, and stay fully active for every
other session in the repo. This is the cross-plugin contract; the failure direction is part
of it: **any doubt means warden stays active.**

- **Files.** `<git-common-dir>/worktree-warden-standdown.json` (data) and
  `<git-common-dir>/worktree-warden-standdown.lock` (flock sidecar, never deleted). The
  directory is `git rev-parse --path-format=absolute --git-common-dir` run in the cwd, then
  `realpath`, so the main checkout and every linked worktree share one file.
- **Format (v1).** `{"version": 1, "sessions": {"<session_id>": {"claims": {"<owner>": {"pid": 123,
  "pid_comm": "claude", "reason": "...", "since": "<ISO-8601 UTC>"}}}}}`.
- **Claim keying.** One claim per (session, owner), so two plugins in one session never overwrite
  each other. `release` removes only the named owner's claim and is idempotent. Warden stands
  down while any valid claim exists for the session.
- **Liveness.** A claim is valid only while `pid` is alive (`kill(pid, 0)`; `EPERM` counts as
  alive) and `ps -o comm=` for that pid still has the recorded `pid_comm` basename (guards pid
  reuse). A crashed client therefore cannot strand a stand-down.
- **Writers** (`register`, `release`) take an exclusive `flock` on the sidecar, read the JSON
  (missing, empty, malformed or unknown-version content is treated as empty and overwritten),
  prune every dead claim and claim-less session, then publish with a same-directory temp file and
  `os.replace`. An empty registry deletes only the data file. Pruning never removes a live claim.
- **Readers** (`is_standing_down`, and every hook) never write and never lock. They look up only
  the caller's own session id, verify liveness, and return "not standing down" on a missing
  session id, missing or unreadable file, parse error, unknown `version`, or git error.
- **Releasing is the client's job.** Warden does not release on `Stop`/`SubagentStop`.

CLI (`python3 scripts/worktree_standdown.py <command>`; `--session-id` defaults to
`$CLAUDE_CODE_SESSION_ID`, `--cwd` to the current directory):

| Command | Behavior |
|---------|----------|
| `register --owner NAME [--reason TEXT]` | Claim for this session. The pid is the nearest ancestor process whose command name contains `claude`; with none, it refuses and exits 2. |
| `release --owner NAME` | Drop that owner's claim (no error if absent). |
| `active` | Silent; exit 0 if standing down, else 1. |
| `show` | Print the registry JSON (`{}` if none). |

Errors are one stderr line and exit 2. From Python: `register`, `release`, `is_standing_down`,
`canonical_common_dir`. Each stand-down a hook honors is written to `audit.log` as
`standdown<TAB>hook: owner=... reason=...`.

The environment switch above still works in this release, as a migration shim. It is
**deprecated and will be removed in the next release**; move to the registry.

### Auto-teardown

The **Stop hook** fires when a session ends while inside a linked worktree that
has pending work. Its behaviour is governed by the `teardown_mode` setting:

| Mode | Behaviour |
| --- | --- |
| `ask` (default) | Self-assess completion; if done, ask whether to finish now, commit only, or skip. |
| `auto` | Self-assess; if confidently done, run `finish-worktree` without confirmation. |
| `commit-only` | Commit dirty files; do not merge or tear down. |
| `always` | Verify task complete + tests pass + no major conflict with main; then run `finish-worktree`. |
| `never` | Never trigger. |

Set it at user scope (`--user`) to apply across all repos, or project scope
(no flag) to override per-repo.

Whenever the Stop hook fires on a worktree with **uncommitted** changes, it also
captures that work non-destructively first: tracked modifications and untracked
files are snapshotted (via a temporary index — the working tree and real index
are untouched) into a **git bundle** under `<git-common-dir>/worktree-warden/wip/`.
Bundles live outside all refs (invisible to `git log`/`git branch`), so if the
worktree directory is later removed by something the destruction gate can't see
(e.g. the harness's own background-isolation cleanup, or a raw `rm -rf` —
see the destruction gate's documented `rm -rf` gap above), the WIP is still
recoverable. `recover` lists the bundles; `recover --gc-days N` expires old ones.

If the repo uses submodules, restoring a bundle (`git fetch <bundle> <ref>`)
brings back the gitlink pointer correctly, but leaves submodule directories
empty — run `git submodule update --init --recursive` after restore to
populate their content. Irrelevant for submodule-free repos.

**Lock lifecycle.** Every linked worktree is `git worktree lock`ed the moment
it is created (the PostToolUse `EnterWorktree` lock hook, above) and stays
locked for its entire life. `worktree_engine teardown` is the only path that
ever unlocks a worktree, and it does so immediately before its own `git
worktree remove` call — so a worktree is unlocked for, at most, the span of
one teardown invocation. The Bash destruction gate enforces this: a manual
`git worktree unlock`, `remove -f -f`, or `rm` of a worktree's lock file is
refused. This is what makes `recover` reliable even when a
worktree's directory disappears while it is still locked: git never marks a
locked entry `prunable`, and its admin record never drops out of `git
worktree list`, so `recover`'s stranded-worktree detector and the
population-tracking external-removal advisory both check the worktree's
directory directly on disk (`os.path.isdir`) rather than relying on git's
`prunable` annotation alone.

## Concurrency lock (serialize merges / main edits)

Git has no "lock this branch for a span of work" primitive — only transient single-operation
locks (`index.lock`, `refs/heads/<b>.lock`). worktree-warden adds a cooperative advisory lock
for the **multi-step** operations git leaves unguarded, because running many concurrent
sessions/agents otherwise lets two merges (or a merge and a direct main edit) interleave on
the default branch — and the merge is itself many separate engine processes, not one.

- **One key per worktree.** A lock is keyed by `realpath(worktree-toplevel)`. The main
  checkout is itself a worktree, so its toplevel is the shared "main-target" key: two
  `/merge-worktrees` (or `/finish-worktree`) runs, and a merge racing a direct main edit, all
  contend on it; work in *different* worktrees never contends.
- **Owner = session id** (`$CLAUDE_CODE_SESSION_ID`), so a lock is re-entrant for its holder.
- **Crash-safe staleness.** The `flock` is held only for each tiny check-and-set, never across
  the merge — a force-killed holder leaves no flock behind, only a record whose sliding lease
  lapses after a generous window. The deterministic escape is one command (also shown at
  SessionStart when a lock looks stale):

  ```bash
  worktree_lock status --repo <path>          # who holds what, and whether it's stale
  worktree_lock force-unlock --repo <path>    # clear a stale lock from a dead session
  ```

- **Fail-open and decoupled.** Any lock-subsystem error proceeds *without* a lock. The
  edit/destruction gates do **not** depend on this module, so a lock bug can never disable
  enforcement; a broken lock module is surfaced loudly at SessionStart instead.
- **What uses it:** `/merge-worktrees` acquires the main-target lock for the whole land and
  releases it at every terminal exit (the engine renews the lease per subcommand); `/bumpall`
  takes it per repo and reports `SKIPPED (locked)` rather than racing another session.
- **Per-worktree occupancy** (on by default; `occupancy_lock: false` to disable). While a
  session edits a worktree it claims it (refreshed on each `Edit`/`Write`); a **different**
  session that tries to edit the *same* worktree is blocked with the holder's id and the
  `force-unlock` command. A session's own subagents share its session id, so they never block
  each other — only genuinely separate sessions contend. The claim is layered only on the
  gate's allow path (a gate-blocked edit never claims a worktree), has no explicit release (it
  lapses with the lease; SessionStart prunes abandoned claims), and is best-effort: a broken
  lock module disables *only* occupancy, never the edit gate. *(git-write Bash serialization is
  intentionally not gated — git's own index/ref locks already prevent that corruption.)*

This is distinct from native `git worktree lock` (an anti-prune marker for removable media).

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
