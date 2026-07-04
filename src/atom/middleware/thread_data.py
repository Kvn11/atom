"""ThreadDataMiddleware — provisions/binds the per-thread workspace. Runs FIRST.

Resolves the workspace for this run (``new`` mints a fresh dir; ``existing`` binds an external
one — decided per-run via WorkspaceContext), creates the directories, and records the resolved
paths in ``state.thread_data`` for prompts, tools, and debugging.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware

from atom.middleware.context import ctx_dict
from atom.sandbox.paths import thread_paths_from_context


class ThreadDataMiddleware(AgentMiddleware):
    def __init__(self, *, home: str | None = None):
        super().__init__()
        self.home = home

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = ctx_dict(runtime)
        tp = thread_paths_from_context(ctx, self.home).ensure()
        return {
            "thread_data": {
                "user_id": tp.user_id,
                "thread_id": tp.thread_id,
                "home": str(tp.home),
                "workspace_is_external": tp.workspace_is_external,
                "physical": {
                    "workspace": str(tp.workspace),
                    "uploads": str(tp.uploads),
                    "outputs": str(tp.outputs),
                    "skills": str(tp.skills),
                    "skill_library": str(tp.skill_library),
                    "tool_library": str(tp.tool_library),
                },
            }
        }
