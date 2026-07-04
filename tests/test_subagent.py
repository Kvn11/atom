"""Subagent delegation: delegate_task runs a child that shares the workspace + limit truncation."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from atom.middleware.subagent import SubagentLimitMiddleware
from atom.runtime import run_agent
from tests.conftest import make_prepared


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


@pytest.mark.asyncio
async def test_delegate_task_runs_child(base_config):
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc(
            "delegate_task",
            {"description": "compute", "prompt": "reply with CHILD_RESULT",
             "subagent_type": "general-purpose"},
            "d1",
        )]),
        AIMessage(content="CHILD_RESULT"),                    # the child's answer
        AIMessage(content="The sub-agent returned CHILD_RESULT."),  # lead's final
    ])
    result = await run_agent("delegate something", config=base_config, prepared=prepared)

    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any("CHILD_RESULT" in m.content for m in tool_msgs)
    assert "CHILD_RESULT" in result.final_text


def test_subagent_limit_truncates_excess_calls():
    mw = SubagentLimitMiddleware(max_concurrent=2)
    calls = [_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}") for i in range(4)]
    calls.append(_tc("read_file", {"path": "x"}, "r1"))
    msg = AIMessage(content="", tool_calls=calls, id="ai1")
    out = mw.after_model({"messages": [msg]}, runtime=None)
    kept = out["messages"][0].tool_calls
    task_calls = [c for c in kept if c["name"] == "delegate_task"]
    assert len(task_calls) == 2  # truncated from 4 to the cap
    assert any(c["name"] == "read_file" for c in kept)  # non-task calls preserved
    assert out["messages"][0].id == "ai1"  # same id -> replaces in place


def test_subagent_limit_clamps_to_2_4_band():
    # Below the floor: a config of 1 must still allow 2.
    mw1 = SubagentLimitMiddleware(max_concurrent=1)
    calls = [_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}") for i in range(4)]
    out1 = mw1.after_model({"messages": [AIMessage(content="", tool_calls=calls, id="a")]}, runtime=None)
    assert len([c for c in out1["messages"][0].tool_calls if c["name"] == "delegate_task"]) == 2
    # Above the ceiling: a config of 10 must cap at 4.
    mw2 = SubagentLimitMiddleware(max_concurrent=10)
    calls6 = [_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}") for i in range(6)]
    out2 = mw2.after_model({"messages": [AIMessage(content="", tool_calls=calls6, id="a")]}, runtime=None)
    assert len([c for c in out2["messages"][0].tool_calls if c["name"] == "delegate_task"]) == 4


def test_subagent_limit_strips_orphaned_tool_use_blocks():
    # Anthropic-shaped AIMessage: tool_use blocks live in list content, mirrored in tool_calls.
    content = [{"type": "text", "text": "spawning"}]
    calls = []
    for i in range(4):
        content.append({"type": "tool_use", "id": f"d{i}", "name": "delegate_task", "input": {}})
        calls.append(_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}"))
    msg = AIMessage(content=content, tool_calls=calls, id="ai1")
    out = SubagentLimitMiddleware(max_concurrent=2).after_model({"messages": [msg]}, runtime=None)
    new = out["messages"][0]
    kept_call_ids = {c["id"] for c in new.tool_calls}
    tool_use_ids = {b["id"] for b in new.content if isinstance(b, dict) and b.get("type") == "tool_use"}
    # No tool_use block may survive without a matching (kept) tool_call, else the next turn 400s.
    assert tool_use_ids == kept_call_ids
    assert len(tool_use_ids) == 2


def test_child_agent_cannot_delegate(base_config):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(base_config.home), context_window=100_000,
                            bash_enabled=True)
    for st in ("general-purpose", "bash"):
        names = [t.name for t in runner._child_tools(st)]
        assert "delegate_task" not in names  # no nested delegation (recursion guard)


@pytest.mark.asyncio
async def test_subagent_usage_attributed_to_parent(base_config):
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc(
            "delegate_task",
            {"description": "c", "prompt": "reply", "subagent_type": "general-purpose"}, "d1")]),
        AIMessage(content="CHILD",
                  usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
        AIMessage(content="done"),
    ])
    result = await run_agent("delegate", config=base_config, prepared=prepared)
    assert result.state.get("usage", {}).get("total_tokens") == 15  # child tokens folded into parent


@pytest.mark.asyncio
async def test_runner_unregistered_after_run(base_config):
    from atom.subagent import get_runner

    prepared = make_prepared([AIMessage(content="hi")])
    result = await run_agent("hi", config=base_config, prepared=prepared)
    assert get_runner(result.thread_id) is None  # no per-thread runner leak
