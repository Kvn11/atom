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
