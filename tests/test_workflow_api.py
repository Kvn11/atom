"""FastAPI automation surface: submit -> poll -> messages/artifacts."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

import atom.observability.export as export_mod
from atom.api.app import _content_disposition, _is_inline_unsafe, create_app
from atom.observability.export import ExportResult
from atom.workflow.engine import WorkflowEngine
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
from tests.conftest import make_prepared

WS = "/mnt/user-data/workspace"


@asynccontextmanager
async def _client(app):
    # Drive the FastAPI lifespan so the queue worker starts/stops (ASGITransport alone does not).
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@asynccontextmanager
async def _client_no_worker(app):
    # Route-only tests: don't drive the lifespan (no queue worker to recover fake runs).
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


def _seed_notes_wf(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "notewf.yaml").write_text(
        "name: notewf\ndescription: has notes.\n"
        "notes:\n  enabled: true\n"
        "steps:\n  - title: S\n    tasks:\n      - id: t1\n        prompt: \"hi\"\n"
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
        assert set(page["counts"]) == {"active", "complete", "halted", "cancelled"}
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


def _export_app(base_config):
    """App whose server is configured for export (a LangSmith project is set)."""
    base_config.observability.project = "proj"
    return create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))


@pytest.mark.asyncio
async def test_export_endpoint_whole_run(base_config, atom_home, monkeypatch):
    seen = {}
    def fake_run(home, run_id, *, project, **kw):
        seen.update(run_id=run_id, project=project)
        return ExportResult(run_id=run_id, path=f"/x/{run_id}/export.json",
                            complete=True, expected_roots=2, fetched_roots=2)
    monkeypatch.setattr(export_mod, "export_run", fake_run)
    app = _export_app(base_config)
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "run" and body["task_id"] is None
        assert body["complete"] is True and body["fetched_roots"] == 2
        assert seen["run_id"] == "r1"


@pytest.mark.asyncio
async def test_export_endpoint_single_task(base_config, atom_home, monkeypatch):
    seen = {}
    def fake_task(home, run_id, step, task, *, project, **kw):
        seen.update(run_id=run_id, step=step, task=task)
        return ExportResult(run_id=run_id, path=f"/x/{run_id}/exports/s{step}__{task}.json",
                            complete=True, expected_roots=1, fetched_roots=1, task_id=task)
    monkeypatch.setattr(export_mod, "export_task", fake_task)
    app = _export_app(base_config)
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={"step": 0, "task": "t1"})
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "task" and body["task_id"] == "t1"
        assert seen == {"run_id": "r1", "step": 0, "task": "t1"}   # step 0 is honored (not treated as falsy)


@pytest.mark.asyncio
async def test_export_endpoint_run_not_found_404(base_config, atom_home, monkeypatch):
    def missing(home, run_id, *, project, **kw):
        raise FileNotFoundError(run_id)
    monkeypatch.setattr(export_mod, "export_run", missing)
    app = _export_app(base_config)
    async with _client(app) as client:
        r = await client.post("/api/runs/ghost/export", json={})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_export_endpoint_non_terminal_task_400(base_config, atom_home, monkeypatch):
    def not_done(home, run_id, step, task, *, project, **kw):
        raise ValueError("task 't1' has not completed (status: running)")
    monkeypatch.setattr(export_mod, "export_task", not_done)
    app = _export_app(base_config)
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={"step": 0, "task": "t1"})
        assert r.status_code == 400
        assert "has not completed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_export_endpoint_unknown_task_404(base_config, atom_home, monkeypatch):
    def unknown(home, run_id, step, task, *, project, **kw):
        raise KeyError(f"task {task!r} not found in step {step} of run {run_id!r}")
    monkeypatch.setattr(export_mod, "export_task", unknown)
    app = _export_app(base_config)
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={"step": 0, "task": "ghost"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_export_endpoint_unconfigured_is_503(base_config, atom_home):
    # No observability.project configured -> a valid request must NOT be blamed on the client (4xx);
    # it's a server-config problem (5xx).
    base_config.observability.project = None
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={})
        assert r.status_code == 503


def _seed_run(atom_home, run_id="r1"):
    """Create a run manifest on disk (no queue entry) so the download route can load it."""
    store = RunStore(str(atom_home))
    store.create(RunManifest(
        run_id=run_id, workflow="wf", created_at="2026-07-17T00:00:00",
        workspace_path=str(store.workspace_dir(run_id)),
        steps=[StepState(index=0, title="S", tasks=[
            TaskState(id="t1", thread_id=f"{run_id}:s0:t1", status="succeeded")])],
    ))
    return store


@pytest.mark.asyncio
async def test_download_export_whole_run(base_config, atom_home):
    store = _seed_run(atom_home)
    payload = {"run_id": "r1", "roots": [{"id": "root1"}]}
    store.export_path("r1").write_text(json.dumps(payload))
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/r1/export/download")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "attachment" in r.headers.get("content-disposition", "")
        assert json.loads(r.content) == payload


@pytest.mark.asyncio
async def test_download_export_single_task(base_config, atom_home):
    store = _seed_run(atom_home)
    p = store.task_export_path("r1", 0, "t1")
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"scope": "task", "task_id": "t1"}
    p.write_text(json.dumps(payload))
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/r1/export/download?step=0&task=t1")
        assert r.status_code == 200
        assert json.loads(r.content) == payload
        assert "attachment" in r.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_download_export_not_generated_is_404(base_config, atom_home):
    _seed_run(atom_home)   # manifest exists, but no export file was written
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/r1/export/download")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_export_unknown_run_is_404(base_config, atom_home):
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/ghost/export/download")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_export_task_traversal_is_404(base_config, atom_home):
    store = _seed_run(atom_home)
    store.export_path("r1").write_text("{}")   # a run export exists; the crafted task must not escape to it or the FS
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/r1/export/download?step=0&task=" + quote("../../etc/passwd", safe=""))
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_export_large_file_streams_with_content_length(base_config, atom_home):
    store = _seed_run(atom_home)
    # ~3 MB export: dozens of FileResponse chunks, exercised as a real streamed download.
    big = json.dumps({"roots": [{"id": i, "blob": "x" * 1000} for i in range(3000)]})
    store.export_path("r1").write_text(big)
    size = store.export_path("r1").stat().st_size
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/r1/export/download")
        assert r.status_code == 200
        assert int(r.headers["content-length"]) == size   # advertised from os.stat — no in-memory cap
        assert len(r.content) == size                      # full byte stream delivered


# --- provider dispatch (LangSmith vs LangFuse) ---

import atom.observability.langfuse_export as lf_mod


@pytest.mark.asyncio
async def test_export_endpoint_dispatches_to_langfuse(base_config, atom_home, monkeypatch):
    """provider=langfuse -> the endpoint calls the LangFuse exporter, gated on LANGFUSE keys."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    base_config.observability.provider = "langfuse"
    seen = {}
    def fake_run(home, run_id, *, project, **kw):
        seen.update(run_id=run_id, project=project)
        return ExportResult(run_id=run_id, path=f"/x/{run_id}/export.json",
                            complete=True, expected_roots=1, fetched_roots=1)
    monkeypatch.setattr(lf_mod, "export_run", fake_run)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "run"
        assert seen == {"run_id": "r1", "project": None}


