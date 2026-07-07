# LangSmith Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give atom's workflow service first-class LangSmith tracing — each lead agent (task) is its own thread, sub-agents are tagged and grouped into the parent thread, and every run carries eval-ready metadata including a prompt fingerprint.

**Architecture:** A single `src/atom/observability.py` module builds trace config (`run_name`/`tags`/`metadata`) in three layers — identity (engine), runtime (`build_lead_agent`), and sub-agent (`SubagentRunner`) — merged into each LangGraph `ainvoke` config. `session_id` is the canonical thread key. Activation is config-driven (`observability:` block) layered over `LANGSMITH_*` env vars. Everything is gated on a trace being present, so the interactive CLI stays untraced.

**Tech Stack:** Python 3, LangChain/LangGraph v1, LangSmith (env-activated tracer + config metadata/tags), Pydantic config, pytest.

## Global Constraints

- **Scope: workflow runs only.** No observability on the interactive CLI (`atom run`/`atom chat`); all new behavior is gated on a `trace` being present.
- **Canonical thread key: `session_id`** — set on the lead run and on all its sub-agent runs (never rely on `thread_id`, which LangGraph auto-populates per sub-agent from `configurable.thread_id`).
- **Env wins over config.** `apply_observability_env` never overwrites an already-set `LANGSMITH_*` var, and enables tracing only when `enabled` **and** `LANGSMITH_API_KEY` is present.
- **No network in tests.** Tracing stays disabled during tests; assert we never force-enable without a key.
- **Metadata values are flat scalars** (str/int/float/bool/None). High-cardinality items (`run_id`, shas) go in metadata; tags stay low-cardinality.
- **Dependency floor:** `langsmith>=0.9,<1` (add to `requirements.txt` and `pyproject.toml`).
- **Run tests with** `.venv/bin/python -m pytest` (NOT bare `pytest` — several modules do `from tests.conftest import ...`).
- Follow existing atom patterns: `_Base(extra="ignore")` config models; scripted `make_prepared` in tests; keep functions small and single-purpose.

---

## File Structure

- **Create** `src/atom/observability.py` — all trace builders + env activation + `_apply_trace` (moved here from `runtime.py`). Pure, network-free.
- **Delete** `src/atom/workflow/observability.py` — superseded (its `build_trace` becomes `build_lead_trace`).
- **Modify** `src/atom/config/schema.py` — add `ObservabilityConfig`, wire into `AtomConfig`.
- **Modify** `src/atom/runtime.py` — re-export `_apply_trace` from observability; pass `trace` + overrides into `build_lead_agent`.
- **Modify** `src/atom/agent.py` — `build_lead_agent` enriches the trace and hands base metadata to the runner; `_build_middlewares` passes it through.
- **Modify** `src/atom/subagent.py` — `SubagentRunner` builds/applies the sub-agent trace on the child config.
- **Modify** `src/atom/workflow/engine.py` — use `build_lead_trace` with `session_id`; call `apply_observability_env` at startup.
- **Modify** `config.yaml`, `.env.example`, `requirements.txt`, `pyproject.toml` — config block, env docs, dependency pin.
- **Create** `tests/test_observability_config.py`, `tests/test_observability.py`; **delete** `tests/test_workflow_observability.py`; **extend** `tests/test_workflow_engine.py` and `tests/test_subagent.py`.

---

## Task 1: Config surface, dependency pin, env docs

**Files:**
- Modify: `src/atom/config/schema.py`
- Modify: `config.yaml`, `.env.example`, `requirements.txt`, `pyproject.toml`
- Test: `tests/test_observability_config.py`

**Interfaces:**
- Produces: `ObservabilityConfig(enabled: bool=False, project: Optional[str]=None, default_tags: list[str]=[], include_prompt_fingerprint: bool=True, capture_git_sha: bool=True)` and `AtomConfig.observability: ObservabilityConfig`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_observability_config.py`:

```python
"""Observability config schema."""
from __future__ import annotations

from atom.config.schema import AtomConfig, ObservabilityConfig


def test_observability_defaults():
    cfg = AtomConfig()
    assert cfg.observability.enabled is False
    assert cfg.observability.project is None
    assert cfg.observability.default_tags == []
    assert cfg.observability.include_prompt_fingerprint is True
    assert cfg.observability.capture_git_sha is True


def test_observability_override():
    oc = ObservabilityConfig(
        enabled=True, project="p", default_tags=["team:atom"],
        include_prompt_fingerprint=False, capture_git_sha=False,
    )
    assert oc.enabled is True and oc.project == "p"
    assert oc.default_tags == ["team:atom"]
    assert oc.include_prompt_fingerprint is False
    assert oc.capture_git_sha is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_observability_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'ObservabilityConfig'`.

- [ ] **Step 3: Add `ObservabilityConfig` and wire it into `AtomConfig`**

In `src/atom/config/schema.py`, add this class after `GuardrailConfig` (near line 66):

```python
class ObservabilityConfig(_Base):
    # LangSmith tracing for workflow runs. Layered over LANGSMITH_* env vars (env wins).
    enabled: bool = False               # -> LANGSMITH_TRACING=true (only if API key present & env unset)
    project: Optional[str] = None       # -> LANGSMITH_PROJECT (only if env unset)
    default_tags: list[str] = Field(default_factory=list)  # tags added to every workflow run
    include_prompt_fingerprint: bool = True  # add system/summary prompt ref + content hash to metadata
    capture_git_sha: bool = True        # best-effort atom_git_sha in metadata
