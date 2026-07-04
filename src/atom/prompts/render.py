"""Resolve ``@file``-or-inline prompt refs and render them with Jinja2.

This is deviation #10: system/user prompts are set at run time from config (or CLI), not baked
into the harness. A prompt value is either an inline string or ``@<path>`` (resolved against the
config dir, then the packaged ``atom/`` dir, then absolute).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]  # .../src/atom
# StrictUndefined: a typo'd or unprovided variable in an operator-authored prompt raises loudly
# at render time instead of silently rendering as an empty string. Use `| default(...)` for
# intentionally-optional variables.
_env = Environment(
    autoescape=False, trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined
)


def resolve_prompt_ref(ref: str, config_dir: str | None = None) -> str:
    """Return prompt text: inline string as-is, or the contents of an ``@file`` ref."""
    if not ref.startswith("@"):
        return ref
    rel = ref[1:]
    p = Path(rel).expanduser()
    if p.is_absolute() and p.exists():
        return p.read_text(encoding="utf-8")
    bases = [Path(config_dir)] if config_dir else []
    bases.append(_PACKAGE_ROOT)
    for base in bases:
        candidate = base / rel
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"Prompt file '{rel}' not found (looked in {', '.join(str(b) for b in bases)})."
    )


def apply_prompt_template(text: str, ctx: dict[str, Any]) -> str:
    """Render ``text`` as a Jinja2 template with ``ctx``."""
    return _env.from_string(text).render(**ctx)


def render_prompt(ref: str, ctx: dict[str, Any], config_dir: str | None = None) -> str:
    """Resolve an ``@file``-or-inline ref and render it."""
    return apply_prompt_template(resolve_prompt_ref(ref, config_dir), ctx)
