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


from atom.config.schema import LangfuseConfig


def test_observability_provider_defaults_none():
    cfg = AtomConfig()
    assert cfg.observability.provider is None            # unset -> legacy fallback
    assert isinstance(cfg.observability.langfuse, LangfuseConfig)
    assert cfg.observability.langfuse.host is None
    assert cfg.observability.langfuse.public_key is None
    assert cfg.observability.langfuse.secret_key is None
    assert cfg.observability.langfuse.sample_rate == 1.0


def test_observability_provider_langfuse_block():
    oc = ObservabilityConfig(
        provider="langfuse",
        langfuse={"host": "http://lf.local", "public_key": "pk",
                  "secret_key": "sk", "environment": "dev", "sample_rate": 0.5},
    )
    assert oc.provider == "langfuse"
    assert oc.langfuse.host == "http://lf.local"
    assert oc.langfuse.public_key == "pk" and oc.langfuse.secret_key == "sk"
    assert oc.langfuse.environment == "dev" and oc.langfuse.sample_rate == 0.5


def test_observability_provider_rejects_unknown():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ObservabilityConfig(provider="datadog")


def test_langfuse_sample_rate_is_bounded():
    """Out-of-range sample_rate must fail at config LOAD (clean ValidationError), not crash the
    LangFuse SDK constructor at provider-build time."""
    import pytest
    from pydantic import ValidationError
    from atom.config.schema import LangfuseConfig
    with pytest.raises(ValidationError):
        LangfuseConfig(sample_rate=1.5)
    with pytest.raises(ValidationError):
        LangfuseConfig(sample_rate=-0.1)
    assert LangfuseConfig(sample_rate=0.0).sample_rate == 0.0
    assert LangfuseConfig(sample_rate=1.0).sample_rate == 1.0
