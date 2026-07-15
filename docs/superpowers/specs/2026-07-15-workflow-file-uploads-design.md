# Workflow file-upload inputs — design

- **Date:** 2026-07-15
- **Status:** Approved (brainstorm), pending implementation plan
- **Scope:** Extend workflow *inputs* to accept file uploads alongside the existing text inputs, across all four layers (declaration, API, UI, agent execution) plus CLI parity.

## Problem

Today a workflow input is a named string. `InputDef` (`src/atom/workflow/schema.py`) has only `name`/`required`/`description`/`default`; the API (`POST /api/runs`) is JSON-only (`RunRequest{workflow, inputs: dict}`); the UI renders a plain text `<input>` per input; and each input value is Jinja2-substituted into task prompts (`render_task_prompt`). There is no way to hand a workflow a file. atom already has an `uploads` *concept* — `VIRTUAL_UPLOADS` = `/mnt/user-data/uploads` (documented in `prompts/lead_system.md` as "read-only files the user provided"), an `{{ uploads }}` template var, and `UploadsMiddleware` that scans it — but in workflow mode the uploads dir resolves *per task-thread* (`src/atom/sandbox/paths.py::thread_paths`, `uploads = base / "uploads"`) and is never populated, so it does nothing.

## Guiding decision

A file input is **"just another named input"** carrying `type: file`. Its resolved value, everywhere downstream, is the **virtual path** to the uploaded file (`/mnt/user-data/uploads/<name>`). So a task prompt references it exactly like a text input:

- text input `{{ topic }}` → the text the user typed
- file input `{{ report }}` → `/mnt/user-data/uploads/report.pdf`

This keeps one mental model and requires **zero** changes to `render_task_prompt` — the file path is a plain string in the `inputs` dict.

### Locked decisions (from brainstorming)

1. **Storage/visibility:** a dedicated per-run `uploads/` dir, bound to the existing `{{ uploads }}` / `/mnt/user-data/uploads` mount, shared across all tasks and steps.
2. **Declaration:** a `type:` field on `InputDef` (not a separate `files:` list).
3. **Cardinality:** one file per file-typed input (declare multiple inputs for multiple files).
4. **Transport:** one-step multipart (`multipart/form-data`) carrying the text inputs + files in a single `POST /api/runs`.

### Decisions made during design (adjustable at spec review)

