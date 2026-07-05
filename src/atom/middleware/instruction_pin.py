"""InstructionPinMiddleware — capture the thread's first user instruction, once.

The captured text lands in the ``pinned_instruction`` ThreadState channel (write-once) and is
re-injected verbatim on every compaction by :class:`PinnedSummarizationMiddleware`, so a long
run can never forget what it was asked to do. Runs in the ``before_agent`` group; ordering among
``before_agent`` hooks is immaterial because compaction runs later, in ``before_model``.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from atom.messages import message_text


class InstructionPinMiddleware(AgentMiddleware):
    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if state.get("pinned_instruction"):        # idempotent: already captured on turn 1
            return None
        for msg in state.get("messages", []):
            if isinstance(msg, HumanMessage):
                text = message_text(msg).strip()
                return {"pinned_instruction": text} if text else None
        return None
