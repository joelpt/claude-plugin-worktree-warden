---
name: finish-exception
description: Close the active main-checkout exception and re-gate main edits.
user-invocable: false
---

Close any active main-checkout exception immediately, re-gating main edits.

Run this as soon as the main-side work that needed the exception is finished:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/worktree_gate.py" finished
```
