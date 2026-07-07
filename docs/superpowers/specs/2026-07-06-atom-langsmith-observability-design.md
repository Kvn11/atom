# atom — LangSmith observability for the workflow service

- **Date:** 2026-07-06
- **Status:** Approved (design) → pending implementation plan
- **Scope:** Workflow runs only (the interactive CLI/`run_agent` path is intentionally left untraced)
- **Related:** `docs/superpowers/specs/2026-07-03-atom-workflows-design.md`,
  `docs/superpowers/specs/2026-07-05-atom-compaction-pin-and-prompts-design.md`

## 1. Background & motivation

atom's workflow service (`src/atom/workflow/engine.py`) runs multi-step workflows where each step's
tasks execute concurrently, and **every task is one lead-agent `run_agent` call** under its own thread
id (`{run_id}:s{step}:{task_id}`). A lead agent may spawn sub-agents via `delegate_task`
(`src/atom/subagent.py::SubagentRunner`).

We want first-class **LangSmith observability** over these runs so we can debug long-running workflows
and, in the upcoming evaluation phase, **measure prompt efficacy** by correlating outcomes with the
exact prompt version that produced them.

**A partial scaffold already exists** and is reused, not replaced:
- `src/atom/workflow/observability.py::build_trace` builds `{run_name, tags, metadata}` for a task.
- `src/atom/runtime.py::_apply_trace` + `build_run_config` merge that trace into the LangGraph run config.
- `src/atom/workflow/engine.py::_run_task` calls `build_trace(...)` and passes `trace=trace` into `run_agent`.
- `langsmith==0.9.7` is already installed (transitively via langchain).

**Gaps this design closes:**
1. **No thread grouping.** LangSmith only forms a thread when a run's *metadata* carries a thread key
   (`session_id` / `thread_id` / `conversation_id`). Today's metadata has none → "each lead agent is its
   own thread" does not happen.
2. **Sub-agents are completely untraced.** `SubagentRunner.run` invokes the child with a bare config
   (`thread_id` + `recursion_limit` only) — no tags, no `is_subagent`, no parent linkage, no prompt info.
3. **No config surface, dependency pin, or env documentation.** `langsmith` is not in `requirements.txt`
   / `pyproject.toml`, and `.env.example` has no `LANGSMITH_*` vars.

## 2. Goals / non-goals

**Goals**
- Each lead agent (each workflow task) is its own LangSmith **thread**.
- Sub-agents are traced, tagged `is_subagent`, and grouped **into their parent lead agent's thread**.
- Rich, eval-ready metadata on every run, including a **prompt fingerprint** (ref + content hash) so a
  prompt version can be correlated with run outcomes.
- Config-driven activation (`observability:` block) layered over the standard `LANGSMITH_*` env vars.
- Zero behavior change and zero network when tracing is disabled (the default).

**Non-goals**
- Tracing the interactive CLI (`atom run` / `atom chat`). Only workflow runs are observed.
- Custom LangSmith dashboards, datasets, or automated evaluators (that is the separate evaluation phase).
- A bespoke callback/tracer implementation (we rely on LangChain's built-in env-activated tracer plus
  config metadata/tags).

## 3. Confirmed LangSmith facts driving the design

Verified against current LangSmith docs (docs.langchain.com/langsmith), 2026-07-06:
- **Threads** are formed by setting a thread key — `session_id`, `thread_id`, or `conversation_id` — in a
  run's **metadata** (not merely in LangGraph's `configurable`).
- Thread metadata **must be set on ALL runs, including child runs**. Child runs lacking it are excluded
  from thread-based filtering, token counting, and cost aggregation. So sub-agent grouping must be done
  by explicitly stamping the thread key — not by relying on trace-nesting alone.
- **Tags and metadata** attach to a LangChain/LangGraph run via `RunnableConfig` (`{"tags": [...],
  "metadata": {...}}`) — exactly what `build_run_config`/`_apply_trace` already do.

**Design consequence:** atom uses **`session_id`** as its canonical thread key. LangGraph auto-populates
`metadata.thread_id` from `configurable.thread_id` (which is a *unique per-child* id for sub-agents), so
using `thread_id` for grouping would scatter sub-agents into their own threads. `session_id` is a key we
fully control and LangGraph does not overwrite. We set `session_id = <lead/task thread id>` on the lead
run and on all of its sub-agent runs, so they share one thread.

## 4. Architecture

Introduce a single top-level module **`src/atom/observability.py`** (promoted from
`src/atom/workflow/observability.py`, which is deleted; the engine import is updated). It contains pure,
network-free functions. Trace metadata is assembled in **three layers**, each stamping only what it knows:

