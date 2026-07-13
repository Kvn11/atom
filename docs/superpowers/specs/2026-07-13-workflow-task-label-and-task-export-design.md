# Design: Workflow message relabel + task-granularity export

Date: 2026-07-13

Two independent changes to the workflow harness:

1. Relabel the initial message of a workflow task from `human` to `task` — it is an
   automated workflow prompt, not a human chat turn.
2. Extend the run exporter to a **task** granularity: a single task's LangSmith trace
   becomes exportable as soon as that task completes, while the existing whole-run export
   remains available once all steps complete.

Both are on-demand (never coupled into the hot execution path).

---

## Change 1 — Relabel the initial workflow message `human` → `task`

### Problem

A workflow task opens with `HumanMessage(content=<rendered prompt>)` (`runtime.py:124`). When the
engine snapshots the task transcript, `serialize_messages` (`run_store.py`) derives the display
`role` from the LangChain message *type* (`"human"`), and the SPA renders it verbatim
(`RunView.tsx`). The label is inaccurate: nothing human authored that turn — the workflow
orchestrator did.

The LangChain message **must remain** a `HumanMessage` (chat providers require a human/user turn
to open a conversation), so the fix is at the **serialization layer**, not the message type.

### Change

In `serialize_messages`, relabel the **first `human`-role message** in the list to `"task"`.
Subsequent human-role messages (skill-activation notes, view-image blocks injected mid-turn) keep
`human`. Only the opening prompt is retitled.

- Rule: track a "have we relabeled the opening human turn yet?" flag; the first message whose role
  resolves to `human` becomes `task`, all later ones are untouched. Using "first human-role
  message" (rather than "index 0") is robust to a non-human message ever preceding it.
- This makes `task` the single source of truth: the persisted `chats/*.json`, the `/messages` API
  response, and the UI all show `task`.

### Blast radius (why this is safe)

`serialize_messages` is used **only** by the workflow engine (`engine.py`, task-snapshot path).
Interactive `atom run` / `atom chat` do not call it, so genuine human turns there are unaffected.

### Touch list

- `src/atom/workflow/run_store.py` — relabel logic in `serialize_messages`.
- `atom-ui/src/RunView.tsx` / `atom-ui/src/styles.css` — add a `.msg.task` transcript style
  (rendering already reads `m.role`; no logic change needed).
- `tests/test_workflow_run_store.py` — update `test_serialize_messages_shape` for the new label.

---

## Change 2 — Task-granularity export

### Enabler

Every task's trace already carries `session_id = <task thread_id>` metadata, and sub-agents nest
under the lead root **sharing** that `session_id` (`observability/trace.py`). So a single task's
full tree is fetchable with `is_root=True` + `eq(session_id, <thread_id>)` — the per-task analog of
today's run-level `eq(run_id, <run_id>)` fetch (which spans every task). The existing exporter
docstring already notes that `session_id` "would only capture one task"; here that is exactly what
we want.

### Export module (`src/atom/observability/export.py`)

- **`export_task(home, run_id, step_index, task_id, *, project, client=None, poll_timeout, poll_interval, now, sleep, monotonic)`**
  - Loads the manifest (`FileNotFoundError` if the run is unknown).
  - Resolves the step by `index` and the task by `id`. Unknown step/task → `KeyError`.
  - Requires the task to be **terminal**: status in `("succeeded", "failed")`. A `pending`/`running`
    task → `ValueError` (its trace would be incomplete). Failed tasks *are* exportable — their
    traces matter for evaluation.
  - Requires `project` and `LANGSMITH_API_KEY` (same guards as `export_run`).
  - Fetches the one root by `session_id = task.thread_id`, polling until `fetched >= 1` or
    `poll_timeout` (absorbs LangSmith async-ingestion lag), with `expected = 1`.
  - No traces (`fetched == 0`) → writes nothing, returns `path == ""` (same contract as `export_run`).
  - Otherwise writes `runs/<run_id>/exports/s<step>__<task_id>.json` via tmp+`os.replace` (atomic,
    mirroring the `chats/` layout). Returns an `ExportResult`.
- **`fetch_task_tree(client, project, session_id)`** — sibling of `fetch_run_tree`, filtering by
  `session_id` metadata. Both delegate to a shared `_fetch_roots(client, project, key, value)`
  helper (list roots by metadata key/value, hydrate each with `load_child_runs=True`).
