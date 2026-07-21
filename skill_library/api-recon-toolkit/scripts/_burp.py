"""Shared parser for Burp Suite proxy XML exports.

Burp items contain base64-encoded raw HTTP requests and responses. This module
extracts them, splits headers from bodies, decompresses gzip/deflate/br when
possible, and exposes typed accessors so other scripts (split / view / index)
can stay small.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import zlib
from dataclasses import dataclass
from html import unescape
from typing import Iterator
from urllib.parse import parse_qsl, urlparse
from xml.etree import ElementTree as ET


def _split_head_body(raw: bytes) -> tuple[bytes, bytes]:
    sep = b"\r\n\r\n"
    idx = raw.find(sep)
    if idx == -1:
        sep = b"\n\n"
        idx = raw.find(sep)
        if idx == -1:
            return raw, b""
    return raw[:idx], raw[idx + len(sep):]


def _parse_headers(head: bytes) -> tuple[str, list[tuple[str, str]]]:
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines:
        return "", []
    start = lines[0]
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
    return start, headers


def _decode_body(body: bytes, headers: list[tuple[str, str]]) -> bytes:
    if not body:
        return body
    enc = ""
    for name, value in headers:
        if name.lower() == "content-encoding":
            enc = value.lower().strip()
            break
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc == "br":
            try:
                import brotli  # type: ignore
                return brotli.decompress(body)
            except Exception:
                # brotli not installed — keep the raw compressed bytes so the
                # caller can still see *something* rather than failing the run.
                return body
    except Exception:
        return body
    return body


@dataclass
class HttpMessage:
    start_line: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def cookies(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for k, v in self.headers:
            kl = k.lower()
            if kl == "cookie":
                for part in v.split(";"):
                    part = part.strip()
                    if "=" in part:
                        n, _, val = part.partition("=")
                        out.append((n.strip(), val.strip()))
            elif kl == "set-cookie":
                first = v.split(";", 1)[0].strip()
                if "=" in first:
                    n, _, val = first.partition("=")
                    out.append((n.strip(), val.strip()))
        return out

    def content_type(self) -> str:
        return (self.header("Content-Type") or "").lower()

    def is_json(self) -> bool:
        return "json" in self.content_type()

    def is_text(self) -> bool:
        ct = self.content_type()
        return ct.startswith("text/") or "json" in ct or "xml" in ct or "javascript" in ct or "form-urlencoded" in ct

    def body_text(self, errors: str = "replace") -> str:
        return self.body.decode("utf-8", errors=errors)

    def body_json(self):
        return json.loads(self.body_text())

    def to_wire(self) -> bytes:
        # Body has already been Content-Encoding-decoded, so the original
        # Content-Encoding/Length headers would lie to anyone re-parsing this.
        # Drop the encoding hop hints and recompute the length.
        skip = {"content-encoding", "transfer-encoding", "content-length"}
        parts = [self.start_line.encode("iso-8859-1", errors="replace"), b"\r\n"]
        for k, v in self.headers:
            if k.lower() in skip:
                continue
            parts.append(f"{k}: {v}\r\n".encode("iso-8859-1", errors="replace"))
        parts.append(f"Content-Length: {len(self.body)}\r\n".encode("ascii"))
        parts.append(b"\r\n")
        parts.append(self.body)
        return b"".join(parts)


def parse_http_message(raw: bytes) -> HttpMessage:
    head, body_raw = _split_head_body(raw)
    start, headers = _parse_headers(head)
    body = _decode_body(body_raw, headers)
    return HttpMessage(start_line=start, headers=headers, body=body)


@dataclass
class BurpItem:
    index: int
    source: str
    time: str
    url: str
    host: str
    port: str
    protocol: str
    method: str
    path: str
    extension: str
    status: str
    responselength: str
    mimetype: str
    comment: str
    request_raw: bytes
    response_raw: bytes

    @property
    def request(self) -> HttpMessage:
        return parse_http_message(self.request_raw)

    @property
    def response(self) -> HttpMessage | None:
        if not self.response_raw:
            return None
        return parse_http_message(self.response_raw)

    def url_path(self) -> str:
        return urlparse(self.url).path or "/"

    def query_params(self) -> list[tuple[str, str]]:
        return parse_qsl(urlparse(self.url).query, keep_blank_values=True)


def _text(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text


def _decode_field(elem: ET.Element | None) -> bytes:
    if elem is None or elem.text is None:
        return b""
    text = elem.text
    if elem.attrib.get("base64", "false").lower() == "true":
        try:
            return base64.b64decode(text)
        except Exception:
            return b""
    return unescape(text).encode("utf-8", errors="replace")


def iter_items(xml_path: str) -> Iterator[BurpItem]:
    """Stream items from a Burp XML export without loading the whole tree."""
    context = ET.iterparse(xml_path, events=("end",))
    idx = 0
    for _, elem in context:
        if elem.tag != "item":
            continue
        item = BurpItem(
            index=idx,
            source=xml_path,
            time=_text(elem.find("time")).strip(),
            url=_text(elem.find("url")).strip(),
            host=_text(elem.find("host")).strip(),
            port=_text(elem.find("port")).strip(),
            protocol=_text(elem.find("protocol")).strip(),
            method=_text(elem.find("method")).strip(),
            path=_text(elem.find("path")).strip(),
            extension=_text(elem.find("extension")).strip(),
            status=_text(elem.find("status")).strip(),
            responselength=_text(elem.find("responselength")).strip(),
            mimetype=_text(elem.find("mimetype")).strip(),
            comment=_text(elem.find("comment")).strip(),
            request_raw=_decode_field(elem.find("request")),
            response_raw=_decode_field(elem.find("response")),
        )
        yield item
        idx += 1
        elem.clear()


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def json_keys_summary(value, max_items: int = 20, depth: int = 0, max_depth: int = 2) -> str:
    """Describe the shape of a JSON value without dumping its contents."""
    pad = "  " * depth
    if isinstance(value, dict):
        keys = list(value.keys())
        lines = [f"{pad}object with {len(keys)} key(s):"]
        for k in keys[:max_items]:
            v = value[k]
            lines.append(f"{pad}  {k}: {_short_type(v)}")
            if depth < max_depth and isinstance(v, (dict, list)) and v:
                lines.append(json_keys_summary(v, max_items, depth + 2, max_depth))
        if len(keys) > max_items:
            lines.append(f"{pad}  ... +{len(keys) - max_items} more keys")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = [f"{pad}array with {len(value)} item(s)"]
        if value and depth < max_depth:
            lines.append(f"{pad}  [0]: {_short_type(value[0])}")
            if isinstance(value[0], (dict, list)):
                lines.append(json_keys_summary(value[0], max_items, depth + 2, max_depth))
        return "\n".join(lines)
    return f"{pad}{_short_type(value)}"


def _short_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"bool({value})"
    if isinstance(value, (int, float)):
        return f"{type(value).__name__}({value})"
    if isinstance(value, str):
        s = value if len(value) <= 60 else value[:57] + "..."
        return f"str(len={len(value)}) {s!r}"
    if isinstance(value, list):
        return f"array(len={len(value)})"
    if isinstance(value, dict):
        return f"object(keys={len(value)})"
    return type(value).__name__


_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def safe_path_component(s: str, max_len: int = 60) -> str:
    cleaned = _UNSAFE_PATH_CHARS.sub("", s.replace("/", "_")).strip("._") or "root"
    return cleaned[:max_len]


# --- recon helpers (added for api-recon-toolkit) ----------------------------

import base64 as _base64
from urllib.parse import urlsplit as _urlsplit

# Static-asset detection: assets are GET responses with an asset extension or mimetype.
_ASSET_EXTS = {
    "js", "mjs", "css", "map", "svg", "png", "jpg", "jpeg", "gif", "webp",
    "ico", "woff", "woff2", "ttf", "otf", "eot",
}
_ASSET_MIMES = {"script", "css", "image", "font"}


def is_asset(item: "BurpItem") -> bool:
    """True when the item is a static asset (noise), not an API/XHR/document request."""
    if (item.method or "").upper() != "GET":
        return False
    ext = (item.extension or "").lower().lstrip(".")
    if ext in _ASSET_EXTS:
        return True
    return (item.mimetype or "").strip().lower() in _ASSET_MIMES


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def find_jwts(text: str) -> list[str]:
    """Return every JWT-looking substring (header.payload.signature), de-duplicated in order."""
    if not text:
        return []
    seen: list[str] = []
    for m in _JWT_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def redact_tokens(text: str) -> str:
    """Replace JWT-shaped substrings with a short non-sensitive marker for DISPLAY.

    Keeps a 12-char prefix (the JWT header is not secret) so an analyst can still recognize
    and correlate the token, without dumping the full payload+signature into notes/summaries.
    Extraction paths (find_jwts/decode_jwt/harvest) operate on the raw text, not this output.
    """
    if not text:
        return text
    return _JWT_RE.sub(lambda m: m.group(0)[:12] + "…<JWT redacted>", text)


def _b64url(seg: str) -> bytes:
    return _base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def decode_jwt(token: str) -> dict | None:
    """Decode a JWT into {alg, header, payload, sig_bytes}. Never returns the raw token.

    Returns None if the token is not a well-formed three-segment JWT.
    """
    if not token or token.count(".") != 2:
        return None
    h, p, s = token.split(".")
    try:
        header = json.loads(_b64url(h))
        payload = json.loads(_b64url(p))
        sig_bytes = len(_b64url(s))
    except Exception:
        return None
    return {"alg": header.get("alg"), "header": header, "payload": payload, "sig_bytes": sig_bytes}


def endpoint_slug(method: str, path: str) -> str:
    """Canonical note filename stem, e.g. ('POST','/api/v1/users') -> 'post_api_v1_users'.

    The query string is dropped; the same endpoint therefore maps to one stable note across
    the capture-observed pass and any later target pass.
    """
    only_path = _urlsplit(path or "").path.strip("/")
    base = f"{method}_{only_path}" if only_path else f"{method}_root"
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug or "endpoint"
