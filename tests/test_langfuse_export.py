"""LangFuse run/task exporter with an injected fake client."""
from __future__ import annotations

import datetime
import inspect
import json
import re
from pathlib import Path

import pytest
from langfuse import Langfuse

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
        def get(self, trace_id):
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


def _lead(id, task, step=0):
    return _Trace(id, {"run_id": "r1", "task_id": task, "step_index": step,
                       "agent_role": "lead", "is_subagent": False})


def _sub(id, task, step=0):
    return _Trace(id, {"run_id": "r1", "task_id": task, "step_index": step,
                       "agent_role": "subagent", "is_subagent": True})


class _DatetimeInPythonModeTrace:
    """A fake trace whose python-mode dump is NOT JSON-safe but whose json-mode dump is.

    Mirrors what a real LangFuse SDK object does: ``model_dump()``/``model_dump(mode="python")``
    keeps native ``datetime`` values, while ``model_dump(mode="json")`` serializes them to strings.
    If the exporter ever regresses to calling ``model_dump()`` with no args, ``json.dumps`` on the
    envelope in ``export_run`` would raise ``TypeError: Object of type datetime is not JSON serializable``.
    """
    def __init__(self, id, task):
        self.id = id
        self._task = task

    def model_dump(self, mode="python"):
        metadata = {"run_id": "r1", "task_id": self._task, "agent_role": "lead", "is_subagent": False}
        if mode == "json":
            return {"id": self.id, "metadata": metadata, "observations": [],
                     "timestamp": "2026-07-16T00:00:00"}
        return {"id": self.id, "metadata": metadata, "observations": [],
                "timestamp": datetime.datetime(2026, 7, 16)}


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


def test_export_run_serializes_trace_dumped_in_json_mode(atom_home, monkeypatch):
    """A real LangFuse trace's model_dump() (python mode) carries a native datetime — not
    JSON-serializable. The exporter must request mode="json" so export.json (written via
    json.dumps) round-trips; if it ever regressed to a no-arg model_dump(), this test would
    fail with TypeError: Object of type datetime is not JSON serializable.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded"])
    by_id = {"L0": _DatetimeInPythonModeTrace("L0", "t0")}
    client = _FakeClient([[_summary("L0")], []], by_id)
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 1
    env = json.loads(Path(result.path).read_text())               # would raise if not JSON-safe
    assert env["roots"][0]["timestamp"] == "2026-07-16T00:00:00"


def test_real_langfuse_trace_get_has_no_fields_param():
    """Mock-drift guard: locks _FakeAPI._TraceNS.get's signature to the real langfuse SDK.

    The installed langfuse client's ``api.trace.get`` is ``get(self, trace_id, *,
    request_options=None)`` — there is no ``fields`` kwarg; passing one raises TypeError
    against the real SDK. Constructing ``Langfuse(...)`` here is offline-safe (lazy, no
    network calls). If a future langfuse upgrade adds/removes this parameter, this test
    fails loudly instead of the fake silently drifting from the real contract again.
    """
    client = Langfuse(public_key="x", secret_key="y")
    params = inspect.signature(client.api.trace.get).parameters
    assert "fields" not in params
    assert "trace_id" in params


def test_export_run_records_real_sdk_version(atom_home, monkeypatch):
    """The envelope's sdk_version must be a real version string, not None.

    ``langfuse`` has no ``__version__`` attribute; ``_langfuse_sdk_version`` must resolve
    the version via ``importlib.metadata`` instead, or every export silently loses
    provenance (sdk_version: null).
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded"])
    by_id = {"L0": _lead("L0", "t0")}
    client = _FakeClient([[_summary("L0")], []], by_id)
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t", sleep=_no_sleep)
    env = json.loads(Path(result.path).read_text())
    assert env["sdk_version"] is not None
    assert re.match(r"\d+\.\d+", env["sdk_version"])


# --- review-fix behaviors ---------------------------------------------------

