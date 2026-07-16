"""FastAPI app exposing the workflow engine as an automation-first REST API (+ static UI).

Automation flow: POST /api/runs (submit) -> poll GET /api/runs/{id} -> GET .../artifacts.
"""
from __future__ import annotations

import datetime
import json
import mimetypes
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile

from atom.api.models import ExportRequest, RunRequest
from atom.config import load_config
from atom.config.schema import AtomConfig
from atom.workflow.engine import WorkflowEngine
from atom.workflow.events import channel_key
from atom.workflow.schema import MissingInputError, list_workflows, load_workflow
from atom.workflow.uploads import (
    UploadTooLarge, UploadTypeNotAllowed, check_extension, check_size, virtual_upload_path,
)

# atom-ui/dist lives at repo root: src/atom/api/app.py -> parents[3] == repo root.
_UI_DIST = Path(__file__).resolve().parents[3] / "atom-ui" / "dist"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# Media types safe to render inline on direct navigation to a raw artifact URL. Everything else —
# html, the whole ``*+xml`` family (xhtml/svg/mathml/atom/rss…), scripts, and unknown/mislabeled
# bytes — is forced to download, so a novel active type can't execute inline without a code change
# (fail closed). The SPA is unaffected either way: it reads text via fetch().text() and images via
# <img>, neither of which honors Content-Disposition; only the PDF <iframe> honors it, so
# application/pdf must stay inline-safe. Raster images are inert, so any ``image/*`` except SVG is safe.
_INLINE_SAFE = {"application/pdf", "text/plain", "text/markdown", "text/csv", "application/json"}


def _is_inline_unsafe(media_type: str) -> bool:
    if media_type == "image/svg+xml":              # SVG can carry <script> — never inline
        return True
    return not (media_type.startswith("image/") or media_type in _INLINE_SAFE)


