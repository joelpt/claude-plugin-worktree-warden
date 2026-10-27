#!/usr/bin/env python3
"""Black-box smoke test for the stand-down registry, driven with real processes.

Builds a throwaway git repo plus a linked worktree in a temp dir, then exercises the
registry through real subprocesses (concurrent registrants, ``kill -9``'d owners) and
through the shipped hook entry points (``python3 hooks/<hook>.py`` fed crafted stdin).
Prints a compact PASS/FAIL table and exits non-zero if any check failed. Isolated via a
temp ``XDG_CONFIG_HOME`` so it never touches the real warden config or audit log.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_HOOKS = _ROOT / "hooks"
sys.path.insert(0, str(_SCRIPTS))

import worktree_standdown as sd  # noqa: E402

_WORKER = """
import sys
sys.path.insert(0, {scripts!r})
import worktree_standdown as sd
repo, session, owner, pid = sys.argv[1:5]
sd.register(session, owner, "smoke", pid=int(pid), cwd=repo)
"""

_RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    """Record one check result."""
    _RESULTS.append((label, bool(passed), "" if passed else detail))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _spawn_owner() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "300"])


def _stray_temp_files(common: Path) -> list[str]:
    return sorted(p.name for p in common.glob(".worktree-warden-standdown.*"))


def _run_hook(hook: str, payload: dict[str, object], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HOOKS / f"{hook}.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )


def scenario_concurrent_with_kills(repo: Path, common: Path, owners: list[subprocess.Popen[bytes]]) -> None:
    """(a) 20 concurrent registrants over 3 sessions; kill -9 some owners; prune on next write."""
    code = _WORKER.format(scripts=str(_SCRIPTS))
    procs = [_spawn_owner() for _ in range(20)]
    owners.extend(procs)
    workers = [
        subprocess.Popen([sys.executable, "-c", code, str(repo), f"conc-s{i % 3}", f"owner{i}", str(procs[i].pid)])
        for i in range(20)
    ]
    check("a: 20 concurrent registrants all exit 0", all(w.wait(timeout=120) == 0 for w in workers))
    data = json.loads((common / sd.REGISTRY_BASENAME).read_text())
    seen = {o for s in data["sessions"].values() for o in s["claims"]}
    check("a: every concurrent claim recorded", seen == {f"owner{i}" for i in range(20)}, str(sorted(seen)))
    doomed = [i for i in range(20) if i % 3 == 0]
    for i in doomed:
        os.kill(procs[i].pid, signal.SIGKILL)
        procs[i].wait()
    sd.register("final", "final-owner", "smoke", pid=procs[1].pid, cwd=repo)
    data = json.loads((common / sd.REGISTRY_BASENAME).read_text())
    seen = {o for s in data["sessions"].values() for o in s["claims"]}
    survivors = {f"owner{i}" for i in range(20) if i % 3 != 0} | {"final-owner"}
    check("a: dead claims pruned, live claims survive", seen == survivors, f"extra={sorted(seen - survivors)} lost={sorted(survivors - seen)}")
    check("a: no temp files left", _stray_temp_files(common) == [], str(_stray_temp_files(common)))
    for i in range(20):
        sd.release(f"conc-s{i % 3}", f"owner{i}", cwd=repo)
    sd.release("final", "final-owner", cwd=repo)


def scenario_hooks(repo: Path, wt: Path, env: dict[str, str], owners: list[subprocess.Popen[bytes]]) -> None:
    """(b) A registered session is stood down by every hook; an unregistered one is not."""
    owner = _spawn_owner()
    owners.append(owner)
    sd.register("sess-reg", "smoke", "hooks scenario", pid=owner.pid, cwd=wt)
    payloads: dict[str, tuple[dict[str, object], Path]] = {
        "enforce_worktree_hook": (
            {"tool_name": "Edit", "cwd": str(repo), "tool_input": {"file_path": str(repo / "x.py")}},
            repo,
        ),
        "guard_destruction_hook": (
            {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": f"git worktree remove --force {wt}"}},
            repo,
        ),
        "auto_teardown_hook": ({"cwd": str(wt)}, wt),
        "check_worktrees_hook": ({"source": "startup", "cwd": str(repo)}, repo),
    }
    for hook, (payload, cwd) in payloads.items():
        reg = _run_hook(hook, {**payload, "session_id": "sess-reg"}, cwd, env)
        check(f"b: {hook} silent for registered session", (reg.returncode, reg.stdout, reg.stderr) == (0, "", ""),
              f"rc={reg.returncode} out={reg.stdout[:80]!r} err={reg.stderr[:80]!r}")
        unreg = _run_hook(hook, {**payload, "session_id": "sess-unreg"}, cwd, env)
        acted = unreg.returncode != 0 or unreg.stdout.strip() != "" or unreg.stderr.strip() != ""
        check(f"b: {hook} active for unregistered session", acted, f"rc={unreg.returncode}")
    blocked = _run_hook("enforce_worktree_hook", {**payloads["enforce_worktree_hook"][0], "session_id": "sess-unreg"}, repo, env)
    check("b: main-checkout edit blocked (exit 2) for unregistered session", blocked.returncode == 2)
    audit = "\n".join(p.read_text() for p in Path(env["XDG_CONFIG_HOME"]).rglob("audit.log"))
    check("b: audit log records owner and reason", "owner=smoke reason=hooks scenario" in audit)
    sd.release("sess-reg", "smoke", cwd=repo)
    after = _run_hook("enforce_worktree_hook", {**payloads["enforce_worktree_hook"][0], "session_id": "sess-reg"}, repo, env)
    check("b: edit blocked again after release", after.returncode == 2)


def scenario_two_owners(repo: Path, owners: list[subprocess.Popen[bytes]]) -> None:
    """(c) Two owners in one session; releasing one keeps the stand-down, the other ends it."""
    pid = _spawn_owner()
    owners.append(pid)
    sd.register("two", "alpha", "a", pid=pid.pid, cwd=repo)
    sd.register("two", "beta", "b", pid=pid.pid, cwd=repo)
    check("c: standing down with two owners", sd.is_standing_down("two", repo))
    sd.release("two", "alpha", cwd=repo)
    check("c: still standing down after releasing one owner", sd.is_standing_down("two", repo))
    sd.release("two", "beta", cwd=repo)
    check("c: not standing down after releasing both", not sd.is_standing_down("two", repo))
    sd.release("two", "beta", cwd=repo)
    check("c: repeat release is a no-op", not sd.is_standing_down("two", repo))


def scenario_file_lifecycle(repo: Path, common: Path, owners: list[subprocess.Popen[bytes]]) -> None:
    """(d) The data file is deleted when empty; the lock file persists."""
    pid = _spawn_owner()
    owners.append(pid)
    sd.register("life", "o", "r", pid=pid.pid, cwd=repo)
    data, lock = common / sd.REGISTRY_BASENAME, common / sd.LOCK_BASENAME
    check("d: data file exists with a claim", data.exists())
    inode = lock.stat().st_ino
    sd.release("life", "o", cwd=repo)
    check("d: data file deleted when empty", not data.exists())
    check("d: lock file persists", lock.exists() and lock.stat().st_ino == inode)


def scenario_corruption(repo: Path, common: Path, owners: list[subprocess.Popen[bytes]]) -> None:
    """(e) Corrupt, wrong-version and truncated registries all fail toward ACTIVE."""
    pid = _spawn_owner()
    owners.append(pid)
    sd.register("bad", "o", "r", pid=pid.pid, cwd=repo)
    data = common / sd.REGISTRY_BASENAME
    good = data.read_text()
    for label, body in (("garbage", "{not json"), ("truncated", good[: len(good) // 2]),
                        ("unknown version", good.replace('"version": 1', '"version": 9')), ("empty", "")):
        data.write_text(body)
        check(f"e: {label} registry fails toward active", not sd.is_standing_down("bad", repo))
    sd.register("bad", "o", "r", pid=pid.pid, cwd=repo)
    check("e: register recovers from a corrupt file", sd.is_standing_down("bad", repo))
    sd.release("bad", "o", cwd=repo)


def scenario_churn(repo: Path, common: Path, owners: list[subprocess.Popen[bytes]]) -> None:
    """(f) 200 register/release cycles leave no temp files and no data file."""
    pid = _spawn_owner()
    owners.append(pid)
    for i in range(200):
        sd.register("churn", f"o{i % 4}", "r", pid=pid.pid, cwd=repo)
        sd.release("churn", f"o{(i + 2) % 4}", cwd=repo)
    for i in range(4):
        sd.release("churn", f"o{i}", cwd=repo)
    check("f: no temp files after 200 cycles", _stray_temp_files(common) == [], str(_stray_temp_files(common)))
    check("f: registry empty after churn", not (common / sd.REGISTRY_BASENAME).exists())


def main() -> int:
    """Run every scenario against a throwaway repo and print the result table."""
    owners: list[subprocess.Popen[bytes]] = []
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="standdown-smoke-") as tmp:
        base = Path(tmp)
        repo, wt = base / "repo", base / "wt"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@t.test")
        _git(repo, "config", "user.name", "Smoke")
        (repo / "seed.txt").write_text("seed\n")
        _git(repo, "add", "seed.txt")
        _git(repo, "commit", "-m", "seed")
        _git(repo, "worktree", "add", "-b", "feature", str(wt))
        (wt / "work.txt").write_text("work\n")
        _git(wt, "add", "work.txt")
        _git(wt, "commit", "-m", "work")
        common = Path(sd.canonical_common_dir(repo))
        check("setup: main and linked worktree share one registry dir", sd.canonical_common_dir(wt) == str(common))
        env = {k: v for k, v in os.environ.items() if k not in ("WORKTREE_WARDEN_FORCE_DISABLE", sd.SESSION_ENV)}
        env.update(XDG_CONFIG_HOME=str(base / "xdg"), CLAUDE_CONFIG_DIR=str(base / "cc"), CLAUDE_PLUGIN_ROOT=str(_ROOT))
        try:
            scenario_concurrent_with_kills(repo, common, owners)
            scenario_hooks(repo, wt, env, owners)
            scenario_two_owners(repo, owners)
            scenario_file_lifecycle(repo, common, owners)
            scenario_corruption(repo, common, owners)
            scenario_churn(repo, common, owners)
        finally:
            for proc in owners:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
    width = max(len(label) for label, _, _ in _RESULTS)
    for label, passed, detail in _RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}  {label.ljust(width)}  {detail}".rstrip())
    failed = [label for label, passed, _ in _RESULTS if not passed]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} passed in {time.time() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
