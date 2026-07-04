"""Shared helpers for tools that reach into per-thread runtime state."""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime

from atom.sandbox import registry
from atom.sandbox.provider import LocalSandbox


def thread_id_of(runtime: Any) -> str | None:
    """Extract the run's thread_id from a Runtime / ToolRuntime.

    Primary source is ``runtime.context['thread_id']`` (the WorkspaceContext, shared across the
    run); falls back to ``runtime.config['configurable']['thread_id']`` for tools.
    """
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict) and ctx.get("thread_id"):
        return ctx["thread_id"]
    if ctx is not None and getattr(ctx, "thread_id", None):
        return ctx.thread_id
    config = getattr(runtime, "config", None)
    if isinstance(config, dict):
        return config.get("configurable", {}).get("thread_id")
    return None


def get_sandbox(runtime: ToolRuntime) -> LocalSandbox:
    """Fetch the live sandbox for this run (registered by SandboxMiddleware.before_agent)."""
    sandbox = registry.get(thread_id_of(runtime))
    if sandbox is None:
        raise RuntimeError(
            "Sandbox is not initialized for this thread. SandboxMiddleware.before_agent must run "
            "before any filesystem tool."
        )
    return sandbox
