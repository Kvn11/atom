# Export run — unbounded-size download — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a run's trace export generatable and downloadable at any size (well beyond 80 MB) by streaming the write to disk and streaming the file to the browser.

**Architecture:** Three independent changes. (1) Generation stops buffering the whole export through `json.dumps(...)` and streams it with `json.dump(fp, ...)` via one shared atomic-write helper. (2) A new `GET …/export/download` route serves the produced file with `FileResponse` (chunked, `Content-Length` from `os.stat`) — the same streaming mechanism the artifact download already uses. (3) The UI surfaces an `<a download>` anchor so the browser streams straight to disk (never buffering bytes in JS).

**Tech Stack:** Python 3.12, FastAPI/Starlette, pytest (async via `httpx.AsyncClient` + `ASGITransport`); UI is React + TypeScript built with `vite`/`tsc` (no JS unit-test harness).

## Global Constraints

- **No new dependencies.** Reuse `FileResponse`, `_content_disposition`, atomic `tmp` + `os.replace`, and `<a download>` — all already in the codebase.
- **Generation output stays byte-identical** — keep `indent=2`; existing exporter tests (`test_export.py`, `test_langfuse_export.py`, `test_cli_export.py`) are the regression guard and must stay green unchanged.
- **Access is direct/local** — no reverse proxy size cap. Do **not** add gzip / range / resumable transfer or per-root streaming fetch (rejected in the spec as YAGNI / disproportionate).
- **Traversal defense:** any client-supplied `task` must be validated against the run manifest before a filesystem path is built.
- Test runner is `pytest` (run from repo root). UI verification is `npm run build` (`tsc && vite build`) from `atom-ui/`.

---

### Task 1: Memory-safe generation (stream `json.dump`, single shared writer)

**Files:**
- Modify: `src/atom/workflow/run_store.py` (add `export_path`, next to `task_export_path` ~line 141-143)
- Modify: `src/atom/observability/export.py` (add `Path` import + `_atomic_write_json`; rewire `export_run` ~171-175 and `export_task` ~248-252)
- Modify: `src/atom/observability/langfuse_export.py` (import `_atomic_write_json`; rewire `export_run` ~210-214 and `export_task` ~287-291; fix one docstring)
- Test: `tests/test_export.py` (add the streaming-guard test)

**Interfaces:**
- Produces: `RunStore.export_path(run_id: str) -> pathlib.Path` — whole-run export location (`run_dir/export.json`). Consumed by Task 2's download route and by the exporters here.
- Produces: `atom.observability.export._atomic_write_json(path: pathlib.Path, obj: Any) -> None` — streams `obj` to `path` as pretty JSON atomically. Consumed by both exporter modules.

- [ ] **Step 1: Write the failing guard test**

Add to the end of `tests/test_export.py`:

```python
def test_export_run_streams_write_without_json_dumps(atom_home, monkeypatch):
    # The write path must stream via json.dump(fp), never buffer the whole export through
    # json.dumps(...) — patch json.dumps to explode and assert the export still writes.
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded"])
    client = _FakeClient([["root1"]], {"root1": {"id": "root1"}})

    import atom.observability.export as exp

    def _boom(*a, **k):
        raise AssertionError("json.dumps used — write is not streaming")
    monkeypatch.setattr(exp.json, "dumps", _boom)

    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep)
    env = json.loads(Path(result.path).read_text())   # wrote successfully, no json.dumps
    assert env["run_id"] == "r1" and env["roots"][0]["id"] == "root1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_export.py::test_export_run_streams_write_without_json_dumps -v`
Expected: FAIL — current `export_run` calls `json.dumps(envelope, indent=2)`, which hits `_boom` and raises `AssertionError: json.dumps used…`.

- [ ] **Step 3: Add `RunStore.export_path`**

In `src/atom/workflow/run_store.py`, immediately after `task_export_path` (the method ending at ~line 143), add:

```python
    def export_path(self, run_id: str) -> Path:
        """Whole-run trace export location (task exports use task_export_path)."""
        return self.run_dir(run_id) / "export.json"
```

(`Path` is already imported in this module.)

- [ ] **Step 4: Add the `Path` import and `_atomic_write_json` helper to `export.py`**

In `src/atom/observability/export.py`, add the import (in the stdlib `from` group, after `import time`):

