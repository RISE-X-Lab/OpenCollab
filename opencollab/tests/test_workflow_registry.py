"""Tests for the workflow registry, the @workflow decorator, and discovery.

The registry is pure application-layer stdlib (decorator metadata + a
duplicate-rejecting Registry). Discovery loads ``@workflow``-decorated functions
from a directory of python files; it lives in bootstrap (it uses importlib to
import arbitrary files), so it is exercised here against a tmp_path directory.
"""

from __future__ import annotations

import textwrap

import pytest
from opencollab.application.workflow_registry import (
    Registry,
    WorkflowSpec,
    workflow,
)


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
