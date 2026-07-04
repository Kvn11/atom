# atom Workflows — Design Spec

**Date:** 2026-07-03
**Status:** Approved (brainstorming) → ready for implementation plan
**Feature:** Multi-agent workflows (Steps × Tasks) over the atom harness, with a LangSmith-traced
execution engine, an automation-first HTTP API, and a React test UI.

---

## 1. Goal & core insight

Add a **workflow** layer on top of the existing atom lead agent. A workflow is a group of lead
agents ("tasks") organized into ordered **steps**; tasks within a step run in parallel, and all
tasks in a run collaborate through **one shared workspace**. A task is *nothing more than an
automated first message sent to a lead agent* — i.e. a `run_agent` call with a custom prompt.

The design leans entirely on capabilities atom already has:

- **One shared workspace, separable chats.** Each task gets its **own `thread_id`** (its own
  checkpointed chat history) but binds the **same directory** via the existing *existing-workspace*
  mode (`WorkspaceContext.workspace_mode = "existing"`, `workspace_path = <shared dir>`). This
  yields "many agents, one workspace, per-task chats" with **no new sandbox-sharing code**.
- **Per-task model + thinking** map directly to the existing `run_agent(override_model=…,
  override_thinking=…)` arguments.
- **Observability** rides on `agent.ainvoke(config={...})`; we thread `run_name`/`tags`/`metadata`
  through `run_agent` so LangSmith separates and tags every task.

The workflow engine is therefore an **orchestrator over `run_agent`**. The harness itself changes
only minimally (one optional `trace` argument on `run_agent`).

Non-goals for this phase: durable job queue (Celery/RQ), cross-task file locking, resumable
in-flight runs after a server restart, per-task tool/skill customization, clarification handling
in automated runs. Seams are left where later phases would add these.

---

## 2. Workflow definition (YAML)

Workflow definitions live in `$ATOM_HOME/workflows/*.yaml`. A repo `workflows/` directory ships the
example (`parallel-poems.yaml`); the loader reads from the ATOM_HOME directory at runtime.

```yaml
name: parallel-poems
description: Draft poems in parallel, then refine and compile them.
inputs:
  - name: topic
    required: true
    description: What the poems are about.
  - name: style
    required: false
    default: free verse
steps:
  - title: Draft
    description: Three poets each draft one poem.
    tasks:
      - id: poet_a
        prompt: "Write a {{ style }} poem about {{ topic }}. Save it as poem_a.md in the workspace."
        model: haiku
        thinking: low
      - id: poet_b
        prompt: "Write a {{ style }} poem about {{ topic }} from a child's view. Save as poem_b.md."
      - id: poet_c
        prompt: "Write a {{ style }} poem about {{ topic }} as a sonnet. Save as poem_c.md."
  - title: Refine & compile
    description: Improve each poem, then assemble an anthology.
    tasks:
      - id: refiner
        prompt: "Read every poem_*.md in the workspace and sharpen each for imagery and rhythm, saving in place."
        model: opus
        thinking: high
```

### Pydantic schema (`atom/workflow/schema.py`)

- **`InputDef`**: `name: str`, `required: bool = False`, `description: str | None = None`,
  `default: str | None = None`. (Strings only in phase 1.)
- **`TaskDef`**: `id: str` (defaults to `task_<n>` when omitted), `prompt: str` (Jinja template,
  required), `model: str | None = None`, `thinking: str | int | None = None`.
- **`StepDef`**: `title: str`, `description: str | None = None`, `tasks: list[TaskDef]`
  (non-empty).
- **`WorkflowDef`**: `name: str`, `description: str | None = None`, `inputs: list[InputDef] = []`,
  `steps: list[StepDef]` (non-empty). Task ids must be unique within a step (validator).

### Loader

`load_workflow(name, home) -> WorkflowDef` and `list_workflows(home) -> list[WorkflowDef]` read and
validate YAML from `$ATOM_HOME/workflows/`. Invalid YAML or schema violations raise a clear error
(surfaced as a 4xx by the API).

