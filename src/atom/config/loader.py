"""Load and validate ``config.yaml`` (with ``${ENV}`` expansion), or return built-in defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from atom.config.schema import AtomConfig

_DEFAULT_NAMES = ("atom.yaml", "config.yaml")


def _expand_env(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``$VAR`` in string leaves."""
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        return p
    for name in _DEFAULT_NAMES:
        p = Path.cwd() / name
        if p.exists():
            return p
    return None


def load_config(path: str | os.PathLike[str] | None = None) -> AtomConfig:
    """Load config from ``path`` (or auto-discovered ``config.yaml``), else built-in defaults."""
    cfg_path = find_config(path)
    if cfg_path is None:
        return AtomConfig()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = AtomConfig.model_validate(_expand_env(raw))
    cfg.config_dir = str(cfg_path.parent)
    return cfg
