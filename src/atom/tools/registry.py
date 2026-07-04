"""Assemble the effective tool list for a lead agent from its profile + capabilities.

Frequent, model-bound tools only; library tools are added by the deferred/search layer (see
:mod:`atom.tools.search`, wired in :mod:`atom.agent`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from atom.config.schema import AgentProfile, AtomConfig
from atom.tools.bash import bash
from atom.tools.clarification import ask_clarification
from atom.tools.filesystem import edit_file, glob, grep, ls, read_file, write_file
from atom.tools.present_files import present_files
from atom.tools.view_image import view_image

# Tools whose exposure is controlled by the profile's `tools.frequent` list.
FREQUENT_ELIGIBLE: dict[str, BaseTool] = {
    t.name: t
    for t in [ls, read_file, write_file, edit_file, glob, grep, bash, present_files, view_image]
}

# Always available regardless of the frequent list.
ALWAYS_ON: list[BaseTool] = [ask_clarification]


def assemble_frequent_tools(
    cfg: AtomConfig, profile: AgentProfile, caps: dict[str, Any]
) -> list[BaseTool]:
    """Return the frequent (up-front bound) tools for this agent."""
    out: list[BaseTool] = []
    seen: set[str] = set()
    for name in profile.tools.frequent:
        tool = FREQUENT_ELIGIBLE.get(name)
        if tool is None:
            continue  # library tool / unknown — handled by the deferred layer
        if name == "bash" and not cfg.sandbox.bash_enabled:
            continue
        if name == "view_image" and not caps.get("supports_vision", True):
            continue
        if name not in seen:
            out.append(tool)
            seen.add(name)
    for tool in ALWAYS_ON:
        if tool.name not in seen:
            out.append(tool)
            seen.add(tool.name)
    return out
