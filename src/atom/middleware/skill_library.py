"""SkillLibraryMiddleware — inject bodies of skills promoted via ``search_skills``.

``search_skills`` records skill names in ``state.promoted_skills`` (and returns only a short
confirmation). This middleware re-injects those skills' ``SKILL.md`` bodies transiently into each
model call, so the guidance survives compaction instead of being persisted once and summarized
away. Mirrors the transient injection of :class:`SkillActivationMiddleware`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from atom.library import parse_skill_md


class SkillLibraryMiddleware(AgentMiddleware):
    def __init__(self, home: str):
        super().__init__()
        self.home = Path(home)

    def _bodies(self, names: list[str]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for name in names:
            for base in (self.home / "skill_library", self.home / "skills"):
                md = base / name / "SKILL.md"
                if md.exists():
                    out.append((name, parse_skill_md(md.read_text(encoding="utf-8"), name).body))
                    break
        return out

    def _inject(self, request: Any) -> Any:
        names = request.state.get("promoted_skills") or []
        bodies = self._bodies(names)
        if not bodies:
            return request
        text = "\n\n---\n\n".join(f"# Skill: {n}\n\n{b}" for n, b in bodies)
        note = HumanMessage(content=f"[Active skill guide(s) — follow these]\n\n{text}")
        return request.override(messages=[*request.messages, note])

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return await handler(self._inject(request))
