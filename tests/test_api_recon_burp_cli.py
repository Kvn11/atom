import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
EXAMPLE_XML = str(ROOT / "examples" / "account.vesync.com.xml")

import burp  # noqa: E402


def _run(*args):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "burp.py"), *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_index_apis_only_hides_assets():
    out = _run("index", EXAMPLE_XML, "--apis-only", "--format", "json")
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["index"] == 22
    assert rows[0]["method"] == "GET"


def test_index_without_filter_lists_all():
    rows = json.loads(_run("index", EXAMPLE_XML, "--format", "json"))
    assert len(rows) == 23


def test_hosts_lists_the_single_domain():
    out = _run("hosts", EXAMPLE_XML)
    assert "account.vesync.com" in out


def test_view_keys_does_not_dump_full_body():
    # response is a 1.2KB HTML doc; --resp-body --limit 200 must truncate
    out = _run("view", EXAMPLE_XML, "--index", "22", "--resp-body", "--limit", "200")
    assert "truncated" in out
    assert "<!doctype html>" in out.lower()


def test_view_decode_auth_finds_jwt_claims_not_raw_token():
    out = _run("view", EXAMPLE_XML, "--index", "22", "--decode-auth")
    assert "22134806" in out          # aud claim surfaced
    assert "HS256" in out             # alg surfaced
    assert "sig_bytes" in out or "signature" in out.lower()


def test_harvest_surfaces_reusable_values():
    data = burp.harvest(EXAMPLE_XML)
    assert "account.vesync.com" in data["hosts"]
    # the account id from the JWT aud claim is captured as a reusable identifier or claim
    blob = json.dumps(data)
    assert "22134806" in blob
    # cookies/headers sections exist (may be empty for this capture) and are lists/dicts
    assert isinstance(data["request_headers"], dict)
    assert isinstance(data["jwts"], list)
