# LangSmith Run Exporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class `atom workflow export <run_id>` command that downloads a workflow run's raw LangSmith trace tree (lead tasks + nested sub-agent/LLM runs, thinking intact) to `runs/<run_id>/export.json`, plus two write-side fixes (tracer flush + activation logging) that make the export reliable and complete.

**Architecture:** Convert `src/atom/observability.py` into a package (`observability/trace.py` push side + `observability/export.py` new pull side; `__init__.py` re-exports so no existing import changes). The exporter reads the local run manifest as a completeness oracle, fetches root runs by the run-wide `run_id` metadata, hydrates each with `read_run(load_child_runs=True)`, polls to absorb async-ingestion lag, and writes a thin envelope around the verbatim LangSmith `Run` dicts. A Typer subcommand wraps it.

**Tech Stack:** Python 3, `langsmith==0.9.7` (already a dependency), Typer CLI, Pydantic (RunManifest / LangSmith `Run` both expose `model_dump(mode="json")`), pytest.

## Global Constraints

- Run tests with `.venv/bin/python -m pytest` (NOT bare `pytest`).
- `langsmith` stays pinned `>=0.9,<1`; **no new dependencies**.
- The exporter is **read-only** against LangSmith (no create/update/delete of runs).
- **Never break** existing `from atom.observability import ...` imports — the package `__init__` must re-export every currently-public name: `apply_observability_env`, `build_lead_trace`, `enrich_lead_trace`, `build_subagent_trace`, `tracing_active`, `prompt_fingerprint`, `git_sha`, `_apply_trace`.
- Zero behavior change when observability is disabled: the flush is a no-op, activation logging is silent, and `export` reports "no traces found" for untraced runs.
- Naming is `verb_noun` (`export_run`, `fetch_run_tree`, `resolve_run_ids`, `expected_root_count`, `build_envelope`).
- Output is written atomically (`*.tmp` → `os.replace`), matching `RunStore.save`.
- Every code step shows the complete code. TDD: failing test first, then minimal implementation.

---

### Task 1: Convert `observability.py` into a package (pure refactor)

**Files:**
- Move: `src/atom/observability.py` → `src/atom/observability/trace.py`
- Create: `src/atom/observability/__init__.py`
- Test: `tests/test_observability.py` (unchanged; add one guard test)

**Interfaces:**
- Consumes: nothing new.
- Produces: `atom.observability` as a package whose `__init__` re-exports the 8 public names above from `atom.observability.trace`. No signatures change.

- [ ] **Step 1: Add a guard test that the public import surface still resolves**

Add to the bottom of `tests/test_observability.py`:

```python
def test_public_import_surface_after_package_move():
    # Every name any other module imports from atom.observability must still resolve
    # from the package root after the module -> package conversion.
    from atom.observability import (
        _apply_trace,
        apply_observability_env,
        build_lead_trace,
        build_subagent_trace,
        enrich_lead_trace,
        git_sha,
        prompt_fingerprint,
        tracing_active,
    )
    assert callable(apply_observability_env) and callable(build_lead_trace)
    assert callable(_apply_trace) and callable(enrich_lead_trace)
```

- [ ] **Step 2: Run it to confirm it passes today (before the move)**

Run: `.venv/bin/python -m pytest tests/test_observability.py::test_public_import_surface_after_package_move -v`
Expected: PASS (the flat module already exports these). This is the invariant we must preserve.

- [ ] **Step 3: Perform the move**

```bash
mkdir -p src/atom/observability
git mv src/atom/observability.py src/atom/observability/trace.py
```

- [ ] **Step 4: Create the package `__init__.py`**

Create `src/atom/observability/__init__.py`:

```python
"""LangSmith observability.

Push side (trace builders + env activation) lives in ``trace``; the pull side
(run exporter) lives in ``export``. This package re-exports the push-side public
names so ``from atom.observability import build_lead_trace`` (and friends) keeps
working unchanged after the module -> package conversion.
"""
from atom.observability.trace import (
    _apply_trace,
    apply_observability_env,
    build_lead_trace,
    build_subagent_trace,
    enrich_lead_trace,
    git_sha,
    prompt_fingerprint,
    tracing_active,
)

__all__ = [
    "_apply_trace",
    "apply_observability_env",
    "build_lead_trace",
    "build_subagent_trace",
    "enrich_lead_trace",
    "git_sha",
    "prompt_fingerprint",
    "tracing_active",
]
```

- [ ] **Step 5: Run the full observability + dependent suites to confirm no breakage**

Run: `.venv/bin/python -m pytest tests/test_observability.py tests/test_subagent.py -q`
Expected: PASS (all existing tests, since imports resolve identically through the package).

- [ ] **Step 6: Commit**

```bash
git add src/atom/observability/ tests/test_observability.py
git commit -m "refactor(observability): split module into observability/ package (trace + future export)"
```

---

### Task 2: `ObservabilityStatus` return value + engine activation logging

