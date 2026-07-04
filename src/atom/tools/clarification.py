"""``ask_clarification`` — the turn-ending clarification request.

This tool body just formats the question. The actual interrupt (ending the turn so the user can
answer) is performed by :class:`atom.middleware.clarification.ClarificationMiddleware`, which must
run last. ``return_direct=True`` stops the agent loop immediately after this tool.
"""

from __future__ import annotations

from typing import Literal

from langchain.tools import tool

ClarificationType = Literal[
    "missing_info", "ambiguous_requirement", "approach_choice", "risk_confirmation", "suggestion"
]


@tool(parse_docstring=True, return_direct=True)
def ask_clarification(
    question: str,
    clarification_type: ClarificationType,
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user a clarifying question when the request is genuinely ambiguous or blocked.

    Use only when a decision is truly the user's to make or critical information is missing —
    not for things you can reasonably decide or discover yourself. This ends your turn.

    Args:
        question: The question to ask the user.
        clarification_type: Why you're asking (missing_info, ambiguous_requirement,
            approach_choice, risk_confirmation, or suggestion).
        context: Optional context explaining why the answer is needed.
        options: Optional list of choices to offer the user.
    """
    lines = [question]
    if context:
        lines.append(f"\n({context})")
    if options:
        lines.append("Options: " + "; ".join(options))
    return "\n".join(lines)


CLARIFICATION_TOOLS = [ask_clarification]
