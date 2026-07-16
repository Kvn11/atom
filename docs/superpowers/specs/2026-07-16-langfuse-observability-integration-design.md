# LangFuse Observability Integration — Design

**Date:** 2026-07-16
**Status:** Approved design, ready for implementation planning
**Predecessor:** `2026-07-06-atom-langsmith-observability-design.md` (the LangSmith integration this generalizes)

## Summary

atom currently supports exactly one observability backend, LangSmith, activated implicitly through `LANGSMITH_*` environment variables. This design adds LangFuse as a second, mutually-exclusive backend selected by config, and introduces a small **provider-strategy abstraction** so backend-specific behavior lives in one place instead of scattered `if` branches across the runtime, sub-agent runner, engine, CLI, and API.

The centerpiece for LangFuse is **sessions**: a LangFuse "session" groups related traces in the UI, and we map one session to one **whole workflow run** (`langfuse_session_id = run_id`), so every step, task, and sub-agent of a run appears grouped under a single session.

### Decisions (settled during brainstorming)

1. **Provider model — either/or.** A single `provider: langsmith | langfuse | none` discriminator. Exactly one backend is active per run. Not simultaneous dual-export.
2. **Session scope — the whole run.** LangFuse `session_id = run_id`. All steps/tasks/sub-agents of one workflow execution group under one session.
3. **Coverage — workflow runs only.** Match LangSmith's current scope. The interactive CLI (`atom run` / `atom chat` → `run_agent` with `trace=None`) stays untraced for every provider.
4. **Export — full parity.** LangFuse gets a pull-side exporter mirroring `export.py`, wired into the `workflow export` CLI command and the `/api/.../export` endpoint, dispatched by the configured provider.

## Background: how observability works today

