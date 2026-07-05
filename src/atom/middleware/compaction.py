"""Compaction driven by the selected model's context window (deviation #5).

We reuse LangChain's built-in ``SummarizationMiddleware`` but compute an *explicit token*
trigger = ``ratio * context_window`` ourselves — where ``context_window`` is resolved
profile-first with a static fallback (:mod:`atom.models.profiles`). This sidesteps the built-in
``("fraction", r)`` path, which reads ``model.profile`` and silently disables itself when the
profile is missing (a real risk for Qwen/DashScope). The built-in already summarizes at a safe
message boundary that never splits a tool-call from its ToolMessage.

``PinnedSummarizationMiddleware`` additionally re-injects the user's original instruction
(captured in the ``pinned_instruction`` state channel) verbatim on every compaction, so it can
never be trimmed or paraphrased away.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, RemoveMessage

_PIN_PREFIX = "[Standing instruction — the user's original request, preserved verbatim]\n\n"


class PinnedSummarizationMiddleware(SummarizationMiddleware):
    """SummarizationMiddleware that re-pins ``state['pinned_instruction']`` on every compaction."""

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._inject_pin(super().before_model(state, runtime), state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._inject_pin(await super().abefore_model(state, runtime), state)

    @staticmethod
    def _inject_pin(result: dict[str, Any] | None, state: Any) -> dict[str, Any] | None:
        # None -> no compaction happened; pass through untouched.
        if not result:
            return result
        pinned = (state.get("pinned_instruction") or "").strip()
        if not pinned:
            return result
        msgs = result["messages"]
        # super() returns [RemoveMessage(ALL), <summary HumanMessage>, *preserved]; splice the pin
        # in immediately AFTER the RemoveMessage sentinel and BEFORE the summary.
        insert_at = 1 if (msgs and isinstance(msgs[0], RemoveMessage)) else 0
        pin_msg = HumanMessage(
            content=f"{_PIN_PREFIX}{pinned}",
            additional_kwargs={"lc_source": "pinned_instruction"},
        )
        result["messages"] = [*msgs[:insert_at], pin_msg, *msgs[insert_at:]]
        return result


def build_compaction_middleware(
    summarizer_model: BaseChatModel,
    *,
    context_window: int,
    ratio: float = 0.5,
    keep_messages: int = 20,
    summary_prompt: str | None = None,
    trim_tokens: int | None = None,
) -> SummarizationMiddleware:
    trigger_tokens = max(1, int(ratio * context_window))
    kwargs: dict[str, Any] = {
        "model": summarizer_model,
        "trigger": ("tokens", trigger_tokens),
        "keep": ("messages", keep_messages),
    }
    # A custom, atom-aware summary prompt (preserves mounts/todos/paths). Falls back to the
    # library default when unset. Must contain the ``{messages}`` placeholder.
    if summary_prompt:
        kwargs["summary_prompt"] = summary_prompt
    # How much history the summarizer reads (trim_tokens_to_summarize). Unset -> library default.
    if trim_tokens is not None:
        kwargs["trim_tokens_to_summarize"] = trim_tokens
    return PinnedSummarizationMiddleware(**kwargs)
