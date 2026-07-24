"""Compact public Python API; package SemVer is its compatibility contract."""

from __future__ import annotations

from opencollab.workflows import workflow

from .client import OpenCollab
from .result import RunError, RunResult

__all__ = [
    "OpenCollab",
    "RunError",
    "RunResult",
    "workflow",
]
