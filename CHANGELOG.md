# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