```

In `AtomConfig` (near line 112, alongside `guardrails`), add the field:

```python
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_observability_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Add the dependency pin**

In `requirements.txt`, under the `# Core harness` group (after the `langgraph-checkpoint-sqlite` line), add:

```
# Observability
langsmith>=0.9,<1
```

In `pyproject.toml`, inside the `dependencies = [` list (after the `langgraph` entries), add:

```
    "langsmith>=0.9,<1",
```

- [ ] **Step 6: Document config + env**

In `config.yaml`, add this block after the `guardrails:` block (before `track_usage:`):

```yaml
observability:
  enabled: false            # set true (or export LANGSMITH_TRACING=true) to send workflow traces to LangSmith
  project: atom-workflows   # LangSmith project name (LANGSMITH_PROJECT env overrides)
  default_tags: []          # tags added to every workflow run
  include_prompt_fingerprint: true   # correlate a prompt version with run outcomes
  capture_git_sha: true
```

In `.env.example`, append after the `ATOM_HOME` comment block:

```
# Optional: LangSmith tracing for workflow runs. Required only when observability.enabled
# (or LANGSMITH_TRACING) is on. Env vars take precedence over the observability: config block.
# LANGSMITH_TRACING=true
# LANGSMITH_API_KEY=
# LANGSMITH_PROJECT=atom-workflows
```

- [ ] **Step 7: Commit**

```bash
git add src/atom/config/schema.py tests/test_observability_config.py config.yaml .env.example requirements.txt pyproject.toml
git commit -m "feat(observability): ObservabilityConfig + langsmith dep + env docs"
```

---

## Task 2: Observability module foundation + engine identity wiring

**Files:**
- Create: `src/atom/observability.py`
- Delete: `src/atom/workflow/observability.py`, `tests/test_workflow_observability.py`
- Modify: `src/atom/runtime.py` (re-export `_apply_trace`)
- Modify: `src/atom/workflow/engine.py` (use `build_lead_trace`; call `apply_observability_env`)
- Test: `tests/test_observability.py`

**Interfaces:**
- Consumes: `ObservabilityConfig` (Task 1).
- Produces:
  - `prompt_fingerprint(text: str) -> str` (12-char sha256)
  - `git_sha() -> str | None`
  - `apply_observability_env(cfg: AtomConfig) -> None`
  - `_apply_trace(run_config: dict, trace: dict | None) -> dict`
  - `build_lead_trace(*, workflow, run_id, step_index, step_title, task_id, session_id, obs) -> dict`

- [ ] **Step 1: Write the failing test**

Create `tests/test_observability.py`:

```python
"""Observability module: helpers, env activation, and the lead-trace builder."""
from __future__ import annotations

import os

from atom.config.schema import AtomConfig, ObservabilityConfig
from atom.observability import (
    apply_observability_env,
    build_lead_trace,
    prompt_fingerprint,
)


def test_prompt_fingerprint_deterministic():
    a = prompt_fingerprint("hello world")
    assert a == prompt_fingerprint("hello world")
    assert len(a) == 12
    assert prompt_fingerprint("other") != a


def test_apply_env_fills_unset(monkeypatch):
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=True, project="proj"))
    apply_observability_env(cfg)
    assert os.environ["LANGSMITH_PROJECT"] == "proj"
    assert os.environ["LANGSMITH_TRACING"] == "true"


def test_apply_env_respects_existing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_PROJECT", "keep")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=True, project="proj"))
    apply_observability_env(cfg)
    assert os.environ["LANGSMITH_PROJECT"] == "keep"   # not overwritten
    assert os.environ["LANGSMITH_TRACING"] == "false"  # not overwritten


def test_apply_env_no_key_no_enable(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=True))
    apply_observability_env(cfg)
    assert "LANGSMITH_TRACING" not in os.environ  # no key -> safe no-op


def test_build_lead_trace_shape():
    obs = ObservabilityConfig(default_tags=["team:atom"])
    t = build_lead_trace(
        workflow="poems", run_id="r1", step_index=0, step_title="Draft",
        task_id="poet_a", session_id="r1:s0:poet_a", obs=obs,
    )
    assert t["run_name"] == "poems/Draft/poet_a"
    assert "atom-workflow" in t["tags"] and "role:lead" in t["tags"]
    assert "team:atom" in t["tags"]
    md = t["metadata"]
    assert md["session_id"] == "r1:s0:poet_a"
    assert md["agent_role"] == "lead" and md["is_subagent"] is False
    assert md["workflow"] == "poems" and md["run_id"] == "r1" and md["task_id"] == "poet_a"
    assert md["step_index"] == 0 and md["step_title"] == "Draft"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_observability.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.observability'`.

- [ ] **Step 3: Create `src/atom/observability.py`**

