# worktree-warden — PLAN

> **Live status & DAG: tracking issue [#7](https://github.com/joelpt/claude-plugin-worktree-warden/issues/7)**

This is the design/rationale SSOT.
Live status, the work DAG, the decision log, and open questions live in the tracking issue — never duplicated here.

## Purpose

worktree-warden enforces worktree-first editing for AI coding agents: a `PreToolUse` gate blocks edits to a repo's main checkout (via exit code 2, before the permission layer, so it holds under `bypassPermissions`), a destruction gate refuses to throw away dirty or un-landed worktrees, uncommitted WIP is captured to recoverable git bundles, and chosen worktrees are landed into the default branch by deterministic rebase + fast-forward with exact-state rollback.

## This effort

The current roadmap is a hardening pass driven by a guru-roundtable code review (Linus Torvalds, Guido van Rossum, Anders Hejlsberg, John Carmack, and Git maintainer Junio Hamano), triaged against the source and — where the experts disagreed — settled empirically.

The verdict that frames every milestone: **the philosophy and the core mechanism are right.** Worktree-first is the correct discipline for an agent that edits fast and confidently, and exit-code-2-before-the-permission-layer is the only mechanism that enforces it under bypass. The work below is hardening on a sound design, not a redesign.

## Design tenets (carried forward, not up for re-litigation)

- **Exit code 2 is load-bearing.** It fires before the permission layer; the JSON `permissionDecision` route is skipped under `bypassPermissions`. Blocking must stay on exit 2. Structured JSON is added *alongside* it (via `additionalContext`), never as a replacement.
- **Fail open — but at the right granularity.** A *runtime* error on a single invocation should allow the edit (a buggy gate must never brick editing). A *deployment* error (import/syntax failure at module load) is different: it disables the gate everywhere and must fail *loudly* with a diagnostic, not silently. These two are currently conflated.
- **The destruction gate is a speedbump for naive agent commands, not a security boundary.** It cannot see `bash -c`, `xargs`, command substitution, or scripts — by design. Native `git worktree lock` plus the WIP-bundle/audit backstops are the real safety net. The README must say so.
- **Deterministic git in the engine; judgment only in the skill.** The engine is pure, testable, and exact-state-reversible; the skill fills only the judgement gaps (land order, confidence-gated conflict HITL).
- **Repo-scoped, always.** The plugin only ever inspects and acts on worktrees of the repo the current session belongs to.
- **Concurrency is serialized cooperatively, not by git.** Git has no branch-span lock; multi-step operations (above all the multi-process merge) are serialized by a cooperative advisory lock keyed by `realpath(worktree-toplevel)`. The lock is a *coordination aid, not a safety gate*: it fails open and is deliberately **decoupled** from the edit/destruction gates' import path, so a lock bug can never disable enforcement (the opposite would expand the gate's blast radius).

## Milestone roadmap (design intent)

Sequence is M1 → M2 → M3 → M4; each milestone depends on the prior. Exit criteria below; live completion status is in the tracking issue.

### M1 — Correctness & data-loss bugs

The ship-blockers. Silent enforcement disablement and data-loss windows in undo, teardown, and land.

- **Exit criteria:** a module-load failure can no longer silently disable the gate; `cmd_undo` cannot destroy commits made to primary after a failed land; teardown cannot silently discard detached-HEAD or mid-rebase work; a concurrent target-advance no longer throws away a good rebase; squash/cherry-pick landings have an audited teardown escape hatch.
- **Epics:** Gate enforcement reliability; Land and undo engine correctness; Teardown safety.

### M2 — Enforcement coverage & resilience

Close the open-enumeration coverage gaps and add independent resilience.

- **Depends on:** M1 (stabilize enforcement before broadening it).
- **Exit criteria:** the tool matcher blocks unknown/future mutation tools by default (allowlist inversion); worktrees are locked at creation so `cmd_recover` survives external removal; the destruction parser is collapsed to its real threat model; remote-only-landed branches are recognized; failure classes are stratified and an independent SessionEnd witness exists.
- **Epics:** Enforcement coverage and destruction-gate hardening; Failure observability and resilience.

### M3 — Workflow & developer experience

Shift enforcement left so the gate becomes a workflow enabler, not a per-edit friction tax.

- **Depends on:** M2.
- **Exit criteria:** SessionStart offers worktree create/resume on a main checkout so normal work never hits the gate; the engine→skill interface emits structured JSON; mid-rebase worktrees are surfaced at SessionStart.
- **Epics:** Workflow and developer experience.

### M4 — Polish & documentation

Low-risk hardening one-liners, honest scoping, and operational hygiene.

