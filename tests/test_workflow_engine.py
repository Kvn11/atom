"""Workflow engine: shared-workspace hand-off and halt-on-failure, with scripted models."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

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
