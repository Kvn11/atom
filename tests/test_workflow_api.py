"""FastAPI automation surface: submit -> poll -> messages/artifacts."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from atom.api.app import create_app
from atom.workflow.engine import WorkflowEngine
from tests.conftest import make_prepared

WS = "/mnt/user-data/workspace"


@asynccontextmanager
async def _client(app):
    # Drive the FastAPI lifespan so the queue worker starts/stops (ASGITransport alone does not).
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _seed(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "demo.yaml").write_text(
        "name: demo\n"
        "inputs:\n  - name: topic\n    required: true\n"
        "steps:\n  - title: Draft\n    tasks:\n      - id: t1\n        prompt: \"write {{ topic }}\"\n"
    )


def _provider(td, sd, wf):
    return make_prepared([
        AIMessage(content="", tool_calls=[{
            "name": "write_file",
            "args": {"description": "w", "path": f"{WS}/out.txt", "content": "hi\n"},
            "id": "c1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{
            "name": "present_files",
            "args": {"filepaths": [f"{WS}/out.txt"]},
            "id": "c2", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])


async def _poll(client, run_id, tries=100):
    for _ in range(tries):
        m = (await client.get(f"/api/runs/{run_id}")).json()
        if m["status"] in ("complete", "halted"):
            return m
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


@pytest.mark.asyncio
async def test_submit_returns_queued_then_worker_drains(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        assert r.status_code == 202
        assert r.json()["status"] == "queued"           # enqueued, not immediately running
        manifest = await _poll(client, r.json()["run_id"])
        assert manifest["status"] == "complete"          # the lifespan worker drained it


@pytest.mark.asyncio
async def test_submit_run_and_fetch_results(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        assert any(w["name"] == "demo" for w in (await client.get("/api/workflows")).json())

        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        manifest = await _poll(client, run_id)
        assert manifest["status"] == "complete"

        arts = (await client.get(f"/api/runs/{run_id}/artifacts")).json()
        art = next(a for a in arts if a["name"] == "out.txt")
        assert art["step"] == 0 and art["task"] == "t1" and art["rel"] == "s0__t1/out.txt"
        body = (await client.get(f"/api/runs/{run_id}/artifacts/{art['rel']}")).text
        assert body == "hi\n"

        msgs = (await client.get(f"/api/runs/{run_id}/tasks/0/t1/messages")).json()
        assert isinstance(msgs, list) and msgs


@pytest.mark.asyncio
async def test_missing_required_input_is_422(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {}})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_runs_list_returns_paginated_summaries(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        run_id = r.json()["run_id"]
        await _poll(client, run_id)

        page = (await client.get("/api/runs?status=all&limit=50&offset=0")).json()
        assert page["total"] >= 1
        assert any(i["run_id"] == run_id for i in page["items"])
        assert set(page["counts"]) == {"active", "complete", "halted"}
        item = next(i for i in page["items"] if i["run_id"] == run_id)
        assert item["tasks_total"] == 1 and item["workflow"] == "demo"


@pytest.mark.asyncio
async def test_unknown_artifact_is_404(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        run_id = r.json()["run_id"]
        await _poll(client, run_id)
        resp = await client.get(f"/api/runs/{run_id}/artifacts/s0__t1/does-not-exist.txt")
        assert resp.status_code == 404


def _html_provider(td, sd, wf):
    return make_prepared([
        AIMessage(content="", tool_calls=[{
            "name": "write_file",
            "args": {"description": "w", "path": f"{WS}/page.html", "content": "<b>hi</b>\n"},
            "id": "c1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{
            "name": "present_files",
            "args": {"filepaths": [f"{WS}/page.html"]},
            "id": "c2", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])


@pytest.mark.asyncio
async def test_html_artifact_served_as_attachment(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_html_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        run_id = r.json()["run_id"]
        await _poll(client, run_id)
        arts = (await client.get(f"/api/runs/{run_id}/artifacts")).json()
        rel = next(a["rel"] for a in arts if a["name"] == "page.html")
        resp = await client.get(f"/api/runs/{run_id}/artifacts/{rel}")
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
