"""SkillActivationMiddleware — activate a skill via ``/skill-name`` in the latest user message.

When the newest human message starts with ``/<skill-name>``, the skill body (from ``skills/`` or
``skill_library/``) is injected transiently into the current model call only — not persisted, so
it doesn't bloat history.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from atom.library import parse_skill_md
from atom.sandbox.paths import VIRTUAL_SKILLS, VIRTUAL_SKILL_LIBRARY

_SLASH = re.compile(r"^\s*/([A-Za-z0-9_\-]+)\b")


class SkillActivationMiddleware(AgentMiddleware):
    def __init__(self, home: str):
        super().__init__()
        self.home = Path(home)

    def _skill_body(self, name: str) -> tuple[str, str] | None:
        for base, mount in (
            (self.home / "skills", VIRTUAL_SKILLS),
            (self.home / "skill_library", VIRTUAL_SKILL_LIBRARY),
        ):
            md = base / name / "SKILL.md"
            if md.exists():
                return mount, parse_skill_md(md.read_text(encoding="utf-8"), name).body
        return None

    def _inject(self, request: Any) -> Any:
        messages = request.messages or []
        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if last_human is None or not isinstance(last_human.content, str):
            return request
        m = _SLASH.match(last_human.content)
        if not m:
            return request
        found = self._skill_body(m.group(1))
        if not found:
            return request
        mount, body = found
        note = HumanMessage(content=(
            f"[Activated skill '{m.group(1)}' — follow this guide. "
            f"Bundled files: {mount}/{m.group(1)}/]\n\n{body}"))
        return request.override(messages=[*messages, note])

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return await handler(self._inject(request))