- **`build_envelope`** gains `scope: "run" | "task"` plus `task_id` / `session_id` fields (so an
  offline eval pipeline knows the export's granularity). Run-level envelopes set `scope="run"`,
  `task_id=None`.
- **`ExportResult`** gains `task_id: Optional[str] = None` (defaulted → backward compatible).
- **`RunStore`** gains `task_export_path(run_id, step_index, task_id)` →
  `run_dir/exports/s<step>__<task_id>.json`.

Run-level `export_run` and its `runs/<id>/export.json` path are **unchanged**.

### CLI (`src/atom/cli.py`, `atom workflow export`)

- Add `--task <step_index>:<task_id>` (e.g. `0:writer`; step is the 0-based manifest step index).
- With `--task`:
  - Resolve **exactly one** run (positional `run_id` or `--latest`); `--all` + `--task` is rejected
    (exporting "one task across many runs" is meaningless).
  - Parse the `step:task_id` selector (malformed → clear error, exit 1).
  - Call `export_task`; surface unknown-task / not-completed / no-traces / missing-key / API errors
    with the same message-and-exit-code style as the existing run path.
- Without `--task`: existing whole-run behavior is unchanged.

### API + UI (powers the button — the SPA cannot invoke the CLI)

- **`POST /api/runs/{run_id}/export`** (`src/atom/api/app.py`), body `ExportRequest {step?: int, task?: str}`:
  - `step` **and** `task` present → `export_task`; otherwise → `export_run` (whole run).
  - Declared as a **sync `def`** so FastAPI runs the blocking poll in its threadpool (never blocks
    the event loop / queue worker).
  - Error mapping: `FileNotFoundError` → 404; unknown task (`KeyError`) → 404; not-terminal task /
    no project (`ValueError`) → 400; missing API key (`RuntimeError`) → 400; LangSmith/network
    (other `Exception`) → 502.
  - Returns `{run_id, scope, task_id, path, complete, expected_roots, fetched_roots}`. `fetched==0`
    is a 200 with `complete=false` (UI shows "no traces found").
  - `ExportRequest` added to `src/atom/api/models.py`.
- **UI** (`atom-ui`):
  - `api.ts`: `exportRun(id, body?)` → `POST`, typed `ExportResponse`.
  - `RunView.tsx`: an **Export task** button in the transcript tabbar, enabled only when the
    selected task is terminal (`succeeded`/`failed`); and an **Export run** button in the run header,
    enabled only when `manifest.status === "complete"` (matches "if all steps complete"). Each shows
    a transient result line (`Exported → …`, `No traces found`, or the error text). Simple
    idle/exporting/done/error local state.
  - `styles.css`: minimal button + result-line styling.

### Gating summary

| Action        | Enabled when            |
|---------------|-------------------------|
| Export task   | task status is terminal (`succeeded`/`failed`) |
| Export run    | run status is `complete` |

### On-disk layout

```
runs/<run_id>/
  run.json
  export.json                       # whole-run export (unchanged)
  exports/
    s0__writer.json                 # per-task exports (new)
    s1__editor.json
  chats/  artifacts/  workspace/
```

---

## Testing

- `tests/test_export.py`: `export_task` happy path (asserts the `session_id` filter, `expected=1`,
  envelope `scope="task"` + `task_id`), non-terminal task → `ValueError`, unknown task → `KeyError`,
  no-traces writes nothing, partial/complete on poll timeout; `fetch_task_tree` filter shape.
- `tests/test_cli_export.py`: `--task` success (path printed), non-terminal / unknown-task → exit 1,
  `--task` + `--all` rejected, malformed selector rejected.
- `tests/test_workflow_api.py`: export endpoint whole-run + task happy paths (monkeypatching
  `export_run`/`export_task`, matching the CLI-test seam), run-not-found → 404, no-project → 400.
- SPA has no unit-test harness (the app serves `/api` only in tests); keep TS types sound and
  ensure the build/tsc passes.

## Docs

Update `README.md` (export section) and the `export.py` module docstring to describe the `--task`
selector and the `exports/` layout.

## Out of scope (YAGNI)

- Auto-export on task completion (explicitly on-demand only).
- Changing run-level export gating/partial-write behavior.
- Relabeling injected mid-turn human notes (only the opening prompt is retitled).
