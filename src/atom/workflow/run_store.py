"""Run manifest + on-disk store for workflow runs (single-writer, atomic saves)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from atom.messages import message_text
from atom.sandbox.paths import atom_home


class TaskState(BaseModel):
    id: str
    thread_id: str
    model: Optional[str] = None
    thinking: Optional[Union[str, int]] = None
    status: str = "pending"            # pending | running | succeeded | failed
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None


class StepState(BaseModel):
    index: int
    title: str
    status: str = "pending"            # pending | running | complete | failed
    tasks: list[TaskState] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"            # pending | running | complete | halted
    created_at: str
    ended_at: Optional[str] = None
    workspace_path: str
    steps: list[StepState] = Field(default_factory=list)


def serialize_messages(messages: list) -> list[dict]:
    """Flatten LangChain messages to a UI-friendly list of dicts."""
    out: list[dict] = []
    for m in messages:
        role = getattr(m, "type", m.__class__.__name__.replace("Message", "").lower())
        entry: dict = {"role": role, "text": message_text(m)}
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            entry["tool_calls"] = [{"name": c.get("name"), "args": c.get("args", {})} for c in tcs]
        name = getattr(m, "name", None)
        if name:
            entry["name"] = name
        out.append(entry)
    return out


class RunStore:
    """File-backed store for run manifests + chat snapshots under $ATOM_HOME/workflows/runs."""

    def __init__(self, home: str | None = None):
        self.home = atom_home(home)

    @property
    def runs_dir(self) -> Path:
        return self.home / "workflows" / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def workspace_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def _manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def create(self, manifest: RunManifest) -> RunManifest:
        self.workspace_dir(manifest.run_id).mkdir(parents=True, exist_ok=True)
        (self.run_dir(manifest.run_id) / "chats").mkdir(parents=True, exist_ok=True)
        self.save(manifest)
        return manifest

    def save(self, manifest: RunManifest) -> None:
        path = self._manifest_path(manifest.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("run.json.tmp")
        tmp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)          # atomic on POSIX

    def load(self, run_id: str) -> RunManifest:
        return RunManifest.model_validate_json(self._manifest_path(run_id).read_text("utf-8"))

    def list(self) -> list[RunManifest]:
        if not self.runs_dir.is_dir():
            return []
        out: list[RunManifest] = []
        for d in self.runs_dir.iterdir():
            mp = d / "run.json"
            if mp.exists():
                try:
                    out.append(RunManifest.model_validate_json(mp.read_text("utf-8")))
                except Exception:  # noqa: BLE001
                    continue
        return sorted(out, key=lambda m: m.created_at, reverse=True)

    def chat_path(self, run_id: str, step_index: int, task_id: str) -> Path:
        return self.run_dir(run_id) / "chats" / f"s{step_index}__{task_id}.json"

    def save_chat(self, run_id: str, step_index: int, task_id: str, messages: list[dict]) -> None:
        p = self.chat_path(run_id, step_index, task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(messages, indent=2), encoding="utf-8")

    def load_chat(self, run_id: str, step_index: int, task_id: str) -> Optional[list[dict]]:
        p = self.chat_path(run_id, step_index, task_id)
        return json.loads(p.read_text("utf-8")) if p.exists() else None
