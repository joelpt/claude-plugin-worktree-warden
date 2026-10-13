# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed

- **Destruction gate refuses destructive verbs hidden behind constructs the parser cannot model.**
  A command whose text contains `git branch -d/-D`, `git worktree remove|unlock` or a `.git/worktrees` path, together with command or process substitution, heredocs, ANSI-C quoting, an unterminated quote, `( )`/`{ }`/`if`/`for` blocks, `eval`, `xargs`, `find -exec` or `sh -c`, is now refused (`unresolved_context`).
  This closes the previously documented `sh -c`/`xargs`/`eval`/substitution gap for those verbs; it is a text heuristic and can over-block.
  `builtin cd`/`command cd` are tracked like `cd`, wrapper flags (`env -i`) no longer hide the command, and git-redirecting env vars are detected anywhere before `git`.
  The text-only exemption is narrower: write-capable or shell-spawning commands (`tee`, `sort`, `uniq`, pagers) and any segment with a `>` redirect are no longer treated as benign, `git` is exempt only for data-carrying subcommands (`commit`, `log`, `grep`, ...), and a git invocation that defines an alias (`-c alias.x=...`, `--config-env`) is refused outright.
  A segment that looks destructive but whose command is not a literal `git` (a `$VAR` head, a wrapper's leftover flag value as in `sudo -u root git ...` or `nice -n 5 git ...`) is refused unless the command only prints or searches text (`echo`, `grep`, `cat`, ...).
  A gated target the shell would expand first (`$VAR`, `{a,b}`, globs, `~`) is refused instead of being looked up literally.
  Supersedes the "permanent gap" wording in the entry below for those shapes; `rm -rf <worktree>`, indirect scripts and `git -c alias.x=...` remain uncovered.
- **Destruction gate parser hardening (security review follow-up).**
  `git branch` delete detection now understands clustered short flags (`-fD`, `-rd`) and abbreviated long options (`--del`, `--forc`), and `-d` plus a force flag counts as forced.
  Operands after a bare `--` are gated targets, and separators inside quotes (including `$'...'` ANSI-C quoting) no longer split a command into segments.
  An unreadable worktree status now blocks `git branch -D` instead of being treated as clean.
  A destructive command whose repository or directory cannot be determined statically is refused: `GIT_DIR`/`--git-dir`/`--work-tree` overrides, and `cd $VAR`, `cd -`, `popd` or a subshell `cd` before a gated command.
  `pushd` and `cd ~` are now tracked like `cd`.
  `sh -c`, `xargs` and `eval` remain the documented, backstopped gap.
- **Destruction gate again blocks the `git worktree remove` / `unlock` lock bypasses.** #26 narrowed the
  gate to `git branch -d/-D` and left `git worktree lock` as the only protection, but git's own error for a
  locked worktree advertises `remove -f -f`, and `git worktree unlock X && git worktree remove --force X`
  (or `rm`-ing the lock file) passed the gate. The gate now judges command shape, independent of lock
  state: `git worktree unlock`, `rm` under `.git/worktrees/`, and `remove` with two or more force flags
  (`-ff`, `--forc --forc`, ...) are refused outright; a single-force `remove` is judged by the target
  worktree's path (dirty files, then un-landed commits on its HEAD, covering detached HEAD) and an
  unresolvable target is refused. `cd` in an earlier segment and `git -C` are honored. Supersedes the
  "covers only `git branch -d/-D`" entry below. `rm -rf <worktree dir>` stays a documented gap.

### Changed

- **Worktree admin records are now protected from garbage collection by native `git worktree lock` (#26).**
  Every linked worktree is locked immediately after creation (via a new `PostToolUse(EnterWorktree)` hook)
  and stays locked for its entire life until `worktree_engine teardown` unlocks it immediately before removal.
  This makes `recover` and `reconcile` reliable even when a worktree's directory is removed externally (e.g.
  `rm -rf`, stray cleanup, force-quit housekeeping) — git never marks a locked worktree `prunable` and never
  drops its admin record from `git worktree list`, so recovery works by checking `os.path.isdir` directly
  instead of relying on git's `prunable` annotation.
  
- **Destruction gate now covers only `git branch -d/-D`** (#26) — superseded; see the Fixed entry above. With native worktree locking in place,
  git itself refuses `git worktree remove [--force]` on a still-locked worktree at the git layer, so the
  parser no longer intercepts that case. `git branch -d/-D` (the only destructive command the parser can
  reliably intercept in shell syntax) is still gated by worktree cleanliness + landed status. `rm -rf`
  and any Bash-hidden destruction (via `bash -c`, `xargs`, etc.) remain known, uncompensated gaps,
  documented in README and backstopped only by WIP bundle capture and the `recover` audit.

### Added

- **`run_tests_on_finish` setting (default off).** Finish no longer auto-detects and runs
  the repo's tests unless `worktree_gate run-tests-on-finish on` (user or project scope)
  enables it; `finish-preflight` otherwise reports `--skip-tests` with source `disabled`,
  and `merge-worktrees` checks the same setting. The force-disable switch overrides an
  enabled setting. BEHAVIOR CHANGE: finish previously ran tests by default.

- **Hidden session-wide force-disable switch for an orchestrator that owns the worktree
  lifecycle.** With `WORKTREE_WARDEN_FORCE_DISABLE=1` in the environment, all four hooks
  (`auto_teardown_hook`, `check_worktrees_hook`, `enforce_worktree_hook`,
  `guard_destruction_hook`) exit 0 at once with no output and no side effects (no WIP
  bundle, lock, audit entry, debounce state or load-error sentinel). Only the exact value
  `1` counts, and the check runs before the hooks' guarded imports so it also holds on a
  broken install. Shared helper: `worktree_gate.force_disabled()`; each hook carries a
  stdlib-only copy. Motivation: the Stop hook's "run finish-worktree" nudge can send a
  pipeline worker to merge and remove its own worktree outside the orchestrator's land
  authority. A change to the `env` block of `.claude/settings.local.json` reaches later
  hooks of a running session (subagents and child `claude` processes included); a Bash
  `export` does not. By design the switch is documented only in README, PLAN.md and this
  file, and is never named in hook output, skill text or errors, so a session under warden
  cannot discover it. No behavior change when the variable is unset or not `1`.

- **Audit log rotation** (closes #33): Both audit logs now rotate at a 10MB size cap, retaining
  the 5 most recent rotated generations. When an audit log reaches 10MB, it becomes
  `audit.log.1`, existing numbered files shift up (e.g., `.1` → `.2`), and anything beyond
  `.5` is deleted. Rotation is **non-blocking and best-effort**: a single non-blocking advisory
  lock (`fcntl.LOCK_NB`) ensures only one concurrent writer performs the rotation; others skip it
  and proceed immediately to their append. The mechanism never raises exceptions and does not block
  any audit operation, preserving the never-raise contract that auditing must not disrupt work.
  Forensic reconstruction is deterministic: read rotated generations newest-first as
  `audit.log`, `audit.log.1`, `audit.log.2`, ..., `audit.log.5`. Applies to both the
  engine audit log (`<git-common-dir>/worktree-warden/audit.log`, logging land/teardown/undo/
  snapshot mutations) and the gate audit log (`~/.config/worktree-gate/audit.log`, logging every
  gated Edit/Write call). For details and rotation internals, see `rotate_audit_log()` in
  `scripts/worktree_gate.py`.

- **`teardown --discard`: a sanctioned path for abandoning a worktree whose work must NOT
  land.** `teardown` refused a dirty worktree (`11 dirty_worktree`) and refused an unmerged
  branch (`12 branch_unmerged`). Both are correct defaults, but together they made abandoning
  a branch impossible through the engine: committing the dirty state to clear the first guard
  merely trips the second, and the only remaining route was hand-rolled
  `git worktree remove --force` + `git branch -D` — precisely what this plugin's own skills
  forbid, and what the engine exists to prevent. Found in practice on a branch that had been
  deliberately *disproved* and therefore had to be discarded rather than merged. `--discard`
  is opt-in, **never** bypasses the path gate (the primary checkout stays untouchable), and is
  recoverable by construction: the branch tip is captured before deletion and dirty tracked
  state is salvaged into a dangling commit via `git stash create`, so both survive in the
  object store until gc. What it destroyed — `uncommitted`, `unmerged_commits`,
  `salvaged_stash`, `branch_tip` — is reported in `details` and the audit log, never silently.
  `finish-worktree`'s fallbacks now route exits `11` and `12` to it (with a user check first)
  instead of dead-ending, and `12` is split out of the report-and-stop catch-all.

- **Abort a merge on a lost lease (F-A Option B; closes the lease-lapse window).** The
  multi-process merge holds its main-target lease across many separate `worktree_engine.py`
  subprocesses; a pause that outlasts the lease — or a human `force-unlock` — can let a second
  session reclaim the key mid-merge. Option A (the long, per-subcommand-renewed lease) *narrowed*
  that window; Option B *closes* it. Mutating engine steps now accept `--require-lease`: when set
  and the per-subcommand `refresh` reports the lease `"lost"`, the step **aborts before any
  mutation** with the new exit code `19` (`EXIT_LEASE_LOST`), so a reclaimed key halts the merge
  instead of racing a second writer onto `main`. The abort reports `details.holder` — the live
  reclaiming session, or `null` after a force-unlock — so the orchestrator knows whether `undo`
  is safe or would collide with a live writer, and emits a `lease-lost` audit event. The flag is
  opt-in (only the orchestrator knows it holds a lease; a bare CLI `land` that never acquired one
  is unaffected — `refresh` alone cannot distinguish "lost my lease" from "never had one"), and
  `undo` deliberately has **no** such flag so recovery is never blockable by the lease-loss it
  recovers from. `/merge-worktrees` passes `--require-lease` on snapshot/land/rebase-continue/
  teardown and treats `19` as a global hard-halt.

- **`worktree_engine.py finish` — one-shot happy-path land.** Collapses the deterministic
  green path (acquire main-target lock → snapshot → rebase + ff-merge → run a `--test-cmd`
  gate → teardown → release lock) into a **single** command, so landing a clean worktree no
  longer means hand-chaining `worktree_lock.py` + `worktree_engine.py` across ~6 calls in the
  right order. It bails cleanly on anything non-trivial — a rebase conflict (13) or test
  failure (18) **preserves state and keeps the lock held** for the caller's judgement
  (`/merge-worktrees`'s ladder); dirty (11) / unsafe (12) / blocked (16) land nothing. It
  **never auto-rolls-back**. `/finish-worktree` now uses it for the single-worktree fast path,
  falling back to `/merge-worktrees` for conflicts, test failures, and multi-worktree ordering.

### Fixed

- **`worktree_lock.py` accepts `--repo`/`--owner` in either position.** They were defined only
  on the subcommands, so `--repo X acquire-main` (the order the merge skill documented, and the
  one matching `worktree_engine.py`'s top-level `--repo`) errored; only `acquire-main --repo X`
  worked. Both orders now work.

### Changed

- **Operation locks (merge/bumpall) now carry a long 120-min lease** (`MERGE_LEASE_SECONDS`,
  configurable via `merge_lease_seconds`), renewed at that length by both `acquire` and the
  engine's per-subcommand refresh — so a merge that pauses for conflict resolution, tests, or
  human review no longer lapses its lease mid-operation (which could let a second merge start).
  Occupancy keeps the shorter default lease. The guarantee is documented honestly: mutual
  exclusion holds only within a lease window; a pause longer than the window can lapse the lock,
  and `force-unlock` is the deterministic escape. (Fully closing the window — aborting a merge
  on a lost lease rather than renewing — is a tracked follow-up.)

### Added

- **Per-worktree occupancy lock** (concurrency lock Phase 2a): while a session edits a
  worktree it claims it (keyed by the worktree toplevel, owner = session id); a **different**
  session editing the same worktree is blocked with the holder's id and the `force-unlock`
  command. A session's own subagents share its session id (POC-verified: a subagent's
  PreToolUse payload carries the parent `session_id` with a distinct `agent_id`), so they never
  block each other — only genuinely separate sessions contend. Layered only on the edit gate's
  allow path (a gate-blocked edit never claims a worktree); enabled by default, switchable via
  `occupancy_lock`. No explicit release — the claim lapses with the sliding lease and
  SessionStart prunes abandoned claims. Best-effort and **decoupled**: the edit gate imports
  the lock module lazily inside the occupancy check, so a broken lock module disables only
  occupancy, never enforcement. git-write Bash serialization is deliberately not gated (git's
  index/ref locks already prevent that corruption).
- **Cooperative concurrency lock** (`scripts/worktree_lock.py`): serializes the multi-step
  operations git can't lock across sessions — above all the multi-process merge. One advisory
  lock per `realpath(worktree-toplevel)` in `<git-common-dir>/worktree-warden/locks.json`,
  owned by the session id, with every check-and-set guarded by a short-lived `flock`. The main
  checkout's toplevel is the shared "main-target" key, so two `/merge-worktrees` runs — or a
  merge racing a direct main edit — contend, while different worktrees never do.
  - `/merge-worktrees` (and `/finish-worktree` via it) acquires the lock for the whole land and
    releases it at every terminal exit; the engine renews the lease on each mutating subcommand.
    This replaces the unreliable `claude agents --json` race re-check (which sees only
    background sessions).
  - `/bumpall` takes the lock per repo and reports `SKIPPED (locked)` instead of racing.
  - Crash-safe staleness: a force-killed holder leaves no flock, only a record whose sliding
    lease lapses; `worktree_lock force-unlock` is the one-step human escape, surfaced at
    SessionStart alongside active/stale locks.
  - Fail-open and **decoupled** from the safety gates: the edit/destruction hooks do not import
    the lock module, so a lock bug can never disable enforcement; a broken lock module is
    surfaced loudly at SessionStart instead. Distinct from native `git worktree lock`.
  - Per-worktree *occupancy* locking on Edit/Write (and git-write Bash) is designed but
    deferred (Phase 2), pending confirmation that out-of-process subagents don't share a
    worktree under distinct session ids.

### Fixed

- **Gate load failure now fails "open loudly"** (#19): Both PreToolUse hooks (edit gate and
  destruction gate) now wrap their module imports in try/except.
  If either `worktree_gate` or `worktree_destruction` fails to import (due to syntax error,
  broken dependency, or installation issue), the hook writes a diagnostic to stderr, drops
  a sentinel file at `<git-common-dir>/worktree-warden/gate-load-error` (including a full
  traceback for debugging), and exits 0 (documented fail-open).
  The SessionStart hook detects the sentinel and surfaces a high-priority advisory even in
  `"never"` display mode, ensuring a deployment error is never silent.
  When the import error is fixed, the next session start self-heals by removing the sentinel
  once both gate modules load cleanly.
  Previously, a module import failure would exit 1 (Python's default), treated as "allow" by
  Claude Code, silently disabling the gate everywhere with no signal.

---

For older releases and detailed history, see the git commit log or
[release tags](https://github.com/joelpt/claude-plugin-worktree-warden/releases).
