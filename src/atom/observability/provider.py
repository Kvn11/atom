"""Observability provider strategy: one interface over LangSmith / LangFuse / none.

Backend differences collapse to three methods:
  is_active()            -> is tracing really on (gates enrichment work)
  decorate_run_config()  -> per-run: attach callbacks + session key (LangFuse); no-op otherwise
  flush()                -> end-of-run flush

build_provider(cfg) resolves cfg.observability into exactly one active provider (or NullProvider),
and logs a one-line activation notice. It must NEVER raise on misconfiguration — telemetry must not
break a run.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from atom.config.schema import AtomConfig
from atom.observability.trace import apply_observability_env, git_sha

logger = logging.getLogger(__name__)


class ObservabilityProvider:
    """Interface (and inert base). Concrete providers override all three methods."""

    name: str = "none"

    def is_active(self) -> bool:
        raise NotImplementedError

    def decorate_run_config(self, config: dict) -> dict:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError


class NullProvider(ObservabilityProvider):
    """No backend: everything is a no-op."""

    name = "none"

    def is_active(self) -> bool:
        return False

    def decorate_run_config(self, config: dict) -> dict:
        return config

    def flush(self) -> None:
        return None


class LangSmithProvider(ObservabilityProvider):
    """LangSmith: env-driven auto-attach. decorate is a no-op; flush drains the tracer queue."""

    name = "langsmith"

    def __init__(self, cfg: AtomConfig) -> None:
        self.status = apply_observability_env(cfg)   # maps config -> LANGSMITH_* env (idempotent)

    def is_active(self) -> bool:
        return self.status.active

    def decorate_run_config(self, config: dict) -> dict:
        return config                                 # LangChainTracer auto-attaches from env

    def flush(self) -> None:
        if not self.status.active:
            return
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()


def build_provider(cfg: AtomConfig, *, langfuse_factory: Any = None) -> ObservabilityProvider:
    """Resolve cfg.observability into an active provider (or NullProvider). Logs status; never raises.

    ``langfuse_factory`` is a test seam: a callable ``(langfuse_cfg, public_key, secret_key) ->
    (client, handler)``. Defaults to the real constructor (added in Task 3).
    """
    obs = cfg.observability
    provider = obs.provider
    if provider is None:                              # legacy fallback
        provider = "langsmith" if obs.enabled else "none"

    if provider == "langsmith":
        p = LangSmithProvider(cfg)
        if p.status.active:
            logger.info("observability: langsmith tracing active -> project %r", p.status.project)
        elif p.status.reason == "enabled-but-no-api-key":
            logger.warning(
                "observability: observability.enabled but LANGSMITH_API_KEY missing "
                "-- traces will NOT be uploaded"
            )
        return p

    if provider == "langfuse":
        return _build_langfuse_provider(obs, langfuse_factory)   # implemented in Task 3

    return NullProvider()


def _build_langfuse_provider(obs, langfuse_factory):  # replaced in Task 3
    logger.warning("observability: provider=langfuse not yet wired -- tracing disabled")
    return NullProvider()
