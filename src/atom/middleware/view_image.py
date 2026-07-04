"""ViewImageMiddleware — inject viewed images into the model call (vision models only).

Only added to the chain when the selected model supports vision. Injects the base64 payloads
stashed in ``state.viewed_images`` transiently (via request override), so images are seen by the
model but never persisted/summarized into history.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from atom.reducers import CLEAR


def _image_message(viewed: dict[str, Any]) -> HumanMessage | None:
    blocks: list[dict[str, Any]] = []
    for path, payload in viewed.items():
        b64 = payload.get("base64")
        mime = payload.get("mime_type", "image/png")
        if not b64:
            continue
        blocks.append({"type": "text", "text": f"[image: {path}]"})
        blocks.append(
            {"type": "image", "source_type": "base64", "mime_type": mime, "data": b64}
        )
    return HumanMessage(content=blocks) if blocks else None


class ViewImageMiddleware(AgentMiddleware):
    def _inject(self, request: Any) -> Any:
        viewed = request.state.get("viewed_images") or {}
        img_msg = _image_message(viewed)
        if img_msg is None:
            return request
        return request.override(messages=[*request.messages, img_msg])

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        return await handler(self._inject(request))

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # The image was injected transiently for this call; clear the stored base64 so it isn't
        # persisted in the checkpoint or re-injected (and re-billed) on every subsequent call.
        if state.get("viewed_images"):
            return {"viewed_images": CLEAR}
        return None
