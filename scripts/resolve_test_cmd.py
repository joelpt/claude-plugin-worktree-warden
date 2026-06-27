#!/usr/bin/env python3
"""Resolve the default verification command for a repo.

Prints a tiny JSON object:

  {"argv":["--test-cmd","just test"],"source":"justfile"}
  {"argv":["--skip-tests"],"source":"none"}

The skill layer can use this instead of carrying repo-type heuristics inline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _has_just_test(repo: Path) -> bool:
    path = repo / "Justfile"
    if not path.is_file():
        return False
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("test:"):
                return True
    except Exception:
        return False
    return False


def _has_python_tests(repo: Path) -> bool:
    markers = (
        "pytest.ini",
        "tox.ini",
        "setup.py",
        "requirements.txt",
        "manage.py",
    )
    if any((repo / name).exists() for name in markers):
        return True
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        return True
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        return True
    return (repo / "tests").exists()


def resolve(repo: Path) -> dict[str, object]:
    if _has_just_test(repo):
        return {"argv": ["--test-cmd", "just test"], "source": "justfile"}
    if (repo / "package.json").is_file():
        return {"argv": ["--test-cmd", "npm test"], "source": "package.json"}
    if _has_python_tests(repo):
        return {"argv": ["--test-cmd", "pytest"], "source": "python"}
    if (repo / "Cargo.toml").is_file():
        return {"argv": ["--test-cmd", "cargo test"], "source": "cargo"}
    return {"argv": ["--skip-tests"], "source": "none"}


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    repo = Path(args[0] if args else ".").resolve()
    print(json.dumps(resolve(repo)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
