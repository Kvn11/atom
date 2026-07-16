"""build_lead_agent threads obs_provider: enrich gate + sub-agent runner wiring."""
from __future__ import annotations

from atom.agent import build_lead_agent
from atom.config.schema import AgentProfile, AtomConfig, ObservabilityConfig
from atom.observability.provider import LangFuseProvider, NullProvider
from tests.conftest import make_prepared


class _Handler: ...
class _Client:
    def flush(self): ...


def _cfg(atom_home):
    # summary_prompt=None keeps enrich a pure metadata op (no summary-file IO); the lead system
    # prompt (@prompts/lead_system.md) still resolves from the shipped package prompts.
    return AtomConfig(
        home=str(atom_home), checkpointer={"backend": "memory"},
        agents={"default": AgentProfile(model="haiku", summary_prompt=None)},
    )


def test_enrich_runs_under_active_provider(atom_home):
    cfg = _cfg(atom_home)
    trace = {"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}}
    prov = LangFuseProvider(_Client(), _Handler())
    build_lead_agent(cfg, "default", prepared=make_prepared([]), trace=trace, obs_provider=prov)
    assert trace["metadata"]["model"] == "haiku"             # enrich stamped runtime fields
    assert any(t.startswith("model:") for t in trace["tags"])


def test_enrich_skipped_without_active_provider(atom_home, monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)   # no env-based langsmith activation
    cfg = _cfg(atom_home)
    trace = {"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}}
    build_lead_agent(cfg, "default", prepared=make_prepared([]), trace=trace, obs_provider=NullProvider())
    assert "model" not in trace["metadata"]                  # inactive -> no enrichment
