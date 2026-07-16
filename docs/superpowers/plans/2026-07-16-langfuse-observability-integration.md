# LangFuse Observability Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add LangFuse as a second, config-selected observability backend alongside LangSmith, with the whole workflow run grouped into one LangFuse session, and full pull-side export parity.

**Architecture:** Introduce a small `ObservabilityProvider` strategy object (`Null` / `LangSmith` / `LangFuse`) built once by a factory from `cfg.observability.provider`. Backend differences collapse to three methods — `is_active()`, `decorate_run_config()`, `flush()`. The provider is threaded from the workflow engine into the lead runtime and the sub-agent runner, where `decorate_run_config()` attaches the LangFuse `CallbackHandler` (via `config["callbacks"]`) and stamps `langfuse_session_id = run_id` on every root run. Pull-side, a LangFuse exporter mirrors `export.py` under a shared envelope, dispatched by provider.

**Tech Stack:** Python 3, Pydantic v2, LangChain/LangGraph v1, `langsmith` (existing), `langfuse>=3,<4` (new), pytest (asyncio auto mode), Typer CLI, FastAPI.

## Global Constraints

- **Dependency floors:** `langfuse>=3,<4` (new), keep `langsmith>=0.9,<1`, `langchain>=1.0,<2`, `pydantic>=2.7`. Pin the resolved `langfuse` version in `requirements.lock.txt`.
- **Naming — avoid the collision:** `agent.build_lead_agent` and `_build_middlewares` already use a parameter named `provider` for the **sandbox** `LocalSandboxProvider`. The observability provider MUST be named `obs_provider` everywhere it is threaded, never `provider`.
- **Telemetry never crashes a run:** every provider construction/flush path must degrade to a no-op (log a warning, return `NullProvider`, swallow flush errors) rather than raise into a run.
- **Provider values:** `cfg.observability.provider ∈ {"langsmith", "langfuse", "none", None}`. `None` (unset) means legacy fallback: LangSmith if `observability.enabled` else none.
- **Session semantics:** LangFuse session = `run_id` (whole run). Do not touch the existing metadata `session_id` (LangSmith thread key).
- **Coverage:** workflow runs only. The interactive CLI path (`run_agent` with `obs_provider=None`) stays untraced — `obs_provider` defaults to `None` and every use is guarded by `if obs_provider is not None`.
- **Preserve the exact LangSmith "no key" warning string** (an existing test asserts it): `observability: observability.enabled but LANGSMITH_API_KEY missing -- traces will NOT be uploaded`.
- **Process:** strict TDD (failing test first), one logical change per commit, run the relevant test file after every change. Every commit message ends with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Run tests with:** `python -m pytest` from repo root (`pythonpath=["src"]`, `asyncio_mode=auto` are already configured in `pyproject.toml`).

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `src/atom/config/schema.py` | Add `LangfuseConfig`; add `provider` + nested `langfuse` to `ObservabilityConfig` | 1 |
| `.env.example`, `config.yaml` | Document/illustrate LangFuse keys + `provider` | 1 |
| `src/atom/observability/provider.py` (new) | Provider protocol, `NullProvider`, `LangSmithProvider`, `LangFuseProvider`, `build_provider` | 2, 3 |
| `src/atom/observability/__init__.py` | Re-export the new provider names | 2, 3 |
| `pyproject.toml`, `requirements.txt`, `requirements.lock.txt` | Add `langfuse>=3,<4` | 3 |
| `src/atom/runtime.py` | `build_run_config` + `run_agent` accept/forward `obs_provider`; decorate lead config | 4 |
| `src/atom/subagent.py` | `SubagentRunner` stores `obs_provider`; `run()` decorates child config | 5 |
| `src/atom/agent.py` | Enrich gate uses `obs_provider.is_active()`; thread `obs_provider` into `build_lead_agent` + `SubagentRunner` | 6 |
| `src/atom/workflow/engine.py` | Build provider once; pass to `run_agent`; flush via provider | 7 |
| `src/atom/observability/export.py` | Generalize `build_envelope` (provider/sdk_version); shared helpers | 8 |
| `src/atom/observability/langfuse_export.py` (new) | LangFuse pull-side exporter | 9 |
| `src/atom/cli.py`, `src/atom/api/app.py` | Export dispatch by provider + LangFuse credential guard | 10 |

**Test files:** `tests/test_observability_config.py` (extend), `tests/test_observability_provider.py` (new), `tests/test_runtime_trace.py` (extend), `tests/test_subagent.py` (extend), `tests/test_workflow_engine.py` (extend), `tests/test_export.py` (extend), `tests/test_langfuse_export.py` (new).

---

### Task 1: Config schema — `provider` discriminator + `LangfuseConfig`

**Files:**
- Modify: `src/atom/config/schema.py:116-122` (`ObservabilityConfig`)
- Modify: `.env.example`, `config.yaml`
- Test: `tests/test_observability_config.py`

**Interfaces:**
- Produces: `ObservabilityConfig.provider: Optional[Literal["langsmith","langfuse","none"]] = None`; `ObservabilityConfig.langfuse: LangfuseConfig`; `LangfuseConfig(host, public_key, secret_key, environment, release, sample_rate)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_observability_config.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_observability_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'LangfuseConfig'` (and attribute errors).

- [ ] **Step 3: Implement the schema change** — in `src/atom/config/schema.py`, ensure `Literal` is imported (the file already imports from `typing`; add `Literal` if missing: `from typing import Literal, Optional, Union`), then replace the `ObservabilityConfig` class (lines 116-122) with:

```python
class LangfuseConfig(_Base):
    # LangFuse tracing backend. Keys fall back to LANGFUSE_* env vars when unset.
    host: Optional[str] = None            # default https://cloud.langfuse.com (SDK default)
    public_key: Optional[str] = None      # or LANGFUSE_PUBLIC_KEY
    secret_key: Optional[str] = None      # or LANGFUSE_SECRET_KEY
    environment: Optional[str] = None     # optional LangFuse "environment" tag
    release: Optional[str] = None         # optional; falls back to captured git sha
    sample_rate: float = 1.0              # 0.0..1.0


class ObservabilityConfig(_Base):
    # Tracing for workflow runs. `provider` selects the backend; None -> legacy fallback
    # (LangSmith if `enabled`, else off). Exactly one backend is active per run.
    provider: Optional[Literal["langsmith", "langfuse", "none"]] = None
    enabled: bool = False               # (legacy LangSmith) -> LANGSMITH_TRACING when key present
    project: Optional[str] = None       # (LangSmith) -> LANGSMITH_PROJECT
    default_tags: list[str] = Field(default_factory=list)   # tags added to every workflow run
    include_prompt_fingerprint: bool = True  # add prompt ref + content hash to metadata (both backends)
    capture_git_sha: bool = True        # best-effort atom_git_sha in metadata (both backends)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_observability_config.py -q`