```python
"""LangSmith observability for workflow runs: trace config builders + env activation.

Trace metadata is assembled in three layers, each stamping only what it knows:
  build_lead_trace     (identity)   -> workflow.engine._run_task
  enrich_lead_trace    (runtime)    -> agent.build_lead_agent
  build_subagent_trace (sub-agent)  -> subagent.SubagentRunner.run

atom's canonical thread key is ``session_id``. LangGraph auto-populates ``thread_id`` from
``configurable.thread_id`` (unique per sub-agent), so using it would scatter sub-agents into their
own threads; ``session_id`` is a key we fully control. LangSmith activates purely from LANGSMITH_*
env vars; when unset, these dicts are harmless metadata on the run config.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Any, Optional

from atom.config.schema import AtomConfig, ObservabilityConfig


def prompt_fingerprint(text: str) -> str:
    """Stable 12-char sha256 of a rendered prompt — correlate a prompt version with run outcomes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def git_sha() -> Optional[str]:
    """Best-effort short commit sha; None outside a repo or on any error (never raises)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — observability must never break a run
        return None


def apply_observability_env(cfg: AtomConfig) -> None:
    """Map the observability config block onto LANGSMITH_* env, never overwriting existing vars.

    Tracing is enabled only when requested AND an API key is present, so a half-configured setup is a
    safe no-op rather than a crash or a keyless export attempt. Idempotent.
    """
    obs = cfg.observability
    if obs.project and not os.environ.get("LANGSMITH_PROJECT"):
        os.environ["LANGSMITH_PROJECT"] = obs.project
    if (
        obs.enabled
        and not os.environ.get("LANGSMITH_TRACING")
        and os.environ.get("LANGSMITH_API_KEY")
    ):
        os.environ["LANGSMITH_TRACING"] = "true"


def _apply_trace(run_config: dict, trace: dict | None) -> dict:
    """Merge LangSmith run_name/tags/metadata into a LangGraph run config (in place)."""
    if trace:
        for key in ("run_name", "tags", "metadata"):
            if trace.get(key) is not None:
                run_config[key] = trace[key]
    return run_config


def build_lead_trace(
    *, workflow: str, run_id: str, step_index: int, step_title: str,
    task_id: str, session_id: str, obs: ObservabilityConfig,
) -> dict[str, Any]:
    """Identity layer: workflow/run/step/task + the session_id thread key + role=lead."""
    tags = [
        "atom-workflow",
        f"workflow:{workflow}",
        f"step:{step_title}",
        f"task:{task_id}",
        f"run:{run_id}",
        "role:lead",
        *obs.default_tags,
    ]
    metadata = {
        "session_id": session_id,
        "agent_role": "lead",
        "is_subagent": False,
        "workflow": workflow,
        "run_id": run_id,
        "step_index": step_index,
        "step_title": step_title,
        "task_id": task_id,
    }
    return {"run_name": f"{workflow}/{step_title}/{task_id}", "tags": tags, "metadata": metadata}
```

- [ ] **Step 4: Run the new module test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_observability.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Move `_apply_trace` out of `runtime.py` (re-export for back-compat)**

In `src/atom/runtime.py`, delete the local `_apply_trace` definition (the `def _apply_trace(...)` block, ~lines 61-67) and add this import near the other `atom` imports (after `from atom.config.schema import AtomConfig`):

```python
from atom.observability import _apply_trace
```

Leave `build_run_config` unchanged — it already calls `_apply_trace`, which now resolves to the imported one. `tests/test_runtime_trace.py` imports `from atom.runtime import _apply_trace`, which the re-export keeps working.

- [ ] **Step 6: Switch the engine to `build_lead_trace` + activate env at startup**

In `src/atom/workflow/engine.py`:

Replace the import (line 17):

```python
from atom.observability import apply_observability_env, build_lead_trace
```

At the end of `WorkflowEngine.__init__` (after `self._task_cfg = self._build_task_cfg(cfg)`), add:

```python
        # Map observability config -> LANGSMITH_* env once, before any run (idempotent).
        apply_observability_env(cfg)
```

In `_run_task`, replace the `build_trace(...)` call (~lines 191-195) with:

```python
            trace = build_lead_trace(
                workflow=workflow.name, run_id=manifest.run_id,
                step_index=step_state.index, step_title=step_state.title, task_id=ts.id,
                session_id=ts.thread_id, obs=self.cfg.observability,
            )
```

- [ ] **Step 7: Delete the superseded module + its test**

```bash
git rm src/atom/workflow/observability.py tests/test_workflow_observability.py
```

- [ ] **Step 8: Run the full suite to verify nothing dangles**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all green; `test_runtime_trace.py`, `test_recursion_limit.py`, and `test_workflow_engine.py` still pass — `build_run_config`/`_apply_trace` behavior is unchanged).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(observability): observability module + session_id lead trace wired into engine"
```

---

## Task 3: Runtime enrichment layer (model + prompt fingerprint)

**Files:**
- Modify: `src/atom/observability.py` (add `enrich_lead_trace`)
- Modify: `src/atom/agent.py` (`build_lead_agent` enriches the trace)
- Modify: `src/atom/runtime.py` (pass `trace` + overrides into `build_lead_agent`)
- Test: `tests/test_observability.py` (extend)

**Interfaces:**
- Consumes: `build_lead_trace` output dict; `AgentProfile`, `AtomConfig` (`cfg.compaction`, `cfg.config_dir`).
- Produces: `enrich_lead_trace(trace, *, cfg, profile, profile_name, system_prompt, context_window, override_model=None, override_thinking=None) -> None` (mutates `trace` in place). `build_lead_agent` gains a `trace: dict | None = None` param.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_observability.py`:

