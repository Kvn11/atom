"""RunEventBus: snapshot-then-live, late-join catch-up, close semantics, bounded buffer."""
from __future__ import annotations

import asyncio

import pytest

from atom.workflow.events import RunEventBus, channel_key


def test_channel_key():
    assert channel_key("r1", 0, "writer") == "r1:s0:writer"


@pytest.mark.asyncio
async def test_subscribe_gets_snapshot_then_live():
    bus = RunEventBus()
    k = "r:s0:t"
    await bus.publish(k, {"type": "text_delta", "text": "he"})
    await bus.publish(k, {"type": "text_delta", "text": "llo"})

    gen = bus.stream(k)
    first = await gen.__anext__()
    assert first["type"] == "snapshot"
    # coalesced into one trailing text block
    assert first["blocks"] == [{"type": "text_delta", "text": "hello"}]

    await bus.publish(k, {"type": "tool_call", "name": "bash", "args": {}})
    live = await gen.__anext__()
    assert live["type"] == "tool_call" and live["name"] == "bash"

    await bus.close(k)
    end = await gen.__anext__()
    assert end["type"] == "done"
    await gen.aclose()


@pytest.mark.asyncio
async def test_late_subscribe_after_close_gets_snapshot_then_done():
    bus = RunEventBus()
    k = "r:s0:t"
    await bus.publish(k, {"type": "text_delta", "text": "hi"})
    await bus.close(k)

    seen = [ev async for ev in bus.stream(k)]
    assert seen[0]["type"] == "snapshot"
    assert seen[0]["blocks"] == [{"type": "text_delta", "text": "hi"}]
    assert seen[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_close_with_error_yields_done_with_error_field():
    bus = RunEventBus()
    k = "r:s0:t"
    await bus.close(k, error="boom")
    seen = [ev async for ev in bus.stream(k)]
    assert seen[-1] == {"type": "done", "error": "boom"}  # terminal is always 'done'; failure carries error


@pytest.mark.asyncio
async def test_accumulator_bounds_trailing_text():
    bus = RunEventBus(max_chars=10)
    k = "r:s0:t"
    for _ in range(20):
        await bus.publish(k, {"type": "text_delta", "text": "xxxxx"})
    gen = bus.stream(k)
    snap = await gen.__anext__()
    assert len(snap["blocks"][-1]["text"]) <= 11  # bounded (elision prefix allowed)
    await bus.close(k)
    await gen.aclose()
