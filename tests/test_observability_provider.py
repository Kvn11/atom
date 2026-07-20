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


def test_build_provider_none_when_unset_and_disabled(monkeypatch):
    # Resolution now honors env activation (env-only LANGSMITH_TRACING -> LangSmith), so pin the env
    # this asserts on rather than inheriting an ambient/leaked LANGSMITH_TRACING.
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
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


# --- review-fix behaviors ---------------------------------------------------

def test_build_provider_langfuse_non_import_error_degrades(monkeypatch, caplog):
    """A non-ImportError from the langfuse factory (bad sample_rate/host/auth) must degrade to
    NullProvider, never propagate out of build_provider into WorkflowEngine.__init__."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    def raising_factory(lf, public, secret):
        raise ValueError("Sample rate must be between 0.0 and 1.0")

    with caplog.at_level(logging.WARNING):
        p = build_provider(_cfg(provider="langfuse"), langfuse_factory=raising_factory)
    assert isinstance(p, NullProvider)
    assert "failed to initialize" in caplog.text


def test_langfuse_disables_env_langsmith_tracing(monkeypatch):
    """provider=langfuse must silence env-driven LangSmith so exactly one backend uploads."""
    import os
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    p = build_provider(_cfg(provider="langfuse"),
                       langfuse_factory=lambda lf, pub, sec: (_FakeLFClient(), _FakeHandler()))
    assert isinstance(p, LangFuseProvider)
    assert os.environ.get("LANGSMITH_TRACING") == "false"


def test_explicit_langsmith_activates_without_enabled(monkeypatch):
    """provider=langsmith activates on an API key alone (no legacy enabled=True required)."""
    import os
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    p = build_provider(_cfg(provider="langsmith"))   # enabled defaults False
    assert isinstance(p, LangSmithProvider) and p.is_active() is True
    assert os.environ.get("LANGSMITH_TRACING") == "true"


def test_legacy_env_only_langsmith_resolves_to_active_langsmith(monkeypatch):
    """provider unset + enabled False + env LANGSMITH_TRACING=true -> active LangSmithProvider,
    so the end-of-run flush is not silently dropped (was: NullProvider)."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    p = build_provider(_cfg())     # provider unset, enabled False
    assert isinstance(p, LangSmithProvider) and p.is_active() is True


def test_env_only_langsmith_maps_project(monkeypatch):
    """Env-activated tracing still maps observability.project -> LANGSMITH_PROJECT."""
    import os
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    build_provider(_cfg(project="myproj"))
    assert os.environ.get("LANGSMITH_PROJECT") == "myproj"


def test_default_langfuse_factory_builds_handler_with_update_trace():
    """The REAL CallbackHandler must be built with update_trace=True.

    The SDK default is update_trace=False, which writes the run-config metadata
    (agent_role / is_subagent / task_id / step_index) ONLY onto the root observation, never onto
    TRACE-level metadata. The pull-side exporter reads ``client.api.trace.get(id).metadata`` to
    pick lead traces and scope a task, so with the default those keys are absent at export time:
    export_task matches nothing and export_run miscounts leads against a real Langfuse backend.
    Constructing the client/handler here is offline-safe (lazy, no network).
    """
    from atom.config.schema import LangfuseConfig
    from atom.observability.provider import _default_langfuse_factory
    _client, handler = _default_langfuse_factory(LangfuseConfig(), "pk", "sk")
    assert handler.update_trace is True


def test_resolve_langfuse_keys_prefers_config_over_env(monkeypatch):
    from atom.observability.provider import resolve_langfuse_keys
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "env_pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "env_sk")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    obs = ObservabilityConfig(
        provider="langfuse",
        langfuse={"public_key": "cfg_pk", "secret_key": "cfg_sk", "host": "http://lf"},
    )
    assert resolve_langfuse_keys(obs) == ("cfg_pk", "cfg_sk", "http://lf")
    assert resolve_langfuse_keys(ObservabilityConfig()) == ("env_pk", "env_sk", None)


# --- truncating mask (task 2) -----------------------------------------------

from atom.observability.provider import _make_truncating_mask


def test_mask_truncates_big_string_leaf():
    mask = _make_truncating_mask(100, 2_000_000)
    out = mask(data={"input": "A" * 5000, "small": "ok"})
    assert len(out["input"]) < 5000
    assert "elided by atom size cap" in out["input"]
    assert out["small"] == "ok"


def test_mask_walks_nested_lists_and_dicts():
    mask = _make_truncating_mask(50, 2_000_000)
    out = mask(data={"messages": [{"text": "B" * 2000}]})
    assert "elided by atom size cap" in out["messages"][0]["text"]


def test_mask_outer_guard_replaces_giant_observation():
    mask = _make_truncating_mask(10_000_000, 500)   # per-string cap huge; per-observation cap tiny
    out = mask(data={"k": "C" * 5000})
    assert isinstance(out, str) and "observation payload elided" in out


def test_mask_never_raises_on_weird_data():
    mask = _make_truncating_mask(100, 2_000_000)

    class _Weird:
        def __str__(self):
            raise RuntimeError("nope")

    out = mask(data=_Weird())        # must not raise
    assert out is not None


def test_default_factory_wires_the_mask(monkeypatch):
    import langfuse
    import langfuse.langchain
    captured = {}

    class _FakeLF:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeCH:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(langfuse, "Langfuse", _FakeLF)
    monkeypatch.setattr(langfuse.langchain, "CallbackHandler", _FakeCH)

    from atom.config.schema import LangfuseConfig
    from atom.observability.provider import _default_langfuse_factory

    _default_langfuse_factory(LangfuseConfig(), "pk", "sk")
    assert "mask" in captured
    masked = captured["mask"](data="Z" * 500_000)
    assert len(masked) < 500_000       # the wired mask really truncates
