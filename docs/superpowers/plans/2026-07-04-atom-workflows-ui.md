# atom Workflows UI + present_files Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture `present_files` deliverables in the workflow engine, serve them safely, and rebuild the atom-ui into a professional two-pane console that scales to hundreds of concurrent runs.

**Architecture:** Backend (strict TDD) — the engine reads each task's `result.state["artifacts"]` and the run store copies those files into a run-local `artifacts/` dir (immutable snapshots), records them on `TaskState.artifacts`, writes a compact per-run `summary.json`, and exposes a paginated/filtered summaries list plus a presented-artifacts list and confined content endpoint. Frontend (manual test surface) — a Vite/React/TS SPA with an Inter typographic system, a Workflows/Runs nav, a paginated Runs dashboard driven by one poll, and a two-pane run console with a markdown/image/code deliverable viewer.

**Tech Stack:** Python 3.11+, FastAPI, pydantic v2, pytest; React 18 + TypeScript + Vite, `react-markdown`, `remark-gfm`, `@fontsource/inter`.

## Global Constraints

- **Deliverables-only:** the UI's artifact surface shows only files presented via `present_files`; no `rglob` of the workspace.
- **Copy-at-capture:** presented files are copied into `runs/<run_id>/artifacts/s<step>__<task>/<name>` at capture time; capture is best-effort and must never flip a succeeded task to failed.
- **Artifact rel format:** exactly `s<step_index>__<task_id>/<name>` (e.g. `s0__poet_a/poem_a.md`). Basename collisions within one task disambiguate as `name.md`, `name-1.md`.
- **List endpoint:** `GET /api/runs?status=<active|complete|halted|all>&limit=&offset=` → `{items, total, counts:{active,complete,halted}}`; defaults `status="all"`, `limit=50`, `offset=0`. `active` = status in (`pending`,`running`).
- **`summary.json`** is a cheap cache; `run.json` is authoritative and written first. Missing/corrupt summary falls back to deriving from `run.json`.
- **O(1) UI request volume:** the Runs dashboard uses one list poll regardless of concurrent-run count; only the open run polls its full manifest.
- **Frontend deps (exact):** `react-markdown`, `remark-gfm`, `@fontsource/inter`. No syntax-highlighting dependency.
- **Typography:** Inter (self-hosted via `@fontsource/inter`) for UI; system monospace stack for transcripts/code. Light, near-neutral palette; semantic status colors.
- **Testing:** all Python is TDD; the React SPA is the manual surface (build clean + eyeball), not TDD.
- **Non-goal:** engine-side global concurrency limiting (see spec §9) — do not implement.
- **Do not commit `atom-ui/dist/`** (git-ignored build artifact).

## File Structure

**Backend (modify):**
- `src/atom/workflow/run_store.py` — add `ArtifactRef`, `RunSummary`, `TaskState.artifacts`, `summarize()`, `artifacts_dir()`, `capture_artifacts()`, `artifact_path()`, `summary.json` writes in `save()`, `list_summaries()`.
- `src/atom/workflow/engine.py` — `_run_task` captures artifacts after `save_chat`.
- `src/atom/api/app.py` — paginated `GET /api/runs`; presented-artifacts list; `FileResponse` content endpoint.
- `workflows/parallel-poems.yaml` — call `present_files`; add anthology.

**Frontend (create/replace):**
- `atom-ui/package.json` — add the three deps.
- `atom-ui/src/main.tsx` — import Inter font weights.
- `atom-ui/src/styles.css` — full design system (replace).
- `atom-ui/src/api.ts` — types + client + `artifactUrl` (replace).
- `atom-ui/src/ui.tsx` — shared presentational helpers (create).
- `atom-ui/src/App.tsx` — shell + Workflows/Runs nav + view routing (replace).
- `atom-ui/src/Workflows.tsx` — `Workflows` list + `RunForm` (create).
- `atom-ui/src/RunsDashboard.tsx` — paginated/filtered dashboard (create).
- `atom-ui/src/RunView.tsx` — two-pane console + transcript + deliverable viewer (create).

**Tests:** `tests/test_workflow_run_store.py`, `tests/test_workflow_engine.py`, `tests/test_workflow_api.py`.

---

### Task 1: Run store — artifact capture (`ArtifactRef`, `capture_artifacts`, `artifact_path`)

**Files:**
- Modify: `src/atom/workflow/run_store.py`
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Consumes: existing `RunStore` (`run_dir`, `workspace_dir`), `TaskState`.
- Produces:
  - `class ArtifactRef(BaseModel): name: str; path: str; rel: str; size: int`
  - `TaskState.artifacts: list[ArtifactRef]` (default `[]`)
  - `RunStore.artifacts_dir(run_id) -> Path`
  - `RunStore.capture_artifacts(run_id, step_index: int, task_id: str, presented: list[dict]) -> list[ArtifactRef]`
  - `RunStore.artifact_path(run_id, rel: str) -> Optional[Path]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_run_store.py`:

```python
from atom.workflow.run_store import ArtifactRef  # add to existing import line


def test_capture_artifacts_copies_and_snapshots(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rc", store.workspace_dir("rc")))
    src = store.workspace_dir("rc") / "poem.md"
    src.write_text("draft\n")
    refs = store.capture_artifacts(
        "rc", 0, "poet_a",
        [{"path": "/mnt/user-data/outputs/poem.md", "physical": str(src)}],
    )
    assert len(refs) == 1
    assert refs[0].name == "poem.md"
    assert refs[0].rel == "s0__poet_a/poem.md"
    assert refs[0].size == len("draft\n")
    dest = store.artifacts_dir("rc") / "s0__poet_a" / "poem.md"
    assert dest.read_text() == "draft\n"
    src.write_text("CHANGED\n")               # snapshot immutability
    assert dest.read_text() == "draft\n"


def test_capture_artifacts_skips_missing_source(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rm", store.workspace_dir("rm")))
    refs = store.capture_artifacts(
        "rm", 0, "t1",
        [{"path": "/mnt/x/gone.md", "physical": str(store.workspace_dir("rm") / "nope.md")}],
    )
    assert refs == []


def test_capture_artifacts_disambiguates_collision(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rd", store.workspace_dir("rd")))
    a = store.workspace_dir("rd") / "a.md"; a.write_text("A\n")
    sub = store.workspace_dir("rd") / "sub"; sub.mkdir()
    b = sub / "a.md"; b.write_text("B\n")
    refs = store.capture_artifacts("rd", 0, "t1", [
        {"path": "/mnt/x/a.md", "physical": str(a)},
        {"path": "/mnt/y/a.md", "physical": str(b)},
    ])
    assert sorted(r.name for r in refs) == ["a-1.md", "a.md"]


def test_artifact_path_confined(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rp", store.workspace_dir("rp")))
    ok = store.artifact_path("rp", "s0__t1/f.md")
    assert ok is not None and str(ok).endswith("/artifacts/s0__t1/f.md")
    assert store.artifact_path("rp", "../../run.json") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_workflow_run_store.py -k "capture or artifact_path" -q`
Expected: FAIL (ImportError `ArtifactRef` / `AttributeError: 'RunStore' object has no attribute 'capture_artifacts'`).

