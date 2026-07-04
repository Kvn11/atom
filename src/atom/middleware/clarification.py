"""ClarificationMiddleware — end the turn when the model asks the user a question.

MUST be last in the chain. Because after_model hooks unwind in reverse, being last makes this the
first after-hook to run, so it can end the turn before any other post-processing. The pending
``ask_clarification`` tool call is intentionally left unanswered; DanglingToolCallMiddleware
repairs it when the user's reply resumes the thread.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage

CLARIFICATION_TOOL = "ask_clarification"


def pending_clarification(messages: list) -> dict | None:
    """Return the ask_clarification tool call on the last AIMessage, if any."""
    if not messages:
        return None
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        for call in last.tool_calls:
            if call.get("name") == CLARIFICATION_TOOL:
                return call
    return None


class ClarificationMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if pending_clarification(state.get("messages", [])) is not None:
            return {"jump_to": "end"}
        return None
