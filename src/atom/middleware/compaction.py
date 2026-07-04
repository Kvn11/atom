"""Compaction driven by the selected model's context window (deviation #5).

We reuse LangChain's built-in ``SummarizationMiddleware`` but compute an *explicit token*
trigger = ``ratio * context_window`` ourselves — where ``context_window`` is resolved
profile-first with a static fallback (:mod:`atom.models.profiles`). This sidesteps the built-in
``("fraction", r)`` path, which reads ``model.profile`` and silently disables itself when the
profile is missing (a real risk for Qwen/DashScope). The built-in already summarizes at a safe
message boundary that never splits a tool-call from its ToolMessage.
"""

from __future__ import annotations

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel


def build_compaction_middleware(
    summarizer_model: BaseChatModel,
    *,
    context_window: int,
    ratio: float = 0.5,
    keep_messages: int = 20,
    summary_prompt: str | None = None,
) -> SummarizationMiddleware:
    trigger_tokens = max(1, int(ratio * context_window))
    kwargs = {
        "model": summarizer_model,
        "trigger": ("tokens", trigger_tokens),
        "keep": ("messages", keep_messages),
    }
    # A custom, atom-aware summary prompt (preserves mounts/todos/paths). Falls back to the
    # library default when unset. Must contain the ``{messages}`` placeholder.
    if summary_prompt:
        kwargs["summary_prompt"] = summary_prompt
    return SummarizationMiddleware(**kwargs)