```python
from pathlib import Path
```

Then add the helper directly after the `_TERMINAL` constant (~line 28), before `expected_root_count`:

```python
def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as pretty JSON, atomically, streaming the encode.

    ``json.dump(obj, fp, ...)`` serializes incrementally straight to the file handle, so it never
    materializes the whole JSON as one in-memory string the way ``json.dumps(...)`` does — peak
    memory stays at ~one copy (the source dict) instead of two, which is what lets a very large
    export (well beyond 80 MB) be written without a second full-size buffer. The tmp-write +
    ``os.replace`` keeps the swap atomic, matching ``RunStore.save``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(obj, fp, indent=2)
    os.replace(tmp, path)
```

- [ ] **Step 5: Rewire both write sites in `export.py`**

In `export_run`, replace this block (~lines 171-175):

```python
    path = store.run_dir(run_id) / "export.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("export.json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)                  # atomic, matching RunStore.save
```

with:

```python
    path = store.export_path(run_id)
    _atomic_write_json(path, envelope)     # streamed write; atomic tmp + os.replace, matching RunStore.save
```

In `export_task`, replace this block (~lines 248-252):

```python
    path = store.task_export_path(run_id, step_index, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)                  # atomic, matching RunStore.save
```

with:

```python
    path = store.task_export_path(run_id, step_index, task_id)
    _atomic_write_json(path, envelope)     # streamed write; atomic tmp + os.replace, matching RunStore.save
```

(`os` and `json` remain imported and used — `os.replace`/`json.dump` in the helper, `os.environ.get` in the exporters.)

- [ ] **Step 6: Rewire `langfuse_export.py` to reuse the shared helper**

In `src/atom/observability/langfuse_export.py`, add `_atomic_write_json` to the import from `atom.observability.export` (the block at ~lines 17-23):

```python
from atom.observability.export import (
    ExportResult,
    _TERMINAL,
    _atomic_write_json,
    build_envelope,
    expected_root_count,
    resolve_run_ids,    # noqa: F401 — dispatched CLI/API import this from here too
)
```

In `export_run`, replace (~lines 210-214):

```python
    path = store.run_dir(run_id) / "export.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("export.json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
```

with:

```python
    path = store.export_path(run_id)
    _atomic_write_json(path, envelope)
```

In `export_task`, replace (~lines 287-291):

```python
    path = store.task_export_path(run_id, step_index, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
```

with:

```python
    path = store.task_export_path(run_id, step_index, task_id)
    _atomic_write_json(path, envelope)
```

Finally, fix the now-stale reference in the `_as_dict` docstring (~line 66): change `json.dumps(envelope, ...)` to `json.dump(envelope, ...)` (the "every value must be JSON-native" point still holds — `json.dump` raises the same `TypeError` on a native datetime).

(`os` and `json` remain used in this module — `os.environ.get` in `_resolve_keys`, `json.loads` in `_as_dict`.)

- [ ] **Step 7: Run the guard test + the full exporter suites**