def _content_disposition(name: str) -> str:
    """Build an RFC 6266 ``attachment`` header value that is injection-safe for any filename.

    The ``filename="..."`` fallback is stripped of quotes, backslashes and non-printable bytes (so a
    name containing ``"`` or CR/LF cannot break out of the quoted-string or inject a header), and a
    percent-encoded ``filename*`` carries the exact (possibly non-ASCII) name for capable clients.
    """
    ascii_fallback = "".join(c for c in name if c.isascii() and c.isprintable() and c not in '"\\') or "download"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def create_app(cfg: AtomConfig | None = None, engine: WorkflowEngine | None = None) -> FastAPI:
    cfg = cfg or load_config()
    engine = engine or WorkflowEngine(cfg)
    store = engine.store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holds = engine.lease.acquire()          # lease first: recover + drain only if we own it
        try:
            if holds:
                engine.recover()
                engine.start_worker()
            yield
        finally:
            if holds:
                await engine.stop_worker()
                engine.lease.release()

    app = FastAPI(title="atom workflows", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/api/workflows")
    def get_workflows() -> list:
        return [
            {"name": w.name, "description": w.description,
             "inputs": [i.model_dump() for i in w.inputs]}
            for w in list_workflows(cfg.home)
        ]

    @app.get("/api/workflows/{name}")
    def get_workflow(name: str) -> dict:
        try:
            return load_workflow(name, cfg.home).model_dump()
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{name}' not found")

    def _create_and_enqueue(wf, inputs: dict, files: dict) -> dict:
        # files: {input_name: (original_filename, data_bytes)}
        run_id = uuid.uuid4().hex[:12]
        merged = dict(inputs)
        for name, (filename, _data) in files.items():
            merged[name] = virtual_upload_path(name, filename)
        try:
            engine.create_run(wf, merged, run_id, _now())
        except MissingInputError as exc:
            raise HTTPException(422, str(exc))
        for name, (filename, data) in files.items():
            store.save_upload(run_id, name, filename, data)
        engine.enqueue(run_id)
        return {"run_id": run_id, "status": "queued"}

    async def _submit_multipart(request: Request) -> dict:
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 — malformed/oversized multipart body -> 400, not 500
            raise HTTPException(400, f"malformed multipart body: {type(exc).__name__}") from exc
        workflow_name = form.get("workflow")
        if not isinstance(workflow_name, str) or not workflow_name:
            raise HTTPException(422, "missing or invalid 'workflow' field")
        raw_inputs = form.get("inputs")
        if raw_inputs is not None and not isinstance(raw_inputs, str):
            raise HTTPException(422, "'inputs' must be a JSON string field")
        try:
            text_inputs = json.loads(raw_inputs) if raw_inputs else {}
        except json.JSONDecodeError:
            raise HTTPException(422, "'inputs' must be a JSON object")
        if not isinstance(text_inputs, dict):
            raise HTTPException(422, "'inputs' must be a JSON object")
        try:
            wf = load_workflow(workflow_name, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{workflow_name}' not found")
        file_input_names = {i.name for i in wf.inputs if i.type == "file"}

        files: dict = {}
        for key, value in form.multi_items():
            if not isinstance(value, UploadFile):
                continue
            if key not in file_input_names:
                raise HTTPException(400, f"'{key}' is not a declared file input of workflow '{workflow_name}'")
            if key in files:
                raise HTTPException(400, f"multiple files supplied for input '{key}'")
            if len(files) + 1 > cfg.uploads.max_files_per_run:
                raise HTTPException(413, f"too many files (> {cfg.uploads.max_files_per_run})")
            try:
                # Reject by size/type BEFORE reading the whole part into memory. Starlette
                # populates UploadFile.size during multipart parsing; the post-read len() check
                # is defense-in-depth for the rare case size is unavailable.
                if value.size is not None:
                    check_size(value.size, cfg.uploads.max_file_bytes)
                check_extension(value.filename or "", cfg.uploads.allowed_extensions)
                data = await value.read()
                check_size(len(data), cfg.uploads.max_file_bytes)
            except UploadTooLarge as exc:
                raise HTTPException(413, str(exc))
            except UploadTypeNotAllowed as exc:
                raise HTTPException(415, str(exc))
            files[key] = (value.filename or key, data)
        return _create_and_enqueue(wf, text_inputs, files)

    @app.post("/api/runs", status_code=202)
    async def submit_run(request: Request) -> dict:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            return await _submit_multipart(request)
        # JSON path (backward-compatible).
        try:
            body = await request.json()
            req = RunRequest.model_validate(body)
        except Exception:  # noqa: BLE001 — malformed JSON body -> 422 like FastAPI's auto-validation
            raise HTTPException(422, "invalid JSON body for RunRequest")
        try:
            wf = load_workflow(req.workflow, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{req.workflow}' not found")
        return _create_and_enqueue(wf, req.inputs, {})

    @app.get("/api/runs")
    def get_runs(status: str = "all", limit: int = 50, offset: int = 0) -> dict:
        return store.list_summaries(status=status, limit=limit, offset=offset)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return store.load(run_id).model_dump()
        except FileNotFoundError:
            raise HTTPException(404, "run not found")

    @app.get("/api/runs/{run_id}/tasks/{step}/{task_id}/messages")
    def get_messages(run_id: str, step: int, task_id: str) -> list:
        chat = store.load_chat(run_id, step, task_id)
        if chat is None:
            raise HTTPException(404, "no chat yet")
        return chat

    @app.get("/api/runs/{run_id}/tasks/{step}/{task_id}/stream")
    async def stream_task(run_id: str, step: int, task_id: str):
        """Server-Sent Events: live thinking/text/tool deltas for one task. Emits a `snapshot`
        (catch-up) then live frames + `ping` heartbeats, ending with `done` (or `error`) on task
        completion — at which point the client refetches the authoritative .../messages snapshot."""
        if not cfg.streaming.enabled:
            raise HTTPException(404, "streaming disabled")
        key = channel_key(run_id, step, task_id)

        async def gen():
            async for ev in engine.bus.stream(key):
                if ev.get("type") == "ping":
                    yield ": ping\n\n"                       # SSE comment (keep-alive)
                    continue
                yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/api/runs/{run_id}/export")
    def export_traces(run_id: str, body: ExportRequest | None = None) -> dict:
        """Download this run's LangSmith trace(s) to disk. With step+task -> one task; else the run.

        Sync def on purpose: the exporter polls LangSmith with blocking sleeps, so FastAPI runs it
        in a threadpool and never stalls the event loop / queue worker.
        """
        provider = cfg.observability.provider
        if provider is None:
            provider = "langsmith" if cfg.observability.enabled else "none"
        if provider == "langfuse":
            from atom.observability import langfuse_export as export_mod
            if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
                raise HTTPException(503, "export not configured: set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY")
            proj = None
        else:
            from atom.observability import export as export_mod
            proj = cfg.observability.project
            if not proj:
                raise HTTPException(503, "export not configured: set observability.project")
        body = body or ExportRequest()
        try:
            if body.step is not None and body.task is not None:
                res = export_mod.export_task(cfg.home, run_id, body.step, body.task, project=proj)
            else:
                res = export_mod.export_run(cfg.home, run_id, project=proj)
        except FileNotFoundError:
            raise HTTPException(404, "run not found")
        except KeyError as e:                                 # unknown step/task
            raise HTTPException(404, e.args[0] if e.args else "not found")
        except RuntimeError as e:                             # missing LANGSMITH_API_KEY — server config, not the client's fault
            raise HTTPException(503, str(e))
        except ValueError as e:                               # task not terminal yet — client can retry once it completes
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001 — LangSmith/network failure
            raise HTTPException(502, f"export failed: {type(e).__name__}: {e}")
        return {
            "run_id": res.run_id,
            "scope": "task" if res.task_id else "run",
            "task_id": res.task_id,
            "path": res.path,
            "complete": res.complete,
            "expected_roots": res.expected_roots,
            "fetched_roots": res.fetched_roots,
        }

    @app.get("/api/runs/{run_id}/artifacts")
    def get_artifacts(run_id: str) -> list:
        try:
            m = store.load(run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found")
        out = []
        for s in m.steps:
            for t in s.tasks:
                for a in t.artifacts:
                    out.append({**a.model_dump(), "step": s.index, "task": t.id})
        return out

    @app.get("/api/runs/{run_id}/artifacts/{rel:path}")
    def get_artifact(run_id: str, rel: str):
        target = store.artifact_path(run_id, rel)
        if target is None or not target.is_file():
            raise HTTPException(404, "artifact not found")
        media_type = mimetypes.guess_type(target.name)[0] or "text/plain"
        # Script-capable types must not render inline on direct navigation (defense-in-depth).
        headers = None
        if _is_inline_unsafe(media_type):
            headers = {"Content-Disposition": _content_disposition(target.name)}
        return FileResponse(target, media_type=media_type, headers=headers)

    if _UI_DIST.is_dir():  # serve the built SPA when present (prod); tests hit /api only
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")

    return app
