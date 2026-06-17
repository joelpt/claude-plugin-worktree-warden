#!/usr/bin/env python3
"""Black-box smoke test: drive the shipped hook and gate CLI end-to-end.

Runs the real ``enforce_worktree_hook.py`` (fed the Claude Code stdin contract)
and the ``worktree_gate.py`` CLI as subprocesses against a throwaway git repo,
asserting the full enforcement lifecycle: block, grant, allow, finished, block,
the linked-worktree allow path, and the disable/enable round-trip. Then exercises
the ``worktree_lock.py`` CLI: acquire, block a second owner, release, reacquire,
force-unlock. Isolated via a temp repo and a temp ``XDG_CONFIG_HOME`` so it never
touches the real gate config or audit log.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _ROOT / "hooks" / "enforce_worktree_hook.py"
_GATE = _ROOT / "scripts" / "worktree_gate.py"
_LOCK = _ROOT / "scripts" / "worktree_lock.py"
_ENGINE = _ROOT / "scripts" / "worktree_engine.py"


class _Smoke:
    """Accumulate check results and remember failures for the exit code."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, passed: bool) -> None:
        """Print one check result and record it when it fails.

        Args:
            label: Human-readable description of the check.
            passed: Whether the check held.
        """
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}")
        if not passed:
            self.failures.append(label)


def _git(repo: Path, *args: str) -> None:
    """Run a git command in repo, raising on a non-zero exit."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_hook(
    cwd: Path, env: Mapping[str, str], session_id: str | None = None
) -> tuple[int, str]:
    """Drive the enforce hook for an Edit at ``cwd/src/main.py``.

    Args:
        cwd: Session working directory the hook should classify against.
        env: Isolated environment (temp XDG plus the plugin root).
        session_id: Optional session id for the payload (owner of occupancy).

    Returns:
        The hook exit code and its stderr text.
    """
    body: dict[str, object] = {
        "tool_name": "Edit",
        "cwd": str(cwd),
        "tool_input": {"file_path": str(cwd / "src" / "main.py")},
    }
    if session_id is not None:
        body["session_id"] = session_id
    payload = json.dumps(body)
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    return proc.returncode, proc.stderr


def _cli(repo: Path, env: Mapping[str, str], *args: str) -> str:
    """Run the gate CLI in repo and return its stdout."""
    proc = subprocess.run(
        [sys.executable, str(_GATE), *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    return proc.stdout


def _lock(repo: Path, env: Mapping[str, str], *args: str) -> tuple[int, str]:
    """Run the lock CLI in repo and return its exit code and stdout."""
    proc = subprocess.run(
        [sys.executable, str(_LOCK), *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    return proc.returncode, proc.stdout


def _engine(repo: Path, env: Mapping[str, str], *args: str) -> tuple[int, str]:
    """Run the engine CLI in repo and return its exit code and stdout."""
    proc = subprocess.run(
        [sys.executable, str(_ENGINE), *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    return proc.returncode, proc.stdout


def _write_user_config(env: Mapping[str, str], data: dict[str, object]) -> None:
    """Write the user-scope worktree-gate config under the smoke's temp XDG dir."""
    d = Path(env["XDG_CONFIG_HOME"]) / "worktree-gate"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(data))


def _seed_repo(repo: Path) -> None:
    """Create a minimal committed git repo at repo."""
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "smoke@test.invalid")
    _git(repo, "config", "user.name", "smoke")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "seed")


