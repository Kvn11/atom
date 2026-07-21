#!/usr/bin/env python3
"""File a markdown note into the domain-split assessment vault — no shell-quoting of note bodies.

The agent writes the note body to the workspace with the normal file tool, then files it here:
    endpoint note -> <root>/<domain>/endpoints/<slug>.md
    recon note    -> <root>/<domain>/recon.md

Subcommands:
    slug "<METHOD> <path>"                          print the canonical filename stem
    put (--vault NAME | --root DIR) --domain D --slug S --from FILE [--kind endpoint|recon] [--overwrite]

--vault resolves the vault's on-disk path via `obsidian vault=<NAME> vault info=path`.
--root passes the vault directory directly (used in tests / when the path is already known).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _burp  # noqa: E402


def compute_slug(method_and_path: str) -> str:
    parts = method_and_path.strip().split(None, 1)
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    return _burp.endpoint_slug(method, path)


def _default_runner(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout


def resolve_root(vault: str, runner=None) -> str:
    runner = runner or _default_runner
    out = runner(["obsidian", f"vault={vault}", "vault", "info=path"])
    # The CLI prints the path (possibly with a label); take the last non-empty line's path-looking token.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line:
            return line.split("\t")[-1].strip()
    raise SystemExit(f"could not resolve on-disk path for vault '{vault}'")


def write_note(root: str, domain: str, slug: str, kind: str, body: str, overwrite: bool = True) -> Path:
    domain = domain.strip().strip("/")
    if not domain:
        raise SystemExit("domain is required")
    if kind == "recon":
        target = Path(root) / domain / "recon.md"
    else:
        target = Path(root) / domain / "endpoints" / f"{slug}.md"
    if target.exists() and not overwrite:
        raise SystemExit(f"refusing to overwrite existing note: {target} (pass --overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def cmd_slug(args) -> int:
    print(compute_slug(args.spec))
    return 0


def cmd_put(args) -> int:
    root = args.root or resolve_root(args.vault)
    body = Path(args.from_file).read_text(encoding="utf-8")
    slug = args.slug or "note"
    p = write_note(root, args.domain, slug, args.kind, body, overwrite=args.overwrite)
    print(f"wrote {p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("slug"); p.add_argument("spec"); p.set_defaults(fn=cmd_slug)
    p = sub.add_parser("put")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True)
    p.add_argument("--slug", default="")
    p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--kind", choices=["endpoint", "recon"], default="endpoint")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(fn=cmd_put)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
