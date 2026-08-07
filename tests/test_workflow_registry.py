"""Tests for the workflow registry, the @workflow decorator, and discovery.

The registry is pure application-layer stdlib (decorator metadata + a
duplicate-rejecting Registry). Discovery loads ``@workflow``-decorated functions
from a directory of python files; it lives in bootstrap (it uses importlib to
import arbitrary files), so it is exercised here against a tmp_path directory.
"""

from __future__ import annotations

import os
import sys
import textwrap
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


def test_discover_workflows_ignores_decorated_imports(tmp_path):
    from opencollab.bootstrap.workflow_runtime import discover_workflows

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    _write_workflow_module(
        wf_dir,
        "_shared.py",
        """
        from opencollab.application.workflow_registry import workflow

        @workflow(name="imported", description="shared helper")
        async def imported(ctx, args):
            return None
        """,
    )
    _write_workflow_module(
        wf_dir,
        "local.py",
        """
        from opencollab.application.workflow_registry import workflow
        from ._shared import imported

        @workflow(name="local", description="local workflow")
        async def local(ctx, args):
            return None
        """,
    )

    reg = discover_workflows(str(wf_dir))

    assert [s.name for s in reg.list_specs()] == ["local"]


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
