"""DeferredToolFilterMiddleware — hide un-promoted library tool schemas from the model.

Innermost model wrap: all library tools are bound to the agent, but their schemas are hidden
until ``search_tools`` promotes them (recorded in ``state.promoted``). This keeps base context
small. It is also the **future-MCP seam**: deferred MCP tools would register the same way.

Two guards: (1) promotions are honored only when their ``catalog_hash`` matches the current
catalog, so a stale promotion (from a changed/reloaded catalog) can't un-hide the wrong tool;
(2) an execution guard rejects a call to a deferred tool that isn't currently promoted, so a
hallucinated or stale ``tool_call`` can't execute unfiltered.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return tool.get("name")
    return getattr(tool, "name", None)


class DeferredToolFilterMiddleware(AgentMiddleware):
    def __init__(self, deferred_names: set[str], *, catalog_hash: str | None = None):
        super().__init__()
        self.deferred_names = set(deferred_names)
        self.catalog_hash = catalog_hash

    def _promoted_names(self, state: Any) -> set[str]:
        promoted = state.get("promoted") or {}
        # A configured catalog hash must match; None (e.g. unit tests) skips the check.
        if self.catalog_hash is not None and promoted.get("catalog_hash") != self.catalog_hash:
            return set()
        return set(promoted.get("names", []))

    def _filter(self, request: Any) -> Any:
        if not self.deferred_names:
            return request
        promoted = self._promoted_names(request.state)
        visible = [
            t
            for t in (request.tools or [])
            if _tool_name(t) not in self.deferred_names or _tool_name(t) in promoted
        ]
        return request.override(tools=visible)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._filter(request))

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return await handler(self._filter(request))

    # --- execution guard: a deferred tool may only run while promoted (hash-matched) ---
    def _blocked(self, request: Any) -> ToolMessage | None:
        if not self.deferred_names:
            return None
        call = request.tool_call
        name = call.get("name")
        if name in self.deferred_names and name not in self._promoted_names(request.state):
            return ToolMessage(
                content=f"Tool '{name}' is not loaded. Call search_tools to load it first.",
                tool_call_id=call.get("id"),
                status="error",
            )
        return None

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._blocked(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        blocked = self._blocked(request)
        return blocked if blocked is not None else await handler(request)
