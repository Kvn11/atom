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


def test_tool_call_weight_is_counted_in_estimate():
    heavy = AIMessage(content="", tool_calls=[
        {"name": "write_file", "args": {"content": "Z" * 4000}, "id": "c1", "type": "tool_call"}])
    recent = HumanMessage(content="recent")
    # budget fits `recent` but not the heavy tool-call message -> heavy is dropped
    out = trim_messages_to_budget([heavy, recent], approx_budget=20, single_msg_marker="[cut]")
    assert out == [recent] or (len(out) == 1 and out[0].content == "recent")


def test_trim_repairs_orphaned_tool_message_end_to_end():
    ai = AIMessage(content="H" * 4000, tool_calls=[{"name": "x", "args": {}, "id": "c1", "type": "tool_call"}])
    tm = ToolMessage(content="r", tool_call_id="c1")
    recent = HumanMessage(content="recent")
    out = trim_messages_to_budget([ai, tm, recent], approx_budget=20, single_msg_marker="[cut]")
    # the big AIMessage is dropped; the now-orphaned ToolMessage must not survive as a leader
    assert not (out and isinstance(out[0], ToolMessage))
    assert all(getattr(m, "content", None) != ai.content for m in out)


import pytest
from atom.middleware.context_overflow import ContextOverflowMiddleware
from atom.middleware.llm_error import (
    ContextOverflowError,
    LLMErrorHandlingMiddleware,
    ProviderUnavailableError,
    RetryPolicy,
)

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
