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