def test_export_run_no_lead_traces_writes_nothing(atom_home, monkeypatch):
    """Only sub-agent traces present (lead not yet uploaded) -> no export.json (parity w/ LangSmith)."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    store = _store_with_run(atom_home, "r1", ["succeeded"])
    by_id = {"S0": _sub("S0", "t0")}
    client = _FakeClient([[_summary("S0")], []], by_id)
    clock = iter([0.0, 100.0])
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t",
                        sleep=_no_sleep, monotonic=lambda: next(clock))
    assert result.fetched_roots == 0 and result.path == ""
    assert not (store.run_dir("r1") / "export.json").exists()


def test_export_task_no_lead_trace_is_incomplete(atom_home, monkeypatch):
    """A task match set with only a sub-agent trace must NOT be written as complete with 0 leads."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    store = _store_with_run(atom_home, "r1", ["succeeded"])
    by_id = {"S0": _sub("S0", "t0")}
    client = _FakeClient([[_summary("S0")], []], by_id)
    clock = iter([0.0, 100.0])
    result = export_task(str(atom_home), "r1", 0, "t0", client=client, now=lambda: "t",
                         sleep=_no_sleep, monotonic=lambda: next(clock))
    assert result.complete is False and result.fetched_roots == 0 and result.path == ""
    assert not (store.run_dir("r1") / "exports" / "s0__t0.json").exists()


def test_export_task_scopes_by_step_index(atom_home):
    """A task id reused across steps must pull ONLY the requested step's traces."""
    store = RunStore(str(atom_home))
    m = RunManifest(
        run_id="r1", workflow="wf", created_at="2026-07-16T00:00:00",
        workspace_path=str(store.workspace_dir("r1")),
        steps=[
            StepState(index=0, title="A",
                      tasks=[TaskState(id="writer", thread_id="r1:s0:writer", status="succeeded")]),
            StepState(index=1, title="B",
                      tasks=[TaskState(id="writer", thread_id="r1:s1:writer", status="succeeded")]),
        ],
    )
    store.create(m)
    import os
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk"
    try:
        by_id = {"L0": _lead("L0", "writer", step=0), "L1": _lead("L1", "writer", step=1)}
        client = _FakeClient([[_summary("L0"), _summary("L1")], []], by_id)
        result = export_task(str(atom_home), "r1", 0, "writer", client=client,
                             now=lambda: "t", sleep=_no_sleep)
    finally:
        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
    env = json.loads(Path(result.path).read_text())
    assert {t["id"] for t in env["roots"]} == {"L0"}    # only step 0's 'writer', NOT step 1's
    assert result.fetched_roots == 1


def test_export_run_accepts_config_keys(atom_home, monkeypatch):
    """cfg with config.yaml langfuse keys (no env) satisfies the export credential guard."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from atom.config.schema import AtomConfig, ObservabilityConfig
    cfg = AtomConfig(
        home=str(atom_home),
        observability=ObservabilityConfig(
            provider="langfuse",
            langfuse={"public_key": "cfg_pk", "secret_key": "cfg_sk"},
        ),
    )
    _store_with_run(atom_home, "r1", ["succeeded"])
    by_id = {"L0": _lead("L0", "t0")}
    client = _FakeClient([[_summary("L0")], []], by_id)
    result = export_run(str(atom_home), "r1", cfg=cfg, client=client, now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 1


# --- real-SDK-object serialization (mock-drift guard) -----------------------

def _real_trace(id, task, step=0):
    """A REAL langfuse ``TraceWithFullDetails`` (pydantic v1, native datetime) — NOT a fake.

    ``client.api.trace.get`` returns this Fern model, which has NO ``model_dump`` and whose
    ``.dict()`` keeps a native ``datetime``. Exercising it (instead of a fake with
    ``model_dump(mode="json")``) is the only way to catch a regression in ``_as_dict``'s
    pydantic-v1 (``.json()``) path — the path every real export takes.
    """
    from langfuse.api.resources.commons.types.trace_with_full_details import TraceWithFullDetails
    return TraceWithFullDetails(
        id=id, timestamp=datetime.datetime(2026, 7, 16, 12, 0, 0),
        htmlPath=f"/traces/{id}", latency=1.0, totalCost=0.0, observations=[], scores=[],
        tags=[], public=False, environment="default",
        metadata={"run_id": "r1", "task_id": task, "step_index": step,
                  "agent_role": "lead", "is_subagent": False},
    )


def test_export_run_serializes_real_sdk_trace_object(atom_home, monkeypatch):
    """Regression against the REAL SDK object, not a fake. A pydantic-v1 ``TraceWithFullDetails``
    has no ``model_dump`` and a native ``datetime`` timestamp; ``_as_dict`` must yield a JSON-safe
    dict (via ``.json()``) or ``json.dumps(envelope)`` raises ``TypeError`` and NO export is written.
    The other fakes define ``model_dump(mode="json")``, which the real object lacks — masking this.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded"])
    by_id = {"L0": _real_trace("L0", "t0")}
    client = _FakeClient([[_summary("L0")], []], by_id)
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 1
    env = json.loads(Path(result.path).read_text())          # raises TypeError before the fix
    assert env["roots"][0]["id"] == "L0"
    assert isinstance(env["roots"][0]["timestamp"], str)     # ISO string, not a native datetime


