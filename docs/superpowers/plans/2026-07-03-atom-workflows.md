# atom Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a workflow layer to atom — ordered Steps of parallel Tasks (each Task a lead-agent `run_agent` call) sharing one workspace, driven by an in-process engine, exposed through an automation-first FastAPI API and a React test UI, and traced per task in LangSmith.

**Architecture:** A Task is a `run_agent` call bound (via existing "existing-workspace" mode) to a per-run shared directory, with its own `thread_id`. Steps run sequentially; tasks within a step run concurrently (`asyncio.gather` + semaphore). A step progresses only if **all** its tasks succeed, otherwise the run **halts**. A JSON manifest per run (single-writer, atomic) is the source of truth; per-task chat snapshots are persisted on completion. FastAPI wraps the engine; a Vite/React SPA consumes the API.

**Tech Stack:** Python 3.11+, LangChain/LangGraph v1, pydantic v2, PyYAML, Jinja2, FastAPI, uvicorn, httpx (tests), React + Vite + TypeScript (UI). LangSmith is env-var-only (no hard dependency).

## Global Constraints

- **Branch:** all work on `feat/workflows` (already checked out). Commit after every green step.
- **TDD (Iron Law):** no production code without a failing test first — for every Python module (schema, status, run_store, observability, engine, runtime trace arg, API, CLI). The React `atom-ui/` app is the **only** exception (manual/e2e surface).
- **Run tests with the venv active:** `source .venv/bin/activate` then `python -m pytest`.
- **Python ≥ 3.11**, dependency lines pinned `<2` as in `pyproject.toml`. New deps: `fastapi`, `uvicorn[standard]` (runtime); `httpx` (dev). LangSmith stays env-only — never add a hard `langsmith` dependency.
- **Naming:** verb_noun functions, snake_case modules, matching existing `src/atom/` style.
- **Timestamps** are stamped at the API/CLI boundary and passed into the engine/store, so status logic stays pure. Use `datetime.datetime.now().isoformat(timespec="seconds")`.
- **Status vocabularies (exact strings):** run = `pending|running|complete|halted`; step = `pending|running|complete|failed`; task = `pending|running|succeeded|failed`.
- **Halt semantics:** a step is `complete` only if every task `succeeded`; any task `failed` ⇒ step `failed` ⇒ run `halted`, and later steps never run.
- **Step hand-off is the shared workspace only** — later tasks read files earlier tasks wrote. No output plumbing.
- **thread_id format:** `f"{run_id}:s{step_index}:{task_id}"`.
- **Run directory:** `$ATOM_HOME/workflows/runs/<run_id>/` containing `workspace/`, `run.json`, `chats/s<step>__<task_id>.json`.
- **Message serialization shape:** `{"role", "text", optional "tool_calls":[{"name","args"}], optional "name"}`.
- **max_parallel** default **4**; per-task timeout default **1800s** (config `workflow.max_parallel`, `workflow.task_timeout_seconds`).

---

## File structure

```
src/atom/workflow/
    __init__.py          # package marker
    schema.py            # WorkflowDef/StepDef/TaskDef/InputDef, load/list, resolve_inputs, render_task_prompt
    status.py            # compute_step_status / compute_run_status (pure)
    run_store.py         # RunManifest/StepState/TaskState, RunStore (atomic), serialize_messages
    observability.py     # build_trace(...)
    engine.py            # WorkflowEngine: create_run / execute / launch
src/atom/api/
    __init__.py
    models.py            # RunRequest (+ response shapes)
    app.py               # create_app(cfg, engine) — FastAPI routes + CORS + static UI mount
src/atom/runtime.py      # + trace arg + _apply_trace helper
src/atom/config/schema.py# + WorkflowConfig on AtomConfig
src/atom/cli.py          # + `workflow list|run|runs` + `serve`
workflows/parallel-poems.yaml   # shipped example
atom-ui/                 # Vite React TS SPA (WorkflowList / RunForm / RunView + api client)
tests/
    test_workflow_schema.py
    test_workflow_status.py
    test_workflow_run_store.py
    test_workflow_observability.py
    test_runtime_trace.py
    test_workflow_engine.py
    test_workflow_api.py
    test_workflow_cli.py
```

