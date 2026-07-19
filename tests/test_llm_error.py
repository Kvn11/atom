"""Retry core: detection, backoff/jitter, exhaustion→raise, RetryingModel, middleware."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from atom.middleware.llm_error import (
    ContextOverflowError,
    LLMErrorHandlingMiddleware,
    ProviderUnavailableError,
    RetryingModel,
    RetryPolicy,
    is_context_overflow,
    is_retryable,
    run_with_retry_async,
    run_with_retry_sync,
)


class _Anthropic(Exception):
    def __init__(self, status_code, msg=""):
        self.status_code = status_code
        super().__init__(msg)


class _Gemini(Exception):
    """google-genai style: HTTP status on .code, not .status_code."""
    def __init__(self, code, msg=""):
        self.code = code
        super().__init__(msg)


# ---- is_retryable -------------------------------------------------------

def test_retryable_status_code_range():
    assert is_retryable(_Anthropic(429))
    assert is_retryable(_Anthropic(500))
    assert is_retryable(_Anthropic(529))          # Anthropic OverloadedError
    assert not is_retryable(_Anthropic(400, "bad request"))
    assert not is_retryable(_Anthropic(401, "unauthorized"))


def test_retryable_gemini_code_attribute():
    assert is_retryable(_Gemini(503, "UNAVAILABLE"))
    assert is_retryable(_Gemini(429, "RESOURCE_EXHAUSTED"))
    assert not is_retryable(_Gemini(404, "not found"))


def test_retryable_httpx_network_errors():
    assert is_retryable(httpx.ReadTimeout(""))     # empty str(exc)
    assert is_retryable(httpx.ConnectTimeout(""))
    assert is_retryable(httpx.ConnectError(""))


def test_retryable_string_markers():
    assert is_retryable(Exception("The model is overloaded"))
    assert is_retryable(Exception("Provider is busy, try again"))
    assert is_retryable(Exception("429 RESOURCE_EXHAUSTED: quota"))
    assert is_retryable(Exception("503 UNAVAILABLE"))
    assert not is_retryable(Exception("invalid api key"))


def test_retryable_bare_numeric_status_strings():
    assert is_retryable(Exception("429 Too Many Requests"))
    assert is_retryable(Exception("502 Bad Gateway"))
    assert is_retryable(Exception("HTTP 503"))
    # a digit-substring inside a larger number must NOT count as a status code
    assert not is_retryable(Exception("used 250000 tokens"))


def test_overflow_and_retryable_stay_disjoint_on_anthropic_token_count():
    exc = _Anthropic(400, "prompt is too long: 250000 tokens > 200000 maximum")
    assert is_context_overflow(exc) and not is_retryable(exc)


# ---- run_with_retry_sync ------------------------------------------------

def test_sync_success_first_try_no_sleep():
    slept = []
    out = run_with_retry_sync(lambda: 42, RetryPolicy(max_retries=3),
                              sleep=slept.append, rand=lambda a, b: b)
    assert out == 42 and slept == []


def test_sync_success_after_retries():
    calls = {"n": 0}
    slept = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Anthropic(503)
        return "ok"

    out = run_with_retry_sync(flaky, RetryPolicy(max_retries=5, base_delay=1.0, max_delay=30.0),
                              sleep=slept.append, rand=lambda a, b: b)
    assert out == "ok" and calls["n"] == 3
    assert slept == [1.0, 2.0]           # full-jitter upper bounds for attempts 0,1


def test_sync_exhaustion_raises_provider_unavailable():
    def always():
        raise _Gemini(503, "UNAVAILABLE")

    with pytest.raises(ProviderUnavailableError) as ei:
        run_with_retry_sync(always, RetryPolicy(max_retries=3),
                            sleep=lambda d: None, rand=lambda a, b: 0.0)
    assert ei.value.attempts == 4                     # max_retries + 1 attempts
    assert isinstance(ei.value.original, _Gemini)


def test_sync_non_retryable_raises_immediately():
    slept = []

    def bad():
        raise _Anthropic(400, "bad request")

    with pytest.raises(ProviderUnavailableError) as ei:
        run_with_retry_sync(bad, RetryPolicy(max_retries=5), sleep=slept.append)
    assert ei.value.attempts == 1 and slept == []


def test_sync_jitter_off_uses_ceiling():
    slept = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Anthropic(500)
        return "ok"

    run_with_retry_sync(flaky, RetryPolicy(max_retries=5, base_delay=2.0, max_delay=30.0, jitter=False),
                        sleep=slept.append)
    assert slept == [2.0, 4.0]


# ---- run_with_retry_async -----------------------------------------------

async def test_async_success_after_retries():
    calls = {"n": 0}
    slept = []

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _Gemini(503)
        return "ok"

    async def fake_sleep(d):
        slept.append(d)

    out = await run_with_retry_async(flaky, RetryPolicy(max_retries=5, base_delay=1.0),
                                     sleep=fake_sleep, rand=lambda a, b: b)
    assert out == "ok" and slept == [1.0]


async def test_async_exhaustion_raises():
    async def always():
        raise _Anthropic(503)

    async def fake_sleep(d):
        return None

    with pytest.raises(ProviderUnavailableError) as ei:
        await run_with_retry_async(always, RetryPolicy(max_retries=2),
                                   sleep=fake_sleep, rand=lambda a, b: 0.0)
    assert ei.value.attempts == 3


# ---- RetryingModel ------------------------------------------------------

class _FlakyModel:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0
        self.some_attr = "delegated"

    def invoke(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _Gemini(503)
        return "RESP"

    async def ainvoke(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _Gemini(503)
        return "ARESP"


def test_retrying_model_retries_invoke_and_delegates_attrs():
    inner = _FlakyModel(fail_times=2)
    m = RetryingModel(inner, RetryPolicy(max_retries=5, base_delay=0.0, max_delay=0.0))
    assert m.invoke("x") == "RESP" and inner.calls == 3
    assert m.some_attr == "delegated"         # __getattr__ passthrough


async def test_retrying_model_retries_ainvoke():
    inner = _FlakyModel(fail_times=1)
    m = RetryingModel(inner, RetryPolicy(max_retries=5, base_delay=0.0, max_delay=0.0))
    assert await m.ainvoke("x") == "ARESP" and inner.calls == 2


# ---- LLMErrorHandlingMiddleware -----------------------------------------

async def test_middleware_awrap_retries_then_succeeds():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Gemini(503)
        return "OUT"

    mw = LLMErrorHandlingMiddleware(RetryPolicy(max_retries=5, base_delay=0.0, max_delay=0.0))
    assert await mw.awrap_model_call("req", handler) == "OUT" and calls["n"] == 3


async def test_middleware_awrap_raises_on_exhaustion():
    async def handler(request):
        raise _Anthropic(503)

    mw = LLMErrorHandlingMiddleware(RetryPolicy(max_retries=2, base_delay=0.0, max_delay=0.0))
    with pytest.raises(ProviderUnavailableError):
        await mw.awrap_model_call("req", handler)


def test_middleware_default_policy_is_20_retries():
    mw = LLMErrorHandlingMiddleware()
    assert mw.policy.max_retries == 20 and mw.policy.base_delay == 1.0
    assert mw.policy.max_delay == 30.0 and mw.policy.jitter is True


# ---- is_context_overflow ------------------------------------------------

def test_overflow_detects_gemini():
    exc = _Gemini(400, "INVALID_ARGUMENT. The input token count (1052342) exceeds the maximum "
                       "number of tokens allowed (1048576).")
    assert is_context_overflow(exc) and not is_retryable(exc)


def test_overflow_detects_anthropic():
    exc = _Anthropic(400, "prompt is too long: 250000 tokens > 200000 maximum")
    assert is_context_overflow(exc) and not is_retryable(exc)


def test_overflow_detects_openai():
    exc = Exception("Error code: 400 - context_length_exceeded: maximum context length is 128000 tokens")
    assert is_context_overflow(exc) and not is_retryable(exc)


def test_overflow_false_for_transient_and_unrelated():
    assert not is_context_overflow(_Anthropic(429, "rate limit exceeded"))
    assert not is_context_overflow(_Gemini(503, "UNAVAILABLE"))
    assert not is_context_overflow(_Anthropic(400, "invalid api key"))
    assert not is_context_overflow(_Anthropic(400, "bad request"))


# ---- ContextOverflowError --------------------------------------------------

def test_context_overflow_error_passes_through_sync_retry_core():
    def raises():
        raise ContextOverflowError(limit=1000, attempts=3, original=ValueError("too big"))
    with pytest.raises(ContextOverflowError) as ei:
        run_with_retry_sync(raises, RetryPolicy(max_retries=5), sleep=lambda d: None)
    assert ei.value.limit == 1000 and ei.value.attempts == 3


async def test_context_overflow_error_passes_through_async_retry_core():
    async def raises():
        raise ContextOverflowError(limit=42, attempts=1, original=ValueError("x"))

    async def fake_sleep(d):
        return None
    with pytest.raises(ContextOverflowError):
        await run_with_retry_async(raises, RetryPolicy(max_retries=5), sleep=fake_sleep)
