"""Sub-agent delegation runtime (``delegate_task``) — always on (deviation #9).

A ``SubagentRunner`` spawns an ephemeral child ``create_agent`` that shares the PARENT's
per-thread workspace/sandbox (the child's context carries the parent ``thread_id``, so its file
tools resolve against the same registered sandbox). Concurrency is bounded by a semaphore; each
child has a wall-clock timeout. Runners are looked up by parent thread_id.
"""

from __future__ import annotations

import asyncio
import datetime
import threading
import uuid
from dataclasses import dataclass
from typing import Literal

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from atom.models import clamp_concurrency
from atom.prompts import render_prompt
from atom.sandbox.paths import VIRTUAL_OUTPUTS, VIRTUAL_UPLOADS, VIRTUAL_WORKSPACE
from atom.state import ThreadState, WorkspaceContext

SubagentType = Literal["general-purpose", "bash"]

_SUBAGENT_PROMPTS = {
    "general-purpose": "@prompts/subagent_general.md",
    "bash": "@prompts/subagent_bash.md",
}

_USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def _sum_usage(messages: list) -> dict[str, int]:
    """Sum ``usage_metadata`` across a child's messages, to attribute to the parent."""
    total = {f: 0 for f in _USAGE_FIELDS}
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if um:
            for f in _USAGE_FIELDS:
                total[f] += int(um.get(f, 0) or 0)
    return {f: v for f, v in total.items() if v}


@dataclass
class SubagentRunner:
    model: BaseChatModel
    home: str
    context_window: int
    bash_enabled: bool
    config_dir: str | None = None
    summarizer: BaseChatModel | None = None  # enables child compaction when set
    compaction_ratio: float = 0.5
    max_concurrent: int = 3
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(clamp_concurrency(self.max_concurrent))

    def _child_tools(self, subagent_type: SubagentType) -> list:
        # Note: children get file tools (+bash) but NOT delegate_task — no nested delegation.
        from atom.tools.bash import bash
        from atom.tools.filesystem import FILESYSTEM_TOOLS

        tools = list(FILESYSTEM_TOOLS)
        if subagent_type == "bash" and self.bash_enabled:
            tools.append(bash)
        return tools

    def _child_middleware(self) -> list:
        """Minimal resilience so long-running children don't hard-fail on context overflow/loops."""
        from atom.middleware.dangling_tool_call import DanglingToolCallMiddleware
        from atom.middleware.loop_detection import LoopDetectionMiddleware
        from atom.middleware.tool_error import ToolErrorHandlingMiddleware

        mw: list = [DanglingToolCallMiddleware()]
        if self.summarizer is not None:
            from atom.middleware.compaction import build_compaction_middleware

            mw.append(
                build_compaction_middleware(
                    self.summarizer,
                    context_window=self.context_window,
                    ratio=self.compaction_ratio,
                    keep_messages=15,
                )
            )
        mw += [ToolErrorHandlingMiddleware(), LoopDetectionMiddleware()]
        return mw

    def _child_agent(self, subagent_type: SubagentType):
        frequent = [t.name for t in self._child_tools(subagent_type)]
        system = render_prompt(
            _SUBAGENT_PROMPTS[subagent_type],
            {
                "date": datetime.date.today().isoformat(),
                "workspace": VIRTUAL_WORKSPACE,
                "uploads": VIRTUAL_UPLOADS,
                "outputs": VIRTUAL_OUTPUTS,
                "frequent_tool_names": frequent,
            },
            self.config_dir,
        )
        return create_agent(
            model=self.model,
            tools=self._child_tools(subagent_type),
            system_prompt=system,
            middleware=self._child_middleware(),
            state_schema=ThreadState,
            context_schema=WorkspaceContext,
        )

    async def run(
        self, parent_thread_id: str, description: str, prompt: str, subagent_type: SubagentType
    ) -> tuple[str, dict[str, int]]:
        """Run a child agent; return ``(report_text, usage_delta)``."""
        async with self._sem:
            agent = self._child_agent(subagent_type)
            child_id = f"{parent_thread_id}:sub:{uuid.uuid4().hex[:8]}"
            # Share the parent workspace: context thread_id == parent so tools find the same sandbox.
            context: WorkspaceContext = {
                "thread_id": parent_thread_id,
                "home": self.home,
                "workspace_mode": "new",
                "allow_bash": self.bash_enabled and subagent_type == "bash",
                "supports_vision": False,
                "context_window": self.context_window,
            }
            try:
                result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": [HumanMessage(content=prompt)]},
                        config={"configurable": {"thread_id": child_id}, "recursion_limit": 60},
                        context=context,
                    ),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                return f"[sub-agent '{description}' timed out after {self.timeout_seconds}s]", {}
            except Exception as exc:  # noqa: BLE001
                return f"[sub-agent '{description}' failed: {type(exc).__name__}: {exc}]", {}
            from atom.messages import message_text

            messages = result.get("messages", [])
            usage = _sum_usage(messages)
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    text = message_text(msg)
                    if text.strip():
                        return text, usage
            return "[sub-agent produced no output]", usage


# ------------------------------------------------------------------ registry

_lock = threading.Lock()
_runners: dict[str, SubagentRunner] = {}


def register_runner(thread_id: str, runner: SubagentRunner) -> None:
    with _lock:
        _runners[thread_id] = runner


def get_runner(thread_id: str | None) -> SubagentRunner | None:
    if not thread_id:
        return None
    with _lock:
        return _runners.get(thread_id)


def unregister_runner(thread_id: str) -> None:
    with _lock:
        _runners.pop(thread_id, None)
