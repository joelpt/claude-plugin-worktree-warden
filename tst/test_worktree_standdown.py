"""Tests for the session-scoped stand-down registry (``scripts/worktree_standdown.py``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import worktree_standdown as sd

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "worktree_standdown.py"


class RegistryCase(unittest.TestCase):
    """A temp repo with one linked worktree and helpers to spawn disposable owner processes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t.test")
        self._git("config", "user.name", "Test")
        (self.repo / "seed.txt").write_text("seed\n")
        self._git("add", "seed.txt")
        self._git("commit", "-m", "seed")
        self.wt = base / "wt"
        self._git("worktree", "add", "-b", "feature", str(self.wt))
        self.common = Path(sd.canonical_common_dir(self.repo))
        self.data = self.common / sd.REGISTRY_BASENAME
        self.lock = self.common / sd.LOCK_BASENAME
        self._procs: list[subprocess.Popen[bytes]] = []

    def tearDown(self) -> None:
        for proc in self._procs:
            proc.kill()
            proc.wait()
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def spawn(self) -> subprocess.Popen[bytes]:
        """Start a live disposable process and track it for cleanup."""
        proc = subprocess.Popen(["sleep", "60"])
        self._procs.append(proc)
        return proc

    def dead_pid(self) -> int:
        """Return the pid of a process that has exited and been reaped."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def tmp_files(self) -> list[str]:
        return sorted(p.name for p in self.common.glob(".worktree-warden-standdown.*"))


class CanonicalDirTest(RegistryCase):
    def test_main_and_linked_worktree_share_one_dir(self) -> None:
        self.assertEqual(sd.canonical_common_dir(self.repo), sd.canonical_common_dir(self.wt))
        sd.register("s1", "jack", "r", pid=self.spawn().pid, cwd=self.repo)
        self.assertTrue(sd.is_standing_down("s1", cwd=self.wt))
        self.assertTrue(self.data.exists())

    def test_non_repo_raises(self) -> None:
        with tempfile.TemporaryDirectory() as plain, self.assertRaises(RuntimeError):
            sd.canonical_common_dir(plain)


class RegisterReleaseTest(RegistryCase):
    def test_schema_v1(self) -> None:
        pid = self.spawn().pid
        sd.register("s1", "jack", "because", pid=pid, cwd=self.repo)
        data = json.loads(self.data.read_text())
        self.assertEqual(data["version"], 1)
        claim = data["sessions"]["s1"]["claims"]["jack"]
        self.assertEqual(claim["pid"], pid)
        self.assertEqual(claim["pid_comm"], "sleep")
        self.assertEqual(claim["reason"], "because")
        self.assertRegex(claim["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_two_owners_one_session(self) -> None:
        pid = self.spawn().pid
        sd.register("s1", "jack", "a", pid=pid, cwd=self.repo)
        sd.register("s1", "other", "b", pid=pid, cwd=self.repo)
        self.assertEqual(sd.active_claims("s1", self.repo), {"jack": "a", "other": "b"})
        sd.release("s1", "jack", cwd=self.repo)
        self.assertTrue(sd.is_standing_down("s1", self.repo))
        self.assertEqual(sd.active_claims("s1", self.repo), {"other": "b"})
        sd.release("s1", "other", cwd=self.repo)
        self.assertFalse(sd.is_standing_down("s1", self.repo))

    def test_release_is_idempotent_and_owner_specific(self) -> None:
        sd.release("nobody", "x", cwd=self.repo)
        pid = self.spawn().pid
        sd.register("s1", "jack", "a", pid=pid, cwd=self.repo)
        sd.release("s1", "someone-else", cwd=self.repo)
        sd.release("s1", "jack", cwd=self.repo)
        sd.release("s1", "jack", cwd=self.repo)
        self.assertFalse(sd.is_standing_down("s1", self.repo))

    def test_sessions_are_isolated(self) -> None:
        pid = self.spawn().pid
        sd.register("s1", "jack", "a", pid=pid, cwd=self.repo)
        self.assertTrue(sd.is_standing_down("s1", self.repo))
        self.assertFalse(sd.is_standing_down("s2", self.repo))
        self.assertFalse(sd.is_standing_down("", self.repo))
        self.assertFalse(sd.is_standing_down(None, self.repo))

    def test_register_requires_identifiers_and_readable_pid(self) -> None:
        with self.assertRaises(RuntimeError):
            sd.register("", "jack", "r", cwd=self.repo)
        with self.assertRaises(RuntimeError):
            sd.register("s1", "", "r", cwd=self.repo)
        with self.assertRaises(RuntimeError):
            sd.register("s1", "jack", "r", pid=self.dead_pid(), cwd=self.repo)


class FileLifecycleTest(RegistryCase):
    def test_empty_registry_deletes_data_file_but_keeps_lock(self) -> None:
        sd.register("s1", "jack", "a", pid=self.spawn().pid, cwd=self.repo)
        self.assertTrue(self.data.exists())
        inode = self.lock.stat().st_ino
        sd.release("s1", "jack", cwd=self.repo)
        self.assertFalse(self.data.exists())
        self.assertTrue(self.lock.exists())
        sd.register("s1", "jack", "a", pid=self.spawn().pid, cwd=self.repo)
        sd.release("s1", "jack", cwd=self.repo)
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_no_temp_files_left_behind(self) -> None:
        pid = self.spawn().pid
        for i in range(30):
            sd.register("s1", f"o{i % 3}", "r", pid=pid, cwd=self.repo)
            sd.release("s1", f"o{(i + 1) % 3}", cwd=self.repo)
        self.assertEqual(self.tmp_files(), [])


class PruneTest(RegistryCase):
    def test_dead_pid_is_not_standing_down_and_is_pruned_by_next_write(self) -> None:
        live = self.spawn()
        doomed = self.spawn()
        sd.register("dead-session", "jack", "r", pid=doomed.pid, cwd=self.repo)
        sd.register("live-session", "jack", "r", pid=live.pid, cwd=self.repo)
        doomed.kill()
        doomed.wait()
        self.assertFalse(sd.is_standing_down("dead-session", self.repo))
        self.assertTrue(sd.is_standing_down("live-session", self.repo))
        sd.register("third", "jack", "r", pid=live.pid, cwd=self.repo)
        sessions = json.loads(self.data.read_text())["sessions"]
        self.assertEqual(sorted(sessions), ["live-session", "third"])

    def test_pid_reuse_comm_mismatch_is_invalid_and_pruned(self) -> None:
        pid = self.spawn().pid
        sd.register("s1", "jack", "r", pid=pid, cwd=self.repo)
        data = json.loads(self.data.read_text())
        data["sessions"]["s1"]["claims"]["jack"]["pid_comm"] = "definitely-not-sleep"
        self.data.write_text(json.dumps(data))
        self.assertFalse(sd.is_standing_down("s1", self.repo))
        sd.register("s2", "jack", "r", pid=pid, cwd=self.repo)
        self.assertEqual(sorted(json.loads(self.data.read_text())["sessions"]), ["s2"])

    def test_bad_pid_values_are_invalid(self) -> None:
        for bad in (0, -1, True, "12", None, 2**80):
            with self.subTest(pid=bad):
                self.assertFalse(sd._claim_valid({"pid": bad, "pid_comm": "sleep"}))
        self.assertFalse(sd._claim_valid("junk"))

    def test_permission_error_counts_as_alive(self) -> None:
        # pid 1 is not ours; kill(1, 0) raises PermissionError for non-root and succeeds for root.
        self.assertTrue(sd._pid_alive(1))


class FailTowardActiveTest(RegistryCase):
    def _claim(self) -> None:
        sd.register("s1", "jack", "r", pid=self.spawn().pid, cwd=self.repo)

    def test_malformed_json(self) -> None:
        self._claim()
        self.data.write_text("{not json")
        self.assertFalse(sd.is_standing_down("s1", self.repo))

    def test_unknown_version(self) -> None:
        self._claim()
        data = json.loads(self.data.read_text())
        data["version"] = 2
        self.data.write_text(json.dumps(data))
        self.assertFalse(sd.is_standing_down("s1", self.repo))

    def test_wrong_shapes(self) -> None:
        for body in ("[]", '{"version":1}', '{"version":1,"sessions":[]}', '{"version":1,"sessions":{"s1":5}}',
                     '{"version":1,"sessions":{"s1":{"claims":[]}}}', ""):
            with self.subTest(body=body):
                self.data.write_text(body)
                self.assertFalse(sd.is_standing_down("s1", self.repo))

    def test_unreadable_file(self) -> None:
        self._claim()
        self.data.chmod(0)
        try:
            if os.access(self.data, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            self.assertFalse(sd.is_standing_down("s1", self.repo))
        finally:
            self.data.chmod(0o600)

    def test_missing_registry_and_non_repo(self) -> None:
        self.assertFalse(sd.is_standing_down("s1", self.repo))
        with tempfile.TemporaryDirectory() as plain:
            self.assertFalse(sd.is_standing_down("s1", plain))

    def test_register_overwrites_corrupt_file(self) -> None:
        self.data.write_text("{garbage")
        sd.register("s1", "jack", "r", pid=self.spawn().pid, cwd=self.repo)
        self.assertTrue(sd.is_standing_down("s1", self.repo))

    def test_reads_do_not_write_or_lock(self) -> None:
        self.assertFalse(sd.is_standing_down("s1", self.repo))
        self.assertFalse(self.data.exists())
        self.assertFalse(self.lock.exists())


_WORKER = """
import sys
sys.path.insert(0, {scripts!r})
import worktree_standdown as sd
mode, repo, session, owner, pid = sys.argv[1:6]
for _ in range(int(sys.argv[6])):
    sd.register(session, owner, "r", pid=int(pid), cwd=repo)
    if mode == "cycle":
        sd.release(session, owner, cwd=repo)
