"""WorkerLease: cross-process (and cross-handle) mutual exclusion via flock."""
from __future__ import annotations

from atom.workflow.lease import WorkerLease


def test_lease_is_mutually_exclusive_and_reacquirable(tmp_path):
    path = tmp_path / "queue" / "worker.lock"
    a = WorkerLease(path)
    b = WorkerLease(path)

    assert a.acquire() is True          # first holder wins
    assert a.acquire() is True          # idempotent for the same handle
    assert b.acquire() is False         # second handle is denied while a holds it

    a.release()
    assert b.acquire() is True          # freed -> b can take it now
    b.release()


def test_release_without_acquire_is_safe(tmp_path):
    WorkerLease(tmp_path / "q" / "w.lock").release()   # must not raise
