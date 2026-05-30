---
name: check-worktrees
description: List every linked git worktree of the current repo with a Ready? verdict (✅ ready / mergeable after commit, 🧹 empty or already-merged & prunable, ⏳ recently active so held back, ❌ blocked by a live session) and a concise Note, then offer to merge the actionable ones. Use on /check-worktrees, "show worktrees", "any stale worktrees?", or when the SessionStart hook flags mergeable worktrees.
allowed-tools: Bash(python3 *) Skill(worktree-warden:merge-worktrees)
---

# /check-worktrees

Repo-scoped review of git worktrees. Operates ONLY on the current repo (the
one this session's cwd belongs to) and its linked worktrees — never cross-repo.

## Procedure

### 1. Render the table

Run the detector:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py
```

- **Empty output** → there are no linked worktrees. Say so in one line and
  stop (when triggered by the SessionStart hook, just continue silently).
- **Otherwise** → **Display the table output verbatim to the user in a code block.**
  Do NOT paraphrase or summarize — show the exact table output from the command.
  It lists **every** linked worktree with a `Ready?` verdict and a concise `Note`:
  - ✅ `ready to merge` (clean, commits ahead) or `can merge after commit` (dirty).
  - 🧹 `empty, can be pruned` (clean, sitting on the base tip) or
    `merged, can be pruned` (clean, HEAD already in the base/`main` chain — its
    work is landed and base has moved past it). Landing either is a no-op the
    merge flow turns into a prune. Still actionable — never call it a no-op.
  - ⏳ `active <15m ago` — edited or had session/transcript activity within the
    last 15 minutes. Held back from the auto-offer as a safety harness against
    landing half-baked work; shown for context, **mergeable on explicit request**.
  - ❌ `live session` — a live `claude` session sits inside it; shown for context
    but **not** mergeable.

> A `dirty` worktree is **mergeable even with 0 commits** — `/merge-worktrees`
> commits its uncommitted work first, then lands it. Never describe a
> dirty/0-commit worktree as a no-op or "nothing to fast-forward": it has work
> to land, it just hasn't been committed yet.

> A `dirty` worktree is **mergeable even with 0 commits** — `/merge-worktrees`
> commits its uncommitted work first, then lands it. Never describe a
> dirty/0-commit worktree as a no-op or "nothing to fast-forward": it has work
> to land, it just hasn't been committed yet. The detector lists it for exactly
> this reason.

### 2. Get the structured set

Fetch the machine-readable list:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check_worktrees.py --json
```

Each entry has `path`, `branch`, `dirty`, `commit_count`, `behind_base`,
`last_rel`, `mtime`, `session_*`, `recently_active`, and the readiness fields
`category` (`ready` / `needs_commit` / `merged` / `prune` / `cooldown` /
`blocked`), `note`, and `ready` (bool).
Keep this list — `/worktree-warden:merge-worktrees` needs the exact `path` + `branch`
of each chosen worktree.

### 3. Ask which to merge

Only worktrees with `ready: true` are candidates (the ✅ and 🧹 rows).
`cooldown` and `blocked` worktrees (`ready: false`) are shown in the table but
never auto-offered.

**Do not silently drop the held-back ones.** If any `cooldown`/`blocked`
worktrees exist, state it in one line so the override stays discoverable, e.g.
*"2 held back — `feat-x` (active <15m), `feat-y` (live session); name one to
merge it anyway."* A held-back worktree is merged only when the user explicitly
asks for it by name (then pass it straight to `/worktree-warden:merge-worktrees`,
bypassing this offer). If **no** worktree is `ready`, surface that line and stop.

`AskUserQuestion` — *"Merge worktrees before continuing?"* with options:

- **Merge all N** — every `ready` worktree.
- **Merge none** — stop here, change nothing.
- **Choose specific** — proceed to subset selection.

For **Choose specific**, present the `ready` worktrees with `multiSelect: true`
in **pages of at most 4** (one option per worktree; label = worktree dir name
(`path` basename) + `note` + age from `last_rel`, e.g.
`feat-x · merge after commit · 2 hours ago`). Accumulate selections across pages
until every `ready` worktree has been offered. The union of ticked options is
the set.

### 4. Hand off

If the chosen set is non-empty, invoke **`/worktree-warden:merge-worktrees`**, passing
the chosen worktrees' `path` + `branch` (from step 2's JSON). If empty, stop.

## Notes

- Never merge a worktree that has a live session; the detector marks those ❌
  `blocked` / `ready: false` and they are not offered (the merge skill re-checks
  for races regardless).
- ⏳ `cooldown` is a deliberate 15-minute safety window, not a hard block — it
  guards against landing a half-baked worktree you (or another session) just
  touched. It errs toward declining; the user can always override by naming the
  worktree. The gate lives only here and in the SessionStart count — never in
  the engine — so an explicit merge of a cooldown worktree still works.
- This skill only lists and asks — all merging/pruning happens in
  `/worktree-warden:merge-worktrees`, which is human-gated when confidence is low.
