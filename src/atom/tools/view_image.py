"""``view_image`` — load an image into state so a vision-capable model can see it.

The base64 payload is stashed in ``viewed_images``; ViewImageMiddleware injects it into the next
model call (only when the model supports vision).
"""

from __future__ import annotations

import base64
import io

from langchain_core.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from atom.tools.common import get_sandbox

_MAX_BYTES = 20 * 1024 * 1024
_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


@tool(parse_docstring=True)
def view_image(runtime: ToolRuntime, image_path: str) -> Command:
    """Load an image file so you can see its contents.

    Args:
        image_path: Path to a JPEG/PNG/WEBP/GIF image (max 20MB).
    """
    sandbox = get_sandbox(runtime)
    resolved = sandbox.resolve(image_path, must_exist=True)
    # Reject on stat BEFORE reading, so a huge file can't be loaded into RAM to be rejected.
    if resolved.stat().st_size > _MAX_BYTES:
        raise ValueError(f"Image {image_path} exceeds 20MB.")
    data = resolved.read_bytes()
    from PIL import Image  # local import: Pillow is only needed for images

    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error
        raise ValueError(f"{image_path} is not a readable image: {exc}") from exc
    mime = _MIME.get(fmt or "")
    if mime is None:
        raise ValueError(f"Unsupported image format {fmt} for {image_path}.")
    b64 = base64.b64encode(data).decode("ascii")
    return Command(
        update={
            "viewed_images": {image_path: {"base64": b64, "mime_type": mime}},
            "messages": [
                ToolMessage(
                    f"Loaded image {image_path} ({fmt}, {len(data)} bytes); it is now visible to you.",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )


VIEW_IMAGE_TOOLS = [view_image]
