"""InstructionPinMiddleware captures the thread's first user instruction, once."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from atom.middleware.instruction_pin import InstructionPinMiddleware


def test_captures_first_human_message():
    mw = InstructionPinMiddleware()
    out = mw.before_agent({"messages": [HumanMessage(content="DO THE THING")]}, None)
    assert out == {"pinned_instruction": "DO THE THING"}


def test_captures_text_from_list_content():
    mw = InstructionPinMiddleware()
    content = [{"type": "text", "text": "REAL TASK"}, {"type": "thinking", "thinking": "hmm"}]
    out = mw.before_agent({"messages": [HumanMessage(content=content)]}, None)
    assert out == {"pinned_instruction": "REAL TASK"}


def test_skips_leading_non_human_messages():
    mw = InstructionPinMiddleware()
    msgs = [AIMessage(content="preamble"), HumanMessage(content="THE TASK")]
    out = mw.before_agent({"messages": msgs}, None)
    assert out == {"pinned_instruction": "THE TASK"}


def test_idempotent_when_already_set():
    mw = InstructionPinMiddleware()
    out = mw.before_agent(
        {"messages": [HumanMessage(content="NEW")], "pinned_instruction": "OLD"}, None
    )
    assert out is None


def test_no_human_message_returns_none():
    mw = InstructionPinMiddleware()
    out = mw.before_agent({"messages": [AIMessage(content="hi")]}, None)
    assert out is None


def test_empty_first_human_returns_none():
    mw = InstructionPinMiddleware()
    out = mw.before_agent({"messages": [HumanMessage(content="   ")]}, None)
    assert out is None
