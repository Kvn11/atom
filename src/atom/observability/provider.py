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


class LangFuseProvider(ObservabilityProvider):
    """LangFuse: attach a shared CallbackHandler per run and stamp the run-level session id.

    The handler is stateless (session/tags come from run-config metadata), so one instance safely
    serves every concurrent task. Each atom task and each sub-agent is a separate LangChain chain
    root, so ``langfuse_session_id`` is stamped on every run config to group the whole run.
    """

    name = "langfuse"

    def __init__(self, client: Any, handler: Any) -> None:
        self._client = client
        self._handler = handler

    def is_active(self) -> bool:
        return True

    def decorate_run_config(self, config: dict) -> dict:
        callbacks = list(config.get("callbacks") or [])
        if self._handler not in callbacks:
            callbacks.append(self._handler)
        config["callbacks"] = callbacks
        metadata = config.setdefault("metadata", {})
        run_id = metadata.get("run_id")               # defensive: skip if absent (CLI path)
        if run_id is not None:
            metadata["langfuse_session_id"] = run_id
        return config

    def flush(self) -> None:
        self._client.flush()


def _default_langfuse_factory(lf: Any, public: str, secret: str) -> tuple[Any, Any]:
    """Construct the global Langfuse client + a CallbackHandler. Raises ImportError if uninstalled."""
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    client = Langfuse(
        public_key=public,
        secret_key=secret,
        host=lf.host or os.environ.get("LANGFUSE_HOST"),
        environment=lf.environment,
        release=lf.release or git_sha(),
        sample_rate=lf.sample_rate,
    )
    return client, CallbackHandler()                  # binds to the global client by public_key


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


def _build_langfuse_provider(obs: Any, langfuse_factory: Any) -> ObservabilityProvider:
    lf = obs.langfuse
    public = lf.public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = lf.secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public and secret):
        logger.warning(
            "observability: provider=langfuse but LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY missing "
            "-- traces will NOT be uploaded"
        )
        return NullProvider()
    factory = langfuse_factory or _default_langfuse_factory
    try:
        client, handler = factory(lf, public, secret)
    except ImportError:
        logger.warning(
            "observability: provider=langfuse but the 'langfuse' package is not installed "
            "-- run `pip install 'langfuse>=3,<4'`"
        )
        return NullProvider()
    logger.info("observability: langfuse tracing active -> host %r",
                lf.host or os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com")
    return LangFuseProvider(client, handler)
