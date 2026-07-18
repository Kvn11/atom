"""TodoContinuationMiddleware — keep the lead agent on-track to finish its todos.

Two mechanisms, lead-agent only:

* ``before_agent`` resets the ``todos`` channel to empty at the start of every turn, so a
  multi-turn thread never inherits a stale plan and the nudge below reasons only about the
  current turn's todos.
* ``after_model`` nudges the model back to work when it tries to end a turn (a no-tool-call
  ``AIMessage``) while todos are still incomplete. Bounded by a progress-aware counter, because
  LoopDetectionMiddleware only catches repeated *tool-call* signatures — a no-tool-call nudge
  loop would evade it, leaving only ``recursion_limit`` (a hard error) as a backstop.

Placement (load-bearing): registered right after ``TodoListMiddleware`` — EARLY in the chain —
so on the reverse ``after_model`` unwind it runs AFTER ClarificationMiddleware and
LoopDetectionMiddleware. When either of those jumps to ``end`` the graph short-circuits to the
exit node before this hook runs, so the nudge never fires on a clarification turn or a detected
loop. Writing ``{"todos": []}`` is legal because the channel is contributed by the always-on
TodoListMiddleware; the two are coupled by construction (both lead-only).
"""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

_NUDGE_PREFIX = "[Automated planning check] "
_NUDGE_SOURCE = "todo_continuation"


class TodoNudgeState(AgentState):
    """State channel owned by TodoContinuationMiddleware (reset each turn by before_agent)."""

    # {"count": <consecutive no-progress nudges>, "completed": <completed-todo count at last nudge>}
    todo_nudge: NotRequired[dict]


def _incomplete(todos: list[dict]) -> list[dict]:
    return [t for t in todos if t.get("status") != "completed"]


def _nudge_text(incomplete: list[dict]) -> str:
    lines = "\n".join(
        f"- ({t.get('status', 'pending')}) {t.get('content', '')}" for t in incomplete
    )
    return (
        f"{_NUDGE_PREFIX}You ended your turn, but these todo items are still open:\n"
        f"{lines}\n"
        "If the task isn't finished, keep going — start the next step now. If it IS finished, "
        "call write_todos to mark these items completed (or remove ones no longer needed), then "
        "write your final answer. Don't stop with open todos unless you're blocked — if you are, "
        "say what's blocking you."
    )


class TodoContinuationMiddleware(AgentMiddleware):
    state_schema = TodoNudgeState

    def __init__(self, *, max_nudges: int = 2) -> None:
        super().__init__()
        self.max_nudges = max_nudges

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
        # New turn: drop any prior turn's plan + nudge bookkeeping so the nudge scopes to this turn.
        return {"todos": [], "todo_nudge": {"count": 0, "completed": 0}}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None  # not a turn-ending (no-tool-call) assistant message
        todos = state.get("todos") or []
        if not todos:
            return None  # no plan -> trivial task, terminate normally
        incomplete = _incomplete(todos)
        if not incomplete:
            return None  # plan fully complete -> clean finish
        cur = state.get("todo_nudge") or {"count": 0, "completed": 0}
        completed_now = len(todos) - len(incomplete)
        made_progress = completed_now > cur.get("completed", 0)
        new_count = 1 if made_progress else cur.get("count", 0) + 1
        if new_count > self.max_nudges:
            return None  # budget exhausted for this no-progress streak -> let the turn end
        return {
            "messages": [
                HumanMessage(
                    content=_nudge_text(incomplete),
                    additional_kwargs={"lc_source": _NUDGE_SOURCE},
                )
            ],
            "jump_to": "model",
            "todo_nudge": {"count": new_count, "completed": completed_now},
        }
