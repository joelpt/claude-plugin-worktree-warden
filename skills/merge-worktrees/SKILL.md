---
name: merge-worktrees
description: Rebase & merge worktrees to main
allowed-tools: Bash(python3 *) Bash(git *) Skill(commit-commands:commitall) Skill(tao:thinkdeep) Skill(tao:chat) Skill(think) Skill(tao:consensus) Skill(tao:vet) Skill(tao:synthesize)
---

# /merge-worktrees

Lands linked worktrees into the repo's **default branch** (`main` unless the repo's
`origin/HEAD` says otherwise) from the **primary checkout**, by **rebase + ff-merge** —
linear history, no merge commits. Every deterministic step is a `worktree_engine.py`
subcommand; you only fill the judgement gaps (conflict resolution, ordering, test-failure
decisions). Repo-scoped: only ever this repo's worktrees.

> **Single clean worktree?** Prefer the one-shot `python3 $ENGINE --repo $REPO finish
> --worktree <path> --branch <branch> --target $TARGET --test-cmd "<just test|…>"` — it does
> lock → snapshot → land → test → teardown → release in one call (the `/finish-worktree`
> path). Drop to the granular steps below when it returns a conflict (`13`) or test failure
> (`18`), or for **multiple** worktrees / deliberate land ordering — the cases that need your
> judgement.

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`,
`GATE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py`,
`LOCK=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_lock.py`, `REPO=<primary checkout path>`,
`TARGET` and cleanliness resolved by **preflight** (see below). Engine exit codes: `0` ok
· `10` n/a (already merged / on target) · `11` worktree dirty · `12` primary unsafe
· `13` rebase conflict (LEFT IN PROGRESS) · `14` ff-merge failed · `15` git error
· `16` lock blocked · `17` core.bare corruption · `18` tests failed · `19` lease lost.

> **Lost-lease abort (exit `19`) — a global rule that overrides per-step handling.** Every
> mutating engine step below is passed `--require-lease`, so it ABORTS *before any mutation*
> if this session's step-0 lock was reclaimed or force-unlocked mid-merge (nothing changed on
> that step). **Any `19` → STOP immediately: do not retry the step, do not run further steps.**
> Surface it to the user with the `message` verbatim and follow the recovery action the
> `message` states. Then read `details.holder` — but treat it as a **point-in-time snapshot,
> not a standing fact**: it was true the instant the abort read the store, and a second session
> can reclaim (or release) the key immediately after. If it names another session, that session
> may be writing `$TARGET` right now — do **not** `undo`; report and let the user coordinate. If
> `holder` is `null`, no live session held the lock *at abort time*: if a snapshot was already
> taken (the abort was at land / rebase-continue / teardown), recovery is `undo` of the held
> snapshot (step 6's command; `undo` is intentionally NOT gated by the lease, so it always runs)
> — but because the advisory can go stale, **re-confirm immediately before undoing** that no
> session has reclaimed the key (`python3 $LOCK status --repo $REPO`); if one now holds it, stop
> and let the user coordinate instead of racing `undo` against a live writer. If the abort was
> at the **snapshot** step itself, nothing landed and there is nothing to undo — just stop and
> re-acquire. `19` is the only code that can interrupt the granular steps out of band; treat it
> as a hard halt, not a per-step branch.

**Safety contract:**

- **No blanket per-merge approval.** The 99% case (clean, chronological) just lands. HITL is
  reached only via the confidence-gated escalation below.
- **Rollback uses scoped `git reset --hard`** via the engine `undo` — AUTHORIZED here only,
  and only safe because we commit everything and snapshot *after* the last commit (no
  uncommitted tracked work exists; `--hard` never touches untracked files). Mid-rebase
  conflicts use the engine's own abort, never a reset.
- **Serialized to one merge at a time.** Step 0 takes a main-target lock so a second merge —
  or a session editing `main` directly — cannot interleave on `$TARGET`. It must be released
  at every terminal exit (see step 0's release discipline).
- Respect the active project's CLAUDE.md (e.g. TACO SSH-approval gates).

## Inputs

Chosen worktrees as `path` + `branch` pairs (from `/check-worktrees`), **or** a single
`--worktree <path>` with `--repo <primary>` (from `/finish-worktree`). If invoked bare, run
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json` and do the
`/check-worktrees` selection first.

