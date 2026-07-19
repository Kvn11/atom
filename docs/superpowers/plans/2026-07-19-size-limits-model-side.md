# Size Limits (Model-Side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover from model input-token overflow by deterministically shrinking the request and retrying, failing with an accurate `ContextOverflowError` if it still won't fit, and cap oversized tool output at creation with an LLM-visible truncation marker.

**Architecture:** A shared `truncate_text` helper backs three call sites. A provider-agnostic `is_context_overflow` classifier and a `ContextOverflowMiddleware` (innermost `wrap_model_call`, inside the existing retry middleware) do hard-trim → retry → shrink-harder, then raise `ContextOverflowError` (which the retry core passes through unwrapped instead of mislabeling "provider unavailable"). A `ToolOutputCapMiddleware` (outermost `wrap_tool_call`) caps any tool result before it enters state. Both middlewares are wired into the lead agent and sub-agents.

**Tech Stack:** Python 3.10+, LangChain agent middleware (`AgentMiddleware`, `wrap_model_call`/`wrap_tool_call`, `request.override(messages=...)`), Pydantic config, pytest + pytest-asyncio.

## Global Constraints

- Telemetry/observability code must NEVER break a run — not applicable here, but the same spirit: middleware must never raise on odd input except the intentional `ContextOverflowError`.
- The provider is the source of truth for "too big." Do NOT rely on accurate token counting; use a `chars // 4` estimate and let the retry-and-shrink loop absorb the error.
- `is_context_overflow` MUST be disjoint from the existing `is_retryable` (overflow is never a transient retry).
- Middleware order is load-bearing: `ContextOverflowMiddleware` is the **innermost** `wrap_model_call` (stays inside `LLMErrorHandlingMiddleware`); `ToolOutputCapMiddleware` is the **outermost** `wrap_tool_call`.
- Existing tests must stay green — especially `tests/test_llm_error.py` and `tests/test_workflow_engine.py::test_*` asserting `"provider unavailable"` for transient failures.
- Test command: `python -m pytest <path> -v` from the repo root.
- Config uses `_Base` (`extra="ignore"`); adding fields is backward-compatible.

---

### Task 1: Shared `truncate_text` helper

**Files:**
- Create: `src/atom/limits.py`
- Test: `tests/test_limits.py`

**Interfaces:**
- Produces: `truncate_text(text: str, *, max_chars: int, marker_template: str) -> str`. Returns `text` unchanged when `len(text) <= max_chars`; otherwise keeps the first and last `max_chars // 2` chars and splices `marker_template` (formatted with `total`, `elided`, `head`, `tail` ints) into the middle. The result MAY exceed `max_chars` by the marker length — `max_chars` is an approximate budget and all callers set it well under any hard limit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_limits.py
from atom.limits import truncate_text


def test_short_text_unchanged():
    assert truncate_text("hello", max_chars=100, marker_template="[cut]") == "hello"


def test_keeps_head_and_tail_with_counts_marker():
    text = "A" * 50 + "B" * 50  # 100 chars
    out = truncate_text(text, max_chars=20, marker_template="[…{elided} of {total} elided…]")
    assert out.startswith("A" * 10)   # head = max_chars // 2
    assert out.endswith("B" * 10)     # tail = max_chars // 2
    assert "80 of 100 elided" in out


def test_zero_budget_returns_marker_only():
    out = truncate_text("Z" * 10, max_chars=0, marker_template="[gone]")
    assert out == "[gone]"


def test_extra_format_keys_ignored_when_unreferenced():
    out = truncate_text("Q" * 10, max_chars=4, marker_template="[{elided}]")
    assert out.startswith("QQ") and out.endswith("QQ") and "[6]" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_limits.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.limits'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/limits.py
"""Shared size-limit helper: text truncation with an informative, counts-bearing elision marker.

Reused by the tool-output cap (Layer 1), the context-overflow trimmer (Layer 2), and the LangFuse
truncating mask (Layer 3a). One helper, three callers, three markers.
"""
from __future__ import annotations


