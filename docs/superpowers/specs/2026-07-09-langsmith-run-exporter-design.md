# atom — LangSmith run exporter (offline-eval on-ramp)

- **Date:** 2026-07-09
- **Status:** Approved (design) → pending implementation plan
- **Scope:** A first-class `atom workflow export` command that downloads a completed workflow run's
  LangSmith traces to disk as a raw-tree JSON artifact, plus two write-side fixes that make the export
  reliable and complete. The offline **evaluator** that consumes the artifact is a separate, later project.
- **Related:** `docs/superpowers/specs/2026-07-06-atom-langsmith-observability-design.md` (the push side),
  `docs/superpowers/plans/2026-07-06-langsmith-spike-checklist.md` (thread-grouping verified live).

## 1. Background & motivation

atom already **pushes** rich, eval-ready traces to LangSmith for every workflow run: each task is one
lead-agent run under its own thread, sub-agents nest under their lead and share the lead's `session_id`,
and every run (lead + sub-agent) carries a shared `run_id` metadata field and a `run:{run_id}` tag
(`src/atom/observability.py`). Reasoning is captured too — extended thinking is on by default
(`thinking: low`), and no middleware strips thinking blocks, so LangSmith stores them verbatim in the
raw model I/O.

What is missing is the **pull** side. There is no committed tooling anywhere in `src/` to download those
traces (`langsmith` is a push-only dependency; the only script that ever read runs back lived in ephemeral
scratchpad and is gone). The upcoming prompt-quality **evaluation phase** needs runs on disk in a
self-contained form so they can be scored offline, with sub-agent content and reasoning visible.

A review (2026-07-09, adversarially verified) surfaced the gaps this design closes:
1. **No download/export tooling** (blocker for offline eval).
2. **A run spans many LangSmith threads** — one per task, keyed `{run_id}:s{step}:{task}` — so a run
   cannot be fetched by `session_id`; it must be fetched by the run-wide `run_id`.
3. **No tracer flush at run exit** — the final trace batch can be dropped before upload, silently
   truncating a later export.
4. **Silent no-op activation** — if `observability.enabled` is true but `LANGSMITH_API_KEY` is missing,
   the run executes and nothing uploads, with no warning; a user believes a run is downloadable when
   nothing was ever sent.
5. **atom's pass/fail verdict is only in `run.json`**, not on the trace, so an eval pipeline must
   reconcile the two.

## 2. Goals / non-goals

**Goals**
- `atom workflow export <run_id>` downloads a run's complete LangSmith trace tree (lead tasks +
  nested sub-agent + LLM runs, with **thinking blocks intact**) to
  `$ATOM_HOME/workflows/runs/<run_id>/export.json`.
- The exporter fetches by the run-wide `run_id` (not `session_id`) so it captures **all** of a run's
  task-threads, and uses the local run manifest as a **completeness oracle** so async-ingestion lag
  cannot silently truncate the export.
- `--latest <workflow>` and `--all <workflow>` resolve run ids from the local `RunStore`.
- Output is a **raw** LangSmith run tree in a thin, self-describing envelope (no normalized/opinionated
  agent schema to maintain); the envelope embeds atom's own manifest (verdict) for scoring.
- Two write-side fixes ship with it: a **tracer flush** at run exit and **activation logging**.
- Zero behavior change when observability is disabled; the exporter is only meaningful for runs that were
  traced.

**Non-goals**
- The offline **evaluator / scorer** itself (separate later project) — this is only the export on-ramp.
- A normalized atom-native agent schema (explicitly rejected in favor of the raw tree).
- Exporting the interactive CLI/REPL path (those runs are untraced by design — `base_trace=None`).
- Bulk/multi-run streaming export via LangSmith's data-export job API (overkill; per-run pull is enough).
- Re-uploading, mutating, or deleting LangSmith runs. Read-only.

## 3. Confirmed LangSmith SDK facts driving the design

Verified against current LangSmith docs (docs.langchain.com/langsmith, 2026-07-09) via `ctx7`:
- **Root runs:** `client.list_runs(project_name=..., is_root=True)` returns only runs with no parent.
  `list_runs` returns an **auto-paginating generator** — iterating it does not silently drop runs;
  the knobs to mind are async-ingestion latency and an explicit `limit`.
