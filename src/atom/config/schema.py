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
    # Reactive emergency recovery when a model call overflows the context window (input too big).
    # When off, the first overflow raises ContextOverflowError immediately (no shrink-and-retry).
    overflow_recovery: bool = True
    overflow_max_attempts: int = 3          # shrink-and-retry rounds before failing clean
    overflow_target_ratio: float = 0.5      # first trim target as a fraction of the context window


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


class StreamingConfig(_Base):
    # Live-stream the lead agent's thinking/text/tool activity to the run view as a task runs.
    # False -> run_agent uses ainvoke and the SSE endpoint 404s (the UI falls back to polling).
    enabled: bool = True
    # Batch text/thinking deltas over this window (ms) OR this many chars, whichever first, before
    # publishing — bounds SSE frame + React re-render frequency for high-rate token streams.
    coalesce_ms: int = 50
    coalesce_chars: int = 240
    # Per-channel catch-up buffer cap; the trailing text block is elided past this to bound memory.
    accumulator_max_chars: int = 20000
    # Per-subscriber queue depth before deltas are dropped (client re-syncs from a fresh snapshot).
    subscriber_queue_max: int = 512
    # Keep this many completed channels so a subscriber joining just after completion still catches up.
    retain_closed: int = 64
    # SSE keep-alive ping cadence (seconds).
    heartbeat_seconds: float = 15.0


class UploadsConfig(_Base):
    # Limits for workflow file-input uploads. The API is unauthenticated with open CORS, so
    # these caps are the primary guard on an otherwise unbounded input surface.
    max_file_bytes: int = 26_214_400        # 25 MiB per file
    allowed_extensions: list[str] = Field(default_factory=list)  # empty = allow any; else lowercase, no dot
    max_files_per_run: int = 20


class RetryConfig(_Base):
    # Transient-provider-error retry for every model call (lead + sub-agents + summarizer).
    # 20 attempts with full-jitter exponential backoff, then the task fails.
    max_retries: int = 20
    base_delay: float = 1.0     # seconds; first backoff
    max_delay: float = 30.0     # seconds; per-attempt cap
    jitter: bool = True         # full jitter on every delay


class GuardrailConfig(_Base):
    enabled: bool = False


class TodosConfig(_Base):
    # When true, if the lead agent ends a turn with incomplete todos, nudge it to keep going
    # (up to max_nudges consecutive no-progress stalls) instead of stopping early.
    continuation_nudge: bool = True
    # Infinite-loop backstop: max consecutive no-progress nudges before the turn is allowed to end.
    max_nudges: int = 2


class NotesRuntimeConfig(_Base):
    # Surface each workflow's Logseq vault directly in the desktop app's graph home so it appears
    # in the graph switcher with no manual export. When True, a workflow's vault is provisioned as
    # `atom.<slug>` under `logseq_root_dir` (the app's home). When False, the vault stays isolated
    # at $ATOM_HOME/notes/<slug>/ (invisible to the GUI) — the legacy behavior. The Pydantic
    # default is False so programmatic/embedded configs stay isolated; the shipped config.yaml
    # turns it on for the normal desktop deployment.
    expose_to_logseq: bool = False
    # The desktop app's graph home (the --root-dir whose graphs/ the app scans). None ->
    # $LOGSEQ_GRAPHS_DIR's parent, else ~/logseq. Must equal the app's actual home, or the
    # shared-db-worker safety is bypassed. Set this only if the app uses a non-default home.
    logseq_root_dir: Optional[str] = None


class LangfuseConfig(_Base):
    # LangFuse tracing backend. Keys fall back to LANGFUSE_* env vars when unset.
    host: Optional[str] = None            # default https://cloud.langfuse.com (SDK default)
    public_key: Optional[str] = None      # or LANGFUSE_PUBLIC_KEY
    secret_key: Optional[str] = None      # or LANGFUSE_SECRET_KEY
    environment: Optional[str] = None     # optional LangFuse "environment" tag
    release: Optional[str] = None         # optional; falls back to captured git sha
    # Bounded at load time: the LangFuse SDK rejects out-of-range values with a ValueError,
    # so validate here to surface a misconfig as a clean ValidationError, not a runtime crash.
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)


class ObservabilityConfig(_Base):
    # Tracing for workflow runs. `provider` selects the backend; None -> legacy fallback
    # (LangSmith if `enabled`, else off). Exactly one backend is active per run.
    provider: Optional[Literal["langsmith", "langfuse", "none"]] = None
    enabled: bool = False               # (legacy LangSmith) -> LANGSMITH_TRACING when key present
    project: Optional[str] = None       # (LangSmith) -> LANGSMITH_PROJECT
    default_tags: list[str] = Field(default_factory=list)   # tags added to every workflow run
    include_prompt_fingerprint: bool = True  # add prompt ref + content hash to metadata (both backends)
    capture_git_sha: bool = True        # best-effort atom_git_sha in metadata (both backends)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)


class ToolsConfig(_Base):
    # Tools auto-bound/injected into the lead agent (everything else is library-deferred).
    frequent: list[str] = Field(
        default_factory=lambda: [
            "read_file", "write_file", "edit_file", "bash", "ls", "grep", "glob",
            "present_files", "view_image",
        ]
    )
    # Cap any single tool result at this many characters before it enters history; the truncation
    # is marked so the model knows it was cut and can re-run narrower. Generous (~25k tokens).
    max_output_chars: int = 100_000


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
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    uploads: UploadsConfig = Field(default_factory=UploadsConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)
    todos: TodosConfig = Field(default_factory=TodosConfig)
    notes: NotesRuntimeConfig = Field(default_factory=NotesRuntimeConfig)
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
