"""Put the plugin's ``scripts/`` and ``hooks/`` dirs on sys.path for tests.

Claude plugins use the plugin layout (``scripts/``, ``hooks/``), not an
installable package, so tests cannot ``import worktree_gate`` (a script) or
``import check_worktrees_hook`` (a hook) without this. The runtime hook does the
equivalent sys.path bootstrap itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The Jack plugin sets this in the sessions it drives; if `just test` runs inside one,
# every hook test would otherwise silently become a no-op. Tests that exercise the
# switch set it explicitly.
os.environ.pop("WORKTREE_WARDEN_FORCE_DISABLE", None)

_PLUGIN = Path(__file__).resolve().parent.parent
for _src in (_PLUGIN / "scripts", _PLUGIN / "hooks"):
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
