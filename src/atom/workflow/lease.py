"""Cross-process single-drainer lease via POSIX flock.

flock is tied to the open file description and is released automatically by the OS when the
holding process dies, so a crashed holder never leaves a stale lock. Two distinct handles
(even in one process) contend, which is what makes "only one drainer" hold across processes.
POSIX only (macOS + Linux); the standalone-drain path is unsupported on Windows.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class WorkerLease:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lease without blocking.

        Returns True if the lease is held (including if already held by this handle).
        Returns False ONLY when another holder currently owns the lock (flock contention).

        Errors from creating or opening the lock file propagate as OSError, which is
        distinct from contention and signals a broken-filesystem condition that should
        fail loudly rather than be silently swallowed.
        """
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
