"""Process-local registry mapping ``thread_id`` -> live :class:`LocalSandbox`.

The sandbox holds a threading.Lock and is not serializable, so it cannot live in checkpointed
graph state. Instead ``SandboxMiddleware.before_agent`` registers it here and tools/middleware
look it up by the run's ``thread_id`` (available on ``runtime.context``).
"""

from __future__ import annotations

import threading

from atom.sandbox.provider import LocalSandbox

_lock = threading.Lock()
_sandboxes: dict[str, LocalSandbox] = {}


def register(thread_id: str, sandbox: LocalSandbox) -> None:
    with _lock:
        _sandboxes[thread_id] = sandbox


def get(thread_id: str | None) -> LocalSandbox | None:
    if not thread_id:
        return None
    with _lock:
        return _sandboxes.get(thread_id)


def unregister(thread_id: str) -> None:
    with _lock:
        _sandboxes.pop(thread_id, None)
