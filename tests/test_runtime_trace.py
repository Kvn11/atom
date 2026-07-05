"""run_agent trace config merge."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.runtime import _apply_trace, run_agent
from tests.conftest import make_prepared


def test_apply_trace_merges_keys():
    cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 100}
    out = _apply_trace(cfg, {"run_name": "wf/s/t", "tags": ["a"], "metadata": {"x": 1}})
    assert out["run_name"] == "wf/s/t"
    assert out["tags"] == ["a"]
    assert out["metadata"] == {"x": 1}


def test_apply_trace_none_is_noop():
    cfg = {"configurable": {}}
    assert _apply_trace(cfg, None) == {"configurable": {}}


@pytest.mark.asyncio
async def test_run_agent_accepts_trace(base_config):
    prepared = make_prepared([AIMessage(content="hello")])
    result = await run_agent(
        "hi", config=base_config, prepared=prepared,
        trace={"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}},
    )
    assert result.final_text == "hello"
