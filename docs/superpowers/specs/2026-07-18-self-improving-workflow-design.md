# Self-improving workflow

**Date:** 2026-07-18
**Status:** Approved, ready for planning
**Area:** `observability/run_log` (new), `api/app`, `workflow/run_store`, `workflows/self-improve.yaml` (new), `atom-ui` (`RunView`, `api.ts`)

## Problem

Improving a workflow today is entirely manual: export a run, eyeball the traces, feed
them to an LLM by hand, decide what to change, and edit the YAML. Nothing about it is
repeatable, and the interesting signal — which tasks failed and *why*, where the
bottlenecks were, which tool calls errored, and which steps burned the most context — is
buried in a tens-of-MB trace export that no one reads end to end.

We want a one-click loop: from any finished run, produce (a) an **improved copy of that
workflow's YAML** with the workflow-related problems fixed, and (b) a **suggestions
report** for the problems that a YAML edit *can't* fix (unclear tool descriptions, harness
bugs, verbose tool output, config/observability gaps).

## Goals

- A **per-run "Improve" button** in the run view, enabled once the run is terminal
  (`complete` **or** `halted` — a failed run is often the most worth improving), that
  launches the self-improvement analysis and navigates to the resulting run.
- The analysis determines, for the source run: what went well, what went wrong, bottlenecks,
  which tool calls failed and the likely cause, and **which steps/tasks were overly
  context-consuming**.
- It produces a **schema-valid improved workflow YAML** (workflow-related fixes, including
  context-bloat fixes) plus a **suggestions report** for non-workflow issues — both as
  ordinary run artifacts the user reviews and promotes by hand.
- The self-improver is **just another `WorkflowDef` YAML**, triggered through the **normal
  run-submission + file-upload path**, so it reuses the durable queue, streaming, artifacts,
  and run view with no bespoke execution machinery.
- The run data reaches the analysis agent as a **compact run-log file** that is always under
  the agent's read cap, yet **loses no unique message**.

## Non-goals (explicitly rejected)

- **Auto-triggering on every run completion.** The trigger is a user-clicked button. This
  sidesteps the self-recursion loop (a self-improve run finishing would trigger another) and
  the single-worker queue flooding (`queue.max_concurrent_runs: 1`) that an unattended
  auto-trigger would cause. A recursion guard still exists as defense-in-depth.
- **Auto-promotion / overwriting `workflows/`.** The improved YAML lands as an artifact and is
  copied in by hand. A workflow task is sandboxed and cannot write into `$ATOM_HOME/workflows/`
  anyway; one-click promotion (validated + backed-up) is a clean future addition, out of scope
  for v1.
- **Cross-run / historical trend analysis.** v1 analyzes the single run the button was clicked
  from. Learning across many runs of a workflow is a future extension.
- **Handing the agent the raw export.** A full `export.json` runs to tens of MB (every LLM
  call stores the *cumulative* message history), the sandboxed `read_file` refuses > 2 MB, and
  uploads cap at 25 MiB. The compact run-log is mandatory, not an optimization.
- **A new "reducer"/code step type in the workflow language.** Reduction happens once, in the
  trigger path, before the workflow runs.

## Background — the constraints that shape this

- **A workflow file is a `WorkflowDef`** (`workflow/schema.py`): `name`, `description?`,
  `inputs[]` (`text`|`file`), `notes`, `steps[]` → `tasks[]` → `{id?, prompt (Jinja), model?,
  thinking?}`. Every model is `extra="ignore"`, so **the LLM cannot invent new YAML
  capabilities** — only rearrange existing fields. "Improvement" = better decomposition,
  parallelization, model/thinking choices, and prompt wording.
- **Steps run sequentially; tasks within a step run concurrently** (`engine.execute`, bounded
  by `workflow.max_parallel`). **Cross-step data flows through the one shared workspace**, not
  template variables — `render_task_prompt` only exposes `inputs`, `workspace`, `uploads`,
  `outputs` (a *path*), `date`. So the analyzer's step-1 tasks write findings files into
  `{{ workspace }}` and the step-2 task reads them.
- **Run data lives in three places** (`run_store.py`): `RunManifest` (`run.json`) has per-task
  `status`/`error`/`started_at`/`ended_at`/`model` — no observability required;
  `chats/s<step>__<task>.json` has the lead task's transcript (`serialize_messages`); and
  `export.json` has the raw provider trace tree (`roots[]`) — the only source of token counts,
  per-call timings, and tool-call-level errors. Sub-agent transcripts exist **only** in the
  traces.
- **Sandbox + size walls:** file tools see only `/mnt/user-data/{workspace,uploads,outputs}`;
  `read_file` refuses > 2,000,000 bytes; uploads cap at 25 MiB. The trigger endpoint, by
  contrast, runs **unsandboxed** in the server process and can read `runs/<id>/` freely.
- **Active backend:** `observability.enabled: true`, provider resolves to **LangSmith**
  (`project: atom`). Design must still branch on `envelope["provider"]` and degrade when traces
  are missing (a failed run whose traces never fully ingested).

## Design

### Component 1 — Compact run-log builder (`src/atom/observability/run_log.py`, new)

