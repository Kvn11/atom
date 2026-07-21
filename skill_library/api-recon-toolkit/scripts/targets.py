#!/usr/bin/env python3
"""Inspect the targets scope file — tolerant of hand-authored JSON, sliced output.

Subcommands:
    list <file> [--format json]                 domain + endpoint table (never dumps schemas)
    show <file> --index N [--part request|response|both] [--limit N]   one target's schema slice

The loader tolerates common hand-authoring slips (missing commas between members, trailing
commas, //-line comments, whole-line # comments) so a malformed file does not fail the run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys


def _strip_comments(text: str) -> str:
    """Remove //, /* */ and # comments that are OUTSIDE string literals.

    String-aware so a `//` inside a URL value (e.g. "https://x") or a `#` inside a
    pseudo-schema value (e.g. "'foo':'int' #optional") is preserved.
    """
    out = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def normalize_jsonish(text: str) -> str:
    """Best-effort repair of hand-authored JSON. Only touches structure, never string values."""
    out = _strip_comments(text).split("\n")
    # Insert a missing comma when a value-closing line is followed by a new key/element.
    for i in range(len(out) - 1):
        cur = out[i].rstrip()
        if not cur:
            continue
        # find the next non-empty line
        j = i + 1
        while j < len(out) and not out[j].strip():
            j += 1
        if j >= len(out):
            break
        nxt = out[j].lstrip()
        ends_value = cur.endswith(('"', "}", "]")) or re.search(r"[0-9eE]$", cur) \
            or cur.endswith(("true", "false", "null"))
        starts_member = nxt.startswith('"') or nxt.startswith("{")
        if ends_value and starts_member and not cur.endswith(","):
            out[i] = cur + ","
    joined = "\n".join(out)
    # Remove trailing commas before a closing bracket/brace.
    joined = re.sub(r",(\s*[}\]])", r"\1", joined)
    return joined


def load_targets(path: str) -> dict:
    raw = open(path, encoding="utf-8").read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fixed = normalize_jsonish(raw)
        try:
            data = json.loads(fixed)
            print(f"warning: {path} was not strict JSON; loaded after tolerant normalization.",
                  file=sys.stderr)
            return data
        except json.JSONDecodeError as e:
            raise SystemExit(f"could not parse {path} even after normalization: {e}\n"
                             f"--- normalized text ---\n{fixed}")


def _endpoints(data: dict) -> list:
    return data.get("api") or data.get("apis") or data.get("endpoints") or []


def cmd_list(args) -> int:
    data = load_targets(args.file)
    eps = _endpoints(data)
    rows = [{"index": i, "method": e.get("method"), "path": e.get("path"),
             "has_request": bool(e.get("request")), "has_response": bool(e.get("response"))}
            for i, e in enumerate(eps)]
    if args.format == "json":
        print(json.dumps({"domain": data.get("domain"), "count": len(rows), "api": rows}, indent=2))
    else:
        print(f"domain: {data.get('domain')}   ({len(rows)} endpoint(s))")
        for r in rows:
            flags = ("req" if r["has_request"] else "   ") + " " + ("resp" if r["has_response"] else "")
            print(f"{r['index']:>3}  {(r['method'] or '?'):6s} {r['path']}   [{flags}]")
    return 0


def cmd_show(args) -> int:
    data = load_targets(args.file)
    eps = _endpoints(data)
    if not (0 <= args.index < len(eps)):
        raise SystemExit(f"index {args.index} out of range (0..{len(eps) - 1})")
    e = eps[args.index]
    print(f"# [{args.index}] {e.get('method')} {e.get('path')}   (domain: {data.get('domain')})")

    def _emit(label, value):
        value = value or ""
        if args.limit and len(value) > args.limit:
            value = value[:args.limit] + f"\n... [truncated, {len(value) - args.limit} more chars]"
        print(f"\n## {label}\n{value}")

    if args.part in ("request", "both"):
        _emit("request schema", e.get("request"))
    if args.part in ("response", "both"):
        _emit("response schema", e.get("response"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("file")
    p.add_argument("--format", choices=["table", "json"], default="table"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("file"); p.add_argument("--index", type=int, required=True)
    p.add_argument("--part", choices=["request", "response", "both"], default="both")
    p.add_argument("--limit", type=int, default=1200); p.set_defaults(fn=cmd_show)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
