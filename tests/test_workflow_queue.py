"""Durable queue: enqueue, worker draining, crash recovery, step-level resume."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared


def _one_task_wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "hi"}]}],
    })


def test_enqueue_marks_queued_and_stamps_enqueued_at(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "rq1", "2026-07-12T00:00:00")
    engine.enqueue("rq1")
    m = engine.store.load("rq1")
    assert m.status == "queued"
    assert m.enqueued_at is not None
    assert engine.store.queued_run_ids() == ["rq1"]

    # terminal runs are never re-opened by enqueue
    done = engine.store.load("rq1")
    done.status = "complete"
    engine.store.save(done)
    engine.enqueue("rq1")
    assert engine.store.load("rq1").status == "complete"


def test_recover_requeues_running_and_resets_interrupted_step(base_config, atom_home):
    from atom.workflow.run_store import RunManifest, StepState, TaskState
    engine = WorkflowEngine(base_config)
    # Step 0 fully done; step 1 interrupted mid-flight (one succeeded, one still "running").
    m = RunManifest(
        run_id="rc1", workflow="demo", status="running",
        created_at="2026-07-12T00:00:00", enqueued_at="2026-07-12T00:00:00.000000",
        workspace_path=str(engine.store.workspace_dir("rc1")),
        steps=[
            StepState(index=0, title="A", status="complete",
                      tasks=[TaskState(id="a", thread_id="rc1:s0:a", status="succeeded")]),
            StepState(index=1, title="B", status="running", tasks=[
                TaskState(id="b1", thread_id="rc1:s1:b1", status="succeeded"),
                TaskState(id="b2", thread_id="rc1:s1:b2", status="running", started_at="t"),
            ]),
        ],
    )
    engine.store.create(m)

    engine.recover()

    r = engine.store.load("rc1")
    assert r.status == "queued"                      # re-queued for resume
    assert r.steps[0].status == "complete"           # finished step untouched
    b1, b2 = r.steps[1].tasks
    assert b1.status == "succeeded"                  # already-done task kept
    assert b2.status == "pending" and b2.started_at is None  # in-flight task reset for rerun
