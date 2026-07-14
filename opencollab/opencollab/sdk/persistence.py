"""Stable run-directory and transcript-persistence helpers."""

from __future__ import annotations

from opencollab.adapters.storage import SessionStore
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    make_run_dir,
    workflow_transcript_path,
)


def reserve_run_directory(workspace: str, *, prefix: str = "") -> str:
    """Atomically reserve one unique OpenCollab run directory."""
    return make_run_dir(workspace, prefix=prefix)


__all__ = [
    "AutoSaveSubscriber",
    "ORCHESTRATION_FILENAME",
    "SessionStore",
    "WORKFLOW_MANIFEST_FILENAME",
    "reserve_run_directory",
    "workflow_transcript_path",
]
