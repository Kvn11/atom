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

from atom.config.schema import AgentProfile, AtomConfig, ObservabilityConfig


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
