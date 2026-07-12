"""WorkerLease: cross-process (and cross-handle) mutual exclusion via flock."""
from __future__ import annotations

import pytest
from pathlib import Path

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


def test_acquire_propagates_env_error_not_swallowed_as_contention(tmp_path):
    # A genuine filesystem error (parent path is a FILE, so the lock dir can't be
    # created) must propagate as OSError, NOT be swallowed and returned as False
    # (which callers would misread as "someone else holds the lease").
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                       # a file where a directory is needed
    lease = WorkerLease(blocker / "sub" / "worker.lock")
    with pytest.raises(OSError):
        lease.acquire()
