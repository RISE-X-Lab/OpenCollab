"""Workflow manifest snapshotting and persistence."""

from __future__ import annotations

import copy
import os
from typing import Any, Literal

from opencollab.adapters.storage import SessionStore
from opencollab.application.ports import TracePort
from opencollab.application.workflow import WorkflowContext
from opencollab.bootstrap.session_factory import WORKFLOW_MANIFEST_FILENAME


def _workflow_manifest_payload(
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
    status: Literal["completed", "stopped", "failed"],
    reason: str | None,
    failure_type: str | None,
    evidence_complete: bool,
) -> dict[str, Any]:
    """Freeze all event-loop-owned values used by the workflow manifest."""
    return copy.deepcopy(
        {
            "workflow": name,
            "args": args,
            "sessions": len(ctx.sessions),
            "tokens_spent": ctx.budget.spent(),
            "budget_total": ctx.budget.total,
            "status": status,
            "reason": reason,
            "failure_type": failure_type,
            "evidence_complete": evidence_complete,
            "trace_enabled": tracer is not None,
            "tracer_write_error": tracer_write_error,
            "tracer_dropped_steps": tracer_dropped_steps,
            "tracer_failure": (
                f"{type(tracer_failure).__name__}: {tracer_failure}" if tracer_failure is not None else None
            ),
        }
    )


def _write_workflow_manifest(
    save_dir: str,
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
    tracer: TracePort | None,
    tracer_failure: BaseException | None,
    tracer_write_error: str | None,
    tracer_dropped_steps: int,
    status: Literal["completed", "stopped", "failed"],
    reason: str | None,
    failure_type: str | None,
    evidence_complete: bool,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Write ``<save_dir>/workflow.json`` summarising the run.

    Ties the run folder's per-role ``<seq>_<role>.json`` transcripts to the
    workflow that produced them, mirroring the chat ``team.json`` manifest.
    """
    if manifest is None:
        manifest = _workflow_manifest_payload(
            name=name,
            args=args,
            ctx=ctx,
            tracer=tracer,
            tracer_failure=tracer_failure,
            tracer_write_error=tracer_write_error,
            tracer_dropped_steps=tracer_dropped_steps,
            status=status,
            reason=reason,
            failure_type=failure_type,
            evidence_complete=evidence_complete,
        )
    SessionStore().save_manifest(os.path.join(save_dir, WORKFLOW_MANIFEST_FILENAME), manifest)