| Layer | Function | Called by | Knows |
|-------|----------|-----------|-------|
| Identity | `build_lead_trace(...)` | `engine._run_task` | workflow / run / step / task, `session_id`, `role=lead`, config default tags |
| Runtime | `enrich_lead_trace(trace, ...)` | `agent.build_lead_agent` | model, thinking, context window, recursion limit, compaction, **lead + summary prompt fingerprint** |
| Sub-agent | `build_subagent_trace(...)` | `subagent.SubagentRunner.run` | `is_subagent`, `subagent_type`, parent linkage, sub-agent prompt fingerprint |

Supporting functions:
- `apply_observability_env(cfg) -> None` — idempotently maps the `observability:` config block onto
  `LANGSMITH_*` env vars at engine startup. **Never overwrites an already-set env var** (env wins over
  config). Only sets `LANGSMITH_TRACING=true` when `enabled` **and** an API key is present, so a
  half-configured setup can never crash a run or silently attempt exports without a key.
- `prompt_fingerprint(text) -> str` — `sha256(text.encode()).hexdigest()[:12]`. Deterministic; used for
  every `*_sha` field.
- `git_sha() -> str | None` — best-effort short commit sha (gated by `capture_git_sha`); returns `None`
  outside a repo or on error (never raises).

`runtime.py` keeps `_apply_trace` / `build_run_config` as-is (they already merge `run_name`/`tags`/
`metadata`). Everything is gated on a trace being present, so the CLI (which passes `trace=None`) attaches
no observability metadata.

## 5. Metadata & tag schema (eval-ready)

**Tags** (low-cardinality, for LangSmith UI filtering):
- `atom-workflow`
- `workflow:{name}`
- `profile:{name}`
- `model:{name}`
- `role:lead` **or** `role:subagent`
- `subagent_type:{type}` (sub-agents only)
- plus every entry in `observability.default_tags`

**Metadata** (flat scalars — easy to filter on):

Threading
- `session_id` — the lead/task thread id; identical on the lead and all its sub-agents (the thread key).

Role & lineage
- `agent_role` — `"lead"` | `"subagent"`
- `is_subagent` — bool
- `subagent_type` — `"general-purpose"` | `"bash"` (sub-agents)
- `subagent_description` — the `delegate_task` description (sub-agents)
- `parent_thread_id` — the lead task thread id (sub-agents)

Workflow context
- `workflow`, `run_id`, `step_index`, `step_title`, `task_id`

Runtime / debug
- `profile_name`, `model` (the effective model spec key — `override_model` or `profile.model`, e.g.
  `haiku`), `thinking` (the resolved `profile.thinking`: mode string, int budget, or `null`),
  `context_window`, `recursion_limit`, `compaction_ratio`, `compaction_summary_input_tokens`

Prompt fingerprint (efficacy) — gated by `observability.include_prompt_fingerprint`
- `system_prompt_ref` + `system_prompt_sha` (lead: `@prompts/lead_system.md`; sub-agent: the resolved
  `subagent_general.md` / `subagent_bash.md`)
- `summary_prompt_ref` + `summary_prompt_sha` (lead only — the compaction summary prompt)

Version
- `atom_git_sha` — best-effort, gated by `observability.capture_git_sha`

**`run_name`**
- lead: `{workflow}/{step_title}/{task_id}`
- sub-agent: `{workflow}/{step_title}/{task_id}/sub:{description[:40]}`

## 6. Config surface, env, dependency

New `ObservabilityConfig` in `src/atom/config/schema.py`, wired into `AtomConfig` as `observability:`:

```python
class ObservabilityConfig(_Base):
    enabled: bool = False               # -> LANGSMITH_TRACING=true (only if API key present & env unset)
    project: Optional[str] = None       # -> LANGSMITH_PROJECT (only if env unset)
    default_tags: list[str] = Field(default_factory=list)
    include_prompt_fingerprint: bool = True
    capture_git_sha: bool = True
```

`config.yaml` documented block:
```yaml
observability:
  enabled: false            # set true (or export LANGSMITH_TRACING=true) to send traces to LangSmith
  project: atom-workflows   # LangSmith project name (LANGSMITH_PROJECT overrides)
  default_tags: []          # tags added to every workflow run
  include_prompt_fingerprint: true
  capture_git_sha: true
```

`.env.example` additions (commented):
```
# Optional: LangSmith tracing for workflow runs. Required only when observability.enabled
# (or LANGSMITH_TRACING) is on. Env vars take precedence over the observability: config block.
# LANGSMITH_TRACING=true
# LANGSMITH_API_KEY=
# LANGSMITH_PROJECT=atom-workflows
```

Dependency: add an explicit pin `langsmith>=0.9,<1` to `requirements.txt` **and** `pyproject.toml`
(currently only present transitively in `requirements.lock.txt`).

**Precedence rule:** an already-set `LANGSMITH_*` env var always wins; config only fills unset values.
Tracing is enabled only when a key is available, so `enabled: true` without a key is a safe no-op.

## 7. Wiring & data flow