### Prompt templating

Each task prompt is rendered with the existing `atom.prompts.render.render_prompt` machinery
(Jinja2, `StrictUndefined`) over a context of:

- every input key at top level (`{{ topic }}`) **and** under `inputs` (`{{ inputs.topic }}`),
- workspace virtual paths (`workspace`, `uploads`, `outputs`) and `date`.

Optional inputs that were not provided fall back to their `default` (or `""` if no default), so
`StrictUndefined` never trips on a declared-but-omitted optional input.

---

## 3. Run & persistence model

Each submission mints a run directory:

```
$ATOM_HOME/workflows/runs/<run_id>/
    workspace/     # the shared bind target (existing-workspace mode) for every task
    run.json       # the manifest — single source of truth for API/UI
```

The **orchestrator is the single writer** of `run.json`, written atomically (temp file + `os.replace`),
so parallel tasks never race on it. Tasks write to `workspace/` and to their own checkpoint; they
never touch the manifest.

### Manifest (`RunManifest`, `atom/workflow/run_store.py`)

```
run_id: str
workflow: str
inputs: dict[str, str]
status: "pending" | "running" | "complete" | "halted"
created_at: str (iso)         # stamped by the caller/API, not inside the engine
ended_at: str | None
workspace_path: str
steps: [
  { index: int, title: str,
    status: "pending" | "running" | "complete" | "failed",
    tasks: [
      { id: str, thread_id: str, model: str | None, thinking: (str|int|None),
        status: "pending" | "running" | "succeeded" | "failed",
        started_at: str | None, ended_at: str | None, error: str | None }
    ] }
]
```

A task's **chat** is not duplicated in the manifest — it is recovered live from the checkpointer by
`thread_id`, where `thread_id = "<run_id>:s<step_index>:<task_id>"`.

`run_store.py` provides: `create_run(...) -> RunManifest`, `save(manifest)` (atomic),
`load(run_id) -> RunManifest`, `list_runs() -> list[RunManifest summaries]`, and
`workspace_dir(run_id) -> Path`.

Because `Date.now()`-style timestamps must not live deep in pure logic, timestamps are stamped by
the API/CLI boundary (the caller) and passed into the store, keeping status computation pure and
testable.

---

## 4. Execution semantics

`WorkflowEngine` (`atom/workflow/engine.py`) drives a run:

- **Steps run sequentially; tasks within a step run concurrently** via `asyncio.gather`, bounded by
  a semaphore of size `max_parallel` (config, default **4**).
- **Task success** = its `run_agent` call returns without raising and without hitting the per-task
  timeout. **Task failure** = an exception or timeout. The failure message is stored in
  `task.error`.
- **Step outcome** (pure function in `atom/workflow/status.py`):
  - all tasks `succeeded` → step `complete` → proceed to the next step;
  - otherwise (any task `failed` — i.e. partial **or** all-fail) → step `failed` → **run halts**
    (`run.status = "halted"`), remaining steps stay `pending` and never run.
- **Run outcome**: all steps `complete` → `complete`; first `failed` step → `halted`.
- **`ask_clarification`** is left unchanged. If a task asks instead of finishing, `run_agent`
  returns normally (`awaiting_clarification=True`); the task counts as **succeeded** and its output
  is the question. Revisit only if it proves a problem in practice.

### Task execution

For each task the engine:

1. Renders the task prompt with the run inputs (§2 templating).
2. Calls `run_agent(prompt, config=cfg, profile=<base>, override_model=task.model,
   override_thinking=task.thinking, workspace=<run workspace path>, thread_id=<task thread id>,
   trace=<observability dict>, prepared=<from DI provider>)`.
   - `workspace=<path>` binds the shared dir in *existing* mode for every task.
   - The base profile is the config default profile (per-task profile overrides are a later add).
3. Marks the task `succeeded`/`failed`, records timing + any error, and saves the manifest.

### Shared workspace & confinement

