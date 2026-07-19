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
