"""Observability module: helpers, env activation, and the lead-trace builder."""
from __future__ import annotations

import os

from atom.config.schema import AgentProfile, AtomConfig, ObservabilityConfig
from atom.observability import (
    apply_observability_env,
    build_lead_trace,
    build_subagent_trace,
    enrich_lead_trace,
    prompt_fingerprint,
    tracing_active,
)


def test_prompt_fingerprint_deterministic():
    a = prompt_fingerprint("hello world")
    assert a == prompt_fingerprint("hello world")
    assert len(a) == 12
    assert prompt_fingerprint("other") != a


def test_tracing_active(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert tracing_active() is False
    for value in ("true", "1", "TRUE"):
        monkeypatch.setenv("LANGSMITH_TRACING", value)
        assert tracing_active() is True
    for value in ("false", "0", ""):
        monkeypatch.setenv("LANGSMITH_TRACING", value)
        assert tracing_active() is False


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
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    cfg = AtomConfig(observability=ObservabilityConfig(enabled=True))
    apply_observability_env(cfg)
    assert "LANGSMITH_TRACING" not in os.environ  # no key -> safe no-op
    assert "LANGSMITH_PROJECT" not in os.environ  # tracing won't enable -> project must not be set


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
