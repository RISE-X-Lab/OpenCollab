"""Compatibility surface for evaluation components migrating out of OpenCollab.

This module keeps the repository dependency boundary explicit while legacy
evaluation components are replaced with the smaller stable SDK abstractions.
Its names are versioned independently from :mod:`opencollab.sdk`.

Benchmark strategy and workflow-specific analysis helpers live in the
companion evaluation package.
"""

from __future__ import annotations

from opencollab.adapters import (
    _atomic_rename as atomic_rename,
)
from opencollab.adapters import (
    _owned_file_cleanup as owned_file_cleanup,
)
from opencollab.adapters import (
    retirement_registry,
    safe_files,
)
from opencollab.adapters._owned_file_cleanup import quarantine_unlink_owned_file
from opencollab.adapters.env import (
    PROCESS_OUTPUT_CAPTURE_BYTES,
    DockerEnvironment,
    Environment,
    ExecResult,
    LocalEnvironment,
    WorktreeEnvironment,
    _await_owned_operation,
)
from opencollab.adapters.git_patch import guarded_staged_diff_command
from opencollab.adapters.llm.types import DEFAULT_MAX_OUTPUT_TOKENS, LLMResponse, Usage, model_context_window
from opencollab.adapters.llm.usage_ledger import pricing_for_model, usage_cost_usd
from opencollab.adapters.repo_map import build_repo_map_via_env
from opencollab.adapters.retirement_registry import (
    INTERNAL_RETIREMENT_LOG_ENV,
    INTERNAL_RETIREMENT_WORKSPACE_ENV,
)
from opencollab.adapters.safe_files import (
    _directory_path_matches_fd,
    _open_directory_no_symlinks,
    create_regular_bytes_atomic,
    ensure_directory_no_symlinks,
    read_regular_bytes,
    regular_path_identity,
    unlink_regular_file_durable,
    write_regular_bytes_atomic,
    write_regular_file_atomic,
)
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application import autosave
from opencollab.application.async_timeout import (
    CallerTimeoutError,
    abandon_on_timeout,
    force_task_terminal,
    isolate_tasks_from_shutdown,
    run_with_bounded_shutdown,
)
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import WorkflowFn
from opencollab.bootstrap import build_session, container, get_config
from opencollab.bootstrap._workflow_runtime_discovery import _load_specs_from_file
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
    DEFAULT_TOP_P,
)
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    agent_save_path,
    make_run_dir,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_THINKING",
    "DEFAULT_THINKING_PARAMS",
    "DEFAULT_TOP_P",
    "INTERNAL_RETIREMENT_LOG_ENV",
    "INTERNAL_RETIREMENT_WORKSPACE_ENV",
    "ORCHESTRATION_FILENAME",
    "PROCESS_OUTPUT_CAPTURE_BYTES",
    "WORKFLOW_MANIFEST_FILENAME",
    "Agent",
    "ApplyPatchTool",
    "AutoSaveSubscriber",
    "BashTool",
    "CallerTimeoutError",
    "DockerEnvironment",
    "EnvWorkingTreeProbe",
    "Environment",
    "ExecResult",
    "FileReadTool",
    "FileWriteTool",
    "GitDiffTool",
    "GrepTool",
    "LocalEnvironment",
    "LLMResponse",
    "RunTestsTool",
    "Session",
    "SessionPhase",
    "SessionStore",
    "Tool",
    "Tracer",
    "Usage",
    "WorkflowBudgetExceeded",
    "WorkflowContext",
    "WorkflowFn",
    "WorktreeEnvironment",
    "_await_owned_operation",
    "_directory_path_matches_fd",
    "_open_directory_no_symlinks",
    "_load_specs_from_file",
    "abandon_on_timeout",
    "add_exception_note",
    "agent_save_path",
    "build_repo_map_via_env",
    "build_session",
    "container",
    "create_regular_bytes_atomic",
    "ensure_directory_no_symlinks",
    "force_task_terminal",
    "get_config",
    "guarded_staged_diff_command",
    "isolate_tasks_from_shutdown",
    "make_run_dir",
    "model_context_window",
    "pricing_for_model",
    "quarantine_unlink_owned_file",
    "read_regular_bytes",
    "regular_path_identity",
    "retirement_registry",
    "atomic_rename",
    "autosave",
    "owned_file_cleanup",
    "run_with_bounded_shutdown",
    "safe_files",
    "unlink_regular_file_durable",
    "usage_cost_usd",
    "workflow_transcript_path",
    "write_regular_bytes_atomic",
    "write_regular_file_atomic",
]