Expected: PASS (all tests, including the two pre-existing ones).

- [ ] **Step 5: Update docs/examples** — in `.env.example`, below the `LANGSMITH_*` block, add:

```bash
# LangFuse (alternative to LangSmith; set observability.provider: langfuse)
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

In `config.yaml`, under the existing `observability:` block, add a commented example (do not enable it):

```yaml
  # provider: langfuse            # langsmith | langfuse | none (default: none)
  # langfuse:
  #   host: ${LANGFUSE_HOST}
  #   public_key: ${LANGFUSE_PUBLIC_KEY}
  #   secret_key: ${LANGFUSE_SECRET_KEY}
```

- [ ] **Step 6: Commit**

```bash
git add src/atom/config/schema.py tests/test_observability_config.py .env.example config.yaml
git commit -m "$(cat <<'EOF'
feat(config): add observability.provider discriminator + LangfuseConfig

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Provider core — protocol, Null, LangSmith, factory

**Files:**
- Create: `src/atom/observability/provider.py`
- Modify: `src/atom/observability/__init__.py`
- Test: `tests/test_observability_provider.py` (new)

**Interfaces:**
- Consumes: `apply_observability_env`, `git_sha`, `ObservabilityStatus` from `atom.observability.trace`; `AtomConfig` from `atom.config.schema`.
- Produces:
  - `class ObservabilityProvider` with `name: str`, `is_active() -> bool`, `decorate_run_config(config: dict) -> dict`, `flush() -> None`.
  - `class NullProvider(ObservabilityProvider)`.
  - `class LangSmithProvider(ObservabilityProvider)` — `__init__(cfg)`.
  - `build_provider(cfg, *, langfuse_factory=None) -> ObservabilityProvider`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_observability_provider.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_observability_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.observability.provider'`.

- [ ] **Step 3: Implement `provider.py`** — create `src/atom/observability/provider.py`:

```python
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
```

Note: `_build_langfuse_provider` is referenced but not yet defined — Task 3 adds it. To keep this task's tree importable and green (no `langfuse` path exercised yet), add a temporary stub at the bottom of the file that Task 3 replaces:

```python
def _build_langfuse_provider(obs, langfuse_factory):  # replaced in Task 3
    logger.warning("observability: provider=langfuse not yet wired -- tracing disabled")
    return NullProvider()
```

- [ ] **Step 4: Export the names** — in `src/atom/observability/__init__.py`, add to the imports/`__all__`:

```python
from atom.observability.provider import (
    ObservabilityProvider, NullProvider, LangSmithProvider, build_provider,
)
```