Dependency order: **1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11**. Tasks 2–6 are independent of each other (all depend only on 1's package dir being present, or nothing).

---

### Task 1: Workflow schema, loader, input validation, templating

**Files:**
- Create: `src/atom/workflow/__init__.py` (empty)
- Create: `src/atom/workflow/schema.py`
- Test: `tests/test_workflow_schema.py`

**Interfaces:**
- Produces:
  - `InputDef(name:str, required:bool=False, description:str|None=None, default:str|None=None)`
  - `TaskDef(id:str|None=None, prompt:str, model:str|None=None, thinking:str|int|None=None)`
  - `StepDef(title:str, description:str|None=None, tasks:list[TaskDef])`
  - `WorkflowDef(name:str, description:str|None=None, inputs:list[InputDef], steps:list[StepDef])`
  - `MissingInputError(ValueError)`
  - `workflows_dir(home:str|None=None) -> Path`
  - `load_workflow(name:str, home:str|None=None) -> WorkflowDef`
  - `list_workflows(home:str|None=None) -> list[WorkflowDef]`
  - `resolve_inputs(workflow:WorkflowDef, provided:dict) -> dict`
  - `render_task_prompt(task:TaskDef, inputs:dict) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workflow_schema.py
"""Workflow YAML schema, loading, input validation, and prompt templating."""
from __future__ import annotations

import pytest

from atom.workflow.schema import (
    MissingInputError, StepDef, TaskDef, WorkflowDef,
    list_workflows, load_workflow, render_task_prompt, resolve_inputs,
)


def _write(home, name, text):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(text)


DEMO = """
name: demo
description: A demo workflow.
inputs:
  - name: topic
    required: true
  - name: style
    required: false
    default: free verse
steps:
  - title: Draft
    tasks:
      - id: poet_a
        prompt: "Write about {{ topic }} in {{ style }}."
        model: haiku
        thinking: low
      - prompt: "Second poem about {{ inputs.topic }}."
"""


def test_load_workflow_parses_steps_and_defaults(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    assert wf.name == "demo"
    assert [s.title for s in wf.steps] == ["Draft"]
    # first task keeps its id; second gets an auto id.
    assert [t.id for t in wf.steps[0].tasks] == ["poet_a", "task_2"]
    assert wf.steps[0].tasks[0].model == "haiku"


def test_list_workflows_returns_all(atom_home):
    _write(atom_home, "demo", DEMO)
    names = {w.name for w in list_workflows(str(atom_home))}
    assert "demo" in names


def test_resolve_inputs_requires_required_and_fills_defaults(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    with pytest.raises(MissingInputError):
        resolve_inputs(wf, {})                       # topic missing
    resolved = resolve_inputs(wf, {"topic": "the sea"})
    assert resolved == {"topic": "the sea", "style": "free verse"}


def test_render_task_prompt_templates_inputs(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    inputs = resolve_inputs(wf, {"topic": "rain", "style": "haiku"})
    assert render_task_prompt(wf.steps[0].tasks[0], inputs) == "Write about rain in haiku."


def test_duplicate_task_ids_rejected():
    with pytest.raises(Exception):
        StepDef(title="s", tasks=[TaskDef(id="x", prompt="a"), TaskDef(id="x", prompt="b")])


def test_empty_steps_rejected():
    with pytest.raises(Exception):
        WorkflowDef(name="w", steps=[])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_workflow_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.workflow'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/workflow/__init__.py
```
(empty file)

```python
# src/atom/workflow/schema.py
"""Workflow definitions (Steps x Tasks), YAML loading, input validation, prompt templating."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atom.prompts.render import apply_prompt_template
from atom.sandbox.paths import VIRTUAL_OUTPUTS, VIRTUAL_UPLOADS, VIRTUAL_WORKSPACE, atom_home


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class InputDef(_Base):
    name: str
    required: bool = False
    description: Optional[str] = None
    default: Optional[str] = None


class TaskDef(_Base):
    id: Optional[str] = None
    prompt: str
    model: Optional[str] = None
    thinking: Optional[Union[str, int]] = None


class StepDef(_Base):
    title: str
    description: Optional[str] = None
    tasks: list[TaskDef] = Field(default_factory=list)

    @field_validator("tasks")
    @classmethod
    def _non_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("a step must define at least one task")
        return v

    @model_validator(mode="after")
    def _assign_and_check_ids(self) -> "StepDef":
        seen: set[str] = set()
        for i, t in enumerate(self.tasks):
            if not t.id:
                t.id = f"task_{i + 1}"
            if t.id in seen:
                raise ValueError(f"duplicate task id '{t.id}' in step '{self.title}'")
            seen.add(t.id)
        return self


class WorkflowDef(_Base):
    name: str
    description: Optional[str] = None
    inputs: list[InputDef] = Field(default_factory=list)
    steps: list[StepDef] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def _non_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("a workflow must define at least one step")
        return v


class MissingInputError(ValueError):
    """Raised when required workflow inputs are absent."""


def workflows_dir(home: str | None = None) -> Path:
    return atom_home(home) / "workflows"


def load_workflow(name: str, home: str | None = None) -> WorkflowDef:
    path = workflows_dir(home) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"workflow '{name}' not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return WorkflowDef.model_validate(data)


def list_workflows(home: str | None = None) -> list[WorkflowDef]:
    d = workflows_dir(home)
    if not d.is_dir():
        return []
    out: list[WorkflowDef] = []
    for p in sorted(d.glob("*.yaml")):
        try:
            out.append(WorkflowDef.model_validate(yaml.safe_load(p.read_text()) or {}))
        except Exception:  # noqa: BLE001 — skip malformed files in listings
            continue
    return out


def resolve_inputs(workflow: WorkflowDef, provided: dict) -> dict:
    provided = provided or {}
    resolved: dict = {}
    missing: list[str] = []
    for inp in workflow.inputs:
        if inp.name in provided and str(provided[inp.name]).strip() != "":
            resolved[inp.name] = provided[inp.name]
        elif inp.default is not None:
            resolved[inp.name] = inp.default
        elif inp.required:
            missing.append(inp.name)
        else:
            resolved[inp.name] = ""
    if missing:
        raise MissingInputError(f"missing required input(s): {', '.join(missing)}")
    return resolved


def render_task_prompt(task: TaskDef, inputs: dict) -> str:
    ctx = {
        **inputs,
        "inputs": inputs,
        "workspace": VIRTUAL_WORKSPACE,
        "uploads": VIRTUAL_UPLOADS,
        "outputs": VIRTUAL_OUTPUTS,
        "date": datetime.date.today().isoformat(),
    }
    return apply_prompt_template(task.prompt, ctx)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_schema.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/__init__.py src/atom/workflow/schema.py tests/test_workflow_schema.py
git commit -m "feat(workflow): schema, loader, input validation, prompt templating"
```

---

### Task 2: Pure status classification

**Files:**
- Create: `src/atom/workflow/status.py`
- Test: `tests/test_workflow_status.py`

**Interfaces:**
- Produces: `compute_step_status(task_statuses:list[str]) -> str`, `compute_run_status(step_statuses:list[str]) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workflow_status.py
"""Pure step/run status classification."""
from __future__ import annotations

from atom.workflow.status import compute_run_status, compute_step_status


def test_step_complete_only_when_all_succeeded():
    assert compute_step_status(["succeeded", "succeeded"]) == "complete"


def test_step_failed_when_any_failed():
    assert compute_step_status(["succeeded", "failed"]) == "failed"
    assert compute_step_status(["failed", "failed"]) == "failed"


def test_step_running_and_pending():
    assert compute_step_status(["running", "pending"]) == "running"
    assert compute_step_status(["pending", "pending"]) == "pending"
    assert compute_step_status([]) == "pending"


def test_run_halts_on_any_failed_step():
    assert compute_run_status(["complete", "failed"]) == "halted"


def test_run_complete_and_running():
    assert compute_run_status(["complete", "complete"]) == "complete"
    assert compute_run_status(["complete", "running"]) == "running"
    assert compute_run_status(["pending", "pending"]) == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_status.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.workflow.status'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/workflow/status.py
"""Pure step/run status classification for workflow runs."""
from __future__ import annotations


def compute_step_status(task_statuses: list[str]) -> str:
    """A step is complete only if every task succeeded; any failed ⇒ failed."""
    if not task_statuses:
        return "pending"
    if any(s in ("pending", "running") for s in task_statuses):
        return "pending" if all(s == "pending" for s in task_statuses) else "running"
    if all(s == "succeeded" for s in task_statuses):
        return "complete"
    return "failed"


def compute_run_status(step_statuses: list[str]) -> str:
    """The run halts if any step failed; completes only if every step completed."""
    if not step_statuses:
        return "pending"
    if any(s == "failed" for s in step_statuses):
        return "halted"
    if all(s == "complete" for s in step_statuses):
        return "complete"
    if all(s == "pending" for s in step_statuses):
        return "pending"
    return "running"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_status.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/status.py tests/test_workflow_status.py
git commit -m "feat(workflow): pure step/run status classification"
```

---

### Task 3: Run store — manifest, atomic persistence, chat snapshots

**Files:**
- Create: `src/atom/workflow/run_store.py`
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Produces:
  - `TaskState(id, thread_id, model=None, thinking=None, status="pending", started_at=None, ended_at=None, error=None)`
  - `StepState(index:int, title:str, status="pending", tasks:list[TaskState])`
  - `RunManifest(run_id, workflow, inputs, status="pending", created_at, ended_at=None, workspace_path, steps:list[StepState])`
  - `serialize_messages(messages:list) -> list[dict]`
  - `RunStore(home:str|None=None)` with `.runs_dir`, `.run_dir(id)`, `.workspace_dir(id)`, `.create(m)`, `.save(m)`, `.load(id)`, `.list()`, `.chat_path(id,step,task)`, `.save_chat(id,step,task,msgs)`, `.load_chat(id,step,task)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workflow_run_store.py
"""Run manifest persistence (atomic) and chat snapshots."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from atom.workflow.run_store import (
    RunManifest, RunStore, StepState, TaskState, serialize_messages,
)


def _manifest(run_id, ws):
    return RunManifest(
        run_id=run_id, workflow="demo", inputs={"topic": "x"},
        created_at="2026-07-03T00:00:00", workspace_path=str(ws),
        steps=[StepState(index=0, title="Draft",
                         tasks=[TaskState(id="t1", thread_id=f"{run_id}:s0:t1")])],
    )


def test_create_and_load_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    m = store.create(_manifest("r1", store.workspace_dir("r1")))
    assert store.workspace_dir("r1").is_dir()
    loaded = store.load("r1")
    assert loaded.run_id == "r1"
    assert loaded.steps[0].tasks[0].thread_id == "r1:s0:t1"


def test_save_is_atomic_no_tmp_left(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("r2", store.workspace_dir("r2")))
    m = store.load("r2")
    m.status = "running"
    store.save(m)
    assert store.load("r2").status == "running"
    leftovers = list(store.run_dir("r2").glob("*.tmp"))
    assert leftovers == []


def test_list_sorted_desc(atom_home):
    store = RunStore(str(atom_home))
    a = _manifest("ra", store.workspace_dir("ra")); a.created_at = "2026-07-01T00:00:00"
    b = _manifest("rb", store.workspace_dir("rb")); b.created_at = "2026-07-02T00:00:00"
    store.create(a); store.create(b)
    assert [m.run_id for m in store.list()] == ["rb", "ra"]


def test_chat_snapshot_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("r3", store.workspace_dir("r3")))
    assert store.load_chat("r3", 0, "t1") is None
    store.save_chat("r3", 0, "t1", [{"role": "ai", "text": "hi"}])
    assert store.load_chat("r3", 0, "t1") == [{"role": "ai", "text": "hi"}]


def test_serialize_messages_shape():
    msgs = [
        HumanMessage(content="do it"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "p"}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="c1", name="write_file"),
        AIMessage(content="done"),
    ]
    out = serialize_messages(msgs)
    assert out[0] == {"role": "human", "text": "do it"}
    assert out[1]["tool_calls"] == [{"name": "write_file", "args": {"path": "p"}}]
    assert out[2]["role"] == "tool" and out[2]["name"] == "write_file"
    assert out[3]["text"] == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_run_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.workflow.run_store'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/workflow/run_store.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_run_store.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(workflow): run manifest store (atomic) + chat snapshots"
```

---

### Task 4: Observability trace builder

**Files:**
- Create: `src/atom/workflow/observability.py`
- Test: `tests/test_workflow_observability.py`

**Interfaces:**
- Produces: `build_trace(*, workflow:str, run_id:str, step_index:int, step_title:str, task_id:str) -> dict` with keys `run_name`, `tags`, `metadata`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workflow_observability.py
"""LangSmith trace config builder."""
from __future__ import annotations

from atom.workflow.observability import build_trace


def test_build_trace_shape():
    t = build_trace(workflow="poems", run_id="r1", step_index=0, step_title="Draft", task_id="poet_a")
    assert t["run_name"] == "poems/Draft/poet_a"
    assert "atom-workflow" in t["tags"]
    assert "workflow:poems" in t["tags"] and "step:Draft" in t["tags"] and "task:poet_a" in t["tags"]
    assert t["metadata"] == {
        "workflow": "poems", "run_id": "r1", "step_index": 0,
        "step_title": "Draft", "task_id": "poet_a",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_observability.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.workflow.observability'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/workflow/observability.py
"""Build a LangSmith trace config (run_name/tags/metadata) for a workflow task.

LangSmith activates purely from env vars (LANGSMITH_TRACING / LANGSMITH_API_KEY /
LANGSMITH_PROJECT). When unset, this dict is harmless metadata on the run config.
"""
from __future__ import annotations


def build_trace(*, workflow: str, run_id: str, step_index: int, step_title: str, task_id: str) -> dict:
    return {
        "run_name": f"{workflow}/{step_title}/{task_id}",
        "tags": [
            "atom-workflow",
            f"workflow:{workflow}",
            f"step:{step_title}",
            f"task:{task_id}",
            f"run:{run_id}",
        ],
        "metadata": {
            "workflow": workflow,
            "run_id": run_id,
            "step_index": step_index,
            "step_title": step_title,
            "task_id": task_id,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_observability.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/observability.py tests/test_workflow_observability.py
git commit -m "feat(workflow): LangSmith trace builder"
```

---

### Task 5: `run_agent` trace argument

**Files:**
- Modify: `src/atom/runtime.py` (add `_apply_trace` helper; add `trace` param to `run_agent`; apply it to `run_config`)
- Test: `tests/test_runtime_trace.py`

**Interfaces:**
- Produces: `_apply_trace(run_config:dict, trace:dict|None) -> dict`; `run_agent(..., trace:dict|None=None)`.
- Consumes: existing `run_agent` from Task-independent runtime.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runtime_trace.py
"""run_agent trace config merge."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.runtime import _apply_trace, run_agent
from tests.conftest import make_prepared


def test_apply_trace_merges_keys():
    cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 100}
    out = _apply_trace(cfg, {"run_name": "wf/s/t", "tags": ["a"], "metadata": {"x": 1}})
    assert out["run_name"] == "wf/s/t"
    assert out["tags"] == ["a"]
    assert out["metadata"] == {"x": 1}


def test_apply_trace_none_is_noop():
    cfg = {"configurable": {}}
    assert _apply_trace(cfg, None) == {"configurable": {}}


@pytest.mark.asyncio
async def test_run_agent_accepts_trace(base_config):
    prepared = make_prepared([AIMessage(content="hello")])
    result = await run_agent(
        "hi", config=base_config, prepared=prepared,
        trace={"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}},
    )
    assert result.final_text == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runtime_trace.py -q`
Expected: FAIL — `ImportError: cannot import name '_apply_trace'`.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/runtime.py`, add this helper just above `run_agent`:

```python
def _apply_trace(run_config: dict, trace: dict | None) -> dict:
    """Merge LangSmith run_name/tags/metadata into a LangGraph run config (in place)."""
    if trace:
        for key in ("run_name", "tags", "metadata"):
            if trace.get(key) is not None:
                run_config[key] = trace[key]
    return run_config
```

Add `trace: dict | None = None,` to `run_agent`'s keyword parameters (e.g. right after `override_system_prompt`). Then change the run-config construction inside `run_agent` from:

```python
        run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
```
to:
```python
        run_config = _apply_trace(
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}, trace
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runtime_trace.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/atom/runtime.py tests/test_runtime_trace.py
git commit -m "feat(runtime): optional trace arg on run_agent (LangSmith run_name/tags/metadata)"
```

---

### Task 6: `WorkflowConfig` on `AtomConfig`

**Files:**
- Modify: `src/atom/config/schema.py` (add `WorkflowConfig`, add `workflow` field to `AtomConfig`)
- Test: `tests/test_workflow_config.py`

**Interfaces:**
- Produces: `WorkflowConfig(max_parallel:int=4, task_timeout_seconds:int=1800)`; `AtomConfig.workflow`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workflow_config.py
"""Workflow engine config defaults."""
from __future__ import annotations

from atom.config.schema import AtomConfig, WorkflowConfig


def test_workflow_config_defaults():
    cfg = AtomConfig()
    assert cfg.workflow.max_parallel == 4
    assert cfg.workflow.task_timeout_seconds == 1800


def test_workflow_config_override():
    wc = WorkflowConfig(max_parallel=2, task_timeout_seconds=60)
    assert wc.max_parallel == 2 and wc.task_timeout_seconds == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'WorkflowConfig'`.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/config/schema.py`, add after `LibraryConfig`:

```python
class WorkflowConfig(_Base):
    # Max tasks run concurrently within a single step; per-task wall-clock timeout.
    max_parallel: int = 4
    task_timeout_seconds: int = 1800
```

And add the field to `AtomConfig` (next to `library`):

```python
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_config.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/atom/config/schema.py tests/test_workflow_config.py
git commit -m "feat(config): WorkflowConfig (max_parallel, task_timeout_seconds)"
```

---

### Task 7: Workflow engine

**Files:**
- Create: `src/atom/workflow/engine.py`
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: `run_agent(..., trace=, prepared=)` (Task 5); `RunStore`, `RunManifest`, `StepState`, `TaskState`, `serialize_messages` (Task 3); `resolve_inputs`, `render_task_prompt`, `load_workflow`, `WorkflowDef`, `StepDef`, `TaskDef` (Task 1); `compute_step_status`, `compute_run_status` (Task 2); `build_trace` (Task 4); `AtomConfig.workflow` (Task 6); `make_prepared` (test fixture).
- Produces:
  - `task_thread_id(run_id, step_index, task_id) -> str`
  - `WorkflowEngine(cfg, *, store=None, prepared_provider=None, launcher=None, profile=None)` with `.store`, `.create_run(workflow, inputs, run_id, created_at=None) -> RunManifest`, `.launch(run_id)`, `async .execute(run_id) -> RunManifest`.
  - `PreparedProvider = Callable[[TaskDef, StepDef, WorkflowDef], PreparedModel|None]`

This task has three red/green cycles (single-step success, cross-step workspace hand-off, failure-halts). Commit once at the end.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workflow_engine.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workflow_engine.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.workflow.engine'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/workflow/engine.py
"""Workflow execution engine.

Orchestrates a run over its steps: steps run sequentially, tasks within a step run
concurrently (bounded by a semaphore), every task is a ``run_agent`` call bound to the run's
one shared workspace (existing-workspace mode) under its own thread id. A step progresses only
if every task succeeds; otherwise the run halts and later steps never run.
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Awaitable, Callable, Optional

from atom.agent import PreparedModel
from atom.config.schema import AtomConfig
from atom.runtime import run_agent
from atom.workflow.observability import build_trace
from atom.workflow.run_store import (
    RunManifest, RunStore, StepState, TaskState, serialize_messages,
)
from atom.workflow.schema import (
    StepDef, TaskDef, WorkflowDef, load_workflow, render_task_prompt, resolve_inputs,
)
from atom.workflow.status import compute_run_status, compute_step_status

PreparedProvider = Callable[[TaskDef, StepDef, WorkflowDef], Optional[PreparedModel]]
Launcher = Callable[[Awaitable], object]


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def task_thread_id(run_id: str, step_index: int, task_id: str) -> str:
    return f"{run_id}:s{step_index}:{task_id}"


class WorkflowEngine:
    def __init__(
        self,
        cfg: AtomConfig,
        *,
        store: RunStore | None = None,
        prepared_provider: PreparedProvider | None = None,
        launcher: Launcher | None = None,
        profile: str | None = None,
    ):
        self.cfg = cfg
        self.store = store or RunStore(cfg.home)
        self.prepared_provider = prepared_provider
        self.launcher: Launcher = launcher or asyncio.create_task
        self.profile = profile or cfg.defaults.agent
        self._defs: dict[str, WorkflowDef] = {}

    # ---- setup ----
    def create_run(
        self, workflow: WorkflowDef, inputs: dict, run_id: str, created_at: str | None = None
    ) -> RunManifest:
        resolved = resolve_inputs(workflow, inputs)          # raises MissingInputError
        steps: list[StepState] = []
        for i, step in enumerate(workflow.steps):
            tasks = [
                TaskState(
                    id=t.id, thread_id=task_thread_id(run_id, i, t.id),
                    model=t.model, thinking=t.thinking,
                )
                for t in step.tasks
            ]
            steps.append(StepState(index=i, title=step.title, tasks=tasks))
        manifest = RunManifest(
            run_id=run_id, workflow=workflow.name, inputs=resolved,
            created_at=created_at or _now(),
            workspace_path=str(self.store.workspace_dir(run_id)), steps=steps,
        )
        self._defs[run_id] = workflow
        return self.store.create(manifest)

    def launch(self, run_id: str):
        """Schedule execute() on the event loop (default asyncio.create_task)."""
        return self.launcher(self.execute(run_id))

    # ---- execution ----
    async def execute(self, run_id: str) -> RunManifest:
        manifest = self.store.load(run_id)
        workflow = self._defs.get(run_id) or load_workflow(manifest.workflow, self.cfg.home)
        manifest.status = "running"
        self.store.save(manifest)

        sem = asyncio.Semaphore(max(1, self.cfg.workflow.max_parallel))
        for step_state, step_def in zip(manifest.steps, workflow.steps):
            step_state.status = "running"
            self.store.save(manifest)

            async def run_one(ts: TaskState, td: TaskDef, sd: StepDef, ss: StepState):
                async with sem:
                    await self._run_task(manifest, workflow, ss, sd, ts, td)

            await asyncio.gather(*[
                run_one(ts, td, step_def, step_state)
                for ts, td in zip(step_state.tasks, step_def.tasks)
            ])

            step_state.status = compute_step_status([t.status for t in step_state.tasks])
            self.store.save(manifest)
            if step_state.status != "complete":
                manifest.status = "halted"
                manifest.ended_at = _now()
                self.store.save(manifest)
                return manifest

        manifest.status = compute_run_status([s.status for s in manifest.steps])
        manifest.ended_at = _now()
        self.store.save(manifest)
        return manifest

    async def _run_task(
        self, manifest: RunManifest, workflow: WorkflowDef,
        step_state: StepState, step_def: StepDef, ts: TaskState, td: TaskDef,
    ) -> None:
        ts.status = "running"
        ts.started_at = _now()
        self.store.save(manifest)

        prompt = render_task_prompt(td, manifest.inputs)
        trace = build_trace(
            workflow=workflow.name, run_id=manifest.run_id,
            step_index=step_state.index, step_title=step_state.title, task_id=ts.id,
        )
        prepared = self.prepared_provider(td, step_def, workflow) if self.prepared_provider else None
        timeout = self.cfg.workflow.task_timeout_seconds or None
        try:
            coro = run_agent(
                prompt, config=self.cfg, profile=self.profile,
                override_model=td.model, override_thinking=td.thinking,
                workspace=manifest.workspace_path, thread_id=ts.thread_id,
                trace=trace, prepared=prepared,
            )
            result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            self.store.save_chat(
                manifest.run_id, step_state.index, ts.id, serialize_messages(result.messages)
            )
            ts.status = "succeeded"
        except Exception as exc:  # noqa: BLE001 — any task failure halts the step
            ts.status = "failed"
            ts.error = f"{type(exc).__name__}: {exc}"
        ts.ended_at = _now()
        self.store.save(manifest)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workflow_engine.py -q`
Expected: PASS (3 passed). If the hand-off test fails on the tool text, print the chat to confirm `read_file` output includes the file content (the filesystem `read_file` returns line-numbered content).

- [ ] **Step 5: Run the whole suite (no regressions)**

Run: `python -m pytest -q`
Expected: all prior tests + the new ones PASS.

- [ ] **Step 6: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "feat(workflow): execution engine (shared workspace, halt-on-failure, chat snapshots)"
```

---

### Task 8: FastAPI application

**Files:**
- Modify: `pyproject.toml` (add `fastapi`, `uvicorn[standard]` to `dependencies`; `httpx` to `dev`)
- Create: `src/atom/api/__init__.py` (empty)
- Create: `src/atom/api/models.py`
- Create: `src/atom/api/app.py`
- Test: `tests/test_workflow_api.py`

**Interfaces:**
- Consumes: `WorkflowEngine`, `RunStore` (Task 7/3); `list_workflows`, `load_workflow`, `MissingInputError` (Task 1); `make_prepared` (fixture).
- Produces: `RunRequest(workflow:str, inputs:dict)`; `create_app(cfg:AtomConfig|None=None, engine:WorkflowEngine|None=None) -> FastAPI`.

- [ ] **Step 1: Add dependencies and install**

Edit `pyproject.toml`. In `[project].dependencies` add after `"rich>=13.7",`:
```toml
    # --- Workflow API (FastAPI automation surface) ---
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
```
In `[project.optional-dependencies].dev` add:
```toml
    "httpx>=0.27",
```
Then install:
```bash
source .venv/bin/activate && pip install -e ".[dev]" -q
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_workflow_api.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_api.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.api'`.

- [ ] **Step 4: Write minimal implementation**

```python
# src/atom/api/__init__.py
```
(empty file)

```python
# src/atom/api/models.py
"""Request/response models for the workflow API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
```

```python
# src/atom/api/app.py
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_api.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/atom/api/ tests/test_workflow_api.py
git commit -m "feat(api): FastAPI workflow API (submit/poll/messages/artifacts) + static UI mount"
```

---

### Task 9: CLI `workflow` subcommands + `serve`

**Files:**
- Modify: `src/atom/cli.py`
- Test: `tests/test_workflow_cli.py`

**Interfaces:**
- Consumes: `list_workflows`, `load_workflow`, `resolve_inputs` (Task 1); `WorkflowEngine` (Task 7); `RunStore` (Task 3); `create_app` (Task 8).
- Produces: CLI commands `workflow list`, `workflow run`, `workflow runs`, `serve`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workflow_cli.py
"""CLI workflow subcommands."""
from __future__ import annotations

from typer.testing import CliRunner

from atom.cli import app

runner = CliRunner()


def _seed(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "demo.yaml").write_text(
        "name: demo\ndescription: A demo.\n"
        "steps:\n  - title: Draft\n    tasks:\n      - id: t1\n        prompt: \"hello\"\n"
    )


def test_workflow_list(atom_home):
    _seed(atom_home)
    result = runner.invoke(app, ["workflow", "list"])
    assert result.exit_code == 0
    assert "demo" in result.stdout


def test_workflow_run_completes(atom_home, monkeypatch):
    _seed(atom_home)
    import atom.workflow.engine as engine_mod
    from atom.runtime import RunResult

    async def fake_run_agent(prompt, **kwargs):
        from langchain_core.messages import AIMessage
        return RunResult(
            thread_id=kwargs.get("thread_id", "t"),
            messages=[AIMessage(content="did it")], final_text="did it", state={},
        )

    monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)
    result = runner.invoke(app, ["workflow", "run", "demo"])
    assert result.exit_code == 0
    assert "complete" in result.stdout.lower()


def test_workflow_runs_lists(atom_home, monkeypatch):
    _seed(atom_home)
    import atom.workflow.engine as engine_mod
    from atom.runtime import RunResult

    async def fake_run_agent(prompt, **kwargs):
        from langchain_core.messages import AIMessage
        return RunResult(thread_id="t", messages=[AIMessage(content="x")], final_text="x", state={})

    monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)
    runner.invoke(app, ["workflow", "run", "demo"])
    result = runner.invoke(app, ["workflow", "runs"])
    assert result.exit_code == 0
    assert "demo" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_cli.py -q`
Expected: FAIL — `No such command 'workflow'` (CLI exit_code != 0 / assertion fails).

- [ ] **Step 3: Write minimal implementation**

At the top of `src/atom/cli.py`, add these imports near the existing ones:

```python
import uuid
```

Then add this block just above `if __name__ == "__main__":`:

```python
# --------------------------------------------------------------------------- workflows
workflow_app = typer.Typer(help="Run multi-agent workflows (Steps x Tasks).")
app.add_typer(workflow_app, name="workflow")


@workflow_app.command("list")
def workflow_list(config: str = typer.Option(None, "--config", "-c")) -> None:
    """List available workflow definitions in $ATOM_HOME/workflows."""
    from atom.workflow.schema import list_workflows

    cfg = load_config(config)
    wfs = list_workflows(cfg.home)
    if not wfs:
        console.print("[dim]No workflows found. Add YAML files under $ATOM_HOME/workflows.[/dim]")
        return
    for w in wfs:
        console.print(f"[bold]{w.name}[/bold]  [dim]{w.description or ''}[/dim]")


@workflow_app.command("run")
def workflow_run(
    name: str = typer.Argument(..., help="Workflow name."),
    input: list[str] = typer.Option(None, "--input", "-i", help="key=value (repeatable)."),
    profile: str = typer.Option(None, "--profile", "-p"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Submit a workflow and poll it to completion."""
    import datetime

    from atom.workflow.engine import WorkflowEngine
    from atom.workflow.schema import load_workflow

    _load_env()
    cfg = load_config(config)
    wf = load_workflow(name, cfg.home)
    inputs = dict(kv.split("=", 1) for kv in (input or []) if "=" in kv)
    engine = WorkflowEngine(cfg, profile=profile)
    run_id = uuid.uuid4().hex[:12]
    engine.create_run(wf, inputs, run_id, datetime.datetime.now().isoformat(timespec="seconds"))
    with console.status(f"[bold]running workflow {name}…[/bold]"):
        manifest = asyncio.run(engine.execute(run_id))
    for step in manifest.steps:
        marks = ", ".join(f"{t.id}:{t.status}" for t in step.tasks)
        console.print(f"  [bold]{step.title}[/bold] [dim]{step.status}[/dim] — {marks}")
    color = "green" if manifest.status == "complete" else "red"
    console.print(f"\n[{color} bold]{manifest.status}[/{color} bold]  [dim]run: {run_id}[/dim]")
    ws = engine.store.workspace_dir(run_id)
    files = [p for p in ws.rglob("*") if p.is_file()] if ws.is_dir() else []
    if files:
        console.print("[bold]Artifacts:[/bold]")
        for p in files:
            console.print(f"  • {p.relative_to(ws)}")


@workflow_app.command("runs")
def workflow_runs(config: str = typer.Option(None, "--config", "-c")) -> None:
    """List workflow runs."""
    from atom.workflow.run_store import RunStore

    cfg = load_config(config)
    runs = RunStore(cfg.home).list()
    if not runs:
        console.print("[dim]No runs yet.[/dim]")
        return
    for m in runs:
        console.print(f"{m.run_id}  [bold]{m.workflow}[/bold]  [dim]{m.status}  {m.created_at}[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Launch the workflow API + UI server."""
    import uvicorn

    from atom.api.app import create_app

    _load_env()
    uvicorn.run(create_app(load_config(config)), host=host, port=port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_cli.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/atom/cli.py tests/test_workflow_cli.py
git commit -m "feat(cli): workflow list/run/runs + serve"
```

---

### Task 10: Example workflow + docs

**Files:**
- Create: `workflows/parallel-poems.yaml`
- Modify: `config.yaml` (add `workflow:` block + `allowed_workspace_roots` note)
- Modify: `README.md` (add a "Workflows" section)

No new tests (documentation + example asset). Verify the example parses.

- [ ] **Step 1: Create the example workflow**

```yaml
# workflows/parallel-poems.yaml — copy to $ATOM_HOME/workflows/ to run it.
name: parallel-poems
description: Draft poems in parallel, then refine them in place.
inputs:
  - name: topic
    required: true
    description: What the poems are about.
  - name: style
    required: false
    default: free verse
steps:
  - title: Draft
    description: Three poets each draft one poem into the shared workspace.
    tasks:
      - id: poet_a
        prompt: "Write a {{ style }} poem about {{ topic }}. Save it as poem_a.md in {{ workspace }}."
        model: haiku
        thinking: low
      - id: poet_b
        prompt: "Write a {{ style }} poem about {{ topic }} from a child's point of view. Save it as poem_b.md in {{ workspace }}."
        model: haiku
        thinking: low
      - id: poet_c
        prompt: "Write a {{ style }} poem about {{ topic }} as a strict sonnet. Save it as poem_c.md in {{ workspace }}."
        model: haiku
        thinking: low
  - title: Refine
    description: One editor sharpens every draft.
    tasks:
      - id: refiner
        prompt: "Read every poem_*.md in {{ workspace }} and sharpen each for imagery and rhythm, saving each back in place."
        model: haiku
        thinking: medium
```

- [ ] **Step 2: Verify it parses**

Run:
```bash
python -c "import shutil,os; from pathlib import Path; \
h=Path(os.path.expanduser('~/.atom/workflows')); h.mkdir(parents=True, exist_ok=True); \
shutil.copy('workflows/parallel-poems.yaml', h); \
from atom.workflow.schema import load_workflow; w=load_workflow('parallel-poems'); \
print('OK', w.name, [s.title for s in w.steps])"
```
Expected: `OK parallel-poems ['Draft', 'Refine']`.

- [ ] **Step 3: Update `config.yaml`**

Add after the `library:` block:
```yaml
workflow:
  max_parallel: 4          # tasks run concurrently within a step
  task_timeout_seconds: 1800
```
And update the `sandbox.allowed_workspace_roots` comment to note the workflow caveat:
```yaml
  allowed_workspace_roots: []   # restrict "existing" workspaces. NOTE: if you set this, include
                                # your ATOM_HOME so workflow tasks can bind their shared run workspace.
```

- [ ] **Step 4: Update `README.md`**

Add this section after the "What's built in" section:
```markdown
## Workflows

Run many agents as ordered **steps** of parallel **tasks** sharing one workspace.

```bash
cp workflows/parallel-poems.yaml ~/.atom/workflows/          # make it discoverable
atom workflow list
atom workflow run parallel-poems --input topic="the tide" --input style=haiku
atom serve                                                   # REST API + web UI at http://127.0.0.1:8000
```

A workflow is defined in YAML (`$ATOM_HOME/workflows/<name>.yaml`): workflow-level `inputs`
(required/optional, used in task prompts via `{{ topic }}`), ordered `steps`, and each step's
`tasks` (a `prompt` plus optional `model`/`thinking`). Tasks in a step run in parallel; a step
advances only if **all** its tasks succeed, otherwise the run halts. Later steps read what earlier
steps wrote to the shared workspace. Set `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY` to trace
each task (tagged by workflow/step/task). The API (`atom serve`) is automation-first: `POST
/api/runs` to submit a job, poll `GET /api/runs/{id}`, then `GET /api/runs/{id}/artifacts`.
```

- [ ] **Step 5: Commit**

```bash
git add workflows/parallel-poems.yaml config.yaml README.md
git commit -m "docs(workflow): example workflow, config block, README section"
```

---

### Task 11: React test UI (`atom-ui/`) — manual verification (non-TDD)

**Files (all Create):** `atom-ui/package.json`, `atom-ui/vite.config.ts`, `atom-ui/tsconfig.json`, `atom-ui/index.html`, `atom-ui/src/main.tsx`, `atom-ui/src/api.ts`, `atom-ui/src/App.tsx`, `atom-ui/src/styles.css`, `atom-ui/.gitignore`.

This is the manual/e2e test surface — **no pytest**. Verify by running it against `atom serve`.

- [ ] **Step 1: Scaffold config files**

```json
// atom-ui/package.json
{
  "name": "atom-ui",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "^5.5.3",
    "vite": "^5.4.0"
  }
}
```

```typescript
// atom-ui/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist" },
});
```

```json
// atom-ui/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": false
  },
  "include": ["src"]
}
```

```html
<!-- atom-ui/index.html -->
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>atom workflows</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

