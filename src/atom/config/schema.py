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


class SubagentConfig(_Base):
    max_concurrent: int = 3  # clamped to [2, 4] at build time
    timeout_seconds: int = 900


class LibraryConfig(_Base):
    search: Literal["bm25"] = "bm25"
    auto_promote_k: int = 3
    min_score: float = 0.0


class GuardrailConfig(_Base):
    enabled: bool = False


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


class AtomConfig(_Base):
    home: Optional[str] = None  # None -> $ATOM_HOME or ~/.atom
    defaults: Defaults = Field(default_factory=Defaults)
    checkpointer: CheckpointerConfig = Field(default_factory=CheckpointerConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)
    track_usage: bool = True
    agents: dict[str, AgentProfile] = Field(default_factory=lambda: {"default": AgentProfile()})

    # Directory of the loaded config file (used to resolve "@file" prompt refs). Not serialized.
    config_dir: Optional[str] = Field(default=None, exclude=True)

    def profile(self, name: str | None = None) -> AgentProfile:
        name = name or self.defaults.agent
        if name not in self.agents:
            raise KeyError(f"Unknown agent profile '{name}'. Defined: {', '.join(self.agents)}.")
        return self.agents[name]
