import importlib.util
import json
import pathlib
import subprocess
import sys

SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("find_disconnected_pages", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Fixture mirrors the design-time probe graph: a 3-page cycle (main graph),
# a 2-page island, and one link-less orphan page.
PAGES = ["Alpha", "Beta", "Gamma", "IslandX", "IslandY", "Lonely"]
EDGES = [
    ("Alpha", "Beta"), ("Beta", "Gamma"), ("Gamma", "Alpha"),
    ("IslandX", "IslandY"), ("IslandY", "IslandX"),
]


def test_analyze_partitions_main_island_and_isolated():
    mod = _load()
    report = mod.analyze(PAGES, EDGES)
    assert report["note_count"] == 6
    assert report["component_count"] == 3
    assert report["main_component"]["size"] == 3
    assert set(report["main_component"]["members"]) == {"Alpha", "Beta", "Gamma"}
    assert report["islands"] == [{"size": 2, "members": ["IslandX", "IslandY"]}]
    assert report["isolated"] == ["Lonely"]


def test_analyze_orphans_and_deadends_are_directed():
    mod = _load()
    report = mod.analyze(PAGES, EDGES)
    # Every page in the two cycles has both an inbound and an outbound edge;
    # only Lonely (no edges at all) is both an orphan and a dead-end.
    assert report["orphans"] == ["Lonely"]
    assert report["deadends"] == ["Lonely"]


def test_analyze_empty_graph():
    mod = _load()
    report = mod.analyze([], [])
    assert report["note_count"] == 0
    assert report["component_count"] == 0
    assert report["main_component"] == {"size": 0, "members": []}
    assert report["islands"] == []
    assert report["isolated"] == []


def test_cli_reads_logseq_query_result_shape(tmp_path):
    # Accepts the raw `logseq query --output json` envelope, not just bare lists.
    pages_file = tmp_path / "pages.json"
    edges_file = tmp_path / "edges.json"
    pages_file.write_text(json.dumps({"data": {"result": [[p, 1] for p in PAGES]}}))
    edges_file.write_text(json.dumps({"data": {"result": [list(e) for e in EDGES]}}))
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--pages-json", str(pages_file),
         "--edges-json", str(edges_file), "--json"],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(out.stdout)
    assert report["component_count"] == 3
    assert report["isolated"] == ["Lonely"]