- **JSON API is preserved** via content-type sniffing; the submit endpoint accepts both `application/json` (existing behavior, unchanged) and `multipart/form-data` (new). Rationale: the CLI drives the engine directly (not HTTP), so the only HTTP JSON callers are the UI (being changed anyway), external automation, and the test suite — keeping JSON working is low-cost and avoids breaking them.
- **Uploads are per-run always** — even a text-only run gets an empty shared `runs/<run_id>/uploads/`, so the `{{ uploads }}` mount is consistently per-run. No observable change for existing runs (an empty dir either way).
- **`allowed_extensions` defaults to empty (allow any)** so nothing existing breaks; operators opt into restriction. Size and count caps are always on.
- **Read-only is not enforced** on the uploads mount (matches today's posture — it was never enforced); enforcement is a noted future hardening.
- **No auth added** — the API is already unauthenticated with wide-open CORS. Mitigation in this scope is size/type/count caps + filename sanitization; auth is a pre-existing, out-of-scope risk, flagged below.

## Architecture (end-to-end)

### 1. Declaration — `src/atom/workflow/schema.py`

- `InputDef` gains `type: Literal["text", "file"] = "text"`. Backward-compatible: existing YAML inputs (no `type`) stay text.
- `resolve_inputs(workflow, provided)` branches minimally on `inp.type`:
  - a provided non-empty value is used (unchanged);
  - for a `file` input, a text `default` is **not** applied (a default file path is meaningless) — so an absent optional file resolves to `""`, and an absent **required** file is collected into `missing` → `MissingInputError` (identical error path to a missing required text input);
  - text inputs keep exactly today's behavior.
- `render_task_prompt` is **unchanged** — the file path is a string value in `inputs`, so `{{ name }}` / `{{ inputs.name }}` already resolve to it.

### 2. Storage — `src/atom/workflow/run_store.py` + new `src/atom/workflow/uploads.py`

- `RunStore.uploads_dir(run_id)` → `runs/<run_id>/uploads`. `RunStore.create` mkdirs it alongside `workspace/` and `chats/`.
- `RunManifest` gains `uploads_path: Optional[str] = None`, set by `create_run` to `str(store.uploads_dir(run_id))`.
- **New pure module `src/atom/workflow/uploads.py`** (unit-testable, no I/O beyond what's passed in):
  - `safe_extension(original_filename: str) -> str` — the sanitized, lowercased suffix of the original name (e.g. `".pdf"`), or `""` if none/unsafe.
  - `stored_name(input_name: str, original_filename: str) -> str` — **the single source of truth for the on-disk name**: `sanitize(input_name) + safe_extension(original_filename)`. Because workflow input names are unique keys, this is **collision-free by construction** and **deterministic** — the API can compute the stored path before writing and it is guaranteed to equal what `save_upload` writes. (Trade-off: the agent sees `report.pdf` rather than the original `q3-results.pdf`; the input name is the meaningful handle, and this removes an entire class of pre-compute/write divergence + collision bugs. The sandbox `resolve()` also confines physically; `sanitize` is defense-in-depth.)
  - `virtual_upload_path(input_name, original_filename) -> str` → `VIRTUAL_UPLOADS + "/" + stored_name(...)`. Used by **both** the API/CLI (to fill `inputs[name]` before the run is created) and `save_upload` (to name the file) so they can never disagree.
  - `check_size(nbytes: int, limit: int)` → raises a typed `UploadTooLarge` when exceeded.
  - `check_extension(original_filename: str, allowed: list[str])` → raises a typed `UploadTypeNotAllowed` when `allowed` is non-empty and the extension is not in it.
- **`RunStore.save_upload(run_id, input_name, original_filename, data: bytes) -> str`** — writes bytes into `uploads_dir(run_id)` at `stored_name(input_name, original_filename)` and returns the **virtual** path `virtual_upload_path(...)`. No collision disambiguation needed (names are unique per input). Both the API and CLI call this, staying symmetric.

### 3. Mount plumbing — `src/atom/sandbox/paths.py`, `src/atom/runtime.py`, `src/atom/state.py`

Mirrors exactly how the existing `workspace` override flows:

- `thread_paths(user_id, thread_id, *, home=None, workspace_override=None, uploads_override=None)` — when `uploads_override` (an absolute path) is given, `uploads = Path(uploads_override).expanduser().resolve()` instead of `base / "uploads"`.
- `thread_paths_from_context(ctx, ...)` reads a new `ctx.get("uploads_path")` and passes it as `uploads_override`.
- `WorkspaceContext` (`src/atom/state.py`, a `TypedDict, total=False`) gains `uploads_path: Optional[str]`, adjacent to `workspace_path`.
- `run_agent(..., uploads: str | None = None)` (`src/atom/runtime.py`) forwards to `_build_context`, which sets `"uploads_path": str(Path(uploads).expanduser().resolve()) if uploads else None`.
- `WorkflowEngine._run_task` (`src/atom/workflow/engine.py`) passes `uploads=manifest.uploads_path` into its `run_agent(...)` call.
- `ThreadPaths.virtual_map()` already maps `VIRTUAL_UPLOADS → self.uploads`, so overriding `self.uploads` automatically remaps the mount — no change to `virtual_map`, `SandboxMiddleware`, or `UploadsMiddleware`.
- `ThreadPaths.ensure()` mkdirs uploads with `exist_ok=True`; it never deletes, so a pre-populated shared uploads dir is safe. **No change needed.**

**Confinement — no change.** `LocalSandboxProvider._check_external_workspace` (`src/atom/sandbox/provider.py`) validates only the *workspace* against `allowed_workspace_roots`. The uploads mount is confined by `LocalSandbox.resolve()` to stay within its own mapped root regardless of `allowed_workspace_roots`, so the per-run uploads override needs no `allowed_workspace_roots` change. (This corrects an earlier exploration caveat.)

### 4. Agent visibility

- **Primary contract:** the workflow author references the file via `{{ name }}` in the relevant task prompt(s) — identical to how a text input must be referenced to be used. The value is the file's virtual path.
- **Ambient:** the lead system prompt already documents `{{ uploads }}` as "read-only files the user provided," and `UploadsMiddleware.before_agent` now scans the shared per-run dir into `uploaded_files` state (previously always empty in workflow mode).
- **Consumption:** text-read tools cap at 2 MB and decode UTF-8 (`LocalSandbox.read_text`, `_READ_MAX_BYTES`), so the agent reads text/CSV/Markdown directly; binary (PDF, images) is reachable by path for `bash` tools or `view_image`, the same as any workspace file.

### 5. API — `src/atom/api/app.py`, `src/atom/api/models.py`, `pyproject.toml`

- Add `python-multipart` to `[project].dependencies` (required by Starlette to parse multipart forms).
- `submit_run` becomes content-type-aware. It takes the raw `Request` and branches:
  - **`application/json`** → parse `RunRequest` from the body and run the existing path unchanged (no files).
  - **`multipart/form-data`** → `await request.form()`; read `workflow` (str field), `inputs` (a JSON string field holding the text inputs dict; default `{}`), and every file part (`starlette.datastructures.UploadFile`) keyed by its declared input name.
- **Submit flow (multipart):**
  1. `load_workflow(workflow)` → `404` if missing.
  2. Determine the declared `file`-typed input names; reject any uploaded field that is not a declared file input → **400**.
  3. Enforce `max_files_per_run`, and per file enforce `max_file_bytes` (via `UploadFile.size`, with a bounded-read fallback if `None`) and `allowed_extensions` → **413** / **415**.
  4. `run_id = uuid4().hex[:12]`.
  5. Build merged `inputs` = text inputs + `{file_input_name: virtual_upload_path(name, filename)}` (deterministic; no writing yet).
  6. `engine.create_run(wf, inputs, run_id)` — creates the run dir (`workspace/`, `chats/`, `uploads/`), sets `uploads_path`, and `resolve_inputs` raises `MissingInputError` → **422** if a required file (or text) input is absent.
  7. Persist each file's bytes via `RunStore.save_upload(run_id, name, filename, data)` into the now-existing uploads dir. Because `save_upload` and step 5 both derive the name from `stored_name(name, filename)`, the written path is guaranteed to equal the `inputs[name]` value stored in the manifest — no divergence possible.
  8. `engine.enqueue(run_id)`.
- **`enqueue` stays the commit point.** A crash between `create_run` and `enqueue` leaves a non-enqueued `pending` run that no worker picks up (recovery only re-queues `pending`/`running` interrupted runs via the summary scan — an un-enqueued pending run that was never meant to run is harmless and inert). Files are on disk before enqueue, so an executing run always sees them.
- `GET /api/workflows` already returns `[i.model_dump() for i in w.inputs]`, so the new `type` field auto-propagates to clients (the UI) with no endpoint change.

### 6. UI — `atom-ui/src/api.ts`, `atom-ui/src/Workflows.tsx`

- `InputDef` TS interface gains `type?: "text" | "file"`.
- `RunForm`: for `i.type === "file"` render `<input type="file">` and track the selected `File` in a separate `files: Record<string, File>` state; text inputs render as today. Show a "required" pill for required files; basic client-side hinting only (server is the source of truth for validation).
- `api.submit(workflow, inputs, files?)`: when `files` is non-empty, build a `FormData` (`workflow`, `inputs` = `JSON.stringify(textValues)`, each file appended under its input name) and POST **without** an explicit `Content-Type` header so the browser sets the multipart boundary; otherwise keep the existing JSON POST.
- Vite dev proxy already forwards `/api` (`atom-ui/vite.config.ts`) — no change.

### 7. CLI parity — `src/atom/cli.py`

- `atom workflow run` gains a repeatable `--file / -f name=path`.
- Validate each token is `name=path`, that `name` is a declared `file` input, that `path` exists and passes the size/type limits.
- Since the CLI drives the engine directly, the flow mirrors the API: build `inputs` with intended virtual paths → `create_run` → read local bytes and `RunStore.save_upload(...)` → `enqueue` → `await_run`. Uses the same `uploads.py` helpers.

### 8. Config — `src/atom/config/schema.py`

New `UploadsConfig(_Base)`, added as `AtomConfig.uploads`:

```python
class UploadsConfig(_Base):
    max_file_bytes: int = 26_214_400        # 25 MiB per file
    allowed_extensions: list[str] = []       # empty = allow any; else lowercase, no dot
    max_files_per_run: int = 20              # safety cap on an unauthenticated surface
```

Config-driven per the project's foundation preference.

## Data model / interface changes (summary)

| File | Change |
|---|---|
| `src/atom/workflow/schema.py` | `InputDef.type`; `resolve_inputs` file branch |
| `src/atom/workflow/uploads.py` | **new**: `stored_name`, `virtual_upload_path`, `safe_extension`, `check_size`, `check_extension`, typed errors |
| `src/atom/workflow/run_store.py` | `uploads_dir`; `RunManifest.uploads_path`; `create` mkdir; `save_upload` |
| `src/atom/workflow/engine.py` | `create_run` sets `uploads_path`; `_run_task` passes `uploads=` |
| `src/atom/sandbox/paths.py` | `thread_paths(uploads_override=)`; `thread_paths_from_context` reads `uploads_path` |
| `src/atom/state.py` | `WorkspaceContext.uploads_path` |
| `src/atom/runtime.py` | `run_agent(uploads=)`; `_build_context` sets `uploads_path` |
| `src/atom/config/schema.py` | `UploadsConfig`; `AtomConfig.uploads` |
| `src/atom/api/app.py` | `submit_run` multipart + JSON; limit/type errors |
| `src/atom/api/models.py` | (unchanged `RunRequest`; multipart parsed inline) |
| `pyproject.toml` | add `python-multipart` |
| `atom-ui/src/api.ts` | `InputDef.type`; `api.submit` FormData path |
| `atom-ui/src/Workflows.tsx` | file input rendering + file state |
| `src/atom/cli.py` | `workflow run --file` |
| `workflows/summarize-doc.yaml` | **new** example |

## Security

The API is unauthenticated with open CORS today; adding uploads widens that surface. In-scope mitigations: `max_file_bytes`, `max_files_per_run`, optional `allowed_extensions` allowlist, and deterministic input-name-derived `stored_name` (basename-only, traversal-stripped — the original client filename never becomes an on-disk path) layered over the sandbox's physical `resolve()` confinement. Authentication is **out of scope** (a pre-existing gap) and flagged as a follow-up. Read-only enforcement on the uploads mount is likewise deferred.

## Testing

- **schema:** `InputDef` parses `type`; `resolve_inputs` — required file missing raises `MissingInputError`; provided file path used; `default` ignored for file inputs; text behavior unchanged.
- **uploads.py:** `stored_name` / `safe_extension` (path traversal, empty name, unicode, missing/unsafe extension; distinct input names → distinct stored names); `virtual_upload_path` matches `save_upload`'s written path; `check_size` / `check_extension` raise the typed errors at their boundaries.
- **run_store:** `save_upload` writes into the run's uploads dir at the deterministic `stored_name` and returns the matching virtual path; `create` makes `uploads/`.
- **paths:** `thread_paths(uploads_override=...)` binds the shared dir; `virtual_map` maps it; `thread_paths_from_context` reads `uploads_path`.
- **API:** multipart submit happy path (file on disk, manifest `inputs` holds the virtual path, run enqueued); JSON submit still works (backward-compat); missing required file → 422; oversize → 413; disallowed extension → 415; undeclared file field → 400.
- **engine (integration, fake model via `prepared`):** a workflow with a file input runs end-to-end; the task prompt `{{ document }}` resolves to `/mnt/user-data/uploads/...`; the agent reads the file from the mount.
- **CLI:** `--file name=path` persists the file and resolves it in the run.
- **UI:** no test harness exists today → verified via `npm run build` + manual run. (Adding a harness is out of scope.)

## Out of scope / future

- Multiple files per single input.
- Per-input `accept` / size overrides in `InputDef`.
- Enforced read-only uploads mount.
- API authentication.
- Surfacing `uploaded_files` state into the system prompt (agent currently learns of files via the referenced `{{ name }}` path).
