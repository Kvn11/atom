"""TitleMiddleware — auto-generate a thread title once, after the first completed exchange."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage


class TitleMiddleware(AgentMiddleware):
    def __init__(self, model: BaseChatModel):
        super().__init__()
        self.model = model

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if state.get("title"):
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        # Only at a natural turn boundary (a final assistant message, not a tool call).
        if not (isinstance(last, AIMessage) and not last.tool_calls):
            return None
        first_human = next((m for m in messages if isinstance(m, HumanMessage)), None)
        if first_human is None:
            return None
        try:
            prompt = (
                "Write a concise 3-6 word title (no quotes, no punctuation at the end) for a task "
                f"that begins:\n\n{str(first_human.content)[:500]}"
            )
            from atom.messages import message_text

            resp = self.model.invoke([HumanMessage(content=prompt)])
            title = message_text(resp).strip().strip('"').splitlines()[0][:80]
        except Exception:  # noqa: BLE001 - title generation must never break a run
            return None
        return {"title": title} if title else None
