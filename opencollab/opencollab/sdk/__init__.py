"""Versioned public Python API for integrations built on OpenCollab."""

from __future__ import annotations

from opencollab.adapters.llm.types import model_context_window
from opencollab.application.fact_sheet import (
    build_fact_sheet,
    estimate_target_complexity,
    format_fact_sheet_hint,
    recon_pool_is_ample,
    size_recon,
)
from opencollab.application.session_run import ENFORCEMENT_OFF
from opencollab.application.submit_findings import format_findings_report
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_registry import Registry, WorkflowFn, WorkflowSpec, workflow
from opencollab.bootstrap.workflow_runtime import discover_workflows

from .environment import ExecResult, ExecutionEnvironment, attach_workspace
from .errors import (
    InvalidSDKRequestError,
    OpenCollabSDKError,
    WorkflowManifestError,
    WorkflowRunTimeoutError,
)
from .models import SDK_API_VERSION, RunBudget, RuntimeConfig, WorkflowRunRequest, WorkflowRunResult
from .runtime import OpenCollabRuntime
from .tools import (
    ApplyPatchTool,
    BashTool,
    FileReadTool,
    FileWriteTool,
    GitDiffTool,
    GrepTool,
    RunTestsTool,
    Tool,
    coding_toolset,
    verification_run_tests_tool,
)

__all__ = [
    "SDK_API_VERSION",
    "ApplyPatchTool",
    "BashTool",
    "ENFORCEMENT_OFF",
    "ExecResult",
    "ExecutionEnvironment",
    "FileReadTool",
    "FileWriteTool",
    "GitDiffTool",
    "GrepTool",
    "InvalidSDKRequestError",
    "OpenCollabRuntime",
    "OpenCollabSDKError",
    "Registry",
    "RunBudget",
    "RunTestsTool",
    "RuntimeConfig",
    "Tool",
    "WorkflowContext",
    "WorkflowFn",
    "WorkflowManifestError",
    "WorkflowRunRequest",
    "WorkflowRunResult",
    "WorkflowRunTimeoutError",
    "WorkflowSpec",
    "attach_workspace",
    "build_fact_sheet",
    "coding_toolset",
    "discover_workflows",
    "estimate_target_complexity",
    "format_fact_sheet_hint",
    "format_findings_report",
    "model_context_window",
    "recon_pool_is_ample",
    "size_recon",
    "verification_run_tests_tool",
    "workflow",
]
