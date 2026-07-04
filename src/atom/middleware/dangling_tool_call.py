"""DanglingToolCallMiddleware — repair tool_calls left unanswered by an interrupt/resume.

When a turn ends on an AIMessage that has tool_calls but no matching ToolMessages (e.g. after a
clarification jump-to-end), the next model call would fail provider tool-pairing validation. This
injects placeholder ToolMessages for the unanswered calls before the model runs.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage

try:
    from langgraph.graph.message import REMOVE_ALL_MESSAGES
except Exception:  # noqa: BLE001 - fall back to the documented sentinel value
    REMOVE_ALL_MESSAGES = "__remove_all__"

_PLACEHOLDER = "[No result: the previous turn ended before this tool ran.]"


class DanglingToolCallMiddleware(AgentMiddleware):
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        dangling = {
            call.get("id")
            for m in messages
            if isinstance(m, AIMessage) and m.tool_calls
            for call in m.tool_calls
            if call.get("id") and call.get("id") not in answered
        }
        if not dangling:
            return None
        # Rebuild history so each placeholder ToolMessage sits IMMEDIATELY after the AIMessage that
        # emitted it — before any resume HumanMessage. OpenAI/Gemini reject a 'tool' message that
        # doesn't directly follow its tool_calls; appending at the end would 400 on those providers.
        rebuilt: list[Any] = []
        seen = set(answered)
        for msg in messages:
            rebuilt.append(msg)
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for call in msg.tool_calls:
                    cid = call.get("id")
                    if cid and cid not in seen:
                        seen.add(cid)
                        rebuilt.append(ToolMessage(content=_PLACEHOLDER, tool_call_id=cid))
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt]}
