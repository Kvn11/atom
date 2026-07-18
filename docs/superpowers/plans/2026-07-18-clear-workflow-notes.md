# Clear Workflow Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators a way to wipe one workflow's persistent Logseq vault (`$ATOM_HOME/notes/<slug>/`) — via CLI, REST, and a UI button — guarded so a vault is never wiped while a run of that workflow is active.

**Architecture:** A single core `clear_vault(home, workflow_name)` in `notes.py` (path-confined `rmtree`, delete-only — the next notes-enabled run re-provisions via the existing `ensure_vault`), plus a `RunStore.has_active_runs(workflow_name)` safety gate. Three thin surfaces call the core: a `atom workflow notes clear` CLI command, a `DELETE /api/workflows/{name}/notes` route, and a "Clear notes" button in the workflow RunForm.

**Tech Stack:** Python 3.11, typer CLI, FastAPI, pytest (+ httpx AsyncClient), React/TypeScript (Vite) UI.

## Global Constraints

- Vault location: `notes_root(home, workflow_name)` = `$ATOM_HOME/notes/<slug>/` where `<slug> = notes._slug(workflow_name)`.
- `clear_vault` is **delete-only** (whole slug dir removed via `shutil.rmtree`); no re-provision, no partial/content-only clear, no backup. It is **idempotent** (absent vault → returns `False`, no error).
- `clear_vault` MUST be path-confined: refuse (raise `ValueError`) if the resolved target is `$ATOM_HOME/notes` itself or anything outside it.
- Active-run gate: `_ACTIVE = ("pending", "queued", "running")` (already defined in `run_store.py`). A vault is NOT cleared while any run of that workflow has an active status.
- CLI (`atom workflow notes clear <name>`): `--yes/-y` skips the confirmation prompt ONLY; the active-run gate is a hard refuse (exit code 1) regardless of `--yes`. The CLI clears by vault name and does NOT require the workflow YAML to still exist (so orphaned vaults are cleanable).
- REST (`DELETE /api/workflows/{name}/notes`): `404` if the workflow YAML is unknown; `409` if a run is active; else `200 {"workflow": name, "cleared": bool}`. Reuse the app's existing `engine.store` for the active-run check.
- `GET /api/workflows` gains a `notes_enabled: bool` field per workflow (from `w.notes.enabled`).
- UI: the "Clear notes" button appears in `RunForm` ONLY when `workflow.notes_enabled`; it confirms before calling `api.clearNotes`. atom-ui has no test runner — verify with `npm run build` (runs `tsc` typecheck + vite build).
- TDD for all Python tasks: failing test first, then minimal implementation.

---

### Task 1: `clear_vault` core

**Files:**
- Modify: `src/atom/notes.py` (add `clear_vault` after `ensure_vault`)
- Test: `tests/test_notes.py`

**Interfaces:**
- Produces: `atom.notes.clear_vault(home, workflow_name: str) -> bool` — `True` if a vault existed and was removed, `False` if absent; raises `ValueError` if the target escapes `$ATOM_HOME/notes/`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notes.py` (it already imports `notes_root` and uses the `atom_home` fixture):

```python
def test_clear_vault_removes_existing(atom_home):
    from atom.notes import clear_vault
    root = notes_root(str(atom_home), "wf-clear")
    (root / "pages").mkdir(parents=True)
    (root / "pages" / "a.md").write_text("note")
    assert clear_vault(str(atom_home), "wf-clear") is True
    assert not root.exists()


def test_clear_vault_absent_is_noop(atom_home):
    from atom.notes import clear_vault
    assert clear_vault(str(atom_home), "never-existed") is False