**Files:**
- Modify: `src/atom/observability/trace.py` (add `ObservabilityStatus`, return it from `apply_observability_env`)
- Modify: `src/atom/observability/__init__.py` (re-export `ObservabilityStatus`)
- Modify: `src/atom/workflow/engine.py` (capture the status, log one line)
- Test: `tests/test_observability.py`, `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: the package from Task 1.
- Produces:
  - `ObservabilityStatus` dataclass: `active: bool`, `project: str | None`, `reason: str` (one of `"active"`, `"enabled-but-no-api-key"`, `"disabled"`).
  - `apply_observability_env(cfg) -> ObservabilityStatus` (same env side-effects as before; now also returns status).

- [ ] **Step 1: Write the failing tests for the status return value**

Add to `tests/test_observability.py`:

```python
def test_apply_env_status_active(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=True, project="proj"))
    status = apply_observability_env(cfg)
    assert status.active is True
    assert status.reason == "active"
    assert status.project == "proj"


def test_apply_env_status_enabled_but_no_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=True, project="proj"))
    status = apply_observability_env(cfg)
    assert status.active is False
    assert status.reason == "enabled-but-no-api-key"


def test_apply_env_status_disabled(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=False))
    status = apply_observability_env(cfg)
    assert status.active is False and status.reason == "disabled"
```

Also update the import at the top of `tests/test_observability.py` to include `ObservabilityStatus`:

```python
from atom.observability import (
    ObservabilityStatus,
    apply_observability_env,
    build_lead_trace,
    build_subagent_trace,
    enrich_lead_trace,
    prompt_fingerprint,
    tracing_active,
)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_observability.py -k status -v`
Expected: FAIL — `ImportError: cannot import name 'ObservabilityStatus'` (and `apply_observability_env` returns `None`).

- [ ] **Step 3: Add `ObservabilityStatus` and return it**

In `src/atom/observability/trace.py`, add the dataclass import at the top:

```python
from dataclasses import dataclass
```

Add the dataclass just above `apply_observability_env`:

```python
@dataclass
class ObservabilityStatus:
    """Result of mapping the observability config onto LANGSMITH_* env.

    reason: "active" | "enabled-but-no-api-key" | "disabled".
    """
    active: bool
    project: str | None
    reason: str
```

Replace the body of `apply_observability_env` with one that returns the status (env behavior is unchanged — it still never overwrites an existing var and only enables with a key present):

```python
def apply_observability_env(cfg: AtomConfig) -> ObservabilityStatus:
    """Map the observability config block onto LANGSMITH_* env, never overwriting existing vars.

    Tracing is enabled only when requested AND an API key is present, so a half-configured setup is a
    safe no-op rather than a crash or a keyless export attempt. Idempotent. Returns a status describing
    whether tracing is (now) active so callers can surface a one-line activation notice.
    """
    obs = cfg.observability
    tracing_on = tracing_active()
    have_key = bool(os.environ.get("LANGSMITH_API_KEY"))
    will_enable = bool(obs.enabled and have_key and not os.environ.get("LANGSMITH_TRACING"))
    if (tracing_on or will_enable) and obs.project and not os.environ.get("LANGSMITH_PROJECT"):
        os.environ["LANGSMITH_PROJECT"] = obs.project
    if will_enable:
        os.environ["LANGSMITH_TRACING"] = "true"

    if tracing_on or will_enable:
        return ObservabilityStatus(
            active=True,
            project=os.environ.get("LANGSMITH_PROJECT") or obs.project,
            reason="active",
        )
    if obs.enabled and not have_key:
        return ObservabilityStatus(active=False, project=obs.project, reason="enabled-but-no-api-key")
    return ObservabilityStatus(active=False, project=None, reason="disabled")
```

- [ ] **Step 4: Re-export `ObservabilityStatus` from the package**

In `src/atom/observability/__init__.py`, add `ObservabilityStatus` to both the import block and `__all__`:

```python
from atom.observability.trace import (
    ObservabilityStatus,
    _apply_trace,
    apply_observability_env,
    build_lead_trace,
    build_subagent_trace,
    enrich_lead_trace,
    git_sha,
    prompt_fingerprint,
    tracing_active,
)

__all__ = [
    "ObservabilityStatus",
    "_apply_trace",
    "apply_observability_env",
    "build_lead_trace",
    "build_subagent_trace",
    "enrich_lead_trace",
    "git_sha",
    "prompt_fingerprint",
    "tracing_active",
]
```

- [ ] **Step 5: Run the observability tests**

Run: `.venv/bin/python -m pytest tests/test_observability.py -v`
Expected: PASS — including the three new status tests and all pre-existing tests (they ignore the new return value).

- [ ] **Step 6: Write the failing engine-logging test**

Add to `tests/test_workflow_engine.py` (module already imports `atom.workflow.engine as engine_mod` and `WorkflowEngine`; add these near the top-level test functions):

```python
def test_engine_warns_when_enabled_but_no_api_key(base_config, monkeypatch, caplog):
    import logging
    from atom.config.schema import ObservabilityConfig

    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    cfg = base_config.model_copy(deep=True)
    cfg.observability = ObservabilityConfig(enabled=True, project="proj")
    with caplog.at_level(logging.WARNING):
        WorkflowEngine(cfg)
    assert any("LANGSMITH_API_KEY missing" in r.message for r in caplog.records)
