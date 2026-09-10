"""Multiprocessing regression test for rotate_audit_log()'s LOCK_NB+re-stat design.

Mirrors the POC harness that INVALIDATED an unsynchronized cascade-rename
rotation under concurrency (see the plan's ``poc_evidence``). The POC's own
proven failure mode -- lost generations and unhandled exceptions when
multiple processes race a detected-oversize rotation -- is regression-locked
here against the shipped design: a single non-blocking (``LOCK_NB``) flock
plus a mandatory re-stat after acquisition, rather than the POC's own tested
blocking ``LOCK_EX`` (required by AC4's prohibition on blocking locks/waits).
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path

import worktree_gate as gate

TRIALS = 20
WORKERS = 8


def _worker(path_str: str, max_bytes: int, backup_count: int) -> tuple[bool, float]:
    """Call rotate_audit_log() once and report success plus elapsed seconds."""
    path = Path(path_str)
    start = time.monotonic()
    try:
        gate.rotate_audit_log(path, max_bytes=max_bytes, backup_count=backup_count)
        ok = True
    except Exception:
        ok = False
    return ok, time.monotonic() - start


class AuditRotationConcurrencyTest(unittest.TestCase):
    """8-way concurrent rotation races, repeated across many trials."""

    def test_concurrent_rotation_is_lossless_and_non_blocking(self) -> None:
        """Concurrent rotation is lossless and non blocking."""
        max_bytes = 1000
        backup_count = 5
        ctx = multiprocessing.get_context("fork") if hasattr(os, "fork") else multiprocessing

        for trial in range(TRIALS):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "audit.log"
                marker = f"trial-{trial}".encode()
                content = marker + b"\n" + b"a" * (max_bytes - len(marker) - 1)
                path.write_bytes(content)

                with ctx.Pool(WORKERS) as pool:
                    results = pool.starmap(
                        _worker, [(str(path), max_bytes, backup_count)] * WORKERS
                    )

                oks = [ok for ok, _ in results]
                durations = [d for _, d in results]

                self.assertTrue(all(oks), f"trial {trial}: a worker raised")
                self.assertTrue(
                    all(d < 1.0 for d in durations),
                    f"trial {trial}: a worker blocked >= 1s: {durations}",
                )

                rotated = Path(tmp) / "audit.log.1"
                self.assertTrue(rotated.exists(), f"trial {trial}: no rotation happened")
                self.assertIn(marker, rotated.read_bytes())
                # Exactly one generation: a missing re-stat-after-lock would let a
                # second racer rotate an already-fresh file, producing .2.
                self.assertFalse(
                    (Path(tmp) / "audit.log.2").exists(),
                    f"trial {trial}: spurious extra generation created",
                )


if __name__ == "__main__":
    unittest.main()