# --- completeness / pagination / parity hardening ---------------------------

def test_export_run_duplicate_lead_does_not_mask_missing_task(atom_home, monkeypatch):
    """Crash recovery re-runs a task, so Langfuse can hold TWO lead traces for the same (step,task).
    Completeness counts DISTINCT (step,task) identities, so a duplicate for t0 must NOT satisfy the
    2-task expectation while t1's lead is still missing — the export stays partial, not falsely
    complete (which would silently drop t1's whole trace tree from an eval export).
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])     # expected = 2 tasks
    by_id = {"L0": _lead("L0", "t0"), "L0b": _lead("L0b", "t0")}     # two leads, BOTH task t0
    client = _FakeClient([[_summary("L0"), _summary("L0b")], []], by_id)
    clock = iter([0.0, 100.0, 200.0])
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t",
                        sleep=_no_sleep, monotonic=lambda: next(clock))
    assert result.expected_roots == 2
    assert result.fetched_roots == 1        # ONE distinct task identity, not two raw leads
    assert result.complete is False         # t1 genuinely missing -> partial


def test_fetch_session_traces_paginates_across_pages(atom_home):
    """Real langfuse ``trace.list`` is paginated; ``fetch_session_traces`` must accumulate across
    every non-empty page until an empty page. Single-page fakes never exercised this loop before.
    """
    by_id = {"L0": _lead("L0", "t0"), "L1": _lead("L1", "t1"), "S0": _sub("S0", "t0")}
    client = _FakeClient([[_summary("L0")], [_summary("L1"), _summary("S0")], []], by_id)
    trees = fetch_session_traces(client, "r1")
    assert {t["id"] for t in trees} == {"L0", "L1", "S0"}
    assert client.api.list_calls == 3       # page 1, page 2, empty page 3 -> stop


def test_export_task_unknown_step_and_task_raise_keyerror(atom_home, monkeypatch):
    """Parity with the LangSmith exporter: an unknown step or task raises KeyError (the API maps
    it to 404). The guard is a verbatim copy of export.py's, so lock it independently here.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded"])          # only task "t0" in step 0
    client = _FakeClient([[]], {})
    with pytest.raises(KeyError):
        export_task(str(atom_home), "r1", 0, "ghost", client=client)     # unknown task
    with pytest.raises(KeyError):
        export_task(str(atom_home), "r1", 9, "t0", client=client)        # unknown step


class _LaggyAPI:
    """Ingestion-lag fake: each successive WHOLE-session poll (a fresh page-1 scan) returns more
    traces. ``page_sets[i]`` is the list of pages returned on the i-th session poll."""

    def __init__(self, page_sets, by_id):
        self._sets = list(page_sets)
        self._by_id = by_id
        self._poll = -1
        self.list_calls = 0

    class _NS:
        def __init__(self, outer): self._o = outer

        def list(self, session_id, page=1):
            o = self._o
            o.list_calls += 1
            if page == 1:                       # a new session scan begins -> advance the poll
                o._poll = min(o._poll + 1, len(o._sets) - 1)
            pages = o._sets[o._poll]
            idx = page - 1
            return _Page(pages[idx] if idx < len(pages) else [])

        def get(self, trace_id):
            return self._o._by_id[trace_id]

    @property
    def trace(self):
        return _LaggyAPI._NS(self)


class _LaggyClient:
    def __init__(self, page_sets, by_id):
        self.api = _LaggyAPI(page_sets, by_id)


def test_export_run_retries_until_complete_under_ingestion_lag(atom_home, monkeypatch):
    """The poll loop must RE-FETCH under ingestion lag: an incomplete first poll followed by a
    complete second poll yields complete=True. Mirrors the LangSmith ingestion-lag test; the
    page-indexed fake could never return more on a later poll, so this path was unverified.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])     # expected = 2
    client = _LaggyClient(
        page_sets=[
            [[_summary("L0")], []],                       # poll 1: only t0's lead (t1 lagging)
            [[_summary("L0"), _summary("L1")], []],       # poll 2+: both leads present
        ],
        by_id={"L0": _lead("L0", "t0"), "L1": _lead("L1", "t1")},
    )
    result = export_run(str(atom_home), "r1", client=client, now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 2
    assert client.api.list_calls >= 4       # poll 1 (page1+empty) + poll 2 (page1+empty) -> re-fetched


# --- resilient fetch: paginated fallback for oversized traces ---------------

import types


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
