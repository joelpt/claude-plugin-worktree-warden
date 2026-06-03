---
name: finish-exception
user-invocable: false
---

# finish-exception

Close any active main-checkout exception immediately, re-gating main edits.

Run this as soon as the main-side work that needed the exception is finished:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py" finished
```
