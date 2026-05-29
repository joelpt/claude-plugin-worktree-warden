"""Put the plugin's ``scripts/`` dir on sys.path for regular imports in tests.

Claude plugins use the plugin layout (``scripts/``, ``hooks/``), not an
installable package, so tests cannot ``import worktree_gate`` without this. The
runtime hook does the equivalent sys.path bootstrap itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
