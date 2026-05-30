# TODO

## Rename plugin: `worktrees` → `worktree-warden`

Perform a proper, thorough rename of this plugin (and its repo) from `worktrees`
to `worktree-warden`. The repo directory and GitHub repo move from
`claude-plugin-worktrees` to `claude-plugin-worktree-warden`.

> **Scope discipline.** Two surfaces look alike but are different. Rename the
> **plugin identity** (brand); leave the **generic git-worktree vocabulary**
> alone. A blind global find/replace of `worktree(s)` will break the plugin.
>
> - **RENAME (plugin identity):** `plugin.json` `name`, the `worktree-warden:` skill
>   namespace prefix, the repo dir, both marketplace entries, the GitHub repo,
>   README branding, the global-CLAUDE.md mentions of "the `worktree-warden` plugin".
> - **DO NOT RENAME (describes git worktrees, not the brand):** script files
>   `worktree_engine.py` / `worktree_gate.py` / `check_worktrees.py` /
>   `enforce_worktree_hook.py` / `check_worktrees_hook.py`, the `worktree_gate`
>   CLI verb, and prose that genuinely means "a git worktree".

### Decisions to confirm before executing (HITL)

- [ ] **Skill names** — keep `check-worktrees` / `merge-worktrees` (they name
      git-worktree *actions*, so arguably stay), or rebrand to `warden-*`?
      Default: **keep** — less churn, names stay descriptive. Changing them is a
      second breaking change (slash-command paths change).
- [ ] **`worktree_gate` CLI verb** — keep as-is. It's referenced by name in the
      global `~/.claude/CLAUDE.md` ("run `worktree_gate grant ...`"). Renaming it
      means editing that file too. Default: **keep**.
- [ ] **GitHub repo rename vs. new repo** — `gh repo rename` preserves history
      and leaves a redirect from the old URL. Default: **rename in place**.

### This is a BREAKING change (claude-plugins.md HITL gate)

Renaming the plugin breaks existing users on the prior version:

- Skill invocation namespace changes: `/worktree-warden:check-worktrees`
  (same for `merge-worktrees`).
- The installed plugin reference changes name, so users must disable the old
  install and enable the new one (a marketplace update alone won't rename it).

Action: surface the break via `AskUserQuestion` at commit time, offer
forward-migration notes in the README, and record `BREAKING:` in the commit body.

### Execution checklist

**Manifest & in-repo references**

- [ ] `.claude-plugin/plugin.json` — `name`: `worktrees` → `worktree-warden`.
      Bump `version` (CalVer `YYYY.MM.DD.N`) in the same commit.
- [x] `skills/check-worktrees/SKILL.md` — `allowed-tools:
      Skill(worktree-warden:merge-worktrees)` and body refs
      `/worktree-warden:merge-worktrees` updated.
- [x] `skills/merge-worktrees/SKILL.md` — no `worktree-warden:` namespace refs needed.
- [x] `README.md` — title/branding and every
      `/worktree-warden:check-worktrees` / `/worktree-warden:merge-worktrees` example
      updated. Added "Renamed from `worktrees`" migration note.
- [x] Sweep for stragglers:
      `grep -rn "worktree-warden:" --include=*.md --include=*.json` passes clean.

**Repo move (host + GitHub)**

- [ ] `git mv`-free dir move: rename working dir
      `~/code/claude-plugin-worktrees` → `~/code/claude-plugin-worktree-warden`
      (close session/worktrees first; this dir holds the active worktree).
- [ ] `gh repo rename claude-plugin-worktree-warden` (from inside the repo),
      then verify `git remote -v` points at the new URL (gh updates origin).

**Marketplaces (keep both in lockstep)**

- [ ] Public — `~/code/joelpt-claude-plugins/.claude-plugin/marketplace.json`:
      entry `name` `worktrees` → `worktree-warden`,
      `repo` `joelpt/claude-plugin-worktrees` → `...-worktree-warden`,
      refresh `description` if behaviour text drifted. Commit + push that repo.
- [ ] Local-dev — `~/code/.claude-plugin/marketplace.json`:
      `{ "name": "worktrees", "source": "./claude-plugin-worktrees" }` →
      `{ "name": "worktree-warden", "source": "./claude-plugin-worktree-warden" }`.
      (Bare file edit — `~/code/` is not a git repo; no push.)

**Global config**

- [ ] `~/.claude/CLAUDE.md` — update the "Worktree-first editing" section that
      says "The `worktrees` plugin runs a `PreToolUse` gate" → `worktree-warden`.
      (Leave the `worktree_gate` command examples unless that decision flips.)

**Re-install & verify**

- [ ] `claude plugin disable worktrees@joelpt-local` (and the github install if
      present), then enable `worktree-warden@...` after marketplace refresh.
- [ ] `claude plugin marketplace update joelpt-local` (+ `joelpt-claude-plugins`)
      or restart Claude Code so the new name registers.
- [ ] Self-tests: `plugin.json` + `hooks/hooks.json` parse; each hook script
      `py_compile`s; `just test` green.
- [ ] Smoke: new session — SessionStart hook fires, the PreToolUse gate still
      blocks main-checkout edits, and `/worktree-warden:check-worktrees`
      resolves and renders the table.

**Cleanup**

- [ ] After all of the above, the old `~/.claude/wip/<old-cwd>/` recap dir is
      keyed by the old path — harmless, optionally remove once confirmed.
