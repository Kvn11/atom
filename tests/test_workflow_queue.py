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


@pytest.mark.asyncio
async def test_execute_resumes_skipping_completed_step(base_config, atom_home, monkeypatch):
    from atom.workflow.run_store import RunManifest, StepState, TaskState
    from atom.runtime import RunResult

    calls: list[str] = []

    async def spy(prompt, **kwargs):
        calls.append(kwargs.get("thread_id", "?"))
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    wf = WorkflowDef.model_validate({
        "name": "demo",
        "steps": [
            {"title": "A", "tasks": [{"id": "a", "prompt": "x"}]},
            {"title": "B", "tasks": [{"id": "b", "prompt": "y"}]},
        ],
    })
    # Persist a run where step 0 already completed; step 1 pending (as after a resume/recover).
    m = RunManifest(
        run_id="rr1", workflow="demo", status="queued",
        created_at="2026-07-12T00:00:00", enqueued_at="2026-07-12T00:00:00.000000",
        workspace_path=str(engine_store_ws(base_config, "rr1")),
        steps=[
            StepState(index=0, title="A", status="complete",
                      tasks=[TaskState(id="a", thread_id="rr1:s0:a", status="succeeded")]),
            StepState(index=1, title="B", status="pending",
                      tasks=[TaskState(id="b", thread_id="rr1:s1:b", status="pending")]),
        ],
    )
    engine = WorkflowEngine(base_config)
    engine.store.create(m)
    engine._defs["rr1"] = wf               # provide the WorkflowDef (no demo.yaml on disk here)

    manifest = await engine.execute("rr1")

    assert manifest.status == "complete"
    assert calls == ["rr1:s1:b"]           # only the unfinished task ran; step 0 was skipped


def engine_store_ws(cfg, run_id):
    from atom.workflow.run_store import RunStore
    return RunStore(cfg.home).workspace_dir(run_id)


@pytest.mark.asyncio
async def test_execute_cancelled_requeues_run_not_halted(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    started = asyncio.Event()

    async def slow(prompt, **kwargs):
        started.set()
        await asyncio.sleep(30)          # block so we can cancel mid-run
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", slow)
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "cx1", "2026-07-12T00:00:00")
    engine.enqueue("cx1")

    task = asyncio.create_task(engine.execute("cx1"))
    await asyncio.wait_for(started.wait(), timeout=5)   # ensure execute() is mid-run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # requeued for step-level resume next start, NOT halted
    assert engine.store.load("cx1").status == "queued"


@pytest.mark.asyncio
async def test_worker_serializes_runs_at_concurrency_one(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult
    live = {"n": 0, "max": 0}

    async def spy(prompt, **kwargs):
        live["n"] += 1
        live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.05)
        live["n"] -= 1
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    base_config.queue.max_concurrent_runs = 1
    engine = WorkflowEngine(base_config)
    for rid in ("w1", "w2", "w3"):
        engine.create_run(_one_task_wf(), {}, rid, "2026-07-12T00:00:00")
        engine.enqueue(rid)

    engine.start_worker()
    for _ in range(200):                       # wait until all drained
        if all(engine.store.load(r).status == "complete" for r in ("w1", "w2", "w3")):
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert live["max"] == 1                     # never two runs at once at concurrency 1
    assert all(engine.store.load(r).status == "complete" for r in ("w1", "w2", "w3"))


@pytest.mark.asyncio
async def test_worker_allows_two_at_concurrency_two(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult
    live = {"n": 0, "max": 0}

    async def spy(prompt, **kwargs):
        live["n"] += 1
        live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.05)
        live["n"] -= 1
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    base_config.queue.max_concurrent_runs = 2
    engine = WorkflowEngine(base_config)
    for rid in ("p1", "p2", "p3"):
        engine.create_run(_one_task_wf(), {}, rid, "2026-07-12T00:00:00")
        engine.enqueue(rid)

    engine.start_worker()
    for _ in range(200):
        if all(engine.store.load(r).status == "complete" for r in ("p1", "p2", "p3")):
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert live["max"] == 2                     # exactly two in flight, never three


@pytest.mark.asyncio
async def test_worker_survives_transient_scan_error(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    async def spy(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    base_config.queue.poll_interval_seconds = 0.05     # fast back-off retry for the test
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "sv1", "2026-07-12T00:00:00")
    engine.enqueue("sv1")

    calls = {"n": 0}
    real_scan = engine.store.queued_run_ids

    def flaky_scan():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient scan failure")     # first iteration blows up
        return real_scan()

    monkeypatch.setattr(engine.store, "queued_run_ids", flaky_scan)

    engine.start_worker()
    for _ in range(200):
        if engine.store.load("sv1").status == "complete":
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert calls["n"] >= 2                              # the loop retried after the error
    assert engine.store.load("sv1").status == "complete"   # worker recovered and drained


def test_recover_resyncs_torn_enqueue_so_run_is_not_stranded(base_config, atom_home):
    # Simulate a crash BETWEEN RunStore.save()'s two atomic writes during enqueue:
    # run.json=queued but summary.json still stale (=pending). Because the queue scan reads the
    # summary cache, such a run is invisible to the worker unless recover() re-syncs it.
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "torn1", "2026-07-12T00:00:00")  # both files = pending
    m = engine.store.load("torn1")
    m.status = "queued"
    m.enqueued_at = "2026-07-12T00:00:01.000000"
    # write ONLY run.json (bypassing save(), which would also refresh summary.json)
    engine.store._manifest_path("torn1").write_text(m.model_dump_json(indent=2), encoding="utf-8")

    assert engine.store.queued_run_ids() == []          # stranded: summary cache still says pending
    engine.recover()                                    # must re-sync, not skip
    assert engine.store.queued_run_ids() == ["torn1"]   # un-stranded: worker can now drain it


@pytest.mark.asyncio
async def test_await_run_drains_own_run_when_lease_free(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    async def spy(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "cli1", "2026-07-12T00:00:00")
    engine.enqueue("cli1")

    m = await engine.await_run("cli1")
    assert m.status == "complete"
    assert engine.lease.acquire() is True          # lease was released after draining
    engine.lease.release()


@pytest.mark.asyncio
async def test_worker_quarantines_poison_run_after_max_attempts(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    base_config.queue.max_drain_attempts = 3

    async def spy(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "poison", "2026-07-12T00:00:00")
    engine.enqueue("poison")
    engine.create_run(_one_task_wf(), {}, "healthy", "2026-07-12T00:00:01")
    engine.enqueue("healthy")

    real_execute = engine.execute
    attempts = {"n": 0}

    async def flaky_execute(run_id):
        if run_id == "poison":
            attempts["n"] += 1
            raise OSError("simulated unreadable run.json")   # fails BEFORE terminalizing
        return await real_execute(run_id)

    monkeypatch.setattr(engine, "execute", flaky_execute)

    engine.start_worker()
    for _ in range(300):
        if engine.store.load("healthy").status == "complete" and "poison" in engine._quarantine:
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert engine.store.load("healthy").status == "complete"   # a healthy run is NOT blocked by the poison one
    assert "poison" in engine._quarantine                       # poison quarantined, not hot-looping forever
    assert attempts["n"] == 3                                    # bounded at max_drain_attempts (not infinite)
