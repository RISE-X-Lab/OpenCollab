"""Tests for the workflow registry, the @workflow decorator, and discovery.

The registry is pure application-layer stdlib (decorator metadata + a
duplicate-rejecting Registry). Discovery loads ``@workflow``-decorated functions
from a directory of python files; it lives in bootstrap (it uses importlib to
import arbitrary files), so it is exercised here against a tmp_path directory.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

from opencollab.application.workflow_registry import (
    Registry,
    WorkflowSpec,
    workflow,
)
from opencollab.bootstrap import _workflow_runtime_discovery as workflow_discovery


def test_workflow_decorator_attaches_frozen_spec_metadata():
    @workflow(name="triage", description="Triage incoming issues", phases=["scan", "rank"])
    async def fn(ctx, args):
        return "ok"

    spec = fn.__workflow_spec__
    assert isinstance(spec, WorkflowSpec)
    assert spec.name == "triage"
    assert spec.description == "Triage incoming issues"
    assert spec.phases == ("scan", "rank")
    assert spec.fn is fn
    # The decorated function is still directly callable.
    assert callable(fn)


def test_workflow_spec_is_frozen():
    @workflow(name="x", description="d")
    async def fn(ctx, args):
        return None

    with pytest.raises(Exception):
        fn.__workflow_spec__.name = "y"  # type: ignore[misc]


def test_workflow_decorator_defaults_phases_to_empty_tuple():
    @workflow(name="bare", description="no phases")
    async def fn(ctx, args):
        return None

    assert fn.__workflow_spec__.phases == ()


def test_registry_register_and_get_roundtrip():
    @workflow(name="solo", description="d")
    async def fn(ctx, args):
        return None

    reg = Registry()
    reg.register(fn.__workflow_spec__)
    assert reg.get("solo") is fn.__workflow_spec__


def test_registry_rejects_duplicate_names():
    @workflow(name="dup", description="first")
    async def a(ctx, args):
        return None

    @workflow(name="dup", description="second")
    async def b(ctx, args):
        return None

    reg = Registry()
    reg.register(a.__workflow_spec__)
    with pytest.raises(ValueError, match="dup"):
        reg.register(b.__workflow_spec__)


def test_registry_get_unknown_raises_keyerror():
    reg = Registry()
    with pytest.raises(KeyError):
        reg.get("missing")


def test_registry_list_specs_sorted_by_name():
    @workflow(name="zeta", description="z")
    async def z(ctx, args):
        return None

    @workflow(name="alpha", description="a")
    async def a(ctx, args):
        return None

    reg = Registry()
    reg.register(z.__workflow_spec__)
    reg.register(a.__workflow_spec__)
    names = [s.name for s in reg.list_specs()]
    assert names == ["alpha", "zeta"]


# -- discovery ------------------------------------------------------------- #


def _write_workflow_module(directory, filename: str, body: str) -> None:
    (directory / filename).write_text(textwrap.dedent(body), encoding="utf-8")


def test_discover_workflows_collects_decorated_functions(tmp_path):
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "first.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="first", description="the first workflow")
        async def run(ctx, args):
            return "first-result"
        """,
    )
    _write_workflow_module(
        wf_dir,
        "second.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="second", description="the second workflow", phases=["a"])
        async def run(ctx, args):
            return "second-result"
        """,
    )

    reg = discover_workflows(str(wf_dir))
    names = [s.name for s in reg.list_specs()]
    assert names == ["first", "second"]
    assert reg.get("second").phases == ("a",)


def test_discover_workflows_ignores_non_workflow_files(tmp_path):
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "helper.py",
        """
        def not_a_workflow():
            return 1
        """,
    )
    _write_workflow_module(
        wf_dir,
        "real.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="real", description="d")
        async def run(ctx, args):
            return None
        """,
    )

    reg = discover_workflows(str(wf_dir))
    assert [s.name for s in reg.list_specs()] == ["real"]


def test_discover_workflows_skips_dunder_and_private_files(tmp_path):
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "__init__.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="should_be_skipped", description="d")
        async def run(ctx, args):
            return None
        """,
    )
    _write_workflow_module(
        wf_dir,
        "keep.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="keep", description="d")
        async def run(ctx, args):
            return None
        """,
    )

    reg = discover_workflows(str(wf_dir))
    assert [s.name for s in reg.list_specs()] == ["keep"]


def test_discover_workflows_missing_dir_returns_empty_registry(tmp_path):
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    reg = discover_workflows(str(tmp_path / "does-not-exist"))
    assert reg.list_specs() == []


