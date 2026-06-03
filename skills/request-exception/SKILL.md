---
name: request-exception
user-invocable: false
---

# request-exception

Open a short, time-boxed exception so the current main-checkout edit can proceed.

Only legitimate when the edit is genuinely main-side — conflict resolution,
landing to the default branch, or an explicit user instruction to edit main.
Normal feature work belongs in a worktree (`EnterWorktree`), never here.

Run this, passing an honest one-line reason (use `$ARGUMENTS` when the caller
supplied one; otherwise write a truthful reason yourself):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py" grant "$ARGUMENTS"
```

The window is short and self-expiring.
The moment the main-side work is done,
invoke the **worktree-warden:finish-exception** skill to close it.