```

- [ ] **Step 7: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py::test_engine_warns_when_enabled_but_no_api_key -v`
Expected: FAIL — no warning is logged (engine currently discards the return value).

- [ ] **Step 8: Log the activation status in the engine**

In `src/atom/workflow/engine.py`, add a module logger. After the existing imports (near line 12), add:

```python
import logging

logger = logging.getLogger(__name__)
```

Then in `WorkflowEngine.__init__`, replace the single line at ~`engine.py:64`:

```python
        # Map observability config -> LANGSMITH_* env once, before any run (idempotent).
        apply_observability_env(cfg)
```

with:

```python
        # Map observability config -> LANGSMITH_* env once, before any run (idempotent).
        status = apply_observability_env(cfg)
        if status.active:
            logger.info("observability: tracing active -> project %r", status.project)
        elif status.reason == "enabled-but-no-api-key":
            logger.warning(
                "observability: observability.enabled but LANGSMITH_API_KEY missing "
                "-- traces will NOT be uploaded"
            )
```

- [ ] **Step 9: Run the engine test**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py::test_engine_warns_when_enabled_but_no_api_key -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/atom/observability/ src/atom/workflow/engine.py tests/test_observability.py tests/test_workflow_engine.py
git commit -m "feat(observability): apply_observability_env returns status; engine logs activation"
```

---

### Task 3: Tracer flush at workflow-run exit

**Files:**
- Modify: `src/atom/workflow/engine.py` (import `wait_for_all_tracers` + `tracing_active`; flush in `execute()`'s `finally`)
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: `tracing_active` (from `atom.observability`), `wait_for_all_tracers` (from `langchain_core.tracers.langchain`).
- Produces: `execute()` calls `wait_for_all_tracers()` exactly once per run, only when `tracing_active()` is true.

- [ ] **Step 1: Write the failing tests (flush when active; skip when inactive)**

Add to `tests/test_workflow_engine.py`:

```python
def _one_task_wf():
    return WorkflowDef.model_validate({
        "name": "demo",
        "inputs": [{"name": "topic", "required": True}],
        "steps": [{"title": "Draft", "tasks": [{"id": "solo", "prompt": "write {{ topic }}"}]}],
    })


@pytest.mark.asyncio
async def test_execute_flushes_tracers_when_active(base_config, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "tracing_active", lambda: True)
    monkeypatch.setattr(engine_mod, "wait_for_all_tracers", lambda: calls.append("flush"))
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared([AIMessage(content="done")]),
    )
    engine.create_run(_one_task_wf(), {"topic": "sea"}, "runF", "2026-07-09T00:00:00")
    await engine.execute("runF")
    assert calls == ["flush"]  # flushed exactly once


@pytest.mark.asyncio
async def test_execute_skips_flush_when_inactive(base_config, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "tracing_active", lambda: False)
    monkeypatch.setattr(engine_mod, "wait_for_all_tracers", lambda: calls.append("flush"))
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared([AIMessage(content="done")]),
    )
    engine.create_run(_one_task_wf(), {"topic": "sea"}, "runG", "2026-07-09T00:00:00")
    await engine.execute("runG")
    assert calls == []  # tracing off -> no flush
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -k flush -v`
Expected: FAIL — `AttributeError: module 'atom.workflow.engine' has no attribute 'wait_for_all_tracers'` (nothing to monkeypatch yet).

- [ ] **Step 3: Import the flush + tracing_active and call it in the `finally`**

In `src/atom/workflow/engine.py`, extend the observability import (currently `from atom.observability import apply_observability_env, build_lead_trace`) to:

```python
from atom.observability import apply_observability_env, build_lead_trace, tracing_active
```

Add the LangChain flush import near the other imports:

```python
from langchain_core.tracers.langchain import wait_for_all_tracers
```

In `execute()`, change the existing `finally` block (at ~`engine.py:191`) from:

```python
        finally:
            self._defs.pop(run_id, None)
```

to:

```python
        finally:
            self._defs.pop(run_id, None)
            # Flush LangSmith's background trace queue before the process can exit, so the run's
            # final batch is guaranteed uploaded and downloadable. No-op when tracing is off.
            if tracing_active():
                wait_for_all_tracers()
```

- [ ] **Step 4: Run the flush tests**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -k flush -v`
Expected: PASS (both).