```
// atom-ui/.gitignore
node_modules/
dist/
```

- [ ] **Step 2: API client + entrypoint**

```typescript
// atom-ui/src/api.ts
export interface InputDef { name: string; required: boolean; description?: string; default?: string; }
export interface Workflow { name: string; description?: string; inputs: InputDef[]; }
export interface TaskState { id: string; status: string; model?: string; error?: string; }
export interface StepState { index: number; title: string; status: string; tasks: TaskState[]; }
export interface Manifest {
  run_id: string; workflow: string; status: string;
  workspace_path: string; steps: StepState[];
}
export interface ChatMsg { role: string; text: string; tool_calls?: { name: string }[]; name?: string; }
export interface Artifact { path: string; size: number; modified: number; }

const j = async (r: Response) => { if (!r.ok) throw new Error(await r.text()); return r.json(); };

export const api = {
  workflows: (): Promise<Workflow[]> => fetch("/api/workflows").then(j),
  submit: (workflow: string, inputs: Record<string, string>): Promise<{ run_id: string }> =>
    fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow, inputs }),
    }).then(j),
  run: (id: string): Promise<Manifest> => fetch(`/api/runs/${id}`).then(j),
  messages: (id: string, step: number, task: string): Promise<ChatMsg[]> =>
    fetch(`/api/runs/${id}/tasks/${step}/${task}/messages`).then(j),
  artifacts: (id: string): Promise<Artifact[]> => fetch(`/api/runs/${id}/artifacts`).then(j),
  artifact: (id: string, path: string): Promise<string> =>
    fetch(`/api/runs/${id}/artifacts/${path}`).then((r) => r.text()),
};
```