```python
from atom.config.schema import AgentProfile
from atom.observability import enrich_lead_trace


def test_enrich_lead_trace_adds_runtime_and_fingerprint():
    obs = ObservabilityConfig(include_prompt_fingerprint=True, capture_git_sha=False)
    # summary_prompt=None keeps this a pure unit test (no prompt-file IO).
    cfg = AtomConfig(
        observability=obs,
        agents={"default": AgentProfile(model="haiku", thinking="low", summary_prompt=None)},
    )
    trace = {"run_name": "x", "tags": ["role:lead"], "metadata": {"session_id": "t"}}
    enrich_lead_trace(
        trace, cfg=cfg, profile=cfg.profile("default"), profile_name="default",
        system_prompt="SYSTEM PROMPT TEXT", context_window=200_000,
    )
    md = trace["metadata"]
    assert md["session_id"] == "t"  # preserved
    assert md["profile_name"] == "default" and md["model"] == "haiku" and md["thinking"] == "low"
    assert md["context_window"] == 200_000 and md["recursion_limit"] == 400
    assert md["compaction_ratio"] == 0.5 and md["compaction_summary_input_tokens"] == 8000
    assert md["system_prompt_ref"] == "@prompts/lead_system.md"
    assert len(md["system_prompt_sha"]) == 12
    assert "summary_prompt_sha" not in md    # summary_prompt was None
    assert "atom_git_sha" not in md          # capture_git_sha False
    assert "profile:default" in trace["tags"] and "model:haiku" in trace["tags"]


def test_enrich_lead_trace_respects_toggles_and_overrides():
    obs = ObservabilityConfig(include_prompt_fingerprint=False, capture_git_sha=False)
    cfg = AtomConfig(observability=obs)
    trace = {"tags": [], "metadata": {}}
    enrich_lead_trace(
        trace, cfg=cfg, profile=cfg.profile("default"), profile_name="default",
        system_prompt="X", context_window=1000,
        override_model="opus", override_thinking="high",
    )
    md = trace["metadata"]
    assert "system_prompt_sha" not in md and "system_prompt_ref" not in md
    assert md["model"] == "opus" and md["thinking"] == "high"  # overrides win
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_observability.py::test_enrich_lead_trace_adds_runtime_and_fingerprint -v`
Expected: FAIL with `ImportError: cannot import name 'enrich_lead_trace'`.

- [ ] **Step 3: Add `enrich_lead_trace` to `src/atom/observability.py`**

Add `AgentProfile` to the schema import at the top:

```python
from atom.config.schema import AgentProfile, AtomConfig, ObservabilityConfig
```

Append this function:

```python
def enrich_lead_trace(
    trace: dict[str, Any], *, cfg: AtomConfig, profile: AgentProfile, profile_name: str,
    system_prompt: str, context_window: int,
    override_model: str | None = None, override_thinking: Any = None,
) -> None:
    """Runtime layer: model/thinking/window/limits/compaction + prompt fingerprints, in place."""
    from atom.prompts.render import resolve_prompt_ref

    obs = cfg.observability
    model_key = override_model or profile.model
    thinking = override_thinking if override_thinking is not None else profile.thinking

    md = trace.setdefault("metadata", {})
    md.update({
        "profile_name": profile_name,
        "model": model_key,
        "thinking": thinking,
        "context_window": context_window,
        "recursion_limit": profile.recursion_limit,
        "compaction_ratio": cfg.compaction.ratio,
        "compaction_summary_input_tokens": cfg.compaction.summary_input_tokens,
    })
    tags = trace.setdefault("tags", [])
    tags.append(f"profile:{profile_name}")
    tags.append(f"model:{model_key}")

    if obs.include_prompt_fingerprint:
        md["system_prompt_ref"] = profile.system_prompt
        md["system_prompt_sha"] = prompt_fingerprint(system_prompt)
        if profile.summary_prompt:
            summary_text = resolve_prompt_ref(profile.summary_prompt, cfg.config_dir)
            md["summary_prompt_ref"] = profile.summary_prompt
            md["summary_prompt_sha"] = prompt_fingerprint(summary_text)
    if obs.capture_git_sha:
        sha = git_sha()
        if sha:
            md["atom_git_sha"] = sha
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_observability.py -v`
Expected: PASS (all tests in the file, including the 2 new ones).

- [ ] **Step 5: Enrich the trace inside `build_lead_agent`**

In `src/atom/agent.py`, add a `trace` param to `build_lead_agent` (in the keyword-only block, after `override_system_prompt`):

```python
    override_system_prompt: str | None = None,
    trace: dict | None = None,
):
```

After the `system_prompt = render_lead_system_prompt(...)` call and before `middleware = _build_middlewares(...)` (~line 144-145), insert:

```python
    if trace is not None:
        from atom.observability import enrich_lead_trace

        enrich_lead_trace(
            trace, cfg=cfg, profile=profile, profile_name=profile_name,
            system_prompt=system_prompt, context_window=prepared.context_window,
            override_model=override_model, override_thinking=override_thinking,
        )
```

