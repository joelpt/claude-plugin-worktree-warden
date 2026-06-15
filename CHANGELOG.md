# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed

- **Gate load failure now fails "open loudly"** (#19): Both PreToolUse hooks (edit gate and
  destruction gate) now wrap their module imports in try/except.
  If either `worktree_gate` or `worktree_destruction` fails to import (due to syntax error,
  broken dependency, or installation issue), the hook writes a diagnostic to stderr, drops
  a sentinel file at `<git-common-dir>/worktree-warden/gate-load-error` (including a full
  traceback for debugging), and exits 0 (documented fail-open).
  The SessionStart hook detects the sentinel and surfaces a high-priority advisory even in
  `"never"` display mode, ensuring a deployment error is never silent.
  When the import error is fixed, the next session start self-heals by removing the sentinel
  once both gate modules load cleanly.
  Previously, a module import failure would exit 1 (Python's default), treated as "allow" by
  Claude Code, silently disabling the gate everywhere with no signal.

---

For older releases and detailed history, see the git commit log or
[release tags](https://github.com/joelpt/claude-plugin-worktree-warden/releases).
