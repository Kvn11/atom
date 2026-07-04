"""``present_files`` — surface finished deliverables to the user (records them as artifacts)."""

from __future__ import annotations

from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from atom.sandbox.provider import PathEscapeError
from atom.tools.common import get_sandbox


@tool(parse_docstring=True)
def present_files(runtime: ToolRuntime, filepaths: list[str]) -> Command:
    """Present finished files to the user as deliverables.

    Call this when you've produced something the user should receive (a report, a generated
    file). Prefer paths under /mnt/user-data/outputs.

    Args:
        filepaths: The file paths to present.
    """
    sandbox = get_sandbox(runtime)
    artifacts: list[dict] = []
    missing: list[str] = []
    for fp in filepaths:
        try:
            resolved = sandbox.resolve(fp, must_exist=True)
            artifacts.append({"path": fp, "physical": str(resolved)})
        except (FileNotFoundError, PathEscapeError):
            missing.append(fp)
    parts = [f"Presented {len(artifacts)} file(s): {', '.join(a['path'] for a in artifacts)}."] if artifacts else []
    if missing:
        parts.append(f"Could not present (missing or invalid): {', '.join(missing)}.")
    return Command(
        update={
            "artifacts": artifacts,
            "messages": [ToolMessage(" ".join(parts) or "No files presented.", tool_call_id=runtime.tool_call_id)],
        }
    )


PRESENT_TOOLS = [present_files]
