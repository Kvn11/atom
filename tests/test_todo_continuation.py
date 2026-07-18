"""Unit tests for TodoContinuationMiddleware (per-turn reset + bounded continuation nudge)."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from atom.middleware.todo_continuation import TodoContinuationMiddleware, _NUDGE_SOURCE


def _todo(content, status):
    return {"content": content, "status": status}


def _mw(max_nudges=2):
    return TodoContinuationMiddleware(max_nudges=max_nudges)


def _nudge_msgs(out):
    return [m for m in out["messages"] if getattr(m, "additional_kwargs", {}).get("lc_source") == _NUDGE_SOURCE]


def test_before_agent_resets_todos_and_counter():
    out = _mw().before_agent({"todos": [_todo("old", "in_progress")]}, runtime=None)
    assert out == {"todos": [], "todo_nudge": {"count": 0, "completed": 0}}


def test_nudge_fires_when_incomplete():
    state = {
        "messages": [AIMessage(content="All set!")],
        "todos": [_todo("build", "in_progress")],
    }
    out = _mw().after_model(state, runtime=None)
    assert out is not None
    assert out["jump_to"] == "model"
    assert out["todo_nudge"] == {"count": 1, "completed": 0}
    nudges = _nudge_msgs(out)
    assert len(nudges) == 1
    assert "build" in nudges[0].content  # lists the open item


def test_no_nudge_when_all_completed():
    state = {
        "messages": [AIMessage(content="done")],
        "todos": [_todo("build", "completed"), _todo("ship", "completed")],
    }
    assert _mw().after_model(state, runtime=None) is None


def test_no_nudge_when_no_todos():
    state = {"messages": [AIMessage(content="42")], "todos": []}
    assert _mw().after_model(state, runtime=None) is None
    state2 = {"messages": [AIMessage(content="42")]}  # todos absent entirely
    assert _mw().after_model(state2, runtime=None) is None


def test_inert_when_last_message_has_tool_calls():
    ai = AIMessage(content="", tool_calls=[{"name": "write_todos", "args": {}, "id": "t1", "type": "tool_call"}])
    state = {"messages": [ai], "todos": [_todo("build", "in_progress")]}
    assert _mw().after_model(state, runtime=None) is None


def test_cap_stops_the_loop():
    # Already nudged max_nudges times with no progress since -> allow the turn to end.
    state = {
        "messages": [AIMessage(content="still working on it")],
        "todos": [_todo("build", "in_progress")],
        "todo_nudge": {"count": 2, "completed": 0},
    }
    assert _mw(max_nudges=2).after_model(state, runtime=None) is None


def test_progress_resets_the_budget():
    # count is at the cap, but one more todo is completed than at the last nudge -> nudge again at count 1.
    state = {
        "messages": [AIMessage(content="made progress")],
        "todos": [_todo("build", "completed"), _todo("ship", "in_progress")],
        "todo_nudge": {"count": 2, "completed": 0},
    }
    out = _mw(max_nudges=2).after_model(state, runtime=None)
    assert out is not None
    assert out["todo_nudge"] == {"count": 1, "completed": 1}
