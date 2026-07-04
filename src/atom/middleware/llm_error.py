"""LLMErrorHandlingMiddleware — retry transient provider errors, normalize the rest.

Outermost model wrap: retries retryable failures (rate limits / 5xx / timeouts) with exponential
backoff, and on final failure returns a clean assistant-facing message instead of crashing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

_RETRYABLE_MARKERS = (
    "429", "500", "502", "503", "504", "overloaded", "rate limit", "rate_limit",
    "timeout", "timed out", "temporarily unavailable", "connection",
)


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (429, 500, 502, 503, 504):
        return True
    text = str(exc).lower()
    return any(m in text for m in _RETRYABLE_MARKERS)


class LLMErrorHandlingMiddleware(AgentMiddleware):
    def __init__(self, max_retries: int = 2, base_delay: float = 1.0, max_delay: float = 8.0):
        super().__init__()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def _fallback(self, exc: Exception, attempts: int) -> AIMessage:
        return AIMessage(
            content=f"I couldn't reach the model after {attempts} attempt(s) due to a provider "
            f"error ({type(exc).__name__}: {exc}). Please retry."
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        delay = self.base_delay
        for attempt in range(self.max_retries + 1):
            try:
                return handler(request)
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries or not _is_retryable(exc):
                    return self._fallback(exc, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, self.max_delay)

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        delay = self.base_delay
        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries or not _is_retryable(exc):
                    return self._fallback(exc, attempt + 1)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_delay)