(Leave the `_build_middlewares(...)` call unchanged in this task — the runner wiring is Task 4.)

- [ ] **Step 6: Pass `trace` + overrides from `run_agent`**

In `src/atom/runtime.py`, update the `build_lead_agent(...)` call inside `run_agent` (~lines 122-125) to forward the trace and overrides:

```python
        agent = build_lead_agent(
            cfg, profile_name, prepared=prepared, checkpointer=cp,
            override_model=override_model, override_thinking=override_thinking,
            override_system_prompt=override_system_prompt, trace=trace,
        )
```

The subsequent `run_config = build_run_config(thread_id, prof.recursion_limit, trace)` now reads the enriched `trace` (enrichment mutated it in place before this line).

- [ ] **Step 7: Verify the CLI path stays clean + full suite**

Run: `.venv/bin/python -m pytest tests/test_runtime_trace.py tests/test_observability.py -q`
Expected: PASS. `test_run_agent_accepts_trace` still passes (trace enrichment is additive); `test_apply_trace_none_is_noop` proves `trace=None` (CLI) attaches nothing.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all green).

- [ ] **Step 8: Commit**

```bash
git add src/atom/observability.py src/atom/agent.py src/atom/runtime.py tests/test_observability.py
git commit -m "feat(observability): enrich lead trace with model + prompt fingerprint"
```

---

## Task 4: Sub-agent trace layer

**Files:**
- Modify: `src/atom/observability.py` (add `build_subagent_trace`)
- Modify: `src/atom/subagent.py` (`SubagentRunner` builds + applies the child trace)
- Modify: `src/atom/agent.py` (`_build_middlewares` passes `trace` + observability into the runner)
- Test: `tests/test_observability.py` (extend), `tests/test_subagent.py` (extend)

**Interfaces:**
- Consumes: the enriched lead `trace` dict (Task 3) as `base_trace`; `ObservabilityConfig`.
- Produces:
  - `build_subagent_trace(base_trace, *, parent_thread_id, subagent_type, description, rendered_prompt, subagent_prompt_ref, recursion_limit, obs) -> dict | None` (None when `base_trace` is None).
  - `SubagentRunner` gains `base_trace: dict | None = None` and `observability: Any = None` fields, and a `_child_system(subagent_type) -> str` helper; `_child_agent(subagent_type, system=None)`.

- [ ] **Step 1: Write the failing unit test for `build_subagent_trace`**

Append to `tests/test_observability.py`:

```python
from atom.observability import build_subagent_trace


def _lead_base():
    return {
        "run_name": "poems/Draft/poet_a",
        "tags": ["atom-workflow", "workflow:poems", "role:lead", "model:haiku"],
        "metadata": {
            "session_id": "r1:s0:poet_a", "agent_role": "lead", "is_subagent": False,
            "workflow": "poems", "run_id": "r1", "step_index": 0, "step_title": "Draft",
            "task_id": "poet_a", "model": "haiku",
            "system_prompt_ref": "@prompts/lead_system.md", "system_prompt_sha": "leadhash1234",
            "summary_prompt_ref": "@prompts/summary.md", "summary_prompt_sha": "sumhash1234",
        },
    }


def test_build_subagent_trace_overrides_role_and_prompt():
    obs = ObservabilityConfig(include_prompt_fingerprint=True)
    t = build_subagent_trace(
        _lead_base(), parent_thread_id="r1:s0:poet_a", subagent_type="bash",
        description="crunch the numbers", rendered_prompt="SUBAGENT SYSTEM",
        subagent_prompt_ref="@prompts/subagent_bash.md", recursion_limit=300, obs=obs,
    )
    md = t["metadata"]
    assert md["is_subagent"] is True and md["agent_role"] == "subagent"
    assert md["session_id"] == "r1:s0:poet_a"       # same thread as the lead
    assert md["parent_thread_id"] == "r1:s0:poet_a"
    assert md["subagent_type"] == "bash"
    assert md["subagent_description"] == "crunch the numbers"
    assert md["recursion_limit"] == 300
    assert md["workflow"] == "poems" and md["run_id"] == "r1"   # inherited from base
    assert md["system_prompt_ref"] == "@prompts/subagent_bash.md"
    assert md["system_prompt_sha"] == prompt_fingerprint("SUBAGENT SYSTEM")
    assert "summary_prompt_ref" not in md and "summary_prompt_sha" not in md  # lead-only, dropped
    assert "role:lead" not in t["tags"] and "role:subagent" in t["tags"]
    assert "subagent_type:bash" in t["tags"]
    assert t["run_name"] == "poems/Draft/poet_a/sub:crunch the numbers"


def test_build_subagent_trace_none_base_returns_none():
    assert build_subagent_trace(
        None, parent_thread_id="x", subagent_type="bash", description="d",
        rendered_prompt="p", subagent_prompt_ref="r", recursion_limit=300,
        obs=ObservabilityConfig(),
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_observability.py::test_build_subagent_trace_overrides_role_and_prompt -v`
Expected: FAIL with `ImportError: cannot import name 'build_subagent_trace'`.

