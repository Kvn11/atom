import json
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import burp  # noqa: E402
import _secassess_fixtures as fx  # noqa: E402


def _run(*args):
    proc = subprocess.run([sys.executable, str(SCRIPTS / "burp.py"), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_identities_groups_one_account_by_subject(tmp_path):
    xml = fx.write_capture(tmp_path)              # 1 identity (aud 55501234) across items 2 & 3
    ids = burp.identities(xml)
    assert len(ids) == 1
    ent = ids[0]
    assert "55501234" in ent["user_ids"]
    assert ent["auth"]["alg"] == "HS256"
    assert "session" in ent["cookie_names"]       # from the POST request's Cookie header
    assert sorted(ent["source_indices"]) == [2, 3]
    assert fx.SAMPLE_JWT not in json.dumps(ent)    # redacted: claims only, never the raw token


def test_identities_finds_two_distinct_accounts(tmp_path):
    p = tmp_path / "multi.xml"
    p.write_text(fx.build_capture_xml_multi(), encoding="utf-8")
    ids = burp.identities(str(p))
    keys = {u for ent in ids for u in ent["user_ids"]}
    assert {"55501234", "99902222"} <= keys
    assert len(ids) == 2


def test_cred_prints_raw_authorization_for_dollar_capture(tmp_path):
    xml = fx.write_capture(tmp_path)
    out = _run("cred", xml, "--index", "3", "--field", "authorization")
    # cred is the ONE deliberate raw path (for TOKEN=$(...) use) — it must emit the real token.
    assert fx.SAMPLE_JWT in out
    assert out.startswith("Bearer ")
