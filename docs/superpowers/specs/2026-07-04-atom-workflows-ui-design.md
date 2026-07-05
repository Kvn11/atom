# atom Workflows UI + present_files Integration — Design Spec

**Date:** 2026-07-04
**Status:** Approved (brainstorming complete)
**Builds on:** `docs/superpowers/specs/2026-07-03-atom-workflows-design.md` (the workflow engine/API/UI already shipped on `main`).

## 1. Motivation

The workflow feature shipped with a functional-but-rough React UI and a broken
link between the agent's `present_files` tool and what the UI shows as a run's
outputs. This spec closes that gap and redesigns the UI to be professional,
minimal, and able to monitor **hundreds of concurrent runs**.

### 1.1 The `present_files` gap (root cause)

- `present_files` (`src/atom/tools/present_files.py`) records deliverables into
  the agent's `artifacts` state channel as `[{"path": <virtual>, "physical":
  <absolute>}]`. `run_agent` returns that state on `RunResult.state`, so the
  presented files **are** available to the engine at
  `result.state["artifacts"]`.
- **The engine discards them.** `WorkflowEngine._run_task` persists only
  `result.messages` (the chat snapshot) via `store.save_chat`; it never reads
  `result.state["artifacts"]`. Nothing downstream knows which files the AI chose
  to present.
- The UI's `GET /api/runs/{id}/artifacts` instead does a blind `rglob` of the
  entire run workspace — it lists every scratch/intermediate file, cannot tell a
  deliverable from junk, and **misses** anything presented into a per-thread
  `/mnt/user-data/outputs` dir (which resolves outside the shared workspace).
  Content is surfaced via `alert()`.
- `workflows/parallel-poems.yaml` never calls `present_files`, so the feature is
  undemonstrated end to end.

## 2. Decisions (locked)

| Area | Decision |
|---|---|
| Artifacts scope | **Deliverables only** — the UI's artifact surface shows only files the AI explicitly presented via `present_files`. Intermediate workspace files are intentionally not surfaced. |
| Capture strategy | **Copy-at-capture** — presented files are copied into a run-local `artifacts/` dir at the moment they are presented (immutable snapshot; see §3.1). |
| Viewer richness | **Markdown + images + code** — render `.md` via `react-markdown` + `remark-gfm`, images inline, code/text in a monospace surface. No syntax-highlighting dep. |
| Run-view layout | **Two-pane console** — left rail = step→task pipeline + deliverables; center = tabbed Transcript / Deliverables. |
| Typography | Self-hosted **Inter** via `@fontsource/inter` (no runtime network); system monospace stack for transcripts/code. |
| Visual direction | Clean, light, near-neutral palette; generous whitespace; hairline borders; subtle elevation; one restrained accent; semantic status colors (green/amber/red/slate). |
| Scale target | UI request volume and payload size are **O(1) in the number of concurrent runs** (§5). |
| New frontend deps | `react-markdown`, `remark-gfm`, `@fontsource/inter`. |

## 3. Backend — capture & persist presented artifacts

### 3.1 Copy-at-capture (why copy)

When a task's agent returns, the engine reads `result.state["artifacts"]` and
the store **copies each presented file** into
`runs/<run_id>/artifacts/s<step>__<task>/<safe-name>`.

Copying (rather than pointing at the live file) is deliberate:

- **Immutability across steps.** In `parallel-poems`, step 2's *Refine* task
  overwrites the poems that step 1's *Draft* tasks presented. Copying snapshots
  each deliverable at present-time, so both the draft and the refined version
  survive, each attributed to the task that produced it.
- **Trivial confinement.** Served content is confined to the run's `artifacts/`
  dir; no need to allow arbitrary physical paths.
- **No path leak.** The absolute server path never reaches the client.

Capture is **best-effort**: a file that cannot be copied (missing, unreadable)
is skipped; it never flips a succeeded task to failed. A catastrophic error
(e.g. cannot create the artifacts dir) surfaces through the task's normal
failure path.

Collision handling: files are written under `s<step>__<task>/` keyed by
basename. If two presented virtual paths share a basename, the second is
disambiguated with a numeric suffix (`name.md`, `name-1.md`).

### 3.2 Data model (`src/atom/workflow/run_store.py`)

```python
class ArtifactRef(BaseModel):
    name: str   # display name (basename)
    path: str   # original virtual path as presented (e.g. /mnt/user-data/outputs/poem_a.md)
    rel: str    # path relative to runs/<run_id>/artifacts/, used for serving
    size: int   # bytes

