"""Engine streaming: a task's events reach the bus and the run still completes normally."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.workflow.engine import WorkflowEngine
from atom.workflow.events import channel_key
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared

WS = "/mnt/user-data/workspace"


def _wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo", "inputs": [{"name": "topic", "required": True}],
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "write {{ topic }}"}]}],
    })


def _provider(td, sd, wf):
    return make_prepared([
        AIMessage(content="", tool_calls=[{"name": "write_file",
            "args": {"description": "w", "path": f"{WS}/o.txt", "content": "hi\n"}, "id": "c1", "type": "tool_call"}]),
        AIMessage(content="all done"),
    ])


@pytest.mark.asyncio
async def test_task_publishes_events_and_completes(base_config, atom_home):
    # Run to completion, THEN read the retained closed channel — deterministic (no subscribe-timing
    # race). Live delivery is covered by the bus unit tests (Task 2) and the SSE test (Task 6).
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    key = channel_key("runS", 0, "t1")

    engine.create_run(_wf(), {"topic": "sea"}, "runS", "2026-07-16T00:00:00")
    manifest = await engine.execute("runS")
    assert manifest.status == "complete"

    seen = [ev async for ev in engine.bus.stream(key)]
    assert seen[0]["type"] == "snapshot"
    block_types = [b["type"] for b in seen[0]["blocks"]]
    assert "tool_call" in block_types and "tool_result" in block_types  # engine published task events
    assert seen[-1] == {"type": "done", "error": None}
    # durable snapshot still written (unchanged behavior)
    assert engine.store.load_chat("runS", 0, "t1") is not None


@pytest.mark.asyncio
async def test_streaming_disabled_still_runs(base_config, atom_home):
    base_config.streaming.enabled = False
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    engine.create_run(_wf(), {"topic": "sea"}, "runD", "2026-07-16T00:00:00")
    manifest = await engine.execute("runD")
    assert manifest.status == "complete"