```tsx
// atom-ui/src/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 3: The three views (`App.tsx`)**

```tsx
// atom-ui/src/App.tsx
import { useEffect, useState } from "react";
import { api, Artifact, ChatMsg, Manifest, Workflow } from "./api";

type View =
  | { name: "list" }
  | { name: "form"; workflow: Workflow }
  | { name: "run"; runId: string };

export default function App() {
  const [view, setView] = useState<View>({ name: "list" });
  return (
    <div className="app">
      <header onClick={() => setView({ name: "list" })}>⚛ atom workflows</header>
      {view.name === "list" && <WorkflowList onPick={(w) => setView({ name: "form", workflow: w })} />}
      {view.name === "form" && (
        <RunForm workflow={view.workflow} onStarted={(id) => setView({ name: "run", runId: id })} />
      )}
      {view.name === "run" && <RunView runId={view.runId} />}
    </div>
  );
}

function WorkflowList({ onPick }: { onPick: (w: Workflow) => void }) {
  const [wfs, setWfs] = useState<Workflow[]>([]);
  useEffect(() => { api.workflows().then(setWfs).catch(console.error); }, []);
  return (
    <div className="panel">
      <h2>Workflows</h2>
      {wfs.map((w) => (
        <div key={w.name} className="card" onClick={() => onPick(w)}>
          <strong>{w.name}</strong>
          <div className="dim">{w.description}</div>
        </div>
      ))}
    </div>
  );
}