class TaskState(BaseModel):
    ...                                    # existing fields unchanged
    artifacts: list[ArtifactRef] = Field(default_factory=list)
```

### 3.3 Store methods (`RunStore`)

- `artifacts_dir(run_id) -> Path` → `runs/<run_id>/artifacts`.
- `capture_artifacts(run_id, step_index, task_id, presented: list[dict]) ->
  list[ArtifactRef]` — copy each `{"path", "physical"}` entry, return refs.
  Robust to individual-file errors.
- `artifact_path(run_id, rel) -> Path | None` — resolve `rel` under
  `artifacts_dir`, reject `..`/escape (realpath-confined), return `None` on
  escape.

### 3.4 Engine wiring (`WorkflowEngine._run_task`)

After `save_chat`, on the success path:

```python
presented = (result.state or {}).get("artifacts", [])
ts.artifacts = self.store.capture_artifacts(
    manifest.run_id, step_state.index, ts.id, presented,
)
```

Capture happens inside the existing `try` so an error is caught by the task's
own failure handling; `capture_artifacts` is itself tolerant of per-file
failures so a copy hiccup does not fail an otherwise-successful task.

## 4. API surface

### 4.1 Presented artifacts (replaces the `rglob` endpoint)

- `GET /api/runs/{id}/artifacts` → manifest-derived list of presented
  deliverables, each `{name, path, rel, size, step, task}`. No filesystem walk,
  no scratch files. 404 if the run is unknown.
- `GET /api/runs/{id}/artifacts/{rel:path}` → serve from the captured
  `artifacts/` dir via `FileResponse` with a media type guessed from the
  suffix. Images render via `<img src>`; text/markdown fetch as text —
  uniformly, path-confined via `artifact_path`. 404 on unknown/escaping `rel`.

### 4.2 Run detail — unchanged

`GET /api/runs/{id}` still returns the authoritative full `RunManifest`
(including per-task `artifacts`). Only the currently-open run polls it.

## 5. Scale — hundreds of concurrent runs

**Principle:** watch the fleet through one cheap list poll; only the single run
you drill into polls full detail. Request volume and payload size stay O(1) in
the number of concurrent runs.

### 5.1 Compact summaries (`src/atom/workflow/run_store.py`)

```python
class RunSummary(BaseModel):
    run_id: str
    workflow: str
    status: str
    created_at: str
    ended_at: Optional[str] = None
    steps_total: int
    steps_done: int          # steps with status "complete"
    tasks_total: int
    tasks_done: int          # tasks with status "succeeded"
    current_step: Optional[str] = None   # title of the first non-complete step