The run workspace lives under `$ATOM_HOME` and is engine-controlled, so binding it must **not** be
blocked by `sandbox.allowed_workspace_roots` (which restricts *user-supplied* external dirs). The
engine binds the run workspace directly (ATOM_HOME's `workflows/runs/**` is implicitly trusted);
the allowed-roots gate continues to apply only to user-provided `--workspace` paths.

### Concurrent-write caveat

Parallel tasks share one workspace but hold **per-thread** (per-sandbox) file locks, not cross-task
locks — two tasks writing the *same* file can race. Design guidance: give parallel tasks **distinct
output files** (as the example does). Documented limitation; a cross-task lock is a later add.

### Testability seam (DI)

`WorkflowEngine(cfg, *, prepared_provider=None, task_timeout=…)`. `prepared_provider(task) ->
PreparedModel | None` lets tests inject `make_prepared([...scripted messages...])` per task so the
engine drives the **real graph + real shared workspace** (files genuinely written in step 1 and
read in step 2). Default `None` → real `prepare_model` per task.

### Hosting model

The engine runs **in-process** as background `asyncio` tasks inside the FastAPI app (and inline in
the CLI). Manifests on disk let the API list/inspect past runs after a restart; in-flight runs are
**not** resumed after a restart in this phase (documented; a durable queue is a later phase).

---

## 5. Observability (LangSmith)

`run_agent` gains an optional `trace: dict | None` argument merged into the invoke `config`:

- `run_name = "{workflow}/{step_title}/{task_id}"` — each task is its own top-level trace.
- `tags = ["atom-workflow", "workflow:{name}", "step:{title}", "task:{id}", "run:{run_id}"]`
- `metadata = {workflow, run_id, step_index, step_title, task_id}`

`atom/workflow/observability.py` builds this dict (`build_trace(...)`) and is unit-tested in
isolation. Tracing activates purely from environment variables (`LANGSMITH_TRACING`,
`LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`) — **no hard dependency** on `langsmith`. When those are
unset, `trace` is harmless metadata. Traces come out separated per task and filterable by
workflow/step/run.

---

## 6. HTTP API (FastAPI — the automation surface)

`atom/api/app.py` (routes) + `atom/api/models.py` (request/response pydantic).

```
GET  /api/workflows                              -> [{name, description, inputs}]
GET  /api/workflows/{name}                       -> full definition
POST /api/runs        {workflow, inputs}         -> 202 {run_id, status}     # submit a job
GET  /api/runs                                   -> run summaries
GET  /api/runs/{id}                              -> full manifest (poll for progress)
GET  /api/runs/{id}/tasks/{step}/{task}/messages -> that agent's chat (from the checkpointer)
GET  /api/runs/{id}/artifacts                    -> shared-workspace file listing
GET  /api/runs/{id}/artifacts/{path}             -> file content / download
```

- `POST /api/runs` validates required inputs (**422** on missing/empty required input), creates the
  run + manifest, starts the engine as a background asyncio task, and returns **202** with
  `{run_id, status}` immediately. Automation flow: **submit → poll `GET /runs/{id}` → fetch
  artifacts** once `status` is terminal.
- `GET …/messages` reads the checkpointed state for the task's `thread_id` and returns a
  serialized message list (role, text, tool calls) for display.
- `GET …/artifacts` lists files under the run `workspace/` (relpath, size, mtime); the `{path}`
  variant returns content, path-confined to the workspace (reject `..`/absolute escapes).
- CORS is enabled for the React dev origin. The built React `dist/` is served as static files at
  `/` so `atom serve` yields a single origin in production.

---

## 7. CLI (reuses the same engine)

Add to `atom/cli.py`:

```
atom workflow list                                           # available definitions
atom workflow run <name> --input topic="the sea" --input style=haiku   # submit + poll to done
atom workflow runs                                           # list runs
atom serve [--host --port]                                   # launch FastAPI + UI (uvicorn)
```

`atom workflow run` is a blocking convenience wrapper: it starts a run and polls the manifest to
completion, printing step/task progress, then the artifact list.

---

## 8. React test UI (Vite + TypeScript)

`atom-ui/` — a small SPA over the API, three views:

1. **WorkflowList** — `GET /api/workflows`; cards to pick one.
2. **RunForm** — a dynamic form generated from the selected workflow's inputs schema (required
   fields marked); submit → `POST /api/runs` → navigate to the run view.
