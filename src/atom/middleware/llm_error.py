"""LLMErrorHandlingMiddleware + shared retry core.

Transient provider errors (rate limits / 5xx / overload / timeouts) are retried with
exponential backoff + full jitter, then — on exhaustion or a non-retryable error — a
``ProviderUnavailableError`` is raised (callers decide how to surface it). The same core
powers ``RetryingModel``, a proxy giving out-of-band model calls (e.g. compaction's
summarizer) the identical retry policy.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

T = TypeVar("T")

_RETRYABLE_MARKERS = (
    "overloaded", "rate limit", "rate_limit", "timeout", "timed out",
    "temporarily unavailable", "unavailable", "connection",
    "resource_exhausted", "resource exhausted", "internal", "deadline",
    "busy", "quota", "try again",
)

# Bare numeric HTTP status codes, digit-bounded so a real status (e.g. "502 Bad Gateway")
# matches but a digit-substring inside a larger number (e.g. "250000 tokens") does not.
_NUMERIC_STATUS_RE = re.compile(r"(?<!\d)(?:429|500|502|503|504|529)(?!\d)")


class ProviderUnavailableError(Exception):
    """Raised when a model call still fails after the retry budget is exhausted (or on a
    non-retryable error). Carries the originating exception and the attempt count."""

    def __init__(self, original: Exception, attempts: int):
        self.original = original
        self.attempts = attempts
        super().__init__(
            f"provider unavailable after {attempts} attempt(s): "
            f"{type(original).__name__}: {original}"
        )


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 20
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: bool = True


def is_retryable(exc: Exception) -> bool:
    """True if ``exc`` looks like a transient provider error worth retrying."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError)):
        return True
    status = getattr(exc, "status_code", None)      # anthropic/openai
    if status is None:
        status = getattr(exc, "code", None)         # google-genai
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    text = str(exc).lower()
    if _NUMERIC_STATUS_RE.search(text):
        return True
    return any(m in text for m in _RETRYABLE_MARKERS)


_OVERFLOW_MARKERS = (
    "input token count",                      # google-genai
    "token count exceeds",                    # google-genai
    "exceeds the maximum number of tokens",   # google-genai
    "prompt is too long",                     # anthropic
    "context_length_exceeded",                # openai
    "maximum context length",                 # openai
    "reduce the length of the messages",      # openai
    "context window",
    "context length",
    "too many tokens",
    "input is too long",
    "maximum number of tokens",
)


def is_context_overflow(exc: Exception) -> bool:
    """True if ``exc`` is a permanent-for-this-input context/token overflow — a 4xx the model will
    reject again unless the input shrinks. Disjoint from :func:`is_retryable`: overflow is never a
    transient retry, so it must not be looped with backoff (futile) nor mislabeled as an outage."""
    text = str(exc).lower()
    return any(m in text for m in _OVERFLOW_MARKERS)


def _backoff_ceiling(attempt: int, policy: RetryPolicy) -> float:
    return min(policy.base_delay * (2 ** attempt), policy.max_delay)


def run_with_retry_sync(
    call: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Any] = time.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> T:
    for attempt in range(policy.max_retries + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            if attempt >= policy.max_retries or not is_retryable(exc):
                raise ProviderUnavailableError(exc, attempt + 1) from exc
            ceiling = _backoff_ceiling(attempt, policy)
            sleep(rand(0.0, ceiling) if policy.jitter else ceiling)
    raise AssertionError("unreachable")  # pragma: no cover


async def run_with_retry_async(
    acall: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> T:
    for attempt in range(policy.max_retries + 1):
        try:
            return await acall()
        except Exception as exc:  # noqa: BLE001
            if attempt >= policy.max_retries or not is_retryable(exc):
                raise ProviderUnavailableError(exc, attempt + 1) from exc
            ceiling = _backoff_ceiling(attempt, policy)
            await sleep(rand(0.0, ceiling) if policy.jitter else ceiling)
    raise AssertionError("unreachable")  # pragma: no cover


class RetryingModel:
    """Proxy wrapping a BaseChatModel so its ``invoke``/``ainvoke`` calls get ``policy``'s
    retry/backoff. All other attribute access delegates to the wrapped model."""

    def __init__(self, inner: BaseChatModel, policy: RetryPolicy):
        self._inner = inner
        self._policy = policy

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return run_with_retry_sync(lambda: self._inner.invoke(*args, **kwargs), self._policy)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return await run_with_retry_async(
            lambda: self._inner.ainvoke(*args, **kwargs), self._policy
        )

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for attrs not found normally; guard the proxy's own slots
        # so an access before __init__ finishes (e.g. copy/pickle) can't infinitely recurse.
        if name in ("_inner", "_policy"):
            raise AttributeError(name)
        return getattr(self._inner, name)


class LLMErrorHandlingMiddleware(AgentMiddleware):
    """Outermost ``wrap_model_call``: retry transient provider errors with backoff, then raise
    ``ProviderUnavailableError`` on exhaustion (or on a non-retryable error)."""

    def __init__(self, policy: RetryPolicy | None = None):
        super().__init__()
        self.policy = policy or RetryPolicy()

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return run_with_retry_sync(lambda: handler(request), self.policy)

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return await run_with_retry_async(lambda: handler(request), self.policy)