(Extend the existing `__all__` list with `"ObservabilityProvider"`, `"NullProvider"`, `"LangSmithProvider"`, `"build_provider"`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_observability_provider.py tests/test_observability.py -q`
Expected: PASS (new provider tests + existing observability tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/atom/observability/provider.py src/atom/observability/__init__.py tests/test_observability_provider.py
git commit -m "$(cat <<'EOF'
feat(observability): ObservabilityProvider strategy + Null/LangSmith + factory

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: LangFuseProvider + dependency

**Files:**
- Modify: `pyproject.toml`, `requirements.txt`, `requirements.lock.txt`
- Modify: `src/atom/observability/provider.py`
- Modify: `src/atom/observability/__init__.py`
- Test: `tests/test_observability_provider.py`

**Interfaces:**
- Produces:
  - `class LangFuseProvider(ObservabilityProvider)` — `__init__(client, handler)`; `is_active() -> True`; `decorate_run_config` appends `handler` to `config["callbacks"]` and sets `config["metadata"]["langfuse_session_id"] = metadata["run_id"]` (skipped when `run_id` absent); `flush()` calls `client.flush()`.
  - `_build_langfuse_provider(obs, langfuse_factory)` and `_default_langfuse_factory(lf, public, secret) -> (client, handler)`.

- [ ] **Step 1: Add the dependency and install** — in `pyproject.toml` "Observability" section add `"langfuse>=3,<4",`; add the same line to `requirements.txt`. Then:

Run: `python -m pip install 'langfuse>=3,<4'`
Then capture the resolved version and pin it in `requirements.lock.txt` (find the installed version):

Run: `python -c "import langfuse; print(langfuse.__version__)"`
Add `langfuse==<printed_version>` to `requirements.lock.txt` (alongside `langsmith==...`).

- [ ] **Step 2: Write the failing tests** — append to `tests/test_observability_provider.py`:

```python
from atom.observability.provider import LangFuseProvider


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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_observability_provider.py -q`
Expected: FAIL — `ImportError: cannot import name 'LangFuseProvider'`.

- [ ] **Step 4: Implement** — in `src/atom/observability/provider.py`, add the `LangFuseProvider` class (after `LangSmithProvider`) and replace the temporary `_build_langfuse_provider` stub with the real factory + default constructor:

```python
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
```

Delete the temporary stub `_build_langfuse_provider` from Task 2.

- [ ] **Step 5: Export the name** — in `src/atom/observability/__init__.py`, add `LangFuseProvider` to the provider import and `__all__`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_observability_provider.py -q`
Expected: PASS (all provider tests).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt requirements.lock.txt src/atom/observability/provider.py src/atom/observability/__init__.py tests/test_observability_provider.py
git commit -m "$(cat <<'EOF'
feat(observability): LangFuseProvider (callbacks + run-level session) + langfuse dep

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Thread `obs_provider` into the lead runtime

**Files:**
- Modify: `src/atom/runtime.py:63-72` (`build_run_config`), `src/atom/runtime.py:75-126` (`run_agent` signature + `build_run_config` call)
- Test: `tests/test_runtime_trace.py`

**Interfaces:**
- Consumes: `ObservabilityProvider` (from `atom.observability`).
- Produces: `build_run_config(thread_id, recursion_limit, trace=None, obs_provider=None) -> dict`; `run_agent(..., obs_provider: ObservabilityProvider | None = None)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_runtime_trace.py`:

```python
from atom.observability.provider import LangFuseProvider


class _FakeHandler:
    pass


class _FakeLFClient:
    def flush(self):
        pass


def test_build_run_config_decorates_with_provider():
    from atom.runtime import build_run_config
    handler = _FakeHandler()
    prov = LangFuseProvider(_FakeLFClient(), handler)
    cfg = build_run_config("r1:s0:t0", 100, {"metadata": {"run_id": "r1"}}, prov)
    assert cfg["callbacks"] == [handler]
    assert cfg["metadata"]["langfuse_session_id"] == "r1"
    assert cfg["configurable"]["thread_id"] == "r1:s0:t0"


def test_build_run_config_no_provider_is_plain():
    from atom.runtime import build_run_config
    cfg = build_run_config("t", 100, {"metadata": {"run_id": "r1"}})
    assert "callbacks" not in cfg


@pytest.mark.asyncio
async def test_run_agent_accepts_obs_provider(base_config):
    prepared = make_prepared([AIMessage(content="hello")])
    prov = LangFuseProvider(_FakeLFClient(), _FakeHandler())
    result = await run_agent(
        "hi", config=base_config, prepared=prepared,
        trace={"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}},
        obs_provider=prov,
    )
    assert result.final_text == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_runtime_trace.py -q`
Expected: FAIL — `TypeError: build_run_config() takes 2 positional arguments...` / `run_agent() got an unexpected keyword argument 'obs_provider'`.

- [ ] **Step 3: Implement** — in `src/atom/runtime.py`, replace `build_run_config` (lines 63-72):

```python
def build_run_config(
    thread_id: str, recursion_limit: int, trace: dict | None = None, obs_provider=None,
) -> dict:
    """Assemble the LangGraph invoke config: thread id + recursion_limit (+ optional trace).

    When an observability provider is supplied, it decorates the config (LangFuse attaches its
    CallbackHandler and stamps the run-level session id; LangSmith/none are no-ops).

    ``recursion_limit`` counts super-steps, not agent turns. atom's middleware chain spends
    ~11 super-steps per turn, so this must be well above the intended turn count (see
    ``AgentProfile.recursion_limit``).
    """
    config = _apply_trace(
        {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}, trace
    )
    if obs_provider is not None:
        obs_provider.decorate_run_config(config)
    return config
```

Add the `obs_provider` parameter to `run_agent` (in the keyword-only block near line 91, alongside `on_event`):

```python
    on_event: "Callable[[dict], Awaitable[None]] | None" = None,
    obs_provider=None,
) -> RunResult:
```

And update the `build_run_config` call (line 126):

```python
        run_config = build_run_config(thread_id, prof.recursion_limit, trace, obs_provider)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_runtime_trace.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/runtime.py tests/test_runtime_trace.py
git commit -m "$(cat <<'EOF'
feat(runtime): thread obs_provider through run_agent/build_run_config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Decorate sub-agent configs via `obs_provider`

**Files:**
- Modify: `src/atom/subagent.py:60-64` (dataclass fields), `src/atom/subagent.py:161-179` (`run()`)
- Test: `tests/test_subagent.py`

**Interfaces:**
- Consumes: `ObservabilityProvider` (duck-typed via `obs_provider` field).
- Produces: `SubagentRunner.obs_provider: Any = None`; `run()` calls `self.obs_provider.decorate_run_config(config)` after the trace merge.

- [ ] **Step 1: Write the failing test** — append to `tests/test_subagent.py` (match the file's existing import style; `SubagentRunner` is imported from `atom.subagent`):

```python
def test_child_config_decorated_with_run_level_session():
    """A sub-agent's config gets the LangFuse handler + langfuse_session_id = run_id."""
    from atom.subagent import SubagentRunner
    from atom.observability.provider import LangFuseProvider

    class _Handler: ...
    class _Client:
        def flush(self): ...

    handler = _Handler()
    prov = LangFuseProvider(_Client(), handler)
    # base_trace supplies run_id in metadata (as build_lead_trace would).
    base_trace = {"run_name": "wf/s/t", "tags": ["atom-workflow"],
                  "metadata": {"run_id": "r1", "session_id": "r1:s0:t0",
                               "workflow": "wf", "step_title": "s", "task_id": "t"}}
    runner = SubagentRunner(
        model=None, home="/tmp", context_window=1000, bash_enabled=False,
        base_trace=base_trace, observability=None, obs_provider=prov,
    )
    config = runner._child_config("r1:s0:t0:sub:ab12")
    # emulate run()'s decoration order: merge subagent trace (if any) THEN decorate
    from atom.observability import build_subagent_trace, _apply_trace
    from atom.config.schema import ObservabilityConfig
    _apply_trace(config, build_subagent_trace(
        base_trace, parent_thread_id="r1:s0:t0", subagent_type="bash",
        description="d", rendered_prompt="p", subagent_prompt_ref="ref",
        recursion_limit=300, obs=ObservabilityConfig()))
    prov.decorate_run_config(config)
    assert handler in config["callbacks"]
    assert config["metadata"]["langfuse_session_id"] == "r1"      # run, not parent thread
    assert config["metadata"]["atom_subagent"] is True            # marker preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent.py::test_child_config_decorated_with_run_level_session -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'obs_provider'`.

- [ ] **Step 3: Implement** — in `src/atom/subagent.py`, add the field to the dataclass (next to `base_trace`/`observability`, around line 62-63):

```python
    base_trace: dict | None = None       # enriched lead trace; None -> sub-agent runs untraced
    observability: Any = None            # ObservabilityConfig | None
    obs_provider: Any = None             # ObservabilityProvider | None (LangFuse callbacks + session)
```

In `run()`, after the existing `_apply_trace(config, build_subagent_trace(...))` block (immediately after line 179, before the `context` dict), add:

```python
            if self.obs_provider is not None:
                # Attach LangFuse callbacks + stamp langfuse_session_id = run_id so this sub-agent
                # joins the whole run's session. Runs after the trace merge, so run_id is present.
                self.obs_provider.decorate_run_config(config)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent.py::test_child_config_decorated_with_run_level_session -q`
Expected: PASS.

- [ ] **Step 5: Run the full sub-agent suite (no regressions)**

Run: `python -m pytest tests/test_subagent.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/atom/subagent.py tests/test_subagent.py
git commit -m "$(cat <<'EOF'
feat(subagent): decorate child run config via obs_provider (LangFuse session)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Generalize the enrich gate + thread `obs_provider` into `build_lead_agent`

**Files:**
- Modify: `src/atom/agent.py:88-101` (`build_lead_agent` signature), `src/atom/agent.py:164-174` (enrich gate), `src/atom/agent.py:249-268` (`SubagentRunner` construction)
- Modify: `src/atom/runtime.py:121-125` (`build_lead_agent` call)
- Test: `tests/test_agent_trace.py` (new, focused)

**Interfaces:**
- Consumes: `ObservabilityProvider`.
- Produces: `build_lead_agent(..., obs_provider=None)` — enrich runs when `trace is not None and (obs_provider.is_active() or tracing_active())`; passes `obs_provider` into `SubagentRunner`.

- [ ] **Step 1: Write the failing test** — create `tests/test_agent_trace.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_trace.py -q`
Expected: FAIL — `build_lead_agent() got an unexpected keyword argument 'obs_provider'`.

- [ ] **Step 3: Implement** — in `src/atom/agent.py`:

Add `obs_provider=None` to the `build_lead_agent` signature (in the keyword block near line 99-100):

```python
    trace: dict | None = None,
    notes: dict | None = None,
    obs_provider=None,
):
```

Replace the enrich gate (lines 164-174) with:

```python
    from atom.observability import enrich_lead_trace, tracing_active

    obs_active = obs_provider is not None and obs_provider.is_active()
    mw_trace = None
    if trace is not None and (obs_active or tracing_active()):
        enrich_lead_trace(
            trace, cfg=cfg, profile=profile, profile_name=profile_name,
            system_prompt=system_prompt, context_window=prepared.context_window,
            override_model=override_model, override_thinking=override_thinking,
            override_system_prompt=override_system_prompt,
        )
        mw_trace = trace
