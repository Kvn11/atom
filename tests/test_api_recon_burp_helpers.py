import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import _burp  # noqa: E402
import _secassess_fixtures as fx  # noqa: E402

# The synthetic capture's JWT (alg=HS256, aud=55501234).
SAMPLE_JWT = fx.SAMPLE_JWT


def test_endpoint_slug_basic():
    assert _burp.endpoint_slug("POST", "/api/v1/users") == "post_api_v1_users"
    assert _burp.endpoint_slug("GET", "/") == "get_root"
    # query string is dropped
    assert _burp.endpoint_slug("GET", "/?action=delAccount&x=1") == "get_root"
    # wildcards / punctuation collapse to single underscores
    assert _burp.endpoint_slug("POST", "/some/api/*/?x={}") == "post_some_api"


def test_decode_jwt_claims_no_raw_token():
    d = _burp.decode_jwt(SAMPLE_JWT)
    assert d is not None
    assert d["alg"] == "HS256"
    assert d["payload"]["aud"] == "55501234"
    assert d["payload"]["iss"] == "example.com"
    assert d["sig_bytes"] == 32  # HS256 signature is 32 bytes
    # the raw token must never be echoed back inside the decoded structure
    assert SAMPLE_JWT not in repr(d)


def test_decode_jwt_rejects_garbage():
    assert _burp.decode_jwt("not.a.jwt") is None
    assert _burp.decode_jwt("") is None


def test_find_jwts_extracts_from_noisy_text():
    # the capture prefixes the JWT with digits ("30410011eyJ...") — still found
    found = _burp.find_jwts("authorizeCode=30410011" + SAMPLE_JWT + "&lang=en")
    assert SAMPLE_JWT in found


def test_is_asset_filters_static_but_keeps_apis(tmp_path):
    xml = fx.write_capture(tmp_path)
    items = list(_burp.iter_items(xml))
    assert len(items) == 4
    assets = [it for it in items if _burp.is_asset(it)]
    apis = [it for it in items if not _burp.is_asset(it)]
    # 2 static assets (js/css) filtered out; 2 API items kept (the HTML doc + the JSON POST)
    assert len(assets) == 2
    assert {it.index for it in assets} == {0, 1}
    assert {it.index for it in apis} == {2, 3}
