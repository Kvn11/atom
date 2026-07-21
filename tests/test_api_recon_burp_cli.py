import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import burp  # noqa: E402
import _secassess_fixtures as fx  # noqa: E402


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    return fx.write_capture(tmp_path_factory.mktemp("cap"))


def _run(*args):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "burp.py"), *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_index_apis_only_hides_assets(capture):
    rows = json.loads(_run("index", capture, "--apis-only", "--format", "json"))
    assert {r["index"] for r in rows} == {2, 3}   # the two static assets are hidden


def test_index_without_filter_lists_all(capture):
    rows = json.loads(_run("index", capture, "--format", "json"))
    assert len(rows) == 4


def test_index_redacts_jwt_in_url(capture):
    out = _run("index", capture, "--format", "json")
    assert fx.SAMPLE_JWT not in out       # the token embedded in item 2's URL is redacted
    assert "JWT redacted" in out


def test_hosts_lists_the_domain(capture):
    assert "api.example.com" in _run("hosts", capture)


def test_view_body_is_truncated(capture):
    # item 2's HTML response is > 200 chars; --resp-body --limit 200 must truncate
    out = _run("view", capture, "--index", "2", "--resp-body", "--limit", "200")
    assert "truncated" in out
    assert "<!doctype html>" in out.lower()


def test_view_keys_shows_json_shape_not_full_body(capture):
    # item 3 has a JSON response; --keys shows the key tree
    out = _run("view", capture, "--index", "3", "--resp-body", "--keys")
    assert "token" in out and "userId" in out


def test_view_decode_auth_finds_jwt_claims_not_raw_token(capture):
    out = _run("view", capture, "--index", "2", "--decode-auth")
    assert "55501234" in out          # aud claim surfaced
    assert "HS256" in out             # alg surfaced
    assert "sig_bytes" in out
    assert fx.SAMPLE_JWT not in out   # raw token never printed


def test_harvest_surfaces_reusable_values(capture):
    data = burp.harvest(capture)
    assert "api.example.com" in data["hosts"]
    blob = json.dumps(data)
    assert "55501234" in blob          # captured via the decoded JWT aud claim
    assert isinstance(data["request_headers"], dict)
    assert isinstance(data["jwts"], list) and data["jwts"]
    # cookies from the POST request are harvested
    assert "session" in data["cookies"]
