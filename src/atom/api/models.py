"""Request/response models for the workflow API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    """Body for POST /api/runs/{id}/export. Both fields present -> per-task export; else whole run."""
    step: int | None = None
    task: str | None = None