- [ ] **Step 3: Implement in `src/atom/workflow/run_store.py`**

Add `import shutil` next to the existing `import os` at the top. Add the model after `TaskState` is defined is fine, but it is referenced by `TaskState`, so define `ArtifactRef` **above** `TaskState` and add the field:

```python
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
```

Add these methods to `RunStore` (near `workspace_dir`):

```python
    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def capture_artifacts(
        self, run_id: str, step_index: int, task_id: str, presented: list[dict]
    ) -> list["ArtifactRef"]:
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
        base = self.artifacts_dir(run_id).resolve()
        target = (base / rel).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            return None
        return target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_workflow_run_store.py -k "capture or artifact_path" -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(workflow): capture presented artifacts via copy-at-capture snapshots"
```

---

### Task 2: Engine — wire artifact capture into `_run_task`

**Files:**
- Modify: `src/atom/workflow/engine.py:210-214`
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: `RunStore.capture_artifacts` (Task 1); `RunResult.state` (a dict) from `run_agent`; `present_files` tool (bound by default; call args `{"filepaths": [...]}`).
- Produces: after a successful task, `ts.artifacts` is populated and files exist under `store.artifacts_dir(...)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workflow_engine.py` (helpers `_tc`, `_write_call`, `WS`, `_one_task_workflow`, `_draft_then_refine` already exist):

```python
def _present_call(paths, cid):
    return AIMessage(content="", tool_calls=[_tc("present_files", {"filepaths": paths}, cid)])


@pytest.mark.asyncio
async def test_presented_artifacts_captured(base_config, atom_home):
    scripts = {"t1": [
        _write_call(f"{WS}/out.md", "hi\n", "w1"),
        _present_call([f"{WS}/out.md"], "p1"),
        AIMessage(content="done"),
    ]}
    engine = WorkflowEngine(
        base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])))
    engine.create_run(_one_task_workflow(), {}, "runart", "2026-07-03T00:00:00")
    manifest = await engine.execute("runart")

    assert manifest.status == "complete"
    arts = manifest.steps[0].tasks[0].artifacts
    assert len(arts) == 1 and arts[0].name == "out.md" and arts[0].rel == "s0__t1/out.md"
    assert (engine.store.artifacts_dir("runart") / "s0__t1" / "out.md").read_text() == "hi\n"


@pytest.mark.asyncio
async def test_draft_artifact_snapshot_survives_refine_overwrite(base_config, atom_home):
    scripts = {
        "poet_a": [_write_call(f"{WS}/poem_a.md", "draft\n", "w1"),
                   _present_call([f"{WS}/poem_a.md"], "p1"), AIMessage(content="d")],
        "refiner": [_write_call(f"{WS}/poem_a.md", "refined\n", "w2"),
                    _present_call([f"{WS}/poem_a.md"], "p2"), AIMessage(content="r")],
    }
    engine = WorkflowEngine(
        base_config, prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])))
    engine.create_run(_draft_then_refine(), {"topic": "sea"}, "runsnap", "2026-07-03T00:00:00")
    manifest = await engine.execute("runsnap")

    assert manifest.status == "complete"
    ad = engine.store.artifacts_dir("runsnap")
    assert (ad / "s0__poet_a" / "poem_a.md").read_text() == "draft\n"      # snapshot preserved
    assert (ad / "s1__refiner" / "poem_a.md").read_text() == "refined\n"
    assert (engine.store.workspace_dir("runsnap") / "poem_a.md").read_text() == "refined\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_workflow_engine.py -k "presented or snapshot" -q`
Expected: FAIL (`arts` is `[]` / snapshot file missing — capture not wired).

- [ ] **Step 3: Implement**

In `src/atom/workflow/engine.py`, in `_run_task`, replace the success block (currently `save_chat` then `ts.status = "succeeded"`) with:

```python
            result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            self.store.save_chat(
                manifest.run_id, step_state.index, ts.id, serialize_messages(result.messages)
            )
            presented = (result.state or {}).get("artifacts", [])
            ts.artifacts = self.store.capture_artifacts(
                manifest.run_id, step_state.index, ts.id, presented,
            )
            ts.status = "succeeded"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_workflow_engine.py -q`
Expected: PASS (all engine tests, including the two new ones).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "feat(workflow): persist each task's presented artifacts on TaskState"
```

---

### Task 3: Run store — compact summaries (`RunSummary`, `summary.json`, `list_summaries`)

**Files:**
- Modify: `src/atom/workflow/run_store.py`
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Consumes: existing `RunStore.save`, `RunManifest`.
- Produces:
  - `class RunSummary(BaseModel)` with fields `run_id, workflow, status, created_at, ended_at(Optional), steps_total, steps_done, tasks_total, tasks_done, current_step(Optional)`.
  - `summarize(manifest) -> RunSummary`
  - `RunStore.list_summaries(status=None, limit=50, offset=0) -> dict` returning `{items, total, counts}`.
  - `RunStore.save` also writes `summary.json`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_run_store.py`:

```python
import json as _json


def _save_with_status(store, run_id, status, created_at,
                      step_status="complete", task_status="succeeded"):
    m = _manifest(run_id, store.workspace_dir(run_id))
    m.created_at = created_at
    m.status = status
    m.steps[0].status = step_status
    m.steps[0].tasks[0].status = task_status
    store.create(m)
    return m


def test_summary_json_written_on_save(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("sm1", store.workspace_dir("sm1")))
    sp = store.run_dir("sm1") / "summary.json"
    assert sp.exists()
    data = _json.loads(sp.read_text())
    assert data["run_id"] == "sm1" and data["tasks_total"] == 1


def test_list_summaries_counts_filter_pagination(atom_home):
    store = RunStore(str(atom_home))
    _save_with_status(store, "r_run", "running", "2026-07-01T00:00:00",
                      step_status="running", task_status="running")
    _save_with_status(store, "r_done", "complete", "2026-07-02T00:00:00")
    _save_with_status(store, "r_halt", "halted", "2026-07-03T00:00:00",
                      step_status="failed", task_status="failed")

    page = store.list_summaries()
    assert page["counts"] == {"active": 1, "complete": 1, "halted": 1}
    assert page["total"] == 3
    assert [i["run_id"] for i in page["items"]] == ["r_halt", "r_done", "r_run"]

    active = store.list_summaries(status="active")
    assert [i["run_id"] for i in active["items"]] == ["r_run"] and active["total"] == 1

    pg2 = store.list_summaries(limit=1, offset=1)
    assert [i["run_id"] for i in pg2["items"]] == ["r_done"] and pg2["total"] == 3


def test_list_summaries_fallback_when_summary_missing(atom_home):
    store = RunStore(str(atom_home))
    _save_with_status(store, "r_x", "complete", "2026-07-01T00:00:00")
    (store.run_dir("r_x") / "summary.json").unlink()
    page = store.list_summaries()
    assert [i["run_id"] for i in page["items"]] == ["r_x"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_workflow_run_store.py -k "summary or list_summaries" -q`
Expected: FAIL (`summary.json` not written / `list_summaries` missing).

- [ ] **Step 3: Implement in `src/atom/workflow/run_store.py`**

Add the model + helper after `RunManifest`:

```python
class RunSummary(BaseModel):
    run_id: str
    workflow: str
    status: str
    created_at: str
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
        created_at=manifest.created_at, ended_at=manifest.ended_at,
        steps_total=len(manifest.steps),
        steps_done=sum(1 for s in manifest.steps if s.status == "complete"),
        tasks_total=len(tasks),
        tasks_done=sum(1 for t in tasks if t.status == "succeeded"),
        current_step=next((s.title for s in manifest.steps if s.status != "complete"), None),
    )
```

Add a module-level constant near the top (after imports): `_ACTIVE = ("pending", "running")`.

Extend `RunStore.save` to also write the summary, and add the summary path + list method:

```python
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
        empty = {"items": [], "total": 0, "counts": {"active": 0, "complete": 0, "halted": 0}}
        if not self.runs_dir.is_dir():
            return empty
        summaries: list[RunSummary] = []
        for d in self.runs_dir.iterdir():
            s = self._read_summary(d)
            if s is not None:
                summaries.append(s)
        counts = {"active": 0, "complete": 0, "halted": 0}
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_workflow_run_store.py -q`
Expected: PASS (all run-store tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(workflow): compact per-run summaries + paginated list_summaries"
```

---

### Task 4: API — paginated summaries, presented-artifacts list, `FileResponse` content

**Files:**
- Modify: `src/atom/api/app.py:69-108`
- Test: `tests/test_workflow_api.py`

**Interfaces:**
- Consumes: `store.list_summaries` (Task 3), `store.artifact_path` (Task 1), `TaskState.artifacts` (Task 1).
- Produces:
  - `GET /api/runs?status=&limit=&offset=` → `{items, total, counts}`.
  - `GET /api/runs/{id}/artifacts` → `list[{name, path, rel, size, step, task}]`.
  - `GET /api/runs/{id}/artifacts/{rel:path}` → `FileResponse` (media type guessed).

- [ ] **Step 1: Update the existing test + add new ones**

In `tests/test_workflow_api.py`, replace `_provider` so it presents the file, and replace the artifact assertions in `test_submit_run_and_fetch_results`. Then add two tests.

Replace `_provider`:

```python
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
```

Replace the artifacts block (old lines 61-64) of `test_submit_run_and_fetch_results` with:

```python
        arts = (await client.get(f"/api/runs/{run_id}/artifacts")).json()
        art = next(a for a in arts if a["name"] == "out.txt")
        assert art["step"] == 0 and art["task"] == "t1" and art["rel"] == "s0__t1/out.txt"
        body = (await client.get(f"/api/runs/{run_id}/artifacts/{art['rel']}")).text
        assert body == "hi\n"
```

Add:

```python
@pytest.mark.asyncio
async def test_runs_list_returns_paginated_summaries(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        run_id = r.json()["run_id"]
        await _poll(client, run_id)
        resp = await client.get(f"/api/runs/{run_id}/artifacts/s0__t1/does-not-exist.txt")
        assert resp.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_workflow_api.py -q`
Expected: FAIL (`/api/runs` returns a list not `{items,...}`; artifacts lack `step`/`rel`).

- [ ] **Step 3: Implement in `src/atom/api/app.py`**

Add imports near the top:

```python
import mimetypes

from fastapi.responses import FileResponse, PlainTextResponse
```

Replace `get_runs` and both artifact endpoints (the block from `@app.get("/api/runs")` through the end of `get_artifact`) with:

```python
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
        return FileResponse(target, media_type=media_type)
```

(The existing `get_run` and `get_messages` definitions that sat between the old endpoints are reproduced above so the block stays contiguous; delete the originals to avoid duplicate route functions. `PlainTextResponse` is no longer used — drop it from the import if your linter flags it, otherwise leaving the import is harmless.)

- [ ] **Step 4: Run the full backend suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (previous 107 + the new tests; zero failures).

- [ ] **Step 5: Commit**

```bash
git add src/atom/api/app.py tests/test_workflow_api.py
git commit -m "feat(api): paginated run summaries + presented-artifact list/content endpoints"
```

---

### Task 5: Frontend — dependencies, fonts, design system

**Files:**
- Modify: `atom-ui/package.json`, `atom-ui/src/main.tsx`
- Replace: `atom-ui/src/styles.css`

**Interfaces:**
- Produces: Inter font loaded; the full CSS token/class system consumed by Tasks 6-8. No TS symbols.

- [ ] **Step 1: Install the dependencies**

Run:
```bash
cd atom-ui && npm install react-markdown@^9 remark-gfm@^4 @fontsource/inter@^5
```
Expected: `package.json` gains the three deps under `dependencies`; `node_modules` updated.

- [ ] **Step 2: Import Inter in `atom-ui/src/main.tsx`**

Replace the file with:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 3: Replace `atom-ui/src/styles.css`**

```css
:root {
  --bg: #fafaf9; --surface: #ffffff; --surface-2: #f5f5f4; --surface-3: #ececeb;
  --border: #e7e5e4; --border-strong: #d6d3d1;
  --ink: #1c1917; --ink-2: #57534e; --ink-3: #a8a29e;
  --accent: #4f46e5; --accent-weak: #eef2ff;
  --ok: #15803d; --ok-weak: #dcfce7; --warn: #b45309; --warn-weak: #fef3c7;
  --err: #b91c1c; --err-weak: #fee2e2; --idle: #78716c; --idle-weak: #f0efee;
  --radius: 10px; --radius-sm: 7px;
  --shadow: 0 1px 2px rgba(28,25,23,.04), 0 1px 3px rgba(28,25,23,.06);
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body, #root { height: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: "Inter", system-ui, -apple-system, sans-serif;
  font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
  font-feature-settings: "cv02", "cv03", "cv04", "cv11";
}
button { font-family: inherit; font-size: inherit; cursor: pointer; }
h1 { font-size: 22px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 4px; }
.app { display: flex; flex-direction: column; min-height: 100%; }

/* Top bar */
.topbar {
  display: flex; align-items: center; gap: 24px; height: 52px; padding: 0 20px;
  background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10;
}
.brand { display: flex; align-items: center; gap: 7px; font-weight: 680; letter-spacing: -0.01em; cursor: pointer; }
.brand .glyph { color: var(--accent); font-size: 17px; }
.tabs { display: flex; gap: 2px; }
.tabs button {
  border: 0; background: transparent; color: var(--ink-2); padding: 7px 12px;
  border-radius: var(--radius-sm); font-weight: 550; display: inline-flex; align-items: center; gap: 7px;
}
.tabs button:hover { background: var(--surface-2); color: var(--ink); }
.tabs button.on { background: var(--accent-weak); color: var(--accent); }
.count { background: var(--accent); color: #fff; border-radius: 999px; font-size: 11px; font-weight: 650; padding: 1px 7px; }

main { flex: 1; min-height: 0; }
.page { max-width: 900px; margin: 0 auto; padding: 32px 24px; }
.page.narrow { max-width: 560px; }
.page.wide { max-width: 1100px; }
.sub { color: var(--ink-2); margin: 0 0 20px; }
.link { border: 0; background: transparent; color: var(--ink-2); padding: 4px 0; margin-bottom: 12px; font-weight: 550; }
.link:hover { color: var(--accent); }
.empty { color: var(--ink-3); padding: 24px; text-align: center; }
.error { color: var(--err); background: var(--err-weak); border-radius: var(--radius-sm); padding: 8px 12px; margin-top: 12px; font-size: 13px; }

/* Workflow cards */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
.wf-card {
  text-align: left; border: 1px solid var(--border); background: var(--surface);
  border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); transition: border-color .12s, transform .12s;
}
.wf-card:hover { border-color: var(--border-strong); transform: translateY(-1px); }
.wf-name { font-weight: 620; margin-bottom: 4px; }
.wf-desc { color: var(--ink-2); font-size: 13px; min-height: 34px; }
.wf-meta { color: var(--ink-3); font-size: 12px; margin-top: 10px; }

/* Form */
.field { display: block; margin: 16px 0; }
.field-label { display: flex; align-items: center; gap: 8px; font-weight: 570; margin-bottom: 4px; }
.req { color: var(--warn); background: var(--warn-weak); font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 999px; }
.field-hint { display: block; color: var(--ink-2); font-size: 12.5px; margin-bottom: 6px; }
.field input {
  width: 100%; padding: 9px 11px; background: var(--surface); color: var(--ink);
  border: 1px solid var(--border-strong); border-radius: var(--radius-sm); font-size: 14px;
}
.field input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-weak); }
button.primary { background: var(--accent); color: #fff; border: 0; border-radius: var(--radius-sm); padding: 10px 18px; font-weight: 570; }
button.primary:disabled { opacity: .6; cursor: default; }

/* Status pill + dot */
.pill { display: inline-flex; align-items: center; font-size: 11.5px; font-weight: 600; padding: 2px 9px; border-radius: 999px; text-transform: capitalize; }
.pill.ok { background: var(--ok-weak); color: var(--ok); }
.pill.warn { background: var(--warn-weak); color: var(--warn); }
.pill.err { background: var(--err-weak); color: var(--err); }
.pill.idle { background: var(--idle-weak); color: var(--idle); }
.dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; flex: none; background: var(--idle); }
.dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-weak); }
.dot.err { background: var(--err); } .dot.idle { background: var(--ink-3); }

