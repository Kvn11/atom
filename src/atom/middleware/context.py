"""Helper for reading the per-run WorkspaceContext off a middleware ``Runtime``."""

from __future__ import annotations

from typing import Any


def ctx_dict(runtime: Any) -> dict[str, Any]:
    """Return the run's context as a plain dict (WorkspaceContext), or ``{}``."""
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        return ctx
    if ctx is None:
        return {}
    # Fallback: a dataclass-like context object.
    return {k: getattr(ctx, k) for k in dir(ctx) if not k.startswith("_") and not callable(getattr(ctx, k))}
