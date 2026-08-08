"""Bounded discovery of decorated workflow functions."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import stat
import sys
import uuid
from types import ModuleType

from opencollab.adapters.safe_files import read_regular_text
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap._workflow_runtime_state import (
    MAX_WORKFLOW_DIRECTORY_ENTRIES,
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_SOURCE_BYTES,
)


class _WorkflowSourceLoader(importlib.abc.Loader):
    """Execute the already bounded source through an importlib module spec."""

    def __init__(self, path: str, source: str) -> None:
        self._path = path
        self._source = source

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        exec(compile(self._source, self._path, "exec"), module.__dict__)


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
        for spec in load_workflow_specs(path):
            registry.register(spec)
    return registry


def load_workflow_specs(path: str) -> list[WorkflowSpec]:
    """Import one bounded workflow file and collect its workflow specs.

    Each workflow gets a unique temporary package.  This gives the module the
    normal import metadata required by dataclasses and by sibling relative
    imports, without exposing one discovered workflow to another.
    """
    source = read_regular_text(path, max_bytes=MAX_WORKFLOW_SOURCE_BYTES)
    package_name = f"_opencollab_workflow_{uuid.uuid4().hex}"
    module_name = f"{package_name}.workflow"
    package_spec = importlib.machinery.ModuleSpec(package_name, loader=None, is_package=True)
    package_spec.submodule_search_locations = [os.path.dirname(os.path.abspath(path))]
    package = importlib.util.module_from_spec(package_spec)
    loader = _WorkflowSourceLoader(path, source)
    module_spec = importlib.util.spec_from_loader(module_name, loader, origin=path)
    if module_spec is None:
        raise ImportError(f"could not create import spec for workflow source: {path}")
    module = importlib.util.module_from_spec(module_spec)

    sys.modules[package_name] = package
    sys.modules[module_name] = module
    try:
        loader.exec_module(module)

        # Dedupe by spec identity: a decorated function bound under more than one
        # module-level name (an alias or a re-export) carries the SAME spec object
        # under each name. Collecting both would register the same name twice and
        # abort discovery of the whole directory, so keep one entry per spec.
        found: list[WorkflowSpec] = []
        seen: set[int] = set()
        for value in vars(module).values():
            wf_spec = getattr(value, "__workflow_spec__", None)
            if (
                isinstance(wf_spec, WorkflowSpec)
                and wf_spec.fn.__module__ == module.__name__
                and id(wf_spec) not in seen
            ):
                seen.add(id(wf_spec))
                found.append(wf_spec)
        return found
    finally:
        prefix = f"{package_name}."
        for registered_name in tuple(sys.modules):
            if registered_name == package_name or registered_name.startswith(prefix):
                sys.modules.pop(registered_name, None)


_load_specs_from_file = load_workflow_specs

__all__ = ["discover_workflows", "load_workflow_specs"]
