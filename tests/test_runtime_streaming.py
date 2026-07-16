"""run_agent streaming path: emits events, preserves the RunResult contract, no-op when disabled."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.runtime import run_agent
from tests.conftest import make_prepared, make_streaming_prepared


@pytest.mark.asyncio
async def test_streaming_emits_text_deltas_and_preserves_result(base_config):
    events = []
    async def on_event(e): events.append(e)
    prepared = make_streaming_prepared("alpha beta gamma")
    result = await run_agent("hi", config=base_config, prepared=prepared, on_event=on_event)
    # contract preserved
    assert result.final_text.strip() == "alpha beta gamma"
    assert result.awaiting_clarification is False
    # some text streamed before the end
    assert any(e["type"] == "text_delta" for e in events)
    assert "".join(e.get("text", "") for e in events if e["type"] == "text_delta").strip() == "alpha beta gamma"


@pytest.mark.asyncio
async def test_streaming_emits_tool_events(base_config, atom_home):
    events = []
    async def on_event(e): events.append(e)
    ws = "/mnt/user-data/workspace"
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[{"name": "write_file",
            "args": {"description": "w", "path": f"{ws}/o.txt", "content": "x\n"}, "id": "c1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             workspace=str(atom_home), on_event=on_event)
    assert result.final_text == "done"
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds


@pytest.mark.asyncio
async def test_no_on_event_is_unchanged(base_config):
    prepared = make_prepared([AIMessage(content="plain")])
    result = await run_agent("hi", config=base_config, prepared=prepared)  # on_event=None
    assert result.final_text == "plain"


@pytest.mark.asyncio
async def test_streaming_disabled_config_skips_stream(base_config):
    base_config.streaming.enabled = False
    events = []
    async def on_event(e): events.append(e)
    prepared = make_prepared([AIMessage(content="plain")])
    result = await run_agent("hi", config=base_config, prepared=prepared, on_event=on_event)
    assert result.final_text == "plain"
    assert events == []  # streaming disabled -> ainvoke path, no events
