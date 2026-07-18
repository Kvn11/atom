"""Run manifest + on-disk store for workflow runs (single-writer, atomic saves)."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from atom.messages import message_text
from atom.sandbox.paths import atom_home
from atom.workflow.uploads import stored_name, virtual_upload_path


_ACTIVE = ("pending", "queued", "running")


def _is_safe_run_id(run_id: str) -> bool:
    """A run_id must be a single, non-parent path segment so run-scoped paths can't escape runs_dir.

    run_ids are internally generated (``uuid4().hex[:12]``); rejecting any value with a path
    separator, a parent/self reference, a NUL, or that is empty closes traversal at the one
    path-construction chokepoint (``run_dir``) for every run-scoped route and the CLI — e.g. a
    crafted ``"../runs/<other>"`` would otherwise collapse back onto another run's files.
    """
    return (
        bool(run_id)
        and run_id not in (".", "..")
        and not any(ch in run_id for ch in ("/", "\\", "\x00"))
    )


class ArtifactRef(BaseModel):
    name: str            # display name (basename, possibly disambiguated)
    path: str            # original virtual path as presented
    rel: str             # path relative to runs/<run_id>/artifacts/, used for serving
    size: int            # bytes


class TaskState(BaseModel):
    id: str
    thread_id: str
    model: Optional[str] = None
    thinking: Optional[Union[str, int]] = None
    status: str = "pending"            # pending | running | succeeded | failed
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class StepState(BaseModel):
    index: int
    title: str
    status: str = "pending"            # pending | running | complete | failed
    tasks: list[TaskState] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"            # pending | queued | running | complete | halted | cancelled
    created_at: str
    enqueued_at: Optional[str] = None  # microsecond-precision; primary FIFO sort key
    ended_at: Optional[str] = None
    workspace_path: str
    uploads_path: Optional[str] = None
    steps: list[StepState] = Field(default_factory=list)


class RunSummary(BaseModel):
    run_id: str
    workflow: str
    status: str
    created_at: str
    enqueued_at: Optional[str] = None
    ended_at: Optional[str] = None
    steps_total: int
    steps_done: int
    tasks_total: int
    tasks_done: int
    current_step: Optional[str] = None


def summarize(manifest: RunManifest) -> RunSummary:
    tasks = [t for s in manifest.steps for t in s.tasks]
    return RunSummary(
        run_id=manifest.run_id, workflow=manifest.workflow, status=manifest.status,
        created_at=manifest.created_at, enqueued_at=manifest.enqueued_at, ended_at=manifest.ended_at,
        steps_total=len(manifest.steps),
        steps_done=sum(1 for s in manifest.steps if s.status == "complete"),
        tasks_total=len(tasks),
        tasks_done=sum(1 for t in tasks if t.status == "succeeded"),
        current_step=next((s.title for s in manifest.steps if s.status != "complete"), None),
    )


def serialize_messages(messages: list) -> list[dict]:
    """Flatten LangChain messages to a UI-friendly list of dicts.

    The opening prompt of a workflow task is a ``HumanMessage`` only because chat providers
    require a human/user turn to start a conversation — no human authored it; the automated
    workflow did. Relabel that first human turn as ``"task"`` so the transcript is accurate.
    Injected mid-turn human notes (skill activations, view-image blocks) keep ``"human"``.
    """
    out: list[dict] = []
    opening_relabeled = False
    for m in messages:
        role = getattr(m, "type", m.__class__.__name__.replace("Message", "").lower())
        if role == "human" and not opening_relabeled:
            role = "task"
            opening_relabeled = True
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

    @property
    def queue_dir(self) -> Path:
        return self.home / "workflows" / "queue"

    def run_dir(self, run_id: str) -> Path:
        if not _is_safe_run_id(run_id):
            raise ValueError(f"unsafe run_id: {run_id!r}")   # hard wall for every path derived from run_dir
        return self.runs_dir / run_id

    def workspace_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def uploads_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "uploads"

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def exports_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "exports"

    def task_export_path(self, run_id: str, step_index: int, task_id: str) -> Path:
        """Per-task trace export; mirrors the chats/ naming (s<step>__<task>.json)."""
        return self.exports_dir(run_id) / f"s{step_index}__{task_id}.json"

    def export_path(self, run_id: str) -> Path:
        """Whole-run trace export location (task exports use task_export_path)."""
        return self.run_dir(run_id) / "export.json"

    def _manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def cancel_marker_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "cancel.request"

    def write_cancel_marker(self, run_id: str, when: str) -> None:
        p = self.cancel_marker_path(run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"requested_at": when}), encoding="utf-8")

    def cancel_requested(self, run_id: str) -> bool:
        if not _is_safe_run_id(run_id):
            return False
        return self.cancel_marker_path(run_id).exists()

    def clear_cancel_marker(self, run_id: str) -> None:
        if not _is_safe_run_id(run_id):
            return
        self.cancel_marker_path(run_id).unlink(missing_ok=True)

    def create(self, manifest: RunManifest) -> RunManifest:
        self.workspace_dir(manifest.run_id).mkdir(parents=True, exist_ok=True)
        self.uploads_dir(manifest.run_id).mkdir(parents=True, exist_ok=True)
        (self.run_dir(manifest.run_id) / "chats").mkdir(parents=True, exist_ok=True)
        self.save(manifest)
        return manifest

    def save_upload(self, run_id: str, input_name: str, original_filename: str, data: bytes) -> str:
        """Write an uploaded file's bytes into the run's uploads dir and return its virtual path.

        The on-disk name comes from uploads.stored_name(input_name, ...) so it is deterministic
        and equals uploads.virtual_upload_path (what the caller stores in RunManifest.inputs).
        """
        d = self.uploads_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / stored_name(input_name, original_filename)).write_bytes(data)
        return virtual_upload_path(input_name, original_filename)

    def _summary_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "summary.json"

    def save(self, manifest: RunManifest) -> None:
        path = self._manifest_path(manifest.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("run.json.tmp")
        tmp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)          # atomic on POSIX; run.json is authoritative
        sp = self._summary_path(manifest.run_id)
        stmp = sp.with_name("summary.json.tmp")
        stmp.write_text(summarize(manifest).model_dump_json(indent=2), encoding="utf-8")
        os.replace(stmp, sp)           # cheap cache for list_summaries

    def load(self, run_id: str) -> RunManifest:
        if not _is_safe_run_id(run_id):
            raise FileNotFoundError(run_id)   # an unsafe id can't name a real run — surface as "not found"
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
        if not _is_safe_run_id(run_id):
            return None
        p = self.chat_path(run_id, step_index, task_id)
        return json.loads(p.read_text("utf-8")) if p.exists() else None

    def capture_artifacts(
        self, run_id: str, step_index: int, task_id: str, presented: list[dict]
    ) -> list[ArtifactRef]:
        """Copy each presented file into artifacts/s<i>__<task>/ (immutable snapshot).

        Best-effort: a missing/unreadable source is skipped, never raised. Basename
        collisions within one task are disambiguated (name.md -> name-1.md).
        """
        refs: list[ArtifactRef] = []
        if not presented:
            return refs
        dest_dir = self.artifacts_dir(run_id) / f"s{step_index}__{task_id}"
        used: set[str] = set()
        for item in presented:
            physical = item.get("physical")
            virtual = item.get("path") or physical or ""
            if not physical:
                continue
            src = Path(physical)
            try:
                if not src.is_file():
                    continue
                base = Path(virtual).name or src.name
                name = base
                i = 1
                while name in used:
                    stem, dot, ext = base.partition(".")
                    name = f"{stem}-{i}{dot}{ext}" if dot else f"{base}-{i}"
                    i += 1
                used.add(name)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / name
                shutil.copyfile(src, dest)
                refs.append(ArtifactRef(
                    name=name, path=virtual,
                    rel=f"s{step_index}__{task_id}/{name}", size=dest.stat().st_size,
                ))
            except OSError:
                continue
        return refs

    def artifact_path(self, run_id: str, rel: str) -> Optional[Path]:
        if not _is_safe_run_id(run_id):
            return None
        base = self.artifacts_dir(run_id).resolve()
        target = (base / rel).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            return None
        return target

    def _read_summary(self, run_dir: Path) -> Optional["RunSummary"]:
        sp = run_dir / "summary.json"
        if sp.exists():
            try:
                return RunSummary.model_validate_json(sp.read_text("utf-8"))
            except Exception:  # noqa: BLE001 — corrupt cache; fall back to the manifest
                pass
        mp = run_dir / "run.json"
        if mp.exists():
            try:
                return summarize(RunManifest.model_validate_json(mp.read_text("utf-8")))
            except Exception:  # noqa: BLE001
                return None
        return None

    def list_summaries(self, status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        offset = max(0, offset)
        limit = max(0, limit)
        empty = {"items": [], "total": 0, "counts": {"active": 0, "complete": 0, "halted": 0, "cancelled": 0}}
        if not self.runs_dir.is_dir():
            return empty
        summaries = self._scan_summaries()
        counts = {"active": 0, "complete": 0, "halted": 0, "cancelled": 0}
        for s in summaries:
            if s.status in _ACTIVE:
                counts["active"] += 1
            elif s.status in counts:
                counts[s.status] += 1
        if status and status != "all":
            if status == "active":
                summaries = [s for s in summaries if s.status in _ACTIVE]
            else:
                summaries = [s for s in summaries if s.status == status]
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        total = len(summaries)
        page = summaries[offset:offset + limit]
        return {"items": [s.model_dump() for s in page], "total": total, "counts": counts}

    def _scan_summaries(self) -> list["RunSummary"]:
        if not self.runs_dir.is_dir():
            return []
        out: list[RunSummary] = []
        for d in self.runs_dir.iterdir():
            s = self._read_summary(d)
            if s is not None:
                out.append(s)
        return out

    def queued_run_ids(self) -> list[str]:
        q = [s for s in self._scan_summaries() if s.status == "queued"]
        q.sort(key=lambda s: (s.enqueued_at or s.created_at, s.run_id))
        return [s.run_id for s in q]

    def interrupted_run_ids(self) -> list[str]:
        return [s.run_id for s in self._scan_summaries() if s.status in ("pending", "running")]
