"""Reactive context-overflow recovery: deterministic hard-trim + retry, then a clean error.

The proactive SummarizationMiddleware keeps the last N messages verbatim and so cannot rescue a
single tool result larger than the window, a wrong profile window, or a summarizer that itself
overflows. This module is the emergency net: on a provider context-overflow error, shrink the
request deterministically and retry, letting the provider re-judge; give up with ContextOverflowError.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage

from atom.limits import truncate_text
from atom.middleware.llm_error import ContextOverflowError, is_context_overflow


def _approx_tokens(text: str) -> int:
    return (len(text) + 3) // 4  # ceil(chars / 4)


def _msg_text(m: Any) -> str:
    c = getattr(m, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            (b.get("text", "") or "") if isinstance(b, dict) else str(b) for b in c
        )
    return str(c)


def _msg_tokens(m: Any) -> int:
    return _approx_tokens(_msg_text(m)) + 4  # small per-message overhead


def _is_protected(m: Any) -> bool:
    if isinstance(m, SystemMessage):
        return True
    return getattr(m, "additional_kwargs", {}).get("lc_source") == "pinned_instruction"


def _drop_dangling_leading_tool_messages(msgs: list) -> list:
    """A ToolMessage at the FRONT of the kept window whose AIMessage (tool_call) was trimmed will
    400 the provider (tool_result without a preceding tool_use). Drop such leading ToolMessages."""
    out = list(msgs)
    while out and isinstance(out[0], ToolMessage):
        out.pop(0)
    return out


def _truncate_message(m: Any, approx_budget: int, marker_template: str) -> Any:
    if _msg_tokens(m) <= approx_budget:
        return m
    truncated = truncate_text(
        _msg_text(m), max_chars=max(0, approx_budget * 4), marker_template=marker_template
    )
    return m.model_copy(update={"content": truncated})


def trim_messages_to_budget(messages: list, approx_budget: int, *, single_msg_marker: str) -> list:
    """Deterministically shrink ``messages`` under ``approx_budget`` tokens (est. chars//4).

    Keeps system + pinned-instruction messages, drops oldest non-protected turns first, repairs a
    dangling leading ToolMessage, then truncates any single retained message still over budget."""
    if not messages:
        return messages
    protected = [m for m in messages if _is_protected(m)]
    rest = [m for m in messages if not _is_protected(m)]

    used = sum(_msg_tokens(m) for m in protected)
    kept_rev: list = []
    for m in reversed(rest):
        t = _msg_tokens(m)
        if kept_rev and used + t > approx_budget:
            break
        kept_rev.append(m)
        used += t
    kept = _drop_dangling_leading_tool_messages(list(reversed(kept_rev)))

    result = protected + kept
    return [_truncate_message(m, approx_budget, single_msg_marker) for m in result]