- [ ] **Step 5: Run the whole engine suite (guard against regressions)**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "feat(observability): flush LangSmith tracers at workflow-run exit"
```

---

### Task 4: Exporter pure helpers — `ExportResult`, `expected_root_count`, `build_envelope`

**Files:**
- Create: `src/atom/observability/export.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `RunManifest`, `StepState`, `TaskState` from `atom.workflow.run_store`.
- Produces:
  - `ExportResult` dataclass: `run_id: str`, `path: str`, `complete: bool`, `expected_roots: int`, `fetched_roots: int`.
  - `expected_root_count(manifest: RunManifest) -> int` — count of tasks whose status is in `{"running", "succeeded", "failed"}` (a pending/never-ran task emits no trace).
  - `build_envelope(run_id, workflow, project, manifest, roots, *, complete, expected, fetched, now) -> dict` — the on-disk envelope (see spec §6). `now` is an ISO string; `roots` is a list of serialized run dicts.

- [ ] **Step 1: Write the failing tests for the pure helpers**

Create `tests/test_export.py`:

```python
"""LangSmith run exporter: pure helpers + the fetch/poll pipeline with an injected fake client."""
from __future__ import annotations

import json

import pytest

from atom.observability.export import (
    ExportResult,
    build_envelope,
    expected_root_count,
)
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState


def _manifest(run_id: str, statuses: list[str]) -> RunManifest:
    tasks = [
        TaskState(id=f"t{i}", thread_id=f"{run_id}:s0:t{i}", status=st)
        for i, st in enumerate(statuses)
    ]
    return RunManifest(
        run_id=run_id, workflow="wf", created_at="2026-07-09T00:00:00",
        workspace_path="/tmp/ws", steps=[StepState(index=0, title="S", tasks=tasks)],
    )


def test_expected_root_count_skips_pending():
    m = _manifest("r1", ["succeeded", "failed", "running", "pending"])
    assert expected_root_count(m) == 3  # pending excluded


def test_expected_root_count_all_pending_is_zero():
    assert expected_root_count(_manifest("r1", ["pending", "pending"])) == 0


def test_build_envelope_shape():
    m = _manifest("r1", ["succeeded"])
    roots = [{"id": "root1", "child_runs": [{"run_type": "llm"}]}]
    env = build_envelope(
        "r1", "wf", "proj", m, roots,
        complete=True, expected=1, fetched=1, now="2026-07-09T12:00:00",
    )
    assert env["run_id"] == "r1" and env["workflow"] == "wf" and env["project"] == "proj"
    assert env["exported_at"] == "2026-07-09T12:00:00"
    assert env["complete"] is True and env["expected_roots"] == 1 and env["fetched_roots"] == 1
    assert env["roots"] == roots
    assert env["atom_manifest"]["run_id"] == "r1"          # manifest embedded verbatim
    assert env["atom_manifest"]["steps"][0]["tasks"][0]["status"] == "succeeded"
    # Whole envelope must be JSON-serializable.
    assert json.loads(json.dumps(env))["run_id"] == "r1"


def test_export_result_is_a_dataclass():
    r = ExportResult(run_id="r1", path="/x/export.json", complete=True,
                     expected_roots=1, fetched_roots=1)
    assert r.run_id == "r1" and r.complete is True
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.observability.export'`.

- [ ] **Step 3: Implement the pure helpers**

Create `src/atom/observability/export.py`:

```python
"""Download a workflow run's LangSmith traces to disk for offline evaluation.

The exporter is read-only: it fetches a run's root runs by the run-wide ``run_id`` metadata
(a run spans one thread per task, so ``session_id`` would only capture one task), hydrates each
root's full child tree (sub-agent + per-LLM-call runs, with thinking blocks intact), and writes a
thin envelope around the verbatim LangSmith ``Run`` dicts. The local run manifest is the completeness
oracle: ``#root runs`` should equal the number of tasks that actually executed.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from atom.workflow.run_store import RunManifest, RunStore

_EXECUTED = ("running", "succeeded", "failed")


@dataclass
class ExportResult:
    run_id: str
    path: str            # where export.json was written ("" when nothing was exported)
    complete: bool       # fetched_roots >= expected_roots
    expected_roots: int
    fetched_roots: int


def expected_root_count(manifest: RunManifest) -> int:
    """How many lead-task root runs LangSmith should hold for this run.

    One lead root per task that reached execution; sub-agents nest under their lead (not extra roots),
    and a pending/never-ran task (e.g. after a halt) emits no trace.
    """
    return sum(1 for s in manifest.steps for t in s.tasks if t.status in _EXECUTED)


def build_envelope(
    run_id: str, workflow: str, project: str, manifest: RunManifest, roots: list[dict],
    *, complete: bool, expected: int, fetched: int, now: str,
) -> dict:
    """The on-disk export.json: a thin, self-describing wrapper around the raw LangSmith trees."""
    import langsmith

    return {
        "run_id": run_id,
        "workflow": workflow,
        "project": project,
        "exported_at": now,
        "langsmith_sdk": getattr(langsmith, "__version__", None),
        "complete": complete,
        "expected_roots": expected,
        "fetched_roots": fetched,
        "atom_manifest": manifest.model_dump(mode="json"),
        "roots": roots,
    }
```

