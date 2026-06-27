# Verify Or Test Failure

Read only after all lands if verify or tests fail.

1. Clear trivial fix with high confidence: fix, then re-run verify/tests.
2. Otherwise undo:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_engine.py --repo $REPO undo --snapshot <snapshot_file>
```

3. Ask the user which path to take:
   - retry with fixes
   - leave original state undone
   - keep current landed state and hand over the undo command
4. Release the lock before any terminal stop:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_lock.py --repo $REPO release-main
```