- [ ] **Step 3: Add `build_subagent_trace` to `src/atom/observability.py`**

```python
def build_subagent_trace(
    base_trace: dict[str, Any] | None, *, parent_thread_id: str, subagent_type: str,
    description: str, rendered_prompt: str, subagent_prompt_ref: str,
    recursion_limit: int, obs: ObservabilityConfig,
) -> dict[str, Any] | None:
    """Sub-agent layer: inherit the lead's workflow/run/model fields, override role + thread + prompt.

    Returns None when there is no base trace (e.g. the CLI path), so callers can skip tracing.
    """
    if base_trace is None:
        return None
    base_md = base_trace.get("metadata", {})
    md = dict(base_md)  # inherit workflow/run/step/model/context_window/git_sha
    md.update({
        "session_id": parent_thread_id,   # keep the sub-agent in the lead's thread
        "agent_role": "subagent",
        "is_subagent": True,
        "subagent_type": subagent_type,
        "subagent_description": description,
        "parent_thread_id": parent_thread_id,
        "recursion_limit": recursion_limit,
    })
    md.pop("summary_prompt_ref", None)     # summary prompt is a lead-only concept
    md.pop("summary_prompt_sha", None)
    if obs.include_prompt_fingerprint:
        md["system_prompt_ref"] = subagent_prompt_ref
        md["system_prompt_sha"] = prompt_fingerprint(rendered_prompt)
    else:
        md.pop("system_prompt_ref", None)
        md.pop("system_prompt_sha", None)

    tags = [t for t in base_trace.get("tags", []) if t != "role:lead"]
    tags += ["role:subagent", f"subagent_type:{subagent_type}"]

    wf = base_md.get("workflow", "")
    step = base_md.get("step_title", "")
    task = base_md.get("task_id", "")
    run_name = f"{wf}/{step}/{task}/sub:{description[:40]}"
    return {"run_name": run_name, "tags": tags, "metadata": md}
```

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_observability.py -q`
Expected: PASS (all).

- [ ] **Step 5: Write the failing runner-wiring test**

Append to `tests/test_subagent.py`:

```python
@pytest.mark.asyncio
async def test_subagent_child_config_carries_trace(base_config):
    from atom.config.schema import ObservabilityConfig
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[AIMessage(content="CHILD_DONE")],
                              profile={"max_input_tokens": 100_000})
    base_trace = {
        "run_name": "wf/Draft/t", "tags": ["role:lead", "workflow:wf"],
        "metadata": {"session_id": "p1", "workflow": "wf", "run_id": "r1",
                     "step_title": "Draft", "task_id": "t", "agent_role": "lead",
                     "is_subagent": False},
    }
    runner = SubagentRunner(
        model=model, home=str(base_config.home), context_window=100_000,
        bash_enabled=False, base_trace=base_trace, observability=ObservabilityConfig(),
    )

    captured = {}

    class _StubAgent:
        async def ainvoke(self, inp, config=None, context=None):
            captured["config"] = config
            return {"messages": [AIMessage(content="CHILD_DONE")]}

    runner._child_agent = lambda st, system=None: _StubAgent()

    text, _usage = await runner.run("p1", "do the thing", "go", "general-purpose")
    assert text == "CHILD_DONE"
    cfg = captured["config"]
    assert cfg["configurable"]["thread_id"].startswith("p1:sub:")  # child keeps its own state id
    md = cfg["metadata"]
    assert md["is_subagent"] is True and md["agent_role"] == "subagent"
    assert md["session_id"] == "p1"                 # grouped into the lead's thread
    assert md["parent_thread_id"] == "p1"
    assert md["subagent_type"] == "general-purpose"
    assert "role:subagent" in cfg["tags"] and "subagent_type:general-purpose" in cfg["tags"]


@pytest.mark.asyncio
async def test_subagent_no_base_trace_is_untraced(base_config):
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[AIMessage(content="OK")],
                              profile={"max_input_tokens": 100_000})
    runner = SubagentRunner(model=model, home=str(base_config.home),
                            context_window=100_000, bash_enabled=False)  # no base_trace

    captured = {}

    class _StubAgent:
        async def ainvoke(self, inp, config=None, context=None):
            captured["config"] = config
            return {"messages": [AIMessage(content="OK")]}

    runner._child_agent = lambda st, system=None: _StubAgent()
    await runner.run("p1", "d", "go", "general-purpose")
    cfg = captured["config"]
    assert "metadata" not in cfg and "tags" not in cfg  # CLI-style: nothing attached
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_subagent.py::test_subagent_child_config_carries_trace -v`
Expected: FAIL — `SubagentRunner` has no `base_trace`/`observability` fields (TypeError on the constructor).

- [ ] **Step 7: Wire the trace into `SubagentRunner`**

In `src/atom/subagent.py`:

Add two fields to the `SubagentRunner` dataclass (after `recursion_limit: int = 300`):

```python
    base_trace: dict | None = None       # enriched lead trace; None -> sub-agent runs untraced
    observability: Any = None            # ObservabilityConfig | None
