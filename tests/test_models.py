"""Per-provider thinking translation, the [2,4] concurrency clamp, and Qwen construction."""

from __future__ import annotations

from atom.models.registry import _thinking_overrides, build_model, clamp_concurrency, resolve_spec


def test_int_thinking_budget_coerced_and_applied():
    spec = resolve_spec("haiku")
    want = {"thinking": {"type": "enabled", "budget_tokens": 16000}}
    assert _thinking_overrides(spec, "16000") == want  # str-int coerced
    assert _thinking_overrides(spec, 16000) == want     # raw int honored


def test_adaptive_gated_to_opus():
    assert _thinking_overrides(resolve_spec("opus"), "adaptive") == {"thinking": {"type": "adaptive"}}
    downgraded = _thinking_overrides(resolve_spec("haiku"), "adaptive")
    assert downgraded["thinking"]["type"] == "enabled"  # non-Opus adaptive downgraded, not sent raw


def test_effort_strings_vary_budget_for_gemini_and_qwen():
    low = _thinking_overrides(resolve_spec("gemini-pro"), "low")
    high = _thinking_overrides(resolve_spec("gemini-pro"), "high")
    assert isinstance(low["thinking_budget"], int) and low["thinking_budget"] > 0
    assert high["thinking_budget"] > low["thinking_budget"]  # the effort dial is not inert

    q_low = _thinking_overrides(resolve_spec("qwen-max"), "low")
    q_high = _thinking_overrides(resolve_spec("qwen-max"), "high")
    assert q_low["enable_thinking"] is True
    assert q_high["thinking_budget"] > q_low["thinking_budget"]


def test_off_disables_each_provider():
    assert _thinking_overrides(resolve_spec("haiku"), "off") == {}
    assert _thinking_overrides(resolve_spec("gpt5-mini"), "off") == {"reasoning_effort": "minimal"}
    assert _thinking_overrides(resolve_spec("gemini-pro"), "off") == {"thinking_budget": 0}
    assert _thinking_overrides(resolve_spec("qwen-max"), "off") == {"enable_thinking": False}


def test_gemini_3_uses_thinking_level_not_budget():
    spec = resolve_spec("gemini-3.5-flash")
    assert spec.provider == "google_genai"
    assert spec.model_name == "gemini-3.5-flash"
    assert spec.supports_reasoning is True
    assert spec.context_window == 1_000_000
    # Gemini 3+ takes the thinking_level enum, NOT the deprecated integer thinking_budget.
    assert _thinking_overrides(spec, "high") == {"thinking_level": "high"}
    assert _thinking_overrides(spec, "medium") == {"thinking_level": "medium"}
    assert _thinking_overrides(spec, "off") == {"thinking_level": "minimal"}  # 3+ floors, can't disable
    assert _thinking_overrides(spec, 24576) == {"thinking_level": "high"}      # int budget -> nearest level
    assert "thinking_budget" not in _thinking_overrides(spec, "high")


def test_gemini_25_still_uses_thinking_budget():
    # Regression: the 2.5 models keep the integer-budget path unchanged.
    assert _thinking_overrides(resolve_spec("gemini-pro"), "high") == {"thinking_budget": 24576}
    assert _thinking_overrides(resolve_spec("gemini-flash"), "off") == {"thinking_budget": 0}


def test_clamp_concurrency_enforces_2_to_4():
    assert clamp_concurrency(1) == 2   # floor
    assert clamp_concurrency(3) == 3   # in band
    assert clamp_concurrency(10) == 4  # ceiling


def test_build_model_uses_chatqwen_for_qwen_and_init_for_others(monkeypatch):
    calls: dict = {}

    class FakeQwen:
        def __init__(self, **kw):
            calls["qwen"] = kw

    def fake_init(init_str, **kw):
        calls["init"] = (init_str, kw)
        return "MODEL"

    import langchain_qwq
    import langchain.chat_models

    monkeypatch.setattr(langchain_qwq, "ChatQwen", FakeQwen)
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init)

    build_model("qwen-max", thinking="off")
    assert "qwen" in calls and "init" not in calls  # Qwen must NOT go through init_chat_model

    build_model("haiku", thinking="off")
    assert calls["init"][0] == "anthropic:claude-haiku-4-5"


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


def test_bedrock_registry_entries_present_and_typed():
    opus = resolve_spec("bedrock-opus")
    assert opus.provider == "bedrock"
    assert opus.wire == "anthropic"
    assert opus.model_name == "us.anthropic.claude-opus-4-8"
    assert opus.init_str is None          # custom-factory path, not init_chat_model
    assert opus.context_window == 1_000_000
    assert opus.max_output_tokens == 128_000

    coder = resolve_spec("bedrock-qwen-coder")
    assert coder.wire == "openai"
    assert coder.supports_reasoning is False
    assert coder.model_name == "qwen.qwen3-coder-480b-a35b-v1:0"

    # All eight bedrock-* keys exist and are the bedrock provider with a wire set.
    keys = ["bedrock-opus", "bedrock-sonnet", "bedrock-haiku", "bedrock-qwen-coder",
            "bedrock-qwen", "bedrock-kimi-thinking", "bedrock-kimi", "bedrock-gpt-oss"]
    for k in keys:
        s = resolve_spec(k)
        assert s.provider == "bedrock"
        assert s.wire in ("anthropic", "openai")
        assert s.base_url is None          # gateway root is env-sourced, not baked in


def test_bedrock_anthropic_wire_reuses_anthropic_thinking():
    spec = resolve_spec("bedrock-opus")  # bedrock id: us.anthropic.claude-opus-4-8
    # adaptive must be recognized despite the "us.anthropic." prefix (substring match, not startswith)
    assert _thinking_overrides(spec, "adaptive") == {"thinking": {"type": "adaptive"}}
    assert _thinking_overrides(spec, 8192) == {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    assert _thinking_overrides(spec, "off") == {}


def test_bedrock_openai_wire_reasoning_passthrough():
    spec = resolve_spec("bedrock-kimi-thinking")  # supports_reasoning=True, wire=openai
    assert _thinking_overrides(spec, "high") == {"extra_body": {"reasoning": {"max_tokens": 24576}}}
    assert _thinking_overrides(spec, 500) == {"extra_body": {"reasoning": {"max_tokens": 1024}}}  # floored
    assert _thinking_overrides(spec, "off") == {}


def test_bedrock_openai_wire_no_reasoning_for_nonreasoning_model():
    spec = resolve_spec("bedrock-qwen-coder")  # supports_reasoning=False
    assert _thinking_overrides(spec, "high") == {}


def test_direct_anthropic_thinking_unchanged_after_refactor():
    # Regression: the existing direct-Anthropic behavior must be identical after extracting the helper.
    assert _thinking_overrides(resolve_spec("opus"), "adaptive") == {"thinking": {"type": "adaptive"}}
    assert _thinking_overrides(resolve_spec("haiku"), 16000) == {
        "thinking": {"type": "enabled", "budget_tokens": 16000}}
    assert _thinking_overrides(resolve_spec("haiku"), "adaptive")["thinking"]["type"] == "enabled"
