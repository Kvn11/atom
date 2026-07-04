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
