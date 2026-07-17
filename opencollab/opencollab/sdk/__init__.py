"""Versioned public Python API for integrations built on OpenCollab.

The surface is this top-level facade: the names re-exported here (plus the
``environment``/``models``/``runtime``/``tools``/``usage``/``workflows``
implementation modules they come from). ``SDK_API_VERSION`` is the compatibility
contract ``test_sdk_api.py`` enforces — export set, dataclass field order, and
public signatures are all frozen against it, so a breaking change must bump the
version. Surface == contract: there are no extra submodule re-export shims.
"""

from __future__ import annotations

from .environment import CommandResult, ExecResult, ExecutionEnvironment, attach_workspace
from .errors import (
    AgentRunLifecycleError,
    AgentRunTimeoutError,
    InvalidSDKRequestError,
    OpenCollabSDKError,
    WorkflowManifestError,
    WorkflowRunLifecycleError,
    WorkflowRunTimeoutError,
)
from .models import (
    SDK_API_VERSION,
    AgentRunBudget,
    AgentRunRequest,
    AgentRunResult,
    RunBudget,
    RuntimeConfig,
    WorkflowRunRequest,
    WorkflowRunResult,
)
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
from .usage import model_context_window
from .workflows import Registry, WorkflowContext, WorkflowFn, WorkflowSpec, discover_workflows, workflow

__all__ = [
    "SDK_API_VERSION",
    "AgentRunBudget",
    "AgentRunLifecycleError",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRunTimeoutError",
    "ApplyPatchTool",
    "BashTool",
    "CommandResult",
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
    "WorkflowRunLifecycleError",
    "WorkflowRunRequest",
    "WorkflowRunResult",
    "WorkflowRunTimeoutError",
    "WorkflowSpec",
    "attach_workspace",
    "coding_toolset",
    "discover_workflows",
    "model_context_window",
    "verification_run_tests_tool",
    "workflow",
]