def test_discover_workflows_dedupes_aliased_workflow(tmp_path):
    """A decorated function bound under two module-level names (alias /
    re-export) carries the SAME spec; discovery must dedupe by spec identity
    rather than abort the whole directory on a duplicate-name registration.
    """
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "aliased.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="alpha", description="a")
        async def alpha(ctx, args):
            return None

        # alias / re-export of the SAME decorated function object
        alpha_alias = alpha
        """,
    )

    reg = discover_workflows(str(wf_dir))
    assert [s.name for s in reg.list_specs()] == ["alpha"]


def test_load_workflow_specs_supports_dataclasses_and_relative_imports(tmp_path):
    """Discovery modules get a package context while they are executing."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "helpers.py",
        '''
        DESCRIPTION = "loaded through a relative import"
        ''',
    )
    _write_workflow_module(
        wf_dir,
        "with_context.py",
        '''
        from __future__ import annotations

        from dataclasses import dataclass

        from .helpers import DESCRIPTION
        from opencollab.application.workflow_registry import workflow

        @dataclass
        class Arguments:
            message: str = DESCRIPTION

        @workflow(name="with_context", description=Arguments().message)
        async def run(ctx, args):
            return None
        ''',
    )

    specs = workflow_discovery.load_workflow_specs(str(wf_dir / "with_context.py"))

    assert [spec.name for spec in specs] == ["with_context"]
    assert specs[0].description == "loaded through a relative import"


@pytest.mark.asyncio
async def test_loaded_workflow_keeps_runtime_relative_imports_available(tmp_path):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(wf_dir, "_runtime_helper.py", 'VALUE = "runtime helper"')
    _write_workflow_module(
        wf_dir,
        "runtime_import.py",
        '''
        from opencollab.application.workflow_registry import workflow

        @workflow(name="runtime-import", description="d")
        async def run(ctx, args):
            from ._runtime_helper import VALUE

            return VALUE
        ''',
    )

    specs = workflow_discovery.load_workflow_specs(str(wf_dir / "runtime_import.py"))
    module_name = specs[0].fn.__module__
    package_name = module_name.partition(".")[0]

    assert module_name not in sys.modules
    assert package_name not in sys.modules
    assert package_name not in workflow_discovery._WORKFLOW_IMPORT_FINDER._package_roots

    assert await specs[0].fn(None, {}) == "runtime helper"
    assert module_name not in sys.modules
    assert package_name not in sys.modules
    assert package_name not in workflow_discovery._WORKFLOW_IMPORT_FINDER._package_roots


@pytest.mark.asyncio
async def test_overlapping_workflow_calls_share_runtime_package_until_last_release(tmp_path):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(wf_dir, "_runtime_helper.py", 'VALUE = "runtime helper"')
    _write_workflow_module(
        wf_dir,
        "overlap.py",
        '''
        from opencollab.application.workflow_registry import workflow

        @workflow(name="overlap", description="d")
        async def run(ctx, args):
            await args["started"].put(args["name"])
            await args["release"].wait()
            from ._runtime_helper import VALUE

            return f"{args['name']} {VALUE}"
        ''',
    )
    spec = workflow_discovery.load_workflow_specs(str(wf_dir / "overlap.py"))[0]
    package_name = spec.fn.__module__.partition(".")[0]
    started = asyncio.Queue()
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    first = asyncio.create_task(
        spec.fn(None, {"name": "first", "started": started, "release": first_release})
    )
    second = asyncio.create_task(
        spec.fn(None, {"name": "second", "started": started, "release": second_release})
    )
    assert {await started.get(), await started.get()} == {"first", "second"}

    first_release.set()
    assert await first == "first runtime helper"
    assert package_name in sys.modules
    assert package_name in workflow_discovery._WORKFLOW_IMPORT_FINDER._package_roots

    second_release.set()
    assert await second == "second runtime helper"
    assert package_name not in sys.modules
    assert package_name not in workflow_discovery._WORKFLOW_IMPORT_FINDER._package_roots


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace_package", [False, True])
async def test_loaded_workflow_supports_nested_local_packages(tmp_path, namespace_package):
    wf_dir = tmp_path / "workflows"
    helpers = wf_dir / "_helpers"
    helpers.mkdir(parents=True)
    if not namespace_package:
        _write_workflow_module(helpers, "__init__.py", "")
    _write_workflow_module(helpers, "value.py", 'VALUE = "nested helper"')
    _write_workflow_module(
        wf_dir,
        "nested_import.py",
        '''
        from opencollab.application.workflow_registry import workflow

        @workflow(name="nested-import", description="d")
        async def run(ctx, args):
            from ._helpers.value import VALUE

            return VALUE
        ''',
    )

    specs = workflow_discovery.load_workflow_specs(str(wf_dir / "nested_import.py"))

    assert await specs[0].fn(None, {}) == "nested helper"


