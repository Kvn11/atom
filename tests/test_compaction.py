"""Compaction trigger is set to ratio * (profile-resolved) context window (deviation #5)."""

from __future__ import annotations

from langchain.agents.middleware import SummarizationMiddleware

from atom.middleware.compaction import build_compaction_middleware
from tests.conftest import ScriptedChatModel


def test_trigger_is_half_the_window():
    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    mw = build_compaction_middleware(model, context_window=200_000, ratio=0.5, keep_messages=15)
    assert isinstance(mw, SummarizationMiddleware)
    assert mw.trigger == ("tokens", 100_000)
    assert mw.keep == ("messages", 15)


def test_trigger_scales_with_window_and_ratio():
    model = ScriptedChatModel(responses=[], profile={})
    # e.g. a Qwen fallback window with a custom ratio
    mw = build_compaction_middleware(model, context_window=1_000_000, ratio=0.6, keep_messages=20)
    assert mw.trigger == ("tokens", 600_000)


def test_custom_summary_prompt_is_used():
    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    mw = build_compaction_middleware(
        model, context_window=200_000, summary_prompt="ATOM-AWARE preserve mounts {messages}"
    )
    assert "ATOM-AWARE" in mw.summary_prompt


def test_fallback_window_drives_trigger_when_profile_missing():
    # Ties the profile-less resolution to the compaction trigger (the real fallback path).
    from atom.models import resolve_context_window, resolve_spec

    spec = resolve_spec("qwen-max")
    model = ScriptedChatModel(responses=[], profile={})  # no max_input_tokens
    window = resolve_context_window(model, spec)
    assert window == spec.context_window  # fell back to the static registry value
    mw = build_compaction_middleware(model, context_window=window, ratio=0.5)
    assert mw.trigger == ("tokens", spec.context_window // 2)


from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from atom.middleware.compaction import PinnedSummarizationMiddleware


def _pinning_mw(summary_text="SUMMARY", keep=2):
    # context_window=2, ratio=0.5 -> trigger ("tokens", 1): any non-empty history summarizes.
    model = ScriptedChatModel(
        responses=[AIMessage(content=summary_text)], profile={"max_input_tokens": 200_000}
    )
    return build_compaction_middleware(
        model, context_window=2, ratio=0.5, keep_messages=keep
    )


def _five_messages(first="ORIGINAL TASK"):
    return [
        HumanMessage(content=first),
        AIMessage(content="a"),
        HumanMessage(content="b"),
        AIMessage(content="c"),
        HumanMessage(content="d"),
    ]


def _pin_msgs(messages):
    return [
        m for m in messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") == "pinned_instruction"
    ]


def test_factory_returns_pinning_subclass():
    mw = _pinning_mw()
    assert isinstance(mw, PinnedSummarizationMiddleware)


def test_pin_injected_verbatim_after_compaction():
    mw = _pinning_mw()
    out = mw.before_model(
        {"messages": _five_messages(), "pinned_instruction": "ORIGINAL TASK"}, None
    )
    result = out["messages"]
    assert isinstance(result[0], RemoveMessage)          # sentinel first
    pins = _pin_msgs(result)
    assert len(pins) == 1
    assert pins[0].content.endswith("ORIGINAL TASK")     # verbatim, with prefix
    assert "Standing instruction" in pins[0].content
    # pin sits BEFORE the library summary message
    pin_i = result.index(pins[0])
    summary_i = next(
        i for i, m in enumerate(result)
        if getattr(m, "additional_kwargs", {}).get("lc_source") == "summarization"
    )
    assert pin_i < summary_i


def test_pin_survives_two_compactions_verbatim():
    pinned = "PIN ME EXACTLY 123"
    mw1 = _pinning_mw(summary_text="SUM1")
    out1 = mw1.before_model(
        {"messages": _five_messages(first=pinned), "pinned_instruction": pinned}, None
    )
    # Apply the RemoveMessage(ALL) sentinel: the surviving list is everything after it.
    kept = [m for m in out1["messages"] if not isinstance(m, RemoveMessage)]
    kept += [AIMessage(content="more"), HumanMessage(content="more2"), AIMessage(content="more3")]
    mw2 = _pinning_mw(summary_text="SUM2")
    out2 = mw2.before_model({"messages": kept, "pinned_instruction": pinned}, None)
    pins = _pin_msgs(out2["messages"])
    assert len(pins) == 1
    assert pins[0].content.endswith(pinned)              # undrifted after 2nd compaction


def test_no_compaction_returns_none_untouched():
    model = ScriptedChatModel(
        responses=[AIMessage(content="S")], profile={"max_input_tokens": 200_000}
    )
    mw = build_compaction_middleware(model, context_window=200_000, ratio=0.5, keep_messages=20)
    out = mw.before_model(
        {"messages": [HumanMessage(content="hi")], "pinned_instruction": "hi"}, None
    )
    assert out is None


def test_empty_pin_no_injection():
    mw = _pinning_mw()
    out = mw.before_model({"messages": _five_messages(), "pinned_instruction": ""}, None)
    assert _pin_msgs(out["messages"]) == []


async def test_async_pin_injection_matches_sync():
    mw = _pinning_mw()
    out = await mw.abefore_model(
        {"messages": _five_messages(first="ASYNC ORIG"), "pinned_instruction": "ASYNC ORIG"}, None
    )
    pins = _pin_msgs(out["messages"])
    assert len(pins) == 1 and pins[0].content.endswith("ASYNC ORIG")


def test_trim_tokens_flows_into_middleware():
    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    mw = build_compaction_middleware(model, context_window=200_000, trim_tokens=8000)
    assert mw.trim_tokens_to_summarize == 8000


def test_trim_tokens_defaults_to_library_default_when_unset():
    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    mw = build_compaction_middleware(model, context_window=200_000)
    assert mw.trim_tokens_to_summarize == 4000


def test_summary_input_tokens_config_default(base_config):
    assert base_config.compaction.summary_input_tokens == 8000


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
