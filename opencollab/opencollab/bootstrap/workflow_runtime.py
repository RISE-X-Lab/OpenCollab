"""Bootstrap wiring for the mini workflow engine.

Binds the application-layer :class:`~opencollab.application.workflow.WorkflowContext`
to the concrete ``build_session`` machinery: :class:`WorkflowSessionFactory`
implements ``WorkflowSessionFactoryPort`` by assembling a one-shot ``Agent`` +
``Session`` per ``ctx.agent`` call, with the resolved model / provider / key /
base-url flowing through.

Also owns workflow *discovery* — loading ``@workflow``-decorated functions from a
directory of python files via importlib — and the ``run_workflow`` entry point
that builds a context, runs the workflow function, and returns its result. This
is composition-root code (it knows concrete types), so it lives in bootstrap.
"""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import math
import os
import stat
import sys
import types
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safe_files import read_regular_text
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.async_timeout import isolate_tasks_from_shutdown
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.ports import (
    EventPublisherPort,
    TracePort,
)
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap import (
    _workflow_runtime_cleanup,
    _workflow_runtime_discovery,
    _workflow_runtime_execution,
    _workflow_runtime_manifest,
    _workflow_runtime_session,
    _workflow_runtime_state,
)
from opencollab.bootstrap._workflow_runtime_cleanup import (
    _abort_session_environments,
    _add_failure_note,
    _await_cleanup_despite_cancellation,
    _await_manifest_despite_cancellation,
    _close_tracer_after_late_cleanup,
    _close_tracer_capture,
    _consume_task_result,
    _defer_owned_tracer_close,
    _inspect_tracer,
    _late_tracer_owner_done,
    _merge_failure,
    _persist_workflow_manifest_owned,
    _positive_cleanup_timeout,
    _quiesce_and_finalize_workflow_context,
    _quiesce_workflow_context,
    _session_environments,
    _session_persistence_succeeded,
    _sticky_tracer_failure,
    _wait_for_context_cleanup,
    _wait_for_late_quiescence,
    _workflow_manifest_owner_done,
)
from opencollab.bootstrap._workflow_runtime_discovery import (
    _load_specs_from_file,
    discover_workflows,
)
from opencollab.bootstrap._workflow_runtime_execution import run_workflow
from opencollab.bootstrap._workflow_runtime_manifest import (
    _workflow_manifest_payload,
    _write_workflow_manifest,
)
from opencollab.bootstrap._workflow_runtime_session import (
    WorkflowSessionFactory,
    _resolve_spec_fn,
    build_workflow_context,
)
from opencollab.bootstrap._workflow_runtime_state import (
    _LATE_TRACER_FAILURES,
    _LATE_TRACER_OWNER_TASKS,
    _WORKFLOW_MANIFEST_OWNER_TASKS,
    DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    MAX_WORKFLOW_DIRECTORY_ENTRIES,
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_SOURCE_BYTES,
    WORKFLOW_AGENT_PROMPT,
)
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
)
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    build_session,
    slug_label,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent

# Backward-compatible alias shared with the evaluation harness.
_slug = slug_label

_COMPATIBILITY_MODULES = (
    _workflow_runtime_state,
    _workflow_runtime_session,
    _workflow_runtime_manifest,
    _workflow_runtime_cleanup,
    _workflow_runtime_execution,
    _workflow_runtime_discovery,
)


class _WorkflowRuntimeFacade(types.ModuleType):
    """Mirror compatibility patches into the focused runtime modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _WorkflowRuntimeFacade

__all__ = [
    "WORKFLOW_AGENT_PROMPT",
    "WorkflowSessionFactory",
    "build_session",
    "build_workflow_context",
    "discover_workflows",
    "run_workflow",
]
