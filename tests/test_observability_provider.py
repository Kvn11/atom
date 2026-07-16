"""Observability provider strategy: factory resolution + LangSmith/Null behavior."""
from __future__ import annotations

import logging

from atom.config.schema import AtomConfig, ObservabilityConfig
from atom.observability.provider import (
    LangFuseProvider,
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


class _FakeHandler:
    pass


class _FakeLFClient:
    def __init__(self):
        self.flushed = 0

    def flush(self):
        self.flushed += 1


def test_langfuse_decorate_attaches_handler_and_session():
    handler = _FakeHandler()
    p = LangFuseProvider(_FakeLFClient(), handler)
    assert p.name == "langfuse" and p.is_active() is True
    cfg = {"configurable": {"thread_id": "r1:s0:t0"}, "metadata": {"run_id": "r1"}}
    out = p.decorate_run_config(cfg)
    assert out["callbacks"] == [handler]
    assert out["metadata"]["langfuse_session_id"] == "r1"     # session = whole run


def test_langfuse_decorate_preserves_existing_callbacks_no_dupes():
    handler = _FakeHandler()
    p = LangFuseProvider(_FakeLFClient(), handler)
    other = _FakeHandler()
    cfg = {"callbacks": [other], "metadata": {"run_id": "r1"}}
    p.decorate_run_config(cfg)
    assert cfg["callbacks"] == [other, handler]
    p.decorate_run_config(cfg)                                 # idempotent
    assert cfg["callbacks"] == [other, handler]


def test_langfuse_decorate_subagent_session_is_run_not_thread():
    # A sub-agent config: its own thread_id, but run_id metadata inherited from the lead.
    handler = _FakeHandler()
    p = LangFuseProvider(_FakeLFClient(), handler)
    cfg = {"configurable": {"thread_id": "r1:s0:t0:sub:ab12"},
           "metadata": {"run_id": "r1", "session_id": "r1:s0:t0", "is_subagent": True}}
    p.decorate_run_config(cfg)
    assert cfg["metadata"]["langfuse_session_id"] == "r1"      # groups into the run, not the parent thread


def test_langfuse_decorate_no_run_id_skips_session():
    p = LangFuseProvider(_FakeLFClient(), _FakeHandler())
    cfg = {"metadata": {}}
    p.decorate_run_config(cfg)
    assert "langfuse_session_id" not in cfg["metadata"]        # defensive: no KeyError


def test_langfuse_flush_delegates_to_client():
    client = _FakeLFClient()
    LangFuseProvider(client, _FakeHandler()).flush()
    assert client.flushed == 1


def test_build_provider_langfuse_uses_injected_factory(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    seen = {}
    client, handler = _FakeLFClient(), _FakeHandler()

    def fake_factory(lf, public, secret):
        seen["public"], seen["secret"] = public, secret
        return client, handler

    p = build_provider(_cfg(provider="langfuse"), langfuse_factory=fake_factory)
    assert isinstance(p, LangFuseProvider) and p.is_active() is True
    assert seen == {"public": "pk", "secret": "sk"}


def test_build_provider_langfuse_missing_keys_degrades(monkeypatch, caplog):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    import logging
    with caplog.at_level(logging.WARNING):
        p = build_provider(_cfg(provider="langfuse"))
    assert isinstance(p, NullProvider)
    assert "LANGFUSE" in caplog.text


def test_build_provider_langfuse_import_error_degrades(monkeypatch, caplog):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    def raising_factory(lf, public, secret):
        raise ImportError("no module named 'langfuse'")

    with caplog.at_level(logging.WARNING):
        p = build_provider(_cfg(provider="langfuse"), langfuse_factory=raising_factory)
    assert isinstance(p, NullProvider)
    assert "langfuse' package is not installed" in caplog.text
