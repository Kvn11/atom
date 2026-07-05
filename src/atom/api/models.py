"""Request/response models for the workflow API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
