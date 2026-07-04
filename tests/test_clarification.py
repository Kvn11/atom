"""Clarification interrupt ends the turn; the reply resumes the same thread (dangling repair)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.config.schema import AgentProfile, AtomConfig
from atom.runtime import run_agent
from tests.conftest import make_prepared


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


@pytest.mark.asyncio
async def test_clarification_interrupt_then_resume(atom_home):
    # sqlite so state persists across the two run_agent calls (resume).
    cfg = AtomConfig(
        home=str(atom_home),
        checkpointer={"backend": "sqlite"},
        agents={"default": AgentProfile(model="haiku")},
    )
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc(
            "ask_clarification",
            {"question": "Which format do you want, JSON or CSV?",
             "clarification_type": "approach_choice", "options": ["JSON", "CSV"]},
            "clar_1",
        )]),
        AIMessage(content="Great, I'll use JSON."),
    ])

    first = await run_agent("export the data", config=cfg, prepared=prepared, thread_id="threadA")
    assert first.awaiting_clarification is True
    assert "JSON or CSV" in first.final_text
    assert "Options: JSON; CSV" in first.final_text

    # Resume on the same thread: the dangling ask_clarification call is repaired, model continues.
    second = await run_agent("JSON please", config=cfg, prepared=prepared, thread_id="threadA")
    assert second.awaiting_clarification is False
    assert second.final_text == "Great, I'll use JSON."
