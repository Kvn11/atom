"""POST /api/runs/{id}/self-improve: validate, build run-log, stage inputs, enqueue a new run."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from atom.api.app import create_app
from atom.workflow.engine import WorkflowEngine
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
from tests.conftest import make_prepared


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _provider(td, sd, wf):
    """Scripted so the queue worker can drain the enqueued self-improve run offline."""
    return make_prepared([AIMessage(content="done")])


def _engine(base_config):
    return WorkflowEngine(base_config, prepared_provider=_provider)


def _install_self_improve(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "self-improve.yaml").write_text(
        "name: self-improve\n"
        "inputs:\n"
        "  - name: run_log\n    type: file\n    required: true\n"
        "  - name: target_workflow\n    type: file\n    required: true\n"
        "  - name: workflow_name\n    required: true\n"
        "  - name: source_run_id\n    required: true\n"
        "  - name: run_status\n    required: false\n"
        "steps:\n  - title: Analyze\n    tasks:\n      - id: a\n        prompt: \"read {{ run_log }}\"\n"
    )


def _install_target(home, name="parallel-poems"):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(
        f"name: {name}\nsteps:\n  - title: S\n    tasks:\n      - id: t1\n        prompt: hi\n")


def _seed_terminal_run(home, run_id="r1", workflow="parallel-poems", status="halted"):
    store = RunStore(str(home))
    m = RunManifest(
        run_id=run_id, workflow=workflow, status=status, created_at="2026-07-18T00:00:00",
        ended_at="2026-07-18T00:01:00", workspace_path=str(store.workspace_dir(run_id)),
        steps=[StepState(index=0, title="S", status="failed", tasks=[
            TaskState(id="t1", thread_id=f"{run_id}:s0:t1", status="failed", error="boom",
                      started_at="2026-07-18T00:00:00", ended_at="2026-07-18T00:01:00")])],
    )
    store.create(m)
    store.save_chat(run_id, 0, "t1", [{"role": "task", "text": "do it"}, {"role": "ai", "text": "boom"}])
    return store


@pytest.mark.asyncio
async def test_self_improve_happy_path(base_config, atom_home):
    _install_self_improve(atom_home)
    _install_target(atom_home)
    _seed_terminal_run(atom_home)
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 202, r.text
        new_id = r.json()["run_id"]
        assert new_id and new_id != "r1"
        # the new run is a self-improve run whose inputs point back at the source
        m = (await client.get(f"/api/runs/{new_id}")).json()
        assert m["workflow"] == "self-improve"
        assert m["inputs"]["source_run_id"] == "r1"
        assert m["inputs"]["workflow_name"] == "parallel-poems"
        # both file inputs were staged into the new run's uploads dir
        store = RunStore(str(atom_home))
        up = store.uploads_dir(new_id)
        assert (up / "run_log.json").exists()
        assert (up / "target_workflow.yaml").exists()
        run_log = json.loads((up / "run_log.json").read_text())
        assert run_log["run"]["run_id"] == "r1" and run_log["run"]["status"] == "halted"


@pytest.mark.asyncio
async def test_self_improve_requires_terminal_run(base_config, atom_home):
    _install_self_improve(atom_home)
    _install_target(atom_home)
    _seed_terminal_run(atom_home, status="running")
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_self_improve_recursion_guard(base_config, atom_home):
    _install_self_improve(atom_home)
    _seed_terminal_run(atom_home, workflow="self-improve")
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_self_improve_run_not_found(base_config, atom_home):
    _install_self_improve(atom_home)
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/ghost/self-improve")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_self_improve_missing_target_yaml(base_config, atom_home):
    _install_self_improve(atom_home)
    _seed_terminal_run(atom_home, workflow="deleted-wf")   # no deleted-wf.yaml on disk
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_self_improve_missing_workflow_definition_503(base_config, atom_home):
    _install_target(atom_home)                              # target exists, self-improve.yaml does NOT
    _seed_terminal_run(atom_home)
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 503