```

Add `obs_provider=obs_provider` to the `SubagentRunner(...)` construction (after line 263 `observability=cfg.observability,`):

```python
        base_trace=trace,
        observability=cfg.observability,
        obs_provider=obs_provider,
```

In `src/atom/runtime.py`, forward `obs_provider` into the `build_lead_agent` call (lines 121-125):

```python
        agent = build_lead_agent(
            cfg, profile_name, prepared=prepared, checkpointer=cp,
            override_model=override_model, override_thinking=override_thinking,
            override_system_prompt=override_system_prompt, trace=trace, notes=notes,
            obs_provider=obs_provider,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_trace.py -q`
Expected: PASS.

- [ ] **Step 5: Regression sweep on the touched modules**

Run: `python -m pytest tests/test_runtime_trace.py tests/test_subagent.py tests/test_observability.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/atom/agent.py src/atom/runtime.py tests/test_agent_trace.py
git commit -m "$(cat <<'EOF'
feat(agent): gate enrich on obs_provider.is_active(); pass provider to sub-agents

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Wire the provider into the workflow engine + flush dispatch

**Files:**
- Modify: `src/atom/workflow/engine.py:22` (imports), `:84-92` (`__init__`), `:364-372` (flush in `finally`), `:412-419` (`run_agent` call)
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: `build_provider` from `atom.observability`.
- Produces: `WorkflowEngine.obs_provider: ObservabilityProvider`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_workflow_engine.py` (it already imports `engine_mod`, `WorkflowEngine`, `AIMessage`, `make_prepared`, and defines the `_one_task_wf()` helper used by the existing flush tests):

```python
def test_engine_builds_langfuse_provider(base_config, monkeypatch):
    from atom.config.schema import ObservabilityConfig
    from atom.observability.provider import LangFuseProvider

    class _Handler: ...
    class _Client:
        def flush(self): ...

    monkeypatch.setattr(engine_mod, "build_provider",
                        lambda cfg: LangFuseProvider(_Client(), _Handler()))
    cfg = base_config.model_copy(update={"observability": ObservabilityConfig(provider="langfuse")})
    engine = WorkflowEngine(cfg)
    assert isinstance(engine.obs_provider, LangFuseProvider)


@pytest.mark.asyncio
async def test_execute_flushes_via_provider(base_config, monkeypatch):
    calls = []
    from atom.observability.provider import ObservabilityProvider

    class _Rec(ObservabilityProvider):
        name = "rec"
        def is_active(self): return True
        def decorate_run_config(self, config): return config
        def flush(self): calls.append("flush")

    monkeypatch.setattr(engine_mod, "build_provider", lambda cfg: _Rec())
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared([AIMessage(content="done")]),
    )
    engine.create_run(_one_task_wf(), {"topic": "sea"}, "runF", "2026-07-09T00:00:00")
    await engine.execute("runF")
    assert calls == ["flush"]  # engine flushes the provider exactly once
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workflow_engine.py -k "provider or flush" -q`
Expected: FAIL — `AttributeError: 'WorkflowEngine' object has no attribute 'obs_provider'`.

- [ ] **Step 3: Implement** — in `src/atom/workflow/engine.py`:

Update the observability import (line 22) from:
```python
from atom.observability import apply_observability_env, build_lead_trace, tracing_active
```
to:
```python
from atom.observability import build_lead_trace, build_provider
```

Also remove the now-unused tracer import (engine.py:15) `from langchain_core.tracers.langchain import wait_for_all_tracers` — the engine no longer calls it directly (LangSmithProvider.flush does).

Replace the `__init__` observability block (lines 84-92) with:

```python
        # Build the observability provider once, before any run (logs its own status).
        self.obs_provider = build_provider(cfg)
```

Replace the flush block in `execute()`'s `finally` (lines 366-372) with:

```python
            # Flush the active backend's trace queue before the process can exit, so the run's
            # final batch is uploaded and downloadable. No-op for NullProvider; a flush failure
            # must never mask a propagating exception.
            try:
                self.obs_provider.flush()
            except Exception:  # noqa: BLE001
                pass
```

Add `obs_provider=self.obs_provider` to the `run_agent(...)` call (lines 412-419):

```python
            coro = run_agent(
                prompt, config=self._task_cfg, profile=self.profile,
                override_model=td.model, override_thinking=td.thinking,
                workspace=manifest.workspace_path, uploads=manifest.uploads_path,
                thread_id=ts.thread_id, trace=trace, prepared=prepared,
                notes=notes.as_prompt_ctx() if notes else None,
                on_event=(emitter.emit if emitter else None),
                obs_provider=self.obs_provider,
            )
