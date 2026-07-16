"""SSE stream endpoint: frame format, snapshot+done, 404 when disabled."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from atom.api.app import create_app
from atom.workflow.engine import WorkflowEngine
from atom.workflow.events import channel_key


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.mark.asyncio
async def test_stream_yields_snapshot_then_done(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    app = create_app(base_config, engine=engine)
    key = channel_key("r1", 0, "t1")
    await engine.bus.publish(key, {"type": "text_delta", "text": "hello"})
    await engine.bus.close(key)

    async with _client(app) as c:
        async with c.stream("GET", "/api/runs/r1/tasks/0/t1/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk
    assert "event: snapshot" in body
    assert "hello" in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_stream_404_when_disabled(base_config, atom_home):
    base_config.streaming.enabled = False
    engine = WorkflowEngine(base_config)
    app = create_app(base_config, engine=engine)
    async with _client(app) as c:
        resp = await c.get("/api/runs/r1/tasks/0/t1/stream")
    assert resp.status_code == 404
