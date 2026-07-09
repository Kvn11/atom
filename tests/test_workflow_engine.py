"""Workflow engine: shared-workspace hand-off and halt-on-failure, with scripted models."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _write_call(path, content, cid):
    return AIMessage(content="", tool_calls=[_tc(
        "write_file", {"description": "w", "path": path, "content": content}, cid)])


def _read_call(path, cid):
    return AIMessage(content="", tool_calls=[_tc("read_file", {"description": "r", "path": path}, cid)])


WS = "/mnt/user-data/workspace"


def _draft_only() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "inputs": [{"name": "topic", "required": True}],
        "steps": [{
            "title": "Draft",
            "tasks": [
                {"id": "poet_a", "prompt": "write {{ topic }} -> a"},
                {"id": "poet_b", "prompt": "write {{ topic }} -> b"},
            ],
        }],
    })


def _draft_then_refine() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "inputs": [{"name": "topic", "required": True}],
        "steps": [
            {"title": "Draft", "tasks": [{"id": "poet_a", "prompt": "write {{ topic }}"}]},
            {"title": "Refine", "tasks": [{"id": "refiner", "prompt": "refine"}]},
        ],
    })


@pytest.mark.asyncio
async def test_single_step_two_tasks_write_shared_workspace(base_config, atom_home):
    scripts = {
        "poet_a": [_write_call(f"{WS}/poem_a.md", "aaa\n", "a1"), AIMessage(content="wrote a")],
        "poet_b": [_write_call(f"{WS}/poem_b.md", "bbb\n", "b1"), AIMessage(content="wrote b")],
    }
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])),
    )
    wf = _draft_only()
    manifest = engine.create_run(wf, {"topic": "sea"}, "run1", "2026-07-03T00:00:00")
    manifest = await engine.execute("run1")

    assert manifest.status == "complete"
    assert manifest.steps[0].status == "complete"
    assert [t.status for t in manifest.steps[0].tasks] == ["succeeded", "succeeded"]
    ws = engine.store.workspace_dir("run1")
    assert (ws / "poem_a.md").read_text() == "aaa\n"
    assert (ws / "poem_b.md").read_text() == "bbb\n"
    # each task's chat snapshot was persisted
    assert engine.store.load_chat("run1", 0, "poet_a") is not None


@pytest.mark.asyncio
async def test_step2_reads_what_step1_wrote(base_config, atom_home):
    scripts = {
        "poet_a": [_write_call(f"{WS}/poem_a.md", "the tide returns\n", "w1"), AIMessage(content="drafted")],
        "refiner": [_read_call(f"{WS}/poem_a.md", "r1"), AIMessage(content="refined")],
    }
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])),
    )
    engine.create_run(_draft_then_refine(), {"topic": "sea"}, "run2", "2026-07-03T00:00:00")
    manifest = await engine.execute("run2")

    assert manifest.status == "complete"
    # the refiner's chat contains a tool message showing step-1's file content -> shared workspace proven
    chat = engine.store.load_chat("run2", 1, "refiner")
    tool_texts = "\n".join(m["text"] for m in chat if m["role"] == "tool")
    assert "the tide returns" in tool_texts


@pytest.mark.asyncio
async def test_failed_task_halts_run_and_skips_next_step(base_config, atom_home, monkeypatch):
    import atom.workflow.engine as engine_mod
    real = engine_mod.run_agent

    async def flaky_run_agent(prompt, **kwargs):
        if "BOOM" in prompt:
            raise RuntimeError("task blew up")
        return await real(prompt, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent", flaky_run_agent)

    wf = WorkflowDef.model_validate({
        "name": "demo",
        "steps": [
            {"title": "Draft", "tasks": [{"id": "boom", "prompt": "BOOM please"}]},
            {"title": "Never", "tasks": [{"id": "later", "prompt": "should not run"}]},
        ],
    })
    engine = WorkflowEngine(base_config)
    engine.create_run(wf, {}, "run3", "2026-07-03T00:00:00")
    manifest = await engine.execute("run3")

    assert manifest.status == "halted"
    assert manifest.steps[0].status == "failed"
    assert manifest.steps[0].tasks[0].status == "failed"
    assert "task blew up" in (manifest.steps[0].tasks[0].error or "")
    # step 2 never ran
    assert manifest.steps[1].status == "pending"
    assert manifest.steps[1].tasks[0].status == "pending"
    assert engine.store.load_chat("run3", 1, "later") is None


@pytest.mark.asyncio
async def test_bad_prompt_template_halts_run(base_config, atom_home):
    wf = WorkflowDef.model_validate({
        "name": "demo",
        "steps": [
            {"title": "Draft", "tasks": [{"id": "t1", "prompt": "use {{ undeclared_var }}"}]},
            {"title": "Never", "tasks": [{"id": "t2", "prompt": "later"}]},
        ],
    })
    engine = WorkflowEngine(base_config)  # no prepared_provider; render error happens pre-run_agent
    engine.create_run(wf, {}, "runbad", "2026-07-03T00:00:00")
    manifest = await engine.execute("runbad")
    assert manifest.status == "halted"
    assert manifest.steps[0].tasks[0].status == "failed"
    assert "undeclared_var" in (manifest.steps[0].tasks[0].error or "")
    assert manifest.steps[1].tasks[0].status == "pending"   # step 2 never ran


@pytest.mark.asyncio
async def test_restricted_allowed_roots_still_allows_run_workspace(base_config, atom_home):
    base_config.sandbox.allowed_workspace_roots = [str(atom_home / "unrelated")]  # does NOT include runs dir
    scripts = {"t1": [_write_call(f"{WS}/out.txt", "hi\n", "w1"), AIMessage(content="ok")]}
    engine = WorkflowEngine(base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])))
    wf = WorkflowDef.model_validate({"name": "demo",
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "write"}]}]})
    engine.create_run(wf, {}, "runrestrict", "2026-07-03T00:00:00")
    manifest = await engine.execute("runrestrict")
    assert manifest.status == "complete"
    assert (engine.store.workspace_dir("runrestrict") / "out.txt").read_text() == "hi\n"


def _one_task_workflow() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "write"}]}],
    })


def _present_call(paths, cid):
    return AIMessage(content="", tool_calls=[_tc("present_files", {"filepaths": paths}, cid)])


# ---- regression tests for code-review findings #1, #2, #4, #7, #10 ----

@pytest.mark.asyncio
async def test_execute_load_workflow_fallback_error_terminalizes_run(base_config, atom_home, monkeypatch):
    """FIX #2: execute()'s load_workflow fallback (and everything else after the first
    store.load) must be inside the terminal-state guard, not before it."""
    engine = WorkflowEngine(base_config)
    wf = _draft_only()
    engine.create_run(wf, {"topic": "sea"}, "run_loaderr", "2026-07-03T00:00:00")
    engine._defs.pop("run_loaderr", None)  # force the load_workflow(...) fallback path

    monkeypatch.setattr(
        engine_mod, "load_workflow",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gone")),
    )

    with pytest.raises(FileNotFoundError):
        await engine.execute("run_loaderr")

    assert engine.store.load("run_loaderr").status == "halted"


@pytest.mark.asyncio
async def test_launch_orphaned_task_terminalized_via_done_callback(base_config, atom_home, monkeypatch):
    """FIX #1: launch() discards the Task it creates; nothing must let it be GC'd, and
    any exception it raises must be retrieved and terminalize the run via a done-callback."""
    engine = WorkflowEngine(base_config)
    wf = _draft_only()
    engine.create_run(wf, {"topic": "sea"}, "run_orphan", "2026-07-03T00:00:00")

    async def boom(run_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "execute", boom)

    task = engine.launch("run_orphan")
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # let any call_soon-scheduled done-callback actually run

    assert engine.store.load("run_orphan").status == "halted"
    assert task not in engine._tasks


@pytest.mark.asyncio
async def test_defs_evicted_after_successful_execute(base_config, atom_home):
    """FIX #7: self._defs must never accumulate one WorkflowDef per run forever in a
    long-lived `atom serve` process."""
    scripts = {"t1": [_write_call(f"{WS}/out.txt", "hi\n", "w1"), AIMessage(content="ok")]}
    engine = WorkflowEngine(
        base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id]))
    )
    engine.create_run(_one_task_workflow(), {}, "run_evict", "2026-07-03T00:00:00")

    manifest = await engine.execute("run_evict")

    assert manifest.status == "complete"
    assert "run_evict" not in engine._defs


@pytest.mark.asyncio
async def test_task_timeout_zero_or_negative_disables_cleanly(base_config, atom_home):
    """FIX #10: 0/negative task_timeout_seconds must be an explicit, documented "disabled"
    sentinel — not a truthiness accident. (0 already happened to fall out to None via the old
    `x or None`, so it's covered here mostly as a locked-in characterization; -1 is the case
    that genuinely regresses under the old code, since `-1 or None` is truthy and gets handed
    to asyncio.wait_for(coro, -1), which raises TimeoutError immediately.)"""
    scripts = {"t1": [AIMessage(content="ok")]}

    def make_engine(timeout_value):
        base_config.workflow.task_timeout_seconds = timeout_value
        return WorkflowEngine(
            base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts["t1"]))
        )

    engine_zero = make_engine(0)
    engine_zero.create_run(_one_task_workflow(), {}, "run_timeout0", "2026-07-03T00:00:00")
    manifest_zero = await engine_zero.execute("run_timeout0")
    assert manifest_zero.status == "complete"

    engine_neg = make_engine(-1)
    engine_neg.create_run(_one_task_workflow(), {}, "run_timeoutneg", "2026-07-03T00:00:00")
    manifest_neg = await engine_neg.execute("run_timeoutneg")
    assert manifest_neg.status == "complete"


@pytest.mark.asyncio
async def test_task_save_failure_does_not_escape_execute_or_wedge_sibling(base_config, atom_home, monkeypatch):
    """FIX #4: _run_task must never raise (even from its own store.save() calls), and
    gather() must run with return_exceptions=True as belt-and-suspenders."""
    scripts = {
        "poet_a": [AIMessage(content="wrote a")],
        "poet_b": [AIMessage(content="wrote b")],
    }
    engine = WorkflowEngine(
        base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id]))
    )
    engine.create_run(_draft_only(), {"topic": "sea"}, "run_savefail", "2026-07-03T00:00:00")

    real_save = engine.store.save
    state = {"raised": False}

    def flaky_save(manifest):
        if not state["raised"]:
            for step in manifest.steps:
                for t in step.tasks:
                    if t.id == "poet_a" and t.status == "running":
                        state["raised"] = True
                        raise RuntimeError("disk full")
        real_save(manifest)

    monkeypatch.setattr(engine.store, "save", flaky_save)

    manifest = await engine.execute("run_savefail")  # must NOT raise

    assert manifest.status in ("complete", "halted")
    assert engine.store.load("run_savefail").status in ("complete", "halted")
    poet_a = next(t for t in manifest.steps[0].tasks if t.id == "poet_a")
    poet_b = next(t for t in manifest.steps[0].tasks if t.id == "poet_b")
    assert poet_a.status == "failed"       # its own save() blew up -> recorded as failed
    assert poet_b.status == "succeeded"    # sibling was not wedged by poet_a's failure


@pytest.mark.asyncio
async def test_presented_artifacts_captured(base_config, atom_home):
    scripts = {"t1": [
        _write_call(f"{WS}/out.md", "hi\n", "w1"),
        _present_call([f"{WS}/out.md"], "p1"),
        AIMessage(content="done"),
    ]}
    engine = WorkflowEngine(
        base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])))
    engine.create_run(_one_task_workflow(), {}, "runart", "2026-07-03T00:00:00")
    manifest = await engine.execute("runart")

    assert manifest.status == "complete"
    arts = manifest.steps[0].tasks[0].artifacts
    assert len(arts) == 1 and arts[0].name == "out.md" and arts[0].rel == "s0__t1/out.md"
    assert (engine.store.artifacts_dir("runart") / "s0__t1" / "out.md").read_text() == "hi\n"


@pytest.mark.asyncio
async def test_draft_artifact_snapshot_survives_refine_overwrite(base_config, atom_home):
    scripts = {
        "poet_a": [_write_call(f"{WS}/poem_a.md", "draft\n", "w1"),
                   _present_call([f"{WS}/poem_a.md"], "p1"), AIMessage(content="d")],
        "refiner": [_write_call(f"{WS}/poem_a.md", "refined\n", "w2"),
                    _present_call([f"{WS}/poem_a.md"], "p2"), AIMessage(content="r")],
    }
    engine = WorkflowEngine(
        base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])))
    engine.create_run(_draft_then_refine(), {"topic": "sea"}, "runsnap", "2026-07-03T00:00:00")
    manifest = await engine.execute("runsnap")

    assert manifest.status == "complete"
    ad = engine.store.artifacts_dir("runsnap")
    assert (ad / "s0__poet_a" / "poem_a.md").read_text() == "draft\n"      # snapshot preserved
    assert (ad / "s1__refiner" / "poem_a.md").read_text() == "refined\n"
    assert (engine.store.workspace_dir("runsnap") / "poem_a.md").read_text() == "refined\n"


@pytest.mark.asyncio
async def test_task_trace_carries_session_id(base_config, atom_home, monkeypatch):
    """Each task's trace must carry its own thread id as session_id (one thread per lead agent)."""
    real = engine_mod.run_agent
    traces = []

    async def spy(prompt, **kwargs):
        traces.append(kwargs.get("trace"))
        return await real(prompt, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    scripts = {
        "poet_a": [AIMessage(content="a done")],
        "poet_b": [AIMessage(content="b done")],
    }
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])),
    )
    engine.create_run(_draft_only(), {"topic": "sea"}, "runx", "2026-07-03T00:00:00")
    await engine.execute("runx")

    sids = {t["metadata"]["session_id"] for t in traces}
    assert sids == {"runx:s0:poet_a", "runx:s0:poet_b"}  # distinct thread per task
    assert all(t["metadata"]["agent_role"] == "lead" for t in traces)
    # Leads carry no role tag (role lives in metadata) so it can't leak onto nested sub-agent runs.
    assert all("role:lead" not in t["tags"] for t in traces)