```

- [ ] **Step 4: Update the existing flush tests** — the three existing flush tests monkeypatch `engine_mod.tracing_active` / `engine_mod.wait_for_all_tracers`, which the engine no longer references. Do the following in `tests/test_workflow_engine.py`:

  1. **Delete** `test_execute_flushes_tracers_when_active` (lines ~425-435) and `test_execute_skips_flush_when_inactive` (lines ~438-449) — superseded by `test_execute_flushes_via_provider` (Step 1) and the provider unit tests (`LangSmithProvider.flush` no-ops when inactive; `NullProvider.flush` no-ops).
  2. **Rewrite** `test_execute_flush_failure_does_not_break_run` (lines ~452-465) to drive the provider seam:

```python
@pytest.mark.asyncio
async def test_execute_flush_failure_does_not_break_run(base_config, monkeypatch):
    """A raising provider.flush() must never mask a propagating exception or break the run."""
    from atom.observability.provider import ObservabilityProvider

    class _Boom(ObservabilityProvider):
        name = "boom"
        def is_active(self): return True
        def decorate_run_config(self, config): return config
        def flush(self): raise RuntimeError("flush exploded")

    monkeypatch.setattr(engine_mod, "build_provider", lambda cfg: _Boom())
    engine = WorkflowEngine(
        base_config,
        prepared_provider=lambda td, sd, wf: make_prepared([AIMessage(content="done")]),
    )
    engine.create_run(_one_task_wf(), {"topic": "sea"}, "runH", "2026-07-09T00:00:00")
    manifest = await engine.execute("runH")   # must NOT raise despite flush blowing up
    assert manifest.status == "complete"
```

  3. **Leave** `test_engine_warns_when_enabled_but_no_api_key` (lines ~31-41) unchanged — `build_provider` logs the identical `LANGSMITH_API_KEY missing` string, so it still passes. Verify, don't edit.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_workflow_engine.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full workflow + streaming regression set**

Run: `python -m pytest tests/test_workflow_engine.py tests/test_workflow_engine_streaming.py tests/test_runtime_streaming.py -q`
Expected: PASS (sub-agent output still filtered from the live stream; `callbacks` addition does not disturb `stream_mode` filtering).

- [ ] **Step 7: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "$(cat <<'EOF'
feat(workflow): build obs provider once; flush via provider; pass to run_agent

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Generalize the export envelope (provider-agnostic)

**Files:**
- Modify: `src/atom/observability/export.py:50-76` (`build_envelope`), `:156-159` and `:230-234` (call sites)
- Test: `tests/test_export.py`

**Interfaces:**
- Produces: `build_envelope(..., provider: str = "langsmith", sdk_version: str | None = None)` — emits `"provider"` and `"sdk_version"` keys (replacing the hardcoded `langsmith_sdk`).

- [ ] **Step 1: Write the failing test** — append to `tests/test_export.py`:

```python
def test_build_envelope_records_provider_and_sdk():
    m = _manifest("r1", ["succeeded"])
    env = build_envelope(
        "r1", "wf", "proj", m, [{"id": "root1"}],
        complete=True, expected=1, fetched=1, now="t",
        provider="langfuse", sdk_version="3.1.0",
    )
    assert env["provider"] == "langfuse" and env["sdk_version"] == "3.1.0"


def test_build_envelope_defaults_to_langsmith():
    m = _manifest("r1", ["succeeded"])
    env = build_envelope("r1", "wf", "proj", m, [], complete=True, expected=1, fetched=1, now="t")
    assert env["provider"] == "langsmith"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_export.py -k build_envelope -q`
Expected: FAIL — `build_envelope() got an unexpected keyword argument 'provider'`.

- [ ] **Step 3: Implement** — in `src/atom/observability/export.py`, replace `build_envelope` (lines 50-76). Remove the hardcoded `import langsmith` / `langsmith_sdk` and add the two params:

```python
def build_envelope(
    run_id: str, workflow: str, project: str, manifest: RunManifest, roots: list[dict],
    *, complete: bool, expected: int, fetched: int, now: str,
    task_id: str | None = None, session_id: str | None = None,
    provider: str = "langsmith", sdk_version: str | None = None,
) -> dict:
    """The on-disk export: a thin, self-describing wrapper around the raw provider trees.

    ``scope`` is ``"task"`` when ``task_id`` is given (a single task's tree, keyed by ``session_id``),
    else ``"run"``. ``provider`` records which backend produced ``roots`` (their shape differs:
    LangSmith Run dicts vs LangFuse trace+observation dicts). The full manifest is embedded either way.
    """
    return {
        "run_id": run_id,
        "workflow": workflow,
        "project": project,
        "scope": "task" if task_id else "run",
        "task_id": task_id,
        "session_id": session_id,
        "exported_at": now,
        "provider": provider,
        "sdk_version": sdk_version,
        "complete": complete,
        "expected_roots": expected,
        "fetched_roots": fetched,
        "atom_manifest": manifest.model_dump(mode="json"),
        "roots": roots,
    }