- [ ] **Step 4: Run the helper tests**

Run: `.venv/bin/python -m pytest tests/test_export.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/export.py tests/test_export.py
git commit -m "feat(observability): export helpers — ExportResult, expected_root_count, build_envelope"
```

---

### Task 5: Exporter pipeline — `fetch_run_tree`, `export_run`, `resolve_run_ids`

**Files:**
- Modify: `src/atom/observability/export.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `ExportResult`, `expected_root_count`, `build_envelope` (Task 4); `RunStore` (`.load`, `.list`, `.run_dir`).
- Produces:
  - `fetch_run_tree(client, project, run_id) -> list[dict]` — `list_runs(...)` roots → `read_run(id, load_child_runs=True)` each → `run.model_dump(mode="json")`.
  - `export_run(home, run_id, *, project=None, client=None, poll_timeout=30.0, poll_interval=2.0, now=None, sleep=None, monotonic=None) -> ExportResult` — load manifest, require key + project, fetch+poll until `fetched >= expected` or timeout, write envelope (only when `fetched > 0`).
  - `resolve_run_ids(home, *, run_id=None, latest=None, all_workflow=None) -> list[str]` — exactly one selector; `--latest`→newest matching run, `--all`→all matching (newest-first), `run_id`→passthrough.

- [ ] **Step 1: Write the failing tests for the pipeline**

Add to `tests/test_export.py` (extend the import at the top to include the new names):

```python
from atom.observability.export import (
    ExportResult,
    build_envelope,
    expected_root_count,
    export_run,
    fetch_run_tree,
    resolve_run_ids,
)
```

Add the fake client + tests:

```python
class _FakeRun:
    def __init__(self, id, dump):
        self.id = id
        self._dump = dump

    def model_dump(self, mode="python"):
        return dict(self._dump)


class _FakeClient:
    """Scripts successive list_runs() results (to simulate async-ingestion lag) and per-id child trees."""
    def __init__(self, list_sequence, children):
        self._seq = list(list_sequence)     # e.g. [["root1"], ["root1", "root2"]]
        self._children = children           # {"root1": {...full dump...}, ...}
        self.list_calls = 0
        self.filters = []

    def list_runs(self, project_name, is_root, filter):
        self.filters.append(filter)
        idx = min(self.list_calls, len(self._seq) - 1)
        self.list_calls += 1
        return iter([_FakeRun(rid, {"id": rid}) for rid in self._seq[idx]])

    def read_run(self, run_id, load_child_runs):
        assert load_child_runs is True
        return _FakeRun(run_id, self._children[run_id])


def _store_with_run(atom_home, run_id, statuses):
    store = RunStore(str(atom_home))
    store.create(_manifest(run_id, statuses).model_copy(update={
        "workspace_path": str(store.workspace_dir(run_id))
    }))
    return store


def _no_sleep(_s):  # deterministic tests: never actually sleep
    pass


def test_fetch_run_tree_hydrates_children():
    client = _FakeClient(
        [["root1", "root2"]],
        {"root1": {"id": "root1", "child_runs": [{"run_type": "llm"}]},
         "root2": {"id": "root2", "child_runs": []}},
    )
    trees = fetch_run_tree(client, "proj", "r1")
    assert [t["id"] for t in trees] == ["root1", "root2"]
    assert trees[0]["child_runs"][0]["run_type"] == "llm"
    assert 'run_id' in client.filters[0] and 'r1' in client.filters[0]  # filtered by run_id metadata


