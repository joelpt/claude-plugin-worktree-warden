"""Hooks, the finish gate and the engine honor the session-scoped stand-down registry."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import worktree_engine as engine
import worktree_gate as gate
import worktree_standdown as sd
from test_force_disable import _HookRepoCase

_SESSION = "sess-claimed"


class _RegistryHookCase(_HookRepoCase):
    """``_HookRepoCase`` plus helpers to register claims for disposable owner processes."""

    def setUp(self) -> None:
        super().setUp()
        self._procs: list[subprocess.Popen[bytes]] = []
        self._claimed_paths = self._payloads()

    def tearDown(self) -> None:
        for proc in self._procs:
            proc.kill()
            proc.wait()
        super().tearDown()

    def _owner_process(self) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(["sleep", "60"])
        self._procs.append(proc)
        return proc

    def _claim(self, session: str = _SESSION, owner: str = "jack", reason: str = "driven by jack") -> int:
        proc = self._owner_process()
        sd.register(session, owner, reason, pid=proc.pid, cwd=self.repo)
        return proc.pid

    def _payload_for(self, name: str, session: str) -> tuple[Path, dict[str, object], Path]:
        hook, payload, cwd = self._claimed_paths[name]
        return hook, {**payload, "session_id": session}, cwd

    def _audit_text(self) -> str:
        return "\n".join(p.read_text() for p in self.xdg.rglob("audit.log"))


class RegistryStandsHooksDownTest(_RegistryHookCase):
    def test_each_hook_stands_down_silently_with_a_valid_claim_and_audits_it(self) -> None:
        self._claim()
        for name in self._claimed_paths:
            with self.subTest(hook=name):
                hook, payload, cwd = self._payload_for(name, _SESSION)
                proc = self._run(hook, payload, switch=None, cwd=cwd)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
                audit = self._audit_text()
                self.assertRegex(audit, rf"standdown\t{name}: owner=jack reason=driven by jack")

    def test_each_hook_stays_active_without_a_claim(self) -> None:
        for name in self._claimed_paths:
            with self.subTest(hook=name):
                hook, payload, cwd = self._payload_for(name, _SESSION)
                proc = self._run(hook, payload, switch=None, cwd=cwd)
                self.assertTrue(proc.stdout.strip() != "" or proc.stderr.strip() != "" or proc.returncode != 0)

    def test_dead_claim_does_not_stand_down(self) -> None:
        pid = self._claim()
        for proc in self._procs:
            if proc.pid == pid:
                proc.kill()
                proc.wait()
        for name in self._claimed_paths:
            with self.subTest(hook=name):
                hook, payload, cwd = self._payload_for(name, _SESSION)
                proc = self._run(hook, payload, switch=None, cwd=cwd)
                self.assertTrue(proc.stdout.strip() != "" or proc.stderr.strip() != "" or proc.returncode != 0)

    def test_other_sessions_claim_does_not_stand_down(self) -> None:
        self._claim()
        for name in self._claimed_paths:
            with self.subTest(hook=name):
                hook, payload, cwd = self._payload_for(name, "sess-other")
                proc = self._run(hook, payload, switch=None, cwd=cwd)
                self.assertTrue(proc.stdout.strip() != "" or proc.stderr.strip() != "" or proc.returncode != 0)
        self.assertNotIn("sess-other", self._audit_text())

    def test_session_id_falls_back_to_the_environment(self) -> None:
        self._claim()
        hook, payload, cwd = self._claimed_paths["enforce_worktree_hook"]
        payload = {k: v for k, v in payload.items() if k != "session_id"}
        env = {**self._env(switch=None), sd.SESSION_ENV: _SESSION}
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    def test_claim_registered_from_linked_worktree_covers_the_main_checkout(self) -> None:
        proc = self._owner_process()
        sd.register(_SESSION, "jack", "r", pid=proc.pid, cwd=self.wt)
        hook, payload, cwd = self._payload_for("enforce_worktree_hook", _SESSION)
        result = self._run(hook, payload, switch=None, cwd=cwd)
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_corrupt_registry_leaves_the_hooks_active(self) -> None:
        self._claim()
        (Path(sd.canonical_common_dir(self.repo)) / sd.REGISTRY_BASENAME).write_text("{oops")
        hook, payload, cwd = self._payload_for("enforce_worktree_hook", _SESSION)
        self.assertEqual(self._run(hook, payload, switch=None, cwd=cwd).returncode, 2)

    def test_env_var_still_works_without_any_claim(self) -> None:
        for name in self._claimed_paths:
            with self.subTest(hook=name):
                hook, payload, cwd = self._payload_for(name, _SESSION)
                proc = self._run(hook, payload, switch="1", cwd=cwd)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_a_missing_standdown_module_leaves_hooks_active(self) -> None:
        # A broken registry module must never stand a hook down.
        self._claim()
        plugin = Path(self._tmp.name) / "plugin"
        shutil.copytree(Path(__file__).resolve().parent.parent / "scripts", plugin / "scripts")
        shutil.copytree(Path(__file__).resolve().parent.parent / "hooks", plugin / "hooks")
        (plugin / "scripts" / "worktree_standdown.py").write_text("raise RuntimeError('broken')\n")
        _, payload, cwd = self._payload_for("enforce_worktree_hook", _SESSION)
        env = {**self._env(switch=None), "CLAUDE_PLUGIN_ROOT": str(plugin)}
        proc = subprocess.run(
            [sys.executable, str(plugin / "hooks" / "enforce_worktree_hook.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        self.assertEqual(proc.returncode, 2)


class TestsOnFinishStandDownTest(_RegistryHookCase):
    def _enable_tests(self) -> None:
        cfg = self.xdg / "worktree-gate" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"run_tests_on_finish": True}))

    def test_gate_stands_down_only_for_the_claimed_session(self) -> None:
        self._claim()
        self._enable_tests()
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            facts = gate.git_facts(str(self.repo))
            self.assertFalse(gate.tests_on_finish_enabled(facts, {}, session_id=_SESSION))
            self.assertTrue(gate.tests_on_finish_enabled(facts, {}, session_id="sess-other"))
            self.assertTrue(gate.tests_on_finish_enabled(facts, {}, session_id=None))
            self.assertTrue(gate.tests_on_finish_enabled(facts, {}))

    def test_engine_threads_the_session_identity(self) -> None:
        self._claim()
        self._enable_tests()
        (self.repo / "Justfile").write_text("test:\n    true\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}):
            claimed = engine.cmd_finish_preflight(str(self.wt), session_id=_SESSION)
            other = engine.cmd_finish_preflight(str(self.wt), session_id="sess-other")
            anonymous = engine.cmd_finish_preflight(str(self.wt))
        assert isinstance(claimed.details, dict) and isinstance(other.details, dict)
        assert isinstance(anonymous.details, dict)
        self.assertEqual(claimed.details["test_argv"], ["--skip-tests"])
        self.assertEqual(other.details["test_argv"], ["--test-cmd", "just test"])
        self.assertEqual(anonymous.details["test_argv"], ["--test-cmd", "just test"])

    def test_engine_cli_takes_the_session_from_the_environment(self) -> None:
        self._claim()
        self._enable_tests()
        (self.repo / "Justfile").write_text("test:\n    true\n")
        script = Path(__file__).resolve().parent.parent / "scripts" / "worktree_engine.py"
        for session, expected in ((_SESSION, ["--skip-tests"]), ("sess-other", ["--test-cmd", "just test"])):
            with self.subTest(session=session):
                env = {**self._env(switch=None), sd.SESSION_ENV: session}
                proc = subprocess.run(
                    [sys.executable, str(script), "finish-preflight", "--worktree", str(self.wt)],
                    capture_output=True,
                    text=True,
                    cwd=str(self.repo),
                    env=env,
                )
                self.assertEqual(json.loads(proc.stdout)["details"]["test_argv"], expected, proc.stderr)


if __name__ == "__main__":
    unittest.main()