def main() -> int:
    """Run the smoke lifecycle against an isolated repo.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    smoke = _Smoke()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        repo = base / "repo"
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(base / "xdg"),
            "CLAUDE_PLUGIN_ROOT": str(_ROOT),
        }
        _seed_repo(repo)
        # Keep the gate + main-lock checks free of occupancy side effects; the
        # dedicated occupancy section below re-enables it.
        _write_user_config(env, {"occupancy_lock": False})

        print("worktree-warden smoke:")

        rc, err = _run_hook(repo, env)
        smoke.check("main-checkout edit is blocked (exit 2)", rc == 2)
        smoke.check(
            "guidance names request-exception/finish-exception skills",
            "worktree-warden:request-exception" in err
            and "worktree-warden:finish-exception" in err,
        )

        _cli(repo, env, "grant", "smoke reason")
        smoke.check("grant opens the window (edit allowed)", _run_hook(repo, env)[0] == 0)

        _cli(repo, env, "finished")
        smoke.check("finished re-gates (edit blocked)", _run_hook(repo, env)[0] == 2)

        worktree = base / "wt"
        _git(repo, "worktree", "add", "-b", "feature", str(worktree))
        smoke.check("linked worktree is allowed", _run_hook(worktree, env)[0] == 0)

        _cli(repo, env, "disable")
        smoke.check("disable lifts the gate", _run_hook(repo, env)[0] == 0)
        _cli(repo, env, "enable")
        smoke.check("enable restores the gate", _run_hook(repo, env)[0] == 2)

        rc_a, _ = _lock(repo, env, "acquire-main", "--repo", str(repo), "--owner", "S1", "smoke")
        smoke.check("lock acquire-main succeeds (exit 0)", rc_a == 0)
        rc_b, out_b = _lock(repo, env, "acquire-main", "--repo", str(repo), "--owner", "S2", "x")
        smoke.check("second owner blocked (exit 1, names holder)", rc_b == 1 and "S1" in out_b)
        rc_s, out_s = _lock(repo, env, "status", "--repo", str(repo))
        smoke.check("status reports the active lock", rc_s == 0 and "S1" in out_s)
        _lock(repo, env, "release-main", "--repo", str(repo), "--owner", "S1")
        rc_c, _ = _lock(repo, env, "acquire-main", "--repo", str(repo), "--owner", "S2", "again")
        smoke.check("lock acquirable after release", rc_c == 0)
        _lock(repo, env, "force-unlock", "--repo", str(repo))
        _, out_st = _lock(repo, env, "status", "--repo", str(repo))
        smoke.check("force-unlock clears the lock", "no active locks" in out_st)

        # Occupancy: a second session editing the same worktree is blocked; the
        # first session (and its subagents, sharing its id) is not.
        _write_user_config(env, {})  # re-enable occupancy (default on)
        rc_oa, _ = _run_hook(worktree, env, session_id="OCC-A")
        smoke.check("occupancy: first session edits worktree (allowed)", rc_oa == 0)
        rc_ob, err_ob = _run_hook(worktree, env, session_id="OCC-B")
        smoke.check(
            "occupancy: second session blocked (exit 2, names holder)",
            rc_ob == 2 and "OCC-A" in err_ob and "occupied" in err_ob,
        )

        # Lock CLI accepts --repo BEFORE the subcommand (engine-style order), too.
        rc_ord, _ = _lock(repo, env, "--repo", str(repo), "acquire-main", "--owner", "ORD", "x")
        smoke.check("lock --repo accepted before subcommand", rc_ord == 0)
        _lock(repo, env, "force-unlock", "--repo", str(repo), "--all")

        # finish: one-shot land of a clean worktree (--no-lock + --skip-tests to
        # isolate the land/teardown flow from the lock store and a test runner).
        fwt = base / "fwt"
        _git(repo, "worktree", "add", "-b", "landme", str(fwt))
        (fwt / "land.txt").write_text("land me\n")
        _git(fwt, "add", "land.txt")
        _git(fwt, "commit", "-m", "land work")
        rc_fin, _ = _engine(
            repo, env, "--repo", str(repo), "finish", "--worktree", str(fwt),
            "--branch", "landme", "--target", "main", "--skip-tests", "--no-lock",
        )
        smoke.check(
            "finish: one-shot land tears down the worktree", rc_fin == 0 and not fwt.exists()
        )

    if smoke.failures:
        print(f"SMOKE FAIL ({len(smoke.failures)})")
        return 1
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