/* Runs dashboard */
.filters { display: flex; gap: 6px; margin: 4px 0 16px; }
.chip { border: 1px solid var(--border); background: var(--surface); color: var(--ink-2); padding: 5px 12px; border-radius: 999px; font-weight: 550; text-transform: capitalize; display: inline-flex; gap: 7px; align-items: center; }
.chip:hover { border-color: var(--border-strong); }
.chip.on { background: var(--ink); color: #fff; border-color: var(--ink); }
.chip-n { background: rgba(255,255,255,.18); border-radius: 999px; font-size: 11px; padding: 0 6px; }
.chip:not(.on) .chip-n { background: var(--surface-3); color: var(--ink-2); }
table.runs { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
table.runs th { text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-3); font-weight: 600; padding: 10px 14px; border-bottom: 1px solid var(--border); }
table.runs td { padding: 11px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
table.runs tbody tr { cursor: pointer; }
table.runs tbody tr:hover { background: var(--surface-2); }
table.runs tbody tr:last-child td { border-bottom: 0; }
.mono-cell { font-weight: 560; }
.rid { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); }
.dim { color: var(--ink-3); }
.pager { display: flex; align-items: center; gap: 14px; justify-content: flex-end; margin-top: 14px; }
.pager button { border: 1px solid var(--border-strong); background: var(--surface); border-radius: var(--radius-sm); padding: 6px 12px; color: var(--ink); }
.pager button:disabled { opacity: .45; cursor: default; }

/* Run view */
.runview { display: flex; flex-direction: column; height: calc(100vh - 52px); }
.run-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 20px; border-bottom: 1px solid var(--border); background: var(--surface); }
.crumbs { display: flex; align-items: center; gap: 10px; }
.crumbs .link { margin: 0; }
.crumbs .sep { color: var(--ink-3); }
.run-status { display: flex; align-items: center; gap: 14px; font-size: 13px; }
.loading { padding: 40px; color: var(--ink-3); }
.run-body { flex: 1; min-height: 0; display: grid; grid-template-columns: 300px 1fr; }
.rail { border-right: 1px solid var(--border); overflow-y: auto; padding: 14px; background: var(--surface); }
.rail-step { margin-bottom: 16px; }
.rail-step-head { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.step-idx { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-3); }
.rail-task { display: flex; align-items: center; gap: 9px; width: 100%; text-align: left; border: 0; background: transparent; padding: 7px 9px; border-radius: var(--radius-sm); color: var(--ink); }
.rail-task:hover { background: var(--surface-2); }
.rail-task.on { background: var(--accent-weak); }
.rail-task-id { flex: 1; font-weight: 540; }
.tag { font-family: var(--mono); font-size: 11px; color: var(--ink-2); background: var(--surface-3); padding: 1px 6px; border-radius: 5px; }
.rail-deliverables { border-top: 1px solid var(--border); padding-top: 12px; }
.rail-h { font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-3); font-weight: 600; margin-bottom: 8px; }
.rail-empty { color: var(--ink-3); font-size: 13px; padding: 4px 9px; }
.rail-art { display: flex; flex-direction: column; align-items: flex-start; width: 100%; text-align: left; border: 0; background: transparent; padding: 6px 9px; border-radius: var(--radius-sm); }
.rail-art:hover { background: var(--surface-2); }
.rail-art.on { background: var(--accent-weak); }
.art-name { font-weight: 540; }
.art-meta { font-size: 11.5px; color: var(--ink-3); }

.center { min-width: 0; display: flex; flex-direction: column; }
.tabbar { display: flex; gap: 2px; padding: 10px 16px 0; border-bottom: 1px solid var(--border); background: var(--surface); }
.tabbar button { border: 0; background: transparent; color: var(--ink-2); padding: 8px 12px; border-bottom: 2px solid transparent; font-weight: 550; margin-bottom: -1px; }
.tabbar button:hover { color: var(--ink); }
.tabbar button.on { color: var(--accent); border-bottom-color: var(--accent); }
.placeholder { padding: 40px; color: var(--ink-3); }

/* Transcript */
.transcript { flex: 1; overflow-y: auto; padding: 18px 20px; }
.msg { margin: 0 0 14px; }
.msg-role { font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em; color: var(--ink-3); font-weight: 600; margin-bottom: 4px; }
.msg.human .msg-text { background: var(--accent-weak); }
.msg-text { white-space: pre-wrap; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px 12px; }
.msg.ai .msg-text { background: var(--surface); }
.tool-calls { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px; }
.tool-calls .msg-text { background: transparent; border: 0; padding: 0 0 6px; }
.toolcall { display: flex; gap: 10px; font-family: var(--mono); font-size: 12.5px; padding: 3px 0; }
.tc-name { color: var(--ink-2); white-space: nowrap; }
.tc-args { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.toolcall.present .tc-name { color: var(--accent); font-weight: 600; }
.msg.tool .msg-text { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); }

