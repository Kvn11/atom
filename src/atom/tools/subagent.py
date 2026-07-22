"""``delegate_task`` — delegate a self-contained subtask to a sub-agent."""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from atom.subagent import get_runner
from atom.tools.common import thread_id_of


@tool(parse_docstring=True)
async def delegate_task(
    runtime: ToolRuntime,
    description: str,
    prompt: str,
    subagent_type: Literal["general-purpose", "bash"] = "general-purpose",
) -> Command:
    """Delegate a well-scoped subtask to a sub-agent that shares your workspace.

    Use this to parallelize or offload focused work (research a directory, run a build, draft a
    file). The sub-agent starts fresh, so give it a complete, self-contained instruction. It
    returns a single report.

    Args:
        description: One-line summary of the subtask (for logs/UI).
        prompt: The full, self-contained instruction for the sub-agent.
        subagent_type: 'general-purpose' (file tools) or 'bash' (adds shell access).
    """
    tcid = runtime.tool_call_id
    runner = get_runner(thread_id_of(runtime))
    if runner is None:
        return Command(update={"messages": [ToolMessage(
            "[sub-agent delegation is unavailable in this run]",
            tool_call_id=tcid, status="error")]})
    text, usage, failed = await runner.run(thread_id_of(runtime), description, prompt, subagent_type)
    update: dict = {"messages": [ToolMessage(
        text, tool_call_id=tcid, status="error" if failed else "success")]}
    if usage:  # attribute the child's token usage to the parent run
        update["usage"] = usage
    return Command(update=update)
