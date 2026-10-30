"""Duo solves a task through two isolated candidates and evidence-based selection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opencollab.patches import patch_paths, split_patch_blocks
from opencollab.workflows import workflow

from ._candidate_evidence_files import CandidateEvidenceFiles
from ._dual_coder import run_dual_coder
from ._file_selection import adjudicate_candidate_files
from ._prompts import _PROMPT_REVISION, SELECTION_PROMPT


def _source_evidence_root(ctx: Any, parent: str | None) -> Path | None:
    workspace = getattr(ctx, "host_workspace", None)
    if parent is None or not isinstance(workspace, str) or not workspace:
        return None
    workspace_path = Path(workspace).resolve()
    parent_path = Path(parent).resolve()
    for root in (workspace_path, *workspace_path.parents):
        if (root / ".git").exists():
            return root if parent_path.is_relative_to(root) else None
    return None

def _without_owned_evidence(source: str, directory: str) -> str:
    retained = []
    for block in split_patch_blocks(source):
        patch = "".join(block)
        paths = patch_paths(patch)
        if paths and all(path == directory or path.startswith(directory + "/") for path in paths):
            continue
        retained.append(patch)
    return "".join(retained)

@workflow(
    name="duo",
    description="Two focused solutions with complete evidence and read-only selection",
    phases=["candidate-a", "candidate-b", "mechanical-selection", "adjudication", "adoption"],
)
async def duo(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Run Duo using task-oriented prompts and complete candidate evidence.

    Supply ``goal`` (or ``description``) with the complete task as text.
    Example inputs: {"goal": "Fix the failing parser test"}.
    ``candidate_evidence_dir`` optionally supplies a host-side parent directory.
    Each adjudication creates its own retained evidence directory.
    ``submission_mode="working_tree"`` declares caller-owned patch capture and
    later submission. The default ``"task"`` follows task-specific delivery.
    ``allow_unisolated_shell`` defaults to false and follows the caller's shell policy.
    """
    evidence_parent = args.get("candidate_evidence_dir")
    evidence_root = _source_evidence_root(ctx, evidence_parent)
    files = CandidateEvidenceFiles(evidence_parent) if evidence_root is not None else None
    source_reader = None
    evidence_exclusions = ()
    if files is not None:
        directory = files.directory.resolve()
        evidence_exclusions = (directory.relative_to(evidence_root).as_posix(),)
        workspace = Path(ctx.host_workspace).resolve()
        excluded = (directory.relative_to(workspace).as_posix(),) if directory.is_relative_to(workspace) else ()

        async def source_reader() -> str | None:
            if excluded:
                source = await ctx.diff(exclude_paths=excluded)
            else:
                # Evidence outside the exposed workspace has no public relative
                # exclusion. Read the existing source port's repository patch.
                read = getattr(getattr(ctx, "_candidate_workspace", None), "source_diff", None)
                source = await read() if callable(read) else None
            if source is None:
                raise RuntimeError("candidate_workspace_tracking_failure: source diff unavailable for Duo evidence")
            return _without_owned_evidence(source, directory.relative_to(evidence_root).as_posix())

    physical_source_reader = None
    source_tracker = None
    candidate_workspace = getattr(ctx, "_candidate_workspace", None)
    track_source = getattr(candidate_workspace, "track_source", None)
    read_physical = getattr(candidate_workspace, "physical_source_revision", None)
    if callable(track_source):
        async def source_tracker(patch: str | None) -> Any:
            return await track_source(patch, exclude_paths=evidence_exclusions)
    elif callable(read_physical):
        async def physical_source_reader() -> str | None:
            return await read_physical(exclude_paths=evidence_exclusions)

    async def adjudicator(context: Any, **options: Any) -> tuple[str, Any, str]:
        return await adjudicate_candidate_files(
            context, **options, evidence_parent=evidence_parent, evidence_files=files,
        )

    result = await run_dual_coder(
        ctx, args, selector_prompt=SELECTION_PROMPT, adjudicator=adjudicator,
        source_reader=source_reader, physical_source_reader=physical_source_reader,
        source_tracker=source_tracker,
    )
    result["prompt_revision"] = _PROMPT_REVISION
    return result

__all__ = ["duo"]