/* Deliverables */
.gallery { flex: 1; overflow-y: auto; padding: 18px 20px; display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; align-content: start; }
.gal-card { text-align: left; border: 1px solid var(--border); background: var(--surface); border-radius: var(--radius); padding: 14px; box-shadow: var(--shadow); }
.gal-card:hover { border-color: var(--border-strong); }
.gal-name { font-weight: 600; margin-bottom: 6px; word-break: break-all; }
.gal-meta { font-size: 12px; color: var(--ink-2); }
.gal-size { font-size: 12px; color: var(--ink-3); margin-top: 4px; }
.viewer { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.viewer-head { display: flex; align-items: center; gap: 14px; padding: 12px 20px; border-bottom: 1px solid var(--border); }
.viewer-head .link { margin: 0; }
.viewer-name { font-weight: 600; }
.art-md, .art-code, .art-img { flex: 1; overflow: auto; padding: 20px 24px; }
.art-code { font-family: var(--mono); font-size: 12.5px; white-space: pre; background: var(--surface-2); margin: 0; }
.art-img { display: flex; align-items: flex-start; justify-content: center; background: var(--surface-2); }
.art-img img { max-width: 100%; height: auto; border-radius: var(--radius-sm); box-shadow: var(--shadow); }
.art-md { max-width: 760px; }
.art-md h1, .art-md h2, .art-md h3 { letter-spacing: -0.01em; margin: 1.2em 0 .5em; }
.art-md h1 { font-size: 24px; } .art-md h2 { font-size: 19px; } .art-md h3 { font-size: 16px; }
.art-md p { margin: 0 0 1em; } .art-md pre { background: var(--surface-2); padding: 12px; border-radius: var(--radius-sm); overflow: auto; font-family: var(--mono); font-size: 12.5px; }
.art-md code { font-family: var(--mono); font-size: .9em; background: var(--surface-2); padding: 1px 5px; border-radius: 4px; }
.art-md pre code { background: transparent; padding: 0; }
.art-md table { border-collapse: collapse; } .art-md th, .art-md td { border: 1px solid var(--border); padding: 6px 10px; }
.art-md blockquote { border-left: 3px solid var(--border-strong); margin: 0 0 1em; padding-left: 14px; color: var(--ink-2); }
```

- [ ] **Step 4: Verify the build is clean**

Run: `cd atom-ui && npm run build`
Expected: `tsc` + `vite build` succeed with no errors (the old `App.tsx` still compiles against the old `api.ts`; only styling changed).

- [ ] **Step 5: Commit**

```bash
git add atom-ui/package.json atom-ui/package-lock.json atom-ui/src/main.tsx atom-ui/src/styles.css
git commit -m "feat(ui): add Inter + markdown deps and professional design system"
```

---

### Task 6: Frontend — API client, shared UI helpers, app shell + Workflows

**Files:**
- Replace: `atom-ui/src/api.ts`, `atom-ui/src/App.tsx`
- Create: `atom-ui/src/ui.tsx`, `atom-ui/src/Workflows.tsx`

**Interfaces:**
- Produces (from `api.ts`): types `InputDef, Workflow, ArtifactRef, TaskState, StepState, Manifest, ChatMsg, RunSummary, RunsPage, Artifact`; `artifactUrl(id, rel)`; `api.{workflows, submit, runs, run, messages, artifacts, artifactText}`.
- Produces (from `ui.tsx`): `StatusPill`, `Dot`, `fmtSize`, `fmtClock`, `elapsed`, `progressText`.
- Produces (from `Workflows.tsx`): `Workflows`, `RunForm`.
- App routes to `RunsDashboard` (Task 7) and `RunView` (Task 8) — created here as **stubs** so the build stays green; Tasks 7-8 replace them.

- [ ] **Step 1: Replace `atom-ui/src/api.ts`**

```ts
export interface InputDef { name: string; required: boolean; description?: string; default?: string; }
export interface Workflow { name: string; description?: string; inputs: InputDef[]; }
export interface ArtifactRef { name: string; path: string; rel: string; size: number; }
export interface TaskState {
  id: string; status: string; model?: string; thinking?: string | number;
  error?: string; artifacts: ArtifactRef[]; started_at?: string; ended_at?: string;
}
export interface StepState { index: number; title: string; status: string; tasks: TaskState[]; }
export interface Manifest {
  run_id: string; workflow: string; status: string; inputs: Record<string, unknown>;
  created_at: string; ended_at?: string; workspace_path: string; steps: StepState[];
}
export interface ChatMsg {
  role: string; text: string; name?: string;
  tool_calls?: { name: string; args?: Record<string, unknown> }[];
}
export interface RunSummary {
  run_id: string; workflow: string; status: string; created_at: string; ended_at?: string;
  steps_total: number; steps_done: number; tasks_total: number; tasks_done: number; current_step?: string;
}
export interface RunsPage { items: RunSummary[]; total: number; counts: { active: number; complete: number; halted: number }; }
export interface Artifact extends ArtifactRef { step: number; task: string; }

const j = async (r: Response) => { if (!r.ok) throw new Error(await r.text()); return r.json(); };

export const artifactUrl = (id: string, rel: string) =>
  `/api/runs/${id}/artifacts/${rel.split("/").map(encodeURIComponent).join("/")}`;

export const api = {
  workflows: (): Promise<Workflow[]> => fetch("/api/workflows").then(j),
  submit: (workflow: string, inputs: Record<string, string>): Promise<{ run_id: string }> =>
    fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow, inputs }),
    }).then(j),
  runs: (status: string, limit: number, offset: number, signal?: AbortSignal): Promise<RunsPage> =>
    fetch(`/api/runs?status=${status}&limit=${limit}&offset=${offset}`, { signal }).then(j),
  run: (id: string): Promise<Manifest> => fetch(`/api/runs/${id}`).then(j),
  messages: (id: string, step: number, task: string): Promise<ChatMsg[]> =>
    fetch(`/api/runs/${id}/tasks/${step}/${task}/messages`).then(j),
  artifacts: (id: string): Promise<Artifact[]> => fetch(`/api/runs/${id}/artifacts`).then(j),
  artifactText: (id: string, rel: string): Promise<string> =>
    fetch(artifactUrl(id, rel)).then(async (r) => { if (!r.ok) throw new Error(await r.text()); return r.text(); }),
};
```

- [ ] **Step 2: Create `atom-ui/src/ui.tsx`**

```tsx
import { RunSummary } from "./api";

export const STATUS_CLASS: Record<string, string> = {
  pending: "idle", running: "warn", succeeded: "ok", failed: "err",
  complete: "ok", halted: "err",
};

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill ${STATUS_CLASS[status] ?? "idle"}`}>{status}</span>;
}

export function Dot({ status }: { status: string }) {
  return <span className={`dot ${STATUS_CLASS[status] ?? "idle"}`} title={status} />;
}

export const fmtSize = (b: number) =>
  b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;

export const fmtClock = (iso?: string) => (iso ? iso.replace("T", " ") : "");

export function elapsed(start?: string, end?: string): string {
  if (!start) return "";
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const sec = Math.max(0, Math.round((e - s) / 1000));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${sec % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export const progressText = (r: RunSummary) =>
  `${r.tasks_done}/${r.tasks_total} tasks` + (r.current_step ? ` · ${r.current_step}` : "");
```

- [ ] **Step 3: Create `atom-ui/src/Workflows.tsx`**

