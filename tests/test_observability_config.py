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