All observability work is gated on a trace being present (CLI passes `trace=None` → untouched).

1. **Startup:** `WorkflowEngine.__init__` calls `apply_observability_env(self.cfg)` once (idempotent).
2. **Per task:** `engine._run_task` calls `build_lead_trace(workflow, run_id, step_index, step_title,
   task_id, session_id=ts.thread_id, obs=cfg.observability)` and passes the resulting `trace` into
   `run_agent` (unchanged call site — `trace=trace`).
3. **Lead enrichment:** `run_agent` forwards `trace` into `build_lead_agent(...)` (new optional param).
   After it renders the system prompt and resolves the summary prompt, `build_lead_agent` calls
   `enrich_lead_trace(trace, profile, profile_name, prepared, system_prompt, summary_prompt_ref,
   summary_prompt_text, obs)` **in place**, then hands the enriched base metadata to the `SubagentRunner`
   (new `base_trace` field). `run_agent` then calls `build_run_config(thread_id, recursion_limit, trace)`
   as today. Order is safe: `build_lead_agent` runs before `build_run_config` in `run_agent`.
4. **Sub-agent:** `SubagentRunner.run` computes the child's rendered prompt (it already does, in
   `_child_agent`) and calls `build_subagent_trace(base_trace, parent_thread_id, subagent_type,
   description, rendered_prompt, recursion_limit=self.recursion_limit, obs=...)`. The child's `ainvoke`
   config carries this trace's `run_name`/`tags`/`metadata` alongside the existing
   `configurable.thread_id` (child id) + `recursion_limit`. The child metadata sets `session_id =
   parent_thread_id`, `is_subagent=true`, `agent_role="subagent"`, `subagent_type`, `subagent_description`,
   `parent_thread_id`, and overrides the prompt fingerprint with the sub-agent prompt's.

Sub-agents inherit workflow/run/step/model fields from `base_trace` (so they remain filterable by run),
override the role/prompt/thread fields, and — because atom's sub-agent execution is async on one event
loop (`asyncio.Semaphore` + `asyncio.wait_for`) — also nest naturally under the parent trace as a bonus
tree view. Grouping does **not** depend on that nesting; it is guaranteed by the explicit `session_id`.

## 8. Verification spike (in the plan; not a release blocker)

Confirm, against a dev LangSmith project (or by inspecting the emitted run metadata via a
`LANGSMITH_TRACING` local run), that:
- a lead task and its sub-agents land in **one** thread keyed by `session_id`, and
- LangGraph's auto-populated `metadata.thread_id` (child id, for sub-agents) does **not** override the
  `session_id`-based grouping.

Fallback if precedence is a problem: additionally stamp `thread_id = session_id` explicitly on each run.
Low risk — the LangSmith docs already prescribe stamping thread metadata on child runs.

## 9. Testing

No real network; tracing stays disabled in tests (assert we never force-enable without a key).
- `ObservabilityConfig` parses from YAML and round-trips defaults.
- `apply_observability_env`: sets env from config when unset; **does not overwrite** existing env; does
  **not** enable tracing when no API key is present; idempotent on repeat calls.
- `build_lead_trace`: correct `run_name`, tags (incl. `role:lead`, default tags), and metadata
  (`session_id` == task thread id, workflow identifiers, `agent_role="lead"`, `is_subagent=False`).
- `enrich_lead_trace`: adds model/thinking/context_window/recursion_limit/compaction and prompt
  fingerprints; respects `include_prompt_fingerprint=False` and `capture_git_sha=False`.
- `build_subagent_trace`: `is_subagent=True`, `role:subagent` tag, `subagent_type:*` tag, `session_id`
  == parent thread id, `parent_thread_id` set, sub-agent prompt fingerprint overrides the lead's.
- `prompt_fingerprint` is deterministic and stable for identical input.
- Engine test: a task's trace carries `session_id`.
- Guard test: `run_agent` with `trace=None` (CLI path) produces a run config with **no** observability
  metadata beyond `configurable`/`recursion_limit`.

## 10. Migration / rollout

- Move `src/atom/workflow/observability.py` → `src/atom/observability.py`; update the import in
  `engine.py`; expand the module per §4. (No external callers besides the engine.)
- Backward compatible and off by default: with `observability.enabled=false` and no `LANGSMITH_*` env,
  behavior and output are unchanged and no traces are sent.

## 11. Risks & open questions

- **Thread-key precedence** (see §8) — mitigated by the spike + `thread_id` fallback.
- **Sub-agent nesting vs. separate root** — irrelevant to correctness because grouping is by explicit
  `session_id`; nesting only affects the visual tree.
- **langsmith version drift** — pin `>=0.9,<1`; the metadata/thread contract used here is stable public API.
- **Metadata cardinality** — all values are flat scalars; high-cardinality items (`run_id`, shas) live in
  metadata (filterable) not tags (kept low-cardinality).