**Establish `REPO` (primary checkout path):**

- If `--repo <path>` was passed explicitly (from `/finish-worktree`): use it as `REPO` and
  skip the session-cwd check — the session may legitimately still be inside the worktree.
- Otherwise: confirm session cwd is the primary checkout
  (`git -C <cwd> rev-parse --git-dir` → `.git`). If inside a linked worktree, call
  `ExitWorktree(action:"keep")` first. If ExitWorktree returns
  "No-op: there is no active EnterWorktree session", fall back to `cd $REPO` via Bash
  (use `git worktree list` first entry as `REPO`) — this moves the shell cwd to the
  primary checkout so subsequent Bash calls are rooted there; all engine calls take
  `--repo` explicitly for belt-and-suspenders correctness.

**Once `REPO` is known, run preflight immediately** (replaces the separate `symbolic-ref`
call and per-worktree `git status` checks with one tool call):

```bash
python3 $ENGINE --repo $REPO preflight --branches <b1,b2,…>
```

Read `details.target` → `TARGET`. Read `details.worktrees` → `[{branch, path, clean, dirty_files}]`.
This single call gives you everything steps 0–1 need; proceed directly to step 0.

## 0. Acquire the merge lock (serialize against other merges / main edits)

Before any commit, snapshot, or land, take the main-target lock so a second merge — or a
session editing `main` directly — cannot interleave on `$TARGET`:

```bash
python3 $LOCK --repo $REPO acquire-main "merge-worktrees: landing <b1,b2,…> into $TARGET"
```

- **exit 0 (`LOCK ACQUIRED`/`REFRESHED`)** → proceed to step 1. The lock is yours; every
  engine call below renews its lease automatically.
- **exit 1 (`LOCK BLOCKED`)** → another session holds it. **Pause** and `AskUserQuestion`,
  surfacing the holder + how long ago it was active (from the command output): options are
  *wait and retry*, or *force-unlock* only if you are certain that session is dead
  (`python3 $LOCK --repo $REPO force-unlock`). Never force-unlock unprompted.
- **`⚠️ … proceeding WITHOUT a lock`** (fail-open: no session id, not a repo) → the lock
  subsystem stepped aside; continue, but the cross-session guard is off for this run.
- **A Python traceback / any output that is not one of the above** → the lock module itself
  is broken. Treat it as fail-open: proceed WITHOUT the lock and note it; never read a crash
  as `LOCK BLOCKED`.

**Release discipline — the lock MUST be released at every terminal exit of this skill:**
after successful teardown (step 7), after an `undo`+abandon (step 6 b.3/b.4), after the
3-failed-rounds post-mortem (step 6), and on any hard-stop refusal you report (step 5 codes
`11`/`12`/`14`/`15`/`17`). The command is always:

```bash
python3 $LOCK --repo $REPO release-main
```

A forgotten lock self-expires after its lease and SessionStart surfaces it with the
force-unlock command — but release explicitly; don't lean on the backstop.

## 1. Commit dirty worktrees (one at a time)

For each entry in `details.worktrees` where `clean == false`: show `dirty_files` from
preflight and `git -C <path> diff HEAD --stat`; `AskUserQuestion` "commit these in `<branch>`?".

- **Yes** → `EnterWorktree(path:<path>)` → `/commit-commands:commitall` (fix review /
  pre-commit issues as normal) → `ExitWorktree(action:"keep")`.
- **No** → drop from the set; note it. Repeat until all remaining are clean.

## 2. Snapshot the restore anchors

