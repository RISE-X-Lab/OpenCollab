"""Contract tests for the compact public Python API."""

from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

import pytest

import opencollab
import opencollab.sdk as sdk
from opencollab.environments import Environment, attach_container
from opencollab.tools import Tool
from opencollab.workflows import WorkflowContext


def test_root_and_sdk_export_one_small_surface() -> None:
    expected = ["OpenCollab", "RunError", "RunResult", "workflow"]
    assert opencollab.__version__ == "0.4.0"
    assert opencollab.__all__ == expected
    assert sdk.__all__ == expected
    assert all(getattr(opencollab, name) is getattr(sdk, name) for name in expected)

    retired = (
        "SDK_API_VERSION",
        "AgentRunRequest",
        "WorkflowRunRequest",
        "OpenCollabRuntime",
        "RuntimeConfig",
    )
    assert all(not hasattr(sdk, name) for name in retired)


def test_public_class_and_method_shapes_stay_lean() -> None:
    expected_parameters = {
        sdk.OpenCollab: (
            "workspace",
            "model",
            "provider",
            "api_key",
            "base_url",
            "config",
            "environment",
        ),
        sdk.OpenCollab.agent: (
            "self",
            "prompt",
            "tools",
            "budget",
            "steps",
            "timeout",
            "artifacts",
            "trace",
            "name",
            "system_prompt",
            "llm",
        ),
        sdk.OpenCollab.team: (
            "self",
            "prompt",
            "config",
            "budget",
            "timeout",
            "artifacts",
            "trace",
            "use_worktrees",
        ),
        sdk.OpenCollab.workflow: (
            "self",
            "flow",
            "inputs",
            "budget",
            "concurrency",
            "timeout",
            "artifacts",
            "trace",
        ),
    }
    for target, expected in expected_parameters.items():
        assert tuple(inspect.signature(target).parameters) == expected

    assert tuple(field.name for field in fields(sdk.RunResult)) == (
        "output",
        "status",
        "reason",
        "tokens",
        "artifacts",
        "error",
        "metrics",
    )


def test_run_result_has_one_status_and_one_error_model(tmp_path: Path) -> None:
    completed = sdk.RunResult(output="done", status="completed", tokens=4)
    assert completed.ok
    assert completed.raise_for_status() is completed

    failed = sdk.RunResult(
        output=None,
        status="failed",
        reason="provider failed",
        artifacts=tmp_path,
        error=ValueError("secret detail"),
    )
    assert not failed.ok
    assert "secret detail" not in repr(failed)
    with pytest.raises(sdk.RunError, match="provider failed") as captured:
        failed.raise_for_status()
    assert captured.value.result is failed


def test_workflow_decorator_is_minimal_but_keeps_explicit_metadata() -> None:
    @sdk.workflow
    async def draft(_ctx, inputs):
        """Draft the answer."""
        return inputs

    assert draft.__workflow_spec__.name == "draft"
    assert draft.__workflow_spec__.description == "Draft the answer."
    assert draft.__workflow_spec__.phases == ()

    @sdk.workflow(name="review", description="Review it", phases=["read"])
    async def review(_ctx, inputs):
        return inputs

    assert review.__workflow_spec__.name == "review"
    assert review.__workflow_spec__.description == "Review it"
    assert review.__workflow_spec__.phases == ("read",)


def test_advanced_capabilities_live_in_small_opt_in_modules() -> None:
    import opencollab.environments as environments
    import opencollab.tools as tools
    import opencollab.workflows as workflows

    assert tools.__all__ == ["Tool"]
    assert environments.__all__ == ["Environment", "attach_container"]
    assert workflows.__all__ == ["WorkflowContext", "workflow"]
    assert Tool is not None
    assert Environment is not None
    assert WorkflowContext is not None


def test_attach_container_validates_non_owning_workspace() -> None:
    environment = attach_container(
        container_id="container-1",
        workspace="/testbed",
    )
    assert environment.workspace == "/testbed"

    with pytest.raises(ValueError, match="absolute"):
        attach_container(container_id="container-1", workspace="testbed")
    with pytest.raises(ValueError, match="normalized"):
        attach_container(container_id="container-1", workspace="/tmp/../testbed")
    with pytest.raises(ValueError, match="container_id"):
        attach_container(container_id=" container-1 ", workspace="/testbed")


def test_client_rejects_invalid_workspace_or_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        sdk.OpenCollab(tmp_path / "missing")
    with pytest.raises(TypeError, match="mapping"):
        sdk.OpenCollab(tmp_path, config=object())
