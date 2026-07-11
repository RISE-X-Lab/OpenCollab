"""Bounded discovery of decorated workflow functions."""

from __future__ import annotations

import os
import stat
import types
import uuid

from opencollab.adapters.safe_files import read_regular_text
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap._workflow_runtime_state import (
    MAX_WORKFLOW_DIRECTORY_ENTRIES,
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_SOURCE_BYTES,
)


def discover_workflows(directory: str) -> Registry:
    """Load every ``@workflow``-decorated function under ``directory``.

    Imports each top-level ``*.py`` file (skipping dunder/private names) via
    importlib and registers every function carrying a ``__workflow_spec__``. A
    missing directory yields an empty registry.
    """
    registry = Registry()
    directory = os.path.abspath(directory)
    try:
        inspected = os.lstat(directory)
    except FileNotFoundError:
        return registry
    if not stat.S_ISDIR(inspected.st_mode):
        raise ValueError(f"workflow path is not a real directory: {directory}")

    filenames: list[str] = []
    with os.scandir(directory) as entries:
        scanned = 0
        for entry in entries:
            scanned += 1
            if scanned > MAX_WORKFLOW_DIRECTORY_ENTRIES:
                raise ValueError(f"workflow directory entries exceed limit of {MAX_WORKFLOW_DIRECTORY_ENTRIES}")
            filename = entry.name
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(f"workflow source is not a regular file: {entry.path}")
            if entry.stat(follow_symlinks=False).st_size > MAX_WORKFLOW_SOURCE_BYTES:
                raise ValueError(f"workflow source exceeds {MAX_WORKFLOW_SOURCE_BYTES}-byte limit: {entry.path}")
            filenames.append(filename)
            if len(filenames) > MAX_WORKFLOW_FILES:
                raise ValueError(f"workflow files exceed limit of {MAX_WORKFLOW_FILES}")

    for filename in sorted(filenames):
        path = os.path.join(directory, filename)
        for spec in _load_specs_from_file(path):
            registry.register(spec)
    return registry


def _load_specs_from_file(path: str) -> list[WorkflowSpec]:
    """Import a single python file and collect its workflow specs."""
    module_name = f"_opencollab_workflow_{uuid.uuid4().hex}"
    source = read_regular_text(path, max_bytes=MAX_WORKFLOW_SOURCE_BYTES)
    module = types.ModuleType(module_name)
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)

    # Dedupe by spec identity: a decorated function bound under more than one
    # module-level name (an alias or a re-export) carries the SAME spec object
    # under each name. Collecting both would register the same name twice and
    # abort discovery of the whole directory, so keep one entry per spec.
    found: list[WorkflowSpec] = []
    seen: set[int] = set()
    for value in vars(module).values():
        wf_spec = getattr(value, "__workflow_spec__", None)
        if isinstance(wf_spec, WorkflowSpec) and id(wf_spec) not in seen:
            seen.add(id(wf_spec))
            found.append(wf_spec)
    return found
