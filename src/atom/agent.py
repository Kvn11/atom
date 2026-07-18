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
from atom.library import LibraryIndex, load_library, load_skill_catalog, register_index
from atom.models import build_model, clamp_concurrency, model_caps, resolve_context_window, resolve_spec
from atom.prompts import render_prompt
from atom.sandbox.paths import (
    VIRTUAL_OUTPUTS,
    VIRTUAL_SKILL_LIBRARY,
    VIRTUAL_SKILLS,
    VIRTUAL_UPLOADS,
    VIRTUAL_WORKSPACE,
    atom_home,
)
from atom.sandbox.provider import LocalSandboxProvider
from atom.state import ThreadState, WorkspaceContext
from atom.subagent import SubagentRunner
from atom.tools.registry import assemble_frequent_tools
from atom.tools.search import load_skill, search_skills, search_tools


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
    skill_catalog: list[dict] | None = None,
    has_tool_library: bool = False,
    has_skill_library: bool = False,
    notes: dict | None = None,
    system_prompt_ref: str | None = None,
) -> str:
    ctx = {
        "agent_name": profile_name,
        "date": datetime.date.today().isoformat(),
        "workspace": VIRTUAL_WORKSPACE,
        "uploads": VIRTUAL_UPLOADS,
        "outputs": VIRTUAL_OUTPUTS,
        "skills": VIRTUAL_SKILLS,
        "skill_library": VIRTUAL_SKILL_LIBRARY,
        # The EFFECTIVE bound tool names (post capability/bash filtering), not the raw config list,
        # so the prompt never advertises a tool that isn't actually available to the model.
        "frequent_tool_names": frequent_tool_names if frequent_tool_names is not None else profile.tools.frequent,
        # Always-on catalog: name + description only (bodies are loaded on demand via load_skill).
        "skill_catalog": list(skill_catalog or []),
        "notes": notes,
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
    notes: dict | None = None,
    obs_provider=None,
):
    """Construct the compiled lead agent for a profile."""
    from atom.tools.registry import FREQUENT_ELIGIBLE

    profile_name = profile_name or cfg.defaults.agent
    profile = cfg.profile(profile_name)

    from atom.middleware.llm_error import RetryPolicy
    retry_policy = RetryPolicy(
        max_retries=cfg.retry.max_retries, base_delay=cfg.retry.base_delay,
        max_delay=cfg.retry.max_delay, jitter=cfg.retry.jitter,
    )

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
    catalog_entries = load_skill_catalog(home, profile.skills.frequent)
    skill_catalog = [{"name": s.name, "description": s.description} for s in catalog_entries]
    has_any_skills = bool(catalog_entries) or library.has_skills

    frequent_tools = assemble_frequent_tools(cfg, profile, prepared.caps)
    tools = list(frequent_tools)
    tools += library.frequent_tools()
    tools += library.deferred_tools()
    if library.has_tools:
        tools.append(search_tools)
    if library.has_skills:
        tools.append(search_skills)
    if has_any_skills:
        tools.append(load_skill)

    # The EFFECTIVE callable set for the prompt: profile-controlled frequent tools that survived
    # capability/bash filtering, plus the always-on tools contributed by middleware.
    effective = [t.name for t in frequent_tools if t.name in FREQUENT_ELIGIBLE]
    extras = ["write_todos", "delegate_task", "ask_clarification"]
    if library.has_tools:
        extras.append("search_tools")
    if library.has_skills:
        extras.append("search_skills")
    if has_any_skills:
        extras.append("load_skill")
    tool_names = effective + [e for e in extras if e not in effective]

    summarizer = _build_summarizer(profile, prepared, retry_policy)
    system_prompt = render_lead_system_prompt(
        cfg, profile, profile_name, prepared.caps,
        frequent_tool_names=tool_names,
        skill_catalog=skill_catalog,
        has_tool_library=library.has_tools,
        has_skill_library=library.has_skills,
        notes=notes,
        system_prompt_ref=override_system_prompt,
    )
    from atom.observability import enrich_lead_trace, tracing_active

    obs_active = obs_provider is not None and obs_provider.is_active()
    mw_trace = None
    if trace is not None and (obs_active or tracing_active()):
        enrich_lead_trace(
            trace, cfg=cfg, profile=profile, profile_name=profile_name,
            system_prompt=system_prompt, context_window=prepared.context_window,
            override_model=override_model, override_thinking=override_thinking,
            override_system_prompt=override_system_prompt,
        )
        mw_trace = trace
    middleware = _build_middlewares(
        cfg, profile, prepared, provider, home, summarizer, library, mw_trace,
        skill_catalog=skill_catalog, retry_policy=retry_policy, notes=notes,
        obs_provider=obs_provider,
    )

    return create_agent(
        model=prepared.model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        state_schema=ThreadState,
        context_schema=WorkspaceContext,
        checkpointer=checkpointer,
    )


def _build_summarizer(profile: AgentProfile, prepared: PreparedModel, policy) -> BaseChatModel:
    """Cheap model for compaction + title, wrapped so its out-of-band calls get retry/backoff."""
    from atom.middleware.llm_error import RetryingModel

    base = build_model(profile.summarizer_model, thinking="off") if profile.summarizer_model else prepared.model
    return RetryingModel(base, policy)


def _build_middlewares(
    cfg: AtomConfig,
    profile: AgentProfile,
    prepared: PreparedModel,
    provider: LocalSandboxProvider,
    home: str,
    summarizer: BaseChatModel,
    library: LibraryIndex,
    trace: dict | None = None,
    *,
    skill_catalog: list[dict] | None = None,
    retry_policy=None,
    notes: dict | None = None,
    obs_provider=None,
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
    from atom.middleware.todo_continuation import TodoContinuationMiddleware
    from atom.middleware.tool_error import ToolErrorHandlingMiddleware
    from atom.middleware.view_image import ViewImageMiddleware

    # The [2,4] clamp (deviation #9) is applied once here and used for both the runner's semaphore
    # and the fan-out limit middleware, so config's max_concurrent can never breach the band.
    max_sub = clamp_concurrency(profile.subagents.max_concurrent)
    from atom.middleware.llm_error import RetryPolicy
    policy = retry_policy or RetryPolicy()
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
        base_trace=trace,
        observability=cfg.observability,
        obs_provider=obs_provider,
        retry=policy,
        skill_catalog=skill_catalog or [],
        has_skill_library=library.has_skills,
        notes=notes,   # bash children rendered vault-aware when the workflow enables notes
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
        LLMErrorHandlingMiddleware(policy),               # 5. outermost: retry, then raise on exhaustion
        SkillActivationMiddleware(home=home),            # 6. inject /skill-name body (transient)
    ]
    if library.has_skills or skill_catalog:
        chain.append(SkillLibraryMiddleware(home=home))  # inject loaded-skill bodies (transient)
    if prepared.caps.get("supports_vision"):
        chain.append(ViewImageMiddleware())              # 7. inject images (vision models only)
    if deferred_names:
        # 8. innermost: hide un-promoted tools + block executing them; hash invalidates stale promos.
        chain.append(DeferredToolFilterMiddleware(deferred_names, catalog_hash=library.catalog_hash))
    chain.append(TodoListMiddleware())                   # planning tool — ALWAYS ON
    if cfg.todos.continuation_nudge:                     # nudge the agent to finish incomplete todos
        chain.append(TodoContinuationMiddleware(max_nudges=cfg.todos.max_nudges))
    chain += [
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