@pytest.mark.asyncio
async def test_export_endpoint_langfuse_missing_keys_is_503(base_config, atom_home, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    base_config.observability.provider = "langfuse"
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/export", json={})
        assert r.status_code == 503
        assert "LANGFUSE" in r.json()["detail"]


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


def test_inline_rendering_is_fail_closed_allowlist():
    # Only the small allowlist (raster images, pdf, plain text/markdown/csv/json) renders inline on
    # direct navigation. Active content — html, the whole xml family, svg — AND anything unknown or
    # novel must download, so the guard can never silently fail open on a type nobody enumerated.
    for mt in ("text/html", "image/svg+xml", "application/xhtml+xml", "application/mathml+xml",
               "application/xml", "text/xml", "application/rss+xml", "application/atom+xml",
               "application/javascript", "application/x-shockwave-flash", "application/octet-stream"):
        assert _is_inline_unsafe(mt), mt
    for mt in ("text/plain", "text/markdown", "text/csv", "application/json", "application/pdf",
               "image/png", "image/jpeg", "image/webp", "image/avif"):
        assert not _is_inline_unsafe(mt), mt


def test_content_disposition_is_header_injection_safe():
    # A double-quote, backslash, or CR/LF in an artifact name must never break the quoted-string
    # or inject a second header line; the exact name survives in the percent-encoded filename*.
    name = 'a"\\\r\nb résumé.svg'
    cd = _content_disposition(name)
    assert "\r" not in cd and "\n" not in cd                       # no header/line injection
    assert cd.startswith('attachment; filename="ab rsum.svg"')     # quote/backslash/ctrl/non-ascii stripped from fallback
    assert cd.endswith("filename*=UTF-8''" + quote(name, safe=""))  # exact name preserved, percent-encoded


def _seed_filewf(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "docwf.yaml").write_text(
        "name: docwf\n"
        "inputs:\n"
        "  - name: doc\n    type: file\n    required: true\n"
        "  - name: extra\n    type: file\n    required: false\n"
        "steps:\n  - title: Read\n    tasks:\n      - id: t1\n        prompt: \"summarize {{ doc }}\"\n"
    )


def _reader_provider(td, sd, wf):
    return make_prepared([
        AIMessage(content="", tool_calls=[{
            "name": "read_file",
            "args": {"description": "r", "path": "/mnt/user-data/uploads/doc.txt"},
            "id": "c1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])


@pytest.mark.asyncio
async def test_multipart_upload_lands_and_run_completes(base_config, atom_home):
    _seed_filewf(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_reader_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post(
            "/api/runs",
            data={"workflow": "docwf", "inputs": "{}"},
            files={"doc": ("report.txt", b"the tide returns\n", "text/plain")},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        manifest = await _poll(client, run_id)
        assert manifest["status"] == "complete"
        assert manifest["inputs"]["doc"] == "/mnt/user-data/uploads/doc.txt"
        assert (engine.store.uploads_dir(run_id) / "doc.txt").read_bytes() == b"the tide returns\n"
        msgs = (await client.get(f"/api/runs/{run_id}/tasks/0/t1/messages")).json()
        assert any("the tide returns" in m["text"] for m in msgs if m["role"] == "tool")


@pytest.mark.asyncio
async def test_json_submit_still_works(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        assert r.status_code == 202 and r.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_multipart_undeclared_file_field_is_400(base_config, atom_home):
    _seed_filewf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"ghost": ("x.txt", b"x", "text/plain")})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_multipart_missing_required_file_is_422(base_config, atom_home):
    _seed_filewf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        # send only the optional 'extra' file -> required 'doc' absent
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"extra": ("notes.txt", b"x", "text/plain")})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_multipart_oversize_is_413(base_config, atom_home):
    _seed_filewf(atom_home)
    base_config.uploads.max_file_bytes = 5
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"doc": ("report.txt", b"way too big", "text/plain")})
        assert r.status_code == 413


@pytest.mark.asyncio
async def test_multipart_disallowed_extension_is_415(base_config, atom_home):
    _seed_filewf(atom_home)
    base_config.uploads.allowed_extensions = ["pdf"]
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"doc": ("report.txt", b"hello", "text/plain")})
        assert r.status_code == 415


@pytest.mark.asyncio
async def test_multipart_duplicate_file_for_input_is_400(base_config, atom_home):
    _seed_filewf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post(
            "/api/runs",
            data={"workflow": "docwf", "inputs": "{}"},
            files=[("doc", ("a.txt", b"a", "text/plain")), ("doc", ("b.txt", b"b", "text/plain"))],
        )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_multipart_file_under_text_field_key_is_4xx(base_config, atom_home):
    _seed_filewf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        # a file uploaded under the 'workflow' form field must be a clean client error, not a 500
        r = await client.post("/api/runs", files={"workflow": ("x.txt", b"x", "text/plain")})
        assert 400 <= r.status_code < 500


@pytest.mark.asyncio
async def test_cancel_unknown_run_is_404(base_config, atom_home):
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs/ghost/cancel")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_finished_run_is_409(base_config, atom_home, monkeypatch):
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    monkeypatch.setattr(engine, "request_cancel",
                        lambda rid: {"run_id": rid, "status": "complete", "already": True})
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs/x/cancel")
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_cancel_running_run_maps_response(base_config, atom_home, monkeypatch):
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    monkeypatch.setattr(engine, "request_cancel",
                        lambda rid: {"run_id": rid, "status": "running", "cancel_requested": True})
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs/anyid/cancel")
        assert r.status_code == 200
        assert r.json() == {"run_id": "anyid", "status": "running", "cancel_requested": True}


@pytest.mark.asyncio
async def test_get_run_exposes_cancel_requested_field(base_config, atom_home):
    store = _seed_run(atom_home, "rgf")
    m = store.load("rgf")
    m.status = "complete"                      # terminal -> untouched by lifespan recover()/worker
    store.save(m)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/rgf")
        assert r.status_code == 200
        assert r.json()["cancel_requested"] is False


@pytest.mark.asyncio
async def test_get_workflows_includes_notes_enabled(base_config, atom_home):
    _seed_notes_wf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client_no_worker(app) as c:
        wfs = (await c.get("/api/workflows")).json()
    nw = next(w for w in wfs if w["name"] == "notewf")
    assert nw["notes_enabled"] is True


@pytest.mark.asyncio
async def test_delete_workflow_notes_clears_vault(base_config, atom_home):
    _seed_notes_wf(atom_home)
    from atom.notes import notes_root
    root = notes_root(str(atom_home), "notewf")
    (root / "pages").mkdir(parents=True)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client_no_worker(app) as c:
        r = await c.delete("/api/workflows/notewf/notes")
    assert r.status_code == 200
    assert r.json() == {"workflow": "notewf", "cleared": True}
    assert not root.exists()


@pytest.mark.asyncio
async def test_delete_workflow_notes_unknown_is_404(base_config, atom_home):
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client_no_worker(app) as c:
        r = await c.delete("/api/workflows/nope/notes")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_workflow_notes_409_when_active_run(base_config, atom_home):
    _seed_notes_wf(atom_home)
    from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
    store = RunStore(str(atom_home))
    m = RunManifest(
        run_id="ar1", workflow="notewf", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("ar1")),
        steps=[StepState(index=0, title="S", tasks=[TaskState(id="t1", thread_id="ar1:s0:t1")])],
    )
    m.status = "running"
    store.create(m)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client_no_worker(app) as c:
        r = await c.delete("/api/workflows/notewf/notes")
    assert r.status_code == 409
