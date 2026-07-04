"""Subagent middlewares: wire the ``delegate_task`` tool and cap fan-out."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from atom.middleware.context import ctx_dict
from atom.models import clamp_concurrency
from atom.subagent import SubagentRunner, register_runner, unregister_runner


class SubagentMiddleware(AgentMiddleware):
    """Registers the per-thread SubagentRunner and contributes the ``delegate_task`` tool."""

    def __init__(self, runner: SubagentRunner):
        super().__init__()
        self.runner = runner
        from atom.tools.subagent import delegate_task

        self.tools = [delegate_task]

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = ctx_dict(runtime)
        if ctx.get("thread_id"):
            register_runner(ctx["thread_id"], self.runner)
        return None

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Free the runner (and its heavy model ref) at turn end; a resume re-registers it.
        tid = ctx_dict(runtime).get("thread_id")
        if tid:
            unregister_runner(tid)
        return None


class SubagentLimitMiddleware(AgentMiddleware):
    """Truncate excess parallel ``delegate_task`` calls to enforce the [2,4] concurrency cap."""

    def __init__(self, max_concurrent: int = 3):
        super().__init__()
        self.max_concurrent = clamp_concurrency(max_concurrent)

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        if not (isinstance(last, AIMessage) and last.tool_calls):
            return None
        task_calls = [c for c in last.tool_calls if c.get("name") == "delegate_task"]
        if len(task_calls) <= self.max_concurrent:
            return None
        keep_ids = {c["id"] for c in task_calls[: self.max_concurrent]}
        drop_ids = {c["id"] for c in task_calls[self.max_concurrent :]}
        trimmed = [
            c for c in last.tool_calls if c.get("name") != "delegate_task" or c["id"] in keep_ids
        ]
        update: dict[str, Any] = {"tool_calls": trimmed}
        # Anthropic carries tool_use blocks in list content; drop the ones we trimmed so no
        # orphaned tool_use (without a tool_result) is re-sent next turn -> provider 400.
        if isinstance(last.content, list):
            update["content"] = [
                b
                for b in last.content
                if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in drop_ids)
            ]
        # Same message id -> add_messages replaces the original in place.
        return {"messages": [last.model_copy(update=update)]}
