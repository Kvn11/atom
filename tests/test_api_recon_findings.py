import json
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import findings  # noqa: E402
import _secassess_fixtures as fx  # noqa: E402


def _cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "findings.py"), *args],
                          capture_output=True, text=True)


def _finding(**over):
    base = {"title": "IDOR reads other user", "description": "returns victim PII",
            "evidence": ["TOKEN=$(x); curl -H \"Authorization: $TOKEN\" https://h/u/2"]}
    base.update(over)
    return base


def test_validate_defaults_confirmed_null():
    out = findings.validate_finding(_finding())
    assert out["confirmed"] is None and out["evidence"]


def test_validate_rejects_bad_fields():
    for bad in [{"title": ""}, {"description": ""}, {"evidence": []}, {"evidence": "x"},
                {"evidence": [""]}]:
        try:
            findings.validate_finding(_finding(**bad))
            assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass


def test_has_raw_jwt_flags_evidence():
    assert findings.has_raw_jwt(_finding(evidence=[f"curl -H 'Authorization: Bearer {fx.SAMPLE_JWT}'"])) == "evidence"
    assert findings.has_raw_jwt(_finding()) is None


def test_read_jsonl_missing_is_empty(tmp_path):
    assert findings.read_jsonl(str(tmp_path / "nope.jsonl")) == []


def test_add_appends_and_defaults(tmp_path):
    fj = tmp_path / "f.json"; jl = tmp_path / "findings.jsonl"
    fj.write_text(json.dumps(_finding()), encoding="utf-8")
    r = _cli("add", "--from", str(fj), "--to", str(jl))
    assert r.returncode == 0 and r.stdout.startswith("OK: added")
    rows = findings.read_jsonl(str(jl))
    assert len(rows) == 1 and rows[0]["confirmed"] is None


def test_add_rejects_raw_jwt(tmp_path):
    fj = tmp_path / "f.json"; jl = tmp_path / "findings.jsonl"
    fj.write_text(json.dumps(_finding(evidence=[f"curl -H 'Authorization: Bearer {fx.SAMPLE_JWT}'"])), encoding="utf-8")
    r = _cli("add", "--from", str(fj), "--to", str(jl))
    assert r.returncode != 0 and "evidence" in r.stderr
    assert findings.read_jsonl(str(jl)) == []


def test_add_flock_no_lost_update(tmp_path):
    jl = tmp_path / "findings.jsonl"
    procs = []
    for i in range(2):
        fj = tmp_path / f"f{i}.json"
        fj.write_text(json.dumps(_finding(title=f"F{i}")), encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, str(SCRIPTS / "findings.py"),
                                       "add", "--from", str(fj), "--to", str(jl)]))
    assert all(p.wait() == 0 for p in procs)
    assert len(findings.read_jsonl(str(jl))) == 2


def test_list_slices_without_dumping(tmp_path):
    jl = tmp_path / "findings.jsonl"
    findings.append_jsonl(str(jl), findings.validate_finding(_finding(description="SECRET-DESC")))
    r = _cli("list", str(jl))
    assert r.returncode == 0 and "SECRET-DESC" not in r.stdout and "IDOR reads other user" in r.stdout


def test_show_full(tmp_path):
    jl = tmp_path / "findings.jsonl"
    findings.append_jsonl(str(jl), findings.validate_finding(_finding(description="SEEME")))
    r = _cli("show", str(jl), "--index", "0")
    assert r.returncode == 0 and "SEEME" in r.stdout


def test_confirm_copies_with_true(tmp_path):
    raw = tmp_path / "raw.jsonl"; conf = tmp_path / "confirmed.jsonl"
    findings.append_jsonl(str(raw), findings.validate_finding(_finding(title="keep me")))
    r = _cli("confirm", "--from", str(raw), "--index", "0", "--to", str(conf))
    assert r.returncode == 0 and r.stdout.startswith("OK: confirmed")
    rows = findings.read_jsonl(str(conf))
    assert rows[0]["confirmed"] is True and rows[0]["title"] == "keep me"


def test_discard_records_reason_and_redacts_output(tmp_path):
    raw = tmp_path / "raw.jsonl"; disc = tmp_path / "discarded.jsonl"
    out = tmp_path / "out.txt"
    out.write_text(f"HTTP/2 200\nleaked {fx.SAMPLE_JWT}\n", encoding="utf-8")
    findings.append_jsonl(str(raw), findings.validate_finding(_finding(title="drop me")))
    r = _cli("discard", "--from", str(raw), "--index", "0", "--to", str(disc),
             "--reason", "403 for attacker; not reproducible", "--output-from", str(out))
    assert r.returncode == 0 and r.stdout.startswith("OK: discarded")
    row = findings.read_jsonl(str(disc))[0]
    assert row["confirmed"] is False and "not reproducible" in row["reason"]
    assert fx.SAMPLE_JWT not in row["repro_output"] and "JWT redacted" in row["repro_output"]
