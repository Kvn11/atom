# Error-Handling Hardening & Config-Driven Retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retry transient provider errors (esp. Gemini "Provider is busy"/503/429) with exponential backoff up to 20 attempts, then *fail* the task; and close every other confirmed error-handling gap so a transient hiccup can never silently corrupt a run, destroy history, or wedge a run.

**Architecture:** A shared retry core in `middleware/llm_error.py` (config-driven `RetryPolicy`, improved `is_retryable`, `run_with_retry_sync/async`, a `RetryingModel` proxy, and an `LLMErrorHandlingMiddleware` that *raises* `ProviderUnavailableError` on exhaustion) is wired into the lead agent, sub-agents, and the summarizer. The engine gains lifecycle guards; the CLI surfaces failures cleanly. DeerFlow branding is scrubbed from user-facing strings.

**Tech Stack:** Python 3.11+, LangChain v1 (`AgentMiddleware`, `SummarizationMiddleware`), pydantic, httpx, typer, pytest (`asyncio_mode="auto"`).

## Global Constraints

- **No new dependencies.** `httpx` is already available (transitive via langchain); everything else is stdlib/existing.
- **Retry defaults (exact):** `max_retries=20`, `base_delay=1.0`s, `max_delay=30.0`s, `jitter=True` (full jitter: `sleep = rand(0, min(base*2**attempt, cap))`).
- **On exhaustion or a non-retryable error → raise `ProviderUnavailableError`** (never return a fallback `AIMessage`). Message: `"provider unavailable after N attempt(s): <Type>: <msg>"`, carrying `.original` and `.attempts`.
- **Compaction on summary failure → skip compaction, keep messages** (return `None`); never commit a broken summary.
- **SDK-level retry disabled:** `build_model` sets `max_retries=1` (Gemini's default is 6; `0` means "Google default 5", so use `1`) and a `timeout=120.0` per-call backstop. Both via `setdefault` (overridable).
- **Leave `inspo.md` untouched** (it is a study *about* DeerFlow). Scrub only `README.md`, `pyproject.toml`, `src/atom/__init__.py`, `src/atom/cli.py`.
- **Naming:** `verb_noun` for functions; match surrounding style.
- **Run tests with** `.venv/bin/python -m pytest` (NOT bare `pytest`).
- **Never break** `from atom.middleware.llm_error import LLMErrorHandlingMiddleware` (agent.py relies on it).

---

## File Structure

- `src/atom/middleware/llm_error.py` — **rewritten**: retry core + `RetryingModel` + middleware (Task 1).
- `src/atom/config/schema.py` — add `RetryConfig` + `AtomConfig.retry` (Task 2).
- `src/atom/agent.py` — build `RetryPolicy` from config; wrap summarizer in `RetryingModel`; pass policy to middleware + `SubagentRunner` (Task 2).
- `src/atom/subagent.py` — `SubagentRunner.retry` field; add `LLMErrorHandlingMiddleware` to `_child_middleware` (Task 3).
- `src/atom/middleware/compaction.py` — fail-closed on the summary-error sentinel (Task 4).
- `src/atom/models/registry.py` — `max_retries=1` + `timeout=120` in `build_model` (Task 5).
- `src/atom/workflow/engine.py` — guard initial load, log silent paths, handle `CancelledError` (Task 6).
- `src/atom/cli.py` + `README.md` + `pyproject.toml` + `src/atom/__init__.py` — CLI error handling + DeerFlow scrub (Task 7).
- Tests: new `tests/test_llm_error.py`; extend `tests/test_workflow_config.py`, `tests/test_subagent.py`, `tests/test_compaction.py`, `tests/test_models.py`, `tests/test_workflow_engine.py`, `tests/test_cli.py`.

---

### Task 1: Shared retry core (`middleware/llm_error.py`)

**Files:**
- Rewrite: `src/atom/middleware/llm_error.py`
- Create: `tests/test_llm_error.py`

**Interfaces:**
- Produces:
  - `class ProviderUnavailableError(Exception)` — `__init__(self, original: Exception, attempts: int)`; attrs `.original`, `.attempts`.
  - `@dataclass(frozen=True) class RetryPolicy` — `max_retries: int = 20`, `base_delay: float = 1.0`, `max_delay: float = 30.0`, `jitter: bool = True`.
  - `def is_retryable(exc: Exception) -> bool`
  - `def run_with_retry_sync(call, policy, *, sleep=time.sleep, rand=random.uniform) -> Any`
  - `async def run_with_retry_async(acall, policy, *, sleep=asyncio.sleep, rand=random.uniform) -> Any`
  - `class RetryingModel` — `__init__(self, inner: BaseChatModel, policy: RetryPolicy)`; `.invoke`, `.ainvoke`, `__getattr__` delegate.
  - `class LLMErrorHandlingMiddleware(AgentMiddleware)` — `__init__(self, policy: RetryPolicy | None = None)`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_llm_error.py`:

```python
"""Retry core: detection, backoff/jitter, exhaustion→raise, RetryingModel, middleware."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from atom.middleware.llm_error import (
    LLMErrorHandlingMiddleware,
    ProviderUnavailableError,
    RetryingModel,
    RetryPolicy,
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_llm_error.py -q`
Expected: FAIL / import errors (the new names don't exist yet).

- [ ] **Step 3: Rewrite `src/atom/middleware/llm_error.py`** with the full contents:

```python
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
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

T = TypeVar("T")

_RETRYABLE_MARKERS = (
    "429", "500", "502", "503", "504", "529",
    "overloaded", "rate limit", "rate_limit", "timeout", "timed out",
    "temporarily unavailable", "unavailable", "connection",
    "resource_exhausted", "resource exhausted", "internal", "deadline",
    "busy", "quota", "try again",
)


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
    return any(m in text for m in _RETRYABLE_MARKERS)


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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_llm_error.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Confirm no importer broke**

Run: `.venv/bin/python -m pytest tests/test_agent_smoke.py tests/test_middleware.py -q`
Expected: PASS (agent still builds with `LLMErrorHandlingMiddleware()`).

- [ ] **Step 6: Commit**

```bash
git add src/atom/middleware/llm_error.py tests/test_llm_error.py
git commit -m "feat(resilience): config-driven retry core — raise on exhaustion, better detection"
```

---

### Task 2: `RetryConfig` + lead-agent wiring (`config/schema.py`, `agent.py`)

**Files:**
- Modify: `src/atom/config/schema.py` (add `RetryConfig`, `AtomConfig.retry`)
- Modify: `src/atom/agent.py` (`build_lead_agent`, `_build_summarizer`, `_build_middlewares`)
- Test: `tests/test_workflow_config.py`, `tests/test_agent_smoke.py`

**Interfaces:**
- Consumes (Task 1): `RetryPolicy`, `RetryingModel`, `LLMErrorHandlingMiddleware`.
- Produces: `AtomConfig.retry: RetryConfig`; `_build_summarizer(profile, prepared, policy) -> BaseChatModel` (returns a `RetryingModel`); `_build_middlewares(..., retry_policy: RetryPolicy)`.

- [ ] **Step 1: Write the failing config test** — append to `tests/test_workflow_config.py`:

```python
def test_retry_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.retry.max_retries == 20
    assert cfg.retry.base_delay == 1.0
    assert cfg.retry.max_delay == 30.0
    assert cfg.retry.jitter is True


def test_retry_config_override():
    from atom.config.schema import RetryConfig
    rc = RetryConfig(max_retries=5, base_delay=0.5, max_delay=10.0, jitter=False)
    assert rc.max_retries == 5 and rc.base_delay == 0.5
    assert rc.max_delay == 10.0 and rc.jitter is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py -q`
Expected: FAIL (`AttributeError`/`ImportError` — no `retry` / `RetryConfig`).

- [ ] **Step 3: Add `RetryConfig` to `src/atom/config/schema.py`**

After the `WorkflowConfig` class (around line 62), add:

```python
class RetryConfig(_Base):
    # Transient-provider-error retry for every model call (lead + sub-agents + summarizer).
    # 20 attempts with full-jitter exponential backoff, then the task fails.
    max_retries: int = 20
    base_delay: float = 1.0     # seconds; first backoff
    max_delay: float = 30.0     # seconds; per-attempt cap
    jitter: bool = True         # full jitter on every delay
```

In `AtomConfig` (after `workflow: WorkflowConfig = ...`, around line 120), add the field:

```python
    retry: RetryConfig = Field(default_factory=RetryConfig)
```

- [ ] **Step 4: Run to verify the config test passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing agent-wiring test** — append to `tests/test_agent_smoke.py`:

```python
def test_summarizer_is_retry_wrapped(base_config):
    from atom.agent import _build_summarizer
    from atom.config.schema import AgentProfile
    from atom.middleware.llm_error import RetryPolicy, RetryingModel
    from tests.conftest import make_prepared
    from langchain_core.messages import AIMessage

    prepared = make_prepared([AIMessage(content="x")])
    prof = AgentProfile(model="haiku")          # no summarizer_model -> reuse lead model
    summ = _build_summarizer(prof, prepared, RetryPolicy(max_retries=3))
    assert isinstance(summ, RetryingModel)


def test_lead_middleware_uses_config_retry_policy(base_config):
    from atom.agent import build_lead_agent
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware
    from tests.conftest import make_prepared
    from langchain_core.messages import AIMessage

    base_config.retry.max_retries = 7
    prepared = make_prepared([AIMessage(content="x")])
    agent = build_lead_agent(base_config, "default", prepared=prepared)
    mws = agent.middleware if hasattr(agent, "middleware") else []
    # Fall back to introspecting the builder directly if the compiled agent hides middleware:
    from atom.agent import _build_middlewares, _build_summarizer
    from atom.middleware.llm_error import RetryPolicy
    from atom.sandbox.provider import LocalSandboxProvider
    from atom.library import load_library
    policy = RetryPolicy(max_retries=base_config.retry.max_retries)
    summ = _build_summarizer(base_config.profile("default"), prepared, policy)
    chain = _build_middlewares(
        base_config, base_config.profile("default"), prepared,
        LocalSandboxProvider(bash_enabled=True), str(base_config.home), summ,
        load_library(str(base_config.home)), None, retry_policy=policy, skill_catalog=[],
    )
    llm_mws = [m for m in chain if isinstance(m, LLMErrorHandlingMiddleware)]
    assert llm_mws and llm_mws[0].policy.max_retries == 7
```

> Note to implementer: if `build_lead_agent`'s compiled agent does not expose `.middleware`, the second half of the test above (calling `_build_middlewares` directly) is the authoritative assertion — keep it; the first two lines are a harmless probe. Verify the exact `_build_middlewares` signature you land in Step 6 matches the call here (adjust the keyword args if you renamed anything).

- [ ] **Step 6: Wire the policy through `src/atom/agent.py`**

In `build_lead_agent`, immediately after `profile = cfg.profile(profile_name)` (around line 106), build the policy and pass it in. Replace the summarizer construction and `_build_middlewares` call:

```python
    from atom.middleware.llm_error import RetryPolicy
    retry_policy = RetryPolicy(
        max_retries=cfg.retry.max_retries, base_delay=cfg.retry.base_delay,
        max_delay=cfg.retry.max_delay, jitter=cfg.retry.jitter,
    )
```

Change the `summarizer = _build_summarizer(profile, prepared)` line (≈147) to:

```python
    summarizer = _build_summarizer(profile, prepared, retry_policy)
```

Change the `_build_middlewares(...)` call (≈168) to pass `retry_policy`:

```python
    middleware = _build_middlewares(
        cfg, profile, prepared, provider, home, summarizer, library, mw_trace,
        skill_catalog=skill_catalog, retry_policy=retry_policy,
    )
```

Update `_build_summarizer` (≈184) to wrap in `RetryingModel`:

```python
def _build_summarizer(profile: AgentProfile, prepared: PreparedModel, policy) -> BaseChatModel:
    """Cheap model for compaction + title, wrapped so its out-of-band calls get retry/backoff."""
    from atom.middleware.llm_error import RetryingModel

    base = build_model(profile.summarizer_model, thinking="off") if profile.summarizer_model else prepared.model
    return RetryingModel(base, policy)
```

Update `_build_middlewares` signature (≈191) to accept `retry_policy` (add it to the keyword-only section after `skill_catalog`):

```python
def _build_middlewares(
    cfg: AtomConfig,
    profile: AgentProfile,
    prepared: PreparedModel,
    provider: LocalSandboxProvider,
    home: str,
    summarizer: BaseChatModel,
    library: LibraryIndex,
    trace: dict | None = None,
    *,
    skill_catalog: list[dict] | None = None,
    retry_policy=None,
) -> list[AgentMiddleware]:
```

Inside `_build_middlewares`, after `max_sub = clamp_concurrency(...)` (≈228), normalize the policy and pass it to the runner:

```python
    from atom.middleware.llm_error import RetryPolicy
    policy = retry_policy or RetryPolicy()
```

Change the `SubagentRunner(...)` construction (≈237) to add `retry=policy` (place it beside `observability=cfg.observability`):

```python
        retry=policy,
```

Change `LLMErrorHandlingMiddleware()` (≈274) to:

```python
        LLMErrorHandlingMiddleware(policy),               # 5. outermost: retry, then raise on exhaustion
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_agent_smoke.py tests/test_workflow_config.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/atom/config/schema.py src/atom/agent.py tests/test_workflow_config.py tests/test_agent_smoke.py
git commit -m "feat(resilience): config-driven retry policy wired into lead agent + summarizer"
```

---

### Task 3: Sub-agent retry parity (`subagent.py`)

**Files:**
- Modify: `src/atom/subagent.py` (`SubagentRunner.retry` field; `_child_middleware`)
- Test: `tests/test_subagent.py`

**Interfaces:**
- Consumes (Task 1): `LLMErrorHandlingMiddleware`, `RetryPolicy`. Consumes (Task 2): agent.py passes `retry=policy` into `SubagentRunner`.
- Produces: `SubagentRunner.retry` field; `_child_middleware()` includes an `LLMErrorHandlingMiddleware`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_subagent.py`:

```python
def test_child_middleware_includes_llm_error_retry(atom_home):
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware, RetryPolicy
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=100_000, bash_enabled=False,
        retry=RetryPolicy(max_retries=9),
    )
    mws = runner._child_middleware()
    llm = [m for m in mws if isinstance(m, LLMErrorHandlingMiddleware)]
    assert llm and llm[0].policy.max_retries == 9


def test_child_middleware_retry_defaults_when_unset(atom_home):
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(atom_home), context_window=100_000,
                            bash_enabled=False)  # retry unset -> default policy
    llm = [m for m in runner._child_middleware() if isinstance(m, LLMErrorHandlingMiddleware)]
    assert llm and llm[0].policy.max_retries == 20
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_subagent.py -q -k llm_error`
Expected: FAIL (no `retry` field / no `LLMErrorHandlingMiddleware` in the child chain).

- [ ] **Step 3: Add the `retry` field to `SubagentRunner`** in `src/atom/subagent.py`

In the `@dataclass class SubagentRunner` field block (after `observability: Any = None`, ≈63), add:

```python
    retry: Any = None                    # RetryPolicy | None; None -> default 20-retry policy
```

- [ ] **Step 4: Add the middleware in `_child_middleware`**

In `_child_middleware` (≈88-115), add the import and prepend the retry middleware. Change the imports block and the final `mw += [...]`:

```python
    def _child_middleware(self) -> list:
        """Pin the delegated prompt and add resilience (retry, compaction, dangling-call repair,
        tool-error, loop detection) so long-running children survive transient provider errors,
        context overflow, and loops."""
        from atom.middleware.dangling_tool_call import DanglingToolCallMiddleware
        from atom.middleware.instruction_pin import InstructionPinMiddleware
        from atom.middleware.llm_error import LLMErrorHandlingMiddleware, RetryPolicy
        from atom.middleware.loop_detection import LoopDetectionMiddleware
        from atom.middleware.tool_error import ToolErrorHandlingMiddleware

        mw: list = [InstructionPinMiddleware(), DanglingToolCallMiddleware()]
        if self.summarizer is not None:
            from atom.middleware.compaction import build_compaction_middleware

            mw.append(
                build_compaction_middleware(
                    self.summarizer,
                    context_window=self.context_window,
                    ratio=self.compaction_ratio,
                    keep_messages=15,
                    trim_tokens=self.summary_input_tokens,
                    summary_prompt=self.summary_prompt,
                )
            )
        if self.skill_catalog or self.has_skill_library:
            from atom.middleware.skill_library import SkillLibraryMiddleware

            mw.append(SkillLibraryMiddleware(self.home))
        mw += [
            LLMErrorHandlingMiddleware(self.retry or RetryPolicy()),  # retry, then raise on exhaustion
            ToolErrorHandlingMiddleware(),
            LoopDetectionMiddleware(),
        ]
        return mw
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_subagent.py -q`
Expected: PASS (new tests + all existing subagent tests).

- [ ] **Step 6: Commit**

```bash
git add src/atom/subagent.py tests/test_subagent.py
git commit -m "feat(resilience): give delegated sub-agents the same provider-retry coverage as the lead"
```

---

### Task 4: Compaction fail-closed on summary failure (`compaction.py`)

**Files:**
- Modify: `src/atom/middleware/compaction.py` (`PinnedSummarizationMiddleware`)
- Test: `tests/test_compaction.py`

**Interfaces:**
- Produces: `PinnedSummarizationMiddleware.before_model`/`abefore_model` return `None` (skip compaction) when the library produced an `"Error generating summary:"` sentinel — preserving history.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_compaction.py`:

```python
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatResult


class _RaisingModel(BaseChatModel):
    """A summarizer whose every call raises a transient error (langchain will convert it to
    the 'Error generating summary: ...' sentinel string)."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise RuntimeError("503 UNAVAILABLE")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise RuntimeError("503 UNAVAILABLE")

    @property
    def _llm_type(self) -> str:
        return "raising"


def test_compaction_skips_and_keeps_history_on_summary_failure():
    mw = build_compaction_middleware(_RaisingModel(), context_window=2, ratio=0.5, keep_messages=2)
    out = mw.before_model(
        {"messages": _five_messages(), "pinned_instruction": "ORIGINAL TASK"}, None
    )
    assert out is None                       # skipped: no RemoveMessage(ALL), history preserved


async def test_compaction_skips_on_summary_failure_async():
    mw = build_compaction_middleware(_RaisingModel(), context_window=2, ratio=0.5, keep_messages=2)
    out = await mw.abefore_model(
        {"messages": _five_messages(), "pinned_instruction": "ORIGINAL TASK"}, None
    )
    assert out is None


def test_summary_failed_detects_sentinel():
    from atom.middleware.compaction import PinnedSummarizationMiddleware
    from langchain_core.messages import HumanMessage
    good = {"messages": [HumanMessage(content="Here is a summary:\n\nclean summary")]}
    bad = {"messages": [HumanMessage(content="Here is a summary:\n\nError generating summary: 503")]}
    assert PinnedSummarizationMiddleware._summary_failed(good) is False
    assert PinnedSummarizationMiddleware._summary_failed(bad) is True
    assert PinnedSummarizationMiddleware._summary_failed(None) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_compaction.py -q -k "fail or sentinel"`
Expected: FAIL (`_summary_failed` doesn't exist; the raising model currently commits a broken summary rather than returning `None`).

- [ ] **Step 3: Implement fail-closed in `src/atom/middleware/compaction.py`**

Add the sentinel constant (after `_PIN_PREFIX`, ≈23):

```python
_SUMMARY_ERROR_SENTINEL = "error generating summary:"
```

Replace `PinnedSummarizationMiddleware.before_model` / `abefore_model` and add `_summary_failed`:

```python
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        result = super().before_model(state, runtime)
        if self._summary_failed(result):
            return None  # summarizer failed even after retries — keep history, retry next trigger
        return self._inject_pin(result, state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        result = await super().abefore_model(state, runtime)
        if self._summary_failed(result):
            return None
        return self._inject_pin(result, state)

    @staticmethod
    def _summary_failed(result: dict[str, Any] | None) -> bool:
        # LangChain's SummarizationMiddleware swallows summarizer exceptions into the summary
        # text "Error generating summary: ...". Detect that sentinel so we never commit a broken
        # summary (which would RemoveMessage(ALL) and destroy history on a transient error).
        if not result:
            return False
        for msg in result.get("messages", []):
            content = getattr(msg, "content", "")
            if isinstance(content, str) and _SUMMARY_ERROR_SENTINEL in content.lower():
                return True
        return False
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_compaction.py -q`
Expected: PASS (new tests + all existing compaction tests, incl. pin injection and `test_no_compaction_returns_none_untouched`).

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/compaction.py tests/test_compaction.py
git commit -m "fix(resilience): compaction fails closed on summary error — never destroy history"
```

---

### Task 5: Single retry authority + per-call timeout (`models/registry.py`)

**Files:**
- Modify: `src/atom/models/registry.py` (`build_model`)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `build_model` sets `max_retries=1` and `timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS` (120.0) via `setdefault` on the kwargs passed to `init_chat_model`/`ChatQwen`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_models.py`:

```python
def test_build_model_disables_sdk_retry_and_sets_timeout(monkeypatch):
    calls: dict = {}

    def fake_init(init_str, **kw):
        calls["init"] = kw
        return "MODEL"

    class FakeQwen:
        def __init__(self, **kw):
            calls["qwen"] = kw

    import langchain.chat_models
    import langchain_qwq

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init)
    monkeypatch.setattr(langchain_qwq, "ChatQwen", FakeQwen)

    build_model("haiku", thinking="off")
    assert calls["init"]["max_retries"] == 1          # SDK retry disabled -> middleware is the authority
    assert calls["init"]["timeout"] == 120.0          # per-call backstop

    build_model("qwen-max", thinking="off")
    assert calls["qwen"]["max_retries"] == 1
    assert calls["qwen"]["timeout"] == 120.0


def test_build_model_respects_explicit_overrides(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(
        __import__("langchain.chat_models", fromlist=["init_chat_model"]),
        "init_chat_model", lambda s, **kw: calls.setdefault("init", kw),
    )
    build_model("haiku", thinking="off", max_retries=3, timeout=42.0)
    assert calls["init"]["max_retries"] == 3 and calls["init"]["timeout"] == 42.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_models.py -q -k "disables_sdk or overrides"`
Expected: FAIL (`max_retries`/`timeout` not in kwargs).

- [ ] **Step 3: Implement in `src/atom/models/registry.py`**

Add a module constant near the top (after `_DEFAULT_WINDOW`, ≈20):

```python
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0  # per-call backstop; middleware owns retry/backoff
```

In `build_model` (≈156), set the defaults before constructing. Change the body:

```python
def build_model(key: str, *, thinking: Any = None, **overrides: Any) -> BaseChatModel:
    """Construct a chat model for a registry key (or raw ``provider:model`` string).

    ``max_retries`` is forced to 1 so the provider SDK's own retry layer is disabled and
    ``LLMErrorHandlingMiddleware`` is the single, predictable retry authority across providers
    (Gemini's SDK default is 6). A per-call ``timeout`` backstops a stalled connection.
    """
    spec = resolve_spec(key)
    kwargs = {**_thinking_overrides(spec, thinking), **overrides}
    kwargs.setdefault("max_retries", 1)
    kwargs.setdefault("timeout", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    if spec.init_str is not None:
        from langchain.chat_models import init_chat_model

        return init_chat_model(spec.init_str, **kwargs)
    # Qwen: init_chat_model has no dashscope provider.
    from langchain_qwq import ChatQwen

    kwargs.setdefault("api_base", spec.base_url)
    return ChatQwen(model=spec.model_name, **kwargs)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_models.py -q`
Expected: PASS (new + existing; `test_build_model_uses_chatqwen_for_qwen_and_init_for_others` still passes because it only asserts routing, not kwargs).

- [ ] **Step 5: Smoke-check real construction (no network)**

Run: `.venv/bin/python -c "from atom.models.registry import build_model; import os; os.environ.setdefault('ANTHROPIC_API_KEY','x'); m=build_model('haiku', thinking='off'); print(type(m).__name__, getattr(m,'max_retries',None))"`
Expected: prints `ChatAnthropic 1` (or similar) with no exception — confirms the provider accepts `max_retries`/`timeout` kwargs. If a provider rejects `timeout`, guard it per-provider and note it in the task report.

- [ ] **Step 6: Commit**

```bash
git add src/atom/models/registry.py tests/test_models.py
git commit -m "fix(resilience): disable SDK-level retry + add per-call timeout so middleware owns retry"
```

---

### Task 6: Workflow-engine lifecycle guards (`workflow/engine.py`)

**Files:**
- Modify: `src/atom/workflow/engine.py` (`execute`, `_run_task`, `_on_task_done`)
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes (Task 1): `ProviderUnavailableError` (for the failure test).
- Produces: guarded initial `store.load`; `logger.exception` in the previously-silent paths; `except asyncio.CancelledError` in `_run_task` that leaves clean terminal task state and re-raises.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_workflow_engine.py`:

```python
@pytest.mark.asyncio
async def test_provider_unavailable_fails_task_and_halts_run(base_config, atom_home, monkeypatch):
    from atom.middleware.llm_error import ProviderUnavailableError

    async def boom(prompt, **kwargs):
        raise ProviderUnavailableError(RuntimeError("503 UNAVAILABLE"), 21)

    monkeypatch.setattr(engine_mod, "run_agent", boom)
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {"topic": "sea"}, "run_pu", "2026-07-10T00:00:00")
    manifest = await engine.execute("run_pu")
    assert manifest.status == "halted"
    assert manifest.steps[0].tasks[0].status == "failed"
    assert "provider unavailable" in (manifest.steps[0].tasks[0].error or "")


@pytest.mark.asyncio
async def test_run_task_cancelled_leaves_clean_terminal_state(base_config, atom_home, monkeypatch):
    async def cancelled(prompt, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(engine_mod, "run_agent", cancelled)
    engine = WorkflowEngine(base_config)
    manifest = engine.create_run(_one_task_wf(), {"topic": "sea"}, "run_cx", "2026-07-10T00:00:00")
    workflow = engine._defs["run_cx"]
    ss = manifest.steps[0]
    sd = workflow.steps[0]
    ts = ss.tasks[0]
    td = sd.tasks[0]

    with pytest.raises(asyncio.CancelledError):
        await engine._run_task(manifest, workflow, ss, sd, ts, td)
    assert ts.status == "failed"
    assert ts.error == "cancelled"


@pytest.mark.asyncio
async def test_initial_manifest_load_failure_logs_and_reraises(base_config, atom_home, monkeypatch, caplog):
    import logging

    engine = WorkflowEngine(base_config)
    monkeypatch.setattr(engine.store, "load",
                        lambda rid: (_ for _ in ()).throw(OSError("disk gone")))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(OSError):
            await engine.execute("run_missing")
    assert any("failed to load manifest" in r.message for r in caplog.records)
```

> `_one_task_wf()` already exists in this file (returns a one-task workflow requiring `topic`).

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -q -k "provider_unavailable or cancelled or initial_manifest"`
Expected: FAIL (cancellation currently leaves `ts.status == "running"`; the initial load isn't guarded/logged).

- [ ] **Step 3: Guard the initial load in `execute`**

In `src/atom/workflow/engine.py`, change the top of `execute` (≈142-144) from:

```python
    async def execute(self, run_id: str) -> RunManifest:
        manifest = self.store.load(run_id)
        try:
```

to:

```python
    async def execute(self, run_id: str) -> RunManifest:
        try:
            manifest = self.store.load(run_id)
        except Exception:
            logger.exception("workflow run %s: failed to load manifest", run_id)
            raise
        try:
```

- [ ] **Step 4: Add logging to the previously-silent paths**

In `_on_task_done` (≈132-139), change the trailing `except Exception: pass`:

```python
        except Exception:
            logger.exception("workflow run %s: done-callback cleanup failed", run_id)
```

In `execute`'s `except BaseException` block (≈198-201), change the inner best-effort save:

```python
            try:
                self.store.save(manifest)
            except Exception:
                logger.exception("workflow run %s: failed to persist halted status", run_id)
```

- [ ] **Step 5: Add `CancelledError` handling in `_run_task`**

In `_run_task`, insert a branch BEFORE `except asyncio.TimeoutError:` (≈259):

```python
        except asyncio.CancelledError:
            ts.status = "failed"
            ts.error = "cancelled"
            ts.ended_at = _now()
            try:
                self.store.save(manifest)
            except Exception:
                pass  # best-effort: cancellation cleanup must not mask the cancellation
            raise
        except asyncio.TimeoutError:
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -q`
Expected: PASS (new tests + all existing engine tests, incl. `test_execute_load_workflow_fallback_error_terminalizes_run`, which exercises a *different* load — `load_workflow` inside the try — and is unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "fix(resilience): guard initial manifest load, log silent paths, clean cancellation state"
```

---

### Task 7: CLI clean errors + DeerFlow scrub (`cli.py`, `README.md`, `pyproject.toml`, `__init__.py`)

**Files:**
- Modify: `src/atom/cli.py` (imports, `app` help, `run`, `chat`)
- Modify: `README.md:3`, `pyproject.toml:8`, `src/atom/__init__.py:1`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes (Task 1): `ProviderUnavailableError`.
- Produces: `atom run` exits 1 with a clean `[red]Error…[/red]` on provider/config/model failures; `atom chat` prints the error and continues the REPL.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`:

```python
def test_run_reports_provider_unavailable_cleanly(monkeypatch):
    from atom.middleware.llm_error import ProviderUnavailableError

    async def boom(task, **kw):
        raise ProviderUnavailableError(RuntimeError("503 UNAVAILABLE"), 21)

    monkeypatch.setattr(cli, "run_agent", boom)
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["run", "do it"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_chat_survives_provider_error_and_continues(monkeypatch):
    from atom.middleware.llm_error import ProviderUnavailableError

    seen = []

    async def flaky(task, **kw):
        seen.append(task)
        if task == "one":
            raise ProviderUnavailableError(RuntimeError("503"), 21)
        return RunResult(thread_id="T", messages=[], final_text="ok", state={})

    monkeypatch.setattr(cli, "run_agent", flaky)
    inputs = iter(["one", "two", "exit"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(inputs))
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["chat"])
    assert result.exit_code == 0, result.output
    assert seen == ["one", "two"]        # REPL survived the error on turn 1


def test_app_help_has_no_deerflow():
    assert "DeerFlow" not in (cli.app.info.help or "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q -k "provider or survives or deerflow"`
Expected: FAIL (uncaught `ProviderUnavailableError` → non-1 exit / crash; help still says "DeerFlow-style").

- [ ] **Step 3: Update `src/atom/cli.py` imports + help**

Add imports near the top (after the existing imports, ≈16):

```python
from pydantic import ValidationError

from atom.middleware.llm_error import ProviderUnavailableError
```

Change the `app` help (line 21):

```python
app = typer.Typer(add_completion=False, help="atom — an agentic harness.")
```

- [ ] **Step 4: Wrap `run()`'s invocation**

Replace the body of `run()` from `with console.status(...)` through the `result = asyncio.run(...)` (≈62-67) with a guarded version:

```python
    _load_env()
    try:
        with console.status("[bold]thinking…[/bold]"):
            result = asyncio.run(run_agent(
                task, config_path=config, profile=profile, override_model=model,
                override_thinking=thinking, override_system_prompt=system_prompt,
                workspace=workspace, thread_id=thread, user_id=user,
            ))
    except (ProviderUnavailableError, ValidationError, KeyError, FileNotFoundError) as e:
        console.print(f"[red]Error: {type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)
```

(The `_print_activity(result)` and printing below stay unchanged.)

- [ ] **Step 5: Wrap `chat()`'s per-turn invocation**

Replace the `with console.status(...)` block and the `tid = result.thread_id` / print lines in `chat()` (≈102-111) with:

```python
        try:
            with console.status("[bold]thinking…[/bold]"):
                result = asyncio.run(run_agent(
                    task, config_path=config, profile=profile, override_model=model,
                    override_thinking=thinking, override_system_prompt=system_prompt,
                    workspace=workspace, thread_id=tid, user_id=user,
                ))
        except (ProviderUnavailableError, ValidationError, KeyError, FileNotFoundError) as e:
            console.print(f"[red]Error: {type(e).__name__}: {e}[/red]")
            continue
        tid = result.thread_id           # pin the thread for the rest of the session
        console.print(f"[bold green]atom ›[/bold green] {result.final_text}\n")
```

- [ ] **Step 6: Scrub DeerFlow branding**

`README.md` line 3 — change:

```
A DeerFlow-style **agentic middleware harness** built on LangChain v1. A single lead agent is
```

to:

```
An **agentic middleware harness** built on LangChain v1. A single lead agent is
```

`pyproject.toml` line 8 — change:

```
description = "atom — a DeerFlow-style agentic middleware harness on LangChain v1"
```

to:

```
description = "atom — an agentic middleware harness on LangChain v1"
```

`src/atom/__init__.py` line 1 — change the docstring:

```
"""atom — a DeerFlow-style agentic middleware harness built on LangChain v1.
```

to:

```
"""atom — an agentic middleware harness built on LangChain v1.
```

- [ ] **Step 7: Verify the scrub is complete (README/pyproject/package only; inspo.md is intentionally kept)**

Run: `grep -rn "DeerFlow" README.md pyproject.toml src/`
Expected: **no output** (all four user-facing strings scrubbed; `inspo.md` is deliberately excluded from this grep).

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: PASS (new + existing CLI tests).

- [ ] **Step 9: Commit**

```bash
git add src/atom/cli.py README.md pyproject.toml src/atom/__init__.py tests/test_cli.py
git commit -m "fix(cli): clean errors on run/chat + docs: remove DeerFlow branding"
```

---

## Final verification (after all tasks)

- [ ] Run the whole suite: `.venv/bin/python -m pytest -q` — Expected: all green.
- [ ] `grep -rn "DeerFlow" README.md pyproject.toml src/` — Expected: no output.

## Self-Review (author checklist — done)

- **Spec coverage:** every traceability row (C1–M13 + DeerFlow) maps to a task — C1→T1+T6, C2→T4(+T2 wrapped summarizer), I3→T1/T2, I4→T3, I5/I6/I7→T1, I8→T5, I9/I10→T6, M11→T2 (wrapped summarizer, no title code change), M12→T5, M13→T7, branding→T7.
- **Placeholder scan:** none — every code step shows complete code.
- **Type consistency:** `RetryPolicy(max_retries, base_delay, max_delay, jitter)`, `ProviderUnavailableError(original, attempts)`, `RetryingModel(inner, policy)`, `_build_summarizer(profile, prepared, policy)`, `_build_middlewares(..., retry_policy=...)`, `SubagentRunner.retry`, `PinnedSummarizationMiddleware._summary_failed` are used identically across tasks.
