"""User-initiated workflow cancellation: graceful, durable, terminal 'cancelled' status."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.runtime import RunResult
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef


def _two_step_wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "steps": [
            {"title": "One", "tasks": [{"id": "t1", "prompt": "do one"}]},
            {"title": "Two", "tasks": [{"id": "t2", "prompt": "do two"}]},
        ],
    })


def test_request_cancel_queued_run_marks_cancelled(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rq", "2026-07-18T00:00:00")
    engine.enqueue("rq")
    assert engine.store.load("rq").status == "queued"

    res = engine.request_cancel("rq")

    assert res["status"] == "cancelled"
    assert engine.store.load("rq").status == "cancelled"
    assert engine.store.cancel_requested("rq") is False       # marker cleared on finalize
    assert "rq" not in engine.store.queued_run_ids()          # dropped from the FIFO scan


def test_request_cancel_idempotent_on_cancelled(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rc", "2026-07-18T00:00:00")
    engine.request_cancel("rc")                                # pending -> cancelled
    res = engine.request_cancel("rc")
    assert res == {"run_id": "rc", "status": "cancelled", "already": True}


def test_request_cancel_finished_run_reports_already(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    m = engine.create_run(_two_step_wf(), {}, "rf", "2026-07-18T00:00:00")
    m.status = "complete"
    engine.store.save(m)
    res = engine.request_cancel("rf")
    assert res == {"run_id": "rf", "status": "complete", "already": True}
    assert engine.store.load("rf").status == "complete"


def test_request_cancel_unknown_run_raises(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    with pytest.raises(FileNotFoundError):
        engine.request_cancel("ghost")


def test_enqueue_will_not_reopen_a_cancelled_run(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rec", "2026-07-18T00:00:00")
    engine.request_cancel("rec")                               # -> cancelled
    engine.enqueue("rec")                                      # must be a no-op
    assert engine.store.load("rec").status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_requested_before_execute_finalizes_immediately(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rpre", "2026-07-18T00:00:00")
    engine._cancel_requested.add("rpre")
    engine.store.write_cancel_marker("rpre", "2026-07-18T00:00:00.000000")

    manifest = await engine.execute("rpre")

    assert manifest.status == "cancelled"
    assert manifest.steps[0].tasks[0].status == "pending"        # no task ran
    assert engine.store.load_chat("rpre", 0, "t1") is None
    assert engine.store.cancel_requested("rpre") is False         # marker cleared


@pytest.mark.asyncio
async def test_running_run_cancels_gracefully_after_current_task(base_config, atom_home, monkeypatch):
    engine = WorkflowEngine(base_config)

    calls = []

    async def spy(prompt, **kwargs):
        calls.append(prompt)
        sc = kwargs.get("should_cancel")
        engine._cancel_requested.add("rcg")          # cancel arrives WHILE this task runs
        return RunResult(
            thread_id=kwargs.get("thread_id", "t"),
            messages=[AIMessage(content="partial")],
            final_text="partial", state={},
            cancelled=bool(sc and sc()),
        )

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    engine.create_run(_two_step_wf(), {}, "rcg", "2026-07-18T00:00:00")

    manifest = await engine.execute("rcg")

    assert manifest.status == "cancelled"
    assert manifest.steps[0].tasks[0].status == "failed"
    assert manifest.steps[0].tasks[0].error == "cancelled"
    assert manifest.steps[1].tasks[0].status == "pending"        # step 2 never started
    assert len(calls) == 1                                        # only the step-1 task ran
    assert engine.store.cancel_requested("rcg") is False          # marker cleared
    # the partial transcript of the interrupted task was still persisted
    assert engine.store.load_chat("rcg", 0, "t1") is not None
