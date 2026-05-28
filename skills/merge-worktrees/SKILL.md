---
name: merge-worktrees
description: Merge a chosen set of the current repo's linked git worktrees into the default branch, in a determined order, with confidence-gated conflict handling, verification, tests, and teardown. Use on /merge-worktrees or after /check-worktrees selects worktrees to land. Repo-scoped — never cross-repo.
allowed-tools: Bash(git *) Bash(python3 *) Skill(commit-commands:commitall) Skill(tao:thinkdeep) Skill(tao:chat) Skill(think) Skill(tao:consensus) Skill(tao:vet) Skill(tao:synthesize)
---

# /merge-worktrees

Lands a chosen set of linked worktrees into the repo's default branch (`main`
unless the repo says otherwise), from the **primary checkout**. Fully
self-contained git — no dependency on other merge skills. Operates ONLY on the
current repo.

**Safety contract (read first):**
- **No blanket per-merge approval.** The 99% case (clean, chronological,
  non-conflicting) just merges. Human-in-the-loop (HITL) is reached ONLY through
  the confidence-gated escalation below.
- **Never `git reset`** to undo. Roll back with `git revert` (creates inverse
  commits) — see step 7.
- Respect the active project's CLAUDE.md (e.g. TACO SSH-approval gates, manifest
  updates). If a merge would touch SSH/security-sensitive files, pause and ask.
- Commit fix-up edits only via `/commit-commands:commitall` (merge commits are
  created by `git merge` itself).

## Inputs
The chosen worktrees as `path` + `branch` pairs (from `/check-worktrees`). If
invoked manually with none given, first run
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json`, show the table,
and do the `/check-worktrees` selection, then continue here.

Confirm you are in the **primary checkout** (`git rev-parse --git-dir` →`.git`).
If inside a linked worktree, `ExitWorktree` (action: keep) back to primary first.

## 1. Commit dirty worktrees (one at a time)
For each chosen worktree that is `dirty`:
1. Show the user its changes: `git -C <path> status` + `git -C <path> diff --stat`.
2. `AskUserQuestion`: "Commit these changes in `<branch>`?"
   - **Yes** → `EnterWorktree` (path: `<path>`) → invoke `/commit-commands:commitall`
     (fix any review / pre-commit issues exactly as in a normal dev session) →
     `ExitWorktree` (action: keep) back to primary.
   - **No** → drop this worktree from the merge set; note it in the final summary.
Repeat until every remaining chosen worktree is clean.

## 2. Stale-base guard (per worktree, before merging)
Worktrees created via `EnterWorktree` default to `baseRef: fresh` (branched from
`origin/<default>`, not local `main`). For each branch:
```
git merge-base --is-ancestor <default> <branch>   # 0 = branch contains current main
git rev-list --count <branch>..<default>           # commits on main not in branch
```
If a branch is based on a stale `origin/<default>` or is behind local `<default>`
such that merging could lose or resurrect work, **pause and explain** to the
user for that worktree rather than merging blindly. (`check_worktrees.py --json`
reports `behind_base` to flag this cheaply.)

## 3. Race re-check (per worktree, immediately before its merge)
Re-run `claude agents --json`.
- If the only session whose cwd is inside this worktree is **this very session**
  (because step 1 entered it): `ExitWorktree` (action: keep) and re-check.
- If **another** session still occupies it: **skip this worktree, pause, and
  explain** — do not merge a worktree someone is actively using.

## 4. Determine merge order
Order the worktrees using: their directory mtime (chronological — older first is
the usual safe default), the project's current state / recent `main` commits /
recently completed work, and the actual content of each worktree's commits.
- **Low/medium confidence** → call **advisor** (the cheap first line; usually
  resolves it).
- **Still low/medium** → run the thinking suite to decide order: `/tao:thinkdeep`,
  `/tao:chat`, `/think`, `/tao:consensus`, `/tao:vet`, `/tao:synthesize`. (This is
  expensive — many agents + external-LLM calls — so it is reached only here, in
  the genuinely ambiguous case.)
- **Still low/medium after that** → **pause**, `AskUserQuestion` explaining the
  conundrum in plain terms (summarize; do NOT reference "tao:chat's Option C" or
  internal step names). Recommend a path forward with rationale.

## 5. Merge in order (from primary checkout)
For each branch in the determined order:
```
git merge --no-ff <branch>
```
- **Clean / trivial conflicts** → resolve the conflict the usual way, then
  `git merge --continue`.
- **A semantic edit is needed** (e.g. branch A adds `argA` to `foo()`, branch B
  adds `argB` → the right result is `foo(argA, argB)` plus tests for the combined
  case): if the correct resolution is **high-confidence**, just do it. If it's
  **low/medium-confidence**, route it through the **same escalation ladder as
  step 4** (advisor → thinking suite → HITL) — this is the only place conflict
  resolution prompts the user.
- Standalone fix-up edits beyond the merge commit → `/commit-commands:commitall`.

## 6. Verify main
After all merges:
```
git log --oneline -n 20
git status --porcelain        # expect clean
```
Confirm the expected commits are present and the tree is clean. **If it looks
wrong:**
1. Find the pre-merge commit in `git reflog` (the HEAD before step 5 began).
2. **Offer** to revert the whole sequence non-destructively:
   `git revert -m 1 <merge-sha>` for each merge commit **in reverse order**
   (never `git reset`).
3. Loop back to **step 4** with the observed failure as new context.
4. Cap at **3 rounds**. After the 3rd failed round, **give up** and produce a
   post-mortem: what was tried, what actually happened, the best explanation of
   why, and recommended next steps for the user.

## 7. Run the test suite (only if verify looked right)
Detect the suite, in order: a Justfile `test` recipe (`just test`) → `npm test`
(if `package.json`) → `pytest` (if Python project) → `cargo test` (if Cargo) →
else ask the user how to run tests.
- **Unexpected failures that are trivial, clear, and high-confidence** → fix them.
- **Otherwise** → advisor + thinking suite, then `AskUserQuestion` with four paths:
  - **b.1** Apply the suggested fixes (give rationale + confidence).
  - **b.2** Revert the whole merge, roll back to step 4, repeat the 3-round path.
  - **b.3** Abandon: full revert to the original state (`git revert` the merges).
  - **b.4** Abandon: leave the current state untouched, and give the user the
    exact `git revert -m 1 <sha> …` command (derived from `git reflog`) to undo
    later if they choose.

## 8. Prune (ONLY if verify looked right AND tests pass)
For each successfully merged worktree:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prune_worktree.py <path> <branch> --repo <primary>
```
Deterministic, idempotent, path-gated: removes the worktree (refuses if dirty),
deletes the branch with `git branch -d` (refuses if unmerged), and prunes admin
entries. Branch on exit code: 0 = pruned/no-op; non-zero = refused (report
verbatim, do not force).

## 9. Final summary
Concise account: which worktrees merged (and in what order), any that were
dropped/skipped and why, conflicts resolved, test result, what was pruned, and
the final state of `main`.