After all commits, before any rebase:
`python3 $ENGINE --repo $REPO snapshot --target $TARGET --branches <b1,b2,…> --require-lease`
Save `details.snapshot_file` (the engine writes the target tip, each branch tip, and each
branch's worktree path to it) — this is what `undo` reads to rebuild the pre-land branch/
target tips **and recreate any torn-down worktrees**. Because step 1 already committed every
worktree's content, the snapshot SHAs capture it all. **Re-run snapshot** if step 1 produced
further commits.

## 3. Determine land order

Order by: dir mtime (chronological; older first is the safe default), project state / recent
`$TARGET` commits, and each worktree's commit content. Order matters under rebase — later
worktrees rebase onto earlier ones. Confidence-gated:

- low/medium → **advisor**; still low/medium → `/tao:thinkdeep` + `/tao:chat` + `/think` +
  `/tao:consensus` + `/tao:vet` + `/tao:synthesize`; still low/medium → **pause**,
  `AskUserQuestion` explaining the conundrum in plain terms (summarize; no internal
  step/option names), recommend a path with rationale. The clear case skips all of this.

## 4. Be in the primary checkout before landing

The step-0 main-target lock already guarantees no other session is merging to `$TARGET` or
editing `main` while this runs, so the old per-worktree `claude agents --json` re-check is
gone (it was unreliable anyway — `claude agents` lists only background sessions). All that
remains is to ensure the session cwd is the primary checkout: if still inside a linked
worktree, `ExitWorktree(action:"keep")`, or `cd $REPO` via Bash if that is a no-op. Engine
calls pass `--repo $REPO` explicitly regardless.

## 5. Land in order

For each branch in order:
`python3 $ENGINE --repo $REPO land --worktree <path> --branch <branch> --target $TARGET --require-lease`

- `0` → landed; next worktree (each subsequent rebases onto the now-advanced `$TARGET`).
- `13` (conflict, rebase LEFT IN PROGRESS) → resolving edits worktree files while this
  session sits in the primary checkout, which this plugin's own PreToolUse gate blocks, so
  first open a scoped exception:
  `python3 $GATE grant "worktree-warden/merge-worktrees — resolve rebase conflict for <branch>"`. Then resolve
  the listed `details.conflicts` in the worktree. High-confidence resolution → just do it.
  Low/medium (e.g. `foo(argA)`+`foo(argB)` → `foo(argA,argB)` + a combined test) → run the
  **step-3 escalation ladder**. Then `git -C <path> add <files>` and
  `python3 $ENGINE --repo $REPO rebase-continue --worktree <path> --branch <branch> --target $TARGET --require-lease`;
  loop on repeated `13`. When the branch lands (`0`) or you stop, close it:
  `python3 $GATE finished`.
- `10` → already merged; skip to teardown for it. `11`/`12`/`14`/`15`/`17` → stop, report
  `message` verbatim (these are preflight/safety refusals, not things to force), **then
  release the lock** (`python3 $LOCK --repo $REPO release-main`).

## 6. Verify + test (ALWAYS, after all lands)

Run both git checks in a single call, then the test suite:

```bash
git -C $REPO log --oneline -n 20 ; echo '---' ; git -C $REPO status --porcelain
```

Expect: clean status + expected commits visible. Then run the suite: Justfile `test`
(`just test`) → `npm test` (if `package.json`) → `pytest` (if Python) → `cargo test`
(if Cargo) → else ask.

- **Pass** → step 7.
- **Verify wrong, or unexpected test failures:**
  - trivial + clear + high-confidence fix → fix it, re-verify.
  - else → **roll back**:
    `python3 $ENGINE --repo $REPO undo --snapshot <snap.snapshot_file>` (restores target +
    branch tips and recreates any torn-down worktree on its branch)
    then advisor + escalation, and `AskUserQuestion` with four paths: **b.1** apply suggested
    fixes (rationale + confidence) and retry from step 5; **b.2** undo + retry the
    escalate-land loop (max **3 rounds**, then post-mortem); **b.3** abandon — leave undone
    (original state); **b.4** abandon but leave the current (landed) state, and hand the user
    the exact roll-back-later command — `python3 $ENGINE --repo $REPO undo --snapshot
    <snap.snapshot_file>` (the snapshot file persists under the git dir, so undo can recreate
    the torn-down worktrees even after step 7). After 3 failed rounds: stop + post-mortem
    (what was tried / what happened / best explanation / next steps).
    **On abandon (b.3/b.4), release the merge lock** (`python3 $LOCK --repo $REPO release-main`)
    once you finish the undo/handoff.

## 7. Teardown (only on green: verify clean AND tests pass)

For each successfully landed worktree:
`python3 $ENGINE --repo $REPO teardown --branch <branch> --target $TARGET --require-lease`
`0` = pruned/no-op; non-zero = refused (report verbatim, never force).

Then **release the merge lock** — the operation is done:

```bash
python3 $LOCK --repo $REPO release-main
```

## 8. Summary

Which worktrees landed and in what order, any dropped/skipped + why, conflicts resolved,
test result, what was pruned, and the final `$TARGET` state.
