# Conflict Path

Read only on engine exit `13`.

1. Open a scoped exception:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py grant "worktree-warden/merge-worktrees: resolve <branch>"
```

2. Resolve listed conflicts in `<path>`.
3. High confidence: fix directly.
4. Low or medium confidence: pause and ask the user.
5. Stage fixes:

```bash
git -C <path> add <files>
```

6. Continue:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py --repo $REPO rebase-continue --worktree <path> --branch <branch> --target $TARGET --require-lease
```

7. Close the exception:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py finished
```