def test_clear_vault_refuses_outside_notes_dir(atom_home, monkeypatch):
    import atom.notes as notes_mod
    from pathlib import Path
    # A pathological notes_root that points outside $ATOM_HOME/notes/ must be refused.
    monkeypatch.setattr(notes_mod, "notes_root", lambda home, name: Path(home) / "workflows")
    with pytest.raises(ValueError):
        notes_mod.clear_vault(str(atom_home), "x")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_notes.py -k clear_vault -v`
Expected: FAIL with `ImportError` (`clear_vault` not defined).

- [ ] **Step 3: Implement `clear_vault`**

In `src/atom/notes.py`, add after `ensure_vault` (`shutil` and `atom_home` are already imported):

```python
def clear_vault(home, workflow_name: str) -> bool:
    """Delete a workflow's persistent Logseq vault. Idempotent; returns whether one existed.

    Confined to ``$ATOM_HOME/notes/``: refuses to remove that directory itself or any path
    outside it. A fresh vault is re-provisioned by :func:`ensure_vault` on the next
    notes-enabled run, so this is a full reset rather than a content wipe.
    """
    notes_base = (atom_home(home) / "notes").resolve()
    root = notes_root(home, workflow_name).resolve()
    if root == notes_base or not root.is_relative_to(notes_base):
        raise ValueError(f"refusing to clear a path outside {notes_base}: {root}")
    if root.exists():
        shutil.rmtree(root)
        return True
    return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_notes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/notes.py tests/test_notes.py
