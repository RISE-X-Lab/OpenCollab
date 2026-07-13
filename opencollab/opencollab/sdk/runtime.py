"""Stable SDK wrapper around the hardened OpenCollab workflow lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Any

from opencollab.adapters.safe_files import (
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_text,
)
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap.session_factory import WORKFLOW_MANIFEST_FILENAME
from opencollab.bootstrap.workflow_runtime import run_workflow as _run_hardened_workflow

from .errors import InvalidSDKRequestError, WorkflowManifestError, WorkflowRunTimeoutError
from .models import WorkflowRunRequest, WorkflowRunResult


class _RaisedInnerTimeout:
    def __init__(self, error: TimeoutError) -> None:
        self.error = error


class OpenCollabRuntime:
    """Execute workflows through the existing hardened lifecycle boundary."""

    async def run_workflow(self, request: WorkflowRunRequest) -> WorkflowRunResult:
        """Run a workflow and return after owned runtime activity is quiescent.

        A non-null artifact directory is reserved for exactly one invocation.
        Retries must use a fresh attempt directory so evidence from separate
        executions cannot be combined.
        """
        if not isinstance(request, WorkflowRunRequest):
            raise TypeError("request must be a WorkflowRunRequest")

        workspace = request.workspace
        if workspace is None and request.environment is not None:
            workspace = getattr(request.environment, "workspace", None)
        artifact_dir = request.artifact_dir
        artifact_claim: bytes | None = None
        if artifact_dir is not None:
            artifact_claim = _claim_artifact_dir(artifact_dir)

        operation = _run_hardened_workflow(
            request.workflow,
            deepcopy(dict(request.inputs)),
            cfg=request.config.as_runtime_dict(),
            workspace=workspace,
            budget=request.budget.max_tokens,
            max_concurrency=request.budget.max_concurrency,
            save_dir=None if artifact_dir is None else str(artifact_dir),
            trace=request.trace,
            env=request.environment,
            cleanup_timeout=request.budget.cleanup_timeout_seconds,
        )
        if request.budget.timeout_seconds is None:
            output = await operation
        else:
            try:
                timed_output = await asyncio.wait_for(
                    _preserve_inner_timeout(operation),
                    timeout=request.budget.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise WorkflowRunTimeoutError(
                    f"workflow exceeded {request.budget.timeout_seconds:g} seconds"
                ) from exc
            if isinstance(timed_output, _RaisedInnerTimeout):
                raise timed_output.error
            output = timed_output

        manifest_path = None
        tokens_spent = None
        session_count = None
        if artifact_dir is not None:
            _verify_artifact_claim(artifact_dir, artifact_claim)
            manifest_path = artifact_dir / WORKFLOW_MANIFEST_FILENAME
            manifest = _read_manifest(manifest_path)
            tokens_spent = _non_negative_manifest_integer(manifest, "tokens_spent", manifest_path)
            session_count = _non_negative_manifest_integer(manifest, "sessions", manifest_path)

        return WorkflowRunResult(
            output=output,
            workflow_name=_workflow_name(request.workflow),
            tokens_spent=tokens_spent,
            session_count=session_count,
            artifact_dir=artifact_dir,
            manifest_path=manifest_path,
        )


_SDK_ARTIFACT_CLAIM_FILENAME = ".opencollab-sdk-run"


def _claim_artifact_dir(artifact_dir: Path) -> bytes:
    ensure_directory_no_symlinks(artifact_dir)
    manifest_path = artifact_dir / WORKFLOW_MANIFEST_FILENAME
    if os.path.lexists(manifest_path):
        raise InvalidSDKRequestError("artifact_dir already contains workflow evidence")
    claim = secrets.token_hex(32).encode("ascii")
    try:
        create_regular_bytes_atomic(
            artifact_dir / _SDK_ARTIFACT_CLAIM_FILENAME,
            claim,
            max_bytes=len(claim),
        )
    except FileExistsError as exc:
        raise InvalidSDKRequestError("artifact_dir is already claimed by an SDK run") from exc
    return claim


def _verify_artifact_claim(artifact_dir: Path, expected: bytes | None) -> None:
    if expected is None:
        raise WorkflowManifestError("SDK artifact claim is missing")
    claim_path = artifact_dir / _SDK_ARTIFACT_CLAIM_FILENAME
    try:
        actual = read_regular_text(claim_path, max_bytes=len(expected)).encode("ascii")
    except (OSError, UnicodeError) as exc:
        raise WorkflowManifestError(f"cannot verify SDK artifact claim: {claim_path}") from exc
    if not secrets.compare_digest(actual, expected):
        raise WorkflowManifestError(f"SDK artifact claim changed during workflow execution: {claim_path}")


async def _preserve_inner_timeout(operation: Any) -> Any:
    try:
        return await operation
    except asyncio.TimeoutError as exc:
        return _RaisedInnerTimeout(exc)


def _workflow_name(workflow: Any) -> str:
    if isinstance(workflow, WorkflowSpec):
        return workflow.name
    spec = getattr(workflow, "__workflow_spec__", None)
    if isinstance(spec, WorkflowSpec):
        return spec.name
    return getattr(workflow, "__name__", "workflow")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_regular_text(path, max_bytes=4 * 1024 * 1024))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowManifestError(f"cannot read hardened workflow manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkflowManifestError(f"hardened workflow manifest is not an object: {path}")
    return payload


def _non_negative_manifest_integer(manifest: dict[str, Any], key: str, path: Path) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowManifestError(f"hardened workflow manifest has invalid {key}: {path}")
    return value


__all__ = ["OpenCollabRuntime"]