```tsx
import { useEffect, useState } from "react";
import { api, Workflow } from "./api";

export function Workflows({ onPick }: { onPick: (w: Workflow) => void }) {
  const [wfs, setWfs] = useState<Workflow[]>([]);
  const [err, setErr] = useState("");
  useEffect(() => { api.workflows().then(setWfs).catch((e) => setErr(String(e))); }, []);
  return (
    <div className="page">
      <h1>Workflows</h1>
      <p className="sub">Pick a workflow to configure and launch a run.</p>
      {err && <div className="error">{err}</div>}
      <div className="grid">
        {wfs.map((w) => (
          <button key={w.name} className="wf-card" onClick={() => onPick(w)}>
            <div className="wf-name">{w.name}</div>
            <div className="wf-desc">{w.description || "—"}</div>
            <div className="wf-meta">{w.inputs.length} input{w.inputs.length === 1 ? "" : "s"}</div>
          </button>
        ))}
        {!wfs.length && !err && <div className="empty">No workflows found.</div>}
      </div>
    </div>
  );
}

export function RunForm(
  { workflow, onStarted, onBack }:
  { workflow: Workflow; onStarted: (id: string) => void; onBack: () => void },
) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setError(""); setBusy(true);
    try { const { run_id } = await api.submit(workflow.name, values); onStarted(run_id); }
    catch (e) { setError(String(e instanceof Error ? e.message : e)); setBusy(false); }
  };
  return (
    <div className="page narrow">
      <button className="link" onClick={onBack}>← Workflows</button>
      <h1>{workflow.name}</h1>
      {workflow.description && <p className="sub">{workflow.description}</p>}
      {workflow.inputs.map((i) => (
        <label key={i.name} className="field">
          <span className="field-label">{i.name}{i.required && <span className="req">required</span>}</span>
          {i.description && <span className="field-hint">{i.description}</span>}
          <input placeholder={i.default ?? ""} value={values[i.name] ?? ""}
            onChange={(e) => setValues((v) => ({ ...v, [i.name]: e.target.value }))} />
        </label>
      ))}
      <button className="primary" disabled={busy} onClick={submit}>{busy ? "Starting…" : "Start run"}</button>
      {error && <div className="error">{error}</div>}
    </div>
  );
}
```

- [ ] **Step 4: Create stub `atom-ui/src/RunsDashboard.tsx` and `atom-ui/src/RunView.tsx`**

`atom-ui/src/RunsDashboard.tsx`:
```tsx
export function RunsDashboard({ onOpen }: { onOpen: (id: string) => void }) {
  void onOpen;
  return <div className="page wide"><h1>Runs</h1><div className="empty">Dashboard coming in Task 7.</div></div>;
}
```

`atom-ui/src/RunView.tsx`:
```tsx
export function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  return (
    <div className="page">
      <button className="link" onClick={onBack}>← Runs</button>
      <h1>Run {runId}</h1>
      <div className="empty">Run console coming in Task 8.</div>
    </div>
  );
}
```

- [ ] **Step 5: Replace `atom-ui/src/App.tsx`**

```tsx
import { useEffect, useState } from "react";
import { api, Workflow } from "./api";
import { Workflows, RunForm } from "./Workflows";
import { RunsDashboard } from "./RunsDashboard";
import { RunView } from "./RunView";

type View =
  | { name: "workflows" }
  | { name: "form"; workflow: Workflow }
  | { name: "runs" }
  | { name: "run"; runId: string };

export default function App() {
  const [view, setView] = useState<View>({ name: "workflows" });
  const [active, setActive] = useState(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try { const p = await api.runs("all", 1, 0); if (live) setActive(p.counts.active); } catch { /* ignore */ }
      if (live) timer = setTimeout(tick, 4000);
    };
    tick();
    return () => { live = false; clearTimeout(timer); };
  }, []);

  const tab = view.name === "form" ? "workflows" : view.name === "run" ? "runs" : view.name;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand" onClick={() => setView({ name: "workflows" })}>
          <span className="glyph">⚛</span> atom
        </div>
        <nav className="tabs">
          <button className={tab === "workflows" ? "on" : ""} onClick={() => setView({ name: "workflows" })}>Workflows</button>
          <button className={tab === "runs" ? "on" : ""} onClick={() => setView({ name: "runs" })}>
            Runs{active > 0 && <span className="count">{active}</span>}
          </button>
        </nav>
      </header>
      <main>
        {view.name === "workflows" && <Workflows onPick={(w) => setView({ name: "form", workflow: w })} />}
        {view.name === "form" && (
          <RunForm workflow={view.workflow} onStarted={(id) => setView({ name: "run", runId: id })}
            onBack={() => setView({ name: "workflows" })} />
        )}
        {view.name === "runs" && <RunsDashboard onOpen={(id) => setView({ name: "run", runId: id })} />}
        {view.name === "run" && <RunView runId={view.runId} onBack={() => setView({ name: "runs" })} />}
      </main>
    </div>
  );
}
```

- [ ] **Step 6: Verify the build is clean**

Run: `cd atom-ui && npm run build`
Expected: `tsc` + `vite build` succeed with no type errors.

- [ ] **Step 7: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/ui.tsx atom-ui/src/Workflows.tsx atom-ui/src/App.tsx atom-ui/src/RunsDashboard.tsx atom-ui/src/RunView.tsx
git commit -m "feat(ui): API client, design helpers, app shell + Workflows/RunForm"
```

---

### Task 7: Frontend — Runs dashboard (paginated, filtered, single poll)

**Files:**
- Replace: `atom-ui/src/RunsDashboard.tsx`

**Interfaces:**
- Consumes: `api.runs`, `RunsPage` (Task 6); `StatusPill, elapsed, fmtClock, progressText` (Task 6 `ui.tsx`).
- Produces: `RunsDashboard({ onOpen })` — one poll drives the table; `AbortController` cancels superseded requests.

- [ ] **Step 1: Replace `atom-ui/src/RunsDashboard.tsx`**

```tsx
import { useEffect, useState } from "react";
import { api, RunsPage } from "./api";
import { StatusPill, elapsed, fmtClock, progressText } from "./ui";

const PAGE = 50;
const FILTERS = ["active", "complete", "halted", "all"] as const;

