"""LoopDetectionMiddleware — stop the agent when it repeats the same tool call too many times."""

from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage


def _signature(call: dict) -> str:
    try:
        args = json.dumps(call.get("args", {}), sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        args = str(call.get("args"))
    return f"{call.get('name')}::{args}"


class LoopDetectionMiddleware(AgentMiddleware):
    def __init__(self, max_repeats: int = 5):
        super().__init__()
        self.max_repeats = max_repeats

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Count only the TRAILING run of an identical tool-call signature (interleaved tool results
        # don't break it). All-time counts would force-stop a benign command legitimately reused
        # across a long thread of otherwise-distinct work.
        target: str | None = None
        run = 0
        broke = False
        for msg in reversed(state.get("messages", [])):
            if not (isinstance(msg, AIMessage) and msg.tool_calls):
                continue
            for call in reversed(msg.tool_calls):
                sig = _signature(call)
                if target is None:
                    target, run = sig, 1
                elif sig == target:
                    run += 1
                else:
                    broke = True
                    break
            if broke:
                break
        if run >= self.max_repeats:
            return {
                "jump_to": "end",
                "messages": [
                    AIMessage(
                        content="I detected a repeated tool-call loop and stopped to avoid "
                        "spinning. Here's where things stand — please refine the request if needed."
                    )
                ],
            }
        return None
