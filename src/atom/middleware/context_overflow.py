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
    n = _approx_tokens(_msg_text(m)) + 4  # small per-message overhead
    tool_calls = getattr(m, "tool_calls", None)
    if tool_calls:
        # tool-call args carry real weight though `content` is often empty; over-counting is the
        # safe direction for an emergency trimmer (drop more, never under-trim the real culprit).
        n += _approx_tokens(str(tool_calls))
    return n


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

    # protected (system + pinned-instruction) are always a FRONT PREFIX in the real call path:
    # PinnedSummarizationMiddleware injects the pin at index 0/1 (see compaction.py), so
    # `protected + kept` preserves chronological order. If pins were ever injected mid-history,
    # this concatenation would reorder — revisit here.
    result = protected + kept
    return [_truncate_message(m, approx_budget, single_msg_marker) for m in result]


class ContextOverflowMiddleware(AgentMiddleware):
    """Innermost wrap_model_call: on a context-overflow provider error, deterministically shrink the
    request and retry — halving the budget each round — then raise ContextOverflowError. It handles
    ONLY overflow (re-raises anything else), leaving transient retry to LLMErrorHandlingMiddleware,
    which wraps it."""

    _TRIM_MARKER = (
        "\n\n[atom: context-overflow emergency trim — {elided} of {total} chars elided from this "
        "message to fit the model's context window]\n\n"
    )

    def __init__(self, *, context_window: int, max_attempts: int = 3,
                 target_ratio: float = 0.5, enabled: bool = True):
        super().__init__()
        self.context_window = context_window
        self.max_attempts = max_attempts
        self.target_ratio = target_ratio
        self.enabled = enabled

    def _budget(self, attempt: int) -> int:
        return max(1, int(self.context_window * self.target_ratio / (2 ** attempt)))

    def _trim(self, request: Any, attempt: int) -> Any:
        trimmed = trim_messages_to_budget(
            request.messages, self._budget(attempt), single_msg_marker=self._TRIM_MARKER
        )
        return request.override(messages=trimmed)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            return handler(request)
        except Exception as exc:  # noqa: BLE001
            if not is_context_overflow(exc):
                raise
            if not self.enabled:
                raise ContextOverflowError(limit=self.context_window, attempts=0, original=exc)
            last = exc
            for attempt in range(self.max_attempts):
                try:
                    return handler(self._trim(request, attempt))
                except Exception as e2:  # noqa: BLE001
                    if not is_context_overflow(e2):
                        raise
                    last = e2
            raise ContextOverflowError(
                limit=self.context_window, attempts=self.max_attempts, original=last
            )

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            if not is_context_overflow(exc):
                raise
            if not self.enabled:
                raise ContextOverflowError(limit=self.context_window, attempts=0, original=exc)
            last = exc
            for attempt in range(self.max_attempts):
                try:
                    return await handler(self._trim(request, attempt))
                except Exception as e2:  # noqa: BLE001
                    if not is_context_overflow(e2):
                        raise
                    last = e2
            raise ContextOverflowError(
                limit=self.context_window, attempts=self.max_attempts, original=last
            )
