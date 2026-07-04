"""SandboxMiddleware — acquires the per-thread LocalSandbox and registers it.

Runs right after ThreadDataMiddleware. This is the **docker seam**: swap ``provider`` for a
``DockerSandboxProvider`` in Phase 2 and nothing else in the chain changes.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware

from atom.middleware.context import ctx_dict
from atom.sandbox import registry
from atom.sandbox.paths import thread_paths_from_context
from atom.sandbox.provider import LocalSandboxProvider


class SandboxMiddleware(AgentMiddleware):
    def __init__(self, provider: LocalSandboxProvider, *, home: str | None = None):
        super().__init__()
        self.provider = provider
        self.home = home

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = ctx_dict(runtime)
        tp = thread_paths_from_context(ctx, self.home)
        sandbox = self.provider.acquire(tp)
        registry.register(tp.thread_id, sandbox)
        return None

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Release the sandbox at turn end so a long-lived process doesn't retain one per thread_id.
        # A resumed turn re-acquires (idempotent). Subagents finish within the turn, so this is safe.
        tid = ctx_dict(runtime).get("thread_id")
        if tid:
            self.provider.release(tid)
            registry.unregister(tid)
        return None
