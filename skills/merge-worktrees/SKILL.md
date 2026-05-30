---
name: merge-worktrees
description: Land a chosen set of the current repo's linked git worktrees into the default branch by rebase + fast-forward (never a merge commit), in a determined order, with confidence-gated conflict handling, post-land tests, exact-state rollback on failure, and scripted teardown. Use on /merge-worktrees or after /check-worktrees selects worktrees. Repo-scoped — never cross-repo.
allowed-tools: Bash(python3 *) Bash(git *) Skill(commit-commands:commitall) Skill(tao:thinkdeep) Skill(tao:chat) Skill(think) Skill(tao:consensus) Skill(tao:vet) Skill(tao:synthesize)
---

# /merge-worktrees

Lands linked worktrees into the repo's **default branch** (`main` unless the repo's
`origin/HEAD` says otherwise) from the **primary checkout**, by **rebase + ff-merge** —
linear history, no merge commits. Every deterministic step is a `worktree_engine.py`
subcommand; you only fill the judgement gaps (conflict resolution, ordering, test-failure
decisions). Repo-scoped: only ever this repo's worktrees.

`ENGINE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py`,
`GATE=${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py`, `REPO=<primary checkout path>`,
`TARGET=<default branch>`. Engine exit codes: `0` ok · `10` n/a (already merged / on target)
· `11` worktree dirty · `12` primary unsafe · `13` rebase conflict (LEFT IN PROGRESS) ·
`14` ff-merge failed · `15` git error · `17` core.bare corruption.

**Safety contract:**

- **No blanket per-merge approval.** The 99% case (clean, chronological) just lands. HITL is
  reached only via the confidence-gated escalation below.
- **Rollback uses scoped `git reset --hard`** via the engine `undo` — AUTHORIZED here only,
  and only safe because we commit everything and snapshot *after* the last commit (no
  uncommitted tracked work exists; `--hard` never touches untracked files). Mid-rebase
  conflicts use the engine's own abort, never a reset.
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
  "No-op: there is no active EnterWorktree session", note it and use
  `git worktree list` first entry as `REPO` — all engine calls take `--repo` explicitly so
  session cwd doesn't affect correctness; teardown will leave the session cwd stale.

Resolve `TARGET` from `git -C $REPO symbolic-ref --quiet refs/remotes/origin/HEAD` (leaf),
else `main`.

## 1. Commit dirty worktrees (one at a time)

For each chosen worktree that is dirty: show `git -C <path> status` + `diff --stat`;
`AskUserQuestion` "commit these in `<branch>`?".

- **Yes** → `EnterWorktree(path:<path>)` → `/commit-commands:commitall` (fix review /
  pre-commit issues as normal) → `ExitWorktree(action:"keep")`.
- **No** → drop from the set; note it. Repeat until all remaining are clean.

## 2. Snapshot the restore anchors

After all commits, before any rebase:
`python3 $ENGINE --repo $REPO snapshot --target $TARGET --branches <b1,b2,…>`
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

## 4. Race re-check (per worktree, just before its land)

Re-run `claude agents --json`. If the only session inside this worktree is **this** session,
call `ExitWorktree(action:"keep")` and re-check (if ExitWorktree is a no-op — session
started directly in the worktree — skip the re-check; engine calls use `--repo $REPO`
explicitly so the session cwd inside the worktree doesn't block the land, and teardown will
leave the cwd stale). If **another** session occupies it → skip + pause + explain.

## 5. Land in order

For each branch in order:
`python3 $ENGINE --repo $REPO land --worktree <path> --branch <branch> --target $TARGET`

- `0` → landed; next worktree (each subsequent rebases onto the now-advanced `$TARGET`).
- `13` (conflict, rebase LEFT IN PROGRESS) → resolving edits worktree files while this
  session sits in the primary checkout, which this plugin's own PreToolUse gate blocks, so
  first open a scoped exception:
  `python3 $GATE grant "merge-worktrees: resolve rebase conflict for <branch>"`. Then resolve
  the listed `details.conflicts` in the worktree. High-confidence resolution → just do it.
  Low/medium (e.g. `foo(argA)`+`foo(argB)` → `foo(argA,argB)` + a combined test) → run the
  **step-3 escalation ladder**. Then `git -C <path> add <files>` and
  `python3 $ENGINE --repo $REPO rebase-continue --worktree <path> --branch <branch> --target $TARGET`;
  loop on repeated `13`. When the branch lands (`0`) or you stop, close it:
  `python3 $GATE finished`.
- `10` → already merged; skip to teardown for it. `11`/`12`/`14`/`15`/`17` → stop, report
  `message` verbatim (these are preflight/safety refusals, not things to force).

## 6. Verify + test (ALWAYS, after all lands)

`git -C $REPO log --oneline -n 20` and `git -C $REPO status --porcelain` (expect clean +
expected commits). Then run the suite: Justfile `test` (`just test`) → `npm test` (if
`package.json`) → `pytest` (if Python) → `cargo test` (if Cargo) → else ask.

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

## 7. Teardown (only on green: verify clean AND tests pass)

For each successfully landed worktree:
`python3 $ENGINE --repo $REPO teardown --branch <branch> --target $TARGET`
`0` = pruned/no-op; non-zero = refused (report verbatim, never force).

## 8. Summary

Which worktrees landed and in what order, any dropped/skipped + why, conflicts resolved,
test result, what was pruned, and the final `$TARGET` state.
