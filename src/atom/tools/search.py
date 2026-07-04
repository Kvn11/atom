"""Library discovery tools: ``search_tools`` (promote deferred tools) and ``search_skills``."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from atom.library import get_index
from atom.tools.common import thread_id_of


def _home(runtime: Any) -> str | None:
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        return ctx.get("home")
    return getattr(ctx, "home", None)


@tool(parse_docstring=True)
def search_tools(runtime: ToolRuntime, query: str) -> Command:
    """Search the tool library for specialized tools and load the best matches so you can call them.

    Args:
        query: Describe the capability you need (e.g. "convert a pdf to text").
    """
    index = get_index(_home(runtime))
    tcid = runtime.tool_call_id
    if index is None or not index.has_tools:
        return Command(update={"messages": [ToolMessage("The tool library is empty.", tool_call_id=tcid)]})
    # Bounded, score-gated promotion (deviation #4): keeps base context small + prompt-cache-stable.
    matches = index.search_tools(query, k=index.auto_promote_k, min_score=index.min_score)
    if not matches:
        return Command(update={"messages": [ToolMessage("No matching tools found.", tool_call_id=tcid)]})
    names = [m.tool.name for m in matches]
    prev = (runtime.state.get("promoted") or {}).get("names", [])
    merged = sorted(set(prev) | set(names))
    listing = "\n".join(f"- {m.tool.name}: {m.description}" for m in matches)
    content = "Loaded these tools — you can now call them directly:\n" + listing
    return Command(
        update={
            "promoted": {"names": merged, "catalog_hash": index.catalog_hash},
            "messages": [ToolMessage(content, tool_call_id=tcid)],
        }
    )


@tool(parse_docstring=True)
def search_skills(runtime: ToolRuntime, query: str, max_results: int = 3) -> Command:
    """Search the skill library for a step-by-step guide and load the best match(es).

    Args:
        query: Describe the task or workflow you need guidance for.
        max_results: Maximum number of skills to load.
    """
    index = get_index(_home(runtime))
    tcid = runtime.tool_call_id
    if index is None or not index.has_skills:
        return Command(update={"messages": [ToolMessage("The skill library is empty.", tool_call_id=tcid)]})
    matches = index.search_skills(query, k=max_results, min_score=index.min_score)
    if not matches:
        return Command(update={"messages": [ToolMessage("No matching skills found.", tool_call_id=tcid)]})
    prev = runtime.state.get("promoted_skills") or []
    names = [m.name for m in matches]
    # Record the promotion + confirm; the full body is injected transiently each turn by
    # SkillLibraryMiddleware (so it survives compaction instead of being summarized out of history).
    content = "Loaded skill guide(s): " + ", ".join(names) + ". Follow them for this task."
    return Command(
        update={
            "promoted_skills": sorted(set(prev) | set(names)),
            "messages": [ToolMessage(content, tool_call_id=tcid)],
        }
    )