- **Metadata filter:** runs are filterable by metadata via the run-filter DSL, e.g.
  `filter='and(eq(metadata_key, "run_id"), eq(metadata_value, "<run_id>"))'`. Combined with
  `is_root=True` this yields exactly a run's task-root runs. (Equivalent: filter on the `run:{run_id}`
  tag. Exact grammar to be pinned in the plan.)
- **Child hydration:** `client.read_run(run_id, load_child_runs=True)` returns the run with its nested
  `child_runs` populated recursively; walk it via `run.child_runs`. This is how sub-agent and per-LLM-call
  runs (carrying raw request/response, including Anthropic thinking blocks) come back.
- **Serialization:** a LangSmith `Run` is a pydantic model; serialize with its dict/JSON dump (JSON mode
  to render datetimes/UUIDs) to persist the tree.

**Design consequence:** sub-agents are *children* of their lead root (atom runs them on one async event
loop, so they nest), not additional roots. Therefore **`#root runs == #executed tasks`**, and hydrating
each root with `load_child_runs=True` captures the whole run. The completeness check counts roots against
executed tasks from the local manifest.

## 4. Architecture

Convert the single module `src/atom/observability.py` into a package so the push and pull sides sit
together with clear names, **without changing any existing import** (the package `__init__` re-exports
everything currently importable from `atom.observability`):

| Path | Responsibility |
|------|----------------|
| `src/atom/observability/__init__.py` | Re-export the existing public names (`build_lead_trace`, `enrich_lead_trace`, `build_subagent_trace`, `apply_observability_env`, `tracing_active`, `prompt_fingerprint`, `git_sha`, `_apply_trace`, `ObservabilityStatus`) so `from atom.observability import ...` keeps working unchanged. |
| `src/atom/observability/trace.py` | The current push-side code, moved verbatim, plus the new `ObservabilityStatus` return type for `apply_observability_env`. |
| `src/atom/observability/export.py` | **New.** The pull-side exporter (below). |

The exporter is pure/injectable: `export_run(...)` takes a `client` parameter that defaults to a real
`langsmith.Client()`, mirroring how `src/atom/notes.py` injects its CLI runner. Tests pass a fake client;
no network, no key.

### 4.1 Exporter interface (`src/atom/observability/export.py`)

```python
from dataclasses import dataclass

@dataclass
class ExportResult:
    run_id: str
    path: str            # where export.json was written
    complete: bool       # fetched_roots >= expected_roots
    expected_roots: int
    fetched_roots: int

def export_run(
    home: str | None,
    run_id: str,
    *,
    project: str | None = None,      # default: cfg.observability.project (caller resolves)
    client: "Any | None" = None,     # default: langsmith.Client(); injectable for tests
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: "Callable[[], str] | None" = None,   # injectable clock for deterministic tests
) -> ExportResult: ...
```

Supporting helpers (unit-tested independently):
- `expected_root_count(manifest) -> int` — number of tasks with status in `{running, succeeded, failed}`
  (pending/never-ran tasks emit no trace, e.g. after a halt).
- `fetch_run_tree(client, project, run_id) -> list[dict]` — `list_runs(...)` roots →
  `read_run(id, load_child_runs=True)` each → serialize to plain dicts.
- `build_envelope(run_id, workflow, project, manifest, roots, *, complete, expected, fetched, now) -> dict`.

## 5. CLI surface

A third subcommand on the existing Typer `workflow` app (next to `run`/`runs` in `src/atom/cli.py`):

```
atom workflow export <run_id>                 # one run by id
atom workflow export --latest <workflow>      # newest run of a workflow (RunStore.list(), newest-first)
atom workflow export --all <workflow>         # every run of that workflow
atom workflow export <run_id> --project NAME  # override project (default: config observability.project)
atom workflow export <run_id> --config PATH   # standard config override, as other subcommands
```

- Exactly one selector is required: a positional `run_id`, or `--latest NAME`, or `--all NAME`
  (mutually exclusive; error if zero or more-than-one given).
