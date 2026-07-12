"""Evaluation adapter contracts and pro-lite helpers."""

from opencollab.harness.eval_adapter.models import (
    EvalResult,
    PatchCandidate,
    PreparedWorkspace,
    RunRecord,
    TaskSpec,
    WorkspaceSpec,
)
from opencollab.harness.eval_adapter.prolite import (
    DEFAULT_DATASET_NAME,
    DEFAULT_REPO_ROOT_CANDIDATES,
    PROLITE_IMAGE_PREFIX,
    classify_technical_failure,
    is_technical_failure,
    load_jsonl_dataset,
    patch_candidate_from_diff,
    select_repo_root,
    task_spec_from_row,
    workspace_spec_for_task,
)
from opencollab.harness.eval_adapter.workspace import docker_environment_for_workspace

__all__ = [
    "DEFAULT_DATASET_NAME",
    "DEFAULT_REPO_ROOT_CANDIDATES",
    "PROLITE_IMAGE_PREFIX",
    "EvalResult",
    "PatchCandidate",
    "PreparedWorkspace",
    "RunRecord",
    "TaskSpec",
    "WorkspaceSpec",
    "classify_technical_failure",
    "is_technical_failure",
    "load_jsonl_dataset",
    "patch_candidate_from_diff",
    "select_repo_root",
    "task_spec_from_row",
    "workspace_spec_for_task",
    "docker_environment_for_workspace",
]