Server-side, unsandboxed. Sits beside `export.py`/`langfuse_export.py` because it branches on
the same provider-shaped `roots[]`. Reduces a finished run into one small JSON.

```
build_run_log(home: str | None, run_id: str, *, cfg: AtomConfig | None = None) -> dict
run_log_bytes(run_log: dict) -> bytes        # json.dumps → utf-8, for staging as an input
```

The returned dict:

- **`run`** — `run_id`, `workflow`, `status`, `created_at`/`ended_at`, wall-clock, and the
  declared `inputs` (values truncated per the body cap below). From the manifest.
- **`steps[]` / `tasks[]`** — per task: `id`, `step_index`, `model`, `thinking`, `status`,
  `error`, `started_at`/`ended_at`, `duration_s`, and — when traces exist — rolled-up
  `tokens` (`prompt`/`completion`/`total`), `llm_calls`, `tool_calls`, `tool_failures`.
- **`calls[]`** — one entry per traced LLM/tool call: `step`/`task`/`agent` attribution,
  `type` (`llm`|`tool`), `name`, `duration_s`, `ttft_s?`, `tokens?`, and for tools
  `ok`/`error` (**error text kept**, capped). This is the raw material for the
  bottleneck / tool-failure / context-hotspot analysis.
- **`transcript[]`** — the **deduplicated** conversation, reconstructed from the **traces**
  when present (see below): every unique message **exactly once**, across the lead agent *and*
  every sub-agent, attributed by step/task/agent, with `role`, `text`, `thinking`,
  `tool_calls`, tool results. (`chats/` is the fallback only when traces are absent.)
- **`meta`** — `provider`, `export_present`, `export_complete`, `truncations[]` (which bodies
  were capped and their original sizes), and degradation flags.

**Transcript reconstruction — the core guarantee.** In a trace, call *k*'s input contains all
prior messages again (cumulative), which is where the O(n²) bulk comes from. We collapse that
by **unioning the message lists across an agent's LLM calls, deduplicated, order-preserving**,
so each unique message survives once. We union across *all* calls rather than trusting the last
call's history, because atom's own **compaction middleware** can summarize earlier turns away
mid-run — only the union recovers those messages. Sub-agent threads are grouped by trace
metadata (`task_id`, `is_subagent`, `agent_role`, `session_id`) and reconstructed the same way.

- **Invariant (unit-tested):** every distinct message that appears in *any* call's input
  appears **exactly once** in the reconstructed transcript. Dedup key = `(role, name,
  tool_call_id, content-hash)`.
- **Oversized body cap:** an individual message body over **32 KB** is trimmed to
  `first 32 KB + "[truncated N bytes — original M bytes]"`, and the event is recorded in
  `meta.truncations`. This never drops a message (the turn, role, tool calls, and metadata all
  survive) — it only trims one giant body, which is itself a context-bloat finding. It is the
  one thing that could otherwise push the run-log past the 2 MB read cap.

**Degradation.** If `export.json` is absent or `complete=false` (observability off, or a failed
run whose traces never ingested), the builder still emits `run`/`steps`/`tasks` from the
manifest and the transcript from `chats/` (lead only), sets `export_present=false` /
`export_complete=false`, and omits token/tool-call detail. The workflow still runs; the analysis
notes the missing data.

### Component 2 — Trigger endpoint (`POST /api/runs/{run_id}/self-improve` in `api/app.py`)

Added inside `create_app` so it closes over `engine`/`store`/`cfg`, reusing `_create_and_enqueue`.

1. `store.load(run_id)` → 404 if missing; require `status in ("complete","halted")` else **409**.
2. **Recursion guard:** if `manifest.workflow == SELF_IMPROVE_WORKFLOW` (`"self-improve"`),
   **400** ("cannot self-improve the self-improvement workflow").
3. Read the target YAML source from `workflows_dir(cfg.home)/<manifest.workflow>.yaml` → **404**
   if it no longer exists.
4. **Ensure the export exists:** if `store.export_path(run_id)` is absent, generate it via the
   same provider dispatch `export_traces` already uses (LangSmith/LangFuse/none). Best-effort —
   a failure or "no traces" does **not** block the trigger.
5. `run_log = build_run_log(cfg.home, run_id, cfg=cfg)`; `data = run_log_bytes(run_log)`.
6. `_create_and_enqueue(self_improve_wf, inputs={"workflow_name": manifest.workflow,
   "source_run_id": run_id, "run_status": manifest.status},
   files={"run_log": ("run-log.json", data), "target_workflow": (f"{name}.yaml", yaml_bytes)})`.
   Staging via `_create_and_enqueue` → `save_upload` bypasses the multipart size/extension
   checks (the run-log is generated, not user-uploaded, and is small by construction).
7. Return `{"run_id": <new>, "status": "queued"}` (202), so the UI can open the new run.

`self_improve_wf = load_workflow(SELF_IMPROVE_WORKFLOW, cfg.home)` → **503** with an install hint
if `self-improve.yaml` isn't in `$ATOM_HOME/workflows/`.

### Component 3 — The `self-improve` workflow (`workflows/self-improve.yaml`, new)

