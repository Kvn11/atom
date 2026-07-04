"""FastAPI automation surface: submit -> poll -> messages/artifacts."""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from atom.api.app import create_app
from atom.workflow.engine import WorkflowEngine
from tests.conftest import make_prepared

WS = "/mnt/user-data/workspace"


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
async def test_submit_run_and_fetch_results(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert any(w["name"] == "demo" for w in (await client.get("/api/workflows")).json())

        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        manifest = await _poll(client, run_id)
        assert manifest["status"] == "complete"

        arts = (await client.get(f"/api/runs/{run_id}/artifacts")).json()
        assert any(a["path"] == "out.txt" for a in arts)
        body = (await client.get(f"/api/runs/{run_id}/artifacts/out.txt")).text
        assert body == "hi\n"

        msgs = (await client.get(f"/api/runs/{run_id}/tasks/0/t1/messages")).json()
        assert isinstance(msgs, list) and msgs


@pytest.mark.asyncio
async def test_missing_required_input_is_422(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {}})
        assert r.status_code == 422
