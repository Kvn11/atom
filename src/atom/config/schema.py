"""Pydantic config schema for atom.

The whole harness is config-driven so it can be reused as a foundation: models, prompts, the
frequent tool/skill sets, compaction, and subagent limits all live here. Notably, *workspace
mode is NOT here* — it is a per-run argument (see :class:`atom.state.WorkspaceContext`).
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Defaults(_Base):
    user_id: str = "default"
    agent: str = "default"  # default profile name


class CheckpointerConfig(_Base):
    backend: Literal["sqlite", "memory"] = "sqlite"


class SandboxConfig(_Base):
    bash_enabled: bool = True
    # Absolute roots an "existing" workspace may be bound under (empty = allow any existing dir).
    allowed_workspace_roots: list[str] = Field(default_factory=list)


class CompactionConfig(_Base):
    # Fraction of the selected model's context window that triggers summarization (deviation #5).
    ratio: float = 0.5
    keep_messages: int = 20
    # How much conversation history the summarizer reads when building a summary
    # (trim_tokens_to_summarize). Higher = richer summaries at more summarizer cost.
    summary_input_tokens: int = 8000


class SubagentConfig(_Base):
    max_concurrent: int = 3  # clamped to [2, 4] at build time
    timeout_seconds: int = 900
    # Max LangGraph super-steps per delegated child run. atom's middleware chain costs ~11
    # super-steps per model turn, so this is ~N/11 agent turns (300 -> ~27 turns).
    recursion_limit: int = 300


class LibraryConfig(_Base):
    search: Literal["bm25"] = "bm25"
    auto_promote_k: int = 3
    min_score: float = 0.0


class WorkflowConfig(_Base):
    # Max tasks run concurrently within a single step; per-task wall-clock timeout.
    # 0 or negative disables the per-task timeout.
    max_parallel: int = 4
    task_timeout_seconds: int = 1800


class QueueConfig(_Base):
    # How many workflow RUNS execute at once (distinct from workflow.max_parallel, which caps
    # TASKS within a step). Default 1 = strictly one workflow at a time. Raise as compute grows.
    max_concurrent_runs: int = 1
    # How often the worker re-scans the store for cross-process enqueues + orphaned runs.
    # In-process API enqueues wake it instantly via an event; this only bounds cross-process latency.
    poll_interval_seconds: float = 3.0
    # A run whose execute() fails BEFORE writing a terminal status (e.g. an unreadable run.json)
    # stays queued and would be re-picked forever; after this many consecutive failed drain
    # attempts the worker quarantines it (skips until restart) instead of hot-looping.
    max_drain_attempts: int = 5


class RetryConfig(_Base):
    # Transient-provider-error retry for every model call (lead + sub-agents + summarizer).
    # 20 attempts with full-jitter exponential backoff, then the task fails.
    max_retries: int = 20
    base_delay: float = 1.0     # seconds; first backoff
    max_delay: float = 30.0     # seconds; per-attempt cap
    jitter: bool = True         # full jitter on every delay


class GuardrailConfig(_Base):
    enabled: bool = False


class ObservabilityConfig(_Base):
    # LangSmith tracing for workflow runs. Layered over LANGSMITH_* env vars (env wins).
    enabled: bool = False               # -> LANGSMITH_TRACING=true (only if API key present & env unset)
    project: Optional[str] = None       # -> LANGSMITH_PROJECT (only if env unset)
    default_tags: list[str] = Field(default_factory=list)  # tags added to every workflow run
    include_prompt_fingerprint: bool = True  # add system/summary prompt ref + content hash to metadata
    capture_git_sha: bool = True        # best-effort atom_git_sha in metadata


class ToolsConfig(_Base):
    # Tools auto-bound/injected into the lead agent (everything else is library-deferred).
    frequent: list[str] = Field(
        default_factory=lambda: [
            "read_file", "write_file", "edit_file", "bash", "ls", "grep", "glob",
            "present_files", "view_image",
        ]
    )


class SkillsConfig(_Base):
    frequent: list[str] = Field(default_factory=list)


class AgentProfile(_Base):
    """One project's lead agent, defined entirely in config."""

    model: str = "haiku"
    summarizer_model: Optional[str] = None  # None -> reuse the lead model
    # None/off | adaptive (opus) | minimal|low|medium|high | int token budget (int or "16000").
    thinking: Optional[Union[str, int]] = None
    # Prompts: inline string OR "@relative/or/abs/path". Resolved at render time.
    system_prompt: str = "@prompts/lead_system.md"
    user_prompt: Optional[str] = None  # optional wrapper for the incoming task
    # Compaction summary prompt (inline OR @file); must contain the "{messages}" placeholder.
    summary_prompt: Optional[str] = "@prompts/summary.md"
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    subagents: SubagentConfig = Field(default_factory=SubagentConfig)
    # Max LangGraph super-steps for a lead/workflow-task run. atom's middleware chain costs
    # ~11 super-steps per model turn, so 400 -> ~36 turns (the old hardcoded 100 gave only ~9,
    # which killed legitimate multi-step tasks mid-work). Loop detection remains the real
    # runaway guard; this is a backstop, so keep it generous.
    recursion_limit: int = 400


class AtomConfig(_Base):
    home: Optional[str] = None  # None -> $ATOM_HOME or ~/.atom
    defaults: Defaults = Field(default_factory=Defaults)
    checkpointer: CheckpointerConfig = Field(default_factory=CheckpointerConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    track_usage: bool = True
    agents: dict[str, AgentProfile] = Field(default_factory=lambda: {"default": AgentProfile()})

    # Directory of the loaded config file (used to resolve "@file" prompt refs). Not serialized.
    config_dir: Optional[str] = Field(default=None, exclude=True)

    def profile(self, name: str | None = None) -> AgentProfile:
        name = name or self.defaults.agent
        if name not in self.agents:
            raise KeyError(f"Unknown agent profile '{name}'. Defined: {', '.join(self.agents)}.")
        return self.agents[name]
