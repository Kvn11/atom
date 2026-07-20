# Size Limits (Telemetry-Side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LangFuse export succeed with full data on oversized traces (paginate observations instead of the one giant `trace.get`), and cap telemetry payloads at the source so future traces stay under LangFuse's 80 MB-per-trace read limit.

**Architecture:** A truncating `mask` on the `Langfuse` client (the SDK's supported serialization hook; the `CallbackHandler` binds to that client) recursively truncates oversized observation fields before export. On the read side, `fetch_session_traces` falls back from `trace.get(id)` to `trace.get(id, fields="core")` + paginated `observations.get_many(trace_id=…)` when a trace is too large to return whole, and writes a metadata-preserving placeholder (marked non-lead) only if even that fails.

**Tech Stack:** LangFuse Python SDK v3 (`Langfuse(mask=…)`, `client.api.trace.get(fields=…)`, `client.api.observations.get_many(cursor=…)`), Pydantic config, pytest.

## Global Constraints

- Telemetry must NEVER break a run: the `mask` wraps its body in try/except and returns `data` unchanged on any error.
- Export must NEVER fail wholesale because of one trace: a per-trace fetch failure degrades to a placeholder + `logger.warning`, not an exception.
- Data-preserving first: prefer paginating a too-large trace's observations over dropping it. A metadata-preserving placeholder is the last resort.
- Completeness honesty: a placeholder for a trace we could not read must NOT be counted as a lead (else a lost lead would falsely satisfy `expected`). Mark it `is_subagent: True`.
- **Dependency:** `atom.limits.truncate_text` from the model-side plan (`2026-07-19-size-limits-model-side.md`, Task 1). Land that helper first, or copy it in as its own commit.
- Test command: `python -m pytest <path> -v` from the repo root. `langfuse` is installed in the test env (`tests/test_langfuse_export.py` imports it).

---

### Task 1: Config fields for the truncating mask

**Files:**
- Modify: `src/atom/config/schema.py` (`LangfuseConfig` ~line 138)
- Test: `tests/test_observability_config.py` (append)

**Interfaces:**
- Produces: `LangfuseConfig.max_field_chars: int` (default `100_000`), `LangfuseConfig.max_observation_bytes: int` (default `2_000_000`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_observability_config.py  (append)
from atom.config.schema import LangfuseConfig


def test_langfuse_mask_size_defaults():
    lf = LangfuseConfig()
    assert lf.max_field_chars == 100_000
    assert lf.max_observation_bytes == 2_000_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_observability_config.py -k mask_size -v`
Expected: FAIL with `AttributeError: 'LangfuseConfig' object has no attribute 'max_field_chars'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/config/schema.py — add to LangfuseConfig (after sample_rate, ~line 147)
    # Truncating mask thresholds (guard LangFuse's 80MB-per-trace read limit). Any observation
    # string field longer than max_field_chars is truncated; if a single observation still
    # serializes larger than max_observation_bytes it is replaced with a marker.
    max_field_chars: int = 100_000
    max_observation_bytes: int = 2_000_000
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_observability_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atom/config/schema.py tests/test_observability_config.py
git commit -m "feat(config): langfuse mask size thresholds (max_field_chars, max_observation_bytes)"
```

---

### Task 2: Truncating `mask` on the LangFuse client

**Files:**
- Modify: `src/atom/observability/provider.py` (add mask helpers near top; wire into `_default_langfuse_factory` ~line 114)
- Test: `tests/test_observability_provider.py` (append)

**Interfaces:**
- Consumes: `atom.limits.truncate_text`; `LangfuseConfig.max_field_chars` / `.max_observation_bytes`.
- Produces:
  - `_make_truncating_mask(max_field_chars: int, max_observation_bytes: int) -> Callable` returning a `mask(*, data, **kwargs) -> Any` that never raises.
  - `_default_langfuse_factory` passes `mask=_make_truncating_mask(lf.max_field_chars, lf.max_observation_bytes)` to `Langfuse(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_observability_provider.py  (append)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_observability_provider.py -k mask -v`
Expected: FAIL with `ImportError: cannot import name '_make_truncating_mask'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/observability/provider.py — add near the top (after imports, before classes)
import json
from typing import Any, Callable

_MASK_MARKER = "[…{elided} of {total} chars elided by atom size cap…]"


def _walk_truncate(data: Any, max_field_chars: int) -> Any:
    from atom.limits import truncate_text
    if isinstance(data, str):
        return truncate_text(data, max_chars=max_field_chars, marker_template=_MASK_MARKER)
    if isinstance(data, dict):
        return {k: _walk_truncate(v, max_field_chars) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_walk_truncate(v, max_field_chars) for v in data]
    return data


def _make_truncating_mask(max_field_chars: int, max_observation_bytes: int) -> Callable[..., Any]:
    """Build a LangFuse ``mask(*, data, **kwargs)`` that recursively truncates oversized string
    fields, then guards a still-huge observation with a marker. Never raises — telemetry must not
    break a run."""
    def _mask(*, data: Any, **_kwargs: Any) -> Any:
        try:
            walked = _walk_truncate(data, max_field_chars)
            try:
                size = len(json.dumps(walked, default=str))
            except Exception:  # noqa: BLE001
                size = 0
            if size > max_observation_bytes:
                return (f"[atom: observation payload elided — ~{size} bytes exceeds the "
                        f"{max_observation_bytes}-byte cap]")
            return walked
        except Exception:  # noqa: BLE001 — telemetry must never break a run
            return data
    return _mask
```

Wire it into `Langfuse(...)` in `_default_langfuse_factory` (~line 114):

```python
    client = Langfuse(
        public_key=public,
        secret_key=secret,
        host=lf.host or os.environ.get("LANGFUSE_HOST"),
        environment=lf.environment,
        release=lf.release or git_sha(),
        sample_rate=lf.sample_rate,
        mask=_make_truncating_mask(lf.max_field_chars, lf.max_observation_bytes),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_observability_provider.py -v`
Expected: PASS (all, including existing provider tests)

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/provider.py tests/test_observability_provider.py
git commit -m "feat(observability): truncating LangFuse mask caps observation payloads at the source"
```

---

### Task 3: Resilient, data-preserving export fetch

**Files:**
- Modify: `src/atom/observability/langfuse_export.py` (add helpers; change `fetch_session_traces` ~line 119–121)
- Test: `tests/test_langfuse_export.py` (append)

**Interfaces:**
- Consumes: existing `_as_dict`, `_item_id`; the LangFuse read API (`client.api.trace.get(id[, fields])`, `client.api.observations.get_many(trace_id=…, cursor=…, limit=…)`).
- Produces: `_fetch_trace_resilient(client, trace_id) -> dict` used by `fetch_session_traces`; falls back to paginated assembly on a too-large `trace.get`, then to a placeholder.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_langfuse_export.py  (append; reuses _Page, _summary, _lead from this file)
import types

from atom.observability.langfuse_export import fetch_session_traces


class _TooLarge(Exception):
    def __init__(self):
        super().__init__("status code 422: observations in trace are too large: "
                         "80.30mb exceeds limit of 80.00mb")


class _ObsPage:
    def __init__(self, data, next_cursor):
        self.data = data
        self.meta = types.SimpleNamespace(next_cursor=next_cursor)


class _ResilientAPI:
    def __init__(self, pages, core_by_id, fail_full, obs_by_trace, fail_core=None):
        self._pages = pages
        self._core = core_by_id
        self._fail_full = fail_full
        self._fail_core = fail_core or set()
        self._obs = obs_by_trace
        self.session_ids = []

    class _TraceNS:
        def __init__(self, o):
            self._o = o

        def list(self, session_id, page=1):
            self._o.session_ids.append(session_id)
            idx = page - 1
            return _Page(self._o._pages[idx] if idx < len(self._o._pages) else [])

        def get(self, trace_id, fields=None):
            if fields == "core":
                if trace_id in self._o._fail_core:
                    raise _TooLarge()
                return self._o._core[trace_id]
            if trace_id in self._o._fail_full:
                raise _TooLarge()
            return self._o._core[trace_id]

    class _ObsNS:
        def __init__(self, o):
            self._o = o

        def get_many(self, *, trace_id, cursor=None, limit=None):
            pages = self._o._obs.get(trace_id, [[]])
            i = cursor or 0
            data = pages[i] if i < len(pages) else []
            nxt = (i + 1) if (i + 1) < len(pages) else None
            return _ObsPage(data, nxt)

    @property
    def trace(self):
        return _ResilientAPI._TraceNS(self)

    @property
    def observations(self):
        return _ResilientAPI._ObsNS(self)


class _ResilientClient:
    def __init__(self, pages, core_by_id, fail_full, obs_by_trace, fail_core=None):
        self.api = _ResilientAPI(pages, core_by_id, fail_full, obs_by_trace, fail_core)


def test_export_paginates_observations_when_trace_get_too_large(atom_home):
    core = {"L0": _lead("L0", "t0")}                      # carries lead metadata via fields="core"
    obs = {"L0": [[{"id": "o1", "input": "big"}, {"id": "o2", "output": "stuff"}]]}
    client = _ResilientClient([[_summary("L0")], []], core, {"L0"}, obs)
    trees = fetch_session_traces(client, "r1")
    assert len(trees) == 1
    assert trees[0]["id"] == "L0"
    assert trees[0]["metadata"]["agent_role"] == "lead"                 # metadata preserved
    assert {o["id"] for o in trees[0]["observations"]} == {"o1", "o2"}   # full data preserved


def test_export_paginates_across_multiple_pages(atom_home):
    core = {"L0": _lead("L0", "t0")}
    obs = {"L0": [[{"id": "o1"}], [{"id": "o2"}], []]}   # cursor 0 -> 1 -> stop
    client = _ResilientClient([[_summary("L0")], []], core, {"L0"}, obs)
    trees = fetch_session_traces(client, "r1")
    assert {o["id"] for o in trees[0]["observations"]} == {"o1", "o2"}


def test_export_placeholder_when_even_core_fails(atom_home):
    client = _ResilientClient([[_summary("L0")], []], {}, {"L0"}, {}, fail_core={"L0"})
    trees = fetch_session_traces(client, "r1")
    assert len(trees) == 1
    assert trees[0]["metadata"].get("atom_export_degraded") == "fetch-failed"
    assert trees[0]["metadata"].get("is_subagent") is True   # not counted as a lead
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_langfuse_export.py -k "too_large or multiple_pages or even_core" -v`
Expected: FAIL — `_TooLarge` propagates out of `fetch_session_traces` (no resilient fetch yet), or `ImportError`/`KeyError`.

- [ ] **Step 3: Write minimal implementation**

Add helpers and swap the hydrate call in `fetch_session_traces`:

```python
# src/atom/observability/langfuse_export.py — add near the other module helpers
import logging

logger = logging.getLogger(__name__)


def _is_too_large(exc: Exception) -> bool:
    text = str(exc).lower()
    return "too large" in text or "exceeds limit" in text or "413" in text


def _next_cursor(resp: Any):
    meta = getattr(resp, "meta", None) or getattr(resp, "metadata", None)
    if meta is None:
        return None
    for attr in ("next_cursor", "nextCursor", "cursor"):
        val = meta.get(attr) if isinstance(meta, dict) else getattr(meta, attr, None)
        if val:
            return val
    return None


def _assemble_from_pages(client: Any, trace_id: str) -> dict:
    """Rebuild an oversized trace WITHOUT the monolithic trace.get: metadata via fields='core',
    observations via cursor-paginated get_many (each page bounded well under the read limit)."""
    core = _as_dict(client.api.trace.get(trace_id, fields="core"))
    observations: list = []
    cursor = None
    while True:
        resp = client.api.observations.get_many(trace_id=trace_id, cursor=cursor, limit=1000)
        items = list(getattr(resp, "data", resp) or [])
        observations.extend(_as_dict(it) for it in items)
        cursor = _next_cursor(resp)
        if not items or not cursor:
            break
    core["observations"] = observations
    return core


def _fetch_trace_resilient(client: Any, trace_id: str) -> dict:
    """Hydrate one trace, tolerating a trace too large for the read API. Fast path is the normal
    trace.get; on any failure fall back to paginated assembly, then to a metadata-safe placeholder."""
    try:
        return _as_dict(client.api.trace.get(trace_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse export: trace.get(%s) failed (%s: %s); paginating observations",
                       trace_id, type(exc).__name__, exc)
    try:
        return _assemble_from_pages(client, trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse export: paginated fallback for trace %s failed (%s: %s); "
                       "writing a degraded placeholder", trace_id, type(exc).__name__, exc)
        # is_subagent=True keeps a lost trace from being miscounted as a satisfied lead.
        return {"id": trace_id, "observations": [],
                "metadata": {"is_subagent": True, "atom_export_degraded": "fetch-failed"}}
```

Then change the hydrate loop in `fetch_session_traces` (~line 119):

```python
        for it in items:
            trees.append(_fetch_trace_resilient(client, _item_id(it)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_langfuse_export.py -v`
Expected: PASS (the 3 new tests + every existing export test — the fast path `test_fetch_session_traces_hydrates_all` is unchanged because normal `trace.get` succeeds).

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/langfuse_export.py tests/test_langfuse_export.py
git commit -m "feat(langfuse-export): resilient paginated fetch survives 80MB-too-large traces"
```

---

### Task 4: Full-suite regression check

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — no regression in `tests/test_langfuse_export.py`, `tests/test_observability_provider.py`, `tests/test_observability_config.py`, or `tests/test_config.py`.

- [ ] **Step 2: Confirm the model-side dependency is present**

Run: `python -c "from atom.limits import truncate_text; print('ok')"`
Expected: prints `ok` (the shared helper from the model-side plan is importable).

---

## Self-Review

**Spec coverage:**
- Layer 3a truncating mask → Task 2 (helpers + factory wiring). Config thresholds → Task 1. ✓
- Layer 3b data-preserving resilient export (paginate on too-large) → Task 3. ✓
- Placeholder marked non-lead to keep completeness honest → Task 3 (`is_subagent: True`) + `test_export_placeholder_when_even_core_fails`. ✓
- "Never break a run / never fail export wholesale" → mask try/except (Task 2), `_fetch_trace_resilient` try/except (Task 3). ✓
- Uses `client.api.trace.get(fields="core")` + `client.api.observations.get_many(cursor=…)` as verified against the SDK. ✓
- Shared `truncate_text` dependency called out in Global Constraints. ✓

**Placeholder scan:** No TBD/TODO; complete code and real assertions in every step. ✓

**Type consistency:** `_make_truncating_mask(max_field_chars, max_observation_bytes)` defined and constructed identically (Task 2). `_fetch_trace_resilient(client, trace_id)` / `_assemble_from_pages(client, trace_id)` / `_next_cursor(resp)` names match across definition and call sites. `LangfuseConfig.max_field_chars` / `.max_observation_bytes` match Task 1 ↔ Task 2 wiring. Fake `observations.get_many(*, trace_id, cursor, limit)` signature matches the production call. ✓