```

- `RunStore.save(manifest)` additionally writes a tiny `summary.json` next to
  `run.json` (atomic replace). `run.json` is authoritative; `summary.json` is a
  cheap cache. `run.json` is written first.
- `RunStore.list_summaries(status=None, limit=50, offset=0) -> dict` reads the
  small `summary.json` files (not full manifests), so list cost is independent
  of manifest size. Returns
  `{"items": [RunSummary...], "total": int, "counts": {"active": int,
  "complete": int, "halted": int}}`. `status` filter is one of
  `active | complete | halted | all` (`active` = pending/running). Sorted by
  `created_at` descending, then sliced `[offset:offset+limit]`. A missing or
  corrupt `summary.json` falls back to deriving the summary from that run's
  `run.json`; an unreadable run is skipped.

Reading hundreds of tiny summary files per poll is fast. An index file is the
escalation path beyond low-thousands of runs; it is out of scope here (a single
shared index invites writer contention under many concurrent runs).

### 5.2 List endpoint (replaces the full-manifest list)

`GET /api/runs?status=<active|complete|halted|all>&limit=&offset=` →
`{items, total, counts}` from `list_summaries`. `status` defaults to `all`,
`limit` to 50, `offset` to 0.

## 6. Frontend — architecture

Stack unchanged: Vite + React + TypeScript SPA in `atom-ui/`, dev-proxied to
the API, built to `atom-ui/dist` and served by FastAPI in prod.

### 6.1 Navigation & views

Top-level nav: **Workflows** (launch) and **Runs** (monitor); the Runs tab
carries a live active-count badge. Views:

1. **Workflows** — refined cards (name, description, input count). Click → run
   form.
2. **Run form** — labeled inputs with required markers, descriptions as helper
   text, defaults as placeholders, inline submit error; Start → run view.
3. **Runs dashboard** — status filter tabs (Active / Complete / Halted / All
   with counts) + a **paginated** table (~50/page) of `RunSummary` rows: status
   pill, workflow, progress (`tasks_done/tasks_total`, current step),
   started/elapsed. Row → run view. Pagination avoids rendering hundreds of DOM
   rows.
4. **Run view (two-pane console)** — see §6.2.

### 6.2 Run view

- **Header (persistent):** `atom / <workflow>` breadcrumb, status pill
  (running/complete/halted, color+icon), compact "Step N of M" progress,
  elapsed time. Wordmark → Workflows.
- **Left rail:** step groups (title + status) → task rows (id, status dot, model
  badge); a **Deliverables** section listing every presented artifact
  (name · producing task · size).
- **Center:** tabbed **Transcript** / **Deliverables**.
  - *Transcript* — clean message timeline for the selected task. Tool calls are
    compact rows (name + concise arg summary); `present_files` calls are visually
    highlighted as the deliverable moment. AI/user text is legible; the
    serialized `tool_calls` args (already present in the API payload) drive the
    summaries.
  - *Deliverables* — a viewer that renders markdown (`react-markdown` +
    `remark-gfm`), shows images inline, and puts code/text in a monospace
    surface. Selecting a deliverable in the left rail opens it here.
- Default-select the running (or first) task so the center is never blank.
  Empty/loading states: "Select a task", "No deliverables yet", loading
  placeholders.

### 6.3 Live updates & scale

- The **Runs dashboard** polls the one list endpoint (~2.5s) regardless of how
  many runs are in flight; an `AbortController` cancels superseded/stale
  requests (on filter change, page change, unmount).
- The **run view** polls only its single open run (`GET /api/runs/{id}` +
  `.../artifacts`), stopping at a terminal status.
- Net: UI request volume is O(1) in concurrent-run count, not O(N).

### 6.4 Visual system

- **Typography:** Inter (self-hosted via `@fontsource/inter`) for UI/body; a
  system monospace stack (`ui-monospace, "SF Mono", Menlo, monospace`) for
  transcripts and code.
- **Palette:** light, near-neutral surfaces; hairline borders; subtle
  elevation; one restrained accent; semantic status colors — succeeded/complete
  green, running/pending amber, failed/halted red, idle slate.
- The `frontend-design` skill guides concrete tokens during implementation so
  the result reads as intentional rather than a templated default.

## 7. Example workflow

Update `workflows/parallel-poems.yaml` so the feature is demonstrable under
deliverables-only:

- Each *Draft* poet **presents** its poem after writing it.
- The *Refine* task sharpens each poem in place, **also compiles a combined
  `anthology.md`**, and **presents** the refined poems plus the anthology.
- Step 2 keeps a single presenting task, so there is no read/write race on the
  shared files within the step.

## 8. Testing

- **TDD (backend, `pytest`):**
  - `capture_artifacts` — copies files, returns refs, snapshots survive a later
    overwrite of the source (immutability), tolerates a missing source.
  - `artifact_path` — resolves under the artifacts dir, rejects `..`/escape.
  - Engine — a task's presented artifacts are persisted on `TaskState.artifacts`.
  - `list_summaries` — status filter, pagination (`limit`/`offset`), `counts`,
    `created_at` ordering, fallback when `summary.json` is missing.
  - API — `GET /api/runs` (paginated summaries + counts), artifacts list
    (presented only), artifact content (text + confinement/404 on escape).
- **Frontend:** the React SPA remains the **manual test surface** (build clean +
  eyeball), per the existing convention; not TDD.

## 9. Non-goals (explicit)

- **Engine-side global concurrency limiting.** Each run currently owns its own
  `max_parallel` semaphore, so hundreds of simultaneous runs imply hundreds ×
  `max_parallel` agent calls hitting the provider at once. That is a
  server-capacity concern (a global cap / queue), separate from *UI* scale, and
  is out of scope for this plan. Flagged for a follow-up.
- **A runs index file.** See §5.1 — tiny-summary reads + pagination handle
  hundreds; an index is the next step only beyond low-thousands.
- **Auth / multi-user.** Unchanged from the base harness (single `default`
  user).
