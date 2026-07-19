"""Shared size-limit helper: text truncation with an informative, counts-bearing elision marker.

Reused by the tool-output cap (Layer 1), the context-overflow trimmer (Layer 2), and the LangFuse
truncating mask (Layer 3a). One helper, three callers, three markers.
"""
from __future__ import annotations


def truncate_text(text: str, *, max_chars: int, marker_template: str) -> str:
    """Truncate ``text`` to ~``max_chars``, keeping a head+tail slice with ``marker_template``
    (formatted with ``total``/``elided``/``head``/``tail`` ints) spliced into the elided middle.

    Returns ``text`` unchanged when it already fits. The result may exceed ``max_chars`` by the
    marker length; ``max_chars`` is an approximate budget set well under any hard limit.
    """
    total = len(text)
    if total <= max_chars:
        return text
    half = max(0, max_chars // 2)
    head = text[:half]
    tail = text[-half:] if half else ""
    elided = total - len(head) - len(tail)
    marker = marker_template.format(total=total, elided=elided, head=len(head), tail=len(tail))
    return f"{head}{marker}{tail}"
