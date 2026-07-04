"""ToolErrorHandlingMiddleware — turn tool exceptions into model-visible error ToolMessages."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


class ToolErrorHandlingMiddleware(AgentMiddleware):
    @staticmethod
    def _to_error(request: Any, exc: Exception) -> ToolMessage:
        call = request.tool_call
        return ToolMessage(
            content=f"Error running {call.get('name')}: {type(exc).__name__}: {exc}",
            tool_call_id=call.get("id"),
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            return handler(request)
        except Exception as exc:  # noqa: BLE001 - convert to error ToolMessage, keep the run alive
            return self._to_error(request, exc)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            return self._to_error(request, exc)
