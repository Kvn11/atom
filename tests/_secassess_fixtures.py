"""Self-contained fixtures for the api-security-assessment tooling tests.

The real inputs under examples/ are gitignored (local-only, possibly sensitive), so these
tests build a tiny synthetic Burp XML capture + targets file at runtime instead. Not a test
module itself (no test_ prefix) — imported by the test files.
"""
from __future__ import annotations

import base64
import json


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    sig = _b64url(b"\x00" * 32)  # 32-byte signature -> sig_bytes == 32 (HS256)
    return f"{header}.{body}.{sig}"


# A deterministic JWT with a recognizable audience id (55501234) and alg=HS256.
SAMPLE_JWT = _make_jwt({
    "iss": "example.com",
    "aud": "55501234",
    "terminalId": "abc123def456",
    "exp": 1779676117243,
    "iat": 1779675217243,
    "jti": "deadbeefcafe",
})

# A second in-scope identity (different audience id) for cross-user/roster tests.
SAMPLE_JWT_2 = _make_jwt({
    "iss": "example.com",
    "aud": "99902222",
    "terminalId": "zzz999yyy888",
    "exp": 1779676117243,
    "iat": 1779675217243,
    "jti": "feedfacecafe",
})


def _item(url, host, method, path, ext, status, mime, request_raw, response_raw) -> str:
    req_b64 = base64.b64encode(request_raw).decode()
    resp_b64 = base64.b64encode(response_raw).decode()
    return (
        "  <item>\n"
        "    <time>Sun May 24 22:14:16 EDT 2026</time>\n"
        f"    <url><![CDATA[{url}]]></url>\n"
        f'    <host ip="1.2.3.4">{host}</host>\n'
        "    <port>443</port>\n"
        "    <protocol>https</protocol>\n"
        f"    <method><![CDATA[{method}]]></method>\n"
        f"    <path><![CDATA[{path}]]></path>\n"
        f"    <extension>{ext}</extension>\n"
        f'    <request base64="true">{req_b64}</request>\n'
        f"    <status>{status}</status>\n"
        f"    <responselength>{len(response_raw)}</responselength>\n"
        f"    <mimetype>{mime}</mimetype>\n"
        f'    <response base64="true">{resp_b64}</response>\n'
        "    <comment></comment>\n"
        "  </item>\n"
    )


def build_capture_xml() -> str:
    """A 4-item capture: 2 static assets (noise) + 2 API items (1 doc w/ JWT-in-URL, 1 JSON POST)."""
    host = "api.example.com"
    long_html = (
        "<!doctype html>\n<html><head><title>Example</title></head><body>"
        + ("<p>filler content to exceed the truncation limit.</p>" * 12)
        + "</body></html>"
    )
    items = [
        # 0 — static asset (js)
        _item(
            f"https://{host}/assets/app.js", host, "GET", "/assets/app.js", "js", "200", "script",
            b"GET /assets/app.js HTTP/1.1\r\nHost: api.example.com\r\n\r\n",
            b"HTTP/2 200 OK\r\nContent-Type: application/javascript\r\n\r\nconsole.log(1)",
        ),
        # 1 — static asset (css)
        _item(
            f"https://{host}/assets/style.css", host, "GET", "/assets/style.css", "css", "200", "css",
            b"GET /assets/style.css HTTP/1.1\r\nHost: api.example.com\r\n\r\n",
            b"HTTP/2 200 OK\r\nContent-Type: text/css\r\n\r\nbody{margin:0}",
        ),
        # 2 — API document: JWT rides in the query as authorizeCode (digits-prefixed)
        _item(
            f"https://{host}/?action=delAccount&authorizeCode=30410011{SAMPLE_JWT}&lang=en",
            host, "GET", "/", "", "200", "HTML",
            b"GET /?action=delAccount&authorizeCode=30410011" + SAMPLE_JWT.encode()
            + b"&lang=en HTTP/1.1\r\nHost: api.example.com\r\nUser-Agent: Mozilla/5.0\r\n\r\n",
            b"HTTP/2 200 OK\r\nContent-Type: text/html\r\n\r\n" + long_html.encode(),
        ),
        # 3 — JSON POST API with Bearer JWT + cookies
        _item(
            f"https://{host}/api/v1/login", host, "POST", "/api/v1/login", "", "200", "JSON",
            b"POST /api/v1/login HTTP/1.1\r\nHost: api.example.com\r\n"
            b"Authorization: Bearer " + SAMPLE_JWT.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Cookie: session=abc123; csrf=xyz789\r\n\r\n"
            b'{"email":"a@b.com","password":"secret"}',
            b"HTTP/2 200 OK\r\nContent-Type: application/json\r\n\r\n"
            b'{"token":"t","userId":55501234}',
        ),
    ]
    return (
        '<?xml version="1.0"?>\n'
        '<items burpVersion="2026.4.3" exportTime="Sun May 24 22:26:26 EDT 2026">\n'
        + "".join(items)
        + "</items>\n"
    )


def build_capture_xml_multi() -> str:
    """Base capture + one more API item authenticated as a SECOND identity (aud 99902222)."""
    extra = _item(
        "https://api.example.com/api/v1/orders", "api.example.com", "GET", "/api/v1/orders", "",
        "200", "JSON",
        b"GET /api/v1/orders HTTP/1.1\r\nHost: api.example.com\r\n"
        b"Authorization: Bearer " + SAMPLE_JWT_2.encode() + b"\r\n\r\n",
        b'HTTP/2 200 OK\r\nContent-Type: application/json\r\n\r\n{"orders":[]}',
    )
    return build_capture_xml().replace("</items>\n", extra + "</items>\n")


def write_capture(dir_path) -> str:
    p = dir_path / "sample_capture.xml"
    p.write_text(build_capture_xml(), encoding="utf-8")
    return str(p)


SAMPLE_TARGETS = {
    "domain": "my.api.com",
    "api": [
        {"method": "POST", "path": "/some/api/*/?x={}",
         "request": "{'foo':'int' #optional,'bar':'str' #required}", "response": "{'zoo':'int'}"},
        {"method": "GET", "path": "/some/api/user/{id}",
         "request": "{'id':'int' #required}",
         "response": "{'id':'int','email':'str','verified':'bool'}"},
    ],
}
