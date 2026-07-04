"""Helpers for reading text out of messages (which may carry list content, e.g. thinking blocks)."""

from __future__ import annotations

from typing import Any


def message_text(message: Any) -> str:
    """Return the human-readable text of a message.

    Reasoning models return ``content`` as a list of blocks (thinking + text); this extracts just
    the text blocks and joins them.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
        return "\n".join(parts).strip()
    return str(content)
