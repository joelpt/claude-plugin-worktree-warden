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

## Empirically-settled findings (evidence, not opinion)

- **Squash-merge teardown deadlock is real.** A multi-commit branch squash-merged into the target is a non-ancestor of it, and `git cherry` prints `+` for every commit, so *both* guard paths (destruction gate via `git cherry`, engine teardown via `--is-ancestor`) block teardown permanently. A single-commit branch squashes cleanly. Hence the escape hatch rather than a heuristic.
- **Import-time fail-open is silent total disablement.** PreToolUse blocks only on exit 2; Python's default exit 1 on `ImportError` is treated as allow, so a bad import turns the gate off everywhere with no signal.
- **Per-Bash latency is a non-issue.** Destructive git calls are gated behind cheap pure-string parsing; non-matching commands cost microseconds.

## Lessons / context

- The "vanished worktree" failure (see project memory `lost-worktrees-rca`) is external directory removal the audit log doesn't account for — `git worktree lock` at creation (M2) is the direct structural fix, keeping admin records alive for `cmd_recover`.