- **Depends on:** M3.
- **Exit criteria:** README honestly scopes the destruction gate; remaining one-line correctness/hardening fixes land; operational and doc hygiene items are closed.
- **Epics:** Polish and documentation.

## Concurrency serialization (cooperative advisory lock)

Distinct from native `git worktree lock` (an anti-prune marker — see M2 / lost-worktrees):
this is cross-session **operation** serialization, added because the user runs many concurrent
sessions/agents and git only locks single index/ref updates, not multi-step operations.

- **Model:** one lock per `realpath(worktree-toplevel)` in `<git-common-dir>/worktree-warden/locks.json`; owner = session id; every check-and-set guarded by a short-lived `flock` on a sibling mutex (held only for the read-modify-write, **never** across the merge). The main checkout is itself a worktree, so its toplevel unifies "two merges", "merge vs. direct main edit", and (Phase 2) "two sessions in one worktree" into one key; different worktrees use different keys and never contend.
- **Staleness without a liveness signal:** a force-killed holder leaves no flock behind (only a record), so the store never wedges; the record's sliding lease lapses after a generous window, and a one-step `force-unlock` (surfaced at SessionStart) is the deterministic human escape. No pid/heartbeat games — a paused session is indistinguishable from a dead one to a short-lived hook.
- **Fail-open + decoupled:** any lock-subsystem error proceeds *without* a lock. For Phase 1 the edit/destruction gates do not import the lock module at all; for Phase 2 the edit gate imports it **lazily, inside the occupancy check, guarded** — so a broken lock module disables *only* occupancy, never the core gate. A broken lock module is surfaced loudly at SessionStart yet never drops the gate-load sentinel.
- **Phase 1 (done):** main-target serialization — `/merge-worktrees` (and `/finish-worktree` via it) acquire/refresh/release; the engine renews the lease on each mutating subcommand; `/bumpall` takes the lock per repo (skip-on-blocked); SessionStart surfaces active/stale locks. Replaces the unreliable `claude agents --json` race re-check in `/merge-worktrees` step 4.
- **Phase 2a (done):** per-worktree *occupancy* locking on Edit/Write, behind `occupancy_lock` (default on). **Linked worktrees only** — a *main-checkout* edit needs no claim: a session making *live* (uncommitted) changes to main already blocks a concurrent merge via the engine's dirty-primary refusal (`EXIT_PRIMARY_UNSAFE`), and committed main edits are just history a merge rebases onto; keeping occupancy off the primary key also means it can never collide with a `merge`/`bumpall` lock there and never leaves a stale main-key claim. Layered **only on the gate's allow path** (a gate-blocked edit never claims a worktree), and only when the edit target is a file *inside* the worktree (not a cross-repo or `.git`-internal edit). Owner = session id; **POC-verified** that a subagent's PreToolUse payload carries the *parent* `session_id` (with a distinct `agent_id`) while separate top-level sessions carry distinct ids — so a session's own subagents never false-block it, but two genuinely separate sessions in one worktree do contend (exactly the requested semantics). `decide_lock` refresh preserves a record's original kind (cheap insurance against any future re-entrant relabel). No explicit release: a session holds a worktree via its sliding lease; SessionStart prunes lapsed occupancy claims and does not surface live ones (steady-state noise).
- **Phase 2b (deferred):** git-write Bash occupancy (commit/merge/rebase/push/reset). Git already prevents the *corruption* it would target via index/ref locks, so it is the lowest-value / highest-false-positive slice — left as a noted follow-up.

### Deadlock freedom (proof)

**Lock inventory.** (1) A per-repo `flock` on `<git-common-dir>/worktree-warden/locks.lock` — the *only* blocking primitive (`LOCK_EX`, no `LOCK_NB`). (2) Advisory lease records in `locks.json` (kinds: merge / bumpall / occupancy) — acquired **non-blocking** (fail-fast). (3) The grant/exception record — a plain atomic file, no blocking wait.

Deadlock needs Coffman's *hold-and-wait* + *circular-wait*. We show no process ever holds any lock while waiting to acquire another, so no wait-for cycle can form.

- **Lemma 1 — the flock is a non-nested leaf.** Every `with _flock(...)` body does only local file I/O (`read_store`, the pure `decide_lock`, `_write_store`) and acquires no other lock. The five flock call sites (`acquire`, `refresh`, `release`, `force_unlock`, `prune_stale_occupancy`) call no other flock-taking function (`read_store` / `_write_store` / `session_advisory` take no flock). So a process holding the flock never waits — it runs a bounded critical section and releases. This also precludes self-deadlock (no path re-acquires the flock while holding it).
- **Lemma 2 — lease acquisition never waits.** `decide_lock` returns `"blocked"` immediately on contention; `acquire`/`refresh`/`release`/`force_unlock`/`prune` never block on a lease. No process holds anything while waiting for a lease.
- **Lemma 3 — at most one flock at a time, held momentarily.** Each hook/CLI invocation touches exactly one repo's store under one flock. `/bumpall` does acquire→release per repo *sequentially* (never two at once). A merge holds the main-target *lease* (a persisted record, not a held flock) across its life; each engine subcommand takes its own brief flock to refresh, then releases. No process holds flock-A while acquiring flock-B.

