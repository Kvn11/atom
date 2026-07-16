"""Chunk->atom-event translation + coalescing emitter."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from atom.streaming import (
    StreamEmitter, translate_message_chunk, translate_update,
)


class _Chunk:
    """Minimal stand-in exposing content_blocks like an AIMessageChunk."""
    def __init__(self, blocks):
        self.content_blocks = blocks
        self.text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def test_translate_text_and_thinking_blocks():
    chunk = _Chunk([
        {"type": "reasoning", "reasoning": "let me think"},
        {"type": "text", "text": "hello"},
    ])
    out = translate_message_chunk(chunk, {})
    assert out == [
        {"type": "thinking_delta", "text": "let me think"},
        {"type": "text_delta", "text": "hello"},
    ]


def test_translate_filters_subagent_chunks():
    chunk = _Chunk([{"type": "text", "text": "child output"}])
    assert translate_message_chunk(chunk, {"atom_subagent": True}) == []
    assert translate_message_chunk(chunk, {"metadata": {"atom_subagent": True}}) == []


def test_translate_update_tool_call_and_result():
    ai = AIMessage(content="", tool_calls=[{"name": "bash", "args": {"cmd": "ls"}, "id": "c1", "type": "tool_call"}])
    tm = ToolMessage(content="file.txt", name="bash", tool_call_id="c1")
    assert translate_update([ai]) == [{"type": "tool_call", "id": "c1", "name": "bash", "args": {"cmd": "ls"}}]
    out = translate_update([tm])
    assert out[0]["type"] == "tool_result" and out[0]["name"] == "bash" and out[0]["text"] == "file.txt"
    assert out[0]["is_error"] is False


@pytest.mark.asyncio
async def test_emitter_coalesces_text_then_flushes_on_structural_event():
    sink = []
    async def pub(e): sink.append(e)
    em = StreamEmitter(pub, coalesce_ms=100000, coalesce_chars=100000)  # never auto-flush on size/time
    await em.emit({"type": "text_delta", "text": "he"})
    await em.emit({"type": "text_delta", "text": "llo"})
    assert sink == []                                   # buffered, not yet flushed
    await em.emit({"type": "tool_call", "name": "bash", "args": {}})
    assert sink == [{"type": "text_delta", "text": "hello"}, {"type": "tool_call", "name": "bash", "args": {}}]


@pytest.mark.asyncio
async def test_emitter_flushes_thinking_before_text():
    sink = []
    async def pub(e): sink.append(e)
    em = StreamEmitter(pub, coalesce_ms=100000, coalesce_chars=100000)
    await em.emit({"type": "thinking_delta", "text": "think"})
    await em.emit({"type": "text_delta", "text": "answer"})
    await em.aclose()
    assert sink == [{"type": "thinking_delta", "text": "think"}, {"type": "text_delta", "text": "answer"}]
