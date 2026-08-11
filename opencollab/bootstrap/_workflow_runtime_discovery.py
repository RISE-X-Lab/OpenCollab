"""Bounded discovery of decorated workflow functions."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import stat
import sys
import threading
import uuid
from functools import wraps
from types import ModuleType

from opencollab.adapters.safe_files import read_regular_text
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap._workflow_runtime_state import (
    MAX_WORKFLOW_DIRECTORY_ENTRIES,
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_SOURCE_BYTES,
)

_WORKFLOW_IMPORT_STATE_LOCK = threading.RLock()


class _WorkflowSourceLoader(importlib.abc.Loader):
    """Execute the already bounded source through an importlib module spec."""

    def __init__(self, path: str, source: str) -> None:
        self._path = path
        self._source = source

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        exec(compile(self._source, self._path, "exec"), module.__dict__)


class _WorkflowImportFinder(importlib.abc.MetaPathFinder):
    """Resolve local workflow imports through the bounded source loader."""

    def __init__(self) -> None:
        self._package_roots: dict[str, str] = {}

    def register(self, package_name: str, directory: str) -> None:
        with _WORKFLOW_IMPORT_STATE_LOCK:
            self._package_roots[package_name] = directory
            if self not in sys.meta_path:
                sys.meta_path.insert(0, self)

    def unregister(self, package_name: str) -> None:
        with _WORKFLOW_IMPORT_STATE_LOCK:
            self._package_roots.pop(package_name, None)
            if not self._package_roots:
                try:
                    sys.meta_path.remove(self)
                except ValueError:
                    pass

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        package_name, separator, relative_name = fullname.partition(".")
        with _WORKFLOW_IMPORT_STATE_LOCK:
            directory = self._package_roots.get(package_name)
        if directory is None or not separator:
            return None
        parts = relative_name.split(".")
        if not parts or any(not part.isidentifier() for part in parts):
            return None

        module_path = os.path.join(directory, *parts) + ".py"
        package_path = os.path.join(directory, *parts, "__init__.py")
        for candidate, is_package in ((package_path, True), (module_path, False)):
            if not os.path.lexists(candidate):
                continue
            source = read_regular_text(candidate, max_bytes=MAX_WORKFLOW_SOURCE_BYTES)
            loader = _WorkflowSourceLoader(candidate, source)
            spec = importlib.util.spec_from_loader(
                fullname,
                loader,
                origin=candidate,
                is_package=is_package,
            )
            if spec is None:
                raise ImportError(f"could not create import spec for workflow source: {candidate}")
            if is_package:
                spec.submodule_search_locations = []
            return spec
        package_directory = os.path.join(directory, *parts)
        if os.path.lexists(package_directory):
            current = directory
            for part in parts:
                current = os.path.join(current, part)
                inspected = os.lstat(current)
                if not stat.S_ISDIR(inspected.st_mode):
                    raise ValueError(f"workflow package is not a real directory: {current}")
            spec = importlib.machinery.ModuleSpec(
                fullname,
                loader=None,
                is_package=True,
            )
            spec.submodule_search_locations = []
            return spec
        return None


_WORKFLOW_IMPORT_FINDER = _WorkflowImportFinder()


def _remove_workflow_package(package_name: str) -> None:
    with _WORKFLOW_IMPORT_STATE_LOCK:
        prefix = f"{package_name}."
        for registered_name in tuple(sys.modules):
            if registered_name == package_name or registered_name.startswith(prefix):
                sys.modules.pop(registered_name, None)
        _WORKFLOW_IMPORT_FINDER.unregister(package_name)


class _WorkflowModuleOwner:
    """Keep workflow modules alive while exposing them only during execution."""

    def __init__(self, package_name: str, directory: str) -> None:
        self.package_name = package_name
        self.directory = directory
        self._modules: dict[str, ModuleType] = {}
        self._active_calls = 0
        self._lock = threading.Lock()

    def capture_modules(self) -> None:
        prefix = f"{self.package_name}."
        with _WORKFLOW_IMPORT_STATE_LOCK:
            modules = sys.modules.copy()
        self._modules = {
            name: module
            for name, module in modules.items()
            if isinstance(module, ModuleType)
            and (name == self.package_name or name.startswith(prefix))
        }

    def acquire(self) -> None:
        with self._lock:
            if self._active_calls == 0:
                with _WORKFLOW_IMPORT_STATE_LOCK:
                    _WORKFLOW_IMPORT_FINDER.register(self.package_name, self.directory)
                    sys.modules.update(self._modules)
            self._active_calls += 1

    def release(self) -> None:
        with self._lock:
            self._active_calls -= 1
            if self._active_calls == 0:
                self.capture_modules()
                _remove_workflow_package(self.package_name)


def _bind_runtime_package(spec: WorkflowSpec, owner: _WorkflowModuleOwner) -> WorkflowSpec:
    original = spec.fn

    @wraps(original)
    async def run(ctx, args):
        owner.acquire()
        try:
            return await original(ctx, args)
        finally:
            owner.release()

    bound = WorkflowSpec(
        name=spec.name,
        description=spec.description,
        fn=run,
        phases=spec.phases,
    )
    run.__workflow_spec__ = bound  # type: ignore[attr-defined]
    return bound


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


def _load_workflow_specs(path: str) -> list[WorkflowSpec]:
    """Import one workflow file and bind its package to returned specs."""
    source = read_regular_text(path, max_bytes=MAX_WORKFLOW_SOURCE_BYTES)
    package_name = f"_opencollab_workflow_{uuid.uuid4().hex}"
    module_name = f"{package_name}.workflow"
    package_spec = importlib.machinery.ModuleSpec(
        package_name,
        loader=None,
        is_package=True,
    )
    package_spec.submodule_search_locations = []
    package = importlib.util.module_from_spec(package_spec)
    loader = _WorkflowSourceLoader(path, source)
    module_spec = importlib.util.spec_from_loader(
        module_name,
        loader,
        origin=path,
    )
    if module_spec is None:
        raise ImportError(f"could not create import spec for workflow source: {path}")
    module = importlib.util.module_from_spec(module_spec)

    with _WORKFLOW_IMPORT_STATE_LOCK:
        _WORKFLOW_IMPORT_FINDER.register(
            package_name,
            os.path.dirname(os.path.abspath(path)),
        )
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
            if isinstance(wf_spec, WorkflowSpec) and id(wf_spec) not in seen:
                seen.add(id(wf_spec))
                found.append(wf_spec)
    except BaseException:
        _remove_workflow_package(package_name)
        raise
    owner = _WorkflowModuleOwner(
        package_name,
        os.path.dirname(os.path.abspath(path)),
    )
    owner.capture_modules()
    bound = [_bind_runtime_package(spec, owner) for spec in found]
    _remove_workflow_package(package_name)
    return bound


def load_workflow_specs(path: str) -> list[WorkflowSpec]:
    """Import one bounded workflow file and collect its workflow specs.

    Each workflow gets a unique execution-scoped package. This keeps normal
    import metadata available while the workflow defines dataclasses or imports
    sibling modules without retaining global import registrations between runs.
    Every local source uses the same bounded, regular-file-only loader.
    """
    return _load_workflow_specs(path)


_load_specs_from_file = load_workflow_specs

__all__ = ["discover_workflows", "load_workflow_specs"]
