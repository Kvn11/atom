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
