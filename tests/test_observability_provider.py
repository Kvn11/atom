"""Observability provider strategy: factory resolution + LangSmith/Null behavior."""
from __future__ import annotations

import logging

from atom.config.schema import AtomConfig, ObservabilityConfig
from atom.observability.provider import (
    LangSmithProvider,
    NullProvider,
    ObservabilityProvider,
    build_provider,
)


def _cfg(**obs) -> AtomConfig:
    return AtomConfig(observability=ObservabilityConfig(**obs))


def test_null_provider_is_inert():
    p = NullProvider()
    assert p.name == "none" and p.is_active() is False
    cfg = {"metadata": {"run_id": "r1"}}
    assert p.decorate_run_config(cfg) is cfg          # unchanged
    assert "callbacks" not in cfg
    p.flush()                                          # no raise


def test_build_provider_none_when_unset_and_disabled():
    assert isinstance(build_provider(_cfg()), NullProvider)


def test_build_provider_explicit_none():
    assert isinstance(build_provider(_cfg(provider="none", enabled=True)), NullProvider)


def test_build_provider_legacy_enabled_is_langsmith(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    p = build_provider(_cfg(enabled=True))             # provider unset + enabled -> langsmith
    assert isinstance(p, LangSmithProvider)
    assert p.is_active() is False                       # no API key -> inactive but present


def test_build_provider_explicit_langsmith_active(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    p = build_provider(_cfg(provider="langsmith", enabled=True, project="proj"))
    assert isinstance(p, LangSmithProvider) and p.is_active() is True


def test_langsmith_decorate_is_noop(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    p = build_provider(_cfg(provider="langsmith", enabled=True))
    cfg = {"configurable": {"thread_id": "t"}}
    assert p.decorate_run_config(cfg) == {"configurable": {"thread_id": "t"}}  # env-driven, no callbacks


def test_build_provider_legacy_missing_key_warns(monkeypatch, caplog):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    with caplog.at_level(logging.WARNING):
        build_provider(_cfg(enabled=True))
    assert "LANGSMITH_API_KEY missing" in caplog.text


def test_langsmith_flush_calls_wait_for_all_tracers(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    called = {"n": 0}
    import langchain_core.tracers.langchain as lct
    monkeypatch.setattr(lct, "wait_for_all_tracers", lambda: called.__setitem__("n", called["n"] + 1))
    p = build_provider(_cfg(provider="langsmith", enabled=True))
    p.flush()
    assert called["n"] == 1
