"""Lead-agent assembly: profile -> model -> rendered prompt -> ordered middleware -> create_agent.

The middleware order here is load-bearing (see the design doc). ClarificationMiddleware is always
last. TodoList (planning) and subagent delegation are always on.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models import BaseChatModel

from atom.config.schema import AgentProfile, AtomConfig
from atom.library import LibraryIndex, load_library, load_named_skills, register_index
from atom.models import build_model, clamp_concurrency, model_caps, resolve_context_window, resolve_spec
from atom.prompts import render_prompt
from atom.sandbox.paths import (
    VIRTUAL_OUTPUTS,
    VIRTUAL_SKILLS,
    VIRTUAL_UPLOADS,
    VIRTUAL_WORKSPACE,
    atom_home,
)
from atom.sandbox.provider import LocalSandboxProvider
from atom.state import ThreadState, WorkspaceContext
from atom.subagent import SubagentRunner
from atom.tools.registry import assemble_frequent_tools
from atom.tools.search import search_skills, search_tools


@dataclass
class PreparedModel:
    model: BaseChatModel
    caps: dict[str, Any]
    context_window: int


def prepare_model(
    profile: AgentProfile,
    override_model: str | None = None,
    override_thinking: str | int | None = None,
) -> PreparedModel:
    key = override_model or profile.model
    thinking = override_thinking if override_thinking is not None else profile.thinking
    spec = resolve_spec(key)
    model = build_model(key, thinking=thinking)
    caps = model_caps(model, spec)
    return PreparedModel(model=model, caps=caps, context_window=resolve_context_window(model, spec))


def render_lead_system_prompt(
    cfg: AtomConfig,
    profile: AgentProfile,
    profile_name: str,
    caps: dict[str, Any],
    *,
    frequent_tool_names: list[str] | None = None,
    frequent_skills: list[Any] | None = None,
    has_tool_library: bool = False,
    has_skill_library: bool = False,
    system_prompt_ref: str | None = None,
) -> str:
    ctx = {
        "agent_name": profile_name,
        "date": datetime.date.today().isoformat(),
        "workspace": VIRTUAL_WORKSPACE,
        "uploads": VIRTUAL_UPLOADS,
        "outputs": VIRTUAL_OUTPUTS,
        "skills": VIRTUAL_SKILLS,
        # The EFFECTIVE bound tool names (post capability/bash filtering), not the raw config list,
        # so the prompt never advertises a tool that isn't actually available to the model.
        "frequent_tool_names": frequent_tool_names if frequent_tool_names is not None else profile.tools.frequent,
        "frequent_skills": [{"name": s.name, "body": s.body} for s in (frequent_skills or [])],
        "bash_enabled": cfg.sandbox.bash_enabled,
        "supports_vision": caps.get("supports_vision", False),
        "has_tool_library": has_tool_library,
        "has_skill_library": has_skill_library,
    }
    return render_prompt(system_prompt_ref or profile.system_prompt, ctx, cfg.config_dir)


def build_lead_agent(
    cfg: AtomConfig,
    profile_name: str | None = None,
    *,
    prepared: PreparedModel | None = None,
    checkpointer: Any = None,
    override_model: str | None = None,
    override_thinking: str | int | None = None,
    override_system_prompt: str | None = None,
    trace: dict | None = None,
):
    """Construct the compiled lead agent for a profile."""
    from atom.tools.registry import FREQUENT_ELIGIBLE

    profile_name = profile_name or cfg.defaults.agent
    profile = cfg.profile(profile_name)
    prepared = prepared or prepare_model(profile, override_model, override_thinking)

    home = str(atom_home(cfg.home))
    provider = LocalSandboxProvider(
        bash_enabled=cfg.sandbox.bash_enabled,
        allowed_workspace_roots=[Path(r) for r in cfg.sandbox.allowed_workspace_roots] or None,
    )

    # --- Library (deviation #4): load + register the index; assemble deferred/visible tools. ---
    library = load_library(home)
    library.auto_promote_k = cfg.library.auto_promote_k  # wire promotion tuning from config
    library.min_score = cfg.library.min_score
    register_index(home, library)
    frequent_skills = load_named_skills(home, profile.skills.frequent)

    frequent_tools = assemble_frequent_tools(cfg, profile, prepared.caps)
    tools = list(frequent_tools)
    tools += library.frequent_tools()
    tools += library.deferred_tools()
    if library.has_tools:
        tools.append(search_tools)
    if library.has_skills:
        tools.append(search_skills)

    # The EFFECTIVE callable set for the prompt: profile-controlled frequent tools that survived
    # capability/bash filtering, plus the always-on tools contributed by middleware.
    effective = [t.name for t in frequent_tools if t.name in FREQUENT_ELIGIBLE]
    extras = ["write_todos", "delegate_task", "ask_clarification"]
    if library.has_tools:
        extras.append("search_tools")
    if library.has_skills:
        extras.append("search_skills")
    tool_names = effective + [e for e in extras if e not in effective]

    summarizer = _build_summarizer(profile, prepared)
    system_prompt = render_lead_system_prompt(
        cfg, profile, profile_name, prepared.caps,
        frequent_tool_names=tool_names,
        frequent_skills=frequent_skills,
        has_tool_library=library.has_tools,
        has_skill_library=library.has_skills,
        system_prompt_ref=override_system_prompt,
    )
    if trace is not None:
        from atom.observability import enrich_lead_trace

        enrich_lead_trace(
            trace, cfg=cfg, profile=profile, profile_name=profile_name,
            system_prompt=system_prompt, context_window=prepared.context_window,
            override_model=override_model, override_thinking=override_thinking,
        )
    middleware = _build_middlewares(cfg, profile, prepared, provider, home, summarizer, library)

    return create_agent(
        model=prepared.model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        state_schema=ThreadState,
        context_schema=WorkspaceContext,
        checkpointer=checkpointer,
    )


def _build_summarizer(profile: AgentProfile, prepared: PreparedModel) -> BaseChatModel:
    """Cheap model for compaction + title; reuse the lead model if none configured."""
    if profile.summarizer_model:
        return build_model(profile.summarizer_model, thinking="off")
    return prepared.model


def _build_middlewares(
    cfg: AtomConfig,
    profile: AgentProfile,
    prepared: PreparedModel,
    provider: LocalSandboxProvider,
    home: str,
    summarizer: BaseChatModel,
    library: LibraryIndex,
) -> list[AgentMiddleware]:
    # Local imports keep the ordered list readable and avoid import cycles.
    from atom.middleware.clarification import ClarificationMiddleware
    from atom.middleware.compaction import build_compaction_middleware
    from atom.middleware.dangling_tool_call import DanglingToolCallMiddleware
    from atom.middleware.deferred_tools import DeferredToolFilterMiddleware
    from atom.middleware.instruction_pin import InstructionPinMiddleware
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware
    from atom.middleware.loop_detection import LoopDetectionMiddleware
    from atom.middleware.sandbox import SandboxMiddleware
    from atom.middleware.seams import (
        GuardrailMiddleware,
        SandboxAuditMiddleware,
        TokenUsageMiddleware,
        UploadsMiddleware,
    )
    from atom.middleware.skill_activation import SkillActivationMiddleware
    from atom.middleware.skill_library import SkillLibraryMiddleware
    from atom.middleware.subagent import SubagentLimitMiddleware, SubagentMiddleware
    from atom.middleware.thread_data import ThreadDataMiddleware
    from atom.middleware.title import TitleMiddleware
    from atom.middleware.tool_error import ToolErrorHandlingMiddleware
    from atom.middleware.view_image import ViewImageMiddleware

    # The [2,4] clamp (deviation #9) is applied once here and used for both the runner's semaphore
    # and the fan-out limit middleware, so config's max_concurrent can never breach the band.
    max_sub = clamp_concurrency(profile.subagents.max_concurrent)
    from atom.prompts.render import resolve_prompt_ref

    # Resolve the summary prompt without Jinja rendering, so the literal "{messages}" placeholder
    # (consumed by SummarizationMiddleware) is preserved.
    summary_prompt = (
        resolve_prompt_ref(profile.summary_prompt, cfg.config_dir) if profile.summary_prompt else None
    )

    runner = SubagentRunner(
        model=prepared.model,
        home=home,
        context_window=prepared.context_window,
        bash_enabled=cfg.sandbox.bash_enabled,
        config_dir=cfg.config_dir,
        summarizer=summarizer,
        compaction_ratio=cfg.compaction.ratio,
        max_concurrent=max_sub,
        timeout_seconds=profile.subagents.timeout_seconds,
        summary_input_tokens=cfg.compaction.summary_input_tokens,
        summary_prompt=summary_prompt,
        recursion_limit=profile.subagents.recursion_limit,
    )
    deferred_names = library.deferred_tool_names()

    chain: list[AgentMiddleware] = [
        # --- before_agent (forward) ---
        ThreadDataMiddleware(home=home),                 # 1. FIRST — provision/bind workspace
        SandboxMiddleware(provider, home=home),          # 2. acquire + register sandbox (docker seam)
        UploadsMiddleware(home=home),                    # register read-only uploads
        InstructionPinMiddleware(),                      # capture first user instruction (pin)
        # --- before_model (forward) ---
        DanglingToolCallMiddleware(),                    # 3. repair orphaned tool calls
        build_compaction_middleware(                     # 4. 50%-of-window summarization
            summarizer,
            context_window=prepared.context_window,
            ratio=cfg.compaction.ratio,
            keep_messages=cfg.compaction.keep_messages,
            summary_prompt=summary_prompt,
            trim_tokens=cfg.compaction.summary_input_tokens,
        ),
        # --- wrap_model_call (outer -> inner by position) ---
        LLMErrorHandlingMiddleware(),                    # 5. outermost: retry/normalize provider errors
        SkillActivationMiddleware(home=home),            # 6. inject /skill-name body (transient)
    ]
    if library.has_skills:
        chain.append(SkillLibraryMiddleware(home=home))  # inject promoted-skill bodies (transient)
    if prepared.caps.get("supports_vision"):
        chain.append(ViewImageMiddleware())              # 7. inject images (vision models only)
    if deferred_names:
        # 8. innermost: hide un-promoted tools + block executing them; hash invalidates stale promos.
        chain.append(DeferredToolFilterMiddleware(deferred_names, catalog_hash=library.catalog_hash))
    chain += [
        TodoListMiddleware(),                            # planning tool — ALWAYS ON
        SubagentMiddleware(runner),                      # delegate_task tool — ALWAYS ON
        # --- wrap_tool_call (outer -> inner) ---
        SandboxAuditMiddleware(),                        # journal every tool call
        GuardrailMiddleware(enabled=cfg.guardrails.enabled),  # dormant policy seam (gates bash)
        ToolErrorHandlingMiddleware(),                   # tool exceptions -> error ToolMessages
        # --- after_model (reverse unwind => Clarification first) ---
        SubagentLimitMiddleware(max_sub),               # cap parallel delegate_task to [2,4]
    ]
    if cfg.track_usage:
        chain.append(TokenUsageMiddleware())             # accumulate token usage
    chain += [
        TitleMiddleware(summarizer),                     # one-shot thread title
        LoopDetectionMiddleware(),                       # break tool-call loops
        ClarificationMiddleware(),                       # LAST — interrupt on ask_clarification
    ]
    return chain
