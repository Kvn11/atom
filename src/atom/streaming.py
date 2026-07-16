"""Translate a LangChain agent stream into atom's provider-agnostic event dicts, and coalesce
text/thinking deltas before publishing. Pure of transport (no bus / HTTP dependency).

Event dicts (the wire contract consumed by RunEventBus and the SSE endpoint):
  {"type": "thinking_delta", "text": str}
  {"type": "text_delta",     "text": str}
  {"type": "tool_call",      "id": str|None, "name": str|None, "args": dict}
  {"type": "tool_result",    "tool_call_id": str|None, "name": str|None, "text": str, "is_error": bool}
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, ToolMessage

from atom.messages import message_text

THINKING = "thinking_delta"
TEXT = "text_delta"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"


def _is_subagent(metadata: dict | None) -> bool:
    md = metadata or {}
    if md.get("atom_subagent"):
        return True
    inner = md.get("metadata")
    return bool(isinstance(inner, dict) and inner.get("atom_subagent"))


def translate_message_chunk(chunk: Any, metadata: dict | None) -> list[dict]:
    """Emit thinking/text deltas from a streamed message chunk. Sub-agent chunks are dropped."""
    if _is_subagent(metadata):
        return []
    out: list[dict] = []
    blocks = getattr(chunk, "content_blocks", None)
    if not blocks:
        text = getattr(chunk, "text", "") or ""
        if text:
            out.append({"type": TEXT, "text": text})
        return out
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("reasoning", "thinking"):
            txt = b.get("reasoning") or b.get("thinking") or b.get("text") or ""
            if txt:
                out.append({"type": THINKING, "text": txt})
        elif t == "text":
            txt = b.get("text") or ""
            if txt:
                out.append({"type": TEXT, "text": txt})
    return out


def translate_update(messages: list) -> list[dict]:
    """Emit tool_call events (from a completed AIMessage) and tool_result events (from a ToolMessage).
    Never emits assistant text — that arrives token-by-token via translate_message_chunk (no dup)."""
    out: list[dict] = []
    for m in messages or []:
        if isinstance(m, AIMessage):
            for tc in (m.tool_calls or []):
                out.append({"type": TOOL_CALL, "id": tc.get("id"),
                            "name": tc.get("name"), "args": tc.get("args", {})})
        elif isinstance(m, ToolMessage):
            out.append({"type": TOOL_RESULT,
                        "tool_call_id": getattr(m, "tool_call_id", None),
                        "name": getattr(m, "name", None),
                        "text": message_text(m),
                        "is_error": getattr(m, "status", None) == "error"})
    return out


class StreamEmitter:
    """Coalesces consecutive text/thinking deltas (by ms window OR char count) before publishing,
    and flushes pending text before any structural event and on close. Event-driven (no timer task):
    the time check fires on each incoming delta, which is exactly when a high-rate stream needs it."""

    def __init__(self, publish: Callable[[dict], Awaitable[None]], *,
                 coalesce_ms: int, coalesce_chars: int):
        self._publish = publish
        self._coalesce_s = max(0, coalesce_ms) / 1000.0
        self._coalesce_chars = max(1, coalesce_chars)
        self._buf_type: str | None = None
        self._buf_text: str = ""
        self._last_flush: float = 0.0
        self._started = False

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def emit(self, event: dict) -> None:
        t = event.get("type")
        if t in (TEXT, THINKING):
            if not self._started:
                self._last_flush = self._now()
                self._started = True
            if self._buf_type and self._buf_type != t:
                await self._flush()
            self._buf_type = t
            self._buf_text += event.get("text", "")
            if (len(self._buf_text) >= self._coalesce_chars
                    or (self._now() - self._last_flush) >= self._coalesce_s):
                await self._flush()
        else:
            await self._flush()
            await self._publish(event)

    async def _flush(self) -> None:
        if self._buf_text:
            await self._publish({"type": self._buf_type, "text": self._buf_text})
            self._buf_text = ""
            self._last_flush = self._now()

    async def aclose(self) -> None:
        await self._flush()
