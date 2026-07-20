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
from dataclasses import dataclass, field
from typing import Any, Literal

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
    summary_input_tokens: int = 8000
    summary_prompt: str | None = None  # atom's summary.md (resolved, not Jinja-rendered); None -> library default
    max_concurrent: int = 3
    timeout_seconds: int = 900
    recursion_limit: int = 300  # max LangGraph super-steps per child run (~N/11 agent turns)
    base_trace: dict | None = None       # enriched lead trace; None -> sub-agent runs untraced
    observability: Any = None            # ObservabilityConfig | None
    obs_provider: Any = None             # ObservabilityProvider | None (LangFuse callbacks + session)
    retry: Any = None                    # RetryPolicy | None; wired in Task 3 (child model retry/backoff)
    skill_catalog: list = field(default_factory=list)  # [{"name","description"}] always-on catalog
    has_skill_library: bool = False      # a skill_library/ exists -> bind search_skills
    notes: dict | None = None            # per-workflow Obsidian vault ctx (vault/root_dir); bash children only
    overflow_recovery: bool = True
    overflow_max_attempts: int = 3
    overflow_target_ratio: float = 0.5
    max_tool_output_chars: int = 100_000

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(clamp_concurrency(self.max_concurrent))

    def _child_config(self, child_id: str) -> dict:
        return {
            "configurable": {"thread_id": child_id},
            "recursion_limit": self.recursion_limit,
            # Tag child runs so their streamed model deltas are filtered out of the lead's live
            # stream (see atom.streaming.translate_message_chunk). Metadata is key-overridden per
            # run in LangSmith, so this stays isolated to the child and never leaks upward.
            "metadata": {"atom_subagent": True},
        }

    def _child_tools(self, subagent_type: SubagentType) -> list:
        # Note: children get file tools (+bash) but NOT delegate_task — no nested delegation.
        from atom.tools.bash import bash
        from atom.tools.filesystem import FILESYSTEM_TOOLS
        from atom.tools.search import load_skill, search_skills

        tools = list(FILESYSTEM_TOOLS)
        if subagent_type == "bash" and self.bash_enabled:
            tools.append(bash)
        if self.skill_catalog or self.has_skill_library:
            tools.append(load_skill)
        if self.has_skill_library:
            tools.append(search_skills)
        return tools

    def _child_middleware(self) -> list:
        """Pin the delegated prompt and add resilience (retry, compaction, dangling-call repair,
        tool-error, loop detection) so long-running children survive transient provider errors,
        context overflow, and loops."""
        from atom.middleware.context_overflow import ContextOverflowMiddleware
        from atom.middleware.dangling_tool_call import DanglingToolCallMiddleware
        from atom.middleware.instruction_pin import InstructionPinMiddleware
        from atom.middleware.llm_error import LLMErrorHandlingMiddleware, RetryPolicy
        from atom.middleware.loop_detection import LoopDetectionMiddleware
        from atom.middleware.tool_error import ToolErrorHandlingMiddleware
        from atom.middleware.tool_output_cap import ToolOutputCapMiddleware

        mw: list = [InstructionPinMiddleware(), DanglingToolCallMiddleware()]
        if self.summarizer is not None:
            from atom.middleware.compaction import build_compaction_middleware

            mw.append(
                build_compaction_middleware(
                    self.summarizer,
                    context_window=self.context_window,
                    ratio=self.compaction_ratio,
                    keep_messages=15,
                    trim_tokens=self.summary_input_tokens,
                    summary_prompt=self.summary_prompt,
                )
            )
        if self.skill_catalog or self.has_skill_library:
            from atom.middleware.skill_library import SkillLibraryMiddleware

            mw.append(SkillLibraryMiddleware(self.home))
        mw += [
            LLMErrorHandlingMiddleware(self.retry or RetryPolicy()),  # retry, then raise on exhaustion
            ContextOverflowMiddleware(                                # innermost model wrap
                context_window=self.context_window,
                max_attempts=self.overflow_max_attempts,
                target_ratio=self.overflow_target_ratio,
                enabled=self.overflow_recovery,
            ),
            ToolOutputCapMiddleware(self.max_tool_output_chars),      # outermost tool wrap
            ToolErrorHandlingMiddleware(),
            LoopDetectionMiddleware(),
        ]
        return mw

    def _child_system(self, subagent_type: SubagentType) -> str:
        frequent = [t.name for t in self._child_tools(subagent_type)]
        return render_prompt(
            _SUBAGENT_PROMPTS[subagent_type],
            {
                "date": datetime.date.today().isoformat(),
                "workspace": VIRTUAL_WORKSPACE,
                "uploads": VIRTUAL_UPLOADS,
                "outputs": VIRTUAL_OUTPUTS,
                "frequent_tool_names": frequent,
                "skill_catalog": list(self.skill_catalog),
                # Only the bash prompt renders a notes block; general-purpose children have no bash
                # and their file tools are workspace-confined, so they cannot reach the vault CLI.
                "notes": self.notes,
            },
            self.config_dir,
        )

    def _child_agent(self, subagent_type: SubagentType, system: str | None = None):
        system = system or self._child_system(subagent_type)
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
            system_text = self._child_system(subagent_type)
            agent = self._child_agent(subagent_type, system=system_text)
            child_id = f"{parent_thread_id}:sub:{uuid.uuid4().hex[:8]}"
            config = self._child_config(child_id)
            if self.base_trace is not None and self.observability is not None:
                from atom.observability import _apply_trace, build_subagent_trace

                _apply_trace(config, build_subagent_trace(
                    self.base_trace, parent_thread_id=parent_thread_id,
                    subagent_type=subagent_type, description=description,
                    rendered_prompt=system_text,
                    subagent_prompt_ref=_SUBAGENT_PROMPTS[subagent_type],
                    recursion_limit=self.recursion_limit, obs=self.observability,
                ))
            if self.obs_provider is not None:
                # Attach LangFuse callbacks + stamp langfuse_session_id = run_id so this sub-agent
                # joins the whole run's session. Runs after the trace merge, so run_id is present.
                self.obs_provider.decorate_run_config(config)
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
                        config=config,
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