```

Update the two LangSmith call sites to pass the SDK version. At the top of `export_run` and `export_task` (or once, near `_default_client`), compute it; update the `build_envelope(...)` calls (lines ~156 and ~230) to add `provider="langsmith", sdk_version=_langsmith_sdk_version()`. Add the helper:

```python
def _langsmith_sdk_version() -> str | None:
    try:
        import langsmith
        return getattr(langsmith, "__version__", None)
    except Exception:  # noqa: BLE001
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_export.py -q`
Expected: PASS (new envelope tests + all existing export tests — the old ones never asserted `langsmith_sdk`).

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/export.py tests/test_export.py
git commit -m "$(cat <<'EOF'
refactor(export): provider-agnostic envelope (provider + sdk_version)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: LangFuse pull-side exporter

**Files:**
- Create: `src/atom/observability/langfuse_export.py`
- Test: `tests/test_langfuse_export.py` (new)

**Interfaces:**
- Consumes: `ExportResult`, `build_envelope`, `expected_root_count`, `resolve_run_ids`, `_EXECUTED`, `_TERMINAL` from `atom.observability.export`; `RunStore` from `atom.workflow.run_store`.
- Produces: `export_run(home, run_id, *, project=None, client=None, poll_timeout=30.0, poll_interval=2.0, now=None, sleep=None, monotonic=None) -> ExportResult`; `export_task(home, run_id, step_index, task_id, *, ...) -> ExportResult`; `fetch_session_traces(client, run_id) -> list[dict]`; re-exports `resolve_run_ids`.

**Design notes for the implementer:**
- LangFuse groups by session; our session = `run_id`. So one `client.api.trace.list(session_id=run_id)` returns **all** traces of the run — task **lead** traces and sub-agent traces as siblings. Each is hydrated with observations via `client.api.trace.get(trace_id, fields="core,io,observations")`.
- The completeness oracle counts **lead** traces only (`metadata.agent_role == "lead"`, else `not metadata.is_subagent`) against `expected_root_count(manifest)` (executed-task count). `roots` in the envelope contains **all** traces (lead + sub-agent). So for LangFuse, `fetched_roots` = lead-trace count and `len(roots) >= fetched_roots`.
- **SDK shape:** the exporter is written against a minimal client contract so tests inject a fake: `client.api.trace.list(session_id=..., page=int)` returns an object with `.data` (a list) or a bare list; `client.api.trace.get(id, fields=...)` returns an object exposing `model_dump()`/`dict()` or a plain dict. Confirm these against the installed `langfuse>=3` at implementation; if `list` items already carry metadata+observations, the per-item `get` can be skipped, but keep `get` for full observation hydration.

- [ ] **Step 1: Write the failing tests** — create `tests/test_langfuse_export.py`:

```python
"""LangFuse run/task exporter with an injected fake client."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atom.observability.langfuse_export import export_run, export_task, fetch_session_traces
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState


def _manifest(run_id, statuses):
    tasks = [TaskState(id=f"t{i}", thread_id=f"{run_id}:s0:t{i}", status=st)
             for i, st in enumerate(statuses)]
    return RunManifest(run_id=run_id, workflow="wf", created_at="2026-07-16T00:00:00",
                       workspace_path="/tmp/ws",
                       steps=[StepState(index=0, title="S", tasks=tasks)])


def _store_with_run(atom_home, run_id, statuses):
    store = RunStore(str(atom_home))
    store.create(_manifest(run_id, statuses).model_copy(update={
        "workspace_path": str(store.workspace_dir(run_id))}))
    return store


class _Trace:
    def __init__(self, id, metadata):
        self.id = id
        self._d = {"id": id, "metadata": metadata, "observations": []}

    def model_dump(self, mode="python"):
        return dict(self._d)


class _Page:
    def __init__(self, data):
        self.data = data


class _FakeAPI:
    def __init__(self, pages, by_id):
        self._pages = pages          # list of pages; each page is a list of trace-summary objects
        self._by_id = by_id          # id -> _Trace (full, hydrated)
        self.list_calls = 0
        self.session_ids = []

    class _TraceNS:
        def __init__(self, outer): self._o = outer
        def list(self, session_id, page=1):
            self._o.session_ids.append(session_id)
            self._o.list_calls += 1
            idx = page - 1
            return _Page(self._o._pages[idx] if idx < len(self._o._pages) else [])
        def get(self, trace_id, fields):
            assert "observations" in fields
            return self._o._by_id[trace_id]

    @property
    def trace(self):
        return _FakeAPI._TraceNS(self)


class _FakeClient:
    def __init__(self, pages, by_id):
        self.api = _FakeAPI(pages, by_id)


def _summary(id):
    class _S:  # a list summary carries at least an id
        pass
    s = _S(); s.id = id
    return s


def _no_sleep(_s): pass


def _lead(id, task): return _Trace(id, {"run_id": "r1", "task_id": task, "agent_role": "lead", "is_subagent": False})
def _sub(id, task): return _Trace(id, {"run_id": "r1", "task_id": task, "agent_role": "subagent", "is_subagent": True})


def test_fetch_session_traces_hydrates_all(atom_home):
    by_id = {"L0": _lead("L0", "t0"), "S0": _sub("S0", "t0")}
    client = _FakeClient([[_summary("L0"), _summary("S0")], []], by_id)
    trees = fetch_session_traces(client, "r1")
    assert {t["id"] for t in trees} == {"L0", "S0"}
    assert client.api.session_ids[0] == "r1"


def test_export_run_counts_lead_traces_only(atom_home, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    by_id = {"L0": _lead("L0", "t0"), "L1": _lead("L1", "t1"), "S0": _sub("S0", "t0")}
    client = _FakeClient([[_summary("L0"), _summary("L1"), _summary("S0")], []], by_id)
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True                       # 2 lead traces == 2 executed tasks
    assert result.fetched_roots == 2 and result.expected_roots == 2
    env = json.loads(Path(result.path).read_text())
    assert env["provider"] == "langfuse"
    assert len(env["roots"]) == 3                         # lead + lead + subagent all present


def test_export_run_partial_on_timeout(atom_home, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    by_id = {"L0": _lead("L0", "t0")}
    client = _FakeClient([[_summary("L0")], []], by_id)   # only 1 of 2 leads ever appears
    clock = iter([0.0, 100.0, 200.0])
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t",
                        sleep=_no_sleep, monotonic=lambda: next(clock), poll_timeout=30.0)
    assert result.complete is False and result.fetched_roots == 1 and result.expected_roots == 2


def test_export_run_requires_keys(atom_home, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    _store_with_run(atom_home, "r1", ["succeeded"])
    with pytest.raises(RuntimeError, match="LANGFUSE"):
        export_run(str(atom_home), "r1", client=_FakeClient([[]], {}))


def test_export_task_selects_by_task_id(atom_home, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    by_id = {"L0": _lead("L0", "t0"), "S0": _sub("S0", "t0"), "L1": _lead("L1", "t1")}
    client = _FakeClient([[_summary("L0"), _summary("S0"), _summary("L1")], []], by_id)
    result = export_task(str(atom_home), "r1", 0, "t0", client=client, now=lambda: "t", sleep=_no_sleep)
    assert result.task_id == "t0" and result.complete is True and result.fetched_roots == 1
    env = json.loads(Path(result.path).read_text())
    assert {t["id"] for t in env["roots"]} == {"L0", "S0"}   # task t0's lead + its subagent only
    assert result.path.endswith("exports/s0__t0.json")


def test_export_task_rejects_non_terminal(atom_home, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["running"])
    with pytest.raises(ValueError, match="not completed"):
        export_task(str(atom_home), "r1", 0, "t0", client=_FakeClient([[]], {}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_langfuse_export.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.observability.langfuse_export'`.

- [ ] **Step 3: Implement** — create `src/atom/observability/langfuse_export.py`:

```python
"""Download a workflow run's LangFuse traces to disk for offline evaluation.