```

Add `Any` to the typing import at the top of the file:

```python
from typing import Any, Literal
```

Extract prompt rendering into a helper and let `_child_agent` accept a pre-rendered system. Replace the existing `_child_agent` method with:

```python
    def _child_system(self, subagent_type: SubagentType) -> str:
        frequent = [t.name for t in self._child_tools(subagent_type)]
        return render_prompt(
            _SUBAGENT_PROMPTS[subagent_type],
            {
                "date": datetime.date.today().isoformat(),
                "workspace": VIRTUAL_WORKSPACE,
                "uploads": VIRTUAL_UPLOADS,
                "outputs": VIRTUAL_OUTPUTS,
                "frequent_tool_names": frequent,
            },
            self.config_dir,
        )

    def _child_agent(self, subagent_type: SubagentType, system: str | None = None):
        system = system or self._child_system(subagent_type)
        return create_agent(
            model=self.model,
            tools=self._child_tools(subagent_type),
            system_prompt=system,
            middleware=self._child_middleware(),
            state_schema=ThreadState,
            context_schema=WorkspaceContext,
        )
```

In `run`, render the system once, build+apply the trace, and pass the rendered system to `_child_agent`. Replace the body from `agent = self._child_agent(subagent_type)` down to the `try:` with:

```python
        async with self._sem:
            system_text = self._child_system(subagent_type)
            agent = self._child_agent(subagent_type, system=system_text)
            child_id = f"{parent_thread_id}:sub:{uuid.uuid4().hex[:8]}"
            config = self._child_config(child_id)
            if self.base_trace is not None and self.observability is not None:
                from atom.observability import _apply_trace, build_subagent_trace

                _apply_trace(config, build_subagent_trace(
                    self.base_trace, parent_thread_id=parent_thread_id,
                    subagent_type=subagent_type, description=description,
                    rendered_prompt=system_text,
                    subagent_prompt_ref=_SUBAGENT_PROMPTS[subagent_type],
                    recursion_limit=self.recursion_limit, obs=self.observability,
                ))
            # Share the parent workspace: context thread_id == parent so tools find the same sandbox.
            context: WorkspaceContext = {
                "thread_id": parent_thread_id,
                "home": self.home,
                "workspace_mode": "new",
                "allow_bash": self.bash_enabled and subagent_type == "bash",
                "supports_vision": False,
                "context_window": self.context_window,
            }
            try:
                result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": [HumanMessage(content=prompt)]},
                        config=config,
                        context=context,
                    ),
                    timeout=self.timeout_seconds,
                )
```

(The `except` blocks and the rest of `run` are unchanged. Note `config` replaces the previous inline `config=self._child_config(child_id)`.)

- [ ] **Step 8: Pass the trace + observability into the runner from `_build_middlewares`**

In `src/atom/agent.py`:

Add a `trace` param to `_build_middlewares` (append to its signature after `library: LibraryIndex`):

```python
    library: LibraryIndex,
    trace: dict | None = None,
) -> list[AgentMiddleware]:
```

In the `SubagentRunner(...)` construction inside `_build_middlewares`, add two arguments (after `recursion_limit=profile.subagents.recursion_limit,`):

```python
        base_trace=trace,
        observability=cfg.observability,
```

Update the `_build_middlewares(...)` call in `build_lead_agent` to forward the (already-enriched) trace:

```python
    middleware = _build_middlewares(cfg, profile, prepared, provider, home, summarizer, library, trace)
```

- [ ] **Step 9: Run the runner tests + full suite**

Run: `.venv/bin/python -m pytest tests/test_subagent.py tests/test_observability.py -q`
Expected: PASS (existing sub-agent tests still pass — `base_trace`/`observability` default to None, so untraced runs are unchanged; the 2 new tests pass).

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all green).

- [ ] **Step 10: Commit**

```bash
git add src/atom/observability.py src/atom/subagent.py src/atom/agent.py tests/test_observability.py tests/test_subagent.py
git commit -m "feat(observability): tag + thread-group sub-agents into the parent lead trace"
```

---

## Task 5: Engine session_id integration test, docs, and verification

**Files:**
- Test: `tests/test_workflow_engine.py` (extend)
- Modify: `README.md`
- Create: `docs/superpowers/plans/2026-07-06-langsmith-spike-checklist.md` (manual spike record)

**Interfaces:**
- Consumes: everything from Tasks 1-4.

- [ ] **Step 1: Write the failing engine integration test**

Append to `tests/test_workflow_engine.py`:

```python
@pytest.mark.asyncio
async def test_task_trace_carries_session_id(base_config, atom_home, monkeypatch):
    """Each task's trace must carry its own thread id as session_id (one thread per lead agent)."""
    real = engine_mod.run_agent
    traces = []

    async def spy(prompt, **kwargs):
        traces.append(kwargs.get("trace"))
        return await real(prompt, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    scripts = {
        "poet_a": [AIMessage(content="a done")],
        "poet_b": [AIMessage(content="b done")],
    }
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared(list(scripts[td.id])),
    )
    engine.create_run(_draft_only(), {"topic": "sea"}, "runx", "2026-07-03T00:00:00")
    await engine.execute("runx")

    sids = {t["metadata"]["session_id"] for t in traces}
    assert sids == {"runx:s0:poet_a", "runx:s0:poet_b"}  # distinct thread per task
    assert all(t["metadata"]["agent_role"] == "lead" for t in traces)
    assert all("role:lead" in t["tags"] for t in traces)
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py::test_task_trace_carries_session_id -v`
Expected: PASS immediately (the engine already builds `build_lead_trace` with `session_id` from Task 2). If it does NOT pass, the failure pinpoints a regression in the Task 2 engine wiring — fix there. (This test formally locks the "one thread per lead agent" guarantee.)

- [ ] **Step 3: Add a README "Observability" section**

In `README.md`, add a section (place it near the workflow/`atom serve` documentation):

```markdown
## Observability (LangSmith)

