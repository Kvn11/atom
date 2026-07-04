"""FastAPI app exposing the workflow engine as an automation-first REST API (+ static UI).

Automation flow: POST /api/runs (submit) -> poll GET /api/runs/{id} -> GET .../artifacts.
"""
from __future__ import annotations

import datetime
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from atom.api.models import RunRequest
from atom.config import load_config
from atom.config.schema import AtomConfig
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import MissingInputError, list_workflows, load_workflow

# atom-ui/dist lives at repo root: src/atom/api/app.py -> parents[3] == repo root.
_UI_DIST = Path(__file__).resolve().parents[3] / "atom-ui" / "dist"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def create_app(cfg: AtomConfig | None = None, engine: WorkflowEngine | None = None) -> FastAPI:
    cfg = cfg or load_config()
    engine = engine or WorkflowEngine(cfg)
    store = engine.store
    app = FastAPI(title="atom workflows")
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

    @app.post("/api/runs", status_code=202)
    async def submit_run(req: RunRequest) -> dict:
        # MUST be async: engine.launch() calls asyncio.create_task, which needs the running
        # event loop. A sync endpoint would run in a threadpool with no loop and raise.
        try:
            wf = load_workflow(req.workflow, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{req.workflow}' not found")
        run_id = uuid.uuid4().hex[:12]
        try:
            engine.create_run(wf, req.inputs, run_id, _now())
        except MissingInputError as exc:
            raise HTTPException(422, str(exc))
        engine.launch(run_id)
        return {"run_id": run_id, "status": "pending"}

    @app.get("/api/runs")
    def get_runs() -> list:
        return [m.model_dump() for m in store.list()]

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

    @app.get("/api/runs/{run_id}/artifacts")
    def get_artifacts(run_id: str) -> list:
        ws = store.workspace_dir(run_id)
        if not ws.is_dir():
            raise HTTPException(404, "run not found")
        out = []
        for p in sorted(ws.rglob("*")):
            if p.is_file():
                st = p.stat()
                out.append({"path": str(p.relative_to(ws)), "size": st.st_size,
                            "modified": st.st_mtime})
        return out

    @app.get("/api/runs/{run_id}/artifacts/{path:path}", response_class=PlainTextResponse)
    def get_artifact(run_id: str, path: str) -> str:
        ws = store.workspace_dir(run_id).resolve()
        target = (ws / path).resolve()
        if target != ws and not str(target).startswith(str(ws) + "/"):
            raise HTTPException(404, "artifact not found")
        if not target.is_file():
            raise HTTPException(404, "artifact not found")
        return target.read_text(encoding="utf-8", errors="replace")

    if _UI_DIST.is_dir():  # serve the built SPA when present (prod); tests hit /api only
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")

    return app
