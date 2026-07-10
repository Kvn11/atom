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
