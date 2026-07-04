"""Shared graph state (:class:`ThreadState`) and per-run context (:class:`WorkspaceContext`).

``ThreadState`` extends LangChain v1's ``AgentState`` with atom-specific channels that
middlewares and tools communicate through. Middleware-owned channels (e.g. ``todos`` from
``TodoListMiddleware``) are contributed by those middlewares' own state schemas and are NOT
redeclared here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, Optional, TypedDict

from langchain.agents import AgentState

from atom.reducers import (
    merge_artifacts,
    merge_name_list,
    merge_promoted,
    merge_usage,
    merge_viewed_images,
)


class ThreadState(AgentState):
    """Graph state for a lead-agent thread (extends AgentState.messages).

    Note: the live sandbox is NOT a state channel (it is unserializable). It lives in
    :mod:`atom.sandbox.registry`, keyed by ``thread_id``.
    """

    # Resolved per-thread paths + ids (set by ThreadDataMiddleware.before_agent).
    thread_data: NotRequired[dict[str, Any]]
    # Deliverables surfaced via present_files (append + dedupe).
    artifacts: Annotated[list[dict[str, Any]], merge_artifacts]
    # Images fed to vision models via view_image (merge / clear).
    viewed_images: Annotated[dict[str, Any], merge_viewed_images]
    # Deferred-tool promotion record: {"catalog_hash": str, "names": [...]}. Reducer-merged so
    # parallel search_tools calls in one super-step don't collide (LastValue -> InvalidUpdateError).
    promoted: Annotated[dict[str, Any], merge_promoted]
    # Names of skills promoted via search_skills (bodies injected by SkillLibraryMiddleware).
    promoted_skills: Annotated[list[str], merge_name_list]
    # Newly seen uploads (registered by UploadsMiddleware).
    uploaded_files: NotRequired[list[str]]
    # Auto-generated thread title (set once by TitleMiddleware).
    title: NotRequired[str]
    # Accumulated token usage (TokenUsageMiddleware + subagent deltas). Additive reducer.
    usage: Annotated[dict[str, int], merge_usage]


class WorkspaceContext(TypedDict, total=False):
    """Per-run context (create_agent context_schema). Read via ``runtime.context``.

    Everything here is decided at invocation time, NOT baked into the agent profile —
    notably the workspace mode, which the same profile may vary across projects.
    """

    user_id: str
    thread_id: str
    profile_name: str
    home: Optional[str]
    # Workspace provisioning (per-run): "new" mints a fresh dir; "existing" binds an
    # external checkout at ``workspace_path``.
    workspace_mode: Literal["new", "existing"]
    workspace_path: Optional[str]
    # Capabilities/limits derived from the selected model's profile.
    allow_bash: bool
    supports_vision: bool
    context_window: int
