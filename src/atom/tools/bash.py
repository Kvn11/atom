"""The ``bash`` tool — runs a shell command in the confined workspace directory.

Enabled by default in v1 (per user decision). There is no container isolation yet, so bash runs
with the user's permissions inside the workspace cwd; the docker sandbox (Phase 2) is where this
becomes properly isolated. The GuardrailMiddleware seam can gate individual commands.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from atom.tools.common import get_sandbox


@tool(parse_docstring=True)
def bash(runtime: ToolRuntime, description: str, command: str) -> str:
    """Run a shell command. The working directory is your workspace.

    Prefer the dedicated file tools for reading/writing files; use bash to run programs, tests,
    builds, and inspection commands. Output is captured and truncated.

    Args:
        description: One-line summary of what this command does.
        command: The shell command to run.
    """
    return get_sandbox(runtime).run_bash(command)


BASH_TOOLS = [bash]