def test_export_run_happy_path(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient(
        [["root1", "root2"]],
        {"root1": {"id": "root1", "child_runs": [{"run_type": "llm", "outputs": {"thinking": "…"}}]},
         "root2": {"id": "root2", "child_runs": []}},
    )
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "2026-07-09T12:00:00", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 2 and result.expected_roots == 2
    env = json.loads(Path(result.path).read_text())
    assert env["run_id"] == "r1" and len(env["roots"]) == 2
    assert env["roots"][0]["child_runs"][0]["outputs"]["thinking"] == "…"  # reasoning present
    assert env["atom_manifest"]["run_id"] == "r1"


def test_export_run_polls_through_ingestion_lag(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient(
        [["root1"], ["root1", "root2"]],   # 1 root first, both on the second poll
        {"root1": {"id": "root1"}, "root2": {"id": "root2"}},
    )
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 2
    assert client.list_calls == 2  # it polled again after the short first result


def test_export_run_partial_on_timeout(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient([["root1"]], {"root1": {"id": "root1"}})  # only ever 1 of 2
    clock = iter([0.0, 100.0, 200.0])  # deadline=0+30=30; second read (100) >= 30 -> stop
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep, monotonic=lambda: next(clock),
                        poll_timeout=30.0)
    assert result.complete is False and result.fetched_roots == 1 and result.expected_roots == 2
    env = json.loads(Path(result.path).read_text())
    assert env["complete"] is False  # eval pipeline can see the truncation


def test_export_run_no_traces_writes_nothing(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    store = _store_with_run(atom_home, "r1", ["succeeded"])
    client = _FakeClient([[]], {})     # tracing was off during the run -> nothing in LangSmith
    clock = iter([0.0, 100.0])
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep, monotonic=lambda: next(clock))
    assert result.fetched_roots == 0 and result.path == ""
    assert not (store.run_dir("r1") / "export.json").exists()  # no misleading empty artifact


def test_export_run_requires_api_key(atom_home, monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    _store_with_run(atom_home, "r1", ["succeeded"])
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        export_run(str(atom_home), "r1", project="proj", client=_FakeClient([[]], {}))


def test_export_run_unknown_run(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    with pytest.raises(FileNotFoundError):
        export_run(str(atom_home), "nope", project="proj", client=_FakeClient([[]], {}))


def test_resolve_run_ids_selectors(atom_home):
    store = RunStore(str(atom_home))
    for rid, wf, created in [("a", "alpha", "2026-07-09T01"), ("b", "alpha", "2026-07-09T03"),
                             ("c", "beta", "2026-07-09T02")]:
        m = _manifest(rid, ["succeeded"]).model_copy(update={"workflow": wf, "created_at": created,
                                                             "workspace_path": str(store.workspace_dir(rid))})
        store.create(m)
    assert resolve_run_ids(str(atom_home), run_id="a") == ["a"]
    assert resolve_run_ids(str(atom_home), latest="alpha") == ["b"]          # newest of alpha
    assert resolve_run_ids(str(atom_home), all_workflow="alpha") == ["b", "a"]  # all alpha, newest-first
    with pytest.raises(ValueError):
        resolve_run_ids(str(atom_home))                                     # zero selectors
    with pytest.raises(ValueError):
        resolve_run_ids(str(atom_home), run_id="a", latest="alpha")         # two selectors
    with pytest.raises(ValueError, match="no runs found"):
        resolve_run_ids(str(atom_home), latest="ghost")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_export.py -k "fetch or export_run or resolve" -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_run_tree'` (and siblings).

- [ ] **Step 3: Implement the pipeline**

Append to `src/atom/observability/export.py`:

```python
def _default_client() -> Any:
    from langsmith import Client
    return Client()


def fetch_run_tree(client: Any, project: str, run_id: str) -> list[dict]:
    """Fetch a run's root runs (by run_id metadata) and hydrate each full child tree.

    Sub-agents nest under their lead root, so ``load_child_runs=True`` brings back the whole
    lead + sub-agent + per-LLM-call tree (with thinking blocks) for each root.
    """
    flt = f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))'
    roots = list(client.list_runs(project_name=project, is_root=True, filter=flt))
    trees: list[dict] = []
    for r in roots:
        full = client.read_run(r.id, load_child_runs=True)
        trees.append(full.model_dump(mode="json"))
    return trees


def export_run(
    home: str | None,
    run_id: str,
    *,
    project: str | None = None,
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download ``run_id``'s LangSmith trace tree to ``runs/<run_id>/export.json``.

    Polls until the number of fetched roots matches the number of executed tasks (from the local
    manifest) or ``poll_timeout`` elapses, absorbing LangSmith's async-ingestion lag. Writes nothing
    when no traces are found (returns ``fetched_roots == 0``, ``path == ""``).
    """
    if not project:
        raise ValueError("no LangSmith project — set observability.project or pass project=")
    store = RunStore(home)
    manifest = store.load(run_id)          # FileNotFoundError if the run is unknown locally
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise RuntimeError("LANGSMITH_API_KEY is not set — cannot export from LangSmith")

    client = client or _default_client()
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    expected = expected_root_count(manifest)
    deadline = monotonic() + poll_timeout
    roots: list[dict] = []
    while True:
        roots = fetch_run_tree(client, project, run_id)
        if expected == 0 or len(roots) >= expected:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    fetched = len(roots)
    if fetched == 0:
        return ExportResult(run_id=run_id, path="", complete=False,
                            expected_roots=expected, fetched_roots=0)

    complete = fetched >= expected
    envelope = build_envelope(
        run_id, manifest.workflow, project, manifest, roots,
        complete=complete, expected=expected, fetched=fetched, now=now(),
    )
    path = store.run_dir(run_id) / "export.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("export.json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)                  # atomic, matching RunStore.save
    return ExportResult(run_id=run_id, path=str(path), complete=complete,
                        expected_roots=expected, fetched_roots=fetched)


def resolve_run_ids(
    home: str | None, *, run_id: str | None = None,
    latest: str | None = None, all_workflow: str | None = None,
) -> list[str]:
    """Resolve exactly one selector to run ids. ``--latest`` -> newest matching run; ``--all`` -> all."""
    provided = [x for x in (run_id, latest, all_workflow) if x]
    if len(provided) != 1:
        raise ValueError("provide exactly one of: <run_id>, --latest <workflow>, --all <workflow>")
    if run_id:
        return [run_id]
    name = latest or all_workflow
    matches = [m.run_id for m in RunStore(home).list() if m.workflow == name]  # newest-first
    if not matches:
        raise ValueError(f"no runs found for workflow {name!r}")
    return matches[:1] if latest else matches
```

- [ ] **Step 4: Run the pipeline tests**

Run: `.venv/bin/python -m pytest tests/test_export.py -v`
Expected: PASS (all — helpers + pipeline + selectors).

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/export.py tests/test_export.py
git commit -m "feat(observability): export_run fetch/poll pipeline + run-id resolution"
```

---

### Task 6: `atom workflow export` CLI subcommand + README

**Files:**
- Modify: `src/atom/cli.py` (add `@workflow_app.command("export")` after `workflow_runs`)
- Modify: `README.md` (document the command under Workflows)
- Test: `tests/test_cli_export.py`

**Interfaces:**
- Consumes: `export_run`, `resolve_run_ids`, `ExportResult` (Tasks 4–5); `load_config`; the Typer `workflow_app` (`src/atom/cli.py:132`).
- Produces: the `atom workflow export` command; exit codes per spec §9.

- [ ] **Step 1: Write the failing CLI tests**

Create `tests/test_cli_export.py`:

```python
"""CLI wiring for `atom workflow export` (export_run/resolve_run_ids are stubbed)."""
from __future__ import annotations

import atom.observability.export as export_mod
from atom.cli import app
from atom.observability.export import ExportResult
from typer.testing import CliRunner

runner = CliRunner()


def _ok(run_id, **kw):
    return ExportResult(run_id=run_id, path=f"/x/{run_id}/export.json",
                        complete=kw.get("complete", True),
                        expected_roots=kw.get("expected", 1), fetched_roots=kw.get("fetched", 1))


def test_export_single_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(export_mod, "resolve_run_ids",
                        lambda home, **kw: [kw.get("run_id")] if kw.get("run_id") else [])
    def fake_export_run(home, run_id, *, project, **kw):
        seen["run_id"] = run_id; seen["project"] = project
        return _ok(run_id)
    monkeypatch.setattr(export_mod, "export_run", fake_export_run)

    res = runner.invoke(app, ["workflow", "export", "abc123", "--project", "proj"])
    assert res.exit_code == 0
    assert seen == {"run_id": "abc123", "project": "proj"}
    assert "exported abc123" in res.stdout


def test_export_requires_a_selector(monkeypatch):
    def boom(home, **kw):
        raise ValueError("provide exactly one of: <run_id>, --latest <workflow>, --all <workflow>")
    monkeypatch.setattr(export_mod, "resolve_run_ids", boom)
    res = runner.invoke(app, ["workflow", "export", "--project", "proj"])
    assert res.exit_code == 1
    assert "exactly one" in res.stdout


def test_export_no_traces_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    monkeypatch.setattr(export_mod, "export_run",
                        lambda home, rid, *, project, **kw: _ok(rid, fetched=0, complete=False))
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 1
    assert "no traces found" in res.stdout


def test_export_partial_warns_but_exits_0(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    monkeypatch.setattr(export_mod, "export_run",
                        lambda home, rid, *, project, **kw: _ok(rid, fetched=1, expected=2, complete=False))
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 0
    assert "partial" in res.stdout


def test_export_missing_key_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    def no_key(home, rid, *, project, **kw):
        raise RuntimeError("LANGSMITH_API_KEY is not set — cannot export from LangSmith")
    monkeypatch.setattr(export_mod, "export_run", no_key)
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 1
    assert "LANGSMITH_API_KEY" in res.stdout
```

Note: the command must resolve `export_run`/`resolve_run_ids` by attribute access on `atom.observability.export` at call time (i.e. `from atom.observability import export as export_mod` then `export_mod.export_run(...)`), so these monkeypatches take effect.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_cli_export.py -v`
Expected: FAIL — the `export` subcommand does not exist (`Error: No such command 'export'`, non-zero exit for the happy-path assertions).

- [ ] **Step 3: Implement the subcommand**

In `src/atom/cli.py`, add this command immediately after `workflow_runs` (after line ~216, before the `@app.command()` at line 219):

```python
@workflow_app.command("export")
def workflow_export(
    run_id: str = typer.Argument(None, help="Run id to export."),
    latest: str = typer.Option(None, "--latest", help="Export the newest run of this workflow."),
    all_workflow: str = typer.Option(None, "--all", help="Export every run of this workflow."),
    project: str = typer.Option(None, "--project", help="LangSmith project (default: observability.project)."),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Download a run's LangSmith traces to runs/<run_id>/export.json (for offline evaluation)."""
    from atom.observability import export as export_mod

    _load_env()
    cfg = load_config(config)
    proj = project or cfg.observability.project
    if not proj:
        console.print("[red]no LangSmith project — set observability.project or pass --project[/red]")
        raise typer.Exit(1)

    try:
        run_ids = export_mod.resolve_run_ids(
            cfg.home, run_id=run_id, latest=latest, all_workflow=all_workflow
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    errors = False
    for rid in run_ids:
        try:
            result = export_mod.export_run(cfg.home, rid, project=proj)
        except FileNotFoundError:
            console.print(f"[red]run '{rid}' not found[/red]")
            errors = True
            continue
        except RuntimeError as e:                       # missing API key — abort the whole command
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if result.fetched_roots == 0:
            console.print(
                f"[red]no traces found for {rid} — was observability enabled when it ran?[/red]"
            )
            errors = True
            continue
        if not result.complete:
            console.print(
                f"[yellow]partial: {rid} {result.fetched_roots}/{result.expected_roots} "
                f"task traces (async ingestion may still be catching up)[/yellow]"
            )
        console.print(f"exported {rid} → {result.path}")
    if errors:
        raise typer.Exit(1)
```

- [ ] **Step 4: Run the CLI tests**

Run: `.venv/bin/python -m pytest tests/test_cli_export.py -v`
Expected: PASS (all five).

- [ ] **Step 5: Document the command in the README**

In `README.md`, under the Workflows section (near where `atom workflow run`/`runs` are described), add:

```markdown
#### Exporting a run for offline evaluation

If the run was executed with observability enabled (`observability.enabled: true` and a
`LANGSMITH_API_KEY` in the environment), download its full LangSmith trace tree to disk:

    atom workflow export <run_id>              # one run by id
    atom workflow export --latest <workflow>   # newest run of a workflow
    atom workflow export --all <workflow>      # every run of a workflow

This writes `$ATOM_HOME/workflows/runs/<run_id>/export.json` — a self-contained record holding the
raw LangSmith run tree (lead tasks plus nested sub-agent and per-LLM-call runs, with reasoning/thinking
blocks intact), the run's `run.json` manifest (inputs + per-task verdict), and a `complete` flag. It is
the input to the separate offline evaluation pipeline. Runs executed without observability have nothing
to download (the command reports "no traces found").
```

- [ ] **Step 6: Run the whole suite (final regression gate)**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all tests, including the pre-existing suite).

- [ ] **Step 7: Commit**

```bash
git add src/atom/cli.py README.md tests/test_cli_export.py
git commit -m "feat(observability): add 'atom workflow export' + README docs"
```

---

## Self-Review

**1. Spec coverage:**
- §4 package split → Task 1. `ObservabilityStatus` → Task 2. `export.py` → Tasks 4–5.
- §5 CLI (`<run_id>`/`--latest`/`--all`/`--project`) → Task 6 + `resolve_run_ids` (Task 5).
- §6 envelope (raw roots + `atom_manifest` + `complete`/`expected`/`fetched` + `langsmith_sdk`) → `build_envelope` (Task 4), asserted in Task 4 & 5 tests.
- §7 data flow (manifest oracle, fetch-by-run_id metadata, `read_run(load_child_runs=True)`, poll, atomic write, zero-trace no-write, partial) → `export_run` (Task 5) + tests.
- §8 flush (Task 3) + activation logging (Task 2).
- §9 error table → CLI (Task 6) + `export_run`/`resolve_run_ids` raises (Task 5), all asserted.
- §10 test list → covered across Tasks 2–6.
- §11 migration (imports unchanged via re-export; no new dep) → Task 1 guard test.
All spec sections map to a task. No gaps.

**2. Placeholder scan:** No TBD/TODO/"handle errors"/"similar to". Every code step shows complete code; every test step shows the assertions; every run step gives an exact command + expected result. The one deferred item from the spec (exact metadata filter-DSL string) is pinned concretely here in `fetch_run_tree` (`and(eq(metadata_key, "run_id"), eq(metadata_value, ...))`), verified against the ctx7 LangSmith docs and `langsmith==0.9.7`.

**3. Type consistency:** `ExportResult` fields (`run_id`, `path`, `complete`, `expected_roots`, `fetched_roots`) are identical in Task 4 definition, Task 5 returns, and Task 6 usage. `export_run`/`resolve_run_ids`/`fetch_run_tree`/`expected_root_count`/`build_envelope` signatures match between their definitions (Tasks 4–5) and call sites (Tasks 5–6 + tests). `ObservabilityStatus(active, project, reason)` is consistent between Task 2's definition, the engine's `.active`/`.reason` reads, and the tests. The CLI option is `all_workflow` (flag `--all`), matching `resolve_run_ids(all_workflow=...)`. Consistent throughout.
