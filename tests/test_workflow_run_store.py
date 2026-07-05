"""Run manifest persistence (atomic) and chat snapshots."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from atom.workflow.run_store import (
    ArtifactRef, RunManifest, RunStore, StepState, TaskState, serialize_messages,
)


def _manifest(run_id, ws):
    return RunManifest(
        run_id=run_id, workflow="demo", inputs={"topic": "x"},
        created_at="2026-07-03T00:00:00", workspace_path=str(ws),
        steps=[StepState(index=0, title="Draft",
                         tasks=[TaskState(id="t1", thread_id=f"{run_id}:s0:t1")])],
    )


def test_create_and_load_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    m = store.create(_manifest("r1", store.workspace_dir("r1")))
    assert store.workspace_dir("r1").is_dir()
    loaded = store.load("r1")
    assert loaded.run_id == "r1"
    assert loaded.steps[0].tasks[0].thread_id == "r1:s0:t1"


def test_save_is_atomic_no_tmp_left(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("r2", store.workspace_dir("r2")))
    m = store.load("r2")
    m.status = "running"
    store.save(m)
    assert store.load("r2").status == "running"
    leftovers = list(store.run_dir("r2").glob("*.tmp"))
    assert leftovers == []


def test_list_sorted_desc(atom_home):
    store = RunStore(str(atom_home))
    a = _manifest("ra", store.workspace_dir("ra")); a.created_at = "2026-07-01T00:00:00"
    b = _manifest("rb", store.workspace_dir("rb")); b.created_at = "2026-07-02T00:00:00"
    store.create(a); store.create(b)
    assert [m.run_id for m in store.list()] == ["rb", "ra"]


def test_chat_snapshot_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("r3", store.workspace_dir("r3")))
    assert store.load_chat("r3", 0, "t1") is None
    store.save_chat("r3", 0, "t1", [{"role": "ai", "text": "hi"}])
    assert store.load_chat("r3", 0, "t1") == [{"role": "ai", "text": "hi"}]


def test_serialize_messages_shape():
    msgs = [
        HumanMessage(content="do it"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "p"}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="c1", name="write_file"),
        AIMessage(content="done"),
    ]
    out = serialize_messages(msgs)
    assert out[0] == {"role": "human", "text": "do it"}
    assert out[1]["tool_calls"] == [{"name": "write_file", "args": {"path": "p"}}]
    assert out[2]["role"] == "tool" and out[2]["name"] == "write_file"
    assert out[3]["text"] == "done"


def test_capture_artifacts_copies_and_snapshots(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rc", store.workspace_dir("rc")))
    src = store.workspace_dir("rc") / "poem.md"
    src.write_text("draft\n")
    refs = store.capture_artifacts(
        "rc", 0, "poet_a",
        [{"path": "/mnt/user-data/outputs/poem.md", "physical": str(src)}],
    )
    assert len(refs) == 1
    assert refs[0].name == "poem.md"
    assert refs[0].rel == "s0__poet_a/poem.md"
    assert refs[0].size == len("draft\n")
    dest = store.artifacts_dir("rc") / "s0__poet_a" / "poem.md"
    assert dest.read_text() == "draft\n"
    src.write_text("CHANGED\n")               # snapshot immutability
    assert dest.read_text() == "draft\n"


def test_capture_artifacts_skips_missing_source(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rm", store.workspace_dir("rm")))
    refs = store.capture_artifacts(
        "rm", 0, "t1",
        [{"path": "/mnt/x/gone.md", "physical": str(store.workspace_dir("rm") / "nope.md")}],
    )
    assert refs == []


def test_capture_artifacts_disambiguates_collision(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rd", store.workspace_dir("rd")))
    a = store.workspace_dir("rd") / "a.md"; a.write_text("A\n")
    sub = store.workspace_dir("rd") / "sub"; sub.mkdir()
    b = sub / "a.md"; b.write_text("B\n")
    refs = store.capture_artifacts("rd", 0, "t1", [
        {"path": "/mnt/x/a.md", "physical": str(a)},
        {"path": "/mnt/y/a.md", "physical": str(b)},
    ])
    assert sorted(r.name for r in refs) == ["a-1.md", "a.md"]


def test_artifact_path_confined(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rp", store.workspace_dir("rp")))
    ok = store.artifact_path("rp", "s0__t1/f.md")
    assert ok is not None and str(ok).endswith("/artifacts/s0__t1/f.md")
    assert store.artifact_path("rp", "../../run.json") is None
