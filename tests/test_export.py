"""LangSmith run exporter: pure helpers + the fetch/poll pipeline with an injected fake client."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atom.observability.export import (
    ExportResult,
    build_envelope,
    expected_root_count,
    export_run,
    export_task,
    fetch_run_tree,
    fetch_task_tree,
    resolve_run_ids,
)
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState


def _manifest(run_id: str, statuses: list[str]) -> RunManifest:
    tasks = [
        TaskState(id=f"t{i}", thread_id=f"{run_id}:s0:t{i}", status=st)
        for i, st in enumerate(statuses)
    ]
    return RunManifest(
        run_id=run_id, workflow="wf", created_at="2026-07-09T00:00:00",
        workspace_path="/tmp/ws", steps=[StepState(index=0, title="S", tasks=tasks)],
    )


def test_expected_root_count_skips_pending():
    m = _manifest("r1", ["succeeded", "failed", "running", "pending"])
    assert expected_root_count(m) == 3  # pending excluded


def test_expected_root_count_all_pending_is_zero():
    assert expected_root_count(_manifest("r1", ["pending", "pending"])) == 0


def test_build_envelope_shape():
    m = _manifest("r1", ["succeeded"])
    roots = [{"id": "root1", "child_runs": [{"run_type": "llm"}]}]
    env = build_envelope(
        "r1", "wf", "proj", m, roots,
        complete=True, expected=1, fetched=1, now="2026-07-09T12:00:00",
    )
    assert env["run_id"] == "r1" and env["workflow"] == "wf" and env["project"] == "proj"
    assert env["exported_at"] == "2026-07-09T12:00:00"
    assert env["complete"] is True and env["expected_roots"] == 1 and env["fetched_roots"] == 1
    assert env["roots"] == roots
    assert env["scope"] == "run" and env["task_id"] is None   # run-wide export
    assert env["atom_manifest"]["run_id"] == "r1"          # manifest embedded verbatim
    assert env["atom_manifest"]["steps"][0]["tasks"][0]["status"] == "succeeded"
    # Whole envelope must be JSON-serializable.
    assert json.loads(json.dumps(env))["run_id"] == "r1"


def test_build_envelope_task_scope():
    m = _manifest("r1", ["succeeded"])
    roots = [{"id": "root0"}]
    env = build_envelope(
        "r1", "wf", "proj", m, roots, complete=True, expected=1, fetched=1,
        now="t", task_id="t0", session_id="r1:s0:t0",
    )
    assert env["scope"] == "task"
    assert env["task_id"] == "t0" and env["session_id"] == "r1:s0:t0"


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


def test_export_result_is_a_dataclass():
    r = ExportResult(run_id="r1", path="/x/export.json", complete=True,
                     expected_roots=1, fetched_roots=1)
    assert r.run_id == "r1" and r.complete is True


class _FakeRun:
    def __init__(self, id, dump):
        self.id = id
        self._dump = dump

    def model_dump(self, mode="python"):
        return dict(self._dump)


class _FakeClient:
    """Scripts successive list_runs() results (to simulate async-ingestion lag) and per-id child trees."""
    def __init__(self, list_sequence, children):
        self._seq = list(list_sequence)     # e.g. [["root1"], ["root1", "root2"]]
        self._children = children           # {"root1": {...full dump...}, ...}
        self.list_calls = 0
        self.filters = []

    def list_runs(self, project_name, is_root, filter):
        self.filters.append(filter)
        idx = min(self.list_calls, len(self._seq) - 1)
        self.list_calls += 1
        return iter([_FakeRun(rid, {"id": rid}) for rid in self._seq[idx]])

    def read_run(self, run_id, load_child_runs):
        assert load_child_runs is True
        return _FakeRun(run_id, self._children[run_id])


def _store_with_run(atom_home, run_id, statuses):
    store = RunStore(str(atom_home))
    store.create(_manifest(run_id, statuses).model_copy(update={
        "workspace_path": str(store.workspace_dir(run_id))
    }))
    return store


def _no_sleep(_s):  # deterministic tests: never actually sleep
    pass


def test_fetch_run_tree_hydrates_children():
    client = _FakeClient(
        [["root1", "root2"]],
        {"root1": {"id": "root1", "child_runs": [{"run_type": "llm"}]},
         "root2": {"id": "root2", "child_runs": []}},
    )
    trees = fetch_run_tree(client, "proj", "r1")
    assert [t["id"] for t in trees] == ["root1", "root2"]
    assert trees[0]["child_runs"][0]["run_type"] == "llm"
    assert 'run_id' in client.filters[0] and 'r1' in client.filters[0]  # filtered by run_id metadata


def test_export_run_happy_path(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient(
        [["root1", "root2"]],
        {"root1": {"id": "root1", "child_runs": [{"run_type": "llm", "outputs": {"thinking": "…"}}]},
         "root2": {"id": "root2", "child_runs": []}},
    )
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "2026-07-09T12:00:00", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 2 and result.expected_roots == 2
    env = json.loads(Path(result.path).read_text())
    assert env["run_id"] == "r1" and len(env["roots"]) == 2
    assert env["roots"][0]["child_runs"][0]["outputs"]["thinking"] == "…"  # reasoning present
    assert env["atom_manifest"]["run_id"] == "r1"


def test_export_run_polls_through_ingestion_lag(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient(
        [["root1"], ["root1", "root2"]],   # 1 root first, both on the second poll
        {"root1": {"id": "root1"}, "root2": {"id": "root2"}},
    )
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 2
    assert client.list_calls == 2  # it polled again after the short first result


def test_export_run_partial_on_timeout(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient([["root1"]], {"root1": {"id": "root1"}})  # only ever 1 of 2
    clock = iter([0.0, 100.0, 200.0])  # deadline=0+30=30; second read (100) >= 30 -> stop
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep, monotonic=lambda: next(clock),
                        poll_timeout=30.0)
    assert result.complete is False and result.fetched_roots == 1 and result.expected_roots == 2
    env = json.loads(Path(result.path).read_text())
    assert env["complete"] is False  # eval pipeline can see the truncation


def test_export_run_no_traces_writes_nothing(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    store = _store_with_run(atom_home, "r1", ["succeeded"])
    client = _FakeClient([[]], {})     # tracing was off during the run -> nothing in LangSmith
    clock = iter([0.0, 100.0])
    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep, monotonic=lambda: next(clock))
    assert result.fetched_roots == 0 and result.path == ""
    assert not (store.run_dir("r1") / "export.json").exists()  # no misleading empty artifact


def test_export_run_requires_api_key(atom_home, monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    _store_with_run(atom_home, "r1", ["succeeded"])
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        export_run(str(atom_home), "r1", project="proj", client=_FakeClient([[]], {}))


def test_export_run_unknown_run(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    with pytest.raises(FileNotFoundError):
        export_run(str(atom_home), "nope", project="proj", client=_FakeClient([[]], {}))


def test_fetch_task_tree_filters_by_session_id():
    client = _FakeClient(
        [["root0"]], {"root0": {"id": "root0", "child_runs": [{"run_type": "llm"}]}},
    )
    trees = fetch_task_tree(client, "proj", "r1:s0:t0")
    assert [t["id"] for t in trees] == ["root0"]
    assert trees[0]["child_runs"][0]["run_type"] == "llm"     # sub-agent tree hydrated
    assert 'session_id' in client.filters[0] and 'r1:s0:t0' in client.filters[0]


def test_export_task_happy_path(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded", "succeeded"])
    client = _FakeClient(
        [["root0"]],
        {"root0": {"id": "root0", "child_runs": [{"run_type": "llm", "outputs": {"thinking": "…"}}]}},
    )
    result = export_task(str(atom_home), "r1", 0, "t0", project="proj", client=client,
                         now=lambda: "2026-07-13T12:00:00", sleep=_no_sleep)
    assert result.task_id == "t0" and result.complete is True
    assert result.fetched_roots == 1 and result.expected_roots == 1
    # filtered by the task's own session_id (thread_id), not the run-wide run_id
    assert 'session_id' in client.filters[0] and 'r1:s0:t0' in client.filters[0]
    assert result.path.endswith("exports/s0__t0.json")
    env = json.loads(Path(result.path).read_text())
    assert env["scope"] == "task" and env["task_id"] == "t0" and env["session_id"] == "r1:s0:t0"
    assert env["roots"][0]["child_runs"][0]["outputs"]["thinking"] == "…"   # reasoning present


def test_export_task_failed_task_is_exportable(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["failed"])
    client = _FakeClient([["root0"]], {"root0": {"id": "root0"}})
    result = export_task(str(atom_home), "r1", 0, "t0", project="proj", client=client,
                         now=lambda: "t", sleep=_no_sleep)
    assert result.complete is True and result.fetched_roots == 1   # failed traces still export


def test_export_task_rejects_non_terminal(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["running", "succeeded"])
    with pytest.raises(ValueError, match="not completed"):
        export_task(str(atom_home), "r1", 0, "t0", project="proj", client=_FakeClient([[]], {}))


def test_export_task_unknown_task_and_step(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded"])
    with pytest.raises(KeyError):
        export_task(str(atom_home), "r1", 0, "nope", project="proj", client=_FakeClient([[]], {}))
    with pytest.raises(KeyError):
        export_task(str(atom_home), "r1", 9, "t0", project="proj", client=_FakeClient([[]], {}))


def test_export_task_no_traces_writes_nothing(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    store = _store_with_run(atom_home, "r1", ["succeeded"])
    client = _FakeClient([[]], {})     # tracing was off -> nothing in LangSmith
    clock = iter([0.0, 100.0])
    result = export_task(str(atom_home), "r1", 0, "t0", project="proj", client=client,
                         now=lambda: "t", sleep=_no_sleep, monotonic=lambda: next(clock))
    assert result.fetched_roots == 0 and result.path == "" and result.task_id == "t0"
    assert not (store.run_dir("r1") / "exports" / "s0__t0.json").exists()


def test_export_task_requires_api_key(atom_home, monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    _store_with_run(atom_home, "r1", ["succeeded"])
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        export_task(str(atom_home), "r1", 0, "t0", project="proj", client=_FakeClient([[]], {}))


def test_export_task_unknown_run(atom_home, monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    with pytest.raises(FileNotFoundError):
        export_task(str(atom_home), "nope", 0, "t0", project="proj", client=_FakeClient([[]], {}))


def test_resolve_run_ids_selectors(atom_home):
    store = RunStore(str(atom_home))
    for rid, wf, created in [("a", "alpha", "2026-07-09T01"), ("b", "alpha", "2026-07-09T03"),
                             ("c", "beta", "2026-07-09T02")]:
        m = _manifest(rid, ["succeeded"]).model_copy(update={"workflow": wf, "created_at": created,
                                                             "workspace_path": str(store.workspace_dir(rid))})
        store.create(m)
    assert resolve_run_ids(str(atom_home), run_id="a") == ["a"]
    assert resolve_run_ids(str(atom_home), latest="alpha") == ["b"]          # newest of alpha
    assert resolve_run_ids(str(atom_home), all_workflow="alpha") == ["b", "a"]  # all alpha, newest-first
    with pytest.raises(ValueError):
        resolve_run_ids(str(atom_home))                                     # zero selectors
    with pytest.raises(ValueError):
        resolve_run_ids(str(atom_home), run_id="a", latest="alpha")         # two selectors
    with pytest.raises(ValueError, match="no runs found"):
        resolve_run_ids(str(atom_home), latest="ghost")


def test_export_run_streams_write_without_json_dumps(atom_home, monkeypatch):
    # The write path must stream via json.dump(fp), never buffer the whole export through
    # json.dumps(...) — patch json.dumps to explode and assert the export still writes.
    monkeypatch.setenv("LANGSMITH_API_KEY", "k")
    _store_with_run(atom_home, "r1", ["succeeded"])
    client = _FakeClient([["root1"]], {"root1": {"id": "root1"}})

    import atom.observability.export as exp

    def _boom(*a, **k):
        raise AssertionError("json.dumps used — write is not streaming")
    monkeypatch.setattr(exp.json, "dumps", _boom)

    result = export_run(str(atom_home), "r1", project="proj", client=client,
                        now=lambda: "t", sleep=_no_sleep)
    env = json.loads(Path(result.path).read_text())   # wrote successfully, no json.dumps
    assert env["run_id"] == "r1" and env["roots"][0]["id"] == "root1"
