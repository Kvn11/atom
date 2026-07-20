"""ToolOutputCapMiddleware — cap an oversized tool result before it enters history.

Outermost wrap_tool_call, so the capped ToolMessage is what gets persisted. The marker is written
AS AN INSTRUCTION TO THE MODEL: it says the output was truncated and how to recover the omitted part
(re-run narrower — grep/range/page). This shrinks the source of both context-window overflow and
oversized telemetry payloads.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from atom.limits import truncate_text


class ToolOutputCapMiddleware(AgentMiddleware):
    _MARKER = (
        "\n\n[atom: tool output truncated to fit context — {elided} of {total} characters elided "
        "(showing the first {head} and last {tail}). To see the omitted portion, re-run this tool "
        "with a narrower scope: grep/filter, a smaller range or page, or head/tail.]\n\n"
    )

    def __init__(self, max_chars: int = 100_000):
        super().__init__()
        self.max_chars = max_chars

    def _cap_message(self, msg: ToolMessage) -> ToolMessage:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            if len(content) <= self.max_chars:
                return msg
            return msg.model_copy(update={"content": truncate_text(
                content, max_chars=self.max_chars, marker_template=self._MARKER)})
        if isinstance(content, list):
            new_blocks = []
            for b in content:
                if isinstance(b, dict) and isinstance(b.get("text"), str) \
                        and len(b["text"]) > self.max_chars:
                    nb = dict(b)
                    nb["text"] = truncate_text(
                        b["text"], max_chars=self.max_chars, marker_template=self._MARKER)
                    new_blocks.append(nb)
                else:
                    new_blocks.append(b)
            return msg.model_copy(update={"content": new_blocks})
        return msg

    def _cap(self, result: Any) -> Any:
        if isinstance(result, ToolMessage):
            return self._cap_message(result)
        update = getattr(result, "update", None)   # Command-like: cap ToolMessages in .update
        if isinstance(update, dict) and isinstance(update.get("messages"), list):
            update["messages"] = [
                self._cap_message(m) if isinstance(m, ToolMessage) else m
                for m in update["messages"]
            ]
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return self._cap(handler(request))

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return self._cap(await handler(request))