- `--latest`/`--all` filter `RunStore.list()` (already sorted newest-first) by `manifest.workflow`.
- The command resolves `project` from `cfg.observability.project` when `--project` is absent, then calls
  `export_run(...)` once per resolved run id, printing the written path and completeness per run.

## 6. Output format

`$ATOM_HOME/workflows/runs/<run_id>/export.json` — a thin envelope around the **verbatim** LangSmith tree:

```json
{
  "run_id": "7f3a9c2b1e04",
  "workflow": "notes-smoke",
  "project": "atom",
  "exported_at": "2026-07-09T14:03:22",
  "langsmith_sdk": "0.9.7",
  "complete": true,
  "expected_roots": 2,
  "fetched_roots": 2,
  "atom_manifest": { "…verbatim run.json…": "inputs, per-step/per-task status + error/verdict" },
  "roots": [
    { "id": "…", "name": "notes-smoke/Recall/recall", "run_type": "chain",
      "inputs": {"…"}, "outputs": {"…"}, "tags": ["…"], "extra": {"metadata": {"…"}},
      "child_runs": [ { "run_type": "llm", "outputs": {"…thinking blocks…"}, "child_runs": [] } ] }
  ]
}
```

- `roots` holds the raw serialized `Run` dicts with nested `child_runs` — nothing normalized, nothing
  dropped; sub-agent runs and per-LLM-call runs (with thinking) are nested inside.
- `complete` / `expected_roots` / `fetched_roots` let the eval pipeline detect a truncated export.
- `atom_manifest` embeds the local `run.json` (inputs + per-task verdict) so the artifact is
  self-contained for scoring — this is the only atom-side data added; it does not touch the raw tree.

## 7. Data flow (`export_run`)

1. **Load manifest.** `RunStore(home).load(run_id)`; `FileNotFoundError` → a clear "run not found locally"
   error. Compute `expected_roots = expected_root_count(manifest)`.
2. **Require key.** If `LANGSMITH_API_KEY` is unset → raise a clear error (caller exits 1). Construct the
   client if not injected.
3. **Fetch + poll.** Loop until `fetched_roots >= expected_roots` or `poll_timeout` elapses:
   `roots = list_runs(project_name=project, is_root=True, filter=<run_id metadata>)`; for each,
   `read_run(id, load_child_runs=True)`; serialize. Sleep `poll_interval` between attempts. (Absorbs
   async-ingestion lag; the auto-paginating generator means no page is missed within an attempt.)
4. **Envelope + write.** Build the envelope; write atomically (`export.json.tmp` → `os.replace`) into the
   run dir. Return `ExportResult`.

Edge outcomes:
- `expected_roots == 0` (run never traced / observability was off) and `fetched_roots == 0` → treat as
  "nothing to export"; caller exits 1 with "no traces found — was observability enabled when this run
  executed?".
- Timeout with `0 < fetched_roots < expected_roots` → write the partial tree, set `complete: false`,
  return; caller prints a warning and exits 0.

## 8. Write-side fixes (bundled prerequisites)

**8.1 Tracer flush.** At the end of `WorkflowEngine.execute()` (`src/atom/workflow/engine.py`), in a
`finally`, call `wait_for_all_tracers()` (from `langchain_core.tracers.langchain`) **gated on
`tracing_active()`**. This one chokepoint covers the CLI and the API/service caller, guaranteeing the
final batched runs upload before the process can exit. No-op (and no import cost paid at call time) when
tracing is off.

**8.2 Activation logging.** `apply_observability_env(cfg)` returns an `ObservabilityStatus`:

```python
@dataclass
class ObservabilityStatus:
    active: bool            # tracing is (now) on
    project: str | None     # effective LANGSMITH_PROJECT, when active
    reason: str             # "active" | "disabled" | "enabled-but-no-api-key" | "env-preset"
```

`WorkflowEngine.__init__` (which already calls `apply_observability_env` once) logs one line from the
result via the standard `logging` module:
- active → `observability: tracing active → project '<project>'`
- `enabled-but-no-api-key` → `observability: observability.enabled but LANGSMITH_API_KEY missing — traces will NOT be uploaded`
- disabled → no log (default, silent).