Run: `pytest tests/test_export.py tests/test_langfuse_export.py tests/test_cli_export.py -v`
Expected: PASS — the new guard test passes, and every pre-existing exporter test still passes (byte-identical output because `indent=2` is unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/atom/workflow/run_store.py src/atom/observability/export.py \
        src/atom/observability/langfuse_export.py tests/test_export.py
git commit -m "$(cat <<'EOF'
feat(export): stream export write via json.dump to remove the double-buffer

json.dumps(envelope) materialized the whole (>80MB) export as a second
full-size string on top of the source dict. Switch all four exporter paths
to a shared _atomic_write_json() that json.dump()s straight to the temp file
(atomic os.replace), and add RunStore.export_path so the whole-run location
is single-sourced. Output is byte-identical (indent=2 preserved).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Streamed download route

**Files:**
- Modify: `src/atom/api/app.py` (add `download_export`, inserted right after the `export_traces` POST handler, ~line 274)
- Test: `tests/test_workflow_api.py` (add a seed helper + six download tests)

**Interfaces:**
- Consumes: `RunStore.export_path` (Task 1), `RunStore.task_export_path` (existing), `_content_disposition` + `FileResponse` (existing in `app.py`).
- Produces: HTTP route `GET /api/runs/{run_id}/export/download?step=&task=` → streamed `application/json` attachment, or 404.

- [ ] **Step 1: Write the failing tests**

At the top of `tests/test_workflow_api.py`, add `import json` (with the stdlib imports) and this import near the other `atom.*` imports:

```python
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
```

Then add a seed helper and the tests (anywhere after `_provider` is defined — e.g. just before the `# --- provider dispatch` section):

```python
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
```

(`quote` is already imported at the top of this test module.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_workflow_api.py -k download_export -v`
Expected: FAIL — the route does not exist yet, so every request returns 404 (the happy-path and large-file tests fail their `200`/body assertions; the 404 tests may pass incidentally, which is fine).

- [ ] **Step 3: Implement the route**

In `src/atom/api/app.py`, insert immediately after the `export_traces` POST handler (after its `return {...}` block, ~line 274) and before `get_artifacts`:

```python
    @app.get("/api/runs/{run_id}/export/download")
    def download_export(run_id: str, step: int | None = None, task: str | None = None):
        """Stream a previously generated export file to the browser.

        Whole-run export by default; one task's export when both ``step`` and ``task`` are given
        (matching the POST). ``FileResponse`` streams from disk (chunked, ``Content-Length`` from
        ``os.stat``), so an export of any size downloads without buffering in server memory. The
        (step, task) pair is validated against the manifest before a path is built — this gives
        clean 404s and blocks path traversal via a crafted ``task``.
        """
        try:
            manifest = store.load(run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found")
        if step is not None and task is not None:
            s = next((s for s in manifest.steps if s.index == step), None)
            if s is None or not any(t.id == task for t in s.tasks):
                raise HTTPException(404, "task not found")
            path = store.task_export_path(run_id, step, task)
            fname = f"atom-export-{run_id}-s{step}-{task}.json"
        else:
            path = store.export_path(run_id)
            fname = f"atom-export-{run_id}.json"
        if not path.is_file():
            raise HTTPException(404, "export not generated yet — POST this run's /export first")
        return FileResponse(
            path, media_type="application/json",
            headers={"Content-Disposition": _content_disposition(fname)},
        )
```

(`FileResponse`, `HTTPException`, and `_content_disposition` are already imported/defined in `app.py`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_workflow_api.py -k download_export -v`
Expected: PASS — all six download tests green.

- [ ] **Step 5: Commit**

```bash
git add src/atom/api/app.py tests/test_workflow_api.py
git commit -m "$(cat <<'EOF'
feat(api): add streamed GET .../export/download route

FileResponse streams the produced export.json (or task export) from disk,
setting Content-Length from os.stat, so exports of any size download without
buffering in server memory. The (step, task) pair is validated against the
manifest before a path is built, blocking traversal via a crafted task id.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: UI — surface a real Download anchor

**Files:**
- Modify: `atom-ui/src/api.ts` (add `exportDownloadUrl`, after `artifactUrl` ~line 36)
- Modify: `atom-ui/src/RunView.tsx` (import the helper; extend `exportMsg` state ~line 108; set `href` on success ~lines 152-156; render the anchor ~lines 188-193)

**Interfaces:**
- Consumes: `GET …/export/download` route (Task 2).
- Produces: `exportDownloadUrl(id: string, body?: { step: number; task: string }) => string`.

Note: there is **no JS unit-test harness** in `atom-ui` (only `vite`/`tsc`). This task is verified by the TypeScript typecheck + build passing, and the real download is exercised in the Execution Handoff `/verify` pass.

- [ ] **Step 1: Add the URL builder to `api.ts`**

In `atom-ui/src/api.ts`, directly after the `artifactUrl` export (~line 36), add:

```ts
export const exportDownloadUrl = (id: string, body?: { step: number; task: string }) =>
  body
    ? `/api/runs/${id}/export/download?step=${body.step}&task=${encodeURIComponent(body.task)}`
    : `/api/runs/${id}/export/download`;
```

- [ ] **Step 2: Import the helper and extend the banner state in `RunView.tsx`**

Update the `./api` import (line 5) to include `exportDownloadUrl`:

```tsx
import { api, artifactUrl, exportDownloadUrl, Artifact, ChatMsg, Manifest, StreamBlock } from "./api";
```

Extend the `exportMsg` state type (line 108) with an optional `href`:

```tsx
  const [exportMsg, setExportMsg] = useState<{ text: string; kind: "ok" | "warn" | "err"; href?: string } | null>(null);
```

- [ ] **Step 3: Attach the download URL on a successful export**

In `runExport`, replace the success `else` branch (~lines 152-156):

```tsx
      } else {
        const what = res.scope === "task" ? `task ${res.task_id}` : "run";
        const partial = res.complete ? "" : ` (partial: ${res.fetched_roots}/${res.expected_roots})`;
        setExportMsg({ text: `Exported ${what} → ${res.path}${partial}`, kind: res.complete ? "ok" : "warn" });
      }
```

with:

```tsx
      } else {
        const what = res.scope === "task" ? `task ${res.task_id}` : "run";
        const partial = res.complete ? "" : ` (partial: ${res.fetched_roots}/${res.expected_roots})`;
        setExportMsg({
          text: `Exported ${what} → ${res.path}${partial}`,
          kind: res.complete ? "ok" : "warn",
          href: exportDownloadUrl(runId, body),
        });
      }
```

(`body` is the `runExport` parameter — `undefined` for a whole run, `{ step, task }` for a task — so the anchor's scope matches what was exported. The `fetched_roots === 0` branch stays as-is with no `href`, so no Download link shows when nothing was exported.)

- [ ] **Step 4: Render the anchor in the banner**

Replace the export banner block (~lines 188-193):

```tsx
      {exportMsg && (
        <div className={`export-banner ${exportMsg.kind}`}>
          <span className="export-text">{exportMsg.text}</span>
          <button className="export-x" onClick={() => setExportMsg(null)} title="Dismiss">✕</button>
        </div>
      )}
```

with:

```tsx
      {exportMsg && (
        <div className={`export-banner ${exportMsg.kind}`}>
          <span className="export-text">{exportMsg.text}</span>
          {exportMsg.href && (
            <a className="export-dl" href={exportMsg.href} download>Download export ↓</a>
          )}
          <button className="export-x" onClick={() => setExportMsg(null)} title="Dismiss">✕</button>
        </div>
      )}
```

The plain `<a href download>` makes the browser stream the response straight to disk — JS never holds the payload (unlike `fetch().blob()`), which is what keeps the client side size-unbounded. The click is a fresh user gesture, so no browser download-block. (No new CSS is required — the anchor is functional as-is; adding an `.export-dl` rule beside the existing `.export-banner` styles is an optional polish.)

- [ ] **Step 5: Typecheck + build**

Run: `cd atom-ui && npm run build`
Expected: PASS — `tsc` typechecks the new `href` field and `exportDownloadUrl` signature, then `vite build` succeeds.

- [ ] **Step 6: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/RunView.tsx
git commit -m "$(cat <<'EOF'
feat(runview): add a Download-export anchor after a successful export

The export banner now shows a plain <a download> to the new
GET .../export/download route, so the browser streams the export straight
to disk (never buffering the payload in JS) — completing the end-to-end
path for arbitrarily large exports.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**
- Memory-safe generation (spec §Design.1) → Task 1 (`_atomic_write_json`, all four sites, `export_path`). ✓
- Streamed download route (spec §Design.2) → Task 2 (`FileResponse`, manifest-validated traversal defense, 404s). ✓
- UI real download (spec §Design.3) → Task 3 (`exportDownloadUrl` + banner anchor). ✓
- Tests: regression/identical-output (existing suites, Task 1 Step 7), `json.dumps` guard (Task 1), route happy/404/traversal/large-file (Task 2). ✓ All spec §Testing items map to a step.
- CLI inherits the generation fix (spec note) — no separate task, called out in Task 1. ✓
- Rejected scope (gzip/range, per-root streaming, auto-download) — encoded in Global Constraints; nothing in the plan reintroduces them. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step shows complete code; every run step shows the exact command and expected result. ✓

**3. Type consistency:** `export_path(run_id)` is defined in Task 1 and consumed with the same signature in Task 2 and the exporters. `_atomic_write_json(path, obj)` is defined in `export.py` (Task 1 Step 4) and imported/used identically in `langfuse_export.py` (Step 6). `exportDownloadUrl(id, body?)` defined in Task 3 Step 1 matches its call in Step 3. The `exportMsg` shape `{ text; kind; href? }` is defined in Step 2 and used consistently in Steps 3-4. ✓

No issues found.
