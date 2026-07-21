#!/usr/bin/env python3
"""Inspect a Burp Suite XML capture — sliced, never dumped (LLM-friendly).

Subcommands:
    hosts   <xml>                             distinct hosts + counts
    index   <xml> [--apis-only] [--method M] [--status S] [--url-contains T] [--format json]
    view    <xml> --index N [slices...]       one item, header/body slices (truncated)
    harvest <xml> [--format json]             reusable headers/cookies/JWT-claims/identifiers

Examples:
    python3 burp.py index capture.xml --apis-only
    python3 burp.py view capture.xml --index 22 --resp-headers --resp-body --keys
    python3 burp.py view capture.xml --index 22 --decode-auth
    python3 burp.py harvest capture.xml --format json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _burp  # noqa: E402

_SHORT = 200  # header/cookie sample truncation


def _short(v: str, n: int = _SHORT) -> str:
    v = _burp.redact_tokens(v)
    return v if len(v) <= n else f"{v[:n]}... [+{len(v) - n} chars]"


# --------------------------------------------------------------------- hosts
def cmd_hosts(args) -> int:
    counts = Counter(it.host for it in _burp.iter_items(args.xml))
    for host, n in counts.most_common():
        print(f"{n:>5}  {host}")
    return 0


# --------------------------------------------------------------------- index
def _rows(args):
    for it in _burp.iter_items(args.xml):
        if args.apis_only and _burp.is_asset(it):
            continue
        if args.method and (it.method or "").upper() != args.method.upper():
            continue
        if args.status and args.status not in (it.status or ""):
            continue
        if args.url_contains and args.url_contains.lower() not in (it.url or "").lower():
            continue
        yield {"index": it.index, "method": it.method, "status": it.status,
               "mimetype": it.mimetype, "url": _burp.redact_tokens(it.url or "")}


def cmd_index(args) -> int:
    rows = list(_rows(args))
    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['index']:>4}  {(r['method'] or '?'):6s} {str(r['status'] or ''):>3}  "
                  f"{(r['mimetype'] or ''):8s}  {r['url']}")
    print(f"\n{len(rows)} item(s)", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------- view
def _find_item(xml_path, index):
    for it in _burp.iter_items(xml_path):
        if it.index == index:
            return it
    raise SystemExit(f"index {index} not found in {xml_path}")


def _print_headers(label, msg):
    print(f"--- {label} headers ({_burp.redact_tokens(msg.start_line)}) ---")
    for k, v in msg.headers:
        print(f"{k}: {_short(v)}")


def _print_cookies(label, msg):
    ck = msg.cookies()
    print(f"--- {label} cookies ({len(ck)}) ---")
    for n, v in ck:
        print(f"  {n} = {_short(v)}")


def _print_body(label, msg, limit, keys_only):
    if not msg.body:
        print(f"--- {label} body: empty ---")
        return
    print(f"--- {label} body ({len(msg.body)} bytes, {msg.content_type() or '?'}) ---")
    if msg.is_json():
        try:
            value = msg.body_json()
        except Exception as e:
            print(f"[json parse failed: {e}]")
            print(_burp.truncate_text(msg.body_text(), limit))
            return
        print(_burp.json_keys_summary(value) if keys_only
              else _burp.redact_tokens(_burp.truncate_text(json.dumps(value, indent=2, ensure_ascii=False), limit)))
        return
    if msg.is_text():
        print(_burp.redact_tokens(_burp.truncate_text(msg.body_text(), limit)))
        return
    print(f"[binary; first {min(len(msg.body), limit)} bytes hex]\n{msg.body[:limit].hex()}")


def _decode_auth(it):
    req = it.request
    sources = []
    auth = req.header("Authorization")
    if auth:
        sources.append(("Authorization header", auth))
    for name, val in req.cookies():
        sources.append((f"cookie {name}", val))
    sources.append(("URL", it.url or ""))
    print("--- decoded auth (claims only; raw tokens NOT shown) ---")
    seen = set()
    any_found = False
    for origin, text in sources:
        for tok in _burp.find_jwts(text):
            if tok in seen:
                continue
            seen.add(tok)
            d = _burp.decode_jwt(tok)
            if not d:
                continue
            any_found = True
            print(f"[{origin}] alg={d['alg']} sig_bytes={d['sig_bytes']}")
            print("  claims: " + json.dumps(d["payload"], ensure_ascii=False))
    if not any_found:
        print("  (no JWTs found in Authorization header, cookies, or URL)")


def cmd_view(args) -> int:
    it = _find_item(args.xml, args.index)
    resp = it.response
    print(f"# {it.method} {_burp.redact_tokens(it.url or '')}")
    print(f"  status: {it.status}  mime: {it.mimetype}")
    print(f"  request: {len(it.request.headers)} headers, {len(it.request.body)} body bytes")
    print(f"  response: {'none' if resp is None else str(len(resp.headers)) + ' headers, ' + str(len(resp.body)) + ' body bytes'}")
    # Default to a summary if no slice flag was given.
    any_slice = any([args.summary, args.req_headers, args.resp_headers, args.cookies,
                     args.req_body, args.resp_body, args.decode_auth])
    if args.req_headers:
        _print_headers("request", it.request)
    if args.resp_headers and resp is not None:
        _print_headers("response", resp)
    if args.cookies:
        _print_cookies("request", it.request)
        if resp is not None:
            _print_cookies("response", resp)
    if args.req_body:
        _print_body("request", it.request, args.limit, args.keys)
    if args.resp_body and resp is not None:
        _print_body("response", resp, args.limit, args.keys)
    if args.decode_auth:
        _decode_auth(it)
    if not any_slice:
        print("  (pass --req-headers/--resp-headers/--cookies/--req-body/--resp-body/--decode-auth to see more)")
    return 0


# ------------------------------------------------------------------- harvest
_ID_RE = re.compile(r"\b([0-9]{4,}|[0-9a-fA-F]{16,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


def harvest(xml_path: str) -> dict:
    hosts: Counter = Counter()
    req_headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    jwts: list[dict] = []
    ident: Counter = Counter()
    seen_tokens: set[str] = set()
    for it in _burp.iter_items(xml_path):
        hosts[it.host] += 1
        if _burp.is_asset(it):
            continue
        req = it.request
        for k, v in req.headers:
            req_headers.setdefault(k, _short(v))
        for n, v in req.cookies():
            cookies.setdefault(n, _short(v))
        for tok in _burp.find_jwts(it.url or "") + _burp.find_jwts(req.header("Authorization") or ""):
            if tok in seen_tokens:
                continue
            seen_tokens.add(tok)
            d = _burp.decode_jwt(tok)
            if d:
                jwts.append({"alg": d["alg"], "sig_bytes": d["sig_bytes"], "claims": d["payload"]})
        for m in _ID_RE.findall((it.url or "") + " " + (req.body_text() if req.is_text() else "")):
            ident[m] += 1
    return {
        "hosts": dict(hosts),
        "request_headers": req_headers,
        "cookies": cookies,
        "jwts": jwts,
        "identifiers": [i for i, _ in ident.most_common(40)],
    }


def cmd_harvest(args) -> int:
    data = harvest(args.xml)
    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print("# hosts\n" + "\n".join(f"  {h} ({n})" for h, n in data["hosts"].items()))
    print(f"\n# reusable request headers ({len(data['request_headers'])})")
    for k, v in data["request_headers"].items():
        print(f"  {k}: {v}")
    print(f"\n# cookies ({len(data['cookies'])})")
    for k, v in data["cookies"].items():
        print(f"  {k} = {v}")
    print(f"\n# decoded JWT/bearer shapes ({len(data['jwts'])}) — claims only, no raw tokens")
    for j in data["jwts"]:
        print(f"  alg={j['alg']} sig_bytes={j['sig_bytes']} claims={json.dumps(j['claims'], ensure_ascii=False)}")
    print("\n# candidate identifiers (reusable as required IDs when testing targets)")
    print("  " + ", ".join(data["identifiers"]) if data["identifiers"] else "  (none)")
    return 0


# ---------------------------------------------------------------- identities
_ID_CLAIM_KEYS = ("sub", "uid", "user_id", "userId", "aud")


def _identity_key(claims: dict):
    for k in _ID_CLAIM_KEYS:
        v = claims.get(k)
        if v:
            return str(v)
    return None


def identities(xml_path: str) -> list:
    """Enumerate distinct in-scope identities from the capture (T3). Keyed by JWT subject/audience,
    falling back to a session-cookie value. Redacted — claims + alg only, never the raw token."""
    roster: dict = {}
    for it in _burp.iter_items(xml_path):
        if _burp.is_asset(it):
            continue
        req = it.request
        toks = _burp.find_jwts(req.header("Authorization") or "") + _burp.find_jwts(it.url or "")
        key = None
        claims: dict = {}
        alg = None
        for tok in toks:
            d = _burp.decode_jwt(tok)
            if d and _identity_key(d["payload"]):
                key, claims, alg = _identity_key(d["payload"]), d["payload"], d["alg"]
                break
        if key is None:
            ck = req.cookies()
            if not ck:
                continue
            key = "cookie:" + ck[0][1][:12]
        ent = roster.setdefault(key, {
            "label": f"id-{key}"[:48], "user_ids": [], "auth": {"alg": alg, "claims": claims},
            "cookie_names": [], "source_indices": [], "user_agents": [],
        })
        for cid in [str(claims[k]) for k in _ID_CLAIM_KEYS if claims.get(k)]:
            if cid not in ent["user_ids"]:
                ent["user_ids"].append(cid)
        for n, _v in req.cookies():
            if n not in ent["cookie_names"]:
                ent["cookie_names"].append(n)
        if it.index not in ent["source_indices"]:
            ent["source_indices"].append(it.index)
        ua = req.header("User-Agent")
        if ua and ua not in ent["user_agents"]:
            ent["user_agents"].append(ua)
        if claims and not ent["auth"]["claims"]:
            ent["auth"] = {"alg": alg, "claims": claims}
    return list(roster.values())


def cmd_identities(args) -> int:
    data = identities(args.xml)
    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"# {len(data)} in-scope identity(ies)")
        for e in data:
            print(f"  {e['label']}: user_ids={e['user_ids']} alg={e['auth']['alg']} "
                  f"cookies={e['cookie_names']} source_items={e['source_indices']}")
    return 0


def cmd_cred(args) -> int:
    """Print the RAW credential value for one item — for `TOKEN=$(burp.py cred ...)` capture only."""
    it = _find_item(args.xml, args.index)
    field = args.field.lower()
    if field == "authorization":
        val = it.request.header("Authorization") or ""
    elif field == "cookie":
        val = it.request.header("Cookie") or ""
    elif field.startswith("header:"):
        val = it.request.header(args.field.split(":", 1)[1]) or ""
    else:
        raise SystemExit("--field must be authorization, cookie, or header:NAME")
    print(val)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("hosts"); p.add_argument("xml"); p.set_defaults(fn=cmd_hosts)

    p = sub.add_parser("index"); p.add_argument("xml")
    p.add_argument("--apis-only", action="store_true")
    p.add_argument("--method"); p.add_argument("--status"); p.add_argument("--url-contains")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("view"); p.add_argument("xml"); p.add_argument("--index", type=int, required=True)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--req-headers", action="store_true")
    p.add_argument("--resp-headers", action="store_true")
    p.add_argument("--cookies", action="store_true")
    p.add_argument("--req-body", action="store_true")
    p.add_argument("--resp-body", action="store_true")
    p.add_argument("--keys", action="store_true")
    p.add_argument("--decode-auth", action="store_true")
    p.add_argument("--limit", type=int, default=2000)
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("harvest"); p.add_argument("xml")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(fn=cmd_harvest)

    p = sub.add_parser("identities"); p.add_argument("xml")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(fn=cmd_identities)

    p = sub.add_parser("cred"); p.add_argument("xml"); p.add_argument("--index", type=int, required=True)
    p.add_argument("--field", default="authorization")
    p.set_defaults(fn=cmd_cred)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