def truncate_text(text: str, *, max_chars: int, marker_template: str) -> str:
    """Truncate ``text`` to ~``max_chars``, keeping a head+tail slice with ``marker_template``
    (formatted with ``total``/``elided``/``head``/``tail`` ints) spliced into the elided middle.

    Returns ``text`` unchanged when it already fits. The result may exceed ``max_chars`` by the
    marker length; ``max_chars`` is an approximate budget set well under any hard limit.
    """
    total = len(text)
    if total <= max_chars:
        return text
    half = max(0, max_chars // 2)
    head = text[:half]
    tail = text[-half:] if half else ""
    elided = total - len(head) - len(tail)
    marker = marker_template.format(total=total, elided=elided, head=len(head), tail=len(tail))
    return f"{head}{marker}{tail}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_limits.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/atom/limits.py tests/test_limits.py
git commit -m "feat(limits): shared truncate_text helper with counts-bearing elision marker"
```

---

### Task 2: `is_context_overflow` classifier

**Files:**
- Modify: `src/atom/middleware/llm_error.py` (add function + marker tuple near `is_retryable`, ~line 64)
- Test: `tests/test_llm_error.py` (append)

**Interfaces:**
- Consumes: existing `is_retryable` (for the disjointness assertions).
- Produces: `is_context_overflow(exc: Exception) -> bool` — True for a permanent-for-this-input context/token overflow across providers.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_error.py  (append; _Anthropic and _Gemini already defined at top of file)
from atom.middleware.llm_error import is_context_overflow


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_llm_error.py -k overflow -v`
Expected: FAIL with `ImportError: cannot import name 'is_context_overflow'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/middleware/llm_error.py  (add after is_retryable, ~line 64)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_llm_error.py -v`
Expected: PASS (all, including the 4 new overflow tests; existing tests still green)

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/llm_error.py tests/test_llm_error.py
git commit -m "feat(llm-error): provider-agnostic is_context_overflow classifier (disjoint from is_retryable)"
```

---

### Task 3: `ContextOverflowError` + retry-core passthrough

**Files:**
- Modify: `src/atom/middleware/llm_error.py` (add exception class near `ProviderUnavailableError` ~line 33; add a guard line in both `run_with_retry_sync` ~line 82 and `run_with_retry_async` ~line 100)
- Test: `tests/test_llm_error.py` (append)

**Interfaces:**
- Produces: `ContextOverflowError(*, limit: int, attempts: int, original: Exception)` with attrs `.limit`, `.attempts`, `.original`. The retry core re-raises it **unwrapped** (never re-wraps into `ProviderUnavailableError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_error.py  (append)
from atom.middleware.llm_error import ContextOverflowError


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_llm_error.py -k context_overflow_error -v`
Expected: FAIL with `ImportError: cannot import name 'ContextOverflowError'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/middleware/llm_error.py  (add after ProviderUnavailableError, ~line 44)
class ContextOverflowError(Exception):
    """Raised when a model call's input still exceeds the model's context window after emergency
    compaction. Distinct from ProviderUnavailableError: the provider is healthy — the input is too
    big — so retrying it unchanged is futile and it must not read as an outage."""

    def __init__(self, *, limit: int, attempts: int, original: Exception):
        self.limit = limit
        self.attempts = attempts
        self.original = original
        super().__init__(
            f"context window exceeded: input still over the model's ~{limit}-token limit after "
            f"{attempts} emergency-compaction attempt(s); reduce input, raise compaction "
            f"aggressiveness, or use a larger-window model "
            f"({type(original).__name__}: {original})"
        )
```

Then add the guard as the FIRST line of the `except Exception as exc:` block in BOTH retry loops:

```python
# in run_with_retry_sync (~line 81) and run_with_retry_async (~line 99)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ContextOverflowError):
                raise                       # accurate error — never re-wrap as 'provider unavailable'
            if attempt >= policy.max_retries or not is_retryable(exc):
                raise ProviderUnavailableError(exc, attempt + 1) from exc
            ceiling = _backoff_ceiling(attempt, policy)
            # sync:  sleep(rand(0.0, ceiling) if policy.jitter else ceiling)
            # async: await sleep(rand(0.0, ceiling) if policy.jitter else ceiling)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_llm_error.py -v`
Expected: PASS (all green, including the 2 new tests)

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/llm_error.py tests/test_llm_error.py
git commit -m "feat(llm-error): ContextOverflowError passes through retry core unwrapped"
```

---

### Task 4: Deterministic message trimmer

**Files:**
- Create: `src/atom/middleware/context_overflow.py`
- Test: `tests/test_context_overflow.py`

**Interfaces:**
- Consumes: `atom.limits.truncate_text`.
- Produces:
  - `trim_messages_to_budget(messages: list, approx_budget: int, *, single_msg_marker: str) -> list` — keeps system + pinned-instruction messages, drops oldest non-protected turns until under `approx_budget` (est. `chars // 4` per message), repairs a dangling leading `ToolMessage`, and truncates any single retained message still over budget.
  - `_drop_dangling_leading_tool_messages(msgs: list) -> list` (helper, tested directly).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_overflow.py
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from atom.middleware.context_overflow import (
    _drop_dangling_leading_tool_messages,
    trim_messages_to_budget,
)


def _pin(text):
    return HumanMessage(content=text, additional_kwargs={"lc_source": "pinned_instruction"})


def test_keeps_system_and_pin_and_drops_oldest():
    msgs = [
        SystemMessage(content="SYS"),
        _pin("PIN"),
        HumanMessage(content="old" * 100),   # ~75 tokens — should be dropped
        AIMessage(content="mid" * 100),       # ~75 tokens — should be dropped
        HumanMessage(content="recent"),
    ]
    out = trim_messages_to_budget(msgs, approx_budget=60, single_msg_marker="[cut]")
    assert any(isinstance(m, SystemMessage) for m in out)
    assert any(m.additional_kwargs.get("lc_source") == "pinned_instruction" for m in out)
    assert out[-1].content == "recent"
    assert not any(isinstance(m.content, str) and "old" in m.content for m in out)


def test_truncates_single_oversized_message():
    big = HumanMessage(content="Z" * 10000)
    out = trim_messages_to_budget([big], approx_budget=100, single_msg_marker="[…{elided}/{total}…]")
    assert len(out) == 1
    assert len(out[0].content) < 10000
    assert "…" in out[0].content


def test_drop_dangling_leading_tool_messages():
    msgs = [ToolMessage(content="r", tool_call_id="c1"), AIMessage(content="ok")]
    out = _drop_dangling_leading_tool_messages(msgs)
    assert not isinstance(out[0], ToolMessage)


def test_empty_messages_returns_empty():
    assert trim_messages_to_budget([], approx_budget=100, single_msg_marker="[cut]") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_context_overflow.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.middleware.context_overflow'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/middleware/context_overflow.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_context_overflow.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/context_overflow.py tests/test_context_overflow.py
git commit -m "feat(context-overflow): deterministic message trimmer (keep system+pin, drop oldest, truncate giants)"
```

---

### Task 5: `ContextOverflowMiddleware`

**Files:**
- Modify: `src/atom/middleware/context_overflow.py` (append the middleware class)
- Test: `tests/test_context_overflow.py` (append)

**Interfaces:**
- Consumes: `trim_messages_to_budget`, `is_context_overflow`, `ContextOverflowError`, and the request contract `request.messages` + `request.override(messages=...)` (as used by `ViewImageMiddleware`).
- Produces: `ContextOverflowMiddleware(*, context_window: int, max_attempts: int = 3, target_ratio: float = 0.5, enabled: bool = True)` implementing `wrap_model_call` / `awrap_model_call`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_overflow.py  (append)
import pytest
from langchain_core.messages import HumanMessage

from atom.middleware.context_overflow import ContextOverflowMiddleware
from atom.middleware.llm_error import ContextOverflowError

_OVERFLOW = "the input token count exceeds the maximum number of tokens allowed"


class _Req:
    def __init__(self, messages):
        self.messages = messages

    def override(self, *, messages):
        return _Req(messages)


def test_recovers_after_trim():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception(_OVERFLOW)
        return "OK"

    mw = ContextOverflowMiddleware(context_window=1000, max_attempts=3)
    out = mw.wrap_model_call(_Req([HumanMessage(content="x" * 8000)]), handler)
    assert out == "OK" and calls["n"] == 2


def test_raises_context_overflow_after_exhaustion():
    def handler(req):
        raise Exception(_OVERFLOW)

    mw = ContextOverflowMiddleware(context_window=1000, max_attempts=2)
    with pytest.raises(ContextOverflowError) as ei:
        mw.wrap_model_call(_Req([HumanMessage(content="x" * 8000)]), handler)
    assert ei.value.attempts == 2


def test_passes_through_non_overflow_error():
    def handler(req):
        raise ValueError("some other error")

    mw = ContextOverflowMiddleware(context_window=1000)
    with pytest.raises(ValueError):
        mw.wrap_model_call(_Req([]), handler)


def test_disabled_raises_clean_without_retry():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise Exception("context_length_exceeded")

    mw = ContextOverflowMiddleware(context_window=1000, enabled=False)
    with pytest.raises(ContextOverflowError):
        mw.wrap_model_call(_Req([]), handler)
    assert calls["n"] == 1   # no retry attempts when recovery disabled


@pytest.mark.asyncio
async def test_async_recovers_after_trim():
    calls = {"n": 0}

    async def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception(_OVERFLOW)
        return "AOK"

    mw = ContextOverflowMiddleware(context_window=1000, max_attempts=3)
    out = await mw.awrap_model_call(_Req([HumanMessage(content="x" * 8000)]), handler)
    assert out == "AOK" and calls["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_context_overflow.py -k "recovers or exhaustion or non_overflow or disabled" -v`
Expected: FAIL with `ImportError: cannot import name 'ContextOverflowMiddleware'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/middleware/context_overflow.py  (append)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_context_overflow.py -v`
Expected: PASS (all, including async)

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/context_overflow.py tests/test_context_overflow.py
git commit -m "feat(context-overflow): ContextOverflowMiddleware — shrink-and-retry then ContextOverflowError"
```

---

### Task 6: Config fields (overflow knobs + tool-output cap)

**Files:**
- Modify: `src/atom/config/schema.py` (`CompactionConfig` ~line 34, `ToolsConfig` ~line 162)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `cfg.compaction.overflow_recovery: bool`, `cfg.compaction.overflow_max_attempts: int`, `cfg.compaction.overflow_target_ratio: float`; `profile.tools.max_output_chars: int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (append)
from atom.config.schema import AtomConfig


def test_overflow_and_tool_cap_defaults():
    cfg = AtomConfig()
    assert cfg.compaction.overflow_recovery is True
    assert cfg.compaction.overflow_max_attempts == 3
    assert cfg.compaction.overflow_target_ratio == 0.5
    assert cfg.profile("default").tools.max_output_chars == 100_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -k overflow_and_tool_cap -v`
Expected: FAIL with `AttributeError: 'CompactionConfig' object has no attribute 'overflow_recovery'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/config/schema.py — add to CompactionConfig (after summary_input_tokens, ~line 40)
    # Reactive emergency recovery when a model call overflows the context window (input too big).
    # When off, the first overflow raises ContextOverflowError immediately (no shrink-and-retry).
    overflow_recovery: bool = True
    overflow_max_attempts: int = 3          # shrink-and-retry rounds before failing clean
    overflow_target_ratio: float = 0.5      # first trim target as a fraction of the context window
```

```python
# src/atom/config/schema.py — add to ToolsConfig (after `frequent`, ~line 169)
    # Cap any single tool result at this many characters before it enters history; the truncation
    # is marked so the model knows it was cut and can re-run narrower. Generous (~25k tokens).
    max_output_chars: int = 100_000
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atom/config/schema.py tests/test_config.py
git commit -m "feat(config): overflow-recovery knobs + tools.max_output_chars"
```

---

### Task 7: `ToolOutputCapMiddleware`

**Files:**
- Create: `src/atom/middleware/tool_output_cap.py`
- Test: `tests/test_tool_output_cap.py`

**Interfaces:**
- Consumes: `atom.limits.truncate_text`; the `wrap_tool_call` contract (handler returns a `ToolMessage` or a `Command`-like object with a `.update` dict holding `"messages"`).
- Produces: `ToolOutputCapMiddleware(max_chars: int = 100_000)` implementing `wrap_tool_call` / `awrap_tool_call`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_output_cap.py
import pytest
from langchain_core.messages import ToolMessage

from atom.middleware.tool_output_cap import ToolOutputCapMiddleware


def test_truncates_long_string_output_with_marker():
    mw = ToolOutputCapMiddleware(max_chars=100)

    def handler(req):
        return ToolMessage(content="Z" * 5000, tool_call_id="c1")

    out = mw.wrap_tool_call(object(), handler)
    assert len(out.content) < 5000
    assert "truncated to fit context" in out.content
    assert out.tool_call_id == "c1"          # identity preserved


def test_small_output_untouched():
    mw = ToolOutputCapMiddleware(max_chars=100)

    def handler(req):
        return ToolMessage(content="small", tool_call_id="c1")

    assert mw.wrap_tool_call(object(), handler).content == "small"


def test_caps_command_update_messages():
    mw = ToolOutputCapMiddleware(max_chars=50)

    class _Cmd:
        def __init__(self, messages):
            self.update = {"messages": messages}

    def handler(req):
        return _Cmd([ToolMessage(content="Q" * 2000, tool_call_id="c1")])

    out = mw.wrap_tool_call(object(), handler)
    assert "truncated to fit context" in out.update["messages"][0].content


def test_caps_list_content_text_block():
    mw = ToolOutputCapMiddleware(max_chars=50)

    def handler(req):
        return ToolMessage(content=[{"type": "text", "text": "W" * 2000}], tool_call_id="c1")

    out = mw.wrap_tool_call(object(), handler)
    assert "truncated to fit context" in out.content[0]["text"]


@pytest.mark.asyncio
async def test_async_truncates_long_string_output():
    mw = ToolOutputCapMiddleware(max_chars=100)

    async def handler(req):
        return ToolMessage(content="Z" * 5000, tool_call_id="c1")

    out = await mw.awrap_tool_call(object(), handler)
    assert len(out.content) < 5000 and "truncated to fit context" in out.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_output_cap.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.middleware.tool_output_cap'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/middleware/tool_output_cap.py
"""ToolOutputCapMiddleware — cap an oversized tool result before it enters history.

Outermost wrap_tool_call, so the capped ToolMessage is what gets persisted. The marker is written
AS AN INSTRUCTION TO THE MODEL: it says the output was truncated and how to recover the omitted part
(re-run narrower — grep/range/page). This shrinks the source of both context-window overflow and
oversized telemetry payloads.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from atom.limits import truncate_text


class ToolOutputCapMiddleware(AgentMiddleware):
    _MARKER = (
        "\n\n[atom: tool output truncated to fit context — {elided} of {total} characters elided "
        "(showing the first {head} and last {tail}). To see the omitted portion, re-run this tool "
        "with a narrower scope: grep/filter, a smaller range or page, or head/tail.]\n\n"
    )

    def __init__(self, max_chars: int = 100_000):
        super().__init__()
        self.max_chars = max_chars

    def _cap_message(self, msg: ToolMessage) -> ToolMessage:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            if len(content) <= self.max_chars:
                return msg
            return msg.model_copy(update={"content": truncate_text(
                content, max_chars=self.max_chars, marker_template=self._MARKER)})
        if isinstance(content, list):
            new_blocks = []
            for b in content:
                if isinstance(b, dict) and isinstance(b.get("text"), str) \
                        and len(b["text"]) > self.max_chars:
                    nb = dict(b)
                    nb["text"] = truncate_text(
                        b["text"], max_chars=self.max_chars, marker_template=self._MARKER)
                    new_blocks.append(nb)
                else:
                    new_blocks.append(b)
            return msg.model_copy(update={"content": new_blocks})
        return msg

    def _cap(self, result: Any) -> Any:
        if isinstance(result, ToolMessage):
            return self._cap_message(result)
        update = getattr(result, "update", None)   # Command-like: cap ToolMessages in .update
        if isinstance(update, dict) and isinstance(update.get("messages"), list):
            update["messages"] = [
                self._cap_message(m) if isinstance(m, ToolMessage) else m
                for m in update["messages"]
            ]
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return self._cap(handler(request))

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return self._cap(await handler(request))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tool_output_cap.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/tool_output_cap.py tests/test_tool_output_cap.py
git commit -m "feat(tool-output-cap): cap oversized tool results with an LLM-actionable marker"
```

---

### Task 8: Wire both middlewares into the lead agent

**Files:**
- Modify: `src/atom/agent.py` (`_build_middlewares`: imports ~line 225–241; `ContextOverflowMiddleware` insert after the `if deferred_names:` block ~line 305; `ToolOutputCapMiddleware` insert before `SandboxAuditMiddleware` ~line 312; `SubagentRunner(...)` kwargs ~line 256)
- Test: `tests/test_agent_smoke.py` (append to the middleware-order test area)

**Interfaces:**
- Consumes: `ContextOverflowMiddleware`, `ToolOutputCapMiddleware`, `cfg.compaction.overflow_*`, `profile.tools.max_output_chars`, `prepared.context_window`.
- Produces: the two middlewares present in the lead chain in the correct relative order.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_smoke.py  (append)
def test_size_limit_middlewares_wired_in_order(base_config, atom_home):
    from atom.agent import _build_middlewares
    from atom.library import load_library
    from atom.sandbox.provider import LocalSandboxProvider

    prepared = make_prepared([])
    profile = base_config.profile("default")
    provider = LocalSandboxProvider()
    library = load_library(str(atom_home))
    chain = _build_middlewares(
        base_config, profile, prepared, provider, str(atom_home), prepared.model, library
    )
    types = [type(m).__name__ for m in chain]
    assert "ContextOverflowMiddleware" in types and "ToolOutputCapMiddleware" in types
    # ContextOverflow is INNER of the retry middleware (later in the list).
    assert types.index("LLMErrorHandlingMiddleware") < types.index("ContextOverflowMiddleware")
    # ToolOutputCap is the OUTERMOST tool wrapper (before SandboxAudit).
    assert types.index("ToolOutputCapMiddleware") < types.index("SandboxAuditMiddleware")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_smoke.py -k size_limit_middlewares -v`
Expected: FAIL with `AssertionError` (`ContextOverflowMiddleware` not in `types`)

- [ ] **Step 3: Write minimal implementation**

Add imports in `_build_middlewares` (with the other `from atom.middleware...` imports, ~line 225):

```python
    from atom.middleware.context_overflow import ContextOverflowMiddleware
    from atom.middleware.tool_output_cap import ToolOutputCapMiddleware
```

Pass overflow + cap settings into the `SubagentRunner(...)` construction (~line 256, add kwargs; used by Task 9):

```python
        notes=notes,   # bash children rendered vault-aware when the workflow enables notes
        overflow_recovery=cfg.compaction.overflow_recovery,
        overflow_max_attempts=cfg.compaction.overflow_max_attempts,
        overflow_target_ratio=cfg.compaction.overflow_target_ratio,
        max_tool_output_chars=profile.tools.max_output_chars,
```

Insert `ContextOverflowMiddleware` as the innermost wrap_model_call — immediately after the
`if deferred_names:` block and before `chain.append(TodoListMiddleware())` (~line 305):

```python
    if deferred_names:
        chain.append(DeferredToolFilterMiddleware(deferred_names, catalog_hash=library.catalog_hash))
    chain.append(ContextOverflowMiddleware(              # innermost wrap_model_call: emergency trim
        context_window=prepared.context_window,
        max_attempts=cfg.compaction.overflow_max_attempts,
        target_ratio=cfg.compaction.overflow_target_ratio,
        enabled=cfg.compaction.overflow_recovery,
    ))
    chain.append(TodoListMiddleware())                   # planning tool — ALWAYS ON
```

Insert `ToolOutputCapMiddleware` as the outermost wrap_tool_call — first in the `chain += [...]`
tool block, before `SandboxAuditMiddleware()` (~line 309):

```python
    chain += [
        SubagentMiddleware(runner),                      # delegate_task tool — ALWAYS ON
        # --- wrap_tool_call (outer -> inner) ---
        ToolOutputCapMiddleware(profile.tools.max_output_chars),  # OUTERMOST: cap before state
        SandboxAuditMiddleware(),                        # journal every tool call
        GuardrailMiddleware(enabled=cfg.guardrails.enabled),
        ToolErrorHandlingMiddleware(),
        SubagentLimitMiddleware(max_sub),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_smoke.py -v`
Expected: PASS (existing order-invariant test + the new wiring test)

- [ ] **Step 5: Commit**

```bash
git add src/atom/agent.py tests/test_agent_smoke.py
git commit -m "feat(agent): wire ContextOverflow (inner model wrap) + ToolOutputCap (outer tool wrap)"
```

---

### Task 9: Wire both middlewares into sub-agents

**Files:**
- Modify: `src/atom/subagent.py` (dataclass fields ~line 68; `_child_middleware` ~line 126)
- Test: `tests/test_subagent.py` (append)

**Interfaces:**
- Consumes: the new `SubagentRunner` kwargs passed by `agent.py` in Task 8.
- Produces: `ContextOverflowMiddleware` and `ToolOutputCapMiddleware` in every child chain.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent.py  (append)
def test_child_middleware_includes_size_limit_middlewares(atom_home):
    from atom.middleware.context_overflow import ContextOverflowMiddleware
    from atom.middleware.tool_output_cap import ToolOutputCapMiddleware
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=123_456, bash_enabled=False,
        overflow_max_attempts=2, max_tool_output_chars=777,
    )
    mws = runner._child_middleware()
    overflow = [m for m in mws if isinstance(m, ContextOverflowMiddleware)]
    cap = [m for m in mws if isinstance(m, ToolOutputCapMiddleware)]
    assert overflow and overflow[0].context_window == 123_456 and overflow[0].max_attempts == 2
    assert cap and cap[0].max_chars == 777
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent.py -k size_limit -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'overflow_max_attempts'`

- [ ] **Step 3: Write minimal implementation**

Add dataclass fields after `notes` (~line 68):

```python
    notes: dict | None = None            # per-workflow Logseq vault ctx; bash children only
    overflow_recovery: bool = True
    overflow_max_attempts: int = 3
    overflow_target_ratio: float = 0.5
    max_tool_output_chars: int = 100_000
```

In `_child_middleware` (~line 126), import and insert the two middlewares — `ContextOverflowMiddleware`
right after `LLMErrorHandlingMiddleware` (inner), `ToolOutputCapMiddleware` before `ToolErrorHandlingMiddleware`
(outermost tool wrapper):

```python
        from atom.middleware.context_overflow import ContextOverflowMiddleware
        from atom.middleware.tool_output_cap import ToolOutputCapMiddleware
        # ... existing imports and mw assembly ...
        mw += [
            LLMErrorHandlingMiddleware(self.retry or RetryPolicy()),  # retry, then raise on exhaustion
            ContextOverflowMiddleware(                                # innermost model wrap
                context_window=self.context_window,
                max_attempts=self.overflow_max_attempts,
                target_ratio=self.overflow_target_ratio,
                enabled=self.overflow_recovery,
            ),
            ToolOutputCapMiddleware(self.max_tool_output_chars),      # outermost tool wrap
            ToolErrorHandlingMiddleware(),
            LoopDetectionMiddleware(),
        ]
        return mw
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent.py -v`
Expected: PASS (existing subagent tests + the new one)

- [ ] **Step 5: Commit**

```bash
git add src/atom/subagent.py tests/test_subagent.py
git commit -m "feat(subagent): wire ContextOverflow + ToolOutputCap into child middleware"
```

---

### Task 10: End-to-end guard — overflow failure surfaces as `ContextOverflowError`

**Files:**
- Test: `tests/test_workflow_engine.py` (append) OR `tests/test_context_overflow.py` — an integration-style test that a persistently-overflowing model call surfaces a `ContextOverflowError`, not `ProviderUnavailableError`, through the retry stack.

**Interfaces:**
- Consumes: `LLMErrorHandlingMiddleware`, `ContextOverflowMiddleware`, `ContextOverflowError`, `ProviderUnavailableError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_overflow.py  (append)
from atom.middleware.llm_error import LLMErrorHandlingMiddleware, ProviderUnavailableError, RetryPolicy


@pytest.mark.asyncio
async def test_overflow_surfaces_as_context_overflow_through_retry_stack():
    """ContextOverflow is inner, LLMErrorHandling outer. A persistent overflow must surface as
    ContextOverflowError (accurate), never ProviderUnavailableError ('provider unavailable')."""
    overflow_mw = ContextOverflowMiddleware(context_window=1000, max_attempts=2)
    retry_mw = LLMErrorHandlingMiddleware(RetryPolicy(max_retries=5, base_delay=0.0, max_delay=0.0))

    async def model(req):
        raise Exception("prompt is too long: too many tokens for the context window")

    async def inner(req):                 # ContextOverflow wraps the model
        return await overflow_mw.awrap_model_call(req, model)

    with pytest.raises(ContextOverflowError):
        await retry_mw.awrap_model_call(_Req([HumanMessage(content="x" * 8000)]), inner)


@pytest.mark.asyncio
async def test_transient_still_surfaces_as_provider_unavailable():
    """Regression: a transient error inside the same stack must still be retried then raised as
    ProviderUnavailableError (ContextOverflow must pass non-overflow errors straight through)."""
    overflow_mw = ContextOverflowMiddleware(context_window=1000, max_attempts=2)
    retry_mw = LLMErrorHandlingMiddleware(RetryPolicy(max_retries=2, base_delay=0.0, max_delay=0.0))

    async def model(req):
        raise Exception("503 UNAVAILABLE")

    async def inner(req):
        return await overflow_mw.awrap_model_call(req, model)

    with pytest.raises(ProviderUnavailableError):
        await retry_mw.awrap_model_call(_Req([HumanMessage(content="hi")]), inner)
```

- [ ] **Step 2: Run test to verify it fails (then passes)**

Run: `python -m pytest tests/test_context_overflow.py -k "surfaces" -v`
Expected: PASS immediately — all machinery exists after Tasks 3 & 5; this test locks the end-to-end contract (no new production code). If it FAILS, the retry-core passthrough (Task 3) or ContextOverflow's non-overflow re-raise (Task 5) regressed — fix there.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (whole suite green — no regression to `test_llm_error.py` / `test_workflow_engine.py`).

- [ ] **Step 4: Commit**

```bash
git add tests/test_context_overflow.py
git commit -m "test(context-overflow): overflow surfaces as ContextOverflowError, transient as ProviderUnavailable"
```

---

## Self-Review

**Spec coverage:**
- Layer 1 tool-output cap → Task 7 (+ wiring Tasks 8/9). LLM-visible marker → `_MARKER` in Task 7. ✓
- Layer 2 classifier → Task 2; trimmer → Task 4; middleware → Task 5; `ContextOverflowError` + unwrap → Task 3; placement → Tasks 8/9; end-to-end surface → Task 10. ✓
- Shared `truncate_text` → Task 1. ✓
- Config knobs (`overflow_*`, `max_output_chars`) → Task 6. ✓
- "No engine change needed" — confirmed; Task 10 asserts the surfaced error type without touching `engine.py`. ✓
- Provider-agnostic detection → Task 2 fixtures (Gemini/Anthropic/OpenAI). ✓
- Lead + sub-agents → Tasks 8 + 9. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows real assertions. ✓

**Type consistency:** `truncate_text(text, *, max_chars, marker_template)` used identically in Tasks 1/4/7. `trim_messages_to_budget(messages, approx_budget, *, single_msg_marker)` defined in Task 4, called in Task 5. `ContextOverflowMiddleware(*, context_window, max_attempts, target_ratio, enabled)` defined in Task 5, constructed in Tasks 8/9. `ContextOverflowError(*, limit, attempts, original)` defined Task 3, raised Task 5. `ToolOutputCapMiddleware(max_chars)` defined Task 7, constructed Tasks 8/9. Config names match Task 6 ↔ Tasks 8/9. ✓
