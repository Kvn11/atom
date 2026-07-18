"""Unit tests for TodoContinuationMiddleware (per-turn reset + bounded continuation nudge)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.middleware.todo_continuation import TodoContinuationMiddleware, _NUDGE_SOURCE
from atom.runtime import run_agent


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


def _build_chain(base_config, atom_home):
    from atom.agent import _build_middlewares
    from atom.library import load_library
    from atom.sandbox.provider import LocalSandboxProvider
    from tests.conftest import make_prepared

    prepared = make_prepared([])
    profile = base_config.profile("default")
    provider = LocalSandboxProvider()
    library = load_library(str(atom_home))
    return _build_middlewares(
        base_config, profile, prepared, provider, str(atom_home), prepared.model, library
    )


def test_nudge_middleware_wired_before_loop_and_clarification(base_config, atom_home):
    from atom.middleware.clarification import ClarificationMiddleware
    from atom.middleware.loop_detection import LoopDetectionMiddleware
    from atom.middleware.todo_continuation import TodoContinuationMiddleware

    chain = _build_chain(base_config, atom_home)
    types = [type(m).__name__ for m in chain]
    assert "TodoContinuationMiddleware" in types
    nudge_i = next(i for i, m in enumerate(chain) if isinstance(m, TodoContinuationMiddleware))
    loop_i = next(i for i, m in enumerate(chain) if isinstance(m, LoopDetectionMiddleware))
    clar_i = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
    # Registered EARLIER than loop/clarification -> runs LATER on the reverse after_model unwind,
    # so their jump_to="end" short-circuits before the nudge can fire.
    assert nudge_i < loop_i < clar_i


def test_nudge_middleware_absent_when_disabled(base_config, atom_home):
    from atom.middleware.todo_continuation import TodoContinuationMiddleware

    base_config.todos.continuation_nudge = False
    chain = _build_chain(base_config, atom_home)
    assert not any(isinstance(m, TodoContinuationMiddleware) for m in chain)


def _wt_call(content, status, cid):
    # A write_todos tool call setting a single todo to `status`.
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "write_todos",
            "args": {"todos": [{"content": content, "status": status}]},
            "id": cid, "type": "tool_call",
        }],
    )


# The shared ScriptedChatModel returns the SAME AIMessage object on every clamp, and
# add_messages dedupes by id -- so a stalling model's repeated identical answer would merge in
# place instead of appending, starving the nudge loop (a fake-model artifact, not real-LLM
# behavior). This local model stamps a fresh id per call so repeated stalls append like a real
# LLM would.
def _prepared_unique(responses):
    import itertools

    from langchain_core.outputs import ChatGeneration, ChatResult

    from atom.agent import PreparedModel
    from tests.conftest import DEFAULT_PROFILE_DATA, ScriptedChatModel

    counter = itertools.count()

    class _UniqueScriptedModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            idx = min(self._i, len(self.responses) - 1)
            self._i += 1
            src = self.responses[idx]
            msg = AIMessage(
                content=src.content,
                tool_calls=list(src.tool_calls or []),
                id=f"ai-{next(counter)}",
            )
            return ChatResult(generations=[ChatGeneration(message=msg)])

    model = _UniqueScriptedModel(responses=responses, profile=DEFAULT_PROFILE_DATA)
    caps = {
        "context_window": model.profile["max_input_tokens"],
        "max_output_tokens": model.profile["max_output_tokens"],
        "supports_vision": model.profile["image_inputs"],
        "supports_reasoning": model.profile["reasoning_output"],
        "has_profile": True,
    }
    return PreparedModel(model=model, caps=caps, context_window=caps["context_window"])


@pytest.mark.asyncio
async def test_run_continues_then_bounds_when_todos_incomplete(base_config):
    # Plan one in_progress todo, then keep stalling with a plain answer. The nudge should re-drive
    # the model each stall, then stop after max_nudges (default 2) -- proving it both continues and
    # is bounded (no infinite loop / recursion crash).
    prepared = _prepared_unique([
        _wt_call("build the thing", "in_progress", "wt1"),
        AIMessage(content="still working on it"),
    ])
    result = await run_agent("do the thing", config=base_config, prepared=prepared)

    # Terminated cleanly with the stall answer as the final text (not a nudge message).
    assert result.final_text == "still working on it"
    # Exactly max_nudges continuation nudges were injected -> continued AND bounded.
    nudges = [
        m for m in result.messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") == _NUDGE_SOURCE
    ]
    assert len(nudges) == base_config.todos.max_nudges == 2
    # The todo is still incomplete (the scripted model never marked it done) -> the run stopped
    # because the nudge budget was exhausted, not because the plan was finished.
    assert any(t["status"] != "completed" for t in result.state.get("todos", []))


@pytest.mark.asyncio
async def test_per_turn_reset_clears_stale_todos_across_turns(base_config):
    from tests.conftest import make_prepared

    # Turn 1: plan + immediately complete one todo, then answer (no nudge — all complete).
    turn1 = make_prepared([
        _wt_call("do it", "completed", "wt1"),
        AIMessage(content="Finished."),
    ])
    r1 = await run_agent("first task", config=base_config, prepared=turn1)
    assert [t["status"] for t in r1.state.get("todos", [])] == ["completed"]

    # Turn 2 on the SAME thread: a plain answer. before_agent must clear turn 1's todos.
    turn2 = make_prepared([AIMessage(content="Hi there.")])
    r2 = await run_agent(
        "unrelated follow-up", config=base_config, prepared=turn2, thread_id=r1.thread_id
    )
    assert r2.final_text == "Hi there."
    assert r2.state.get("todos", []) == []  # stale plan was reset, so no nudge could mis-fire