3. **RunView** — polls `GET /api/runs/{id}` (~1.5s): steps rendered as sections, tasks as
   selectable status chips; selecting a task loads its chat (`…/messages`); an **Artifacts** panel
   lists workspace files (`…/artifacts`) with a viewer/download.

Dev: Vite dev server proxies `/api` to FastAPI. Prod: `npm run build` → FastAPI serves `dist/`.

---

## 9. Module layout & dependencies

```
src/atom/workflow/
    schema.py         # WorkflowDef/StepDef/TaskDef/InputDef + load_workflow/list_workflows + templating
    status.py         # pure step/run status computation
    run_store.py      # RunManifest + run dir + atomic save/load/list + workspace_dir
    observability.py  # build_trace(...) -> {run_name, tags, metadata}
    engine.py         # WorkflowEngine.start()/_run(); task execution; DI prepared_provider
src/atom/api/
    app.py            # FastAPI routes + static mount + CORS
    models.py         # request/response models
src/atom/runtime.py   # + optional `trace` arg threaded into ainvoke config
src/atom/cli.py       # + `workflow` subcommands + `serve`
workflows/parallel-poems.yaml    # shipped example
atom-ui/              # Vite React TS app (WorkflowList · RunForm · RunView · api client)
tests/
    test_workflow_schema.py      # load/validate, required-input validation, templating
    test_workflow_status.py      # pure status function
    test_workflow_run_store.py   # atomic manifest read/write/list
    test_workflow_engine.py      # end-to-end with FakeChatModel (shared-workspace hand-off, halt)
    test_workflow_api.py         # FastAPI TestClient (submit, poll, messages, artifacts)
```

New dependencies: `fastapi`, `uvicorn[standard]`, `httpx` (tests). LangSmith stays env-only (no
hard dependency).

---

## 10. Testing strategy (TDD)

**Strict TDD (pytest, no API keys)** for all Python:

- schema load/validation and unique-task-id enforcement;
- required-input validation (missing required → error) and optional-default fallthrough;
- prompt templating (`{{ topic }}` / `{{ inputs.topic }}`, optional defaults);
- the **pure status function** (all-success → complete/progress; any-fail → step failed → halt);
- atomic manifest read/write/list;
- **engine end-to-end with `FakeChatModel`**, including the load-bearing tests:
  - a 2-step run where step-1 tasks write `poem_*.md` and a step-2 task **reads them back from the
    shared workspace** (proves the workspace-sharing design), and
  - a failing step-1 task → step halts → step 2 **never runs**;
- observability `build_trace(...)` shape;
- API via FastAPI `TestClient` + injected fake models (submit → poll to terminal → messages →
  artifacts), memory checkpointer.

The engine's `prepared_provider` DI seam lets these tests exercise the **real graph and real shared
workspace** with scripted models — no provider calls.

**React UI** is built **without strict TDD** — it is the manual/e2e test surface (a Playwright
smoke can come later). Per the TDD skill this is the explicit "generated/UI code" exception, agreed
with the project owner.

---

## 11. Decisions locked during brainstorming

| Question | Decision |
|---|---|
| Partial step (some tasks fail) | **Halt the run** — only an all-success step progresses. |
| Step → step hand-off | **Shared workspace only** — later tasks read earlier tasks' files. |
| UI + API stack | **FastAPI REST API + React (Vite/TS) SPA.** |
| `ask_clarification` in tasks | **Left as-is** — rely on clear prompts; revisit if it bites. |
| Orchestration hosting | **In-process asyncio** (no queue in phase 1). |
| `max_parallel` default | **4.** |