function RunForm({ workflow, onStarted }: { workflow: Workflow; onStarted: (id: string) => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const submit = async () => {
    const { run_id } = await api.submit(workflow.name, values);
    onStarted(run_id);
  };
  return (
    <div className="panel">
      <h2>{workflow.name}</h2>
      {workflow.inputs.map((i) => (
        <label key={i.name} className="field">
          {i.name}{i.required ? " *" : ""}
          <input
            placeholder={i.default ?? ""}
            onChange={(e) => setValues((v) => ({ ...v, [i.name]: e.target.value }))}
          />
        </label>
      ))}
      <button onClick={submit}>Start run</button>
    </div>
  );
}

function RunView({ runId }: { runId: string }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [sel, setSel] = useState<{ step: number; task: string } | null>(null);
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [arts, setArts] = useState<Artifact[]>([]);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const m = await api.run(runId).catch(() => null);
      if (live && m) {
        setManifest(m);
        api.artifacts(runId).then(setArts).catch(() => {});
        if (m.status === "complete" || m.status === "halted") return;
      }
      if (live) setTimeout(tick, 1500);
    };
    tick();
    return () => { live = false; };
  }, [runId]);

  useEffect(() => {
    if (!sel) return;
    api.messages(runId, sel.step, sel.task).then(setChat).catch(() => setChat([]));
  }, [sel, runId, manifest?.status]);

  if (!manifest) return <div className="panel">Loading…</div>;
  return (
    <div className="run">
      <div className="steps">
        <h2>{manifest.workflow} <span className={`badge ${manifest.status}`}>{manifest.status}</span></h2>
        {manifest.steps.map((s) => (
          <div key={s.index} className="step">
            <div className="step-title">{s.title} <span className="dim">{s.status}</span></div>
            {s.tasks.map((t) => (
              <div
                key={t.id}
                className={`task ${t.status} ${sel?.task === t.id && sel?.step === s.index ? "active" : ""}`}
                onClick={() => setSel({ step: s.index, task: t.id })}
              >
                {t.id} <span className="dim">{t.status}</span>
              </div>
            ))}
          </div>
        ))}
        <h3>Artifacts</h3>
        {arts.map((a) => (
          <div key={a.path} className="artifact" onClick={() => api.artifact(runId, a.path).then(alert)}>
            {a.path} <span className="dim">{a.size}b</span>
          </div>
        ))}
      </div>
      <div className="chat">
        <h3>{sel ? `${sel.task} (step ${sel.step})` : "Select a task"}</h3>
        {chat.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="role">{m.name || m.role}</div>
            <div className="text">{m.text || (m.tool_calls ? `→ ${m.tool_calls.map((c) => c.name).join(", ")}` : "")}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

```css
/* atom-ui/src/styles.css */
* { box-sizing: border-box; }
body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; background: #0f1115; color: #e6e6e6; }
.app > header { padding: 12px 20px; font-weight: 700; background: #171a21; cursor: pointer; }
.panel { max-width: 720px; margin: 24px auto; padding: 0 20px; }
.card { padding: 14px; margin: 8px 0; background: #171a21; border-radius: 8px; cursor: pointer; }
.card:hover { background: #1e2230; }
.dim { color: #8b93a7; font-size: 13px; }
.field { display: block; margin: 12px 0; }
.field input { display: block; width: 100%; padding: 8px; margin-top: 4px; background: #0b0d12; color: #e6e6e6; border: 1px solid #2a2f3a; border-radius: 6px; }
button { padding: 10px 16px; background: #3b82f6; color: white; border: 0; border-radius: 6px; cursor: pointer; }
.run { display: grid; grid-template-columns: 320px 1fr; gap: 16px; padding: 20px; }
.step { margin: 10px 0; padding: 10px; background: #171a21; border-radius: 8px; }
.step-title { font-weight: 600; margin-bottom: 6px; }
.task { padding: 6px 8px; margin: 4px 0; border-radius: 6px; cursor: pointer; background: #0b0d12; }
.task.active { outline: 2px solid #3b82f6; }
.task.succeeded { border-left: 3px solid #22c55e; }
.task.failed { border-left: 3px solid #ef4444; }
.task.running { border-left: 3px solid #eab308; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #2a2f3a; }
.badge.complete { background: #14532d; } .badge.halted { background: #7f1d1d; }
.chat { background: #171a21; border-radius: 8px; padding: 12px; min-height: 60vh; }
.msg { margin: 10px 0; padding: 8px 10px; background: #0b0d12; border-radius: 6px; }
.msg .role { font-size: 12px; color: #8b93a7; } .msg.tool .role { color: #eab308; }
.artifact { padding: 4px 8px; cursor: pointer; font-size: 13px; } .artifact:hover { color: #3b82f6; }
```

- [ ] **Step 4: Manual verification**

```bash
cd atom-ui && npm install && npm run build && cd ..
# Terminal A: source .venv/bin/activate && atom serve
# Browser: http://127.0.0.1:8000
```
Verify: (1) workflow list shows `parallel-poems`; (2) the form shows `topic *` + `style`; submitting starts a run; (3) the run view shows steps/tasks changing status, selecting a task shows its chat, and the Artifacts panel lists `poem_*.md`. (Dev alternative: `npm run dev` with `atom serve` running, open the Vite URL.)

- [ ] **Step 5: Commit**

```bash
git add atom-ui/
git commit -m "feat(ui): React test UI (workflow list, run form, run view)"
```

---

## Final verification

- [ ] Run the full Python suite: `python -m pytest -q` → all green (baseline 67 + new workflow tests).
- [ ] `python -m compileall -q src` → COMPILE OK.
- [ ] `atom workflow list` shows the example (after copying it to `~/.atom/workflows/`).
- [ ] The `atom-ui` build succeeds and the three views work against `atom serve`.

---

## Self-review notes (author)

- **Spec coverage:** §2 YAML → Task 1; §3 run/persistence → Task 3; §4 execution/halt → Tasks 2+7; §5 observability → Tasks 4+5; §6 API → Task 8; §7 CLI → Task 9; §8 UI → Task 11; §9 layout → all; §10 testing → each task's TDD steps; §11 decisions → Tasks 6/7. Example workflow (spec §1/§10) → Task 10.
- **Deviation from spec wording:** spec §3/§6 said task chats are "recovered live from the checkpointer." This plan persists a **completion snapshot** per task (`chats/s<i>__<task>.json`) written by the engine and served by the API — simpler, version-independent, and testable with the memory checkpointer, while fully satisfying "view each task's chat separated by step." The durable checkpointer still holds each task's state under its `thread_id`.
- **Type consistency:** status strings, `thread_id` format, `serialize_messages` shape, and `prepared_provider(TaskDef, StepDef, WorkflowDef)` signature are used identically across Tasks 3/7/8.
