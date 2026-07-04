"""Dormant / lightweight cross-cutting middlewares (uploads, audit, guardrail, token usage).

These are thin seams that grow in later phases (guardrails become policy-driven; audit feeds a UI;
docker changes the sandbox). They are cheap and off/no-op by default where appropriate.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from atom.middleware.context import ctx_dict
from atom.sandbox.paths import thread_paths_from_context

_audit_log = logging.getLogger("atom.audit")

# Obvious-footgun denylist; only enforced when guardrails are enabled.
DEFAULT_BASH_DENY = ["rm -rf /", ":(){", "mkfs", "shutdown", "reboot", "dd if=", "> /dev/sd"]


class UploadsMiddleware(AgentMiddleware):
    """Record files present in the read-only uploads dir so the agent knows they exist."""

    def __init__(self, home: str):
        super().__init__()
        self.home = home

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        tp = thread_paths_from_context(ctx_dict(runtime), self.home)
        if not tp.uploads.exists():
            return None
        files = [str(p.relative_to(tp.uploads)) for p in sorted(tp.uploads.rglob("*")) if p.is_file()]
        return {"uploaded_files": files} if files else None


class SandboxAuditMiddleware(AgentMiddleware):
    """Log every tool invocation (security-critical once bash/docker land)."""

    @staticmethod
    def _record(request: Any) -> None:
        call = request.tool_call
        args = call.get("args", {})
        summary = {k: (str(v)[:80]) for k, v in args.items() if k != "content"}
        _audit_log.info("tool=%s args=%s", call.get("name"), summary)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._record(request)
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        self._record(request)
        return await handler(request)


class GuardrailMiddleware(AgentMiddleware):
    """Pluggable per-tool-call authorization. Disabled by default (the safety seam for bash)."""

    def __init__(self, enabled: bool = False, bash_deny: list[str] | None = None):
        super().__init__()
        self.enabled = enabled
        self.bash_deny = bash_deny if bash_deny is not None else DEFAULT_BASH_DENY

    def _deny(self, request: Any) -> ToolMessage | None:
        if not self.enabled:
            return None
        call = request.tool_call
        if call.get("name") == "bash":
            command = call.get("args", {}).get("command", "")
            for bad in self.bash_deny:
                if bad in command:
                    return ToolMessage(
                        content=f"Blocked by guardrail: command contains a disallowed pattern ('{bad}').",
                        tool_call_id=call.get("id"),
                        status="error",
                    )
        return None

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        denied = self._deny(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        denied = self._deny(request)
        return denied if denied is not None else await handler(request)


class TokenUsageMiddleware(AgentMiddleware):
    """Report this model step's token usage as a delta; ``state.usage``'s additive reducer
    accumulates it (alongside subagent usage attributed by ``delegate_task``)."""

    _FIELDS = ("input_tokens", "output_tokens", "total_tokens")

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        usage = getattr(messages[-1], "usage_metadata", None)
        if not usage:
            return None
        delta = {f: int(usage.get(f, 0)) for f in self._FIELDS if usage.get(f)}
        return {"usage": delta} if delta else None