**Conclusion.** The only blocking primitive is a non-nested, bounded leaf (L1, L3); every advisory acquire is non-blocking (L2); the grant record has no blocking wait. Hold-and-wait is structurally absent ⇒ no circular wait ⇒ **deadlock is impossible.** Worst case under contention is brief serialization on one repo's flock plus a fail-fast `blocked` on a contended lease — a liveness cost, never a deadlock. This holds across every combination (merge×merge, merge×bumpall, occupancy×occupancy same/different worktree, same-session multi-key, N-way flock contention, cross-repo bumpall): each reduces to a non-blocking lease contend or a bounded-leaf flock serialize.

### Lock follow-ups (post-2a backlog)

A 5-luminary review (Torvalds, van Rossum, Hejlsberg, Carmack, Lamport) + a tournament-reasoning pass adjudicated what blocks the 2a land vs. follows up. Discriminant: *does the 2a diff itself introduce/worsen the hazard?* Two land-blockers were applied (both surfaces 2a newly made load-bearing): `acquire()` now **raises on an unknown `kind`** (`kind` drives prune/surface), and the occupancy hook's fail-open exception is now **logged** (`occupancy-error`), matching the plugin's fail-open-*loudly* ethos. The rest, prioritized (safety > correctness > ergonomics > perf):

- **F-A (correctness, Phase-1, highest):** the multi-process merge holds a *lease* that can lapse during a long HITL/test pause; the engine's per-subcommand `refresh()` returns `"lost"` but currently logs-and-continues, so a 2nd session could `acquire-main` and two merges race to `main`. Fix = **A now** (generous merge lease 90–120 min + force-unlock + document the guarantee as "holds only within a lease window") **+ B as a TDD'd follow-up** (engine aborts on a `"lost"` refresh; requires the mid-merge undo path read+tested first) → converge on A+B. Pre-existing on `main`; **orthogonal to occupancy** (primary-key, decoupled).
- **F-B (safety, structural):** promote `kind: str` → a `LockKind` enum carrying a `prunable` attribute, so "occupancy is the only prunable kind" is enforced by construction, not by two scattered string compares. Supersedes the BLOCK-1 `raise`.
- **F-C (ergonomics):** show lease-expiry ("expires in ~Nm") in both block messages; map `kind` → user prose ("occupancy"→"editing", "merge"→"merging").
- **F-D (correctness/ergonomics):** separate lease windows — occupancy short (~10 min) vs. merge long (F-A). Low urgency (force-unlock already prevents wedging).
- **F-E (perf):** collapse the redundant per-Edit config reads (`occupancy_enabled` + `read_lease_seconds` re-read the same two JSON files). Lowest priority — dwarfed by the human edit itself.
- **F-F (rejected as primary):** moving the occupancy claim from per-Edit to `EnterWorktree` — would miss background / in-worktree-start sessions that never call it (the user's actual workload). A lockless fast-path + a usage metric is an optional perf/observability follow-up; the claim stays per-Edit.

## Empirically-settled findings (evidence, not opinion)

- **Squash-merge teardown deadlock is real.** A multi-commit branch squash-merged into the target is a non-ancestor of it, and `git cherry` prints `+` for every commit, so *both* guard paths (destruction gate via `git cherry`, engine teardown via `--is-ancestor`) block teardown permanently. A single-commit branch squashes cleanly. Hence the escape hatch rather than a heuristic.
- **Import-time fail-open is silent total disablement.** PreToolUse blocks only on exit 2; Python's default exit 1 on `ImportError` is treated as allow, so a bad import turns the gate off everywhere with no signal. *Mitigated (#19): each PreToolUse hook wraps its gate-module import in try/except and, on failure, writes a stderr diagnostic plus a durable sentinel at `<git-common-dir>/worktree-warden/gate-load-error` before deliberately exiting 0; the SessionStart banner surfaces that sentinel and self-heals it once the module imports cleanly again.*
- **Per-Bash latency is a non-issue.** Destructive git calls are gated behind cheap pure-string parsing; non-matching commands cost microseconds.

## Lessons / context

- The "vanished worktree" failure (see project memory `lost-worktrees-rca`) is external directory removal the audit log doesn't account for — `git worktree lock` at creation (M2) is the direct structural fix, keeping admin records alive for `cmd_recover`.