Read-only. LangFuse groups by session, and atom's session == the whole run_id, so one session
list returns every trace of the run — task LEAD traces and sub-agent traces as siblings (unlike
LangSmith, where sub-agents nest under the lead root). The completeness oracle counts lead traces
only (== executed tasks); the envelope's ``roots`` holds all traces, each hydrated with its
observation tree.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from typing import Any, Callable

from atom.observability.export import (
    ExportResult,
    _EXECUTED,          # noqa: F401 — re-exported for symmetry/testing
    _TERMINAL,
    build_envelope,
    expected_root_count,
    resolve_run_ids,    # noqa: F401 — dispatched CLI/API import this from here too
)
from atom.workflow.run_store import RunStore


def _require_keys() -> None:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are not set — cannot export from LangFuse"
        )


def _default_client() -> Any:
    from langfuse import Langfuse
    return Langfuse()


def _langfuse_sdk_version() -> str | None:
    try:
        import langfuse
        return getattr(langfuse, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            return fn()
    return dict(vars(obj))


def _item_id(item: Any) -> str:
    return getattr(item, "id", None) or (item["id"] if isinstance(item, dict) else None)


def fetch_session_traces(client: Any, run_id: str) -> list[dict]:
    """List every trace in the run's session and hydrate each with its observation tree.

    Pages until an empty page is returned (works for the real paginated API and simple fakes).
    """
    trees: list[dict] = []
    page = 1
    while True:
        resp = client.api.trace.list(session_id=run_id, page=page)
        items = list(getattr(resp, "data", resp) or [])
        if not items:
            break
        for it in items:
            full = client.api.trace.get(_item_id(it), fields="core,io,observations")
            trees.append(_as_dict(full))
        page += 1
    return trees


def _metadata(trace: dict) -> dict:
    md = trace.get("metadata")
    return md if isinstance(md, dict) else {}


def _is_lead(trace: dict) -> bool:
    md = _metadata(trace)
    if "agent_role" in md:
        return md["agent_role"] == "lead"
    return not md.get("is_subagent", False)


def _lead_count(traces: list[dict]) -> int:
    return sum(1 for t in traces if _is_lead(t))


def export_run(
    home: str | None,
    run_id: str,
    *,
    project: str | None = None,          # unused for LangFuse; kept for signature parity
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download ``run_id``'s LangFuse traces to ``runs/<run_id>/export.json``.

    Polls until #lead-traces matches #executed tasks (local manifest) or ``poll_timeout`` elapses.
    Writes nothing when no traces are found.
    """
    store = RunStore(home)
    manifest = store.load(run_id)                        # FileNotFoundError if unknown locally
    _require_keys()

    client = client or _default_client()
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    expected = expected_root_count(manifest)
    deadline = monotonic() + poll_timeout
    traces: list[dict] = []
    while True:
        traces = fetch_session_traces(client, run_id)
        if expected == 0 or _lead_count(traces) >= expected:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    fetched = _lead_count(traces)
    if not traces:
        return ExportResult(run_id=run_id, path="", complete=False,
                            expected_roots=expected, fetched_roots=0)

    complete = fetched >= expected
    envelope = build_envelope(
        run_id, manifest.workflow, project or "", manifest, traces,
        complete=complete, expected=expected, fetched=fetched, now=now(),
        provider="langfuse", sdk_version=_langfuse_sdk_version(),
    )
    path = store.run_dir(run_id) / "export.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("export.json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return ExportResult(run_id=run_id, path=str(path), complete=complete,
                        expected_roots=expected, fetched_roots=fetched)


def export_task(
    home: str | None,
    run_id: str,
    step_index: int,
    task_id: str,
    *,
    project: str | None = None,          # unused for LangFuse; kept for signature parity
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download one task's LangFuse traces (its lead + sub-agent traces) to
    ``runs/<run_id>/exports/s<step>__<task>.json``. The task must be terminal.
    """
    store = RunStore(home)
    manifest = store.load(run_id)

    step = next((s for s in manifest.steps if s.index == step_index), None)
    if step is None:
        raise KeyError(f"step {step_index} not found in run {run_id!r}")
    task = next((t for t in step.tasks if t.id == task_id), None)
    if task is None:
        raise KeyError(f"task {task_id!r} not found in step {step_index} of run {run_id!r}")
    if task.status not in _TERMINAL:
        raise ValueError(f"task {task_id!r} has not completed (status: {task.status})")
    _require_keys()

    client = client or _default_client()
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    def _for_task(traces: list[dict]) -> list[dict]:
        return [t for t in traces if _metadata(t).get("task_id") == task_id]

    deadline = monotonic() + poll_timeout
    selected: list[dict] = []
    while True:
        selected = _for_task(fetch_session_traces(client, run_id))
        if _lead_count(selected) >= 1:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    fetched = _lead_count(selected)
    if not selected:
        return ExportResult(run_id=run_id, path="", complete=False,
                            expected_roots=1, fetched_roots=0, task_id=task_id)

    envelope = build_envelope(
        run_id, manifest.workflow, project or "", manifest, selected,
        complete=True, expected=1, fetched=fetched, now=now(),
        task_id=task_id, session_id=task.thread_id,
        provider="langfuse", sdk_version=_langfuse_sdk_version(),
    )
    path = store.task_export_path(run_id, step_index, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return ExportResult(run_id=run_id, path=str(path), complete=True,
                        expected_roots=1, fetched_roots=fetched, task_id=task_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_langfuse_export.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/langfuse_export.py tests/test_langfuse_export.py
git commit -m "$(cat <<'EOF'
feat(observability): LangFuse pull-side exporter (session = run, lead oracle)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Export dispatch by provider (CLI + API)

**Files:**
- Modify: `src/atom/cli.py:273-376` (`_export_one_task`, `workflow_export`)
- Modify: `src/atom/api/app.py:222-258` (`export_traces`)
- Test: `tests/test_cli_export.py` (extend), `tests/test_workflow_api.py` or the API test module that covers `export_traces`

**Interfaces:**
- Consumes: `cfg.observability.provider`; both `atom.observability.export` and `atom.observability.langfuse_export` expose identical `export_run` / `export_task` / `resolve_run_ids` signatures.
- Produces: `_export_module(cfg)` helper selecting the exporter; provider-aware credential/project guards.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli_export.py` (it already has module-level `runner = CliRunner()`, `app`, `ExportResult`, and the `_ok` helper):

```python
import atom.cli as cli
import atom.observability.langfuse_export as lf_mod
from atom.config.schema import AtomConfig, ObservabilityConfig


def test_export_dispatches_to_langfuse(monkeypatch):
    """provider=langfuse -> `workflow export` calls the LangFuse exporter, gated on LANGFUSE keys."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    cfg = AtomConfig(observability=ObservabilityConfig(provider="langfuse"))
    monkeypatch.setattr(cli, "load_config", lambda config: cfg)
    monkeypatch.setattr(lf_mod, "resolve_run_ids",
                        lambda home, **kw: [kw["run_id"]] if kw.get("run_id") else [])
    seen = {}
    def fake_run(home, run_id, *, project, **kw):
        seen["run_id"] = run_id
        return _ok(run_id)
    monkeypatch.setattr(lf_mod, "export_run", fake_run)

    res = runner.invoke(app, ["workflow", "export", "abc123"])   # no --project needed for langfuse
    assert res.exit_code == 0
    assert seen["run_id"] == "abc123"
    assert "exported abc123" in res.stdout


def test_export_langfuse_missing_keys_exits_1(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    cfg = AtomConfig(observability=ObservabilityConfig(provider="langfuse"))
    monkeypatch.setattr(cli, "load_config", lambda config: cfg)
    res = runner.invoke(app, ["workflow", "export", "abc123"])
    assert res.exit_code == 1
    assert "LANGFUSE" in res.stdout
```

Note: `_load_env()` uses `python-dotenv` with `override=False`, so the `monkeypatch.setenv` values win over any `.env` entries.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_export.py -k langfuse -q`
Expected: FAIL (LangSmith exporter is still called / project guard rejects).

- [ ] **Step 3: Implement the CLI dispatch** — in `src/atom/cli.py`, add a helper near `_export_one_task`:

```python
def _export_module(cfg):
    """Select the exporter matching the configured provider (both expose export_run/export_task/resolve_run_ids)."""
    provider = cfg.observability.provider
    if provider is None:
        provider = "langsmith" if cfg.observability.enabled else "none"
    if provider == "langfuse":
        from atom.observability import langfuse_export as mod
        return "langfuse", mod
    from atom.observability import export as mod
    return "langsmith", mod
```

Rewrite the head of `workflow_export` (the current lines 326-337, from `from atom.observability import export as export_mod` through the `proj` guard) to load config first, then branch on provider — LangFuse needs credentials, not a project:

```python
    _load_env()
    cfg = load_config(config)
    provider, export_mod = _export_module(cfg)

    if provider == "langfuse":
        proj = None                                       # LangFuse has no --project concept here
        if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
            console.print("[red]set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to export from LangFuse[/red]")
            raise typer.Exit(1)
    else:
        proj = project or cfg.observability.project
        if not proj:
            console.print("[red]no LangSmith project — set observability.project or pass --project[/red]")
            raise typer.Exit(1)

    if task is not None:
        _export_one_task(export_mod, cfg, proj, run_id, latest, all_workflow, task)
        return
```

This replaces the old `from atom.observability import export as export_mod` line (now provided by `_export_module`) and the old `cfg = load_config(config)` / `proj = ...` lines. Ensure `import os` is present at the top of `cli.py` (it is used elsewhere). The remaining `export_run`/`export_task` calls already pass `project=proj`, which the LangFuse exporter accepts and ignores.

- [ ] **Step 4: Implement the API dispatch** — in `src/atom/api/app.py`, replace the head of `export_traces` (lines 229-239):

```python
        provider = cfg.observability.provider
        if provider is None:
            provider = "langsmith" if cfg.observability.enabled else "none"
        if provider == "langfuse":
            from atom.observability import langfuse_export as export_mod
            if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
                raise HTTPException(503, "export not configured: set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY")
            proj = None
        else:
            from atom.observability import export as export_mod
            proj = cfg.observability.project
            if not proj:
                raise HTTPException(503, "export not configured: set observability.project")
        body = body or ExportRequest()
        try:
            if body.step is not None and body.task is not None:
                res = export_mod.export_task(cfg.home, run_id, body.step, body.task, project=proj)
            else:
                res = export_mod.export_run(cfg.home, run_id, project=proj)
```

(Ensure `import os` is present at the top of `app.py`.) The existing `except` handlers already map `RuntimeError -> 503` (the LangFuse `_require_keys()` raises `RuntimeError`), `ValueError -> 400`, `KeyError -> 404`, etc.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_export.py -q`
Expected: PASS (LangSmith dispatch unchanged; new LangFuse dispatch works).

- [ ] **Step 6: Full-suite regression**

Run: `python -m pytest -q`
Expected: PASS (entire suite green).

- [ ] **Step 7: Commit**

```bash
git add src/atom/cli.py src/atom/api/app.py tests/test_cli_export.py
git commit -m "$(cat <<'EOF'
feat(export): dispatch workflow export CLI/API by observability.provider

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] Run the whole suite: `python -m pytest -q` — expect all green.
- [ ] Smoke the provider factory offline:
  `python -c "from atom.config.schema import AtomConfig, ObservabilityConfig; from atom.observability import build_provider; print(build_provider(AtomConfig(observability=ObservabilityConfig(provider='none'))).name)"`
  Expected: `none`.
- [ ] Confirm `langfuse` imports: `python -c "from langfuse.langchain import CallbackHandler; print('ok')"` → `ok`.
- [ ] Manual (optional, needs a LangFuse instance): set `provider: langfuse` + keys, run a small workflow, confirm one **session = run_id** in the LangFuse UI with lead + sub-agent traces grouped, then `atom workflow export <run_id>` writes `runs/<run_id>/export.json` with `"provider": "langfuse"`.

## Spec coverage check

- Provider discriminator + `LangfuseConfig` → Task 1. Backward-compat legacy fallback → Task 2 (`build_provider`).
- Provider strategy (`Null`/`LangSmith`/`LangFuse`) → Tasks 2–3.
- Push-side decorate at lead + sub-agent roots; `langfuse_session_id = run_id` on both → Tasks 4–5.
- Enrich gate generalized off `tracing_active()` → Task 6.
- Provider built once in engine; flush via provider (no-mask) → Task 7.
- Export parity: shared envelope + oracle, LangFuse native roots, lead-only completeness, per-task selection, CLI/API dispatch + credential guard → Tasks 8–10.
- Dependency `langfuse>=3,<4`, `.env.example`/`config.yaml` docs → Tasks 1, 3.
- Testing mirrors existing suites with mocked SDKs → every task.
