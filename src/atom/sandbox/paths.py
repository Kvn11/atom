"""ATOM_HOME resolution and the per-thread on-disk directory layout.

The agent always works in terms of *virtual* paths (``/mnt/user-data/workspace`` …);
this module computes the *physical* directories those map to. Path confinement /
resolution lives in :mod:`atom.sandbox.provider`.

Physical layout under ``$ATOM_HOME`` (default ``~/.atom``)::

    $ATOM_HOME/
        atom.sqlite                         # checkpointer db
        skills/<name>/SKILL.md              # always-on skills (shared)
        skill_library/<name>/SKILL.md       # deferred skills (shared)
        tool_library/<name>/                # deferred tools (shared)
        users/<user_id>/threads/<thread_id>/user-data/
            workspace/                      # (new mode) per-thread scratch
            uploads/                        # read-only inputs
            outputs/                        # deliverables
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = "~/.atom"

# Virtual mount points the model sees.
VIRTUAL_WORKSPACE = "/mnt/user-data/workspace"
VIRTUAL_UPLOADS = "/mnt/user-data/uploads"
VIRTUAL_OUTPUTS = "/mnt/user-data/outputs"
VIRTUAL_SKILLS = "/mnt/skills"
VIRTUAL_SKILL_LIBRARY = "/mnt/skill_library"


def atom_home(config_home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the atom home dir: ``ATOM_HOME`` env > ``config_home`` > ``~/.atom``."""
    raw = os.environ.get("ATOM_HOME") or (str(config_home) if config_home else None) or DEFAULT_HOME
    return Path(raw).expanduser().resolve()


@dataclass(frozen=True)
class ThreadPaths:
    """Resolved physical directories for one thread."""

    home: Path
    user_id: str
    thread_id: str
    workspace: Path
    uploads: Path
    outputs: Path
    # Shared (not per-thread):
    skills: Path
    skill_library: Path
    tool_library: Path
    # True when ``workspace`` points at a caller-supplied external dir (reuse mode).
    workspace_is_external: bool

    def ensure(self) -> "ThreadPaths":
        """Create all per-thread dirs (and shared dirs). Never mkdir an external workspace root
        beyond ensuring it exists; never touch its contents."""
        for d in (self.uploads, self.outputs, self.skills, self.skill_library, self.tool_library):
            d.mkdir(parents=True, exist_ok=True)
        # An external (reuse) workspace must already exist; a new one we create.
        if not self.workspace_is_external:
            self.workspace.mkdir(parents=True, exist_ok=True)
        return self

    def virtual_map(self) -> dict[str, Path]:
        """Map each virtual mount prefix to its physical directory."""
        return {
            VIRTUAL_WORKSPACE: self.workspace,
            VIRTUAL_UPLOADS: self.uploads,
            VIRTUAL_OUTPUTS: self.outputs,
            VIRTUAL_SKILLS: self.skills,
            VIRTUAL_SKILL_LIBRARY: self.skill_library,
        }


def thread_paths(
    user_id: str,
    thread_id: str,
    *,
    home: str | os.PathLike[str] | None = None,
    workspace_override: str | os.PathLike[str] | None = None,
    uploads_override: str | os.PathLike[str] | None = None,
) -> ThreadPaths:
    """Compute :class:`ThreadPaths` for a thread.

    ``workspace_override`` (an absolute path) binds an *existing* external directory as the
    workspace (reuse mode); otherwise a fresh per-thread ``workspace/`` is used (new mode).
    ``uploads_override`` (an absolute path) binds a shared external ``uploads/`` (per-run
    uploads) instead of the per-thread default. Directories are not created here — call
    :meth:`ThreadPaths.ensure`.
    """
    h = atom_home(home)
    base = h / "users" / user_id / "threads" / thread_id / "user-data"
    external = workspace_override is not None
    workspace = Path(workspace_override).expanduser().resolve() if external else base / "workspace"
    uploads = (
        Path(uploads_override).expanduser().resolve()
        if uploads_override is not None
        else base / "uploads"
    )
    return ThreadPaths(
        home=h,
        user_id=user_id,
        thread_id=thread_id,
        workspace=workspace,
        uploads=uploads,
        outputs=base / "outputs",
        skills=h / "skills",
        skill_library=h / "skill_library",
        tool_library=h / "tool_library",
        workspace_is_external=external,
    )


def thread_paths_from_context(ctx: dict, home_default: str | None = None) -> ThreadPaths:
    """Build :class:`ThreadPaths` from a WorkspaceContext dict (used by middleware)."""
    mode = ctx.get("workspace_mode", "new")
    override = ctx.get("workspace_path") if mode == "existing" else None
    return thread_paths(
        ctx.get("user_id", "default"),
        ctx["thread_id"],
        home=ctx.get("home") or home_default,
        workspace_override=override,
        uploads_override=ctx.get("uploads_path"),
    )
