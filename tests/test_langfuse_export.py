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


def _lead(id, task): return _Trace(id, {"run_id": "r1", "task_id": task, "agent_role": "lead", "is_subagent": False})
def _sub(id, task): return _Trace(id, {"run_id": "r1", "task_id": task, "agent_role": "subagent", "is_subagent": True})


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