Shipped in the repo `workflows/` dir; installed to `$ATOM_HOME/workflows/` like the other
samples.

- **Inputs:** `run_log` (file, required), `target_workflow` (file, required), `workflow_name`
  (text), `source_run_id` (text), `run_status` (text).
- **Step 1 — "Analyze" (3 parallel tasks; each reads `{{ run_log }}` + `{{ target_workflow }}`
  and writes a findings file into `{{ workspace }}`):**
  - `failures_and_tools` → `analysis/failures.md`: what failed and why; each tool-call failure
    with its error text and a **workflow-related vs harness/tool-related** tag.
  - `bottlenecks_and_context` → `analysis/performance.md`: per-step/task duration and **token
    consumption**; ranked bottlenecks; **context hotspots** (which steps/messages were overly
    context-consuming) with a fixable-in-YAML verdict.
  - `structure_and_prompts` → `analysis/structure.md`: decomposition, parallelization
    opportunities, prompt clarity, model/thinking choices, and **what went well** (keep it).
- **Step 2 — "Improve" (1 task):** reads `analysis/*.md` + `{{ target_workflow }}` +
  `{{ run_log }}`, then writes and `present_files`:
  - `improved-{{ workflow_name }}.yaml` — a schema-valid rewrite applying the workflow-related
    fixes (incl. context-bloat fixes), **self-checked** against the schema rules (≥1 step, ≥1
    task/step, unique task ids, valid `model`/`thinking` values). Prompts note that novel keys
    are silently dropped (`extra="ignore"`), so it must stay within the existing language.
  - `suggestions.md` — non-workflow issues (unclear tool descriptions, harness bugs, verbose
    tool output, config/observability gaps), a what-went-well/wrong/bottleneck/context summary,
    and a **changelog** of every YAML change with its rationale.

### Component 4 — UI (`atom-ui/src/RunView.tsx` + `api.ts`)

- **"Improve" button** in the `.run-status` header beside "Export run" (`RunView.tsx:181`),
  `disabled` unless `status === "complete" || status === "halted"`; **hidden** when
  `manifest.workflow === "self-improve"`.
- `api.selfImprove(id): Promise<{ run_id: string; status: string }>` → `POST
  /api/runs/${id}/self-improve` (mirrors `exportRun`'s error-surfacing).
- On success, a banner (same pattern as the export banner) shows **"Self-improvement run
  started → View it"**; the link opens the new run through the existing run-open path (a new
  `onOpenRun?` prop on `RunView`, wired in `App.tsx`), so the user lands on the new run and
  watches it stream live.

## Data flow

```
[finished run] --click "Improve"--> POST /api/runs/{id}/self-improve
  └─ load manifest (terminal? not self-improve?)
  └─ read target workflows/<name>.yaml
  └─ ensure export.json (generate if missing; best-effort)
  └─ build_run_log(manifest + export.json + chats/) --> small JSON (dedup, capped)
  └─ _create_and_enqueue(self-improve, inputs, files={run_log, target_workflow})
        --> new run_id (queued)
[self-improve run] Step 1 (parallel) writes analysis/*.md into shared workspace
                   Step 2 reads them --> improved-<name>.yaml + suggestions.md (present_files)
                        --> captured as artifacts
[user] reviews artifacts, copies improved YAML into workflows/ by hand
```

## Error handling / degradation

| Situation | Behavior |
|---|---|
| Run not terminal | 409 (button is disabled, but the endpoint guards too). |
| Source workflow is `self-improve` | 400 recursion guard; button hidden in UI. |
| Target YAML deleted since the run | 404 with a clear message. |
| `self-improve.yaml` not installed | 503 with an install hint. |
| Observability off / no traces / incomplete export | Run-log degrades to manifest + `chats/`; `meta` flags it; workflow still runs. |
| Export generation fails | Proceed with whatever's on disk; never block the trigger. |
| One pathological giant message body | Body capped at 32 KB with a marker + recorded original size; message kept. |

## Testing

- **Run-log builder** (unit): the dedup invariant — every distinct message survives exactly
  once — including a **compaction** case (a message present in an early call but summarized out
  of later calls still appears once); tool-failure extraction with error text; token roll-ups;
  the 32 KB body cap + `meta.truncations`; graceful degradation when `export.json` is
  absent/`complete=false`. Cover **both** LangSmith (nested `child_runs`) and LangFuse (flat
  sibling `observations[]`) root shapes with fixtures.
- **Trigger endpoint** (TestClient): terminal-status gate (409), recursion guard (400),
  missing-target-YAML (404), missing-`self-improve.yaml` (503), and the happy path staging both
  file inputs and returning a new queued `run_id`.
- **Workflow YAML**: `WorkflowDef.model_validate` loads `self-improve.yaml`; a smoke test runs
  it against a fixture run-log and asserts both artifacts (`improved-*.yaml`, `suggestions.md`)
  are produced, and that the emitted YAML re-validates as a `WorkflowDef`.

## Open questions

None blocking. Deferred by decision: one-click promotion of the improved YAML into
`workflows/` (validated + backed-up); cross-run/historical analysis; making the 32 KB body cap
configurable.