The env-mutation behavior is unchanged (never overwrites an already-set var; only enables with a key
present; idempotent). Only the return value + the one log line are added.

## 9. Error handling

| Condition | Behavior |
|-----------|----------|
| `LANGSMITH_API_KEY` unset | `export_run` raises; CLI prints "set LANGSMITH_API_KEY to export" and exits 1. |
| `run_id` not in local store | CLI prints "run '<id>' not found" and exits 1. |
| `--latest`/`--all` matches no local run | CLI prints "no runs found for workflow '<name>'" and exits 1. |
| Zero roots after timeout | Exit 1, "no traces found — was observability enabled when this run executed?". |
| Partial (`fetched < expected`) at timeout | Write partial, `complete: false`, warn, exit 0. |
| LangSmith API/network error | Surface the error message, exit 1. Read-only, so nothing to roll back. |
| More than one / zero selectors | Exit 1 with usage message. |

## 10. Testing

All offline (fake `langsmith` client, injected clock; no key, no network):
- **Exporter happy path:** envelope shape + fields; roots hydrated with nested `child_runs`; output written
  to `runs/<run_id>/export.json`; `complete: true`.
- **Async-lag poll:** fake `list_runs` returns 1 root on the first call then 2 → exporter polls and
  succeeds with `fetched_roots == expected_roots`.
- **Timeout / partial:** fake never reaches `expected_roots` → `complete: false`, partial tree written.
- **Expected-roots oracle:** a manifest with a `pending` task is excluded from `expected_roots`.
- **Zero-trace run:** `expected==0`, `fetched==0` → the "nothing to export" signal.
- **Manifest embed:** `atom_manifest` in the envelope equals the on-disk `run.json`.
- **Selection:** `--latest`/`--all` resolve the right run ids from a fake `RunStore` (filter by workflow,
  newest-first for `--latest`).
- **Flush:** `WorkflowEngine.execute()` calls `wait_for_all_tracers` iff `tracing_active()` (monkeypatched
  both ways).
- **Activation status:** `apply_observability_env` returns `active` (key + enabled), `enabled-but-no-api-key`
  (enabled, no key — env unchanged, nothing enabled), `env-preset`/`disabled` as appropriate; still never
  overwrites env; idempotent on repeat calls.
- **CLI:** Typer `CliRunner` with `export_run` monkeypatched — arg wiring, mutually-exclusive selectors,
  exit codes for the error table.
- **Package move:** existing `from atom.observability import build_lead_trace` (and the other names) still
  import after the package conversion (a guard test).

## 11. Migration / rollout

- Move `src/atom/observability.py` → `src/atom/observability/trace.py`; add `__init__.py` re-exporting the
  public names; add `export.py`. Update nothing else (imports unchanged via re-export). Existing
  observability tests keep passing untouched.
- `langsmith` is already a dependency (`pyproject.toml`); no new dependency.
- Backward compatible and off by default: with observability disabled, the flush is a no-op, activation
  logging is silent, and `export` simply reports "no traces found" for untraced runs.
- Document the command in `README.md` (Workflows section) and note that export requires the run to have
  been executed with observability enabled.

## 12. Risks & open questions

- **Metadata filter grammar drift.** The exact `list_runs(filter=...)` DSL string is pinned during
  implementation against the installed `langsmith` version; a fallback is filtering on the `run:{run_id}`
  tag, or listing roots by project + `run_id` metadata client-side. Low risk — both are stable public API.
- **`Run` serialization shape.** The serialized dict shape follows the SDK's model; the envelope treats it
  as opaque, so SDK field additions are naturally preserved. Datetime/UUID rendering uses JSON dump mode.
- **Completeness heuristic.** `#roots == #executed tasks` assumes one lead root per task and sub-agents as
  children (true today — single event loop). If a future change made a task emit multiple roots, the
  oracle would need updating; the `complete` flag would read as "over-complete" (`fetched > expected`),
  which the plan should treat as complete, not an error.
- **Flush latency in a server.** `wait_for_all_tracers()` blocks briefly at run end; acceptable because it
  is gated on active tracing and bounded to the pending queue. If it ever matters for the service path, it
  can be moved to a thread executor — out of scope now.