Workflow runs can be traced to [LangSmith](https://smith.langchain.com). Enable it via the
`observability:` block in `config.yaml` or the standard `LANGSMITH_*` env vars (env wins):

```yaml
observability:
  enabled: true
  project: atom-workflows
```

Set `LANGSMITH_API_KEY` in `.env`. Tracing turns on only when a key is present.

Each workflow task is its own LangSmith **thread** (keyed by `session_id` = the task thread id).
Sub-agents are tagged `role:subagent` / `is_subagent` and grouped into their parent lead agent's
thread. Every run carries eval-ready metadata: `workflow` / `run_id` / `step_*` / `task_id`, the
`model` / `thinking` / `context_window` / `recursion_limit`, compaction settings, and a **prompt
fingerprint** (`system_prompt_ref` + `system_prompt_sha`, plus `summary_prompt_*` for the lead) so a
prompt version can be correlated with run outcomes. Filter in the UI by tags such as
`workflow:<name>`, `profile:<name>`, `model:<name>`, `role:lead` / `role:subagent`.
```

- [ ] **Step 4: Commit code + docs**

```bash
git add tests/test_workflow_engine.py README.md
git commit -m "test(observability): lock one-thread-per-lead-agent; document LangSmith setup"
```

- [ ] **Step 5: Manual verification spike (requires a LangSmith key)**

This step is manual — it needs a real LangSmith project and cannot run in CI. Perform it, then record the outcome in `docs/superpowers/plans/2026-07-06-langsmith-spike-checklist.md`.

1. In a scratch checkout, export `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true`, `LANGSMITH_PROJECT=atom-spike`.
2. Run a small workflow whose lead agent delegates at least one sub-agent (e.g. adapt `workflows/parallel-poems.yaml` to add a `delegate_task` step, or run any workflow whose prompt instructs a delegation).
3. In the LangSmith UI, open the project's **Threads** view and confirm:
   - [ ] Each task appears as a distinct thread named by its `session_id` (`<run>:s<step>:<task>`).
   - [ ] The lead run and its sub-agent run(s) appear in the **same** thread.
   - [ ] Sub-agent runs carry `is_subagent=true`, `role:subagent`, and a `system_prompt_sha` that differs from the lead's.
   - [ ] LangGraph's auto `metadata.thread_id` (the child id, for sub-agents) does **not** split sub-agents into their own threads.
4. If sub-agents split into separate threads, apply the documented fallback: in `build_subagent_trace` and `build_lead_trace`, also set `metadata["thread_id"] = session_id` (and re-run). Record which key LangSmith honored.

- [ ] **Step 6: Record the spike result and (if needed) apply the fallback**

Write `docs/superpowers/plans/2026-07-06-langsmith-spike-checklist.md` capturing the checkbox results from Step 5. If the fallback was applied, add a test to `tests/test_observability.py` asserting `metadata["thread_id"] == session_id` on both builders, then:

```bash
git add docs/superpowers/plans/2026-07-06-langsmith-spike-checklist.md src/atom/observability.py tests/test_observability.py
git commit -m "docs(observability): record LangSmith thread-grouping spike result"
```

- [ ] **Step 7: Final full-suite verification**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all green).

---

## Self-Review

**Spec coverage:**
- §4 module + three layers → Tasks 2 (identity + helpers + env), 3 (runtime/enrich), 4 (sub-agent). ✓
- §5 metadata/tag schema → `build_lead_trace` (Task 2), `enrich_lead_trace` (Task 3), `build_subagent_trace` (Task 4); every field present incl. prompt fingerprint. ✓
- §6 config + env + dep → Task 1. ✓
- §7 wiring/data flow → engine (Task 2), run_agent/build_lead_agent (Task 3), runner (Task 4). ✓
- §8 verification spike → Task 5 Steps 5-6, with documented `thread_id` fallback. ✓
- §9 testing → config (Task 1), module unit tests (Tasks 2-4), engine session_id (Task 5), CLI-untraced guards (Task 3 Step 7, Task 4 `test_subagent_no_base_trace_is_untraced`). ✓
- §10 migration (move file, update import) → Task 2 Steps 5-7. ✓

**Placeholder scan:** No TBD/TODO; every code and test step contains complete content. The one manual step (Task 5 Step 5) is explicitly a human verification with a concrete checklist, not a code placeholder. ✓

**Type consistency:** `session_id` is the thread key everywhere; `build_lead_trace`/`enrich_lead_trace`/`build_subagent_trace` signatures match their call sites in engine/agent/subagent; `_child_agent(subagent_type, system=None)` and `_child_system(subagent_type)` are used consistently; `SubagentRunner.base_trace`/`observability` fields match the constructor calls in `_build_middlewares` and the tests. ✓