- **Activation is env-driven and implicit.** LangSmith's `LangChainTracer` auto-attaches when `LANGSMITH_TRACING`/`LANGSMITH_API_KEY` are set. There is **no** explicit tracer object, no `callbacks=` key in any run config, and `langsmith` is imported lazily only inside `export.py`. The harness's only push-side job is `apply_observability_env(cfg)` (maps config → env, called once in `WorkflowEngine.__init__`) plus merging `{run_name, tags, metadata}` dicts into the LangGraph run config.
- **Trace metadata is built in three provider-agnostic layers** (`observability/trace.py`): `build_lead_trace` (identity), `enrich_lead_trace` (runtime: model/thinking/prompt fingerprints/git sha), `build_subagent_trace` (sub-agent). They produce plain dicts and are reusable as-is.
- **Run/execution model.** run → steps → tasks. Each task = one `run_agent` lead call with `thread_id = f"{run_id}:s{step_index}:{task_id}"`. Sub-agents are spawned by `SubagentRunner` via separate `agent.ainvoke(...)` calls with their own child `thread_id`.
- **Two distinct "session_id" meanings exist — do not conflate them:**
  - atom's existing **metadata `session_id`** = the task's `thread_id` (and, for sub-agents, overridden back to the *parent* thread so they group into the lead's thread). This is LangSmith's thread-grouping key.
  - LangFuse's **`langfuse_session_id`** (new) = `run_id` (the whole run). This is a *different* key with *different* semantics and is the LangFuse session grouping key. The existing metadata `session_id` is left untouched.
- **Flush.** `engine.execute()`'s `finally` calls `wait_for_all_tracers()` when `tracing_active()`, and a flush exception must not mask the real run error (there is a test for this).

## LangFuse SDK facts (v3) grounding this design

Verified against `/langfuse/langfuse-python` (Context7, 2026-07-16):

- **Push-side:** `from langfuse.langchain import CallbackHandler`. The handler binds to a process-global `Langfuse` client by public key. It is passed per-run via `config["callbacks"]`. Unlike LangSmith, it does **not** auto-attach — omitting it means no trace.
- **Sessions via metadata:** the handler reads `metadata["langfuse_session_id"]` at each chain root (`parent_run_id is None`) and propagates it to all child spans. So setting one metadata key per root run is all that's needed to group traces into a session. (It similarly reads `langfuse_user_id` and tags; we use only session.)
- **Client construction:** `Langfuse(public_key=, secret_key=, host=, environment=, release=, sample_rate=, tracing_enabled=)`, all also settable via `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` env vars. Default host `https://cloud.langfuse.com`.
- **Flush:** `client.flush()`.
- **Pull-side:** `Langfuse(...).api.trace.list(session_id=...)` returns trace summaries; `api.trace.get(trace_id, fields="core,io,observations")` hydrates a trace's observation tree. (Exact pagination signature to be confirmed at implementation time against the installed version.)

## Architecture: provider-strategy abstraction

A new module `observability/provider.py` defines one small protocol and three implementations, built once by a factory. Every backend difference (activation, per-run decoration, flush) collapses to a method call behind this interface.

```python
class ObservabilityProvider(Protocol):
    name: str                                   # "langsmith" | "langfuse" | "none"
    def is_active(self) -> bool: ...            # replaces the LangSmith-specific tracing_active() gate
    def decorate_run_config(self, config: dict) -> dict: ...  # per-run: add callbacks + session key
    def flush(self) -> None: ...                # end-of-run flush

def build_provider(cfg: AtomConfig) -> ObservabilityProvider:
    """Resolve cfg.observability into an active provider (or NullProvider). Logs an ObservabilityStatus.
    Never raises on misconfiguration — telemetry must never break a run."""
```

- **`NullProvider`** — `is_active() -> False`, `decorate_run_config` returns config unchanged, `flush` is a no-op.
- **`LangSmithProvider`** — `__init__` calls the existing `apply_observability_env(cfg)` (kept in `trace.py`, re-used verbatim); `is_active()` reflects that status; `decorate_run_config` is a **no-op** (env-driven auto-attach, nothing to thread in); `flush` calls `wait_for_all_tracers()`.
- **`LangFuseProvider`** — lazy-imports `langfuse` (like `export.py` lazy-imports `langsmith`, so LangSmith-only installs don't need the dep). Constructs the global `Langfuse(...)` client once from `cfg.observability.langfuse` and holds a **single shared** `CallbackHandler`. The handler is stateless — per-run session/tags come from run-config metadata — so one instance safely serves all concurrent tasks. `decorate_run_config`:
  1. appends the handler to `config["callbacks"]` (creating the list if absent), and
  2. reads `run_id` from the config metadata and, when present, sets `config.setdefault("metadata", {})["langfuse_session_id"] = run_id` — using a defensive `metadata.get("run_id")` and skipping the session stamp if absent (never `KeyError`), since the trace layer always provides `run_id` for workflow runs.
  `flush` calls `client.flush()`.

### Factory resolution (backward compatible)

`cfg.observability.provider` resolution:
- `"langfuse"` → `LangFuseProvider` (or `NullProvider` + warning if keys missing).
- `"langsmith"` → `LangSmithProvider` (existing env behavior).
- `"none"` → `NullProvider`.
- **unset (`None`, the default)** → **legacy fallback**: if `enabled` is true and `LANGSMITH_API_KEY` is present → `LangSmithProvider`, else `NullProvider`. This preserves every existing config and test unchanged.

## Config schema changes (`config/schema.py`)

Add a `provider` discriminator and a nested LangFuse block to `ObservabilityConfig`. All existing fields are retained; `include_prompt_fingerprint` and `capture_git_sha` are provider-agnostic (they shape the trace metadata dicts, which both backends consume).

```python
class LangfuseConfig(_Base):
    host: Optional[str] = None            # default https://cloud.langfuse.com (SDK default)
    public_key: Optional[str] = None      # or LANGFUSE_PUBLIC_KEY env
    secret_key: Optional[str] = None      # or LANGFUSE_SECRET_KEY env
    environment: Optional[str] = None      # optional LangFuse "environment" tag
    release: Optional[str] = None          # optional; if None, fall back to captured git sha
    sample_rate: float = 1.0               # 0.0..1.0

class ObservabilityConfig(_Base):
    provider: Optional[Literal["langsmith", "langfuse", "none"]] = None  # None -> legacy fallback
    enabled: bool = False                  # (existing) legacy LangSmith toggle
    project: Optional[str] = None          # (existing) LangSmith project
    default_tags: list[str] = Field(default_factory=list)     # (existing, shared)
    include_prompt_fingerprint: bool = True                    # (existing, shared)
    capture_git_sha: bool = True                               # (existing, shared)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)  # (new)
```

`config.yaml` example:

```yaml
observability:
  provider: langfuse
  include_prompt_fingerprint: true
  capture_git_sha: true
  default_tags: [prod]
  langfuse:
    host: ${LANGFUSE_HOST}
    public_key: ${LANGFUSE_PUBLIC_KEY}
    secret_key: ${LANGFUSE_SECRET_KEY}
    environment: ${LANGFUSE_ENV}
```

`${ENV}` expansion already works via the loader; `_Base` uses `extra="ignore"`, so adding keys is non-breaking.

## Push-side wiring & data flow

The provider is built once and threaded down; two run-config construction sites call `decorate_run_config`.

1. **`WorkflowEngine.__init__`** builds `self.provider = build_provider(cfg)` (replacing the bare `apply_observability_env` call), logs its status, and passes it into `_run_task`.
2. **Lead — `runtime.build_run_config`** gains a `provider` parameter and calls `provider.decorate_run_config(config)` after `_apply_trace`. `run_agent` accepts and forwards `provider` (default `None` → treated as `NullProvider`, so the untraced CLI path is unaffected).
3. **Sub-agent — `SubagentRunner`** stores the provider (alongside `base_trace`) and calls `provider.decorate_run_config(config)` in `run()` after merging the sub-agent trace. Because LangChain propagates `callbacks` down to child runnables, attaching the handler at these two roots also captures the middleware model calls (`TitleMiddleware`, retry wrappers) — no extra hook needed.
4. **`enrich_lead_trace` gate** in `agent.build_lead_agent` changes from `if tracing_active():` to `if provider.is_active():`, so LangFuse runs receive the same enriched metadata (model/thinking/prompt fingerprints/git sha) that LangSmith runs get today. This requires passing the provider (or an `is_active` bool) into `build_lead_agent`.

### Session stamping — why every root needs it

Each atom task is a separate `.astream`/`.ainvoke` call → a separate LangChain chain root → a separate LangFuse trace. Each sub-agent is likewise a separate `.ainvoke` → a separate root → a separate trace. LangFuse joins traces into a session **only** via `langfuse_session_id` on each root. Therefore `decorate_run_config` stamps `langfuse_session_id = run_id` on **both** lead and sub-agent configs (reading `metadata["run_id"]`, which the trace layer already provides on both — sub-agents inherit `run_id` via `build_subagent_trace`'s `dict(base_md)`). Result: the whole run collapses into one LangFuse session, exactly as the approved mock showed:

```
LangFuse Session: run_a1b2c3
  ├─ trace: step0/task_plan (lead)   ├─ trace: step0/task_plan/sub:... (subagent)
  ├─ trace: step1/task_build (lead)  └─ trace: step1/task_test (lead)
```

## Flush

`engine.execute()`'s `finally` replaces `if tracing_active(): wait_for_all_tracers()` with `self.provider.flush()`. The existing guarantee is preserved: a `flush()` exception is caught/logged and must not mask the real run error (keep the existing test's contract).

## Pull-side / export parity

Goal: `workflow export` (CLI) and the `/api/.../export` endpoint produce a self-describing export regardless of backend, dispatched by `cfg.observability.provider`.

### What is shared vs provider-native

- **Shared (unchanged):** the manifest-driven scaffolding in `export.py` — `ExportResult`, `resolve_run_ids`, the envelope wrapper (`run_id`, `workflow`, `scope`, `complete`, `expected_roots`, `fetched_roots`, embedded `atom_manifest`), atomic temp-file writes, and the async-ingestion polling loop. The `expected_root_count` oracle (executed-task count from the local manifest) is also shared. The envelope grows a `provider` field and the `langsmith_sdk` field generalizes to record whichever SDK produced the export.
- **Provider-native `roots` payload:** the LangSmith exporter keeps writing **verbatim LangSmith `Run` dicts** (unchanged, so existing eval consumers and `test_export.py` are undisturbed). The new LangFuse exporter (`observability/langfuse_export.py`) writes LangFuse trace + observation dicts.

### The critical trace-tree asymmetry

- **LangSmith:** sub-agents are **nested child runs under the task's root**. `load_child_runs=True` hydrates the whole lead+sub-agent tree per root. `expected_roots = #executed tasks`; one root per task.
- **LangFuse:** sub-agents are **sibling traces within the run session** (each `.ainvoke` is its own root/trace, joined by session). So `api.trace.list(session_id=run_id)` returns *tasks and sub-agents as siblings*. The completeness oracle therefore counts only **lead traces** (`metadata.is_subagent == False` / `agent_role == "lead"`) toward `expected_root_count`; sub-agent traces are still included in `roots` but are not part of the completeness count. Each fetched trace is hydrated via `api.trace.get(trace_id, fields="core,io,observations")`.

### Granularities (mirroring `export.py`)

- **Run export:** LangFuse `api.trace.list(session_id=run_id)`; partition fetched traces into lead vs sub-agent by metadata; poll until `#lead traces >= expected` (executed tasks) or timeout; write `runs/<run_id>/export.json`.
- **Task export:** fetch the run's session traces and locally select the task's lead trace + its sub-agent traces by metadata `task_id` (leads carry `task_id`; sub-agents inherit it via `build_subagent_trace`). Task must be terminal. Write `runs/<run_id>/exports/s<step>__<task>.json`.

### Dispatch

`cli.py` (`workflow export`, `_export_one_task`) and `api/app.py` (`export_traces`) select the exporter by `cfg.observability.provider`, and require that provider's credentials (LangFuse: public+secret key; mirroring today's `LANGSMITH_API_KEY` `RuntimeError` guard). A thin dispatch layer (e.g. `export_run(...)` / `export_task(...)` façade that routes to the LangSmith or LangFuse implementation) keeps the call sites provider-agnostic.

## Error handling & graceful degradation

- Misconfiguration (provider selected but keys missing, host unreachable at init, import failure) → log a warning and fall back to `NullProvider`; the run proceeds untraced. Telemetry must never crash a run. This mirrors LangSmith's existing `enabled-but-no-api-key` status; reuse/extend the `ObservabilityStatus` dataclass with a LangFuse-aware `reason`.
- `flush()` failures are caught and logged, never re-raised into the run's error path.
- The `langfuse` import is lazy and confined to `LangFuseProvider` and `langfuse_export.py`, so a LangSmith-only or `none` install never imports it.

## Dependencies

- Add `langfuse>=3,<4` to the "Observability" section of `pyproject.toml`, `requirements.txt`, and `requirements.lock.txt` (pin the resolved version in the lock).
- `.env.example` documents `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (and optional `LANGFUSE_ENV`).

## Testing strategy

Mock the SDK — no network — mirroring `test_export.py`'s mocking of `langsmith.Client`.

- **Factory resolution** (`test_observability_provider.py`, new): each `provider` value → correct class; `langfuse` with missing keys → `NullProvider` + warning; unset `provider` + legacy `enabled` + key → `LangSmithProvider`; `none` → `NullProvider`.
- **`LangFuseProvider.decorate_run_config`:** appends the handler to `callbacks` (and preserves any pre-existing callbacks); stamps `langfuse_session_id` from `metadata["run_id"]`; verify a **sub-agent** config is stamped with `run_id` (whole run), *not* the parent thread — the key session-grouping assertion.
- **Enrich gate:** `enrich_lead_trace` runs under a LangFuse-active provider (extend `test_observability.py` / `test_runtime_trace.py`).
- **Flush dispatch:** each provider's `flush` calls the right underlying call; a raising `flush` does not mask a run error (extend the existing engine test).
- **LangSmith non-regression:** existing `test_observability*.py`, `test_runtime_trace.py`, `test_export.py`, `test_cli_export.py` pass unchanged (legacy fallback path).
- **LangFuse export** (`test_langfuse_export.py`, new): mock `api.trace.list`/`api.trace.get`; assert the lead/sub-agent partition, the completeness oracle on lead traces, the envelope schema (incl. `provider`), and per-task selection by metadata.
- **CLI/API dispatch:** `workflow export` and the export endpoint route to the LangFuse exporter when configured, and raise the credential guard when keys are absent.

## Files touched

| File | Change |
|------|--------|
| `config/schema.py` | Add `provider` + `LangfuseConfig`; nest `langfuse` in `ObservabilityConfig` |
| `observability/provider.py` | **New** — protocol, `NullProvider`, `LangSmithProvider`, `LangFuseProvider`, `build_provider` |
| `observability/langfuse_export.py` | **New** — LangFuse pull-side exporter (native roots, shared envelope/oracle) |
| `observability/trace.py` | Keep builders; `apply_observability_env` now consumed by `LangSmithProvider`; retain `tracing_active` for the legacy fallback |
| `observability/export.py` | Extract shared envelope/oracle; add a provider-dispatch façade; LangSmith roots unchanged |
| `observability/__init__.py` | Re-export the new provider names |
| `runtime.py` | `build_run_config` + `run_agent` accept/forward `provider`; call `decorate_run_config` |
| `subagent.py` | `SubagentRunner` stores `provider`; `run()` calls `decorate_run_config` |
| `agent.py` | Enrich gate `tracing_active()` → `provider.is_active()` |
| `workflow/engine.py` | Build provider once; thread it; `finally` flush via `provider.flush()` |
| `cli.py`, `api/app.py` | Export dispatch by provider + LangFuse credential guard |
| `pyproject.toml`, `requirements.txt`, `requirements.lock.txt`, `.env.example`, `config.yaml` | Dep + docs + example config |

## Out of scope (YAGNI)

- Simultaneous dual-export to both backends (the either/or decision).
- Tracing the interactive CLI (`atom run`/`atom chat`) path.
- LangFuse-specific features beyond tracing + sessions (prompt management, evals, datasets, scores).
- Per-task/per-user LangFuse `user_id`; only session grouping is in scope.

## Risks / to confirm at implementation time

- **LangFuse `api.trace.list` filter + pagination signature** for the installed `langfuse>=3` — confirm `session_id` filtering and metadata availability on list results; if metadata isn't returned by `list`, hydrate via `get` before partitioning lead vs sub-agent.
- **Ingestion lag:** LangFuse batches/flushes asynchronously; the export poll loop (already present) absorbs this, but LangFuse's flush cadence differs from LangSmith's — verify the default poll window is adequate, and ensure the run-end `flush()` runs before an immediately-following export.
- **`callbacks` propagation vs. streaming filter:** the existing `atom_subagent` metadata marker (used by the SSE stream filter) is untouched; confirm adding `callbacks` to sub-agent configs doesn't interact with `stream_mode` message filtering.