export function RunsDashboard({ onOpen }: { onOpen: (id: string) => void }) {
  const [status, setStatus] = useState<string>("active");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<RunsPage | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => { setOffset(0); }, [status]);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const ctrl = new AbortController();
    const tick = async () => {
      try {
        const p = await api.runs(status, PAGE, offset, ctrl.signal);
        if (live) { setData(p); setErr(""); }
      } catch (e) {
        if (live && (e as Error).name !== "AbortError") setErr(String(e));
      }
      if (live) timer = setTimeout(tick, 2500);
    };
    tick();
    return () => { live = false; ctrl.abort(); clearTimeout(timer); };
  }, [status, offset]);

  const counts = data?.counts;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="page wide">
      <h1>Runs</h1>
      <div className="filters">
        {FILTERS.map((f) => (
          <button key={f} className={status === f ? "chip on" : "chip"} onClick={() => setStatus(f)}>
            {f}{counts && f !== "all" ? <span className="chip-n">{counts[f as "active" | "complete" | "halted"]}</span> : null}
          </button>
        ))}
      </div>
      {err && <div className="error">{err}</div>}
      <table className="runs">
        <thead>
          <tr><th>Status</th><th>Workflow</th><th>Progress</th><th>Started</th><th>Elapsed</th></tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.run_id} onClick={() => onOpen(r.run_id)}>
              <td><StatusPill status={r.status} /></td>
              <td className="mono-cell">{r.workflow}<div className="rid">{r.run_id}</div></td>
              <td>{progressText(r)}</td>
              <td className="dim">{fmtClock(r.created_at)}</td>
              <td className="dim">{elapsed(r.created_at, r.ended_at)}</td>
            </tr>
          ))}
          {!items.length && (
            <tr><td colSpan={5} className="empty">No {status === "all" ? "" : status} runs.</td></tr>
          )}
        </tbody>
      </table>
      <div className="pager">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>← Prev</button>
        <span className="dim">{total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, total)}`} of {total}</span>
        <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next →</button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build is clean**

Run: `cd atom-ui && npm run build`
Expected: `tsc` + `vite build` succeed with no type errors.

- [ ] **Step 3: Commit**

```bash
git add atom-ui/src/RunsDashboard.tsx
git commit -m "feat(ui): paginated/filtered Runs dashboard on a single poll"
```

---

### Task 8: Frontend — Run view (two-pane console, transcript, deliverable viewer)

**Files:**
- Replace: `atom-ui/src/RunView.tsx`

**Interfaces:**
- Consumes: `api.run, api.artifacts, api.messages, api.artifactText, artifactUrl`, types `Manifest, Artifact, ChatMsg` (Task 6); `Dot, StatusPill, elapsed, fmtSize` (Task 6 `ui.tsx`); `react-markdown` + `remark-gfm` (Task 5).
- Produces: `RunView({ runId, onBack })`.

- [ ] **Step 1: Replace `atom-ui/src/RunView.tsx`**

```tsx
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, artifactUrl, Artifact, ChatMsg, Manifest } from "./api";
import { Dot, StatusPill, elapsed, fmtSize } from "./ui";

const IMG = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;
const MD = /\.(md|markdown)$/i;

type Sel = { step: number; task: string };

function argSummary(args?: Record<string, unknown>): string {
  if (!args) return "";
  const a = args as Record<string, unknown>;
  if (Array.isArray(a.filepaths)) return (a.filepaths as unknown[]).join(", ");
  if (typeof a.path === "string") return a.path;
  return Object.keys(a).slice(0, 2).map((k) => `${k}=${String(a[k]).slice(0, 40)}`).join(", ");
}

export function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [arts, setArts] = useState<Artifact[]>([]);
  const [sel, setSel] = useState<Sel | null>(null);
  const [tab, setTab] = useState<"transcript" | "deliverables">("transcript");
  const [openArt, setOpenArt] = useState<Artifact | null>(null);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const m = await api.run(runId).catch(() => null);
      if (live && m) {
        setManifest(m);
        api.artifacts(runId).then(setArts).catch(() => { /* ignore */ });
        if (m.status === "complete" || m.status === "halted") return;
      }
      if (live) timer = setTimeout(tick, 1500);
    };
    tick();
    return () => { live = false; clearTimeout(timer); };
  }, [runId]);

  // default-select the running (or first) task once the manifest arrives
  useEffect(() => {
    if (!manifest || sel) return;
    const flat = manifest.steps.flatMap((s) => s.tasks.map((t) => ({ step: s.index, task: t.id, status: t.status })));
    const running = flat.find((t) => t.status === "running");
    const pick = running ?? flat[0];
    if (pick) setSel({ step: pick.step, task: pick.task });
  }, [manifest, sel]);

  const doneSteps = manifest ? manifest.steps.filter((s) => s.status === "complete").length : 0;
  const curStep = manifest
    ? Math.min(manifest.status === "complete" ? doneSteps : doneSteps + 1, manifest.steps.length)
    : 0;

  return (
    <div className="runview">
      <div className="run-head">
        <div className="crumbs">
          <button className="link" onClick={onBack}>← Runs</button>
          <span className="sep">/</span>
          <b>{manifest?.workflow ?? runId}</b>
        </div>
        {manifest && (
          <div className="run-status">
            <StatusPill status={manifest.status} />
            <span className="dim">Step {curStep} of {manifest.steps.length}</span>
            <span className="dim">{elapsed(manifest.created_at, manifest.ended_at)}</span>
          </div>
        )}
      </div>

      {!manifest ? (
        <div className="loading">Loading…</div>
      ) : (
        <div className="run-body">
          <aside className="rail">
            {manifest.steps.map((s) => (
              <div key={s.index} className="rail-step">
                <div className="rail-step-head">
                  <span className="step-idx">Step {s.index + 1}</span> {s.title} <Dot status={s.status} />
                </div>
                {s.tasks.map((t) => (
                  <button key={t.id}
                    className={`rail-task${sel?.task === t.id && sel?.step === s.index ? " on" : ""}`}
                    onClick={() => { setSel({ step: s.index, task: t.id }); setTab("transcript"); }}>
                    <Dot status={t.status} />
                    <span className="rail-task-id">{t.id}</span>
                    {t.model && <span className="tag">{t.model}</span>}
                  </button>
                ))}
              </div>
            ))}
            <div className="rail-deliverables">
              <div className="rail-h">Deliverables</div>
              {arts.length === 0 && <div className="rail-empty">None yet</div>}
              {arts.map((a) => (
                <button key={`${a.step}-${a.task}-${a.rel}`}
                  className={`rail-art${openArt?.rel === a.rel ? " on" : ""}`}
                  onClick={() => { setOpenArt(a); setTab("deliverables"); }}>
                  <span className="art-name">{a.name}</span>
                  <span className="art-meta">{a.task} · {fmtSize(a.size)}</span>
                </button>
              ))}
            </div>
          </aside>

          <section className="center">
            <div className="tabbar">
              <button className={tab === "transcript" ? "on" : ""} onClick={() => setTab("transcript")}>Transcript</button>
              <button className={tab === "deliverables" ? "on" : ""} onClick={() => setTab("deliverables")}>
                Deliverables{arts.length ? ` (${arts.length})` : ""}
              </button>
            </div>
            {tab === "transcript"
              ? <Transcript runId={runId} sel={sel} status={manifest.status} />
              : <Deliverables runId={runId} arts={arts} open={openArt} setOpen={setOpenArt} />}
          </section>
        </div>
      )}
    </div>
  );
}