git commit -m "feat(notes): clear_vault — path-confined reset of a workflow's Logseq vault"
```

---

### Task 2: `RunStore.has_active_runs`

**Files:**
- Modify: `src/atom/workflow/run_store.py` (add method to `RunStore`)
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Consumes: module-level `_ACTIVE` and `RunStore._scan_summaries()` (both already exist).
- Produces: `RunStore.has_active_runs(workflow_name: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_run_store.py` (it already imports `RunStore`, `RunManifest`, `StepState`, `TaskState` and defines `_manifest`):

```python
def test_has_active_runs_true_for_running(atom_home):
    store = RunStore(str(atom_home))
    m = _manifest("ha1", store.workspace_dir("ha1"))
    m.workflow = "wf-x"; m.status = "running"
    store.create(m)
    assert store.has_active_runs("wf-x") is True
    assert store.has_active_runs("wf-other") is False


def test_has_active_runs_false_for_terminal(atom_home):
    store = RunStore(str(atom_home))
    m = _manifest("ha2", store.workspace_dir("ha2"))
    m.workflow = "wf-y"; m.status = "complete"
    store.create(m)
    assert store.has_active_runs("wf-y") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_run_store.py -k has_active_runs -v`
Expected: FAIL (`AttributeError: 'RunStore' object has no attribute 'has_active_runs'`).

- [ ] **Step 3: Implement the method**

In `src/atom/workflow/run_store.py`, add to `RunStore` (near `queued_run_ids` / `interrupted_run_ids`):

```python
    def has_active_runs(self, workflow_name: str) -> bool:
        """True if any run of ``workflow_name`` is pending/queued/running (non-terminal)."""
        return any(
            s.workflow == workflow_name and s.status in _ACTIVE
            for s in self._scan_summaries()
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_run_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(run_store): has_active_runs — non-terminal runs of a workflow"
```

---

### Task 3: CLI `atom workflow notes clear <name>`

**Files:**
- Modify: `src/atom/cli.py` (add a `notes` sub-typer under `workflow_app`, after the `workflow_app` definition ~line 170)
- Test: `tests/test_workflow_cli.py`

**Interfaces:**
- Consumes: `atom.notes.clear_vault` (Task 1), `RunStore.has_active_runs` (Task 2), existing `load_config`, `console`, `workflow_app`.
- Produces: CLI command path `atom workflow notes clear <name> [--yes] [--config PATH]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_cli.py` (it already imports `app`, defines `runner` and `_seed`):

```python
def test_workflow_notes_clear_removes_vault(atom_home):
    from atom.notes import notes_root
    root = notes_root(str(atom_home), "demo")
    (root / "pages").mkdir(parents=True)
    result = runner.invoke(app, ["workflow", "notes", "clear", "demo", "--yes"])
    assert result.exit_code == 0
    assert not root.exists()
    assert "Cleared" in result.stdout


def test_workflow_notes_clear_noop_when_absent(atom_home):
    result = runner.invoke(app, ["workflow", "notes", "clear", "ghost", "--yes"])
    assert result.exit_code == 0
    assert "No notes vault" in result.stdout


def test_workflow_notes_clear_refuses_when_active_run(atom_home):
    from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
    store = RunStore(str(atom_home))
    m = RunManifest(
        run_id="cc1", workflow="demo", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("cc1")),
        steps=[StepState(index=0, title="S", tasks=[TaskState(id="t1", thread_id="cc1:s0:t1")])],
    )
    m.status = "running"
    store.create(m)
    (store.home / "notes" / "demo").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(app, ["workflow", "notes", "clear", "demo", "--yes"])
    assert result.exit_code == 1
    assert "active" in result.stdout.lower()
    assert (store.home / "notes" / "demo").exists()   # not touched
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_cli.py -k notes_clear -v`
Expected: FAIL (no such command; non-zero exit / usage error).

- [ ] **Step 3: Add the `notes` sub-typer + `clear` command**

In `src/atom/cli.py`, after the `workflow_app` is created and added (right after the `app.add_typer(workflow_app, name="workflow")` line), add:

```python
notes_app = typer.Typer(help="Manage a workflow's persistent Logseq vault.")
workflow_app.add_typer(notes_app, name="notes")


@notes_app.command("clear")
def workflow_notes_clear(
    name: str = typer.Argument(..., help="Workflow name whose vault to clear."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Delete a workflow's persistent Logseq vault (a fresh one is provisioned on the next run)."""
    from atom.notes import clear_vault
    from atom.workflow.run_store import RunStore

    cfg = load_config(config)
    if RunStore(cfg.home).has_active_runs(name):
        console.print(
            f"[red]Refusing to clear notes for '{name}': a run is active. "
            f"Wait for it to finish or cancel it first.[/red]"
        )
        raise typer.Exit(1)
    if not yes:
        typer.confirm(f"Delete the persistent Logseq vault for workflow '{name}'?", abort=True)
    if clear_vault(cfg.home, name):
        console.print(f"[green]Cleared notes vault for '{name}'.[/green]")
    else:
        console.print(f"[dim]No notes vault existed for '{name}'.[/dim]")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/cli.py tests/test_workflow_cli.py
git commit -m "feat(cli): atom workflow notes clear — wipe a workflow's Logseq vault"
```

---

### Task 4: REST — `DELETE /api/workflows/{name}/notes` + `notes_enabled`

**Files:**
- Modify: `src/atom/api/app.py` (`get_workflows` ~line 93; add the DELETE route near the other `/api/workflows` routes)
- Test: `tests/test_workflow_api.py`

**Interfaces:**
- Consumes: `atom.notes.clear_vault` (Task 1), `engine.store.has_active_runs` (Task 2), existing `load_workflow`, `list_workflows`, `HTTPException`, closure vars `cfg` and `engine`.
- Produces: `GET /api/workflows` items include `notes_enabled`; `DELETE /api/workflows/{name}/notes` → `{"workflow", "cleared"}` / 404 / 409.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workflow_api.py` a no-worker client helper (once, near `_client`) and the tests:

```python
@asynccontextmanager
async def _client_no_worker(app):
    # Route-only tests: don't drive the lifespan (no queue worker to recover fake runs).
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _seed_notes_wf(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "notewf.yaml").write_text(
        "name: notewf\ndescription: has notes.\n"
        "notes:\n  enabled: true\n"
        "steps:\n  - title: S\n    tasks:\n      - id: t1\n        prompt: \"hi\"\n"
    )


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
```

(`asynccontextmanager`, `AsyncClient`, `ASGITransport` are already imported at the top of the file.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py -k "notes_enabled or workflow_notes" -v`
Expected: FAIL (`notes_enabled` KeyError; DELETE route 405/404 mismatch).

- [ ] **Step 3: Add `notes_enabled` to `get_workflows`**

In `src/atom/api/app.py`, update `get_workflows`:

```python
    @app.get("/api/workflows")
    def get_workflows() -> list:
        return [
            {"name": w.name, "description": w.description,
             "notes_enabled": w.notes.enabled,
             "inputs": [i.model_dump() for i in w.inputs]}
            for w in list_workflows(cfg.home)
        ]
```

- [ ] **Step 4: Add the DELETE route**

In `src/atom/api/app.py`, add right after `get_workflow` (the `/api/workflows/{name}` GET route):

```python
    @app.delete("/api/workflows/{name}/notes")
    def clear_workflow_notes(name: str) -> dict:
        """Delete a workflow's persistent Logseq vault (re-provisioned on its next run)."""
        from atom.notes import clear_vault

        try:
            load_workflow(name, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{name}' not found")
        if engine.store.has_active_runs(name):
            raise HTTPException(409, f"workflow '{name}' has an active run; cannot clear notes")
        return {"workflow": name, "cleared": clear_vault(cfg.home, name)}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py -v`
Expected: PASS (new tests + pre-existing API tests).

- [ ] **Step 6: Commit**

```bash
git add src/atom/api/app.py tests/test_workflow_api.py
git commit -m "feat(api): DELETE /api/workflows/{name}/notes + notes_enabled in listing"
```

---

### Task 5: UI — "Clear notes" button in RunForm

**Files:**
- Modify: `atom-ui/src/api.ts` (`Workflow` interface + `clearNotes` client method)
- Modify: `atom-ui/src/Workflows.tsx` (`RunForm` — conditional button)

**Interfaces:**
- Consumes: `DELETE /api/workflows/{name}/notes` and the `notes_enabled` field from Task 4.
- Produces: `api.clearNotes(name)`; a "Clear notes" button rendered in `RunForm` when `workflow.notes_enabled`.

- [ ] **Step 1: Extend the API client**

In `atom-ui/src/api.ts`, add `notes_enabled` to the `Workflow` interface (line 2):

```ts
export interface Workflow { name: string; description?: string; notes_enabled?: boolean; inputs: InputDef[]; }
```

Add `clearNotes` to the `api` object (mirror the `cancel`/`selfImprove` error handling):

```ts
  clearNotes: (name: string): Promise<{ workflow: string; cleared: boolean }> =>
    fetch(`/api/workflows/${encodeURIComponent(name)}/notes`, { method: "DELETE" }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `clear notes failed (${r.status})`);
      return data as { workflow: string; cleared: boolean };
    }),
```

- [ ] **Step 2: Add the button to `RunForm`**

In `atom-ui/src/Workflows.tsx`, inside `RunForm`, add local state and a handler near the other `useState` hooks:

```tsx
  const [notesMsg, setNotesMsg] = useState("");
  const clearNotes = async () => {
    if (!window.confirm(`Delete the persistent notes vault for "${workflow.name}"?`)) return;
    setNotesMsg("");
    try {
      const { cleared } = await api.clearNotes(workflow.name);
      setNotesMsg(cleared ? "Notes vault cleared." : "No notes vault existed.");
    } catch (e) {
      setNotesMsg(String(e instanceof Error ? e.message : e));
    }
  };
```

Render the button just after the "Start run" button (before or after the existing `{error && ...}` line):

```tsx
      {workflow.notes_enabled && (
        <div className="notes-actions">
          <button className="link" onClick={clearNotes}>Clear notes vault</button>
          {notesMsg && <span className="field-hint">{notesMsg}</span>}
        </div>
      )}
```

- [ ] **Step 3: Typecheck + build**

Run: `cd atom-ui && npm run build`
Expected: `tsc` passes (no type errors) and `vite build` completes. (atom-ui has no unit-test runner; the build IS the verification.)

- [ ] **Step 4: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/Workflows.tsx
git commit -m "feat(ui): Clear notes vault button on notes-enabled workflows"
```

---

## Final verification

- [ ] Run the full Python suite: `.venv/bin/python -m pytest -q`
- [ ] Build the UI: `cd atom-ui && npm run build`
- [ ] Expected: all Python tests green; UI build clean.