"""


class ConcurrencyTest(RegistryCase):
    def _launch(self, mode: str, session: str, owner: str, pid: int, loops: int) -> subprocess.Popen[bytes]:
        code = _WORKER.format(scripts=str(_ROOT / "scripts"))
        return subprocess.Popen(
            [sys.executable, "-c", code, mode, str(self.repo), session, owner, str(pid), str(loops)]
        )

    def test_many_processes_never_lose_a_live_claim(self) -> None:
        pid = self.spawn().pid
        workers = [self._launch("keep", f"s{i % 3}", f"owner{i}", pid, 5) for i in range(16)]
        # Interleave churn from other sessions that register and release continuously.
        churn = [self._launch("cycle", f"churn{i}", "c", pid, 15) for i in range(4)]
        for proc in [*workers, *churn]:
            self.assertEqual(proc.wait(timeout=120), 0)
        data = json.loads(self.data.read_text())
        owners = {o for s in data["sessions"].values() for o in s["claims"]}
        self.assertEqual(owners, {f"owner{i}" for i in range(16)})
        self.assertEqual(self.tmp_files(), [])

    def test_readers_never_see_partial_data_during_writes(self) -> None:
        pid = self.spawn().pid
        sd.register("stable", "jack", "r", pid=pid, cwd=self.repo)
        writer = self._launch("cycle", "churn", "c", pid, 60)
        while writer.poll() is None:
            self.assertTrue(sd.is_standing_down("stable", self.repo))
        self.assertEqual(writer.wait(), 0)


class CliTest(RegistryCase):
    def _cli(self, *args: str, env: dict[str, str] | None = None, exe: str | None = None):  # type: ignore[no-untyped-def]
        full_env = {k: v for k, v in os.environ.items() if k != sd.SESSION_ENV}
        full_env.update(env or {})
        return subprocess.run(
            [exe or sys.executable, str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env=full_env,
        )

    def _fake_claude(self) -> str:
        """Return a shell binary whose process comm is ``claude`` (a copy of /bin/sh)."""
        exe = Path(self._tmp.name) / "bin" / "claude"
        exe.parent.mkdir(exist_ok=True)
        shutil.copy("/bin/sh", exe)
        if shutil.which("codesign"):
            subprocess.run(["codesign", "--force", "-s", "-", str(exe)], capture_output=True)
        probe = subprocess.run([str(exe), "-c", "exit 0"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("cannot execute a renamed copy of /bin/sh here")
        return str(exe)

    def _orphan(self, *argv: str) -> tuple[str, str, str]:
        """Run *argv* detached so it has no ``claude`` ancestor; return (rc, stdout, stderr)."""
        out = Path(self._tmp.name) / "orphan"
        out.mkdir(exist_ok=True)
        script = (
            f'("$@" >{out}/out 2>{out}/err; echo $? >{out}/rc.tmp; mv {out}/rc.tmp {out}/rc) &'
        )
        subprocess.run(["/bin/sh", "-c", script, "sh", *argv], cwd=str(self.repo), check=True)
        deadline = time.time() + 30
        while not (out / "rc").exists() and time.time() < deadline:
            time.sleep(0.05)
        return (out / "rc").read_text().strip(), (out / "out").read_text(), (out / "err").read_text()

    def test_register_refuses_without_claude_ancestor(self) -> None:
        rc, _, err = self._orphan(
            sys.executable, str(_SCRIPT), "register", "--session-id", "s1", "--owner", "jack"
        )
        self.assertEqual(rc, "2")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertFalse(self.data.exists())

    def test_find_claude_ancestor_none_when_orphaned(self) -> None:
        code = f"import sys;sys.path.insert(0,{str(_ROOT / 'scripts')!r});import worktree_standdown as s;print(s.find_claude_ancestor())"
        rc, out, _ = self._orphan(sys.executable, "-c", code)
        self.assertEqual((rc, out.strip()), ("0", "None"))

    def test_register_active_release_through_a_claude_named_ancestor(self) -> None:
        exe = self._fake_claude()
        register = (
            f"{sys.executable} {_SCRIPT} register --session-id s1 --owner jack "
            f"--reason x --cwd {self.repo}; echo $?; sleep 60; true"
        )
        holder = subprocess.Popen([exe, "-c", register], stdout=subprocess.PIPE, text=True)
        self._procs.append(holder)  # type: ignore[arg-type]
        assert holder.stdout is not None
        self.assertEqual(holder.stdout.readline().strip(), "0")
        claim = json.loads(self.data.read_text())["sessions"]["s1"]["claims"]["jack"]
        self.assertEqual(claim["pid"], holder.pid)
        self.assertEqual(claim["pid_comm"], "claude")
        self.assertEqual(self._cli("active", "--session-id", "s1").returncode, 0)
        self.assertEqual(self._cli("active", "--session-id", "other").returncode, 1)
        self.assertEqual(self._cli("active", env={sd.SESSION_ENV: "s1"}).returncode, 0)
        shown = self._cli("show")
        self.assertEqual(json.loads(shown.stdout)["version"], 1)
        rel = self._cli("release", "--session-id", "s1", "--owner", "jack")
        self.assertEqual(rel.returncode, 0)
        self.assertEqual(self._cli("active", "--session-id", "s1").returncode, 1)

    def test_active_is_silent_and_one_line_errors(self) -> None:
        proc = self._cli("active", "--session-id", "s1")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (1, "", ""))
        proc = self._cli("release", "--owner", "jack")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
        self.assertEqual(self._cli("show").stdout.strip(), "{}")


class StandDownForTest(RegistryCase):
    def test_audits_owner_and_reason_and_uses_env_fallback(self) -> None:
        sd.register("s1", "jack", "multi\tline\nreason", pid=self.spawn().pid, cwd=self.repo)
        cfg = Path(self._tmp.name) / "xdg"
        old = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", sd.SESSION_ENV)}
        os.environ["XDG_CONFIG_HOME"] = str(cfg)
        os.environ[sd.SESSION_ENV] = "s1"
        try:
            self.assertTrue(sd.stand_down_for("enforce_worktree_hook", None, self.repo))
            self.assertFalse(sd.stand_down_for("enforce_worktree_hook", "other", self.repo))
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        logs = list(cfg.rglob("audit.log"))
        self.assertEqual(len(logs), 1)
        lines = logs[0].read_text().splitlines()
        self.assertEqual(len(lines), 1)
        stamp, action, reason, _ = lines[0].split("\t")
        self.assertEqual(action, "standdown")
        self.assertIn("enforce_worktree_hook", reason)
        self.assertIn("owner=jack", reason)
        self.assertIn("reason=multi line reason", reason)
        self.assertTrue(stamp.endswith("Z"))

    def test_no_claim_is_false_without_audit(self) -> None:
        self.assertFalse(sd.stand_down_for("h", "s1", self.repo))

    def test_forged_owner_in_registry_is_dropped_and_never_audited(self) -> None:
        pid = self.spawn().pid
        sd.register("s1", "jack", "r", pid=pid, cwd=self.repo)
        data = json.loads(self.data.read_text())
        claims = data["sessions"]["s1"]["claims"]
        claims["x\n2026-01-01T00:00:00Z\tstanddown\tFORGED"] = dict(claims["jack"])
        self.data.write_text(json.dumps(data))
        self.assertEqual(sd.active_claims("s1", self.repo), {"jack": "r"})

    def test_register_rejects_unsafe_owner(self) -> None:
        for owner in ("a b", "x\ny", "t\tt", "a" * 65, "é"):
            with self.assertRaises(RuntimeError):
                sd.register("s1", owner, "r", pid=self.spawn().pid, cwd=self.repo)


if __name__ == "__main__":
    unittest.main()