function Transcript({ runId, sel, status }: { runId: string; sel: Sel | null; status: string }) {
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (!sel) { setChat([]); return; }
    let live = true;
    setPending(true);
    api.messages(runId, sel.step, sel.task)
      .then((m) => { if (live) setChat(m); })
      .catch(() => { if (live) setChat([]); })
      .finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [runId, sel?.step, sel?.task, status]);

  if (!sel) return <div className="placeholder">Select a task to view its transcript.</div>;
  if (pending && !chat.length) return <div className="placeholder">Loading transcript…</div>;
  if (!chat.length) return <div className="placeholder">No messages yet for {sel.task}.</div>;

  return (
    <div className="transcript">
      {chat.map((m, i) => m.tool_calls?.length ? (
        <div key={i} className="msg tool-calls">
          {m.text && <div className="msg-text">{m.text}</div>}
          {m.tool_calls.map((c, k) => (
            <div key={k} className={`toolcall${c.name === "present_files" ? " present" : ""}`}>
              <span className="tc-name">{c.name === "present_files" ? "⇪ present_files" : `→ ${c.name}`}</span>
              <span className="tc-args">{argSummary(c.args)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div key={i} className={`msg ${m.role}`}>
          <div className="msg-role">{m.name || m.role}</div>
          <div className="msg-text">{m.text}</div>
        </div>
      ))}
    </div>
  );
}

function Deliverables(
  { runId, arts, open, setOpen }:
  { runId: string; arts: Artifact[]; open: Artifact | null; setOpen: (a: Artifact | null) => void },
) {
  if (!arts.length) return <div className="placeholder">No deliverables presented yet.</div>;
  if (open) {
    return (
      <div className="viewer">
        <div className="viewer-head">
          <button className="link" onClick={() => setOpen(null)}>← All deliverables</button>
          <span className="viewer-name">{open.name}</span>
          <span className="dim">step {open.step} · {open.task} · {fmtSize(open.size)}</span>
        </div>
        <ArtifactBody runId={runId} art={open} />
      </div>
    );
  }
  return (
    <div className="gallery">
      {arts.map((a) => (
        <button key={`${a.step}-${a.task}-${a.rel}`} className="gal-card" onClick={() => setOpen(a)}>
          <div className="gal-name">{a.name}</div>
          <div className="gal-meta">step {a.step} · {a.task}</div>
          <div className="gal-size">{fmtSize(a.size)}</div>
        </button>
      ))}
    </div>
  );
}

function ArtifactBody({ runId, art }: { runId: string; art: Artifact }) {
  const isImg = IMG.test(art.name);
  const isMd = MD.test(art.name);
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (isImg) return;
    let live = true;
    setText(null); setErr("");
    api.artifactText(runId, art.rel).then((t) => { if (live) setText(t); }).catch((e) => { if (live) setErr(String(e)); });
    return () => { live = false; };
  }, [runId, art.rel, isImg]);

  if (isImg) return <div className="art-img"><img src={artifactUrl(runId, art.rel)} alt={art.name} /></div>;
  if (err) return <div className="error">{err}</div>;
  if (text === null) return <div className="placeholder">Loading…</div>;
  if (isMd) return <div className="art-md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>;
  return <pre className="art-code">{text}</pre>;
}
```

- [ ] **Step 2: Verify the build is clean**

Run: `cd atom-ui && npm run build`
Expected: `tsc` + `vite build` succeed with no type errors.

- [ ] **Step 3: Commit**

```bash
git add atom-ui/src/RunView.tsx
git commit -m "feat(ui): two-pane run console with transcript + markdown/image/code deliverable viewer"
```

---

### Task 9: Example workflow — exercise `present_files`

**Files:**
- Modify: `workflows/parallel-poems.yaml`

**Interfaces:**
- Consumes: the `present_files` tool (bound by default). No code interfaces.

- [ ] **Step 1: Replace `workflows/parallel-poems.yaml`**

```yaml
# workflows/parallel-poems.yaml — copy to $ATOM_HOME/workflows/ to run it.
name: parallel-poems
description: Draft poems in parallel, then refine them and compile an anthology.
inputs:
  - name: topic
    required: true
    description: What the poems are about.
  - name: style
    required: false
    default: free verse
steps:
  - title: Draft
    description: Three poets each draft one poem into the shared workspace and present it.
    tasks:
      - id: poet_a
        prompt: "Write a {{ style }} poem about {{ topic }}. Save it as poem_a.md in {{ workspace }}, then call present_files on poem_a.md."
        model: haiku
        thinking: low
      - id: poet_b
        prompt: "Write a {{ style }} poem about {{ topic }} from a child's point of view. Save it as poem_b.md in {{ workspace }}, then call present_files on poem_b.md."
        model: haiku
        thinking: low
      - id: poet_c
        prompt: "Write a {{ style }} poem about {{ topic }} as a strict sonnet. Save it as poem_c.md in {{ workspace }}, then call present_files on poem_c.md."
        model: haiku
        thinking: low
  - title: Refine
    description: One editor sharpens every draft, compiles an anthology, and presents the finished set.
    tasks:
      - id: refiner
        prompt: "Read every poem_*.md in {{ workspace }} and sharpen each for imagery and rhythm, saving each back in place. Then compile all three into anthology.md in {{ workspace }} with a title and each poem under a heading. Finally, call present_files on poem_a.md, poem_b.md, poem_c.md, and anthology.md."
        model: haiku
        thinking: medium
```

- [ ] **Step 2: Verify it parses**

Run: `.venv/bin/python -c "from atom.workflow.schema import WorkflowDef; import yaml; WorkflowDef.model_validate(yaml.safe_load(open('workflows/parallel-poems.yaml'))); print('ok')"`
Expected: prints `ok` (schema validates).

- [ ] **Step 3: Commit**

```bash
git add workflows/parallel-poems.yaml
git commit -m "docs(workflow): parallel-poems presents drafts, refined poems, and an anthology"
```

---

## Self-Review

**1. Spec coverage:**
- §1.1 present_files gap → Tasks 1, 2 (capture + persist), Task 4 (serve), Task 9 (example).
- §3.1 copy-at-capture + immutability → Task 1 (`capture_artifacts` + snapshot test) and Task 2 (cross-step snapshot test).
- §3.2/3.3 data model + store methods → Task 1.
- §3.4 engine wiring → Task 2.
- §4.1 artifacts endpoints (list + FileResponse) → Task 4.
- §4.2 run detail unchanged → Task 4 keeps `get_run`.
- §5.1 RunSummary + summary.json + list_summaries + fallback → Task 3.
- §5.2 list endpoint → Task 4.
- §6.1 nav/views → Task 6 (shell, Workflows, RunForm) + Task 7 (dashboard).
- §6.2 run view two-pane/transcript/viewer → Task 8.
- §6.3 live/scale polling → Task 6 (nav badge), Task 7 (single dashboard poll + AbortController), Task 8 (single-run poll).
- §6.4 typography/palette → Task 5.
- §7 example → Task 9.
- §8 testing split → backend TDD Tasks 1-4; frontend build-clean Tasks 5-8.
- §9 non-goals → not implemented (engine concurrency untouched).

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step contains complete code.

**3. Type consistency:** `ArtifactRef{name,path,rel,size}` identical in `run_store.py` (Task 1) and `api.ts` (Task 6). `Artifact extends ArtifactRef {step,task}` matches the API dict `{**a.model_dump(), step, task}` (Task 4). `RunSummary` fields match between `run_store.py` (Task 3) and `api.ts` (Task 6) and the dashboard's `progressText`/columns (Tasks 6-7). `list_summaries` return `{items,total,counts}` matches `RunsPage` and the endpoint (Tasks 3, 4, 6). `present_files` call args `{"filepaths":[...]}` match the tool signature and are used consistently in Tasks 2, 4, 9. Rail/viewer status classes come from the shared `STATUS_CLASS` map (Task 6) used by `StatusPill`/`Dot` across Tasks 7-8.