def test_repeated_loads_leave_no_global_workflow_modules_or_finder_roots(tmp_path):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "repeat.py",
        '''
        from opencollab.application.workflow_registry import workflow

        @workflow(name="repeat", description="d")
        async def run(ctx, args):
            return "ok"
        ''',
    )
    before = {name for name in sys.modules if name.startswith("_opencollab_workflow_")}

    loaded = [
        workflow_discovery.load_workflow_specs(str(wf_dir / "repeat.py"))
        for _ in range(20)
    ]

    assert all(specs[0].name == "repeat" for specs in loaded)
    assert {name for name in sys.modules if name.startswith("_opencollab_workflow_")} == before
    assert workflow_discovery._WORKFLOW_IMPORT_FINDER._package_roots == {}


@pytest.mark.asyncio
async def test_loaded_workflow_supports_function_local_dataclass(tmp_path):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "local_dataclass.py",
        '''
        from opencollab.application.workflow_registry import workflow

        @workflow(name="local-dataclass", description="d")
        async def run(ctx, args):
            from dataclasses import dataclass

            @dataclass
            class Result:
                value: str

            return Result(value="created after discovery")
        ''',
    )

    specs = workflow_discovery.load_workflow_specs(str(wf_dir / "local_dataclass.py"))

    result = await specs[0].fn(None, {})
    assert result.value == "created after discovery"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized"])
def test_relative_import_helpers_use_bounded_regular_loader(tmp_path, monkeypatch, kind):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    helper = wf_dir / "_helper.py"
    if kind == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("VALUE = 1\n", encoding="utf-8")
        helper.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(helper)

        def feed_fifo() -> None:
            try:
                helper.write_text("VALUE = 1\n", encoding="utf-8")
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=feed_fifo, daemon=True)
        writer.start()
    else:
        helper.write_text("VALUE = 1\n" + "x" * 512, encoding="utf-8")
        monkeypatch.setattr(workflow_discovery, "MAX_WORKFLOW_SOURCE_BYTES", 256)
    _write_workflow_module(
        wf_dir,
        "imports_helper.py",
        '''
        from ._helper import VALUE
        from opencollab.application.workflow_registry import workflow

        @workflow(name="imports-helper", description=str(VALUE))
        async def run(ctx, args):
            return VALUE
        ''',
    )

    try:
        with pytest.raises((OSError, ValueError), match="_helper.py"):
            workflow_discovery.load_workflow_specs(str(wf_dir / "imports_helper.py"))
    finally:
        if kind == "fifo":
            fd = os.open(helper, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
            writer.join(timeout=1)


def test_load_workflow_specs_rolls_back_registered_modules_after_failure(tmp_path, monkeypatch):
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(wf_dir, "helpers.py", "VALUE = 1")
    _write_workflow_module(
        wf_dir,
        "broken.py",
        '''
        from .helpers import VALUE

        raise RuntimeError(f"broken workflow: {VALUE}")
        ''',
    )
    module_prefix = "_opencollab_workflow_failure"
    monkeypatch.setattr(
        workflow_discovery.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="failure"),
    )

    with pytest.raises(RuntimeError, match="broken workflow: 1"):
        workflow_discovery.load_workflow_specs(str(wf_dir / "broken.py"))

    assert not any(name.startswith(module_prefix) for name in sys.modules)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized"])
def test_discover_workflows_rejects_unsafe_or_oversized_source(
    tmp_path,
    monkeypatch,
    kind,
):
    from opencollab.bootstrap import workflow_runtime

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    source = wf_dir / "unsafe.py"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
        source.symlink_to(outside)
    else:
        source.write_text("x" * 65, encoding="utf-8")
        monkeypatch.setattr(workflow_discovery, "MAX_WORKFLOW_SOURCE_BYTES", 64)

    with pytest.raises(ValueError, match="workflow source"):
        workflow_runtime.discover_workflows(str(wf_dir))


def test_discover_workflows_rejects_symlink_directory(tmp_path):
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="not a real directory"):
        discover_workflows(str(linked))


def test_discover_workflows_directory_enumeration_is_bounded(tmp_path, monkeypatch):
    from opencollab.bootstrap import workflow_runtime

    for index in range(4):
        (tmp_path / f"entry-{index}").touch()
    monkeypatch.setattr(workflow_discovery, "MAX_WORKFLOW_DIRECTORY_ENTRIES", 3)

    with pytest.raises(ValueError, match="entries exceed limit"):
        workflow_runtime.discover_workflows(str(tmp_path))
