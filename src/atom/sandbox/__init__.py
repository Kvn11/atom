"""Local sandbox: per-thread workspace layout, path confinement, and file ops."""

from atom.sandbox.paths import (
    ThreadPaths,
    VIRTUAL_OUTPUTS,
    VIRTUAL_SKILL_LIBRARY,
    VIRTUAL_SKILLS,
    VIRTUAL_UPLOADS,
    VIRTUAL_WORKSPACE,
    atom_home,
    thread_paths,
)
from atom.sandbox.provider import LocalSandbox, LocalSandboxProvider, PathEscapeError
from atom.sandbox import registry

__all__ = [
    "registry",
    "ThreadPaths",
    "atom_home",
    "thread_paths",
    "VIRTUAL_WORKSPACE",
    "VIRTUAL_UPLOADS",
    "VIRTUAL_OUTPUTS",
    "VIRTUAL_SKILLS",
    "VIRTUAL_SKILL_LIBRARY",
    "LocalSandbox",
    "LocalSandboxProvider",
    "PathEscapeError",
]
