"""Custom LangGraph state reducers used by :class:`atom.state.ThreadState`."""

from __future__ import annotations

from typing import Any

# Sentinel to reset viewed_images (e.g. after a vision middleware injects & consumes them).
CLEAR = "__clear__"


def merge_artifacts(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """Append new artifacts, de-duplicating by ``path`` (dicts) or identity (str), order-preserving."""
    left = left or []
    right = right or []
    seen: set[str] = set()
    out: list[Any] = []
    for item in [*left, *right]:
        key = item.get("path") if isinstance(item, dict) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def merge_viewed_images(
    left: dict[str, Any] | None, right: dict[str, Any] | None | str
) -> dict[str, Any]:
    """Merge image payloads keyed by path. ``right == CLEAR`` resets the map."""
    if right == CLEAR:
        return {}
    merged = dict(left or {})
    if isinstance(right, dict):
        merged.update(right)
    return merged


def merge_promoted(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge deferred-tool promotion records ``{"names": [...], "catalog_hash": str}``.

    Names are unioned; the catalog hash from the newer (right) write wins. A reducer is required
    because two parallel ``search_tools`` calls write this channel in one super-step.
    """
    left = left or {}
    right = right or {}
    names = sorted(set(left.get("names", [])) | set(right.get("names", [])))
    out: dict[str, Any] = {"names": names}
    catalog_hash = right.get("catalog_hash") or left.get("catalog_hash")
    if catalog_hash is not None:
        out["catalog_hash"] = catalog_hash
    return out


def merge_name_list(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Union two lists of names (sorted, deduped) — safe under concurrent writes."""
    return sorted(set(left or []) | set(right or []))


def merge_usage(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    """Add token-usage counters. Additive so parent model steps and subagent deltas accumulate
    (and so parallel delegate_task usage writes in one super-step don't collide)."""
    left = left or {}
    right = right or {}
    return {k: int(left.get(k, 0)) + int(right.get(k, 0)) for k in set(left) | set(right)}
