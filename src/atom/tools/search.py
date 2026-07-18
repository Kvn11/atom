"""Library discovery tools: ``search_tools`` (promote deferred tools) and ``search_skills``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from atom.library import get_index
from atom.sandbox.paths import VIRTUAL_SKILLS, VIRTUAL_SKILL_LIBRARY
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
    """Search the skill library to discover skills relevant to a task.

    Returns each match's name and description. To use one, load its full instructions with
    load_skill("<name>").

    Args:
        query: Describe the task or workflow you need guidance for.
        max_results: Maximum number of skills to list.
    """
    index = get_index(_home(runtime))
    tcid = runtime.tool_call_id
    if index is None or not index.has_skills:
        return Command(update={"messages": [ToolMessage("The skill library is empty.", tool_call_id=tcid)]})
    matches = index.search_skills(query, k=max_results, min_score=index.min_score)
    if not matches:
        return Command(update={"messages": [ToolMessage("No matching skills found.", tool_call_id=tcid)]})
    listing = "\n".join(f"- {m.name}: {m.description}" for m in matches)
    content = (
        'Found these skills. Load one with load_skill("<name>") to get its full instructions:\n'
        + listing
    )
    return Command(update={"messages": [ToolMessage(content, tool_call_id=tcid)]})


@tool(parse_docstring=True)
def load_skill(runtime: ToolRuntime, name: str) -> Command:
    """Load a skill's full instructions into context by its exact name.

    Use a name shown in the skills catalog or returned by search_skills.

    Args:
        name: The exact skill name to load (e.g. "logseq-cli").
    """
    tcid = runtime.tool_call_id
    clean = (name or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        return Command(update={"messages": [ToolMessage(f"Invalid skill name '{name}'.", tool_call_id=tcid)]})
    home = _home(runtime)
    mount: str | None = None
    if home:
        if (Path(home) / "skills" / clean / "SKILL.md").exists():
            mount = VIRTUAL_SKILLS
        elif (Path(home) / "skill_library" / clean / "SKILL.md").exists():
            mount = VIRTUAL_SKILL_LIBRARY
    if mount is None:
        return Command(update={"messages": [ToolMessage(
            f"No skill named '{clean}' found. Check the skills catalog or use search_skills.",
            tool_call_id=tcid)]})
    # promoted_skills is a union-reducer channel (merge_name_list); returning just this name suffices.
    return Command(update={
        "promoted_skills": [clean],
        "messages": [ToolMessage(
            f"Loaded skill '{clean}'. Follow its instructions for this task. "
            f"Its bundled files are at {mount}/{clean}/.", tool_call_id=tcid)],
    })
