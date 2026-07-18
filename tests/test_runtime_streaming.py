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
async def test_subagent_output_does_not_leak_into_lead_live_stream(base_config):
    """A delegated child's own token stream must never surface as text_delta/thinking_delta in the
    lead's live on_event stream (see atom/streaming.py's translate_message_chunk atom_subagent
    filter + SubagentRunner._child_config's marker). The child's answer legitimately appears as
    the delegate_task tool_result -- that's the intended delegation contract, not a leak."""
    events = []
    async def on_event(e): events.append(e)
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[{
            "name": "delegate_task",
            "args": {"description": "compute", "prompt": "reply with SUBAGENT_ONLY_TEXT",
                     "subagent_type": "general-purpose"},
            "id": "d1", "type": "tool_call",
        }]),
        AIMessage(content="SUBAGENT_ONLY_TEXT"),  # the child's own answer
        AIMessage(content="LEAD_FINAL_TEXT"),     # the lead's final answer
    ])
    result = await run_agent("delegate something", config=base_config, prepared=prepared,
                             on_event=on_event)

    assert "LEAD_FINAL_TEXT" in result.final_text  # the run completed with the lead's final answer

    live_text = "".join(
        e.get("text", "") for e in events if e["type"] in ("text_delta", "thinking_delta")
    )
    assert "LEAD_FINAL_TEXT" in live_text        # the lead's own output DID stream live
    assert "SUBAGENT_ONLY_TEXT" not in live_text  # the child's own output did NOT leak into it

    # It's fine (and expected) for the child's answer to show up as the tool_result -- that's the
    # designed delegation surface (tool_call + tool_result), not a live-token leak.
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert any("SUBAGENT_ONLY_TEXT" in e.get("text", "") for e in tool_results)


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


@pytest.mark.asyncio
async def test_should_cancel_stops_stream_and_flags_result(base_config):
    async def on_event(e):
        pass
    prepared = make_streaming_prepared("alpha beta gamma")
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             on_event=on_event, should_cancel=lambda: True)
    assert result.cancelled is True
    assert result.final_text.strip() != "alpha beta gamma"   # stopped before completing the stream


@pytest.mark.asyncio
async def test_should_cancel_false_completes_normally(base_config):
    async def on_event(e):
        pass
    prepared = make_streaming_prepared("alpha beta gamma")
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             on_event=on_event, should_cancel=lambda: False)
    assert result.cancelled is False
    assert result.final_text.strip() == "alpha beta gamma"


@pytest.mark.asyncio
async def test_should_cancel_ignored_when_streaming_disabled(base_config):
    base_config.streaming.enabled = False
    prepared = make_prepared([AIMessage(content="plain")])
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             on_event=lambda e: None, should_cancel=lambda: True)
    assert result.cancelled is False   # ainvoke path has no cooperative check
    assert result.final_text == "plain"
